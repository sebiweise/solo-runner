"""Client for GitHub's runner scale set API (the Actions service), on urllib only.

MODE=scaleset uses it instead of polling the REST API. The protocol follows
github.com/actions/scaleset (client.go, session_client.go); what solo-runner
relies on was checked against real GitHub in the spikes described in
docs/proposals/runner-scale-sets.md.

A personal account has no organisation, so a scale set always belongs to one
repo, and every repo has its own admin connection: a registration token from
the REST API is exchanged for the URL and a short-lived admin token of the
Actions service.
"""

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .github import USER_AGENT, GitHub, GitHubError

API_VERSION = "6.0-preview"
# The long poll is held ~50 s by GitHub; the read timeout must be longer.
POLL_TIMEOUT = 120
# The admin token lasted ~16 minutes in the spike; renew it a bit before.
ADMIN_TOKEN_MARGIN = 120
ADMIN_TOKEN_FALLBACK = 600


class ScaleSetError(GitHubError):
    """An Actions service call failed; a GitHubError, so callers handle both alike."""


def token_expiry(token: str):
    """exp of a JWT (seconds since the epoch), None if it cannot be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


class ScaleSetService:
    """Scale sets, sessions, messages and JIT configs of one repo."""

    def __init__(self, github: GitHub, repo: str, opener=None, clock=time.time):
        self.github = github
        self.repo = repo
        self._open = opener or urllib.request.urlopen
        self._clock = clock
        self._lock = threading.Lock()
        self._base = ""
        self._admin = ""
        self._admin_expires = 0.0

    # --- transport --------------------------------------------------------

    def _http(self, method, url, body=None, headers=None, timeout=30):
        """(status, parsed JSON or None). HTTP errors are returned, not raised."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self._open(req, timeout=timeout) as resp:
                status, raw = getattr(resp, "status", 200), resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        text = raw.decode("utf-8-sig", "replace") if raw else ""
        try:
            return status, (json.loads(text) if text else None)
        except ValueError:
            return status, text[:300]

    def _connect(self) -> None:
        """A fresh admin connection: registration token -> Actions service URL + admin token."""
        reg = self.github.request("POST", f"/repos/{self.repo}/actions/runners/registration-token")
        api = self.github.api_url
        # A freshly minted registration token is not always accepted at once
        # (actions/scaleset retries 401/403 for this one call too).
        for attempt in range(4):
            status, conn = self._http(
                "POST",
                f"{api}/actions/runner-registration",
                {"url": f"{_web_url(api)}/{self.repo}", "runner_event": "register"},
                {"Authorization": f"RemoteAuth {reg['token']}"},
            )
            if status not in (401, 403) or attempt == 3:
                break
            time.sleep(2)
        if not 200 <= status < 300 or not isinstance(conn, dict) or not conn.get("url") or not conn.get("token"):
            raise ScaleSetError(f"{self.repo}: runner registration failed: HTTP {status}", status)
        self._base = conn["url"].rstrip("/")
        self._admin = conn["token"]
        expiry = token_expiry(self._admin)
        self._admin_expires = expiry if expiry else self._clock() + ADMIN_TOKEN_FALLBACK

    def _admin_token(self, renew: bool = False) -> str:
        with self._lock:
            if renew or not self._admin or self._clock() > self._admin_expires - ADMIN_TOKEN_MARGIN:
                self._connect()
            return self._admin

    def call(self, method, path, body=None, bearer=None, timeout=30, query=None):
        """Call the Actions service; with the admin token, a 401 renews it once."""
        token = bearer or self._admin_token()
        params = dict(query or {}, **{"api-version": API_VERSION})
        url = f"{self._base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        status, result = self._http(method, url, body, {"Authorization": f"Bearer {token}"}, timeout)
        if status == 401 and bearer is None:
            token = self._admin_token(renew=True)
            url = f"{self._base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
            status, result = self._http(method, url, body, {"Authorization": f"Bearer {token}"}, timeout)
        return status, result

    def _expect(self, what, status, result, *ok):
        if status not in ok:
            detail = result.get("message") if isinstance(result, dict) else result
            raise ScaleSetError(f"{self.repo}: {what}: HTTP {status} {detail or ''}".rstrip(), status)
        return result

    # --- scale sets -------------------------------------------------------

    def find_scale_set(self, name: str):
        status, result = self.call("GET", "_apis/runtime/runnerscalesets", query={"runnerGroupId": 1, "name": name})
        result = self._expect("list scale sets", status, result, 200) or {}
        return next((item for item in result.get("value", []) if item.get("name") == name), None)

    def ensure_scale_set(self, name: str, labels: list) -> dict:
        """The scale set `name`, created if missing, with exactly `labels`."""
        wanted = [{"name": label, "type": "System"} for label in labels]
        existing = self.find_scale_set(name)
        if existing is not None:
            have = sorted(label.get("name", "").lower() for label in existing.get("labels", []))
            if have == sorted(label.lower() for label in labels):
                return existing
            status, result = self.call(
                "PATCH", f"_apis/runtime/runnerscalesets/{existing['id']}", {"name": name, "labels": wanted}
            )
            return self._expect("update scale set", status, result, 200)
        status, result = self.call(
            "POST",
            "_apis/runtime/runnerscalesets",
            {
                "name": name,
                # The only runner group a personal repo has.
                "runnerGroupId": 1,
                "labels": wanted,
                # The image's runner is replaced by image updates, not by itself.
                "RunnerSetting": {"disableUpdate": True},
            },
        )
        return self._expect("create scale set", status, result, 200)

    def delete_scale_set(self, scale_set_id: int) -> None:
        status, result = self.call("DELETE", f"_apis/runtime/runnerscalesets/{scale_set_id}")
        self._expect("delete scale set", status, result, 204, 404)

    # --- sessions and messages --------------------------------------------

    def create_session(self, scale_set_id: int, owner: str) -> dict:
        status, result = self.call(
            "POST", f"_apis/runtime/runnerscalesets/{scale_set_id}/sessions", {"ownerName": owner}
        )
        return self._expect("create session", status, result, 200)

    def delete_session(self, scale_set_id: int, session: dict) -> None:
        status, result = self.call(
            "DELETE", f"_apis/runtime/runnerscalesets/{scale_set_id}/sessions/{session['sessionId']}"
        )
        self._expect("delete session", status, result, 204, 404)

    def refresh_session(self, scale_set_id: int, session: dict) -> dict:
        status, result = self.call(
            "PATCH", f"_apis/runtime/runnerscalesets/{scale_set_id}/sessions/{session['sessionId']}"
        )
        return self._expect("refresh session", status, result, 200)

    def get_message(self, session: dict, last_id, capacity: int):
        """One long poll: (message or None, session). The session is refreshed
        when its queue token has expired, so the caller keeps the returned one."""
        for attempt in range(2):
            url = session["messageQueueUrl"]
            if last_id is not None:
                url += ("&" if "?" in url else "?") + f"lastMessageId={last_id}"
            status, result = self._http(
                "GET",
                url,
                headers={
                    "Authorization": f"Bearer {session['messageQueueAccessToken']}",
                    "Accept": f"application/json; api-version={API_VERSION}",
                    # GitHub assigns no more jobs to the set than this.
                    "X-ScaleSetMaxCapacity": str(max(0, capacity)),
                },
                timeout=POLL_TIMEOUT,
            )
            if status == 401 and attempt == 0:
                session = self.refresh_session(session["runnerScaleSet"]["id"], session)
                continue
            if status == 202:  # the poll timed out without a message
                return None, session
            return self._expect("get message", status, result, 200), session
        raise ScaleSetError(f"{self.repo}: get message: queue token not accepted after refresh", 401)

    def delete_message(self, session: dict, message_id: int) -> None:
        base, _, query = session["messageQueueUrl"].partition("?")
        url = f"{base}/{message_id}" + (f"?{query}" if query else "")
        status, result = self._http(
            "DELETE", url, headers={"Authorization": f"Bearer {session['messageQueueAccessToken']}"}
        )
        self._expect("delete message", status, result, 204, 404)

    # --- runners ----------------------------------------------------------

    def jit_config(self, scale_set_id: int, name: str, work_folder: str) -> dict:
        """{"runner": {"id": ...}, "encoded_jit_config": ...}, like the REST call."""
        status, result = self.call(
            "POST",
            f"_apis/runtime/runnerscalesets/{scale_set_id}/generatejitconfig",
            {"name": name, "workFolder": work_folder},
        )
        result = self._expect("generate JIT config", status, result, 200)
        return {"runner": result["runner"], "encoded_jit_config": result["encodedJITConfig"]}

    def remove_runner(self, runner_id: int) -> None:
        status, result = self.call("DELETE", f"_apis/distributedtask/pools/0/agents/{runner_id}")
        # A runner that ran its job is gone already.
        self._expect("remove runner", status, result, 200, 204, 404)


def parse_jobs(message: dict) -> list:
    """The job messages batched in one queue message (its body is a JSON string)."""
    if message.get("messageType") != "RunnerScaleSetJobMessages" or not message.get("body"):
        return []
    try:
        jobs = json.loads(message["body"])
    except ValueError:
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _web_url(api_url: str) -> str:
    """https://api.github.com -> https://github.com; GHES https://host/api/v3 -> https://host."""
    if api_url.rstrip("/") == "https://api.github.com":
        return "https://github.com"
    return api_url.rstrip("/").removesuffix("/api/v3")

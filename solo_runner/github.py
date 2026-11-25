"""The few GitHub REST calls solo-runner needs, on urllib only."""

import calendar
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from .appauth import app_jwt

USER_AGENT = f"solo-runner/{os.environ.get('SOLO_RUNNER_VERSION') or __version__}"


# Run states whose runs can hold a job that is queued for a runner.
RUN_STATES = ("queued", "in_progress", "waiting", "pending")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class TokenAuth:
    """A personal access token (fine-grained or classic)."""

    kind = "token"

    def __init__(self, token: str):
        self._token = token

    def token(self, client) -> str:  # noqa: ARG002 - same signature as AppAuth
        return self._token


class AppAuth:
    """A GitHub App: JWT -> installation token, renewed 5 minutes before it expires."""

    kind = "app"

    def __init__(self, app_id: str, private_key: str, installation_id: str = ""):
        self.app_id = app_id
        self._key = private_key
        self.installation_id = installation_id
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def jwt(self) -> str:
        return app_jwt(self.app_id, self._key)

    def token(self, client) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            if not self.installation_id:
                installations = client.request("GET", "/app/installations?per_page=100", bearer=self.jwt())
                if len(installations) != 1:
                    raise GitHubError(f"the app has {len(installations)} installations; set GITHUB_APP_INSTALLATION_ID")
                self.installation_id = str(installations[0]["id"])
            result = client.request(
                "POST", f"/app/installations/{self.installation_id}/access_tokens", bearer=self.jwt()
            )
            self._token = result["token"]
            # "2026-11-17T23:00:00Z"
            self._expires = calendar.timegm(time.strptime(result["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
            return self._token


class GitHub:
    def __init__(self, auth, api_url: str = "https://api.github.com", opener=None, metrics=None):
        self.auth = auth
        self.metrics = metrics
        self.api_url = api_url.rstrip("/")
        self._open = opener or urllib.request.urlopen
        self._etags = {}
        self._etag_lock = threading.Lock()

    def request(self, method: str, path: str, body=None, cache: bool = False, bearer: str = ""):
        """Call the API. With cache=True a 304 returns the previous body (and costs no rate limit)."""
        url = path if path.startswith("https://") else self.api_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {bearer or self.auth.token(self)}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self._etag_lock:
            cached = self._etags.get(url) if cache else None
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with self._open(req, timeout=30) as resp:
                raw = resp.read()
                result = json.loads(raw) if raw else None
                etag = resp.headers.get("ETag") if resp.headers else None
                self._count(getattr(resp, "status", 200), resp.headers)
                if cache and etag:
                    with self._etag_lock:
                        self._etags[url] = (etag, result)
                return result
        except urllib.error.HTTPError as err:
            self._count(err.code, err.headers)
            if err.code == 304 and cached:
                return cached[1]
            try:
                detail = err.read().decode(errors="replace")[:300].strip()
            except (http.client.HTTPException, OSError):
                detail = ""
            raise GitHubError(f"{method} {path}: HTTP {err.code} {detail}".rstrip(), err.code) from None
        except http.client.RemoteDisconnected:
            # Both an HTTPException and a ConnectionResetError: handled as the
            # network error it is (below), not as a broken answer.
            self._count_network()
            raise
        except http.client.HTTPException as err:
            # IncompleteRead, BadStatusLine, LineTooLong ... are not OSErrors,
            # so without this they slip past every `except OSError` in the
            # callers. GitHub's front end produces them while it returns 5xx.
            self._count_network()
            raise GitHubError(f"{method} {path}: broken answer: {type(err).__name__} {err}"[:400]) from None
        except OSError:  # URLError, timeouts, resets: no answer at all
            self._count_network()
            raise

    def _count_network(self) -> None:
        if self.metrics:
            self.metrics.inc("solo_runner_github_requests_total", status="network")
            self.metrics.inc("solo_runner_github_errors_total", status="network")

    def _count(self, status, headers) -> None:
        if not self.metrics:
            return
        self.metrics.inc("solo_runner_github_requests_total", status=str(status))
        if status >= 400:
            self.metrics.inc("solo_runner_github_errors_total", status=str(status))
        remaining = headers.get("X-RateLimit-Remaining") if headers else None
        if remaining is not None and str(remaining).isdigit():
            self.metrics.set("solo_runner_github_rate_limit_remaining", int(remaining))

    def paginate(self, path: str, key=None, limit: int = 1000):
        """All items of a list endpoint (per_page=100), up to limit."""
        items, page = [], 1
        separator = "&" if "?" in path else "?"
        while len(items) < limit:
            result = self.request("GET", f"{path}{separator}per_page=100&page={page}", cache=True)
            batch = result.get(key, []) if key else result
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    # --- repositories -----------------------------------------------------

    def accessible_repos(self) -> list:
        """Every repo the credential can reach: (full_name, private, archived)."""
        if self.auth.kind == "app":
            repos = self.paginate("/installation/repositories", key="repositories")
        else:
            # A fine-grained PAT only ever sees the repos it was given;
            # a classic PAT sees everything the user owns or collaborates on.
            repos = self.paginate("/user/repos?affiliation=owner,collaborator&sort=full_name")
        return [(repo["full_name"], bool(repo.get("private")), bool(repo.get("archived"))) for repo in repos]

    def repo(self, full_name: str) -> dict:
        return self.request("GET", f"/repos/{full_name}", cache=True)

    # --- jobs and runners -------------------------------------------------

    def queued_jobs(self, repo: str) -> list:
        """Queued jobs of repo as dicts with id, name, labels, created_at, run_name."""
        jobs, seen_runs = [], set()
        # A job can be queued in a run that is already "in_progress" (it
        # `needs:` an earlier job). GitHub also reports runs as "pending" or
        # "waiting" (concurrency groups, environments with required
        # reviewers) while one of their jobs already shows "Waiting for a
        # runner to pick up this job" and has the status "queued". So all of
        # these run states are searched.
        for status in RUN_STATES:
            runs = self.request("GET", f"/repos/{repo}/actions/runs?status={status}&per_page=100", cache=True)
            for run in runs.get("workflow_runs", []):
                # A run can change state between the requests and show up twice.
                if run["id"] in seen_runs:
                    continue
                seen_runs.add(run["id"])
                listing = self.request(
                    "GET", f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", cache=True
                )
                for job in listing.get("jobs", []):
                    if job.get("status") == "queued":
                        jobs.append(
                            {
                                "id": job["id"],
                                "name": job.get("name", ""),
                                "labels": job.get("labels", []),
                                "created_at": job.get("created_at", ""),
                                "run_name": run.get("name", ""),
                            }
                        )
        return jobs

    def jit_config(self, repo: str, name: str, labels: list, work_folder: str) -> dict:
        return self.request(
            "POST",
            f"/repos/{repo}/actions/runners/generate-jitconfig",
            {
                "name": name,
                "runner_group_id": 1,  # the only group a personal repo has
                "labels": labels,
                "work_folder": work_folder,
            },
        )

    def delete_runner(self, repo: str, runner_id: int) -> None:
        try:
            self.request("DELETE", f"/repos/{repo}/actions/runners/{runner_id}")
        except GitHubError as err:
            if err.status != 404:  # a runner that ran its job is gone already
                raise

    def runners(self, repo: str) -> list:
        return self.paginate(f"/repos/{repo}/actions/runners", key="runners")

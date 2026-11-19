"""Throwaway spike: how do Runner Scale Sets behave for a personal account's repo?

Answers the open questions of docs/proposals/runner-scale-sets.md against
real GitHub, using the end-to-end test repo and token:

  Q3 labels     Is a job with `runs-on: [self-hosted, <name>]` offered to a
                repo scale set named <name>?
  Q1 unacquired If a JobAvailable is acknowledged but not acquired, is it
                offered again: in the same session, or after a new session?
  Q2 acquired   If a job is acquired but no runner ever starts, when does
                GitHub take it back, and what happens next?
  end to end    A JIT runner from the scale set runs an acquired job.

Protocol as in github.com/actions/scaleset (client.go, session_client.go).
Prints one JSON line per event; the summary at the end is what goes into the
proposal. Standard library only, like the dispatcher. Not part of the image.

Environment: GH_TOKEN (fine-grained PAT, Administration + Actions write on the
repo), E2E_REPO (owner/name), NAME (scale set name, unique per run),
E2E_WORKFLOW (the test workflow's file name).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
TOKEN = os.environ["GH_TOKEN"]
REPO = os.environ["E2E_REPO"]
NAME = os.environ["NAME"]
WORKFLOW = os.environ.get("E2E_WORKFLOW", "solo-runner-e2e.yml")
IMAGE = os.environ.get("RUNNER_IMAGE", "ghcr.io/actions/actions-runner:latest")
EXTRA_LABELS = [x for x in os.environ.get("EXTRA_LABELS", "self-hosted").split(",") if x]
SECRET_KEYS = {"token", "messageQueueAccessToken", "encodedJITConfig"}

T0 = time.monotonic()
findings = {}


def emit(event, **fields):
    print(json.dumps({"t": round(time.monotonic() - T0, 1), "event": event, **fields}, default=str), flush=True)


def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def http(method, url, body=None, headers=None, timeout=30):
    """(status, parsed JSON or text or None). Never raises for HTTP errors."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "solo-runner-spike")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    raw = raw.decode("utf-8-sig", "replace")
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw[:500]


def gh(method, path, body=None):
    return http(
        method,
        API + path,
        body,
        {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
    )


class Actions:
    """The Actions service side: admin connection, scale set, sessions."""

    def __init__(self):
        status, reg = gh("POST", f"/repos/{REPO}/actions/runners/registration-token")
        emit("registration-token", status=status)
        assert status == 201, reg
        for _ in range(5):
            # A fresh registration token is not always accepted right away
            # (actions/scaleset retries 401/403 here too).
            status, conn = http(
                "POST",
                API + "/actions/runner-registration",
                {"url": f"https://github.com/{REPO}", "runner_event": "register"},
                {"Authorization": f"RemoteAuth {reg['token']}"},
            )
            if status not in (401, 403):
                break
            time.sleep(3)
        emit("runner-registration", status=status, keys=sorted(conn or {}))
        assert 200 <= status < 300, redact(conn)
        self.base = conn["url"].rstrip("/")
        self.admin = conn["token"]
        self.scale_set = None
        self.session = None

    def call(self, method, path, body=None, bearer=None, timeout=30):
        sep = "&" if "?" in path else "?"
        url = f"{self.base}/{path.lstrip('/')}{sep}api-version=6.0-preview"
        return http(method, url, body, {"Authorization": f"Bearer {bearer or self.admin}"}, timeout)

    def create_scale_set(self):
        status, groups = self.call("GET", "_apis/runtime/runnergroups/?groupName=default")
        emit("runner-group", status=status, groups=redact(groups))
        group_id = groups["value"][0]["id"] if status == 200 and groups.get("count") else 1
        status, created = self.call(
            "POST",
            "_apis/runtime/runnerscalesets",
            {
                "name": NAME,
                "runnerGroupId": group_id,
                # Run 1 with only [NAME]: a job asking for [self-hosted, NAME] was
                # never offered. So the scale set carries self-hosted itself.
                "labels": [{"name": label, "type": "System"} for label in [NAME, *EXTRA_LABELS]],
                "RunnerSetting": {"disableUpdate": True},
            },
        )
        emit("scale-set-created", status=status, scale_set=redact(created))
        assert status == 200, created
        self.scale_set = created["id"]

    def open_session(self, owner):
        path = f"_apis/runtime/runnerscalesets/{self.scale_set}/sessions"
        status, session = self.call("POST", path, {"ownerName": owner})
        emit("session-opened", status=status, session=redact(session))
        assert status == 200, redact(session)
        self.session = session

    def close_session(self):
        if not self.session:
            return
        sid = self.session["sessionId"]
        status, _ = self.call("DELETE", f"_apis/runtime/runnerscalesets/{self.scale_set}/sessions/{sid}")
        emit("session-closed", status=status)
        self.session = None

    def poll(self, last_id, capacity):
        """One long poll: (message or None)."""
        url = self.session["messageQueueUrl"]
        if last_id is not None:
            url += ("&" if "?" in url else "?") + f"lastMessageId={last_id}"
        headers = {
            "Authorization": f"Bearer {self.session['messageQueueAccessToken']}",
            "Accept": "application/json; api-version=6.0-preview",
            "X-ScaleSetMaxCapacity": str(capacity),
        }
        status, msg = http("GET", url, headers=headers, timeout=120)
        if status == 401:
            # Queue token expired: refresh the session and try again.
            sid = self.session["sessionId"]
            s2, session = self.call("PATCH", f"_apis/runtime/runnerscalesets/{self.scale_set}/sessions/{sid}")
            emit("session-refreshed", status=s2)
            self.session = session
            return self.poll(last_id, capacity)
        if status == 202:
            return None
        assert status == 200, (status, msg)
        return msg

    def ack(self, message_id):
        base, _, query = self.session["messageQueueUrl"].partition("?")
        url = f"{base}/{message_id}" + (f"?{query}" if query else "")
        status, _ = http("DELETE", url, headers={"Authorization": f"Bearer {self.session['messageQueueAccessToken']}"})
        if status != 204:
            emit("ack-failed", status=status)

    def acquire(self, request_ids):
        status, result = self.call(
            "POST",
            f"_apis/runtime/runnerscalesets/{self.scale_set}/acquirejobs",
            request_ids,
            bearer=self.session["messageQueueAccessToken"],
        )
        emit("acquire", asked=request_ids, status=status, result=result)
        return result.get("value", []) if status == 200 and isinstance(result, dict) else []

    def jit(self, name):
        status, cfg = self.call(
            "POST",
            f"_apis/runtime/runnerscalesets/{self.scale_set}/generatejitconfig",
            {"name": name, "workFolder": "_work"},
        )
        emit("jit-config", status=status, runner=(cfg or {}).get("runner") if isinstance(cfg, dict) else cfg)
        assert status == 200, redact(cfg)
        return cfg["encodedJITConfig"]

    def delete_scale_set(self):
        if self.scale_set is None:
            return
        status, body = self.call("DELETE", f"_apis/runtime/runnerscalesets/{self.scale_set}")
        emit("scale-set-deleted", status=status, body=redact(body))


def listen(actions, seconds, capacity, state, on_available=None):
    """Poll for `seconds`, log every job message, call on_available(msg)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        msg = actions.poll(state.get("last"), capacity)
        if msg is None:
            emit("poll-empty")
            continue
        state["last"] = msg["messageId"]
        jobs = json.loads(msg["body"]) if msg.get("body") else []
        emit("message", id=msg["messageId"], type=msg.get("messageType"), statistics=msg.get("statistics"))
        for job in jobs:
            kind = job.get("messageType")
            emit(
                kind,
                request=job.get("runnerRequestId"),
                job=job.get("jobDisplayName"),
                labels=job.get("requestLabels"),
                result=job.get("result"),
                runner=job.get("runnerName"),
                run=job.get("workflowRunId"),
            )
            state.setdefault("seen", []).append((round(time.monotonic() - T0, 1), kind, job.get("runnerRequestId")))
            if kind == "JobAvailable" and on_available:
                on_available(job)
        actions.ack(msg["messageId"])


def dispatch():
    _, info = gh("GET", f"/repos/{REPO}")
    ref = info["default_branch"]
    status, body = gh(
        "POST",
        f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        {"ref": ref, "inputs": {"label": NAME, "nonce": NAME}, "return_run_details": True},
    )
    emit("dispatched", status=status, body=body)
    run_id = body.get("workflow_run_id") if isinstance(body, dict) else None
    for _ in range(30):
        if run_id:
            break
        time.sleep(2)
        _, runs = gh("GET", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=20")
        run_id = next((r["id"] for r in runs["workflow_runs"] if r["display_title"].endswith(" " + NAME)), None)
    emit("run", id=run_id, url=f"https://github.com/{REPO}/actions/runs/{run_id}")
    return run_id


def rest_jobs(run_id):
    _, body = gh("GET", f"/repos/{REPO}/actions/runs/{run_id}/jobs")
    jobs = [(j["name"], j["status"], j["conclusion"], j.get("runner_name")) for j in (body or {}).get("jobs", [])]
    emit("rest-jobs", jobs=jobs)
    return jobs


def main():
    actions = Actions()
    run_id = None
    runners = []
    try:
        actions.create_scale_set()
        actions.open_session(f"{NAME}-a")
        state = {}
        # Initial statistics come with the session itself.
        run_id = dispatch()

        # --- Q3 + Q1: listen, acknowledge, never acquire --------------------
        offered = {}

        def note(job):
            offered.setdefault(job["runnerRequestId"], []).append(round(time.monotonic() - T0, 1))

        listen(actions, 150, 2, state, note)
        findings["Q3 [self-hosted, name] offered"] = bool(offered)
        rest_jobs(run_id)
        if not offered:
            findings["Q3 note"] = "no JobAvailable within 150 s: [self-hosted, <name>] does not reach the scale set"
            return
        first = dict(offered)
        listen(actions, 300, 2, state, note)
        findings["Q1 re-offered in same session"] = {rid: times for rid, times in offered.items() if len(times) > 1}
        actions.close_session()
        actions.open_session(f"{NAME}-b")
        state = {}
        before = {rid: len(times) for rid, times in offered.items()}
        listen(actions, 120, 2, state, note)
        findings["Q1 re-offered after new session"] = [rid for rid in offered if len(offered[rid]) > before.get(rid, 0)]
        rest_jobs(run_id)

        # --- Q2: acquire one job, start no runner ---------------------------
        target = sorted(first)[0]
        got = actions.acquire([target])
        findings["Q2 acquire of an earlier offer"] = {"asked": target, "got": got}
        if not got:
            # Not acquirable any more: take the next fresh offer instead.
            fresh = []
            listen(actions, 120, 2, state, lambda job: fresh.append(job["runnerRequestId"]))
            if fresh:
                target = fresh[0]
                got = actions.acquire([target])
        acquired_at = time.monotonic() - T0
        findings["Q2 acquired"] = {"request": target, "got": got, "at": round(acquired_at, 1)}
        mark = len(state.get("seen", []))
        listen(actions, 720, 2, state, None)
        later = [s for s in state.get("seen", [])[mark:] if s[2] == target or s[1] == "JobAvailable"]
        findings["Q2 messages after acquire (t, type, request)"] = later
        rest_jobs(run_id)

        # --- end to end: acquire every offer and run a JIT runner for it ---
        def take(job):
            rid = job["runnerRequestId"]
            if not actions.acquire([rid]):
                return
            name = f"{NAME}-{len(runners) + 1}"
            cfg = actions.jit(name)
            log = open(f"runner-{len(runners) + 1}.log", "w")  # noqa: SIM115 - closed with the process below
            proc = subprocess.Popen(
                ["docker", "run", "--rm", "-e", "JIT", IMAGE, "sh", "-c", './run.sh --jitconfig "$JIT"'],
                env={**os.environ, "JIT": cfg},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            runners.append((proc, log))
            emit("runner-started", name=name, request=rid)

        listen(actions, 480, 2, state, take)
        jobs = rest_jobs(run_id)
        findings["end to end"] = jobs
        findings["messages total"] = [s[1] for s in state.get("seen", [])]
    finally:
        for proc, log in runners:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        if run_id:
            _, run = gh("GET", f"/repos/{REPO}/actions/runs/{run_id}")
            if (run or {}).get("status") != "completed":
                status, _ = gh("POST", f"/repos/{REPO}/actions/runs/{run_id}/cancel")
                emit("run-cancelled", status=status)
        try:
            actions.close_session()
        finally:
            actions.delete_scale_set()
        print("SUMMARY " + json.dumps(findings, indent=2, default=str), flush=True)


if __name__ == "__main__":
    sys.exit(main())

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
        self.connect()
        self.scale_set = None
        self.session = None

    def connect(self):
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
        self.admin_at = time.monotonic()

    def call(self, method, path, body=None, bearer=None, timeout=30):
        sep = "&" if "?" in path else "?"
        url = f"{self.base}/{path.lstrip('/')}{sep}api-version=6.0-preview"
        status, result = http(method, url, body, {"Authorization": f"Bearer {bearer or self.admin}"}, timeout)
        if status == 401 and bearer is None:
            # Run 3: the admin token expired after ~20 min (401, PublicAccess).
            emit("admin-token-expired", age=round(time.monotonic() - self.admin_at))
            self.connect()
            status, result = http(method, url, body, {"Authorization": f"Bearer {self.admin}"}, timeout)
        return status, result

    def remove_leftovers(self):
        """Scale sets of earlier spike runs (run 3 could not delete its own)."""
        status, sets = self.call("GET", "_apis/runtime/runnerscalesets?runnerGroupId=1")
        for item in (sets or {}).get("value", []) if status == 200 else []:
            if item.get("name", "").startswith("spike-"):
                s2, _ = self.call("DELETE", f"_apis/runtime/runnerscalesets/{item['id']}")
                emit("leftover-deleted", name=item["name"], status=s2)

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


def listen(actions, seconds, capacity, state, on_message=None):
    """Poll for `seconds` with X-ScaleSetMaxCapacity=capacity, log every job
    message and call on_message(statistics) after each message."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        msg = actions.poll(state.get("last"), capacity)
        if msg is None:
            emit("poll-empty", capacity=capacity)
            continue
        state["last"] = msg["messageId"]
        jobs = json.loads(msg["body"]) if msg.get("body") else []
        stats = msg.get("statistics") or {}
        emit("message", id=msg["messageId"], capacity=capacity, statistics=stats)
        for job in jobs:
            kind = job.get("messageType")
            emit(
                kind,
                job=job.get("jobDisplayName"),
                labels=job.get("requestLabels"),
                result=job.get("result"),
                runner=job.get("runnerName"),
                job_id=job.get("jobId"),
            )
            state.setdefault("seen", []).append(
                (round(time.monotonic() - T0, 1), kind, job.get("jobDisplayName"), job.get("result"))
            )
        actions.ack(msg["messageId"])
        if on_message:
            on_message(stats)


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
    # Run 2: with labels [NAME, self-hosted] GitHub assigns jobs to the scale
    # set straight away (JobAssigned), no JobAvailable/acquire step.
    # Run 3: capacity 0 holds jobs back; capacity 1 right after assigned
    # nothing in 13 min; capacity 2 assigned both after ~4.5 min. Unclear
    # whether that is capacity or a backoff, so this run:
    #   B capacity 1 from the start: is one of the two jobs assigned?
    #   D capacity 2, no runner: when is an assignment taken back (requeue)?
    #   C capacity 2: JIT runners from the scale set run the assigned jobs.
    actions = Actions()
    run_id = None
    runners = []
    try:
        actions.remove_leftovers()
        actions.create_scale_set()
        actions.open_session(f"{NAME}-a")
        state = {}
        run_id = dispatch()
        peak = {}

        def since(m):
            return state.get("seen", [])[m:]

        def track(phase):
            def inner(stats):
                peak[phase] = max(peak.get(phase, 0), stats.get("totalAssignedJobs", 0))

            return inner

        listen(actions, 360, 1, state, track("B"))
        findings["B capacity 1 from start: max totalAssignedJobs"] = peak.get("B", 0)
        findings["B messages"] = since(0)
        rest_jobs(run_id)

        mark = len(state.get("seen", []))
        listen(actions, 540, 2, state, track("D"))
        findings["D capacity 2, no runner: max totalAssignedJobs"] = peak.get("D", 0)
        findings["D messages (t, type, job, result)"] = since(mark)
        rest_jobs(run_id)

        mark = len(state.get("seen", []))

        def scale(stats):
            # Like the dockerscaleset example: as many runners as assigned jobs.
            wanted = stats.get("totalAssignedJobs", 0) - len(runners)
            for _ in range(max(0, wanted)):
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
                emit("runner-started", name=name)

        scale({"totalAssignedJobs": peak.get("D", 0)})
        listen(actions, 420, 2, state, scale)
        findings["C capacity 2 with runners: messages"] = since(mark)
        findings["C runners started"] = len(runners)
        findings["C REST jobs"] = rest_jobs(run_id)
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

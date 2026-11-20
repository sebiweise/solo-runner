import base64
import io
import json
import threading
import time
import urllib.error
import urllib.parse

import pytest
from test_dispatcher import make_config, make_runner, wait_until

from solo_runner.dispatcher import Slot
from solo_runner.github import GitHubError
from solo_runner.scaleset import ScaleSetService, _web_url, parse_jobs, token_expiry
from solo_runner.scaleset_mode import Listener, ScaleSetDispatcher, queue_seconds

# --- the Actions service client -------------------------------------------


def jwt(exp):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


class Resp:
    def __init__(self, status, body):
        self.status = status
        self._raw = b"" if body is None else json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    api_url = "https://api.github.com"

    def __init__(self):
        self.calls = []

    def request(self, method, path, **_):
        self.calls.append((method, path))
        return {"token": f"reg-{len(self.calls)}"}


class FakeActions:
    """An opener: routes (method, path) to handlers that return (status, body)."""

    def __init__(self, routes, admin_exp=10_000):
        self.routes = routes
        self.requests = []
        self.admin_exp = admin_exp
        self.registrations = 0

    def __call__(self, req, timeout):
        url = urllib.parse.urlsplit(req.full_url)
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), url.path, urllib.parse.parse_qs(url.query), body, req))
        if url.path == "/actions/runner-registration":
            self.registrations += 1
            return Resp(200, {"url": "https://pipelines.example/abc/", "token": jwt(self.admin_exp)})
        status, result = self.routes[(req.get_method(), url.path)](req, body)
        if status >= 400:
            raw = io.BytesIO(json.dumps(result).encode() if result is not None else b"")
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, raw)
        return Resp(status, result)


def service(routes, clock=lambda: 0.0, **kwargs):
    opener = FakeActions(routes, **kwargs)
    return ScaleSetService(FakeGitHub(), "me/a", opener=opener, clock=clock), opener


SETS = "/abc/_apis/runtime/runnerscalesets"


def test_connect_and_create_scale_set():
    svc, opener = service(
        {
            ("GET", SETS): lambda req, body: (200, {"count": 0, "value": []}),
            ("POST", SETS): lambda req, body: (200, {"id": 5, **body}),
        }
    )
    created = svc.ensure_scale_set("solo", ["self-hosted", "linux", "x64"])
    assert created["id"] == 5
    method, path, _, body, req = opener.requests[0]
    assert (method, path) == ("POST", "/actions/runner-registration")
    assert body == {"url": "https://github.com/me/a", "runner_event": "register"}
    assert req.get_header("Authorization") == "RemoteAuth reg-1"
    _, _, query, _, req = opener.requests[1]
    assert query == {"runnerGroupId": ["1"], "name": ["solo"], "api-version": ["6.0-preview"]}
    assert req.get_header("Authorization") == f"Bearer {jwt(10_000)}"
    post = opener.requests[2][3]
    assert post["runnerGroupId"] == 1 and post["RunnerSetting"] == {"disableUpdate": True}
    assert post["labels"] == [{"name": n, "type": "System"} for n in ("self-hosted", "linux", "x64")]


def test_existing_scale_set_is_kept_or_its_labels_updated():
    existing = {"id": 7, "name": "solo", "labels": [{"name": "Self-Hosted"}, {"name": "linux"}]}
    patched = []
    svc, opener = service(
        {
            ("GET", SETS): lambda req, body: (200, {"value": [existing]}),
            ("PATCH", f"{SETS}/7"): lambda req, body: (patched.append(body), (200, {"id": 7, **body}))[1],
        }
    )
    assert svc.ensure_scale_set("solo", ["self-hosted", "linux"]) == existing
    assert not patched
    svc.ensure_scale_set("solo", ["self-hosted", "linux", "gpu"])
    assert [label["name"] for label in patched[0]["labels"]] == ["self-hosted", "linux", "gpu"]


def test_admin_token_is_renewed_on_401_and_before_it_expires():
    answers = iter([(401, None), (200, {"value": []})])
    now = [0.0]
    svc, opener = service({("GET", SETS): lambda req, body: next(answers)}, clock=lambda: now[0], admin_exp=1000)
    assert svc.find_scale_set("solo") is None
    assert opener.registrations == 2
    assert svc.github.calls == [("POST", "/repos/me/a/actions/runners/registration-token")] * 2
    opener.routes[("GET", SETS)] = lambda req, body: (200, {"value": []})
    now[0] = 900  # within ADMIN_TOKEN_MARGIN of exp
    svc.find_scale_set("solo")
    assert opener.registrations == 3


def test_errors_are_github_errors_with_status():
    svc, _ = service({("DELETE", f"{SETS}/3"): lambda req, body: (500, {"message": "boom"})})
    with pytest.raises(GitHubError) as err:
        svc.delete_scale_set(3)
    assert err.value.status == 500 and "boom" in str(err.value)
    svc._open.routes[("DELETE", f"{SETS}/3")] = lambda req, body: (404, None)
    svc.delete_scale_set(3)  # already gone is fine


def session(token="q1"):
    return {
        "sessionId": "s1",
        "messageQueueUrl": "https://broker.example/message?sessionId=s1",
        "messageQueueAccessToken": token,
        "runnerScaleSet": {"id": 5},
    }


def test_get_message_long_poll():
    polls = []

    def poll(req, body):
        polls.append(req)
        if req.get_header("Authorization") == "Bearer q1":
            return 401, None
        return (202, None) if len(polls) == 2 else (200, {"messageId": 4, "messageType": "x"})

    svc, opener = service(
        {
            ("GET", "/message"): poll,
            ("PATCH", f"{SETS}/5/sessions/s1"): lambda req, body: (200, session("q2")),
        }
    )
    message, current = svc.get_message(session(), None, 3)
    assert message is None and current["messageQueueAccessToken"] == "q2"
    assert polls[1].get_header("X-scalesetmaxcapacity") == "3"
    message, current = svc.get_message(current, 3, -1)
    assert message["messageId"] == 4
    assert "lastMessageId=3" in polls[2].full_url and polls[2].get_header("X-scalesetmaxcapacity") == "0"


def test_delete_message_and_jit_config():
    svc, opener = service(
        {
            ("DELETE", "/message/4"): lambda req, body: (204, None),
            ("POST", f"{SETS}/5/generatejitconfig"): lambda req, body: (
                200,
                {"runner": {"id": 11, "name": body["name"]}, "encodedJITConfig": "abc"},
            ),
        }
    )
    svc.delete_message(session(), 4)
    assert opener.requests[0][2] == {"sessionId": ["s1"]}
    jit = svc.jit_config(5, "solo-a-1", "/work/slot-0")
    assert jit == {"runner": {"id": 11, "name": "solo-a-1"}, "encoded_jit_config": "abc"}
    assert opener.requests[-1][3] == {"name": "solo-a-1", "workFolder": "/work/slot-0"}


def test_helpers():
    assert token_expiry(jwt(123)) == 123.0
    assert token_expiry("not-a-jwt") is None
    assert _web_url("https://api.github.com") == "https://github.com"
    assert _web_url("https://ghe.example/api/v3") == "https://ghe.example"
    body = json.dumps([{"messageType": "JobAssigned", "jobId": "j1"}, "junk"])
    assert parse_jobs({"messageType": "RunnerScaleSetJobMessages", "body": body}) == [
        {"messageType": "JobAssigned", "jobId": "j1"}
    ]
    assert parse_jobs({"messageType": "RunnerScaleSetJobMessages", "body": "{"}) == []
    assert parse_jobs({"messageType": "Other", "body": body}) == []
    now = time.mktime(time.strptime("2026-11-19T23:41:03", "%Y-%m-%dT%H:%M:%S")) - time.timezone
    assert queue_seconds("2026-11-19T23:40:03.1234567Z", now) == 60
    assert queue_seconds("", now) is None


# --- the scale set dispatcher ---------------------------------------------


def jobs_message(message_id, *jobs, assigned=None):
    return {
        "messageId": message_id,
        "messageType": "RunnerScaleSetJobMessages",
        "body": json.dumps([{"messageType": kind, "jobId": job_id, "queueTime": t} for kind, job_id, t in jobs]),
        "statistics": {"totalAssignedJobs": len(jobs) if assigned is None else assigned},
    }


class FakeService:
    def __init__(self, messages=(), conflict=0):
        self.messages = list(messages)
        self.conflict = conflict
        self.capacities = []
        self.calls = []
        self.lock = threading.Lock()

    def _record(self, *call):
        with self.lock:
            self.calls.append(call)

    def ensure_scale_set(self, name, labels):
        self._record("ensure", name, tuple(labels))
        return {"id": 3}

    def create_session(self, scale_set_id, owner):
        if self.conflict:
            self.conflict -= 1
            raise GitHubError("conflict", 409)
        self._record("session", scale_set_id)
        return {"sessionId": "s"}

    def get_message(self, session, last_id, capacity):
        self.capacities.append(capacity)
        if self.messages:
            return self.messages.pop(0), session
        time.sleep(0.02)
        return None, session

    def delete_message(self, session, message_id):
        self._record("ack", message_id)

    def delete_session(self, scale_set_id, session):
        self._record("delete-session", scale_set_id)

    def delete_scale_set(self, scale_set_id):
        self._record("delete-set", scale_set_id)

    def jit_config(self, scale_set_id, name, work_folder):
        self._record("jit", scale_set_id, name)
        return {"runner": {"id": 9}, "encoded_jit_config": "ZmFrZQ=="}

    def remove_runner(self, runner_id):
        self._record("remove", runner_id)


class FakeRestGitHub:
    def __init__(self, repos):
        self.auth = type("A", (), {"kind": "token"})()
        self.repos = repos

    def accessible_repos(self):
        return [(name, True, False) for name in self.repos]

    def runners(self, repo):
        return []


def make_dispatcher(tmp_path, repos=("me/a",), **env):
    config = make_config(tmp_path, MODE="scaleset", **env)
    services = {}

    def factory(repo):
        services.setdefault(repo, FakeService())
        return services[repo]

    dispatcher = ScaleSetDispatcher(config, github=FakeRestGitHub(list(repos)), service_factory=factory)
    return dispatcher, services


def test_listener_tracks_assigned_started_and_completed_jobs(tmp_path):
    dispatcher, _ = make_dispatcher(tmp_path)
    listener = Listener(dispatcher, "me/a", FakeService())
    listener.handle(jobs_message(1, ("JobAssigned", "j1", "2026-11-19T10:00:02Z"), ("JobAssigned", "j2", "1")))
    assert listener.counts() == (2, 0, "1")
    listener.handle(jobs_message(2, ("JobStarted", "j2", ""), assigned=2))
    assert listener.counts() == (1, 1, "2026-11-19T10:00:02Z")
    # j1 taken back after 5 minutes: it comes again under a new id, maybe elsewhere.
    listener.handle(jobs_message(3, ("JobCompleted", "j1", ""), assigned=1))
    assert listener.counts() == (0, 1, None)
    listener.running.add("lost")
    listener.handle(jobs_message(4, assigned=0))
    assert listener.counts() == (0, 0, None)


def test_open_rebuilds_a_set_left_open_but_not_another_hosts(tmp_path):
    dispatcher, _ = make_dispatcher(tmp_path)
    svc = FakeService(conflict=1)
    listener = Listener(dispatcher, "me/a", svc)
    listener.open()
    assert [c[0] for c in svc.calls] == ["ensure", "delete-set", "ensure", "session"]
    assert svc.calls[0][1] == "test"
    svc.conflict = 1
    with pytest.raises(GitHubError, match="RUNNER_NAME_PREFIX"):
        listener.open()


class StubListener:
    def __init__(self, waiting=0, running=0, oldest=None):
        self.state = (waiting, running, oldest)

    def counts(self):
        return self.state


def record_starts(dispatcher):
    started = []

    def start(job):
        with dispatcher._lock:
            slot = Slot(dispatcher._free_index(), job["repo"])
            dispatcher._slots[slot.index] = slot
        started.append(job["repo"])

    dispatcher.start = start
    return started


def test_schedule_gives_free_slots_to_the_oldest_waiting_jobs(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("repos:\n  me/b:\n    max_concurrent: 1\n")
    dispatcher, _ = make_dispatcher(tmp_path, MAX_CONCURRENT="4", CONFIG_FILE=str(path))
    dispatcher.listeners = {
        "me/a": StubListener(2, 0, "2026-11-19T10:00:05Z"),
        "me/b": StubListener(3, 0, "2026-11-19T10:00:01Z"),
        "me/c": StubListener(0, 0),
    }
    started = record_starts(dispatcher)
    dispatcher.schedule()
    # me/b is oldest but may have one runner; me/a gets one per waiting job.
    assert started == ["me/b", "me/a", "me/a"]
    dispatcher.schedule()  # every waiting job has a runner starting
    assert len(started) == 3
    assert dispatcher.capacity_for("me/a") == 1
    assert dispatcher.capacity_for("me/b") == 0


def test_schedule_counts_running_jobs_against_their_slots(tmp_path):
    dispatcher, _ = make_dispatcher(tmp_path, MAX_CONCURRENT="3")
    started = record_starts(dispatcher)
    dispatcher.listeners = {"me/a": StubListener(1, 0, "1")}
    dispatcher.schedule()
    assert started == ["me/a"]
    # The runner took its job; a second job is assigned.
    dispatcher.listeners = {"me/a": StubListener(1, 1, "2")}
    dispatcher.schedule()
    assert started == ["me/a", "me/a"]
    dispatcher.schedule()
    assert len(started) == 2


def test_assigned_job_gets_a_runner_of_the_scale_set(tmp_path):
    make_runner(tmp_path, '[ "$2" = "ZmFrZQ==" ] || exit 3\necho "Running job: build"\n')
    dispatcher, services = make_dispatcher(tmp_path, MAX_CONCURRENT="2", LABELS="homelab")
    dispatcher.refresh_repos(force=True)
    dispatcher.sync_listeners()
    svc = services["me/a"]
    listener = dispatcher.listeners["me/a"]
    try:
        svc.messages.append(jobs_message(1, ("JobAssigned", "j1", "2026-11-19T10:00:00Z")))
        assert wait_until(lambda: listener.counts()[0] == 1)
        dispatcher.schedule()
        assert wait_until(lambda: dispatcher.active() == 0 and ("remove", 9) in svc.calls)
        assert ("ack", 1) in svc.calls
        ensure = next(c for c in svc.calls if c[0] == "ensure")
        assert ensure[1] == "test" and "homelab" in ensure[2] and "self-hosted" in ensure[2]
        jit = next(c for c in svc.calls if c[0] == "jit")
        assert jit[1] == 3 and jit[2].startswith("test-a-")
        assert 2 in svc.capacities
        assert dispatcher.healthy()
    finally:
        listener.close()
    assert ("delete-set", 3) in svc.calls
    gauges = {(name, tuple(labels.items())): value for name, labels, value in dispatcher._gauges()}
    assert gauges[("solo_runner_jobs_queued", (("repo", "me/a"),))] == 1


def test_listener_of_a_repo_no_longer_served_is_closed(tmp_path):
    dispatcher, services = make_dispatcher(tmp_path, repos=("me/a", "me/b"))
    dispatcher.refresh_repos(force=True)
    dispatcher.sync_listeners()
    assert sorted(dispatcher.listeners) == ["me/a", "me/b"]
    dispatcher.repos = ["me/a"]
    dispatcher.sync_listeners()
    assert sorted(dispatcher.listeners) == ["me/a"]
    assert ("delete-set", 3) in services["me/b"].calls
    dispatcher.listeners["me/a"].close()

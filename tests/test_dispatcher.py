import os
import stat
import threading
import time

from solo_runner.config import load
from solo_runner.dispatcher import (
    Dispatcher,
    filter_repos,
    job_matches,
    pick_jobs,
    prepare_runner_copy,
    runner_env,
)


def make_config(tmp_path, **extra):
    env = {
        "GITHUB_TOKEN": "t",
        "RUNNER_NAME_PREFIX": "test",
        "POLL_INTERVAL": "5",
        "WORK_DIR": str(tmp_path / "work"),
        "RUNNER_DIR": str(tmp_path / "runner"),
        **extra,
    }
    return load(env)


def test_job_matches(tmp_path):
    config = make_config(tmp_path, LABELS="homelab")
    arch = config.arch
    assert job_matches(["self-hosted", "homelab"], config)
    assert job_matches(["self-hosted", "Linux", arch.upper(), "homelab"], config)
    assert not job_matches(["self-hosted"], config)  # LABELS set: plain self-hosted is someone else's
    assert not job_matches(["self-hosted", "homelab", "gpu"], config)  # a label we lack
    assert not job_matches(["ubuntu-latest"], config)
    plain = make_config(tmp_path)
    assert job_matches(["self-hosted"], plain)
    assert job_matches(["homelab"], config)  # runs-on: ${{ vars.LINUX_RUNNER }} with the bare label
    assert not job_matches([], plain)


def job(repo, created, job_id):
    return {"repo": repo, "created_at": created, "id": job_id}


def test_pick_jobs_per_repo_limit():
    jobs = [job("me/a", "1", 1), job("me/a", "2", 2), job("me/b", "3", 3), job("me/a", "4", 4)]
    # me/a may get one more runner: its second job waits, me/b's younger job goes first.
    assert [j["id"] for j in pick_jobs(jobs, {}, 3, {"me/a": 1})] == [1, 3]
    assert [j["id"] for j in pick_jobs(jobs, {}, 3, {"me/a": 0})] == [3]
    # An idle me/a runner takes job 1 whatever the limit; job 2 needs room.
    assert [j["id"] for j in pick_jobs(jobs, {"me/a": 1}, 3, {"me/a": 0})] == [3]


def test_job_matches_per_repo_labels(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("repos:\n  me/gpu:\n    labels: [solo, gpu]\n")
    config = make_config(tmp_path, LABELS="solo", CONFIG_FILE=str(path))
    assert job_matches(["solo"], config, "me/a")
    assert not job_matches(["solo"], config, "me/gpu")  # the repo's labels replace LABELS
    assert job_matches(["self-hosted", "solo", "gpu"], config, "me/gpu")
    assert not job_matches(["solo", "gpu"], config, "me/a")


def test_pick_jobs_oldest_first_and_skips_jobs_idle_runners_will_take():
    jobs = [job("me/a", "3", 3), job("me/b", "1", 1), job("me/a", "2", 2), job("me/c", "4", 4)]
    assert [j["id"] for j in pick_jobs(jobs, {}, 2)] == [1, 2]
    # One runner for me/a is up and waiting: it will take one of me/a's jobs.
    assert [j["id"] for j in pick_jobs(jobs, {"me/a": 1}, 2)] == [1, 3]
    assert pick_jobs(jobs, {}, 0) == []


def test_filter_repos(tmp_path):
    public = []
    config = make_config(tmp_path, REPO_EXCLUDE="*-archive me/skip")
    repos = [
        ("me/a", True, False),
        ("me/pub", False, False),
        ("me/old", True, True),
        ("me/x-archive", True, False),
        ("me/skip", True, False),
    ]
    assert filter_repos(repos, config, public=public) == ["me/a"]
    assert public == ["me/pub"]
    allowed = make_config(tmp_path, ALLOW_PUBLIC_REPOS="true")
    assert filter_repos(repos[:2], allowed) == ["me/a", "me/pub"]


def make_runner(tmp_path, script):
    runner = tmp_path / "runner"
    (runner / "bin").mkdir(parents=True)
    (runner / "bin" / "Runner.Listener").write_text("binary")
    (runner / ".runner").write_text("stale identity")
    (runner / "_diag").mkdir()
    run = runner / "run.sh"
    run.write_text("#!/bin/sh\n" + script)
    run.chmod(run.stat().st_mode | stat.S_IEXEC)
    return runner


def test_prepare_runner_copy_skips_state(tmp_path):
    runner = make_runner(tmp_path, "exit 0\n")
    target = runner / "_slots" / "slot-0"
    prepare_runner_copy(str(runner), str(target))
    assert sorted(os.listdir(target)) == ["bin", "run.sh"]
    assert (target / "bin" / "Runner.Listener").read_text() == "binary"


class FakeGitHub:
    def __init__(self, jobs=None):
        self.auth = type("A", (), {"kind": "token"})()
        self.jobs = jobs or {}
        self.registered = []
        self.deleted = []
        self.lock = threading.Lock()

    def repo(self, name):
        return {"full_name": name, "private": True, "archived": False}

    def accessible_repos(self):
        return [(name, True, False) for name in self.jobs]

    def queued_jobs(self, repo):
        return list(self.jobs.get(repo, []))

    def jit_config(self, repo, name, labels, work_folder):
        with self.lock:
            self.registered.append((repo, name, tuple(labels), work_folder))
            return {"runner": {"id": len(self.registered)}, "encoded_jit_config": "ZmFrZQ=="}

    def delete_runner(self, repo, runner_id):
        with self.lock:
            self.deleted.append((repo, runner_id))

    def runners(self, repo):
        return []


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_runs_one_runner_per_job_and_cleans_up(tmp_path):
    # The fake runner checks it got the JIT config, "runs" a job and leaves a file behind.
    make_runner(tmp_path, '[ "$2" = "ZmFrZQ==" ] || exit 3\necho "Running job: build"\ntouch "$PWD/marker"\n')
    config = make_config(tmp_path, LABELS="homelab")
    jobs = {
        "me/a": [
            {
                "id": 1,
                "name": "build",
                "labels": ["self-hosted", "homelab"],
                "created_at": "2026-11-17T10:00:00Z",
                "run_name": "CI",
            }
        ]
    }
    gh = FakeGitHub(jobs)
    dispatcher = Dispatcher(config, github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0 and gh.deleted)
    repo, name, labels, work_folder = gh.registered[0]
    assert repo == "me/a" and name.startswith("test-a-") and "homelab" in labels
    assert work_folder == str(tmp_path / "work" / "slot-0")
    assert gh.deleted == [("me/a", 1)]
    assert os.listdir(work_folder) == []
    assert not os.path.exists(tmp_path / "runner" / "_slots" / "slot-0")


def test_concurrency_limit_across_repos(tmp_path):
    make_runner(tmp_path, 'echo "Running job: x"\nsleep 1\n')
    config = make_config(tmp_path, MAX_CONCURRENT="2")
    jobs = {
        repo: [{"id": i, "name": "j", "labels": ["self-hosted"], "created_at": str(i), "run_name": "CI"}]
        for i, repo in enumerate(["me/a", "me/b", "me/c"])
    }
    gh = FakeGitHub(jobs)
    dispatcher = Dispatcher(config, github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert dispatcher.active() == 2
    assert wait_until(lambda: len(gh.registered) == 2)
    assert sorted(r[0] for r in gh.registered) == ["me/a", "me/b"]
    dispatcher.tick()  # no free slot: nothing new
    assert len(gh.registered) == 2
    assert wait_until(lambda: dispatcher.active() == 0)


def test_idle_runner_is_stopped_and_removed(tmp_path):
    make_runner(tmp_path, "sleep 30\n")
    config = make_config(tmp_path, IDLE_TIMEOUT="30")
    config.idle_timeout = 1  # below the configurable minimum, to keep the test fast
    gh = FakeGitHub({"me/a": [{"id": 1, "name": "j", "labels": ["self-hosted"], "created_at": "1", "run_name": "CI"}]})
    dispatcher = Dispatcher(config, github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0, timeout=15)
    assert gh.deleted == [("me/a", 1)]


def test_failed_registration_frees_the_slot(tmp_path):
    make_runner(tmp_path, "exit 0\n")
    gh = FakeGitHub({"me/a": [{"id": 1, "name": "j", "labels": ["self-hosted"], "created_at": "1", "run_name": "CI"}]})

    def broken(*args):
        raise OSError("network down")

    gh.jit_config = broken
    dispatcher = Dispatcher(make_config(tmp_path), github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0)
    assert gh.deleted == []
    assert not os.path.exists(tmp_path / "runner" / "_slots" / "slot-0")


def test_explicit_repo_list_still_refuses_public_repos(tmp_path):
    gh = FakeGitHub()
    gh.repo = lambda name: {"full_name": name, "private": name != "me/public", "archived": False}
    dispatcher = Dispatcher(make_config(tmp_path, REPOS="me/a me/public"), github=gh)
    dispatcher.refresh_repos(force=True)
    assert dispatcher.repos == ["me/a"]


def test_public_repos_are_logged_only_when_the_set_changes(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr("solo_runner.dispatcher.log", lambda message, **fields: lines.append(message))
    gh = FakeGitHub()
    found = [("me/a", True, False), ("me/pub1", False, False), ("me/pub2", False, False)]
    gh.accessible_repos = lambda: list(found)
    dispatcher = Dispatcher(make_config(tmp_path), github=gh)

    dispatcher.refresh_repos(force=True)
    skipped = [line for line in lines if "public repo skipped" in line]
    assert [line.split(":")[0] for line in skipped] == ["me/pub1", "me/pub2"]
    assert "skipped 2 public repos" in lines

    # The next discovery finds the same public repos: nothing about them.
    lines.clear()
    dispatcher.refresh_repos(force=True)
    assert not any("public" in line for line in lines)

    # A new public repo is named on its own; a gone one only changes the count.
    lines.clear()
    found[1] = ("me/pub3", False, False)
    dispatcher.refresh_repos(force=True)
    assert [line for line in lines if "public" in line] == [
        "me/pub3: public repo skipped (a pull request from a fork could run code on this host; see ALLOW_PUBLIC_REPOS)",
        "skipped 2 public repos",
    ]

    lines.clear()
    del found[1:]
    dispatcher.refresh_repos(force=True)
    assert [line for line in lines if "public" in line] == ["skipped 0 public repos"]


def test_metrics_follow_a_job(tmp_path):
    make_runner(tmp_path, 'echo "Running job: build"\n')
    gh = FakeGitHub(
        {
            "me/a": [
                {
                    "id": 1,
                    "name": "j",
                    "labels": ["self-hosted"],
                    "created_at": "2026-11-17T10:00:00Z",
                    "run_name": "CI",
                }
            ]
        }
    )
    dispatcher = Dispatcher(make_config(tmp_path, MAX_CONCURRENT="3"), github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0)
    metrics = dispatcher.metrics
    for name in ("runners_started", "jobs_started", "jobs_finished"):
        assert metrics.value(f"solo_runner_{name}_total", repo="me/a") == 1
    text = metrics.render()
    assert "solo_runner_job_wait_seconds_count 1" in text
    assert 'solo_runner_jobs_queued{repo="me/a"} 1' in text
    assert "solo_runner_slots_max 3" in text and "solo_runner_slots_free 3" in text
    assert "solo_runner_last_scan_timestamp_seconds " in text


def test_metrics_count_idle_runners_and_failures(tmp_path):
    make_runner(tmp_path, "sleep 30\n")
    config = make_config(tmp_path)
    config.idle_timeout = 1
    gh = FakeGitHub({"me/a": [{"id": 1, "name": "j", "labels": ["self-hosted"], "created_at": "1", "run_name": "CI"}]})
    dispatcher = Dispatcher(config, github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0, timeout=15)
    assert dispatcher.metrics.value("solo_runner_runners_idle_total", repo="me/a") == 1
    assert dispatcher.metrics.value("solo_runner_jobs_finished_total", repo="me/a") == 0

    def broken(*args):
        raise OSError("network down")

    gh.jit_config = broken
    dispatcher.tick()
    assert wait_until(lambda: dispatcher.active() == 0)
    assert dispatcher.metrics.value("solo_runner_runner_errors_total", repo="me/a") == 1


def test_per_repo_limit_and_labels(tmp_path):
    make_runner(tmp_path, 'echo "Running job: x"\nsleep 1\n')
    path = tmp_path / "config.yaml"
    path.write_text("repos:\n  me/a:\n    max_concurrent: 1\n    labels: [solo, big]\n")
    config = make_config(tmp_path, MAX_CONCURRENT="3", LABELS="solo", CONFIG_FILE=str(path))

    def queued(repo, n, labels):
        return [
            {"id": i, "name": "j", "labels": labels, "created_at": f"{repo}-{i}", "run_name": "CI"} for i in range(n)
        ]

    gh = FakeGitHub({"me/a": queued("me/a", 3, ["solo", "big"]), "me/b": queued("me/b", 1, ["solo"])})
    dispatcher = Dispatcher(config, github=gh)
    dispatcher.refresh_repos(force=True)
    dispatcher.tick()
    assert wait_until(lambda: len(gh.registered) == 2)
    by_repo = {r[0]: r[2] for r in gh.registered}
    assert sorted(by_repo) == ["me/a", "me/b"]  # only one of me/a's three jobs
    assert by_repo["me/a"][-2:] == ("solo", "big")
    assert "big" not in by_repo["me/b"]
    gh.jobs["me/b"] = []  # taken
    dispatcher.tick()  # me/a is at its limit, its other two jobs wait
    assert len(gh.registered) == 2
    assert wait_until(lambda: dispatcher.active() == 0)


def test_runner_trace_is_off_unless_asked_for():
    # The base image sets ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT=1.
    base = {"ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT": "1", "PATH": "/usr/bin"}
    quiet = runner_env(load({"GITHUB_TOKEN": "a"}), base)
    assert "ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT" not in quiet
    assert quiet["PATH"] == "/usr/bin"
    assert base["ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT"] == "1"  # not modified
    loud = runner_env(load({"GITHUB_TOKEN": "a", "RUNNER_TRACE": "true"}), {})
    assert loud["ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT"] == "1"

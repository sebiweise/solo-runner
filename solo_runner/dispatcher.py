"""Find queued jobs in many repos and run one just-in-time runner per job.

A personal GitHub account has no organisation-level runners: a runner belongs
to exactly one repo. The dispatcher fakes a shared pool. It polls every repo
for queued jobs that this host can take, registers a just-in-time (JIT) runner
for the repo with the oldest one and runs it in a free slot. A JIT runner
takes exactly one job and is then removed by GitHub. At most MAX_CONCURRENT
runners exist at any time, across all repos; the other jobs wait in GitHub's
queue (up to 24 h) until a slot is free.

Polling instead of webhooks: the host needs no inbound port. Unchanged answers
come back as 304 thanks to ETags, and GitHub does not count those against the
rate limit.
"""

import contextlib
import fnmatch
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .github import AppAuth, GitHub, GitHubError, TokenAuth
from .log import log, passthrough

HEARTBEAT_EVERY = 60
_RUNNER_STATE = {".runner", ".credentials", ".credentials_rsaparams"}


def job_matches(job_labels, config: Config) -> bool:
    """True if every label the job asks for is one this runner has, and the job
    asks for all of the extra LABELS (so with LABELS set, plain `self-hosted`
    jobs are left to other runners). `runs-on: solo` alone is enough when
    LABELS=solo: GitHub hands a job to any runner that has all its labels."""
    asked = {label.lower() for label in job_labels}
    ours = {label.lower() for label in config.runner_labels}
    wanted = {label.lower() for label in config.labels}
    return bool(asked) and asked <= ours and wanted <= asked


def pick_jobs(jobs, idle_per_repo: dict, free: int) -> list:
    """The oldest jobs to start runners for, at most `free`.

    A runner that has been started but has not been handed a job yet will take
    one of its repo's queued jobs, so that many jobs per repo are skipped.
    """
    skipped = dict(idle_per_repo)
    picked = []
    for job in sorted(jobs, key=lambda j: (j["created_at"], j["id"])):
        if skipped.get(job["repo"], 0) > 0:
            skipped[job["repo"]] -= 1
            continue
        if len(picked) >= free:
            break
        picked.append(job)
    return picked


def filter_repos(repos, config: Config, warn=log) -> list:
    """(full_name, private, archived) -> names solo-runner may serve."""
    result = []
    for name, private, archived in repos:
        if archived:
            continue
        if any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.split("/", 1)[1], pattern)
            for pattern in config.repo_exclude
        ):
            continue
        if not private and not config.allow_public_repos:
            warn(
                f"{name}: public repo skipped (a pull request from a fork could run code on this host;"
                " see ALLOW_PUBLIC_REPOS)"
            )
            continue
        result.append(name)
    return sorted(set(result))


def clear_dir(path: str) -> None:
    """Empty path (create it if missing), keeping the directory itself."""
    os.makedirs(path, exist_ok=True)
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.remove(full)


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def prepare_runner_copy(runner_dir: str, target: str) -> None:
    """A fresh copy of the runner installation for one job, hard-linked where possible.

    Each runner writes its own state (.runner, _diag, self-updates) into its
    directory, so concurrent runners cannot share one, and a new job should not
    see what the previous one left behind.
    """
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    for entry in os.listdir(runner_dir):
        src, dst = os.path.join(runner_dir, entry), os.path.join(target, entry)
        # _work, _diag, the slots themselves and a registered runner's identity
        # are per-runner state, not part of the installation.
        if (
            entry.startswith("_")
            or entry in _RUNNER_STATE
            or os.path.abspath(src) in (os.path.abspath(target), os.path.abspath(os.path.dirname(target)))
        ):
            continue
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True, copy_function=_link_or_copy)
        elif os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            _link_or_copy(src, dst)


class Slot:
    """One running (or starting) JIT runner."""

    def __init__(self, index: int, repo: str):
        self.index = index
        self.repo = repo
        self.name = ""
        self.runner_id = None
        self.got_job = threading.Event()
        self.process = None
        self.thread = None


class Dispatcher:
    def __init__(self, config: Config, github: GitHub = None):
        self.config = config
        if github is None:
            auth = (
                AppAuth(config.app_id, config.app_private_key, config.app_installation_id)
                if config.app_id
                else TokenAuth(config.token)
            )
            github = GitHub(auth, config.api_url)
        self.github = github
        self.repos = []
        self._next_discovery = 0.0
        self._slots = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        # Set when a slot frees up, so the next job starts without waiting
        # for the rest of POLL_INTERVAL.
        self._wake = threading.Event()
        self._last_beat = 0.0

    # --- repos ------------------------------------------------------------

    def refresh_repos(self, force: bool = False) -> None:
        if not force and time.monotonic() < self._next_discovery:
            return
        if self.config.repos:
            found = []
            for name in self.config.repos:
                try:
                    info = self.github.repo(name)
                except GitHubError as err:
                    log(f"{name}: cannot read repo, skipped: {err}")
                    continue
                found.append((info["full_name"], bool(info.get("private")), bool(info.get("archived"))))
        else:
            found = self.github.accessible_repos()
        repos = filter_repos(found, self.config)
        if repos != self.repos:
            log(f"serving {len(repos)} repos", repos=" ".join(repos))
        self.repos = repos
        self._next_discovery = time.monotonic() + self.config.discovery_interval

    # --- heartbeat --------------------------------------------------------

    def heartbeat(self) -> None:
        """GET HEARTBEAT_URL at most once a minute; a failed ping is only logged."""
        if not self.config.heartbeat_url or time.monotonic() - self._last_beat < HEARTBEAT_EVERY:
            return
        self._last_beat = time.monotonic()
        try:
            req = urllib.request.Request(self.config.heartbeat_url, headers={"User-Agent": "solo-runner"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, OSError) as err:
            log(f"heartbeat failed: {err}")

    # --- scanning ---------------------------------------------------------

    def _scan(self, repo):
        try:
            return [
                dict(job, repo=repo) for job in self.github.queued_jobs(repo) if job_matches(job["labels"], self.config)
            ]
        except (GitHubError, OSError, KeyError, ValueError) as err:
            return err

    def scan(self):
        """(matching queued jobs of all repos, True if every repo answered)."""
        jobs, ok = [], True
        if not self.repos:
            return jobs, ok
        with ThreadPoolExecutor(max_workers=min(len(self.repos), 16)) as pool:
            for repo, found in zip(self.repos, pool.map(self._scan, self.repos), strict=True):
                if isinstance(found, Exception):
                    log(f"{repo}: {found}")
                    ok = False
                else:
                    jobs.extend(found)
        return jobs, ok

    # --- slots ------------------------------------------------------------

    def _free_index(self) -> int:
        index = 0
        while index in self._slots:
            index += 1
        return index

    def active(self) -> int:
        with self._lock:
            return len(self._slots)

    def idle_per_repo(self) -> dict:
        with self._lock:
            counts = {}
            for slot in self._slots.values():
                if not slot.got_job.is_set():
                    counts[slot.repo] = counts.get(slot.repo, 0) + 1
            return counts

    def start(self, job) -> Slot:
        with self._lock:
            slot = Slot(self._free_index(), job["repo"])
            self._slots[slot.index] = slot
        slot.thread = threading.Thread(target=self._run_slot, args=(slot,), name=f"slot-{slot.index}", daemon=True)
        slot.thread.start()
        return slot

    def _run_slot(self, slot: Slot) -> None:
        try:
            self.run_runner(slot)
        except Exception as err:  # noqa: BLE001 - a slot must always be freed
            log(f"{slot.repo}: runner {slot.name or '(not registered)'} failed: {err}", slot=slot.index)
        finally:
            with self._lock:
                self._slots.pop(slot.index, None)
            self._wake.set()

    def run_runner(self, slot: Slot) -> None:
        config = self.config
        work = os.path.join(config.work_dir, f"slot-{slot.index}")
        runner_copy = os.path.join(config.slots_dir, f"slot-{slot.index}")
        try:
            clear_dir(work)
            prepare_runner_copy(config.runner_dir, runner_copy)

            short = slot.repo.split("/", 1)[1]
            slot.name = f"{config.name_prefix}-{short}-{int(time.time())}-{slot.index}"[:64]
            # Same path inside and outside the container (see README): the host's
            # Docker daemon resolves a job's `docker run -v "$PWD:..."` there.
            jit = self.github.jit_config(slot.repo, slot.name, config.runner_labels, work)
            slot.runner_id = jit["runner"]["id"]
            log(f"{slot.repo}: started runner {slot.name}", slot=slot.index)

            env = dict(os.environ)
            if os.geteuid() == 0:
                env["RUNNER_ALLOW_RUNASROOT"] = "1"
            slot.process = subprocess.Popen(
                ["./run.sh", "--jitconfig", jit["encoded_jit_config"]],
                cwd=runner_copy,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

            def pump():
                for line in slot.process.stdout:
                    passthrough(line, slot=slot.index)
                    if "Running job:" in line:
                        slot.got_job.set()

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()

            deadline = time.monotonic() + config.idle_timeout
            while slot.process.poll() is None:
                if not slot.got_job.is_set() and time.monotonic() > deadline:
                    log(
                        f"{slot.repo}: no job after {config.idle_timeout}s, stopping runner {slot.name}",
                        slot=slot.index,
                    )
                    slot.process.terminate()
                    break
                if self._stopping.wait(1):
                    break
            try:
                slot.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                slot.process.kill()
                slot.process.wait()
            reader.join(timeout=5)
        finally:
            # A runner that ran its job is gone already; one that never got a
            # job would stay listed as offline until GitHub removes it a day later.
            if slot.runner_id is not None:
                try:
                    self.github.delete_runner(slot.repo, slot.runner_id)
                except (GitHubError, OSError) as err:
                    log(f"{slot.repo}: could not remove runner {slot.name}: {err}", slot=slot.index)
            clear_dir(work)
            shutil.rmtree(runner_copy, ignore_errors=True)
            if slot.runner_id is not None:
                log(f"{slot.repo}: runner {slot.name} finished", slot=slot.index)

    # --- cleanup ----------------------------------------------------------

    def remove_stale_runners(self) -> None:
        """Offline runners from an earlier run of this host (crash, kill -9)."""
        prefix = f"{self.config.name_prefix}-"
        for repo in self.repos:
            try:
                for runner in self.github.runners(repo):
                    if runner.get("name", "").startswith(prefix) and runner.get("status") == "offline":
                        self.github.delete_runner(repo, runner["id"])
                        log(f"{repo}: removed stale runner {runner['name']}")
            except GitHubError as err:
                log(f"{repo}: could not list runners: {err}")

    # --- main loop --------------------------------------------------------

    def stop(self, signum=None, _frame=None) -> None:
        if signum is not None:
            log(f"caught signal {signum}, stopping")
        self._stopping.set()
        self._wake.set()
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            if slot.process is not None and slot.process.poll() is None:
                slot.process.terminate()

    def tick(self) -> None:
        """One round: start runners for the oldest matching jobs while slots are free."""
        self.refresh_repos()
        free = self.config.max_concurrent - self.active()
        if free <= 0:
            # All slots busy: nothing to start, but the dispatcher is alive.
            self.heartbeat()
            return
        jobs, ok = self.scan()
        if ok:
            self.heartbeat()
        picked = pick_jobs(jobs, self.idle_per_repo(), free)
        for job in picked:
            waiting = len(jobs) - len(picked)
            more = f", {waiting} more waiting" if waiting > 0 else ""
            log(f"{job['repo']}: next job '{job['run_name']} / {job['name']}' (queued {job['created_at']}{more})")
            self.start(job)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        cfg = self.config
        log(
            f"solo-runner starting: max {cfg.max_concurrent} concurrent, labels {','.join(cfg.runner_labels)},"
            f" every {cfg.poll_interval}s, auth {self.github.auth.kind}"
        )
        os.makedirs(cfg.work_dir, exist_ok=True)
        while not self._stopping.is_set():
            try:
                self.refresh_repos(force=True)
                break
            except (GitHubError, OSError, KeyError, ValueError) as err:
                log(f"cannot list repos: {err}; retrying in {cfg.poll_interval}s")
                self._stopping.wait(cfg.poll_interval)
        self.remove_stale_runners()
        while not self._stopping.is_set():
            try:
                self.tick()
            except (GitHubError, OSError, KeyError, ValueError) as err:
                log(f"error: {err}")
            self._wake.wait(cfg.poll_interval)
            self._wake.clear()
        deadline = time.monotonic() + 30
        with self._lock:
            threads = [slot.thread for slot in self._slots.values() if slot.thread]
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        log("stopped")

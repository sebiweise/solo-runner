"""MODE=scaleset: GitHub assigns jobs to one runner scale set per repo and host.

Experimental. Instead of polling every repo's runs, the dispatcher keeps one
scale set per served repo (named RUNNER_NAME_PREFIX, with the runner labels)
and one listener thread per set that long-polls the Actions service. GitHub
assigns jobs to a set up to the capacity sent with each poll, which is this
host's free slots. Several hosts with their own prefix need no coordination:
a job is assigned to exactly one set, and one a host does not start within
5 minutes is taken back and assigned again, possibly to another host.

A repo's set can get more jobs than there are free slots, since every set is
told the same free capacity. Assigned jobs wait in a per-repo list; whenever a
slot is free, the repo with the oldest waiting job gets a runner. Runners and
slots are the same as in polling mode (Dispatcher.run_runner).

What this relies on was measured against real GitHub; see "Spike results" in
docs/proposals/runner-scale-sets.md.
"""

import os
import signal
import threading
import time

from .config import Config
from .dispatcher import Dispatcher, Slot, queued_seconds
from .github import GitHubError
from .log import log
from .scaleset import ScaleSetService, parse_jobs

# A listener that has not completed a poll for this long counts as unhealthy
# (one poll is held ~50 s by GitHub, POLL_TIMEOUT at most).
HEALTHY_WITHIN = 180


def queue_seconds(queue_time: str, now: float):
    """Seconds since an Actions service time ("2026-11-19T23:40:03.1234567Z")."""
    if not queue_time:
        return None
    return queued_seconds(queue_time.split(".")[0].rstrip("Z") + "Z", now)


class Listener(threading.Thread):
    """Owns one repo's scale set and session; tracks the jobs assigned to it."""

    def __init__(self, dispatcher, repo: str, service: ScaleSetService):
        super().__init__(daemon=True, name=f"listen-{repo}")
        self.dispatcher = dispatcher
        self.repo = repo
        self.service = service
        self.scale_set = None
        self.session = None
        self.last_id = None
        self.lock = threading.Lock()
        self.waiting = {}  # job id -> queue time: assigned to us, no runner has it yet
        self.running = set()  # job ids a runner of ours has started
        self.healthy_at = 0.0
        self.opened = False
        self.stopping = threading.Event()

    # --- state the dispatcher reads ---------------------------------------

    def counts(self):
        """(waiting jobs, running jobs, oldest waiting queue time or None)."""
        with self.lock:
            oldest = min(self.waiting.values(), default=None)
            return len(self.waiting), len(self.running), oldest

    # --- messages ---------------------------------------------------------

    def handle(self, message: dict) -> None:
        stats = message.get("statistics") or {}
        with self.lock:
            for job in parse_jobs(message):
                kind, job_id = job.get("messageType"), job.get("jobId")
                if not job_id:
                    continue
                if kind == "JobAssigned":
                    self.waiting[job_id] = job.get("queueTime") or ""
                elif kind == "JobStarted":
                    self.waiting.pop(job_id, None)
                    self.running.add(job_id)
                elif kind == "JobCompleted":
                    # "canceled" with no runner is GitHub taking an assignment
                    # back after 5 minutes; the job comes again as a new
                    # JobAssigned with a new id, here or on another host.
                    self.waiting.pop(job_id, None)
                    self.running.discard(job_id)
            # One message holds at most 50 job messages; the statistics are
            # always current. With nothing assigned, nothing can be waiting.
            if stats.get("totalAssignedJobs", 1) == 0:
                self.waiting.clear()
                self.running.clear()

    # --- life cycle -------------------------------------------------------

    def open(self) -> None:
        config = self.dispatcher.config
        name, labels = config.name_prefix, config.runner_labels_for(self.repo)
        self.scale_set = self.service.ensure_scale_set(name, labels)
        try:
            self.session = self.service.create_session(self.scale_set["id"], name)
        except GitHubError as err:
            if err.status != 409 or self.opened:
                if err.status == 409:
                    # Ours was open before, so the session is someone else's:
                    # another host started with the same RUNNER_NAME_PREFIX.
                    raise GitHubError(
                        f"scale set {name} is used by another host; give every host its own RUNNER_NAME_PREFIX",
                        409,
                    ) from None
                raise
            # On start, an open session is one an earlier container left
            # behind (kill -9, crash): GitHub keeps it, and refuses a second
            # one. The set is ours by its name, so it is rebuilt.
            log(f"{self.repo}: scale set {name} has an open session, recreating the scale set")
            self.service.delete_scale_set(self.scale_set["id"])
            self.scale_set = self.service.ensure_scale_set(name, labels)
            self.session = self.service.create_session(self.scale_set["id"], name)
        self.opened = True
        self.last_id = None
        with self.lock:
            self.waiting.clear()
            self.running.clear()
        log(f"{self.repo}: listening on scale set {name} (id {self.scale_set['id']})")

    def run(self) -> None:
        retry = self.dispatcher.config.poll_interval
        while not self.stopping.is_set():
            try:
                if self.session is None:
                    self.open()
                message, self.session = self.service.get_message(
                    self.session, self.last_id, self.dispatcher.capacity_for(self.repo)
                )
                self.healthy_at = time.monotonic()
                if message is not None:
                    self.handle(message)
                    self.service.delete_message(self.session, message["messageId"])
                    self.last_id = message["messageId"]
                self.dispatcher.wake()
            except (GitHubError, OSError, KeyError, TypeError, ValueError) as err:
                if self.stopping.is_set():
                    break
                log(f"{self.repo}: scale set listener: {err}; retrying in {retry}s")
                if getattr(err, "status", 0) == 404:
                    self.session = None  # set or session deleted elsewhere
                self.stopping.wait(retry)

    def close(self, delete: bool = True) -> None:
        """End the session and, by default, delete the set: like polling mode,
        no runner is registered while solo-runner is not running, and jobs
        stay queued until it is back."""
        self.stopping.set()
        try:
            if self.session is not None and self.scale_set is not None:
                self.service.delete_session(self.scale_set["id"], self.session)
            if delete and self.scale_set is not None:
                self.service.delete_scale_set(self.scale_set["id"])
        except (GitHubError, OSError) as err:
            log(f"{self.repo}: could not close scale set: {err}")


class ScaleSetDispatcher(Dispatcher):
    def __init__(self, config: Config, github=None, metrics=None, service_factory=None):
        super().__init__(config, github, metrics)
        self.listeners = {}
        self._service = service_factory or (lambda repo: ScaleSetService(self.github, repo))
        self._schedule_lock = threading.Lock()

    def wake(self) -> None:
        self._wake.set()

    # --- capacity and scheduling ------------------------------------------

    def capacity_for(self, repo: str) -> int:
        """What the repo's listener reports to GitHub: this host's free slots,
        or fewer if the repo has its own max_concurrent."""
        free = self.config.max_concurrent - self.active()
        limit = self.config.max_concurrent_for(repo)
        if limit is not None:
            free = min(free, limit - self.active_per_repo().get(repo, 0))
        return max(0, free)

    def needs(self) -> list:
        """(oldest queue time, repo) of every repo with an assigned job no runner covers."""
        active = self.active_per_repo()
        result = []
        for repo, listener in list(self.listeners.items()):
            waiting, running, oldest = listener.counts()
            # Our slots of this repo whose runner has not been reported
            # running yet will take one of its waiting jobs each.
            starting = max(0, active.get(repo, 0) - running)
            if waiting - starting > 0:
                result.append((oldest or "", repo))
        return sorted(result)

    def schedule(self) -> None:
        """Start a runner for the repo with the oldest waiting job, while slots are free.
        A started slot counts for its repo at once (needs() subtracts it)."""
        with self._schedule_lock:
            while self.config.max_concurrent - self.active() > 0:
                candidates = [(oldest, repo) for oldest, repo in self.needs() if self._room(repo)]
                if not candidates:
                    return
                oldest, repo = candidates[0]
                waited = queue_seconds(oldest, time.time())
                if waited is not None:
                    self.metrics.observe("solo_runner_job_wait_seconds", waited)
                log(f"{repo}: job assigned to scale set {self.config.name_prefix}, starting a runner")
                self.start({"repo": repo})

    def _room(self, repo: str) -> bool:
        limit = self.config.max_concurrent_for(repo)
        return limit is None or self.active_per_repo().get(repo, 0) < limit

    # --- runners ----------------------------------------------------------

    def register_runner(self, slot: Slot, work: str) -> dict:
        listener = self.listeners[slot.repo]
        return listener.service.jit_config(listener.scale_set["id"], slot.name, work)

    def unregister_runner(self, slot: Slot) -> None:
        listener = self.listeners.get(slot.repo)
        if listener is not None:
            listener.service.remove_runner(slot.runner_id)

    # --- listeners --------------------------------------------------------

    def sync_listeners(self) -> None:
        """One listener per served repo; repos that are no longer served lose theirs."""
        for repo in self.repos:
            if repo not in self.listeners:
                listener = Listener(self, repo, self._service(repo))
                self.listeners[repo] = listener
                listener.start()
        for repo in [repo for repo in self.listeners if repo not in self.repos]:
            listener = self.listeners[repo]
            # A running job keeps its runner; the set goes once nothing uses it.
            if self.active_per_repo().get(repo, 0) == 0:
                listener.close()
                del self.listeners[repo]

    def healthy(self) -> bool:
        now = time.monotonic()
        return all(now - listener.healthy_at < HEALTHY_WITHIN for listener in self.listeners.values())

    def _gauges(self):
        result = super()._gauges()
        queued = {repo: listener.counts()[0] for repo, listener in list(self.listeners.items())}
        return [item for item in result if item[0] != "solo_runner_jobs_queued"] + [
            ("solo_runner_jobs_queued", {"repo": repo}, queued.get(repo, 0)) for repo in self.repos
        ]

    # --- main loop --------------------------------------------------------

    def stop(self, signum=None, _frame=None) -> None:
        super().stop(signum, _frame)
        for listener in list(self.listeners.values()):
            listener.stopping.set()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        cfg = self.config
        log(
            f"solo-runner starting in scale set mode (experimental): max {cfg.max_concurrent} concurrent,"
            f" scale set {cfg.name_prefix}, labels {','.join(cfg.runner_labels)}, auth {self.github.auth.kind}"
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
                self.refresh_repos()
                self.sync_listeners()
                self.schedule()
                if self.listeners and self.healthy():
                    self.metrics.set("solo_runner_last_scan_timestamp_seconds", time.time())
                    self.heartbeat()
            except (GitHubError, OSError, KeyError, ValueError) as err:
                log(f"error: {err}")
            self._wake.wait(cfg.poll_interval)
            self._wake.clear()
        deadline = time.monotonic() + 30
        with self._lock:
            threads = [slot.thread for slot in self._slots.values() if slot.thread]
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for listener in list(self.listeners.values()):
            listener.close()
        log("stopped")

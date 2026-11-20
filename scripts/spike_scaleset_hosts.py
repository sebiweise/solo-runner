"""Spike 2: two scale sets with the same labels in one repo, as two hosts would have.

Follows scripts/spike_scaleset.py (same protocol code, imported from it) and
answers what spike 1 left open (docs/proposals/runner-scale-sets.md):

  Phase 1  Both "hosts" report capacity 0, then A raises to 1 and later B to
           1. How long until a raised capacity gets a job? Does each host get
           one of the two jobs, and do both run on their own runners?
  Phase 2  A reports capacity 2 but starts no runner, B reports 0. Once A
           holds both jobs, B raises to 2 and starts runners. Do the jobs move
           to B when GitHub takes them back from A after 5 minutes?

Environment as for spike_scaleset.py; NAME is the label both scale sets carry
(their names are NAME-a and NAME-b).
"""

import json
import os
import subprocess
import sys
import threading
import time

import spike_scaleset as s1
from spike_scaleset import IMAGE, NAME, REPO, Actions, emit, gh, rest_jobs

findings = {}


class HostActions(Actions):
    def __init__(self, set_name):
        self.set_name = set_name
        super().__init__()

    def create_scale_set(self):
        status, created = self.call(
            "POST",
            "_apis/runtime/runnerscalesets",
            {
                "name": self.set_name,
                "runnerGroupId": 1,
                # Same labels on both sets: a job asking for [self-hosted, NAME]
                # matches either, as it would with one set per host.
                "labels": [{"name": label, "type": "System"} for label in (NAME, "self-hosted")],
                "RunnerSetting": {"disableUpdate": True},
            },
        )
        emit("scale-set-created", host=self.set_name, status=status, id=(created or {}).get("id"))
        assert status == 200, created
        self.scale_set = created["id"]


class Host(threading.Thread):
    """One listener: long-polls with self.capacity and, if self.run, starts
    as many JIT runners as GitHub has assigned jobs beyond its running ones."""

    def __init__(self, key):
        super().__init__(daemon=True, name=key)
        self.key = key
        self.actions = HostActions(f"{NAME}-{key}")
        self.capacity = 0
        self.run_runners = False
        self.assigned = 0
        self.ever_assigned = 0
        self.procs = []
        self.stopping = threading.Event()
        self.seen = []

    def active(self):
        return sum(1 for proc, _ in self.procs if proc.poll() is None)

    def start_runner(self):
        name = f"{NAME}-{self.key}-{len(self.procs) + 1}"
        cfg = self.actions.jit(name)
        log = open(f"runner-{self.key}-{len(self.procs) + 1}.log", "w")  # noqa: SIM115 - closed in finish()
        proc = subprocess.Popen(
            ["docker", "run", "--rm", "-e", "JIT", IMAGE, "sh", "-c", './run.sh --jitconfig "$JIT"'],
            env={**os.environ, "JIT": cfg},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.procs.append((proc, log))
        emit("runner-started", host=self.key, name=name)

    def run(self):
        last = None
        while not self.stopping.is_set():
            capacity = self.capacity
            msg = self.actions.poll(last, capacity)
            if msg is not None:
                last = msg["messageId"]
                stats = msg.get("statistics") or {}
                self.assigned = stats.get("totalAssignedJobs", 0)
                self.ever_assigned = max(self.ever_assigned, self.assigned)
                emit("message", host=self.key, capacity=capacity, statistics=stats)
                for job in json.loads(msg["body"]) if msg.get("body") else []:
                    kind = job.get("messageType")
                    emit(kind, host=self.key, job=job.get("jobDisplayName"), result=job.get("result"))
                    self.seen.append(
                        (round(time.monotonic() - s1.T0, 1), kind, job.get("jobDisplayName"), job.get("result"))
                    )
                self.actions.ack(last)
            if self.run_runners:
                for _ in range(max(0, self.assigned - self.active())):
                    self.start_runner()

    def finish(self):
        self.stopping.set()
        for proc, log in self.procs:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()


def wait_for(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return round(time.monotonic() - s1.T0, 1)
        time.sleep(2)
    return None


def run_done(run_id):
    _, run = gh("GET", f"/repos/{REPO}/actions/runs/{run_id}")
    return (run or {}).get("status") == "completed"


def main():
    hosts = {}
    runs = []
    try:
        cleaner = Actions()
        cleaner.remove_leftovers()
        for key in ("a", "b"):
            host = Host(key)
            host.actions.create_scale_set()
            host.actions.open_session(f"{NAME}-{key}")
            hosts[key] = host
            host.start()
        a, b = hosts["a"], hosts["b"]

        # --- phase 1 ------------------------------------------------------
        runs.append(s1.dispatch())
        time.sleep(60)
        raised = round(time.monotonic() - s1.T0, 1)
        a.capacity, a.run_runners = 1, True
        emit("capacity", host="a", capacity=1)
        got = wait_for(lambda: a.ever_assigned > 0, 600)
        findings["1 A raised 0->1 at"] = raised
        findings["1 A first assignment at"] = got
        raised = round(time.monotonic() - s1.T0, 1)
        b.capacity, b.run_runners = 1, True
        emit("capacity", host="b", capacity=1)
        got = wait_for(lambda: b.ever_assigned > 0 or run_done(runs[0]), 600)
        findings["1 B raised 0->1 at"] = raised
        findings["1 B first assignment (or run done) at"] = got
        wait_for(lambda: run_done(runs[0]), 300)
        findings["1 REST jobs (name, status, conclusion, runner)"] = rest_jobs(runs[0])

        # --- phase 2 ------------------------------------------------------
        a.capacity, a.run_runners = 2, False
        b.capacity, b.run_runners = 0, False
        time.sleep(60)  # let both long polls pick up the new capacities
        mark_a, mark_b = len(a.seen), len(b.seen)
        runs.append(s1.dispatch())
        findings["2 A holds both at"] = wait_for(lambda: a.assigned >= 2, 240)
        b.capacity, b.run_runners = 2, True
        emit("capacity", host="b", capacity=2)
        findings["2 B raised 0->2 at"] = round(time.monotonic() - s1.T0, 1)
        findings["2 run done at"] = wait_for(lambda: run_done(runs[1]), 780)
        findings["2 A messages"] = a.seen[mark_a:]
        findings["2 B messages"] = b.seen[mark_b:]
        findings["2 REST jobs (name, status, conclusion, runner)"] = rest_jobs(runs[1])
        findings["1 A messages"] = a.seen[:mark_a]
        findings["1 B messages"] = b.seen[:mark_b]
    finally:
        for host in hosts.values():
            host.finish()
        for run_id in runs:
            if run_id and not run_done(run_id):
                status, _ = gh("POST", f"/repos/{REPO}/actions/runs/{run_id}/cancel")
                emit("run-cancelled", run=run_id, status=status)
        for host in hosts.values():
            try:
                host.actions.close_session()
            finally:
                host.actions.delete_scale_set()
        print("SUMMARY " + json.dumps(findings, indent=2, default=str), flush=True)


if __name__ == "__main__":
    sys.exit(main())

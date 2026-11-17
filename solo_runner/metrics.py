"""Prometheus metrics in the text exposition format, on the standard library only.

Off by default. With METRICS_PORT set, `GET /metrics` on that port returns the
counters below plus gauges read from the dispatcher at scrape time.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

# Upper bounds in seconds: GitHub keeps a job queued for up to 24 hours.
WAIT_BUCKETS = (5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 86400)

# name -> (type, help)
DEFINITIONS = {
    "solo_runner_info": ("gauge", "Always 1; the version is a label."),
    "solo_runner_slots_max": ("gauge", "MAX_CONCURRENT: runners allowed at the same time."),
    "solo_runner_slots_busy": ("gauge", "Runners that exist now (starting, waiting for a job or running one)."),
    "solo_runner_slots_free": ("gauge", "Slots a new runner could use now."),
    "solo_runner_repos_served": ("gauge", "Repos the dispatcher polls."),
    "solo_runner_jobs_queued": ("gauge", "Matching queued jobs seen in the last scan, per repo."),
    "solo_runner_last_scan_timestamp_seconds": ("gauge", "Unix time of the last scan in which every repo answered."),
    "solo_runner_runners_started_total": ("counter", "Just-in-time runners registered, per repo."),
    "solo_runner_jobs_started_total": ("counter", "Jobs a runner picked up, per repo."),
    "solo_runner_jobs_finished_total": ("counter", "Jobs whose runner exited after running them, per repo."),
    "solo_runner_runners_idle_total": ("counter", "Runners removed because no job came within IDLE_TIMEOUT, per repo."),
    "solo_runner_runner_errors_total": ("counter", "Runners that failed to register or start, per repo."),
    "solo_runner_job_wait_seconds": ("histogram", "Time from a job being queued to a runner being started for it."),
    "solo_runner_github_requests_total": ("counter", "GitHub API requests, by HTTP status (304 = served from cache)."),
    "solo_runner_github_errors_total": (
        "counter",
        "Failed GitHub API requests, by HTTP status, or 'network' when no answer came.",
    ),
    "solo_runner_github_rate_limit_remaining": ("gauge", "X-RateLimit-Remaining of the last API answer."),
}


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in labels) + "}"


def _number(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


class Metrics:
    """Thread-safe counters, gauges and one kind of histogram.

    `collect` is called at scrape time and returns (name, labels_dict, value)
    for gauges whose value lives elsewhere (the dispatcher's slots, say).
    """

    def __init__(self, collect=None):
        self._lock = threading.Lock()
        self._values = {}  # (name, ((label, value), ...)) -> number
        self._histograms = {}  # name -> [bucket counts..., sum, count]
        self.collect = collect

    @staticmethod
    def _key(name, labels):
        if name not in DEFINITIONS:
            raise KeyError(f"undefined metric {name}")
        return name, tuple(sorted(labels.items()))

    def inc(self, name: str, amount: float = 1, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def set(self, name: str, value: float, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._values[key] = value

    def observe(self, name: str, value: float) -> None:
        self._key(name, {})
        with self._lock:
            hist = self._histograms.setdefault(name, [0] * (len(WAIT_BUCKETS) + 2))
            for index, bound in enumerate(WAIT_BUCKETS):
                if value <= bound:
                    hist[index] += 1
            hist[-2] += value
            hist[-1] += 1

    def value(self, name: str, **labels):
        """The current value of one series (for tests), 0 if it does not exist."""
        with self._lock:
            return self._values.get(self._key(name, labels), 0)

    def render(self) -> str:
        series = {}
        with self._lock:
            for (name, labels), value in self._values.items():
                series.setdefault(name, []).append((labels, value))
            histograms = {name: list(hist) for name, hist in self._histograms.items()}
        series.setdefault("solo_runner_info", []).append(((("version", __version__),), 1))
        if self.collect:
            for name, labels, value in self.collect():
                series.setdefault(name, []).append((tuple(sorted(labels.items())), value))

        lines = []
        for name, (kind, text) in DEFINITIONS.items():
            if name not in series and name not in histograms:
                continue
            lines.append(f"# HELP {name} {text}")
            lines.append(f"# TYPE {name} {kind}")
            if kind == "histogram":
                hist = histograms[name]
                for index, bound in enumerate(WAIT_BUCKETS):
                    lines.append(f'{name}_bucket{{le="{bound}"}} {hist[index]}')
                lines.append(f'{name}_bucket{{le="+Inf"}} {hist[-1]}')
                lines.append(f"{name}_sum {_number(hist[-2])}")
                lines.append(f"{name}_count {hist[-1]}")
                continue
            for labels, value in sorted(series[name]):
                lines.append(f"{name}{_labels(labels)} {_number(value)}")
        return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    metrics: Metrics = None

    def do_GET(self):  # name required by BaseHTTPRequestHandler
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        body = self.metrics.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # scrapes are not worth a log line
        pass


def serve(metrics: Metrics, addr: str, port: int) -> ThreadingHTTPServer:
    """Serve /metrics in a daemon thread. Raises OSError if the port is taken."""
    handler = type("Handler", (_Handler,), {"metrics": metrics})
    server = ThreadingHTTPServer((addr, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server

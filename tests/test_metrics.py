import io
import json
import urllib.error
import urllib.request

import pytest

from solo_runner.github import GitHub, GitHubError, TokenAuth
from solo_runner.metrics import Metrics, serve


def test_render_counters_gauges_and_histogram():
    metrics = Metrics(collect=lambda: [("solo_runner_slots_free", {}, 2)])
    metrics.inc("solo_runner_jobs_started_total", repo="me/a")
    metrics.inc("solo_runner_jobs_started_total", repo="me/a")
    metrics.inc("solo_runner_jobs_started_total", repo='we"ird\\')
    metrics.observe("solo_runner_job_wait_seconds", 3)
    metrics.observe("solo_runner_job_wait_seconds", 100)
    text = metrics.render()
    assert "# TYPE solo_runner_jobs_started_total counter" in text
    assert 'solo_runner_jobs_started_total{repo="me/a"} 2' in text
    assert 'solo_runner_jobs_started_total{repo="we\\"ird\\\\"} 1' in text
    assert "solo_runner_slots_free 2" in text
    assert 'solo_runner_info{version="' in text
    # Buckets are cumulative.
    assert 'solo_runner_job_wait_seconds_bucket{le="5"} 1' in text
    assert 'solo_runner_job_wait_seconds_bucket{le="60"} 1' in text
    assert 'solo_runner_job_wait_seconds_bucket{le="120"} 2' in text
    assert 'solo_runner_job_wait_seconds_bucket{le="+Inf"} 2' in text
    assert "solo_runner_job_wait_seconds_sum 103" in text
    assert "solo_runner_job_wait_seconds_count 2" in text
    # Metrics without a value are left out.
    assert "solo_runner_runner_errors_total" not in text


def test_unknown_metric_is_a_bug():
    with pytest.raises(KeyError):
        Metrics().inc("solo_runner_typo_total")


def test_http_endpoint():
    metrics = Metrics()
    metrics.inc("solo_runner_runners_started_total", repo="me/a")
    server = serve(metrics, "127.0.0.1", 0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/metrics", timeout=5) as resp:
            assert resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")
            assert 'solo_runner_runners_started_total{repo="me/a"} 1' in resp.read().decode()
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/", timeout=5)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


class Response:
    def __init__(self, body, headers):
        self._body = json.dumps(body).encode()
        self.headers = headers
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_github_requests_and_errors_are_counted():
    answers = [
        Response({}, {"X-RateLimit-Remaining": "4999"}),
        urllib.error.HTTPError("u", 502, "Bad Gateway", {"X-RateLimit-Remaining": "4998"}, io.BytesIO(b"")),
        urllib.error.URLError("connection refused"),
    ]

    def opener(req, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    metrics = Metrics()
    gh = GitHub(TokenAuth("t"), opener=opener, metrics=metrics)
    gh.request("GET", "/x")
    with pytest.raises(GitHubError):
        gh.request("GET", "/x")
    with pytest.raises(urllib.error.URLError):
        gh.request("GET", "/x")
    assert metrics.value("solo_runner_github_requests_total", status="200") == 1
    assert metrics.value("solo_runner_github_errors_total", status="502") == 1
    assert metrics.value("solo_runner_github_errors_total", status="network") == 1
    assert metrics.value("solo_runner_github_rate_limit_remaining") == 4998

import io
import json
import urllib.error

from solo_runner.github import GitHub, TokenAuth


class FakeResponse:
    def __init__(self, body, etag=None):
        self._body = json.dumps(body).encode()
        self.headers = {"ETag": etag} if etag else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_etag_cache_returns_previous_body_on_304():
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if req.get_header("If-none-match") == '"v1"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO(b""))
        return FakeResponse({"workflow_runs": [{"id": 1}]}, etag='"v1"')

    gh = GitHub(TokenAuth("t"), opener=opener)
    first = gh.request("GET", "/repos/me/a/actions/runs", cache=True)
    second = gh.request("GET", "/repos/me/a/actions/runs", cache=True)
    assert first == second == {"workflow_runs": [{"id": 1}]}
    assert calls[0].get_header("Authorization") == "Bearer t"
    assert calls[1].get_header("If-none-match") == '"v1"'


def test_queued_jobs_include_jobs_of_in_progress_runs():
    responses = {
        "status=queued": {"workflow_runs": []},
        "status=in_progress": {"workflow_runs": [{"id": 7, "name": "CI"}]},
        "status=waiting": {"workflow_runs": []},
        "status=pending": {"workflow_runs": []},
        "/runs/7/jobs": {
            "jobs": [
                {"id": 1, "name": "lint", "status": "completed", "labels": ["self-hosted"]},
                {
                    "id": 2,
                    "name": "deploy",
                    "status": "queued",
                    "labels": ["self-hosted", "homelab"],
                    "created_at": "2026-11-17T10:00:00Z",
                },
            ]
        },
    }

    def opener(req, timeout):
        for key, body in responses.items():
            if key in req.full_url:
                return FakeResponse(body)
        raise AssertionError(req.full_url)

    jobs = GitHub(TokenAuth("t"), opener=opener).queued_jobs("me/a")
    assert jobs == [
        {
            "id": 2,
            "name": "deploy",
            "labels": ["self-hosted", "homelab"],
            "created_at": "2026-11-17T10:00:00Z",
            "run_name": "CI",
        }
    ]


def test_queued_jobs_include_jobs_of_pending_runs_once():
    # GitHub reported a run as "pending" while one of its jobs was queued for
    # a runner; a run that changes state between the requests is listed twice.
    run = {"id": 9, "name": "Release"}
    responses = {
        "status=queued": {"workflow_runs": [run]},
        "status=in_progress": {"workflow_runs": []},
        "status=waiting": {"workflow_runs": []},
        "status=pending": {"workflow_runs": [run]},
        "/runs/9/jobs": {
            "jobs": [
                {"id": 3, "name": "submit", "status": "waiting", "labels": ["homelab"]},
                {"id": 4, "name": "check", "status": "queued", "labels": ["homelab"], "created_at": "t"},
            ]
        },
    }
    urls = []

    def opener(req, timeout):
        urls.append(req.full_url)
        for key, body in responses.items():
            if key in req.full_url:
                return FakeResponse(body)
        raise AssertionError(req.full_url)

    jobs = GitHub(TokenAuth("t"), opener=opener).queued_jobs("me/a")
    assert [job["id"] for job in jobs] == [4]
    assert sum("/runs/9/jobs" in url for url in urls) == 1


def test_a_broken_answer_becomes_a_github_error():
    # http.client.IncompleteRead is no OSError and used to escape every handler.
    import http.client

    import pytest

    from solo_runner.github import GitHubError

    class Broken(FakeResponse):
        def read(self):
            raise http.client.IncompleteRead(b"{", 10)

    gh = GitHub(TokenAuth("t"), opener=lambda req, timeout: Broken({}))
    with pytest.raises(GitHubError, match="IncompleteRead"):
        gh.request("GET", "/repos/me/a/actions/runs")

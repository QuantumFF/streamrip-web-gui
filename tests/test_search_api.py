"""Seam: the Search endpoint (/api/search), driven through the Flask test client
against a fake `rip search` runner.

Search shells out to `rip search --output-file <tmp> ...` (ADR-0001): the real
runner writes the results JSON to the temp file the endpoint created and returns
a CompletedProcess. These tests swap run_search for a fake of the same shape, so
no test ever launches a real `rip` process.
"""
import json
import re

import app as app_module


class FakeCompleted:
    """Same shape as subprocess.CompletedProcess where run_search uses it:
    .returncode / .stdout / .stderr."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_search_runner(items, returncode=0, stdout="", stderr=""):
    """Build a fake `rip search` runner: writes the canned results JSON to the
    --output-file path the endpoint passed on the command line (just as real
    `rip search` does), then returns a CompletedProcess-shaped result."""
    def runner(cmd):
        if returncode == 0:
            output_path = cmd[cmd.index("--output-file") + 1]
            with open(output_path, "w") as f:
                json.dump(items, f)
        return FakeCompleted(returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def _search(client, **payload):
    payload.setdefault("query", "radiohead")
    payload.setdefault("type", "album")
    payload.setdefault("source", "qobuz")
    return client.post("/api/search", json=payload)


def test_search_success_returns_parsed_results(client, monkeypatch):
    items = [
        {"id": "111", "source": "qobuz", "media_type": "album", "desc": "OK Computer by Radiohead"},
        {"id": "222", "source": "qobuz", "media_type": "album", "desc": "Kid A by Radiohead"},
    ]
    monkeypatch.setattr(app_module, "run_search", fake_search_runner(items))

    resp = _search(client, query="radiohead")
    assert resp.status_code == 200
    data = resp.get_json()
    # Response shape is unchanged: results / query / source / total_count.
    assert set(["results", "query", "source", "total_count"]).issubset(data.keys())
    assert data["query"] == "radiohead"
    assert data["source"] == "qobuz"
    assert data["total_count"] == 2

    first = data["results"][0]
    assert first["id"] == "111"
    assert first["title"] == "OK Computer"
    assert first["artist"] == "Radiohead"
    assert first["url"] == "https://open.qobuz.com/album/111"


def test_search_nonzero_exit_is_500_error(client, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "run_search",
        fake_search_runner([], returncode=1, stdout="Traceback (most recent call last):"),
    )

    resp = _search(client, query="radiohead")
    assert resp.status_code == 500
    data = resp.get_json()
    assert "error" in data
    assert data["debug_info"]["return_code"] == 1


def test_search_missing_query_is_rejected(client):
    resp = client.post("/api/search", json={"type": "album", "source": "qobuz"})
    assert resp.status_code == 400


def test_search_empty_output_reports_no_results(client, monkeypatch):
    monkeypatch.setattr(app_module, "run_search", fake_search_runner([]))

    resp = _search(client, query="nothing here")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == []
    assert data["total_count"] == 0

"""API: GET /api/run/<id>/log — rohes Run-Log als Plain-Text (Tail via ?lines=N)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from fabrikant import config

RUN_ID = "20260101T000000Z-t"
LOG_LINES = ["[10:00:00] start", "[10:00:01] step a", "[10:00:02] step b", "[10:00:03] done"]


@pytest.fixture
def fabrikant_home(tmp_path, monkeypatch):
    """Isolierte FABRIKANT_HOME mit einem Run + zugehöriger Log-Datei."""
    home = tmp_path / "fabrikant_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs" / "uitest").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "projects" / "uitest.fabrikant.yaml").write_text(
        f'name: uitest\nroot: {home}\nloops:\n  t:\n    steps:\n      - {{type: shell, name: a, cmd: "true"}}\n'
    )
    (home / "runs" / "uitest" / f"{RUN_ID}.json").write_text(
        json.dumps({"id": RUN_ID, "project": "uitest", "loop": "t", "status": "pass"})
    )
    (home / "runs" / "uitest" / f"{RUN_ID}.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return home


@pytest.fixture
def fabrikant_url(fabrikant_home, live_server):
    return live_server()


def _get_text(url: str) -> tuple[int, str, dict]:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_run_log_returns_plain_text(fabrikant_url):
    status, body, headers = _get_text(f"{fabrikant_url}/api/run/{RUN_ID}/log")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/plain")
    assert body == "\n".join(LOG_LINES) + "\n"


def test_run_log_tail_lines(fabrikant_url):
    status, body, _ = _get_text(f"{fabrikant_url}/api/run/{RUN_ID}/log?lines=2")
    assert status == 200
    assert body == "\n".join(LOG_LINES[-2:])


def test_run_log_missing_is_404(fabrikant_url):
    status, _, _ = _get_text(f"{fabrikant_url}/api/run/keiner-da/log")
    assert status == 404


def test_run_log_traversal_is_404(fabrikant_url):
    # Encodetes Traversal erreicht den Handler; get_run_log lehnt die Id ab → 404.
    status, _, _ = _get_text(f"{fabrikant_url}/api/run/..%2F..%2Fetc%2Fpasswd/log")
    assert status == 404

"""scripts/sonar-pr-cleanup.py (ADR 0053) — verwaiste Sonar-PR-Projekte aufräumen.

Sonar-API als lokaler HTTP-Stub, GitHub (`gh api`) gemockt. Fail-safe:
unbestimmbarer PR-Status → kein Löschen; jeder Fehlschlag → Warnung, Exit 0.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sonar-pr-cleanup.py"
BASE = "quark-playground:fabrik"
REPO = "o/r"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sonar_pr_cleanup", SCRIPT)
    assert spec is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pr(state: str = "closed", merged: bool = False) -> dict:
    return {"state": state, "merged": merged}


def _gh_states(mod, states: dict[int, object], monkeypatch) -> None:
    """`gh api repos/<repo>/pulls/<n>` mocken: PR-Dict oder Exception pro Nummer."""

    def fake(*args: str):
        assert args[0] == "api"
        number = int(args[1].rsplit("/", 1)[1])
        result = states.get(number, subprocess.CalledProcessError(1, "gh"))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(mod, "_gh_json", fake)


def _sonar_handler(projects: list[str], *, delete_status: int = 200, search_status: int = 200):
    """SonarQube-Stub: paginierte `api/projects/search` + `api/projects/delete`.

    `state["requests"]` sammelt (Method, Path) für Assertions."""
    state: dict = {"requests": []}

    class Handler(BaseHTTPRequestHandler):
        state: dict  # Klassen-Attribut (Request-Log), wird nach der Klassendefinition gesetzt

        def _reply(self, status: int, body: bytes = b"{}") -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler-API
            state["requests"].append(("GET", self.path))
            if not self.path.startswith("/api/projects/search"):
                self._reply(404)
                return
            if search_status != 200:
                self._reply(search_status)
                return
            page = int(re.search(r"[?&]p=(\d+)", self.path).group(1))
            ps = int(re.search(r"[?&]ps=(\d+)", self.path).group(1))
            batch = projects[(page - 1) * ps : page * ps]
            body = json.dumps(
                {
                    "projects": [{"key": k} for k in batch],
                    "p": {"pageIndex": page, "pageSize": ps, "total": len(projects)},
                }
            ).encode()
            self._reply(200, body)

        def do_DELETE(self):  # noqa: N802
            state["requests"].append(("DELETE", self.path))
            self._reply(delete_status)

        def log_message(self, *_args):
            pass

    Handler.state = state
    return Handler


@contextmanager
def _serve(handler) -> Iterator[tuple[str, dict]]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler.state
    finally:
        srv.shutdown()
        srv.server_close()


# ── Key-Matching ──


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (f"{BASE}:pr-7", 7),
        (f"{BASE}:pr-123456", 123456),
        (BASE, None),  # main-Projekt
        (f"{BASE}:pr-x", None),
        (f"{BASE}:pr-", None),
        (f"{BASE}:pr", None),
        (f"{BASE}:pr-7:extra", None),
        ("other:pr-7", None),  # fremdes Projekt
        (f"{BASE}:pr-7 ", None),
    ],
)
def test_pr_number_matching(mod, key, expected):
    assert mod.pr_number(key, mod.pr_key_pattern(BASE)) == expected


# ── Auflistung (Paginierung) ──


def test_list_project_keys_paginates(mod, monkeypatch):
    monkeypatch.setattr(mod, "PAGE_SIZE", 2)
    calls: list[str] = []

    def fake(url: str, token: str, method: str = "GET") -> dict:
        calls.append(url)
        page = int(re.search(r"[?&]p=(\d+)", url).group(1))
        projects = ["a", "b", "c"]
        batch = projects[(page - 1) * 2 : page * 2]
        return {"projects": [{"key": k} for k in batch], "p": {"pageIndex": page, "pageSize": 2, "total": 3}}

    monkeypatch.setattr(mod, "_http_json", fake)
    assert mod.list_project_keys("http://x", "tok") == ["a", "b", "c"]
    assert len(calls) == 2  # Seite 2, dann Stopp (3 >= total)


def test_list_project_keys_stops_on_empty_page(mod, monkeypatch):
    """Defensive: leere Seite ohne total → kein Endlos-Loop."""
    pages = iter([{"projects": [{"key": "a"}], "p": {"total": 0}}, {"projects": [], "p": {"total": 0}}])

    monkeypatch.setattr(mod, "_http_json", lambda url, token, method="GET": next(pages))
    assert mod.list_project_keys("http://x", "tok") == ["a"]


# ── PR-Status ──


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (_pr("open"), True),
        (_pr("closed", merged=True), False),  # gemergt
        (_pr("closed"), False),  # geschlossen ohne Merge
        (_pr("weird"), None),  # unbekannter State → fail-safe
        ({}, None),
    ],
)
def test_pr_is_open(mod, pr, expected, monkeypatch):
    monkeypatch.setattr(mod, "_gh_json", lambda *a: pr)
    assert mod.pr_is_open(REPO, 7) is expected


def test_pr_is_open_gh_failure_is_none(mod, monkeypatch):
    def fake(*args: str):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(mod, "_gh_json", fake)
    assert mod.pr_is_open(REPO, 7) is None


# ── cleanup() ──


def test_cleanup_deletes_only_closed_prs(mod, monkeypatch):
    projects = [BASE, f"{BASE}:pr-1", f"{BASE}:pr-2", f"{BASE}:pr-3", "other:pr-9"]
    _gh_states(mod, {1: _pr("open"), 2: _pr("closed"), 3: _pr("closed", merged=True)}, monkeypatch)
    with _serve(_sonar_handler(projects)) as (url, state):
        stats = mod.cleanup(url, "tok", REPO, BASE)
    deleted = [p for m, p in state["requests"] if m == "DELETE"]
    # urllib normalisiert die Request-URL: `:` im Query wird percent-encoded
    assert sorted(deleted) == [
        f"/api/projects/delete?projectKey={urllib.parse.quote(f'{BASE}:pr-2')}",
        f"/api/projects/delete?projectKey={urllib.parse.quote(f'{BASE}:pr-3')}",
    ]
    assert stats == {"found": 3, "deleted": 2, "kept_open": 1, "skipped": 0, "failed": 0}


def test_cleanup_unknown_status_is_kept(mod, monkeypatch, capsys):
    """Fail-safe: PR-Status nicht ermittelbar (gh 404/Fehler) → Projekt bleibt."""
    projects = [f"{BASE}:pr-1", f"{BASE}:pr-2"]
    _gh_states(mod, {1: _pr("open")}, monkeypatch)  # PR 2: gh-Fehler
    with _serve(_sonar_handler(projects)) as (url, state):
        stats = mod.cleanup(url, "tok", REPO, BASE)
    assert not [p for m, p in state["requests"] if m == "DELETE"]
    assert stats["skipped"] == 1
    assert "fail-safe" in capsys.readouterr().out


def test_cleanup_delete_failure_warns_and_continues(mod, monkeypatch, capsys):
    """Abgelehntes Löschen (400) → Warnung, weitere Projekte werden trotzdem versucht."""
    projects = [f"{BASE}:pr-1", f"{BASE}:pr-2"]
    _gh_states(mod, {1: _pr("closed"), 2: _pr("closed")}, monkeypatch)
    with _serve(_sonar_handler(projects, delete_status=400)) as (url, state):
        stats = mod.cleanup(url, "tok", REPO, BASE)
    assert len([p for m, p in state["requests"] if m == "DELETE"]) == 2  # beide versucht
    assert stats["failed"] == 2 and stats["deleted"] == 0
    assert "WARNUNG" in capsys.readouterr().out


def test_cleanup_search_failure_is_noop(mod, capsys):
    """Sonar-API nicht erreichbar/500 → kein Löschen, keine Exception."""
    with _serve(_sonar_handler([], search_status=500)) as (url, state):
        stats = mod.cleanup(url, "tok", REPO, BASE)
    assert stats == {"found": 0, "deleted": 0, "kept_open": 0, "skipped": 0, "failed": 0}
    assert not [p for m, p in state["requests"] if m == "DELETE"]
    assert "WARNUNG" in capsys.readouterr().out


def test_cleanup_dry_run_does_not_delete(mod, monkeypatch):
    projects = [f"{BASE}:pr-1"]
    _gh_states(mod, {1: _pr("closed")}, monkeypatch)
    with _serve(_sonar_handler(projects)) as (url, state):
        stats = mod.cleanup(url, "tok", REPO, BASE, dry_run=True)
    assert not [p for m, p in state["requests"] if m == "DELETE"]
    assert stats["deleted"] == 1


# ── CLI (main) ──


def test_main_returns_zero_when_sonar_unreachable(mod, monkeypatch, capsys):
    monkeypatch.setenv("SONAR_TOKEN", "tok")
    monkeypatch.delenv("SONAR_TOKEN_FILE", raising=False)
    assert mod.main(["--host-url", "http://127.0.0.1:1", "--repo", REPO, "--base-key", BASE]) == 0
    assert "WARNUNG" in capsys.readouterr().out


def test_main_without_token_is_noop(mod, monkeypatch, capsys):
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    monkeypatch.setenv("SONAR_TOKEN_FILE", "/nonexistent/sonar-token.txt")
    assert mod.main(["--repo", REPO]) == 0
    assert "kein Scanner-Token" in capsys.readouterr().out


def test_main_reads_token_from_env_and_runs(mod, monkeypatch, capsys):
    monkeypatch.setenv("SONAR_TOKEN", "tok")
    projects = [f"{BASE}:pr-1"]
    _gh_states(mod, {1: _pr("closed")}, monkeypatch)
    with _serve(_sonar_handler(projects)) as (url, state):
        assert mod.main(["--host-url", url, "--repo", REPO, "--base-key", BASE]) == 0
    assert len([p for m, p in state["requests"] if m == "DELETE"]) == 1
    assert "erledigt" in capsys.readouterr().out

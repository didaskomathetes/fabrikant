"""scripts/sonar-scan.sh: Exit-Codes melden die Fehlerklasse (ADR 0035, sysexits 75/78).

Läuft gegen einen garantiert geschlossenen Port bzw. einen Health-Stub; `docker` und `uv` sind Stubs im PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sonar-scan.sh"
EX_GATE_RED = 1
EX_TEMPFAIL = 75
EX_CONFIG = 78
EX_NOTFOUND = 127
needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl fehlt")


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub(bindir: Path, name: str, body: str) -> None:
    exe = bindir / name
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)


def _sonar_stub(health_status: int = 200, token_body: bytes = b'{"token": "neu"}') -> type[BaseHTTPRequestHandler]:
    """SonarQube-Stub: GET (Health) antwortet `health_status` mit GREEN, POST (Token-Erzeugung) `token_body`."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler-API
            self._reply(health_status, b'{"health": "GREEN"}')

        def do_POST(self):  # noqa: N802
            self._reply(200, token_body)

        def log_message(self, *_args):
            pass

    return Handler


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def green_server() -> Iterator[str]:
    with _serve(_sonar_stub()) as url:
        yield url


def _run(
    tmp_path: Path,
    *,
    token: str | None,
    docker_exit: int = 0,
    host_url: str | None = None,
    uv: str | None = None,
    bindir: Path | None = None,
    script: Path = SCRIPT,
    extra_env: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess:
    """Skript mit Stubs ausführen; ``extra_env``-Wert ``None`` entfernt die Variable."""
    if bindir is None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
    _stub(bindir, "docker", f"exit {docker_exit}")
    if uv is not None:
        _stub(bindir, "uv", uv)
    token_file = tmp_path / "sonar-token.txt"
    if token is not None:
        token_file.write_text(token)
    # Kein geerbtes SONAR_* (z. B. läuft die Suite selbst in `SONAR_BRANCH=auto sonar-scan.sh`).
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("SONAR_")}
    env = {
        **inherited,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "SONAR_HOST_URL": host_url or f"http://127.0.0.1:{_closed_port()}",
        "SONAR_TOKEN_FILE": str(token_file),
        "SONAR_READY_TIMEOUT": "1",
        "SONAR_LINT_CMD": "",  # Analyzer-Reports nur in den Tests dafür (sonst liefe scripts/lint.sh gegen den uv-Stub)
        **(extra_env or {}),
    }
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)


@needs_curl
def test_unreachable_server_with_cached_token_is_tempfail(tmp_path):
    p = _run(tmp_path, token="tok")
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "nicht GREEN" in p.stdout


@needs_curl
def test_unreachable_server_without_token_is_tempfail(tmp_path):
    p = _run(tmp_path, token=None)
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "Token-Erzeugung fehlgeschlagen" in p.stdout


def test_docker_start_failure_is_config(tmp_path):
    p = _run(tmp_path, token="tok", docker_exit=1)
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr


@needs_curl
def test_missing_pysonar_is_not_found_not_tempfail(tmp_path, green_server):
    """`uv run pysonar` endet bei fehlendem Scanner mit 2 — das Skript muss 127 melden, nicht 75."""
    uv = 'case "$*" in *"command -v pysonar"*) exit 1;; *pysonar*) exit 2;; *) exit 0;; esac'
    p = _run(tmp_path, token="tok", host_url=green_server, uv=uv)
    assert p.returncode == EX_NOTFOUND, p.stdout + p.stderr
    assert "pysonar nicht gefunden" in p.stdout


@needs_curl
def test_failed_upload_is_tempfail(tmp_path, green_server):
    uv = 'case "$*" in *"command -v pysonar"*) exit 0;; *pysonar*) exit 1;; *) exit 0;; esac'
    p = _run(tmp_path, token="tok", host_url=green_server, uv=uv)
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "Scanner fehlgeschlagen" in p.stdout


@needs_curl
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_cached_token_is_config_not_tempfail(tmp_path, status):
    """Widerrufener Token: Warten heilt das nicht (ADR 0035 → environment)."""
    with _serve(_sonar_stub(health_status=status)) as url:
        p = _run(tmp_path, token="alt", host_url=url)
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "Scanner-Token abgelehnt" in p.stdout


@needs_curl
def test_malformed_token_response_is_config_not_outcome(tmp_path):
    with _serve(_sonar_stub(token_body=b"<html>proxy</html>")) as url:
        p = _run(tmp_path, token=None, host_url=url)
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "keinen Token" in p.stdout


@needs_curl
@pytest.mark.parametrize(("pytest_exit", "expected"), [(1, EX_GATE_RED), (2, EX_GATE_RED), (4, EX_CONFIG), (137, 137)])
def test_pytest_exit_maps_to_kind(tmp_path, green_server, pytest_exit, expected):
    """Tests rot/Collection-Fehler → outcome (1); Aufruf-/Setup-Fehler → 78; Signal unverändert."""
    uv = f'case "$*" in *pytest*) exit {pytest_exit};; *) exit 0;; esac'
    p = _run(tmp_path, token="tok", host_url=green_server, uv=uv)
    assert p.returncode == expected, p.stdout + p.stderr


def _full_scan_stub() -> type[BaseHTTPRequestHandler]:
    """SonarQube-Stub für den kompletten Scan: Health GREEN, CE-Task SUCCESS, Gate OK."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler-API
            if "/api/ce/task" in self.path:
                self._reply(200, b'{"task": {"status": "SUCCESS"}}')
            elif "/api/qualitygates/project_status" in self.path:
                self._reply(200, b'{"projectStatus": {"status": "OK"}}')
            else:
                self._reply(200, b'{"health": "GREEN"}')

        def log_message(self, *_args):
            pass

    return Handler


# uv-Stub für den kompletten main-Scan (neue sonar-scan.sh): pytest grün,
# `command -v pysonar`-Vorabprüfung OK, pysonar lädt hoch (CE-Task-ID im Output),
# Cleanup-Skript scheitert (advisory → Warnung, Exit 0).
_FULL_SCAN_UV = (
    'case "$*" in'
    ' *"command -v pysonar"*) exit 0;;'
    " *pytest*) exit 0;;"
    " *sonar-pr-cleanup*) exit 1;;"
    ' *pysonar*) echo "upload … api/ce/task?id=01234567-89ab-cdef-0123-456789abcdef …"; exit 0;;'
    " *) exit 0;;"
    " esac"
)


@needs_curl
def test_cleanup_failure_does_not_break_scan(tmp_path):
    """Legacy-PR-Projekt-Aufräumen ist advisory (ADR 0053): Fehlschlag → Warnung, Exit 0."""
    with _serve(_full_scan_stub()) as url:
        p = _run(tmp_path, token="tok", host_url=url, uv=_FULL_SCAN_UV)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Quality Gate OK" in p.stdout
    assert "WARNUNG" in p.stdout and "Aufräumen" in p.stdout


@needs_curl
def test_pr_scan_skips_cleanup(tmp_path):
    """PR-Scan (SONAR_PR gesetzt): die Legacy-Aufräumung läuft nicht mit (ADR 0053)."""
    bindir = _bindir_with(
        tmp_path,
        gh=f"printf '42\\tfeat/z\\tmain\\t{PR_HEAD_SHA}\\n'",
        git=f'case "$*" in *rev-parse*) echo {PR_HEAD_SHA};; esac',
    )
    with _serve(_full_scan_stub()) as url:
        p = _run(
            tmp_path,
            token="tok",
            host_url=url,
            uv=_FULL_SCAN_UV,
            bindir=bindir,
            extra_env={"SONAR_PR": "42"},
        )
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Quality Gate OK" in p.stdout
    assert "Räume verwaiste" not in p.stdout  # Aufräumung nur im main-Scan


@needs_curl
def test_unexpected_command_failure_is_config(tmp_path, green_server):
    """Ein ungeschützter Befehl scheitert (set -e): nicht Exit 1 (= „Gate rot“), sondern 78."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "mktemp", "exit 1")
    uv = 'case "$*" in *"command -v pysonar"*) exit 0;; *) exit 0;; esac'
    p = _run(tmp_path, token="tok", host_url=green_server, uv=uv, bindir=bindir)
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "unerwarteter Fehler" in p.stdout


# ---------------------------------------------------------------- PR-Head-Checkout (SONAR_PR_HEAD_REF)
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path) -> dict[str, Path | str]:
    """origin (main + Branch `feat`) und ein Klon mit Kopie des Skripts unter ``fabrik/scripts/``."""
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f.txt").write_text("main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "main")
    _git(origin, "checkout", "-qb", "feat")
    (origin / "f.txt").write_text("feat")
    _git(origin, "commit", "-qam", "feat")
    feat_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-q", "main")
    main_sha = _git(origin, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "--detach", "origin/main")
    script = clone / "fabrik" / "scripts" / "sonar-scan.sh"
    script.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, script)
    return {"clone": clone, "script": script, "feat": feat_sha, "main": main_sha}


def _scan_head(tmp_path: Path, repo: dict, green_server: str, ref: str | None) -> str:
    """Skript laufen lassen; der uv-Stub hält den gescannten HEAD fest (pytest-Aufruf → rot, Ende vor pysonar)."""
    clone = repo["clone"]
    uv = f'git -C "{clone}" rev-parse HEAD > "{tmp_path}/head.txt"; exit 1'
    env: dict[str, str | None] = {} if ref is None else {"SONAR_PR_HEAD_REF": ref}
    p = _run(tmp_path, token="tok", host_url=green_server, uv=uv, script=repo["script"], extra_env=env)
    assert p.returncode == EX_GATE_RED, p.stdout + p.stderr
    return (tmp_path / "head.txt").read_text().strip()


@needs_curl
def test_pr_run_checks_out_pr_head(tmp_path, green_server, pr_repo):
    assert _scan_head(tmp_path, pr_repo, green_server, "feat") == pr_repo["feat"]


@needs_curl
@pytest.mark.parametrize("ref", [None, "", "{pr_head_ref}"])
def test_no_pr_scans_default_checkout(tmp_path, green_server, pr_repo, ref):
    assert _scan_head(tmp_path, pr_repo, green_server, ref) == pr_repo["main"]


@needs_curl
def test_unfetchable_pr_head_is_tempfail(tmp_path, green_server, pr_repo):
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        uv="exit 0",
        script=pr_repo["script"],
        extra_env={"SONAR_PR_HEAD_REF": "gone"},
    )
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "nicht abrufbar" in p.stdout


# --- Branch-/PR-Analyse (Community-Branch-Plugin) ---------------------------------------------

# pysonar-Aufruf protokollieren und scheitern lassen: genügt, um die Scanner-Parameter zu prüfen.
_UV_RECORD = (
    'case "$*" in *"command -v pysonar"*) exit 0;; *pysonar*) echo "$*" > "$SCAN_ARGS_FILE"; exit 1;; *) exit 0;; esac'
)
PR_HEAD_SHA = "abc123"


def _scan_args(tmp_path: Path, green_server: str, env: dict[str, str], bindir: Path | None = None) -> str:
    args_file = tmp_path / "scan-args"
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        uv=_UV_RECORD,
        bindir=bindir,
        extra_env={**env, "SCAN_ARGS_FILE": str(args_file)},
    )
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr  # gestubbter Upload-Fehler
    return args_file.read_text()


def _bindir_with(tmp_path: Path, **stubs: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in stubs.items():
        _stub(bindir, name, body)
    return bindir


@needs_curl
def test_default_scan_has_no_branch_params(tmp_path, green_server):
    """Ohne SONAR_BRANCH/SONAR_PR bleibt es bei der Main-Branch-Analyse (Loop `sonar-scan`)."""
    args = _scan_args(tmp_path, green_server, {})
    assert "sonar.branch" not in args and "sonar.pullrequest" not in args


@needs_curl
def test_explicit_branch_is_passed_to_scanner(tmp_path, green_server):
    args = _scan_args(tmp_path, green_server, {"SONAR_BRANCH": "feature/x"})
    assert "-Dsonar.branch.name=feature/x" in args


@needs_curl
def test_auto_branch_uses_current_git_branch(tmp_path, green_server):
    bindir = _bindir_with(tmp_path, git='case "$*" in *symbolic-ref*) echo topic/y;; esac')
    args = _scan_args(tmp_path, green_server, {"SONAR_BRANCH": "auto"}, bindir=bindir)
    assert "-Dsonar.branch.name=topic/y" in args


@needs_curl
def test_pr_passes_pull_request_params(tmp_path, green_server):
    bindir = _bindir_with(
        tmp_path,
        gh=f"printf '42\\tfeat/z\\tmain\\t{PR_HEAD_SHA}\\n'",
        git=f'case "$*" in *rev-parse*) echo {PR_HEAD_SHA};; esac',
    )
    args = _scan_args(tmp_path, green_server, {"SONAR_PR": "42", "SONAR_BRANCH": "ignored"}, bindir=bindir)
    for param in ("sonar.pullrequest.key=42", "sonar.pullrequest.branch=feat/z", "sonar.pullrequest.base=main"):
        assert f"-D{param}" in args
    assert "sonar.branch.name" not in args  # PR hat Vorrang


@needs_curl
def test_unknown_pr_is_config(tmp_path, green_server):
    """Aufruffehler fällt vor dem Testlauf auf (→ environment, nicht transient)."""
    bindir = _bindir_with(tmp_path, gh="exit 1")
    p = _run(tmp_path, token="tok", host_url=green_server, bindir=bindir, extra_env={"SONAR_PR": "999"})
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "PR '999' nicht gefunden" in p.stdout


@needs_curl
def test_auto_on_detached_head_is_config(tmp_path, green_server):
    bindir = _bindir_with(tmp_path, git="exit 1")
    p = _run(tmp_path, token="tok", host_url=green_server, bindir=bindir, extra_env={"SONAR_BRANCH": "auto"})
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "detached" in p.stdout


@needs_curl
def test_unresolved_placeholder_means_no_pr(tmp_path, green_server):
    """Cron-Run desselben Loops: `{pr_number}` bleibt literal → Main-Branch-Analyse, kein gh-Aufruf."""
    bindir = _bindir_with(tmp_path, gh="exit 99")
    args = _scan_args(tmp_path, green_server, {"SONAR_PR": "{pr_number}", "SONAR_BRANCH": "{x}"}, bindir=bindir)
    assert "sonar.branch" not in args and "sonar.pullrequest" not in args


# --- Ergebnis-Report (SONAR_REPORT_FILE) ---------------------------------------------------------

_REPORT_API = {
    "/api/ce/task": {"task": {"status": "SUCCESS"}},
    "/api/qualitygates/project_status": {
        "projectStatus": {
            "status": "ERROR",
            "conditions": [{"status": "ERROR", "metricKey": "new_coverage", "actualValue": "50.0"}],
        }
    },
    "/api/measures/component": {
        "component": {
            "measures": [{"metric": "new_coverage", "period": {"value": "50.0"}}, {"metric": "coverage", "value": "90"}]
        }
    },
    "/api/issues/search": {
        "total": 1,
        "issues": [
            {
                "rule": "python:S1",
                "severity": "MINOR",
                "impacts": [{"severity": "LOW"}, {"severity": "HIGH"}],
                "message": "m",
                "component": "quark-playground:fabrik:src/x.py",
                "line": 3,
            }
        ],
    },
}


def _report_stub() -> type[BaseHTTPRequestHandler]:
    """SonarQube-Stub für den vollständigen Lauf: Health GREEN + feste Antworten je API-Pfad."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler-API
            path = self.path.split("?", 1)[0]
            body = json.dumps(_REPORT_API.get(path, {"health": "GREEN", "status": "UP"})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


@needs_curl
def test_report_file_is_written_even_when_gate_is_red(tmp_path):
    """Rotes Gate: Exit 1 (outcome), aber der Report liegt vor — der PR-Kommentar braucht ihn."""
    report = tmp_path / "out" / "report.json"
    comment = tmp_path / "out" / "comment.md"
    uv = (
        'case "$*" in *"command -v pysonar"*) exit 0;; '
        '*pysonar*) echo "INFO: http://x/api/ce/task?id=abc-123"; exit 0;; *) exit 0;; esac'
    )
    with _serve(_report_stub()) as url:
        p = _run(
            tmp_path,
            token="tok",
            host_url=url,
            uv=uv,
            extra_env={
                "SONAR_REPORT_FILE": str(report),
                "SONAR_COMMENT_FILE": str(comment),
                "SONAR_LINK_BASE": "https://github.com/o/r/blob/abc",
            },
        )
    assert p.returncode == EX_GATE_RED, p.stdout + p.stderr
    data = json.loads(report.read_text())
    assert data["status"] == "ERROR"
    assert data["conditions"][0]["metricKey"] == "new_coverage"
    assert data["measures"] == {"new_coverage": "50.0", "coverage": "90"}  # period-Wert für new_*
    issue = data["issues"][0]
    assert issue["path"] == "fabrik/src/x.py"  # Repo-relativ (Monorepo-Präfix), nicht Sonar-Component
    assert issue["severity"] == "HIGH"  # höchste Impact-Severity statt Legacy-MINOR
    assert data["dashboard"].endswith("dashboard?id=quark-playground:fabrik")
    assert "[fabrik/src/x.py:3](https://github.com/o/r/blob/abc/fabrik/src/x.py#L3)" in comment.read_text()


# --- Fremde Projekte (SONAR_PROJECT_DIR, SONAR_TEST_CMD, SONAR_SCANNER, SONAR_SCANNER_ARGS) ------------


@needs_curl
def test_other_project_uses_its_dir_test_cmd_scanner_and_args(tmp_path, green_server):
    """Merchant-Assistant & Co.: Tests/Scanner laufen im Projekt, nicht im fabrik-Verzeichnis."""
    project = tmp_path / "proj"
    project.mkdir()
    out = tmp_path / "scanner-call"
    bindir = _bindir_with(tmp_path, scanner=f'echo "cwd=$(pwd) args=$*" > "{out}"; exit 1')
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        bindir=bindir,
        extra_env={
            "SONAR_PROJECT_DIR": str(project),
            "SONAR_TEST_CMD": "pwd > tests-ran-here",
            "SONAR_COVERAGE_PATHS": "cov/coverage-*.xml",
            "SONAR_SCANNER": "scanner --flag",
            "SONAR_SCANNER_ARGS": "-Dsonar.projectKey=merchant -Dsonar.sources=src,libs",
        },
    )
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr  # gestubbter Scanner-Fehler
    assert (project / "tests-ran-here").read_text().strip() == str(project)
    assert "WARNUNG: kein Coverage-Report" in p.stdout  # cov/coverage-*.xml existiert nicht
    call = out.read_text()
    assert call.startswith(f"cwd={project} args=--flag ")
    for arg in ("-Dsonar.python.coverage.reportPaths=cov/coverage-*.xml", "-Dsonar.projectKey=merchant",
                "-Dsonar.sources=src,libs"):  # fmt: skip
        assert arg in call


@needs_curl
def test_existing_coverage_report_gives_no_warning(tmp_path, green_server):
    project = tmp_path / "proj"
    (project / "cov").mkdir(parents=True)
    (project / "cov" / "coverage-0.xml").write_text("<coverage/>")
    bindir = _bindir_with(tmp_path, scanner="exit 1")
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        bindir=bindir,
        extra_env={
            "SONAR_PROJECT_DIR": str(project),
            "SONAR_TEST_CMD": "true",
            "SONAR_COVERAGE_PATHS": "other.xml,cov/coverage-*.xml",  # Komma-Liste: ein Treffer genügt
            "SONAR_SCANNER": "scanner",
        },
    )
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "kein Coverage-Report" not in p.stdout


@needs_curl
def test_missing_custom_scanner_is_not_found(tmp_path, green_server):
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        uv="exit 0",
        extra_env={"SONAR_SCANNER": "/nonexistent/pysonar"},
    )
    assert p.returncode == EX_NOTFOUND, p.stdout + p.stderr
    assert "Scanner nicht gefunden" in p.stdout


@needs_curl
@pytest.mark.parametrize(("gate_status", "expected"), [("OK", EX_GATE_RED), ("ERROR", EX_GATE_RED)])
def test_scanner_failure_after_upload_is_outcome_with_report(tmp_path, gate_status, expected, monkeypatch):
    """Projekt-Skript lädt hoch und meldet dann Tests rot (exit 1): kein „transient“, Report entsteht trotzdem."""
    monkeypatch.setitem(_REPORT_API, "/api/qualitygates/project_status", {"projectStatus": {"status": gate_status}})
    report = tmp_path / "report.json"
    bindir = _bindir_with(tmp_path, scanner='echo "INFO: http://x/api/ce/task?id=abc-123"; exit 1')
    with _serve(_report_stub()) as url:
        p = _run(
            tmp_path,
            token="tok",
            host_url=url,
            bindir=bindir,
            extra_env={"SONAR_SCANNER": "scanner", "SONAR_TEST_CMD": "true", "SONAR_COVERAGE_PATHS": "",
                       "SONAR_REPORT_FILE": str(report)},
        )  # fmt: skip
    assert p.returncode == expected, p.stdout + p.stderr
    assert json.loads(report.read_text())["status"] == gate_status
    assert "kein Coverage-Report" not in p.stdout  # leer = Projekt-Config, keine Warnung


@needs_curl
def test_empty_coverage_paths_passes_no_coverage_arg(tmp_path, green_server):
    out = tmp_path / "scanner-call"
    bindir = _bindir_with(tmp_path, scanner=f'echo "$*" > "{out}"; exit 1')
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        bindir=bindir,
        extra_env={"SONAR_SCANNER": "scanner", "SONAR_TEST_CMD": "true", "SONAR_COVERAGE_PATHS": "",
                   "SONAR_BRANCH": "feat/x"},
    )  # fmt: skip
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr  # kein CE-Task im Output → Upload gescheitert
    assert out.read_text().split() == ["-Dsonar.branch.name=feat/x"]


# --- Analyzer-Reports (SONAR_LINT_CMD → ruff/bandit/pyright als externe Issues) ---------------------------------


@needs_curl
def test_lint_reports_are_passed_to_scanner(tmp_path, green_server):
    """Nur vorhandene, nicht leere Reports werden als Import-Parameter übergeben."""
    lint_dir = tmp_path / "lint"
    cmd = 'echo "[]" > "$SONAR_LINT_DIR/ruff.json"; echo "{}" > "$SONAR_LINT_DIR/pyright-sonar.json"'
    args = _scan_args(tmp_path, green_server, {"SONAR_LINT_CMD": cmd, "SONAR_LINT_DIR": str(lint_dir)})
    assert f"-Dsonar.python.ruff.reportPaths={lint_dir}/ruff.json" in args
    assert f"-Dsonar.externalIssuesReportPaths={lint_dir}/pyright-sonar.json" in args
    assert "bandit" not in args


@needs_curl
def test_stale_lint_reports_are_removed_before_run(tmp_path, green_server):
    lint_dir = tmp_path / "lint"
    lint_dir.mkdir()
    (lint_dir / "bandit.json").write_text("{}")  # Rest eines früheren Laufs
    args = _scan_args(tmp_path, green_server, {"SONAR_LINT_CMD": "true", "SONAR_LINT_DIR": str(lint_dir)})
    assert "reportPaths" not in args.replace("coverage.reportPaths", "")
    assert not (lint_dir / "bandit.json").exists()


@needs_curl
@pytest.mark.parametrize(("lint_exit", "expected"), [(1, EX_CONFIG), (2, EX_CONFIG), (127, EX_NOTFOUND), (137, 137)])
def test_lint_failure_maps_to_kind(tmp_path, green_server, lint_exit, expected):
    """Funde sind kein Exit ≠ 0 (lint.sh --reports) — ein Fehlschlag ist Setup (78), Tool fehlt 127, Signal bleibt."""
    p = _run(
        tmp_path,
        token="tok",
        host_url=green_server,
        uv="exit 0",
        extra_env={"SONAR_LINT_CMD": f"exit {lint_exit}", "SONAR_LINT_DIR": str(tmp_path / "lint")},
    )
    assert p.returncode == expected, p.stdout + p.stderr
    assert "Analyzer-Reports fehlgeschlagen" in p.stdout


@needs_curl
def test_other_project_has_no_default_lint_cmd(tmp_path, green_server):
    """Ohne SONAR_LINT_CMD läuft für fremde Projekte kein fabrik-Lint (deren Tools/Config fehlen dort)."""
    project = tmp_path / "proj"
    project.mkdir()
    bindir = _bindir_with(tmp_path, scanner="exit 1")
    env = {
        "SONAR_PROJECT_DIR": str(project),
        "SONAR_TEST_CMD": "true",
        "SONAR_SCANNER": "scanner",
        "SONAR_LINT_CMD": None,
    }
    p = _run(tmp_path, token="tok", host_url=green_server, bindir=bindir, extra_env=env)
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr
    assert "Analyzer-Reports" not in p.stdout


@needs_curl
def test_fabrik_default_lint_cmd_runs_lint_reports(tmp_path, green_server):
    """Ohne SONAR_LINT_CMD ruft der fabrik-Scan `scripts/lint.sh --reports <SONAR_LINT_DIR>` im Projekt auf."""
    scripts = tmp_path / "fab" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "sonar-scan.sh"
    shutil.copy(SCRIPT, script)
    (scripts / "lint.sh").write_text('[ "$1" = --reports ] && echo "[]" > "$2/ruff.json"\n')
    out = tmp_path / "scanner-call"
    bindir = _bindir_with(tmp_path, scanner=f'echo "$*" > "{out}"; exit 1')
    env = {"SONAR_LINT_CMD": None, "SONAR_TEST_CMD": "true", "SONAR_SCANNER": "scanner", "SONAR_COVERAGE_PATHS": ""}
    p = _run(tmp_path, token="tok", host_url=green_server, bindir=bindir, script=script, extra_env=env)
    assert p.returncode == EX_TEMPFAIL, p.stdout + p.stderr  # gestubbter Upload-Fehler
    assert out.read_text().split() == ["-Dsonar.python.ruff.reportPaths=.fabrik/sonar/lint/ruff.json"]
    assert (tmp_path / "fab" / ".fabrik" / "sonar" / "lint" / "ruff.json").exists()

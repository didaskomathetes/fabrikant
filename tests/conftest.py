"""Shared fixtures: import fabrik, isolate RUNS_DIR / PROJECTS_DIR / env per test."""

import os
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from fabrik import config, connections, procs, sessionlog
from fabrik import llm as llm_registry
from fabrik.runstore import STORE
from fabrik.server import FabrikHandler

# Env-Präfixe echter externer Backends (LLM-Provider, Agent-Server), die config
# lokal aus fabrik.env lädt — in CI gibt es sie nicht.
EXTERNAL_BACKEND_ENV_PREFIXES = ("FABRIK_LLM_", "FABRIK_AGENT_")

# serve_forever() prüft das Shutdown-Flag nur alle poll_interval Sekunden; der
# Default (0.5 s) kostete jeden HTTP-Test eine halbe Sekunde im Teardown.
SERVER_POLL_INTERVAL_S = 0.01


@pytest.fixture(autouse=True)
def _isolate_fabrik_home(tmp_path, monkeypatch):
    """Biegt alle schreibenden FABRIK_HOME-Pfade (runs/, state/, sessionlog.db)
    für jeden Test in ein tmp-Verzeichnis um. Ohne das schreiben Engine-Tests,
    die nur PROJECTS_DIR isolieren, Run-Records ins echte fabrik/runs/ (sichtbar
    in `fabrik runs` / UI). Tests, die eigene Pfade brauchen, überschreiben sie
    danach per monkeypatch (z. B. Fixture `runs_dir`)."""
    home = tmp_path / "_fabrik_home"
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    monkeypatch.setattr(config, "SCRATCH_DIR", home / "state" / "scratch")
    # Worktrees nie in die echten Wurzeln (XDG-State bzw. Legacy im Repo) schreiben/aufräumen.
    monkeypatch.setattr(config, "WORKTREES_DIR", home / "worktrees")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", home / "legacy-worktrees")
    monkeypatch.setenv("FABRIK_SESSIONLOG", str(home / "sessionlog.db"))
    # Viele Bestandstests legen Run-JSON direkt ab bzw. lesen sie: sie laufen explizit gegen das
    # JSON-Backend. Die gemeinsame Store-Suite (Fixture `store`) und test_runstore_sql decken SQL ab.
    monkeypatch.setenv(config.RUN_STORE_ENV, config.RUN_STORE_JSON)
    monkeypatch.delenv(config.DB_URL_ENV, raising=False)
    monkeypatch.setattr(sessionlog, "_conn", None)
    connections._invalidate()  # Secret-Cache gehört zur DB des vorigen Tests


@pytest.fixture(autouse=True)
def _isolated_proc_registry(monkeypatch):
    """Frische procs-Registry pro Test: Backend-Tests registrieren Fake-Popens (erfundene PIDs),
    die ein späteres `kill_all()` sonst an echte Prozessgruppen schicken würde."""
    monkeypatch.setattr(procs, "_live", set())


@pytest.fixture(autouse=True)
def _no_real_backends(monkeypatch):
    """Keine echten LLM-/Agent-Backends in Tests: FABRIK_LLM_*/FABRIK_AGENT_* (lokal aus
    fabrik.env geladen) entfernen, damit lokale Läufe wie CI ohne Backends laufen. Sonst
    passieren Tests lokal nur, weil ein echtes Modell/ein echter Server antwortet — und
    schlagen in CI fehl. Tests, die ein Backend brauchen, setzen es per monkeypatch.setenv."""
    for key in [k for k in os.environ if k.startswith(EXTERNAL_BACKEND_ENV_PREFIXES)]:
        monkeypatch.delenv(key)
    llm_registry.reset_cache()
    yield
    llm_registry.reset_cache()


@pytest.fixture(autouse=True)
def _isolated_spec_sources(tmp_path, monkeypatch):
    """Spec-Suchpfad (ADR 0047) pro Test leeren: keine echten Beispiel-Specs/Templates/Registrierungen."""
    monkeypatch.delenv(config.PROJECTS_PATH_ENV, raising=False)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "_projects")
    monkeypatch.setattr(config, "BUNDLED_PROJECTS_DIR", tmp_path / "_bundled-projects")
    monkeypatch.setattr(config, "USER_TEMPLATES_DIR", tmp_path / "_user-templates")
    monkeypatch.setattr(config, "REGISTRY_FILE", tmp_path / "_state" / "registered-projects.json")


@pytest.fixture(autouse=True)
def _dispose_sql_stores():
    """SQL-Run-Stores (je Test eigene DB-URL im tmp-State-Dir) nach dem Test schließen."""
    yield
    sql = sys.modules.get("fabrik.runstore_sql")
    if sql is not None:
        sql.dispose_stores()


@pytest.fixture(params=config.RUN_STORE_BACKENDS)
def store_backend(request, monkeypatch, runs_dir):
    """Aktives Run-Store-Backend (``json`` | ``sql``) für die gemeinsame Store-Suite (ADR 0034).

    Setzt ``FABRIK_RUN_STORE``; das SQL-Backend nutzt die Default-DB im (isolierten) State-Dir."""
    monkeypatch.setenv(config.RUN_STORE_ENV, request.param)
    monkeypatch.delenv(config.DB_URL_ENV, raising=False)
    return request.param


@pytest.fixture
def store(store_backend):
    """Konkretes Backend hinter ``STORE`` für die parametrisierte Store-Suite."""
    return STORE.backend()


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(config, "RUNS_DIR", d)
    return d


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "projects"
    d.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", d)
    return d


@pytest.fixture
def clean_token(monkeypatch):
    monkeypatch.delenv("FABRIK_TOKEN", raising=False)
    return monkeypatch


@pytest.fixture
def live_server():
    """Factory: startet fabrik-HTTP-Server auf freien Ports (ohne Scheduler) → Basis-URL.

    Alle gestarteten Server werden im Teardown gestoppt."""
    servers: list[ThreadingHTTPServer] = []

    def start() -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FabrikHandler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, args=(SERVER_POLL_INTERVAL_S,), daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _default_message_language(monkeypatch):
    """Server messages are asserted in the default language (de), independent of the host env."""
    monkeypatch.delenv("FABRIK_LANG", raising=False)

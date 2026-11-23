"""Zentrale Konfiguration: Pfade, Konstanten, Env-Loading, Zeit, Log-Lock.

Single Source of Truth für die lauffähigen Pfade (RUNS_DIR, PROJECTS_DIR, …).
Alle Module referenzieren `config.RUNS_DIR` / `config.PROJECTS_DIR` per
Attribut-Zugriff (nicht `from .config import RUNS_DIR`), damit Tests die Pfade
per `monkeypatch.setattr(fabrik.config, "RUNS_DIR", …)` tauschen können.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .i18n import t

# src/fabrik/config.py → 3 Ebenen hoch = Projekt-Root (fabrik/)
FABRIK_HOME = Path(os.environ.get("FABRIK_HOME", Path(__file__).resolve().parent.parent.parent))


def _load_env_file(p: Path):
    """fabrik.env: KEY=VALUE-Zeilen (os.environ gewinnt).

    Explizit UTF-8: das Laden läuft bei jedem `import fabrik` — unter
    `LC_ALL=C` (Container, systemd) wäre die Default-Encoding ASCII und
    nicht-ASCII-Bytes in der Datei würden den Import crashen.
    """
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Vor allen Env-abhängigen Konstanten laden: z. B. FABRIK_DATA wirkt auch aus fabrik.env.
_load_env_file(FABRIK_HOME / "fabrik.env")

# Daten-/Konfigurationsverzeichnis der Installation (eigene Specs, Templates). Default
# FABRIK_HOME: ohne FABRIK_DATA verhält sich die Fabrik wie bisher (Spec-Auflösung: ADR 0047).
DATA_DIR = Path(os.environ.get("FABRIK_DATA", FABRIK_HOME))
PROJECTS_DIR = DATA_DIR / "projects"  # primärer (schreibbarer) Spec-Ordner
BUNDLED_PROJECTS_DIR = FABRIK_HOME / "projects"  # mitgelieferte Beispiel-Specs (niedrigste Priorität)
TEMPLATES_DIR = FABRIK_HOME / "templates"  # mitgelieferte Loop-Vorlagen
USER_TEMPLATES_DIR = DATA_DIR / "templates"
RUNS_DIR = FABRIK_HOME / "runs"
WORKTREES_ENV = "FABRIK_WORKTREES"
XDG_STATE_DEFAULT = Path(".local") / "state"  # relativ zu $HOME (XDG Base Directory Spec)
INSTALL_ID_HASH_LEN = 8


def install_id(home: Path) -> str:
    """Stabile Kennung einer Installation (`<ordner>-<hash>`): trennt die Worktrees zweier
    Checkouts, deren Projekte gleich heißen (sonst räumt der eine die Runs des anderen ab)."""
    resolved = home.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:INSTALL_ID_HASH_LEN]
    return f"{resolved.name}-{digest}"


def default_worktrees_dir(home: Path, env: Mapping[str, str]) -> Path:
    """Worktree-Wurzel (ADR 0047, Nachtrag 2026-11-03): `FABRIK_WORKTREES`, sonst
    `$XDG_STATE_HOME/fabrik/worktrees/<install_id>`.

    Bewusst außerhalb jedes Quell-Checkouts: IDEs/Indexer/File-Watcher, die das Repo
    beobachten, würden jeden Run-Checkout (samt `.venv`) mitindizieren. Außerhalb von
    `fabrik/` liegt der Pfad ohnehin (OpenCode lädt AGENTS.md vom Workdir aufwärts)."""
    explicit = env.get(WORKTREES_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_home = env.get("XDG_STATE_HOME", "").strip()
    if state_home:
        base = Path(state_home).expanduser()
        if not base.is_absolute():  # XDG-Spec: absolut; relativ → defensiv auf $HOME ankernd
            base = Path.home() / base
    else:
        base = Path.home() / XDG_STATE_DEFAULT
    return base / "fabrik" / "worktrees" / install_id(home)


WORKTREES_DIR = default_worktrees_dir(FABRIK_HOME, os.environ)
# Bis 2026-11 lagen Worktrees im Repo (`<repo>/.fabrik/worktrees`). Nur noch gelesen und
# aufgeräumt, damit pausierte Runs (`needs_approval`) ihren Worktree beim Resume finden.
LEGACY_WORKTREES_DIR = FABRIK_HOME.parent / ".fabrik" / "worktrees"
WEB_DIR = FABRIK_HOME / "web"
STATE_DIR = FABRIK_HOME / "state"
# Spec-Auflösung (ADR 0047): Suchpfad-Env (doppelpunkt-getrennt wie PATH), Spec-Datei im
# Zielrepo, Register der per `fabrik project add` bekannten Zielrepos.
PROJECTS_PATH_ENV = "FABRIK_PROJECTS_PATH"
TARGET_SPEC_RELPATH = Path(".fabrik") / "fabrik.yaml"
SPEC_GLOBS = ("*.fabrik.yaml", "*.fabrik.json")
REGISTRY_FILE = DATA_DIR / "state" / "registered-projects.json"
TEMPLATE_SUFFIX = ".loop.yaml"
# Worktree-Lease (ADR 0028): Eine lebende Lease darf nie ablaufen, solange ihr Run läuft.
# Invariante: WORKTREE_LEASE_TIMEOUT_S > Heartbeat-Intervall + Puffer.
# Der Cleanup hält den projektweiten Lock (`<projekt>.lock`) nur für die kurze
# Lease-Prüfung/den Claim, nie über die `repo_lock`-Erwerbung in `remove_worktree`
# (die bis zu ~600 s blockieren kann). Heartbeats lebender Runs können daher nie so
# lange blockiert werden, dass eine lebende Lease abläuft; das Timeout muss nur das
# Heartbeat-Intervall plus einen Puffer für ein blockiertes Schreiben übersteigen.
# 300 s > 30 s + Puffer.
WORKTREE_LEASE_SUFFIX = ".lease"
WORKTREE_LEASE_HEARTBEAT_S = 30.0
WORKTREE_LEASE_TIMEOUT_S = 300.0

DEFAULT_PORT = 8321
# Austauschverzeichnis Harness ↔ Agent im Worktree (lokal git-ignoriert): was der Agent lesen soll
# (Brief, Session-Plan) und was er abliefert (z. B. Audit-Report). Die Sandbox (ADR 0054) trägt es
# in den Container und zurück. Was darin auf seiner Seite bleiben soll (Arbeitskopien), steht in
# `HARNESS_LOCAL_FILE`-Dateien (gitignore-Syntax, pro Verzeichnis). Harness-interner Zustand gehört
# NICHT hierher — der Agent kann alles darin ändern.
HARNESS_DIR = ".fabrik-session"
# Harness-interner Zustand (Plan, Snapshot, Check-in) — NIE beim Agenten: Die Sandbox trägt
# HARNESS_DIR in den Container und zurück, aber dieses Verzeichnis bleibt auf dem Host.
HARNESS_PRIVATE_DIR = ".fabrik-harness"
HARNESS_LOCAL_FILE = ".fabrik-local"
DEFAULT_HOST = "127.0.0.1"
MAX_STEP_OUTPUT = 200_000  # chars kept per step in run records
# Agent-Konversation im Run-Log (runs/<projekt>/<id>.log): Caps, damit große
# Prompts/Texte das Log nicht sprengen (Volltext bleibt in der Session).
AGENT_LOG_PROMPT_CAP = 2000  # Zeichen des Agent-Prompts (Input)
AGENT_LOG_TEXT_CAP = 400  # Zeichen je Assistant-Message (Text-Part)

PROTECTED_DEFAULT = ["main", "master"]
# GitHub-API: Retry bei transienten Netzwerkfehlern (DNS-Flakes, Resets).
GH_RETRIES = 3
GH_RETRY_DELAY_S = 2.0
# GitHub-API: Obergrenze für `per_page` bei Listen-Endpunkten (API-Limit).
GH_MAX_PER_PAGE = 100
# Alert-Dedup (notify): Seiten-Größe des GitHub-Issue-Lookups (offene Issues
# mit Alert-Labels), gekappt auf GH_MAX_PER_PAGE. Der Lookup paginiert über
# alle Seiten (bis ALERT_LOOKUP_MAX_PAGES) — ein API-Call pro Seite.
ALERT_OPEN_ISSUES_PAGE = GH_MAX_PER_PAGE
# Alert-Dedup (notify): Fingerprint-Länge in Hex-Zeichen (16 = 64 Bit;
# Kollisionsrisiko für die wenigen Fehlerbilder pro Projekt/Loop vernachlässigbar).
ALERT_FP_LEN = 16
# Alert-Dedup (notify): Cap für den normalisierten Fehlertext im Fingerprint.
ALERT_REASON_CAP = 200
# Alert-Dedup (notify): Rate-Limit-Schutz — nach N Seiten wird der Issue-Lookup
# abgebrochen (ein Duplikat-Issue ist der günstigere Fehler als ein
# aufgebrauchtes API-Budget). 50 × ALERT_OPEN_ISSUES_PAGE (100) = 5000 Issues.
ALERT_LOOKUP_MAX_PAGES = 50
# Alert (notify): log_tail vor dem Versand pattern-basiert redigieren (ADR 0026).
ALERT_LOG_REDACTION = True
STEP_TYPES = (
    "shell",
    "git",
    "pr",
    "agent",
    "report",
    "http",
    "llm",
    "notify",
    "state",
    "verify",
    "review",
    "pr_review",
    "pr_merge",
    "issue",
    "human_gate",
    "alert_route",
    "escalation",
)
TRIGGER_TYPES = ("manual", "cron", "on_file", "on_issue", "on_pr", "on_webhook", "on_alert", "on_slack")
# Polling-Trigger mit `poll`-Intervall (Sekunden, endliche Zahl >= 0).
POLL_TRIGGER_TYPES = ("on_issue", "on_pr", "on_slack")
# on_slack (ADR 0040): Defaults der optionalen Trigger-Felder `max_pages` und `max_alert_chars`.
SLACK_DEFAULT_MAX_PAGES = 20  # conversations.history-Seiten pro Channel und Poll (je HISTORY_LIMIT Nachrichten)
SLACK_DEFAULT_MAX_ALERT_CHARS = 4000  # längere `alert_text` werden mit Marker gekürzt
# on_pr: zulässige PR-States für den `states`-Filter (GitHub-API-Werte).
PR_STATES = ("open", "closed")
# on_pr `reconcile: head`: Reconcile pro Head-SHA statt pro PR (ADR 0023) —
# jeder neue Push feuert genau einmal, auch nach verworfenem Trigger/Restart.
RECONCILE_PER_HEAD = "head"
# Step-Typen, die den Git-Worktree des Projekts brauchen (Checkout, Commit,
# Agent-Edits, Diff aus dem Worktree). Loops mit `worktree: false` dürfen sie
# nicht enthalten (ADR 0020).
WORKTREE_STEP_TYPES = ("git", "pr", "agent", "review", "pr_review")
# Workdir für Loops ohne Worktree: leeres Verzeichnis pro Run (ADR 0020).
SCRATCH_DIR = STATE_DIR / "scratch"

# Run-Store (ADR 0034): Backend und DB-URL, zur Aufrufzeit gelesen (Tests setzen die Env).
# Default `sql` (Umschaltung #278, Bestand per `fabrik migrate-runs`); `json` = alte Run-JSON-Ablage.
RUN_STORE_ENV = "FABRIK_RUN_STORE"
RUN_STORE_JSON = "json"
RUN_STORE_SQL = "sql"
RUN_STORE_BACKENDS = (RUN_STORE_JSON, RUN_STORE_SQL)
DB_URL_ENV = "FABRIK_DB_URL"
DB_FILENAME = "fabrik.db"
# SQLite: so lange (ms) auf einen Schreib-Lock eines anderen Prozesses warten, statt sofort zu scheitern.
SQLITE_BUSY_TIMEOUT_MS = 10_000


def run_store_backend() -> str:
    """Aktives Run-Store-Backend aus ``FABRIK_RUN_STORE`` (Default ``sql``); unbekannt → ValueError."""
    backend = os.environ.get(RUN_STORE_ENV, RUN_STORE_SQL).strip().lower() or RUN_STORE_SQL
    if backend not in RUN_STORE_BACKENDS:
        raise ValueError(
            t("config.expected_one_of", v1=RUN_STORE_ENV, backend=backend, v3=", ".join(RUN_STORE_BACKENDS))
        )
    return backend


def db_url() -> str:
    """SQLAlchemy-URL des SQL-Run-Stores: ``FABRIK_DB_URL`` oder SQLite im State-Dir."""
    return os.environ.get(DB_URL_ENV) or f"sqlite:///{STATE_DIR / DB_FILENAME}"


def runs_archive_dir() -> Path:
    """Archiv für importierte Run-JSON (ADR 0034 Nr. 8): ``state/runs-archive`` (Layout wie ``runs/``)."""
    return STATE_DIR / "runs-archive"


# Eskalation an einen Menschen (ADR 0059). Gleiche Werte wie in scripts/pr-status.py
# (Host-Skript ohne Paket-Import; tests/test_escalation.py prüft den Gleichstand).
HUMAN_LABEL = "needs human"
ESCALATION_KINDS = ("blocked", "stalled", "exhausted", "scope", "access")

# human_gate-Defaults (ADR 0010): Diff-Heuristiken für die Kritizitäts-Check.
# ADR-Pfade: Touch → immer menschliche Freigabe (Critical Decision).
HUMAN_GATE_ADR_PATHS = ["docs/adr"]
HUMAN_GATE_MAX_FILES = 40  # mehr geänderte Dateien → kritisch (großes Refactoring)
HUMAN_GATE_MAX_LINES = 1500  # mehr +/- Zeilen → kritisch
HUMAN_GATE_KEYWORDS = [
    "gdpr",
    "datenschutz",
    "data loss",
    "datenverlust",
    "drop table",
    "truncate",
    "migration",
    "irreversib",
    "krypto",
    "secret rotation",
]

# Parallele Runs pro Projekt (ADR 0028); überschreibbar per `max_parallel` in der Spec.
DEFAULT_MAX_PARALLEL = 3
DEFAULT_WORKERS = 4  # Worker-Pool-Größe für Trigger-Runs, überschreibbar via FABRIK_WORKERS


def configured_workers() -> int:
    """Konfigurierte Worker-Pool-Größe (FABRIK_WORKERS, Default :data:`DEFAULT_WORKERS`)."""
    return int(os.environ.get("FABRIK_WORKERS", str(DEFAULT_WORKERS)))


LAUNCHER_ENV = "FABRIK_LAUNCHER"
DEFAULT_LAUNCHER = "inprocess"  # Ausführungs-Substrat für Runs (launcher.registry)


def configured_launcher() -> str:
    """Konfiguriertes Run-Launcher-Substrat (FABRIK_LAUNCHER, Default :data:`DEFAULT_LAUNCHER`).

    Leerer Wert zählt als nicht gesetzt. Die gültigen Namen liefert
    :func:`fabrik.launcher.registry.known_launchers`."""
    return os.environ.get(LAUNCHER_ENV, "").strip() or DEFAULT_LAUNCHER


# Variablen, die einen Run an ein Issue/PR binden (Dedup: ein aktiver Run pro Loop + Bezug).
DEDUPE_VARS = ("issue_number", "pr_number")

_log_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_id() -> str:
    # uuid-Suffix (vollständiges uuid4, 128 Bit): zwei Runs im selben
    # Sekundentakt (z. B. parallele Trigger) dürfen sich nicht auf dieselbe
    # Run-Datei schreiben. 6 Hex-Zellen reichten nicht (Geburtstagsparadoxon:
    # ~11 % Kollision bei 2000 Calls).
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex

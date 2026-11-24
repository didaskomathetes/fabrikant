"""Central configuration: paths, constants, env loading, time, log lock.

Single source of truth for the runnable paths (RUNS_DIR, PROJECTS_DIR, …).
All modules reference `config.RUNS_DIR` / `config.PROJECTS_DIR` via
attribute access (not `from .config import RUNS_DIR`), so tests can swap the paths
via `monkeypatch.setattr(fabrik.config, "RUNS_DIR", …)`.
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

# Read-only assets (web UI, loop templates, relay prompts, example specs) ship inside the package (ADR 0069).
PACKAGE_DIR = Path(__file__).resolve().parent
_CHECKOUT_ROOT = PACKAGE_DIR.parent.parent
APP_DIR_NAME = "fabrik"


def is_source_checkout(root: Path) -> bool:
    """True if `root` is a fabrik git/source checkout (`pyproject.toml` next to `src/fabrik`)."""
    return (root / "pyproject.toml").is_file() and (root / "src" / APP_DIR_NAME).is_dir()


def default_home(env: Mapping[str, str], checkout_root: Path = _CHECKOUT_ROOT) -> Path:
    """Writable installation home (runs, state, sessionlog, `fabrik.env`), ADR 0069.

    `FABRIK_HOME` wins; a source checkout uses its own root (legacy/dev mode); an installed
    wheel falls back to `$XDG_DATA_HOME/fabrik` (default `~/.local/share/fabrik`)."""
    explicit = env.get("FABRIK_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_source_checkout(checkout_root):
        return checkout_root
    data_home = env.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


FABRIK_HOME = default_home(os.environ)


def _load_env_file(p: Path):
    """fabrik.env: KEY=VALUE lines (os.environ wins).

    Explicitly UTF-8: loading runs on every `import fabrik` — under
    `LC_ALL=C` (container, systemd) the default encoding would be ASCII and
    non-ASCII bytes in the file would crash the import.
    """
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Load before all env-dependent constants: e.g. FABRIK_DATA also takes effect from fabrik.env.
_load_env_file(FABRIK_HOME / "fabrik.env")

# Data/config directory of the installation (own specs, templates). Default
# FABRIK_HOME: without FABRIK_DATA the factory behaves as before (spec resolution: ADR 0047).
DATA_DIR = Path(os.environ.get("FABRIK_DATA", FABRIK_HOME))
PROJECTS_DIR = DATA_DIR / "projects"  # primary (writable) spec folder
BUNDLED_PROJECTS_DIR = (
    FABRIK_HOME / "projects"
)  # lowest priority; the starter spec in `examples/` is only copied by `fabrik init`
TEMPLATES_DIR = PACKAGE_DIR / "templates"  # mitgelieferte Loop-Vorlagen
USER_TEMPLATES_DIR = DATA_DIR / "templates"
RUNS_DIR = FABRIK_HOME / "runs"
WORKTREES_ENV = "FABRIK_WORKTREES"
XDG_STATE_DEFAULT = Path(".local") / "state"  # relative to $HOME (XDG Base Directory spec)
INSTALL_ID_HASH_LEN = 8


def install_id(home: Path) -> str:
    """Stable identifier of an installation (`<folder>-<hash>`): separates the worktrees of two
    checkouts whose projects have the same names (otherwise one cleans up the other's runs)."""
    resolved = home.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:INSTALL_ID_HASH_LEN]
    return f"{resolved.name}-{digest}"


def default_worktrees_dir(home: Path, env: Mapping[str, str]) -> Path:
    """Worktree root (ADR 0047, addendum 2026-11-03): `FABRIK_WORKTREES`, otherwise
    `$XDG_STATE_HOME/fabrik/worktrees/<install_id>`.

    Deliberately outside every source checkout: IDEs/indexers/file watchers watching the
    repo would otherwise index every run checkout (including `.venv`). The path is outside
    `fabrik/` anyway (OpenCode loads AGENTS.md from the workdir upwards)."""
    explicit = env.get(WORKTREES_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_home = env.get("XDG_STATE_HOME", "").strip()
    if state_home:
        base = Path(state_home).expanduser()
        if not base.is_absolute():  # XDG spec: absolute; relative → defensively anchored to $HOME
            base = Path.home() / base
    else:
        base = Path.home() / XDG_STATE_DEFAULT
    return base / "fabrik" / "worktrees" / install_id(home)


WORKTREES_DIR = default_worktrees_dir(FABRIK_HOME, os.environ)
# Until 2026-11 worktrees lived in the repo (`<repo>/.fabrik/worktrees`). Now only read and
# cleaned up, so paused runs (`needs_approval`) find their worktree on resume.
LEGACY_WORKTREES_DIR = FABRIK_HOME.parent / ".fabrik" / "worktrees"
WEB_DIR = PACKAGE_DIR / "web"
STATE_DIR = FABRIK_HOME / "state"
# Spec resolution (ADR 0047): search-path env (colon-separated like PATH), spec file in the
# target repo, register of the target repos known via `fabrik project add`.
PROJECTS_PATH_ENV = "FABRIK_PROJECTS_PATH"
TARGET_SPEC_RELPATH = Path(".fabrik") / "fabrik.yaml"
SPEC_GLOBS = ("*.fabrik.yaml", "*.fabrik.json")
REGISTRY_FILE = DATA_DIR / "state" / "registered-projects.json"
TEMPLATE_SUFFIX = ".loop.yaml"
# Worktree lease (ADR 0028): a live lease must never expire while its run is running.
# Invariant: WORKTREE_LEASE_TIMEOUT_S > heartbeat interval + buffer.
# The cleanup holds the project-wide lock (`<project>.lock`) only for the short
# lease check/claim, never across the `repo_lock` acquisition in `remove_worktree`
# (which can block for up to ~600 s). Heartbeats of live runs therefore can never be
# blocked so long that a live lease expires; the timeout only has to exceed the
# heartbeat interval plus a buffer for a blocked write.
# 300 s > 30 s + buffer.
WORKTREE_LEASE_SUFFIX = ".lease"
WORKTREE_LEASE_HEARTBEAT_S = 30.0
WORKTREE_LEASE_TIMEOUT_S = 300.0

DEFAULT_PORT = 8321
# Exchange directory harness ↔ agent in the worktree (locally git-ignored): what the agent should read
# (brief, session plan) and what it delivers (e.g. audit report). The sandbox (ADR 0054) carries it
# into the container and back. What should stay on its side (working copies) is listed in
# `HARNESS_LOCAL_FILE` files (gitignore syntax, per directory). Harness-internal state does
# NOT belong here — the agent can change anything in it.
HARNESS_DIR = ".fabrik-session"
# Harness-internal state (plan, snapshot, check-in) — NEVER at the agent: the sandbox carries
# HARNESS_DIR into the container and back, but this directory stays on the host.
HARNESS_PRIVATE_DIR = ".fabrik-harness"
HARNESS_LOCAL_FILE = ".fabrik-local"
DEFAULT_HOST = "127.0.0.1"
MAX_STEP_OUTPUT = 200_000  # chars kept per step in run records
# Agent conversation in the run log (runs/<project>/<id>.log): caps so that large
# prompts/texts do not blow up the log (full text stays in the session).
AGENT_LOG_PROMPT_CAP = 2000  # Zeichen des Agent-Prompts (Input)
AGENT_LOG_TEXT_CAP = 400  # Zeichen je Assistant-Message (Text-Part)

PROTECTED_DEFAULT = ["main", "master"]
# GitHub-API: Retry bei transienten Netzwerkfehlern (DNS-Flakes, Resets).
GH_RETRIES = 3
GH_RETRY_DELAY_S = 2.0
# GitHub API: upper bound for `per_page` on list endpoints (API limit).
GH_MAX_PER_PAGE = 100
# Alert dedup (notify): page size of the GitHub issue lookup (open issues
# with alert labels), capped at GH_MAX_PER_PAGE. The lookup paginates over
# all pages (up to ALERT_LOOKUP_MAX_PAGES) — one API call per page.
ALERT_OPEN_ISSUES_PAGE = GH_MAX_PER_PAGE
# Alert dedup (notify): fingerprint length in hex characters (16 = 64 bit;
# collision risk negligible for the few failure patterns per project/loop).
ALERT_FP_LEN = 16
# Alert dedup (notify): cap for the normalized error text in the fingerprint.
ALERT_REASON_CAP = 200
# Alert dedup (notify): rate-limit protection — after N pages the issue lookup is
# aborted (a duplicate issue is the cheaper error compared to an
# exhausted API budget). 50 × ALERT_OPEN_ISSUES_PAGE (100) = 5000 issues.
ALERT_LOOKUP_MAX_PAGES = 50
# Alert (notify): redact log_tail pattern-based before sending (ADR 0026).
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
# on_slack (ADR 0040): defaults of the optional trigger fields `max_pages` and `max_alert_chars`.
SLACK_DEFAULT_MAX_PAGES = 20  # conversations.history-Seiten pro Channel und Poll (je HISTORY_LIMIT Nachrichten)
SLACK_DEFAULT_MAX_ALERT_CHARS = 4000  # longer `alert_text` is truncated with a marker
# on_pr: allowed PR states for the `states` filter (GitHub API values).
PR_STATES = ("open", "closed")
# on_pr `reconcile: head`: reconcile per head SHA instead of per PR (ADR 0023) —
# every new push fires exactly once, even after a discarded trigger/restart.
RECONCILE_PER_HEAD = "head"
# Step types that need the project's git worktree (checkout, commit,
# agent edits, diff from the worktree). Loops with `worktree: false` must
# not contain them (ADR 0020).
WORKTREE_STEP_TYPES = ("git", "pr", "agent", "review", "pr_review")
# Workdir for loops without worktree: empty directory per run (ADR 0020).
SCRATCH_DIR = STATE_DIR / "scratch"

# Run store (ADR 0034): backend and DB URL, read at call time (tests set the env).
# Default `sql` (switch #278, existing data via `fabrik migrate-runs`); `json` = old run-JSON storage.
RUN_STORE_ENV = "FABRIK_RUN_STORE"
RUN_STORE_JSON = "json"
RUN_STORE_SQL = "sql"
RUN_STORE_BACKENDS = (RUN_STORE_JSON, RUN_STORE_SQL)
DB_URL_ENV = "FABRIK_DB_URL"
DB_FILENAME = "fabrik.db"
# SQLite: wait this long (ms) for a write lock of another process instead of failing immediately.
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
    """SQLAlchemy URL of the SQL run store: ``FABRIK_DB_URL`` or SQLite in the state dir."""
    return os.environ.get(DB_URL_ENV) or f"sqlite:///{STATE_DIR / DB_FILENAME}"


def runs_archive_dir() -> Path:
    """Archive for imported run JSON (ADR 0034 no. 8): ``state/runs-archive`` (layout like ``runs/``)."""
    return STATE_DIR / "runs-archive"


# Escalation to a human (ADR 0059). Same values as in scripts/pr-status.py
# (host script without package import; tests/test_escalation.py checks they match).
HUMAN_LABEL = "needs human"
ESCALATION_KINDS = ("blocked", "stalled", "exhausted", "scope", "access")

# human_gate defaults (ADR 0010): diff heuristics for the criticality check.
# ADR paths: touch → always human approval (critical decision).

HUMAN_GATE_ADR_PATHS = ["docs/adr"]
HUMAN_GATE_MAX_FILES = 40  # more changed files → critical (large refactoring)
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

# Parallel runs per project (ADR 0028); overridable via `max_parallel` in the spec.
DEFAULT_MAX_PARALLEL = 3
DEFAULT_WORKERS = 4  # Worker pool size for trigger runs, overridable via FABRIK_WORKERS


def configured_workers() -> int:
    """Configured worker pool size (FABRIK_WORKERS, default :data:`DEFAULT_WORKERS`)."""
    return int(os.environ.get("FABRIK_WORKERS", str(DEFAULT_WORKERS)))


LAUNCHER_ENV = "FABRIK_LAUNCHER"
DEFAULT_LAUNCHER = "inprocess"  # Execution substrate for runs (launcher.registry)


def configured_launcher() -> str:
    """Configured run-launcher substrate (FABRIK_LAUNCHER, default :data:`DEFAULT_LAUNCHER`).

    An empty value counts as unset. The valid names are provided by
    :func:`fabrik.launcher.registry.known_launchers`."""
    return os.environ.get(LAUNCHER_ENV, "").strip() or DEFAULT_LAUNCHER


# Variables that bind a run to an issue/PR (dedup: one active run per loop + reference).
DEDUPE_VARS = ("issue_number", "pr_number")

_log_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_id() -> str:
    # uuid suffix (full uuid4, 128 bit): two runs in the same
    # second (e.g. parallel triggers) must not write to the same
    # run file. 6 hex digits were not enough (birthday paradox:
    # ~11 % collision at 2000 calls).
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex

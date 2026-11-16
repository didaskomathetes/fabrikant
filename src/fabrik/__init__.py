"""fabrik — die Software-Fabrik.

Dev-Loops als deklarativer Code: Specs (YAML) → Engine (Steps) → Runs (JSON).
P0: lokal, dateibasiert, stateless pro Run. P1: Agent-Backends + SQLite.
P2: Postgres + Queue + stateless Worker. P3: ECS/GKE.

Modul-Layout:
  config    Pfade, Konstanten, Env-Loading, Zeit, Locks
  secrets   Secret-Masking (env + Env-Dateien + Spec-env)
  context   Output-Cap, Variable-Substitution, Risk-Tier-Policy
  specs     Spec-Loading & -Validierung
  branches  Branch-Overrides (state/branch-overrides.json, ADR 0004)
  gitops    Git-Worktrees, GitHub-API, HTTP-POST
  runs      Run-Records (runs/<projekt>/<id>.json)
  runsearch Run-Historie: Filter, Freitext, Cursor-Paginierung (Issue #163, ADR 0034)
  backends/ Agent-Backends (opencode)
  steps/    Step-Implementierungen (shell, git, pr, agent, http, llm, notify)
  engine    run_loop, Step-Dispatch, Worker-Pool
  scheduler Cron + on_file + on_issue + on_pr
  chat      Chat-Befehle + LLM-Copilot
  server    HTTP API + UI
  cli       Kommandozeile
"""

from . import config, llm
from .backends import OpenCodeBackend
from .chat import _chat, _chat_alert_test, _chat_resume, _chat_start_run
from .config import now_id, utcnow
from .engine import _get_run_pool, _safe_run, _WorkerPool, run_loop
from .runs import claim_run, get_run, get_run_log, list_runs, runs_dir_for
from .scheduler import Scheduler, cron_matches
from .secrets import _redact, _redact_record
from .server import FabrikHandler, serve
from .specs import _validate_spec, load_projects, load_spec, spec_path
from .steps import _SCHEMA_TYPES, _notify, _validate_schema

__all__ = [
    "OpenCodeBackend",
    "Scheduler",
    "FabrikHandler",
    "config",
    "claim_run",
    "cron_matches",
    "get_run",
    "get_run_log",
    "list_runs",
    "llm",
    "load_projects",
    "load_spec",
    "now_id",
    "run_loop",
    "runs_dir_for",
    "serve",
    "spec_path",
    "utcnow",
    "_SCHEMA_TYPES",
    "_get_run_pool",
    "_notify",
    "_redact",
    "_redact_record",
    "_safe_run",
    "_validate_schema",
    "_validate_spec",
    "_WorkerPool",
    "_chat",
    "_chat_alert_test",
    "_chat_resume",
    "_chat_start_run",
]

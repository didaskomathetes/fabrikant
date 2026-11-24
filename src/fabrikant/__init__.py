"""fabrikant — the software factory.

Dev loops as declarative code: specs (YAML) → engine (steps) → runs (JSON).
P0: local, file-based, stateless per run. P1: agent backends + SQLite.
P2: Postgres + queue + stateless workers. P3: ECS/GKE.

Module layout:
  config    paths, constants, env loading, time, locks
  secrets   secret masking (env + env files + spec env)
  context   output cap, variable substitution, risk-tier policy
  specs     spec loading & validation
  branches  branch overrides (state/branch-overrides.json, ADR 0004)
  gitops    git worktrees, GitHub API, HTTP POST
  runs      run records (runs/<project>/<id>.json)
  runsearch run history: filters, full-text, cursor pagination (issue #163, ADR 0034)
  backends/ agent backends (opencode)
  steps/    step implementations (shell, git, pr, agent, http, llm, notify)
  engine    run_loop, step dispatch, worker pool
  scheduler cron + on_file + on_issue + on_pr
  chat      chat commands + LLM copilot
  server    HTTP API + UI
  cli       command line
"""

from . import _bootstrap, config, llm  # noqa: F401  (_bootstrap first: legacy env adoption, ADR 0070)
from .backends import OpenCodeBackend
from .chat import _chat, _chat_alert_test, _chat_resume, _chat_start_run
from .config import now_id, utcnow
from .engine import _get_run_pool, _safe_run, _WorkerPool, run_loop
from .launcher import InProcessLauncher, RunLauncher, create_launcher
from .runs import claim_run, get_run, get_run_log, list_runs, runs_dir_for
from .scheduler import Scheduler, cron_matches
from .secrets import _redact, _redact_record
from .server import FabrikantHandler, serve
from .specs import _validate_spec, load_projects, load_spec, spec_path
from .steps import _SCHEMA_TYPES, _notify, _validate_schema

__all__ = [
    "OpenCodeBackend",
    "Scheduler",
    "FabrikantHandler",
    "config",
    "claim_run",
    "cron_matches",
    "create_launcher",
    "get_run",
    "get_run_log",
    "list_runs",
    "llm",
    "load_projects",
    "load_spec",
    "InProcessLauncher",
    "now_id",
    "run_loop",
    "runs_dir_for",
    "RunLauncher",
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

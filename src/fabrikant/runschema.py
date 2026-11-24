"""Relational schema of the SQL run store (ADR 0034 no. 4) — SQLAlchemy Core, no ORM.

Strictly relational, no JSON columns in the core: runs, steps, usage, review results (+ findings),
verify checks, approvals (+ permissions), run variables (key/value with type) and the session log
(events, artifacts, checkpoints; ADR 0002). Status columns are restricted via ``CHECK`` to the enum values
from :mod:`fabrikant.runmodel`, child tables hang off the run via foreign key (``ON DELETE
CASCADE``).

The only opaque column: ``run_events.data`` — the foreign payload of a session-log event (e.g.
tool calls of the agent backends) as redacted JSON text, never interpreted by the store.

This module is the target definition for ``alembic`` (``target_metadata``). Every change needs
a new revision under ``fabrikant/migrations/versions``; a test compares ``upgrade head`` with
these tables.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
)

from .runmodel import ApprovalReason, ApprovalStatus, FailureKind, RunStatus, StepStatus

# Benennungskonvention: stabile Constraint-Namen (Alembic-Migrationen, Fehlermeldungen in Tests).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class VarScope(StrEnum):
    """Origin of a run variable: ``vars`` (resume context) or ``queued_vars`` (trigger variables
    of a queued run)."""

    VARS = "vars"
    QUEUED_VARS = "queued_vars"


class VarType(StrEnum):
    """Python type of a variable value (the value itself is stored as text in ``run_vars.value``)."""

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"


def in_enum(column: str, enum_cls: type[StrEnum]) -> str:
    """SQL expression ``column IN ('a', 'b', …)`` over all values of ``enum_cls`` (for ``CHECK``)."""
    values = ", ".join(f"'{e.value}'" for e in enum_cls)
    return f"{column} IN ({values})"


def _nullable_in_enum(column: str, enum_cls: type[StrEnum]) -> str:
    return f"{column} IS NULL OR {in_enum(column, enum_cls)}"


_FAILED_RUN = "status IN ('fail', 'error')"
_FAILED_STEP = "status IN ('fail', 'timeout', 'error')"


def _run_fk() -> ForeignKey:
    return ForeignKey("runs.id", ondelete="CASCADE")


runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text, nullable=False),
    Column("loop", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("started", Text, nullable=False, server_default=""),
    # Normalized UTC time of ``started`` (fixed width) for date filters; NULL = unparseable.
    Column("started_utc", Text),
    Column("finished", Text),
    Column("trigger", Text),
    Column("note", Text),
    Column("spec_hash", Text),
    Column("waiting_step", Text),
    Column("resumed", Boolean),
    Column("approval_queued", Text),
    Column("started_by", Text),
    Column("approved_by", Text),
    Column("rejected_by", Text),
    Column("error", Text),
    Column("failure_kind", Text),
    Column("retry_after", Text),
    Column("retry_of", Text),
    Column("replay_of", Text),
    Column("attempt", Integer),
    Column("trigger_key", Text),
    Column("worktree_cleanup_error", Text),
    Column("scratch_cleanup_error", Text),
    Column("state_merge_error", Text),
    CheckConstraint("id <> ''", name="id_not_empty"),
    CheckConstraint("project <> ''", name="project_not_empty"),
    CheckConstraint(in_enum("status", RunStatus), name="status"),
    CheckConstraint(_nullable_in_enum("failure_kind", FailureKind), name="failure_kind"),
    # ADR 0035: only failed runs carry a failure class.
    CheckConstraint(f"failure_kind IS NULL OR {_FAILED_RUN}", name="failure_kind_only_failed"),
    CheckConstraint("attempt IS NULL OR attempt >= 0", name="attempt_non_negative"),
)
Index("ix_runs_order", runs.c.started, runs.c.project, runs.c.id)
Index("ix_runs_project_order", runs.c.project, runs.c.started, runs.c.id)
Index("ix_runs_project_loop_status", runs.c.project, runs.c.loop, runs.c.status)
Index("ix_runs_status", runs.c.status)
Index("ix_runs_started_utc", runs.c.started_utc)

steps = Table(
    "steps",
    metadata,
    Column("run_id", Text, _run_fk(), primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("duration_s", Float),
    Column("summary", Text),
    Column("output", Text),
    Column("failure_kind", Text),
    Column("retry_after", Text),
    Column("verdict", Text),  # fachliches Ergebnis (Migration 0003)
    CheckConstraint("idx >= 0", name="idx_non_negative"),
    CheckConstraint(in_enum("status", StepStatus), name="status"),
    CheckConstraint(_nullable_in_enum("failure_kind", FailureKind), name="failure_kind"),
    CheckConstraint(f"failure_kind IS NULL OR {_FAILED_STEP}", name="failure_kind_only_failed"),
)


def _step_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["run_id", "step_idx"], ["steps.run_id", "steps.idx"], ondelete="CASCADE")


step_usage = Table(
    "step_usage",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("step_idx", Integer, primary_key=True),
    Column("model", Text),
    Column("provider", Text),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("total_tokens", Integer),
    Column("cost_usd", Float),
    Column("cache_read_tokens", Integer),
    Column("cache_write_tokens", Integer),
    Column("billing", Text),
    _step_fk(),
)

review_results = Table(
    "review_results",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("step_idx", Integer, primary_key=True),
    Column("verdict", Text, nullable=False),
    Column("model", Text),
    Column("summary", Text),
    Column("pr", Integer),
    Column("round", Integer),
    CheckConstraint("round IS NULL OR round >= 1", name="round_positive"),
    _step_fk(),
)

review_findings = Table(
    "review_findings",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("step_idx", Integer, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("severity", Text),
    Column("text", Text),
    Column("suggestion", Text),
    ForeignKeyConstraint(
        ["run_id", "step_idx"], ["review_results.run_id", "review_results.step_idx"], ondelete="CASCADE"
    ),
)

verify_checks = Table(
    "verify_checks",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("step_idx", Integer, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("output", Text),
    _step_fk(),
)

approvals = Table(
    "approvals",
    metadata,
    Column("run_id", Text, _run_fk(), primary_key=True),
    # Abgeleiteter Zustand (ADR 0034 Nr. 2): pending | queued | approved | rejected.
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("kind", Text),  # Eskalationsart bei reason = escalation (ADR 0059)
    Column("step", Text),
    Column("condition", Text),
    Column("why", Text),
    Column("what", Text),
    Column("issue", Text),
    Column("pr", Text),
    Column("pr_title", Text),
    Column("tier", Integer),
    CheckConstraint(in_enum("status", ApprovalStatus), name="status"),
    CheckConstraint(_nullable_in_enum("reason", ApprovalReason), name="reason"),
)

approval_permissions = Table(
    "approval_permissions",
    metadata,
    Column("run_id", Text, ForeignKey("approvals.run_id", ondelete="CASCADE"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("permission", Text, nullable=False),
)

run_vars = Table(
    "run_vars",
    metadata,
    Column("run_id", Text, _run_fk(), primary_key=True),
    Column("scope", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("value", Text, nullable=False),
    CheckConstraint(in_enum("scope", VarScope), name="scope"),
    CheckConstraint(in_enum("type", VarType), name="type"),
)

# ---------------------------------------------------------------- Session-Log (ADR 0002 → ADR 0034 Nr. 7)
run_events = Table(
    "run_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, _run_fk(), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("ts", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("step_name", Text, nullable=False, server_default=""),
    Column("step_type", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default=""),
    # Opake Fremd-Payload (redigierter JSON-Text), siehe Moduldoku.
    Column("data", Text, nullable=False, server_default="{}"),
    CheckConstraint("kind <> ''", name="kind_not_empty"),
)
Index("ix_run_events_run_seq", run_events.c.run_id, run_events.c.seq)

run_artifacts = Table(
    "run_artifacts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, _run_fk(), nullable=False),
    Column("step_name", Text, nullable=False, server_default=""),
    Column("kind", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)
Index("ix_run_artifacts_run", run_artifacts.c.run_id)

run_checkpoints = Table(
    "run_checkpoints",
    metadata,
    Column("run_id", Text, _run_fk(), primary_key=True),
    Column("step_idx", Integer, primary_key=True),
    Column("step_name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("data", Text, nullable=False, server_default="{}"),
)

# Child tables of a run record (without session log), in insertion order (foreign keys).
RECORD_CHILD_TABLES = (
    steps,
    step_usage,
    review_results,
    review_findings,
    verify_checks,
    approvals,
    approval_permissions,
    run_vars,
)

# Named connections (ADR 0052): LLM providers and agent backends, managed in the UI.
# ``secret`` is Fernet-encrypted (API key or server password), never plaintext.
connections = Table(
    "connections",
    metadata,
    Column("name", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("driver", Text, nullable=False, server_default=""),
    Column("endpoint", Text, nullable=False, server_default=""),
    Column("model", Text, nullable=False, server_default=""),
    Column("username", Text, nullable=False, server_default=""),
    Column("secret", Text, nullable=False, server_default=""),
    Column("is_default", Boolean, nullable=False, server_default="0"),
    Column("updated_at", Text, nullable=False, server_default=""),
    CheckConstraint("kind IN ('llm', 'agent')", name="kind_known"),
)

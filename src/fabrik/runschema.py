"""Relationales Schema des SQL-Run-Stores (ADR 0034 Nr. 4) — SQLAlchemy Core, kein ORM.

Strikt relational, keine JSON-Spalten im Kern: Runs, Steps, Usage, Review-Ergebnisse (+ Findings),
Verify-Checks, Approvals (+ Rechte), Run-Variablen (Key/Value mit Typ) und das Session-Log
(Events, Artefakte, Checkpoints; ADR 0002). Status-Spalten sind per ``CHECK`` auf die Enum-Werte
aus :mod:`fabrik.runmodel` beschränkt, Kind-Tabellen hängen per Fremdschlüssel (``ON DELETE
CASCADE``) am Run.

Einzige opake Spalte: ``run_events.data`` — die fremde Payload eines Session-Log-Events (z. B.
Tool-Calls der Agent-Backends) als redigierter JSON-Text, vom Store nie interpretiert.

Dieses Modul ist die Soll-Definition für ``alembic`` (``target_metadata``). Jede Änderung braucht
eine neue Revision unter ``fabrik/migrations/versions``; ein Test vergleicht ``upgrade head`` mit
diesen Tabellen.
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
    """Herkunft einer Run-Variable: ``vars`` (Resume-Kontext) oder ``queued_vars`` (Trigger-Variablen
    eines eingereihten Runs)."""

    VARS = "vars"
    QUEUED_VARS = "queued_vars"


class VarType(StrEnum):
    """Python-Typ eines Variablenwerts (der Wert selbst steht als Text in ``run_vars.value``)."""

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"


def in_enum(column: str, enum_cls: type[StrEnum]) -> str:
    """SQL-Ausdruck ``column IN ('a', 'b', …)`` über alle Werte von ``enum_cls`` (für ``CHECK``)."""
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
    # Normalisierte UTC-Zeit von ``started`` (fester Breite) für Datumsfilter; NULL = unparsebar.
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
    Column("attempt", Integer),
    Column("trigger_key", Text),
    Column("worktree_cleanup_error", Text),
    Column("scratch_cleanup_error", Text),
    Column("state_merge_error", Text),
    CheckConstraint("id <> ''", name="id_not_empty"),
    CheckConstraint("project <> ''", name="project_not_empty"),
    CheckConstraint(in_enum("status", RunStatus), name="status"),
    CheckConstraint(_nullable_in_enum("failure_kind", FailureKind), name="failure_kind"),
    # ADR 0035: nur gescheiterte Runs tragen eine Fehlerklasse.
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

# Kind-Tabellen eines Run-Records (ohne Session-Log), in Einfüge-Reihenfolge (Fremdschlüssel).
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

# Benannte Verbindungen (ADR 0052): LLM-Provider und Agent-Backends, in der UI verwaltet.
# ``secret`` ist Fernet-verschlüsselt (API-Key bzw. Server-Passwort), nie Klartext.
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

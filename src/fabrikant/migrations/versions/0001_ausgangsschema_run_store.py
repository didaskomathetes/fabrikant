"""Initial run-store schema (ADR 0034, #277): runs, steps, review/verify, approvals, variables, session log.

Frozen: status values are literals here, not from the enums — an enum change
needs a new revision (a test checks ``upgrade head`` against :mod:`fabrikant.runschema`).

Revision ID: 0001
Revises: -
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("loop", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started", sa.Text(), server_default="", nullable=False),
        sa.Column("started_utc", sa.Text(), nullable=True),
        sa.Column("finished", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("spec_hash", sa.Text(), nullable=True),
        sa.Column("waiting_step", sa.Text(), nullable=True),
        sa.Column("resumed", sa.Boolean(), nullable=True),
        sa.Column("approval_queued", sa.Text(), nullable=True),
        sa.Column("started_by", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("rejected_by", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("retry_after", sa.Text(), nullable=True),
        sa.Column("retry_of", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("trigger_key", sa.Text(), nullable=True),
        sa.Column("worktree_cleanup_error", sa.Text(), nullable=True),
        sa.Column("scratch_cleanup_error", sa.Text(), nullable=True),
        sa.Column("state_merge_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "failure_kind IS NULL OR failure_kind IN ('interrupted', 'transient', 'environment', 'outcome', 'unknown')",
            name=op.f("ck_runs_failure_kind"),
        ),
        sa.CheckConstraint(
            "failure_kind IS NULL OR status IN ('fail', 'error')", name=op.f("ck_runs_failure_kind_only_failed")
        ),
        sa.CheckConstraint("id <> ''", name=op.f("ck_runs_id_not_empty")),
        sa.CheckConstraint("project <> ''", name=op.f("ck_runs_project_not_empty")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'needs_approval', 'pass', 'fail', 'error', 'rejected')",
            name=op.f("ck_runs_status"),
        ),
        sa.CheckConstraint("attempt IS NULL OR attempt >= 0", name=op.f("ck_runs_attempt_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index("ix_runs_order", ["started", "project", "id"], unique=False)
        batch_op.create_index("ix_runs_project_loop_status", ["project", "loop", "status"], unique=False)
        batch_op.create_index("ix_runs_project_order", ["project", "started", "id"], unique=False)
        batch_op.create_index("ix_runs_started_utc", ["started_utc"], unique=False)
        batch_op.create_index("ix_runs_status", ["status"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("step", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("why", sa.Text(), nullable=True),
        sa.Column("what", sa.Text(), nullable=True),
        sa.Column("issue", sa.Text(), nullable=True),
        sa.Column("pr", sa.Text(), nullable=True),
        sa.Column("pr_title", sa.Text(), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN ('step', 'gate', 'tier', 'criticality')", name=op.f("ck_approvals_reason")
        ),
        sa.CheckConstraint("status IN ('pending', 'queued', 'approved', 'rejected')", name=op.f("ck_approvals_status")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_approvals_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_approvals")),
    )
    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_name", sa.Text(), server_default="", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_artifacts_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_artifacts")),
    )
    with op.batch_alter_table("run_artifacts", schema=None) as batch_op:
        batch_op.create_index("ix_run_artifacts_run", ["run_id"], unique=False)

    op.create_table(
        "run_checkpoints",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_checkpoints_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "step_idx", name=op.f("pk_run_checkpoints")),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("step_name", sa.Text(), server_default="", nullable=False),
        sa.Column("step_type", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="", nullable=False),
        sa.Column("data", sa.Text(), server_default="{}", nullable=False),
        sa.CheckConstraint("kind <> ''", name=op.f("ck_run_events_kind_not_empty")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
    )
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_index("ix_run_events_run_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "run_vars",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.CheckConstraint("scope IN ('vars', 'queued_vars')", name=op.f("ck_run_vars_scope")),
        sa.CheckConstraint("type IN ('str', 'int', 'float', 'bool')", name=op.f("ck_run_vars_type")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_vars_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "scope", "name", name=op.f("pk_run_vars")),
    )
    op.create_table(
        "steps",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("retry_after", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "failure_kind IS NULL OR failure_kind IN ('interrupted', 'transient', 'environment', 'outcome', 'unknown')",
            name=op.f("ck_steps_failure_kind"),
        ),
        sa.CheckConstraint(
            "failure_kind IS NULL OR status IN ('fail', 'timeout', 'error')",
            name=op.f("ck_steps_failure_kind_only_failed"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'waiting', 'pass', 'fail', 'timeout', 'error', 'skipped')",
            name=op.f("ck_steps_status"),
        ),
        sa.CheckConstraint("idx >= 0", name=op.f("ck_steps_idx_non_negative")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_steps_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "idx", name=op.f("pk_steps")),
    )
    op.create_table(
        "approval_permissions",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["approvals.run_id"], name=op.f("fk_approval_permissions_run_id_approvals"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_approval_permissions")),
    )
    op.create_table(
        "review_results",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("pr", sa.Integer(), nullable=True),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.CheckConstraint("round IS NULL OR round >= 1", name=op.f("ck_review_results_round_positive")),
        sa.ForeignKeyConstraint(
            ["run_id", "step_idx"],
            ["steps.run_id", "steps.idx"],
            name=op.f("fk_review_results_run_id_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "step_idx", name=op.f("pk_review_results")),
    )
    op.create_table(
        "step_usage",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id", "step_idx"],
            ["steps.run_id", "steps.idx"],
            name=op.f("fk_step_usage_run_id_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "step_idx", name=op.f("pk_step_usage")),
    )
    op.create_table(
        "verify_checks",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id", "step_idx"],
            ["steps.run_id", "steps.idx"],
            name=op.f("fk_verify_checks_run_id_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "step_idx", "seq", name=op.f("pk_verify_checks")),
    )
    op.create_table(
        "review_findings",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id", "step_idx"],
            ["review_results.run_id", "review_results.step_idx"],
            name=op.f("fk_review_findings_run_id_review_results"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "step_idx", "seq", name=op.f("pk_review_findings")),
    )


def downgrade() -> None:
    op.drop_table("review_findings")
    op.drop_table("verify_checks")
    op.drop_table("step_usage")
    op.drop_table("review_results")
    op.drop_table("approval_permissions")
    op.drop_table("steps")
    op.drop_table("run_vars")
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.drop_index("ix_run_events_run_seq")

    op.drop_table("run_events")
    op.drop_table("run_checkpoints")
    with op.batch_alter_table("run_artifacts", schema=None) as batch_op:
        batch_op.drop_index("ix_run_artifacts_run")

    op.drop_table("run_artifacts")
    op.drop_table("approvals")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_status")
        batch_op.drop_index("ix_runs_started_utc")
        batch_op.drop_index("ix_runs_project_order")
        batch_op.drop_index("ix_runs_project_loop_status")
        batch_op.drop_index("ix_runs_order")

    op.drop_table("runs")

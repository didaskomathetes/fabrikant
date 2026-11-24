"""Domain result of a step (verdict): column ``steps.verdict``.

A step can be technically successful and report a `fail` verdict (e.g. relay-audit);
the UI shows it as a badge next to the run status.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Native ADD COLUMN (nullable, without constraint) instead of batch mode: batch rebuilds `steps`,
    # and the DROP of the old table with active foreign keys triggered the CASCADE deletion of the
    # child tables (usage, review, verify).
    op.add_column("steps", sa.Column("verdict", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("steps", "verdict")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

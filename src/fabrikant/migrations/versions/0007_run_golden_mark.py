"""Golden mark: column ``runs.golden`` (ADR 0068, roadmap #130).

Marks a finished run as the baseline of a golden run; ``''`` = no golden. The
committed review baseline stays in the sidecar file ``projects/<project>.golden.json``
— the column only links which run is the baseline and enables
``fabrikant eval --golden <run-id>``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Native ADD COLUMN (nullable, no constraint) — no batch mode needed.
    op.add_column("runs", sa.Column("golden", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "golden")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

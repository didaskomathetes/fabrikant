"""Replay link: column ``runs.replay_of`` (ADR 0061).

Links a replay run to the run id of its original; ``''`` = not a replay.
Analogous to ``retry_of`` (ADR 0036), purely informational — affects neither claims
nor retry chains nor planning.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Native ADD COLUMN (nullable, no constraint) — no batch mode needed.
    op.add_column("runs", sa.Column("replay_of", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "replay_of")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

"""Replay-Link: Spalte ``runs.replay_of`` (ADR 0061).

Verlinkt einen Replay-Run mit der Run-Id seines Originals; ``''`` = kein Replay.
Analog zu ``retry_of`` (ADR 0036), rein informativ — beeinflusst weder Claims
noch Retry-Ketten noch die Planung.

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
    # Natives ADD COLUMN (nullable, ohne Constraint) — kein Batch-Modus nötig.
    op.add_column("runs", sa.Column("replay_of", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "replay_of")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

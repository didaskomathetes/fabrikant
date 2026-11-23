"""Golden-Mark: Spalte ``runs.golden`` (ADR 0068, Roadmap #130).

Markiert einen abgeschlossenen Run als Baseline eines Golden-Runs; ``''`` =
kein Golden. Der committed Review-Baseline-Text bleibt in der Sidecar-Datei
``projects/<project>.golden.json`` — die Spalte verlinkt nur, welcher Run die
Baseline ist, und ermöglicht ``fabrikant eval --golden <run-id>``.

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
    # Natives ADD COLUMN (nullable, ohne Constraint) — kein Batch-Modus nötig.
    op.add_column("runs", sa.Column("golden", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "golden")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

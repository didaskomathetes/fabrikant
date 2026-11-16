"""Fachliches Step-Ergebnis (Verdikt): Spalte ``steps.verdict``.

Ein Step kann technisch erfolgreich sein und ein `fail`-Verdikt melden (z. B. relay-audit);
die UI zeigt es als Badge neben dem Run-Status.

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
    # Natives ADD COLUMN (nullable, ohne Constraint) statt Batch-Modus: Batch baut `steps` neu auf,
    # und das DROP der alten Tabelle löste mit aktiven Fremdschlüsseln die CASCADE-Löschung der
    # Kind-Tabellen (Usage, Review, Verify) aus.
    op.add_column("steps", sa.Column("verdict", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("steps", "verdict")  # SQLite ≥ 3.35 / PostgreSQL: natives DROP COLUMN

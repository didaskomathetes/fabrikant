"""Cache-Aufschlüsselung und Abrechnungsart: ``step_usage.cache_read_tokens``/``cache_write_tokens``/``billing``.

Alle nullable (native ADD COLUMN); Alt-Zeilen bleiben NULL = „nicht gemeldet“.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("cache_read_tokens", sa.Integer()),
    ("cache_write_tokens", sa.Integer()),
    ("billing", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("step_usage", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("step_usage") as batch:
        for name, _type in reversed(_COLUMNS):
            batch.drop_column(name)

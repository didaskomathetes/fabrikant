"""Named connections (ADR 0052): table ``connections`` for LLM providers and agent backends.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("driver", sa.Text(), server_default="", nullable=False),
        sa.Column("endpoint", sa.Text(), server_default="", nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("username", sa.Text(), server_default="", nullable=False),
        sa.Column("secret", sa.Text(), server_default="", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.Text(), server_default="", nullable=False),
        sa.CheckConstraint("kind IN ('llm', 'agent')", name=op.f("ck_connections_kind_known")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_connections")),
    )


def downgrade() -> None:
    op.drop_table("connections")

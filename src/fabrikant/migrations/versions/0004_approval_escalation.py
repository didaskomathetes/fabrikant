"""Eskalation an einen Menschen (ADR 0059): ``approvals.kind`` und ``reason = 'escalation'``.

``kind`` ist ein natives ADD COLUMN (nullable). Der ``reason``-CHECK bekommt den neuen Wert; unter
SQLite heißt das Tabellen-Neubau (Batch). Das DROP der alten ``approvals`` löst die CASCADE-Löschung
von ``approval_permissions`` aus (Fremdschlüssel sind aktiv, das PRAGMA wirkt in der
Migrations-Transaktion nicht) — die Zeilen werden deshalb vorher gesichert und danach neu eingefügt.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OLD = ("step", "gate", "tier", "criticality")
_NEW = (*_OLD, "escalation")


def _reason_check(values: tuple[str, ...]) -> str:
    return "reason IS NULL OR reason IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_reason_check(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    perms = sa.table("approval_permissions", sa.column("run_id"), sa.column("seq"), sa.column("permission"))
    saved = [dict(r._mapping) for r in bind.execute(sa.select(perms))] if bind.dialect.name == "sqlite" else []
    with op.batch_alter_table("approvals") as batch:
        batch.drop_constraint("reason", type_="check")
        batch.create_check_constraint("reason", _reason_check(values))
    if saved:
        bind.execute(sa.delete(perms))  # falls kein Cascade lief: keine Doppel-Schlüssel
        bind.execute(sa.insert(perms), saved)


def upgrade() -> None:
    op.add_column("approvals", sa.Column("kind", sa.Text(), nullable=True))
    _replace_reason_check(_NEW)


def downgrade() -> None:
    op.execute("UPDATE approvals SET reason = 'gate' WHERE reason = 'escalation'")
    _replace_reason_check(_OLD)
    op.drop_column("approvals", "kind")

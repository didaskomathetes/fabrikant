"""Alembic-Umgebung des Run-Stores (ADR 0034).

Läuft programmatisch über :func:`fabrik.runstore_sql.migrate`: die Verbindung kommt aus
``config.attributes["connection"]`` (eine Transaktion, kein ``alembic.ini``). Ohne Verbindung
wird ``FABRIK_DB_URL`` bzw. die Default-URL genutzt (``alembic`` per CLI, z. B. für Revisionen).
"""

from alembic import context

from fabrik import config as fabrik_config
from fabrik.runschema import metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    from fabrik.runstore_sql import make_engine

    engine = make_engine(fabrik_config.db_url())
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()

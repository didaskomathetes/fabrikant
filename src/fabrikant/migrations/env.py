"""Alembic environment of the run store (ADR 0034).

Runs programmatically via :func:`fabrikant.runstore_sql.migrate`: the connection comes from
``config.attributes["connection"]`` (one transaction, no ``alembic.ini``). Without a connection
``FABRIKANT_DB_URL`` or the default URL is used (``alembic`` via CLI, e.g. for revisions).
"""

from alembic import context

from fabrikant import config as fabrikant_config
from fabrikant.runschema import metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    from fabrikant.runstore_sql import make_engine

    engine = make_engine(fabrikant_config.db_url())
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()

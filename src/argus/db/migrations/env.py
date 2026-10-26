"""Alembic environment. Migrations are plain SQL, so there is no model metadata to compare."""

from alembic import context
from sqlalchemy import Connection

from argus.db import engine_from_env


def run(connection: Connection) -> None:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


# Tests hand over their own connection; everything else uses ARGUS_DATABASE_URL.
given: Connection | None = context.config.attributes.get("connection")
if given is not None:
    run(given)
else:
    with engine_from_env().connect() as connection:
        run(connection)
        connection.commit()

"""Database access: the engine and the Alembic migrations (ADR 0001, ADR 0005)."""

import os

from sqlalchemy import Connection, Engine, create_engine

DATABASE_URL_ENV = "ARGUS_DATABASE_URL"


def engine_from_env(url: str | None = None) -> Engine:
    """Build the engine from ``ARGUS_DATABASE_URL``, for example
    ``postgresql+psycopg://argus_app@localhost/argus``. Credentials come from the environment,
    never from the repo."""
    url = url or os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not set")
    return create_engine(url)


def run_script(connection: Connection, sql: str) -> None:
    """Run a multi statement SQL script as it is, without parameter parsing, so ``%`` in
    PL/pgSQL ``raise`` messages stays literal. Used by the migrations."""
    connection.connection.driver_connection.execute(sql)  # type: ignore[union-attr]

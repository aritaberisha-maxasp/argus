"""A real PostgreSQL for the case store tests.

Set ``ARGUS_TEST_DATABASE_URL`` to an empty database the tests may wipe. Its name must end in
``_test``, for example
``postgresql+psycopg://postgres@localhost/argus_test``. The README shows how to start one.
Locally the tests are skipped without it; in CI (``CI`` is set) a missing database fails the run,
so these tests can never be skipped there.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

URL_ENV = "ARGUS_TEST_DATABASE_URL"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def migrate(engine: Engine, revision: str, *, down: bool = False) -> None:
    config = Config(str(ALEMBIC_INI))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if down:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(URL_ENV)
    if not url:
        if os.environ.get("CI"):
            pytest.fail(f"{URL_ENV} must be set in CI, the case store tests need PostgreSQL")
        pytest.skip(f"{URL_ENV} is not set, see README.md to run the database tests")
    # The tests wipe the database, so refuse anything that is not clearly a test database.
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail(f"{URL_ENV} must name a database ending in _test, the tests wipe it")
    return url


@pytest.fixture(scope="session")
def owner(database_url: str) -> Iterator[Engine]:
    """Connects as the owner that runs the migrations. Starts from an empty schema, runs every
    migration up, all the way down and up again, so both directions are tested."""
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("drop schema public cascade"))
        connection.execute(text("create schema public"))
    migrate(engine, "head")
    migrate(engine, "base", down=True)
    migrate(engine, "head")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app(owner: Engine, database_url: str) -> Iterator[Engine]:
    """Connects with the rights of the application's database user, argus_app."""
    engine = create_engine(database_url, connect_args={"options": "-c role=argus_app"})
    yield engine
    engine.dispose()

"""A real PostgreSQL for the case store tests, and Azurite for the document store tests.

Set ``ARGUS_TEST_DATABASE_URL`` to an empty database the tests may wipe. Its name must end in
``_test``, for example
``postgresql+psycopg://postgres@localhost/argus_test``. The README shows how to start one.
Locally the tests are skipped without it; in CI (``CI`` is set) a missing database fails the run,
so these tests can never be skipped there.

The mailbox tests share the fake Graph fixtures and the ``clean`` fixture at the end.

The document store tests need Azurite, the local Azure Blob Storage emulator. Set
``ARGUS_TEST_BLOB_CONNECTION_STRING`` to ``UseDevelopmentStorage=true``; the README shows how to
start it. The same rule applies: skipped locally without it, a failure in CI.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, make_url, text

from argus.documents.store import AzureDocumentStore
from argus.intake.graph import GraphClient
from standins.graph.app import create_app
from tests.unit.intake.graph_helpers import TOKEN, client_for

URL_ENV = "ARGUS_TEST_DATABASE_URL"
BLOB_ENV = "ARGUS_TEST_BLOB_CONNECTION_STRING"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def head() -> str:
    """The newest migration, so tests need no change when a story adds one."""
    revision = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    assert revision is not None
    return revision


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


@pytest.fixture(scope="session")
def blob_connection_string() -> str:
    value = os.environ.get(BLOB_ENV)
    if not value:
        if os.environ.get("CI"):
            pytest.fail(f"{BLOB_ENV} must be set in CI, the document store tests need Azurite")
        pytest.skip(f"{BLOB_ENV} is not set, see README.md to run the document store tests")
    return value


@pytest.fixture(scope="session")
def documents(blob_connection_string: str) -> AzureDocumentStore:
    """A fresh container in Azurite for this test run, so runs never see each other's files."""
    store = AzureDocumentStore.from_connection_string(
        blob_connection_string, f"documents-test-{uuid.uuid4().hex[:12]}"
    )
    store.create_container_if_missing()
    return store


@pytest.fixture
def clean(owner: Engine) -> None:
    """Each test starts with no email records. Records cannot be deleted, so the owner turns
    the guard off for this test cleanup only. The audit log stays (it is append only); tests
    only look at events of their own records."""
    with owner.begin() as connection:
        # Stored documents point at the records and are append only. The replica role lets
        # this test cleanup leave them behind; record IDs are never reused (#90).
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(
            text("alter table mailbox_email disable trigger mailbox_email_no_delete")
        )
        connection.execute(text("delete from mailbox_email"))
        connection.execute(text("alter table mailbox_email enable trigger mailbox_email_no_delete"))


@pytest.fixture
def fake_graph() -> FastAPI:
    return create_app(token=TOKEN)


@pytest.fixture
def graph(fake_graph: FastAPI) -> GraphClient:
    return client_for(fake_graph)

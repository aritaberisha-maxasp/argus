"""The demo reset empties the demo database and the demo document store, nothing else (#124).

Runs against a real PostgreSQL: a database named after the test database with ``_demo`` added,
created for this module and dropped afterwards. And against Azurite for the documents.

Covers: NF-01, NF-04
"""

import uuid
from collections.abc import Iterator

import pytest
from azure.storage.blob import BlobServiceClient
from psycopg import errors as pg
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError

from argus.approvers.table import list_vendor_approvers
from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.demo import DemoRefused, load_demo_approvers, reset_database, reset_documents
from argus.demo.reset import DEMO_APPROVERS
from argus.documents.store import AzureDocumentStore
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from tests.integration.conftest import head

INTAKE = Actor.system("intake")


@pytest.fixture(scope="module")
def demo_database(owner: Engine) -> Iterator[Engine]:
    url = owner.url.set(database=f"{owner.url.database}_demo")
    maintenance = create_engine(owner.url, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f'drop database if exists "{url.database}" with (force)'))
        connection.execute(text(f'create database "{url.database}"'))
    engine = create_engine(url)
    reset_database(engine)
    yield engine
    engine.dispose()
    with maintenance.connect() as connection:
        connection.execute(text(f'drop database if exists "{url.database}" with (force)'))
    maintenance.dispose()


def emails(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("select count(*) from mailbox_email")).scalar_one())


def audit_events(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("select count(*) from audit_event")).scalar_one())


@pytest.mark.req("NF-04")
def test_reset_empties_the_demo_database_and_runs_the_migrations_again(
    demo_database: Engine,
) -> None:
    with unit_of_work(demo_database, INTAKE) as db:
        see_email(db, InboxMessage(f"demo-{uuid.uuid4().hex}", None, None))
    assert emails(demo_database) == 1
    assert audit_events(demo_database) >= 1

    assert reset_database(demo_database) == head()

    assert emails(demo_database) == 0
    assert audit_events(demo_database) == 0


@pytest.mark.req("NF-01")
def test_after_a_reset_the_guards_on_every_row_are_back(demo_database: Engine) -> None:
    reset_database(demo_database)
    with unit_of_work(demo_database, INTAKE) as db:
        see_email(db, InboxMessage(f"demo-{uuid.uuid4().hex}", None, None))
    with pytest.raises(DBAPIError) as refused, demo_database.begin() as connection:
        connection.execute(text("delete from mailbox_email"))
    assert isinstance(refused.value.orig, pg.RaiseException)


@pytest.mark.req("NF-01", "NF-04")
def test_reset_refuses_the_real_database_and_leaves_it_as_it_is(owner: Engine) -> None:
    assert not (make_url(owner.url).database or "").endswith("_demo")
    before = audit_events(owner)
    with pytest.raises(DemoRefused, match="not to a database ending in _demo"):
        reset_database(owner)
    assert audit_events(owner) == before


@pytest.mark.req("VM-02")
def test_the_demo_approver_table_is_filled_after_a_reset(demo_database: Engine) -> None:
    """So demo invoices carry on to approval; 70008 stays without one for E13 (#35)."""
    reset_database(demo_database)

    assert load_demo_approvers(demo_database, DEMO_APPROVERS) == 7

    with demo_database.connect() as connection:
        accounts = [v.vendor_account for v in list_vendor_approvers(connection)]
    assert accounts == [str(n) for n in range(70001, 70008)]


@pytest.mark.req("NF-04")
def test_demo_approvers_are_never_loaded_into_the_real_database(owner: Engine) -> None:
    with pytest.raises(DemoRefused, match="not to a database ending in _demo"):
        load_demo_approvers(owner, DEMO_APPROVERS)


@pytest.mark.req("NF-04")
def test_reset_deletes_the_files_in_the_demo_document_store(blob_connection_string: str) -> None:
    name = f"documents-demo-{uuid.uuid4().hex[:12]}"
    store = AzureDocumentStore.from_connection_string(blob_connection_string, name)
    store.create_container_if_missing()
    store.put(b"synthetic demo email one", "message/rfc822")
    store.put(b"synthetic demo email two", "message/rfc822")
    container = BlobServiceClient.from_connection_string(
        blob_connection_string
    ).get_container_client(name)

    assert reset_documents(container) == 2
    assert list(container.list_blobs()) == []
    assert reset_documents(container) == 0
    container.delete_container()

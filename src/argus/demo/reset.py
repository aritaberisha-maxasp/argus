"""Empty the demo data so the demo can start over (#124).

Each part checks for itself that it only touches the demo:

- :func:`reset_database` drops and rebuilds the ``public`` schema of a database whose name
  ends in ``_demo``, then runs the migrations again. Dropping a table does not run its
  ``no delete`` and ``no truncate`` triggers, which still guard every row of the real database.
- :func:`reset_documents` deletes the files in a demo document store, an account whose name
  contains ``demo`` (or the local Azurite emulator). The real store is locked for 10 years and
  refuses deletes anyway (ADR 0001).
- :func:`reset_standins` asks the three stand ins to forget what ARGUS sent them. In Azure the
  Demo workflow restarts them instead, since they cannot be reached from outside.
"""

from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config as AlembicConfig
from azure.storage.blob import ContainerClient
from sqlalchemy import Engine, make_url, text

import argus.db
from argus.connections import Connections, is_standin_address
from argus.demo.common import DemoRefused, control_root

DEMO_SUFFIX = "_demo"
# The account name of the local Azurite emulator (UseDevelopmentStorage=true).
EMULATOR_ACCOUNT = "devstoreaccount1"
MIGRATIONS = Path(argus.db.__file__).resolve().parent / "migrations"


def database_name(url: str) -> str:
    return make_url(url).database or ""


def check_demo_database(url: str) -> None:
    if not database_name(url).endswith(DEMO_SUFFIX):
        raise DemoRefused(f"the database name must end in {DEMO_SUFFIX}, so the real one is safe")


def reset_database(engine: Engine) -> str:
    """Drop everything in the demo database and run the migrations again. Returns the
    migration the database is on afterwards."""
    with engine.begin() as connection:
        name: str = connection.execute(text("select current_database()")).scalar_one()
        if not name.endswith(DEMO_SUFFIX):
            raise DemoRefused(f"connected to {name}, not to a database ending in {DEMO_SUFFIX}")
        # A sweep in the middle of a transaction holds locks; wait for it, but not forever.
        connection.execute(text("set local lock_timeout = '30s'"))
        connection.execute(text("drop schema public cascade"))
        connection.execute(text("create schema public"))
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
        revision = connection.execute(text("select version_num from alembic_version")).scalar()
    return str(revision)


def documents_account(connection_string: str) -> str:
    if connection_string.strip() == "UseDevelopmentStorage=true":
        return EMULATOR_ACCOUNT
    parts = dict(part.split("=", 1) for part in connection_string.split(";") if "=" in part)
    return parts.get("AccountName", "")


def check_demo_documents(connection_string: str) -> None:
    account = documents_account(connection_string)
    if "demo" not in account.lower() and account != EMULATOR_ACCOUNT:
        raise DemoRefused("the document store is not the demo one, its name must contain demo")


def reset_documents(container: ContainerClient) -> int:
    """Delete every file in the demo document store. Returns how many were deleted."""
    account = container.account_name or ""
    if "demo" not in account.lower() and account != EMULATOR_ACCOUNT:
        raise DemoRefused("the document store is not the demo one, its name must contain demo")
    if not container.exists():
        return 0
    deleted = 0
    for blob in container.list_blobs():
        container.delete_blob(blob.name)
        deleted += 1
    return deleted


def reset_standins(connections: Connections, http: httpx.Client) -> list[str]:
    """Ask each stand in ARGUS is pointed at to start over. Returns the ones that did."""
    if not connections.demo:
        raise DemoRefused("demo mode is off, so there are no stand ins to reset")
    targets = [
        ("fake Graph", connections.graph_url, "/_standin/reset"),
        ("fake DATEV", connections.datev_url, "/_fake/reset"),
        ("Wise stand in", connections.wise_url, "/_standin/reset"),
    ]
    done = []
    for label, url, path in targets:
        if not url:
            continue
        if not is_standin_address(url):
            raise DemoRefused(f"the {label} address is not a stand in address")
        http.post(f"{control_root(url)}{path}").raise_for_status()
        done.append(label)
    return done

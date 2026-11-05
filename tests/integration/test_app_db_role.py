"""The app's own database role, as the deploy pipeline sets it up in Azure (#81).

Azure maps an Entra identity to a database role with ``pgaadauth_create_principal_with_oid``.
The test database has no such function, so the stand in from ``standins/postgres`` creates a
plain login role. Everything
after that is the real thing: ``tools/deploy.py`` grants the role ``argus_app``, the app's engine
switches to ``argus_app`` on connect, and ``/health/database`` reports the role and migration.
"""

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, make_url, text
from sqlalchemy.exc import ProgrammingError

from argus.app import create_app
from argus.db import engine_from_env
from tests.integration.conftest import head

REPO = Path(__file__).resolve().parents[2]
OBJECT_ID = "55555555-6666-7777-8888-999999999999"

# Stands in for the function Azure Database for PostgreSQL ships. Same signature.
STAND_IN = (REPO / "standins" / "postgres" / "pgaadauth.sql").read_text(encoding="utf-8")


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_tool", REPO / "tools" / "deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_tool()


@pytest.fixture
def identity(owner: Engine) -> Iterator[str]:
    name = f"id-argus-app-{uuid.uuid4().hex[:8]}"
    with owner.begin() as connection:
        connection.execute(text(STAND_IN))
    yield name
    with owner.begin() as connection:
        connection.execute(text("drop function pgaadauth_create_principal_with_oid"))
        connection.execute(text(f'drop owned by "{name}"'))
        connection.execute(text(f'drop role "{name}"'))


def app_engine(database_url: str, identity: str) -> Engine:
    url = make_url(database_url).set(username=identity, password=None)
    return engine_from_env(
        url.render_as_string(hide_password=False),
        environ={"ARGUS_DATABASE_ROLE": "argus_app"},
    )


@pytest.mark.req("NF-04", "NF-06")
def test_the_app_identity_works_as_argus_app(
    owner: Engine, database_url: str, identity: str
) -> None:
    assert deploy.ensure_app_role(owner, owner, identity, OBJECT_ID) is True
    # Every deploy runs it again; the second run keeps the role.
    assert deploy.ensure_app_role(owner, owner, identity, OBJECT_ID) is False

    engine = app_engine(database_url, identity)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("select current_user")).scalar_one() == "argus_app"
            assert connection.execute(text("select session_user")).scalar_one() == identity
            connection.execute(text("select count(*) from audit_event")).scalar_one()
        # The pool hands back the same connection: the role switch must survive the rollback.
        with engine.connect() as connection:
            assert connection.execute(text("select current_user")).scalar_one() == "argus_app"
        with engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text("delete from audit_event"))
    finally:
        engine.dispose()


@pytest.mark.req("NF-06")
def test_the_health_check_reports_role_and_migration(
    owner: Engine, database_url: str, identity: str
) -> None:
    deploy.ensure_app_role(owner, owner, identity, OBJECT_ID)
    engine = app_engine(database_url, identity)
    try:
        response = TestClient(create_app(lambda: engine)).get("/health/database")
    finally:
        engine.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "role": "argus_app",
        "revision": head(),
        "head": head(),
    }


def test_the_health_check_says_behind_when_a_migration_is_missing(
    owner: Engine, database_url: str, identity: str
) -> None:
    deploy.ensure_app_role(owner, owner, identity, OBJECT_ID)
    engine = app_engine(database_url, identity)
    with owner.begin() as connection:
        connection.execute(text("update alembic_version set version_num = 'older'"))
    try:
        response = TestClient(create_app(lambda: engine)).get("/health/database")
    finally:
        engine.dispose()
        with owner.begin() as connection:
            connection.execute(text("update alembic_version set version_num = :v"), {"v": head()})

    assert response.status_code == 503
    assert response.json()["status"] == "behind"
    assert response.json()["revision"] == "older"

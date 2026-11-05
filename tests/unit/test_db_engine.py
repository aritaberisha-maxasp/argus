"""How the app signs in to PostgreSQL in Azure (#81, NF-06).

No database is needed: a listener after ours records what would be sent to the server and
stops the connection there.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Engine, event

from argus.db import ENTRA_TOKEN_SCOPE, engine_from_env

FIRST, SECOND = (f"token-{n}" for n in (1, 2))

URL = "postgresql+psycopg://id-argus-app@db.example.invalid:5432/argus?sslmode=require"


class Stop(Exception):
    pass


class FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str) -> FakeToken:
        self.scopes.append(scopes)
        return FakeToken(f"token-{len(self.scopes)}")


def recorder(engine: Engine) -> Callable[[], dict[str, Any]]:
    """Returns a function that opens one connection and gives back what would be sent."""
    seen: list[dict[str, Any]] = []

    @event.listens_for(engine, "do_connect")
    def record(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
        seen.append(dict(cparams))
        raise Stop

    def connect() -> dict[str, Any]:
        with pytest.raises(Stop):
            engine.connect()
        return seen[-1]

    return connect


def connect_params(engine: Engine) -> dict[str, Any]:
    return recorder(engine)()


@pytest.mark.req("NF-06")
def test_entra_sign_in_sends_a_fresh_token_as_password_on_every_connection() -> None:
    credential = FakeCredential()
    engine = engine_from_env(URL, environ={"ARGUS_DATABASE_AUTH": "entra"}, credential=credential)

    connect = recorder(engine)
    first = connect()
    second = connect()

    assert first["password"] == FIRST
    assert second["password"] == SECOND
    assert credential.scopes == [(ENTRA_TOKEN_SCOPE,), (ENTRA_TOKEN_SCOPE,)]


def test_without_entra_no_password_is_added() -> None:
    engine = engine_from_env(URL, environ={})

    assert "password" not in connect_params(engine)


def test_an_unknown_sign_in_method_is_refused() -> None:
    with pytest.raises(RuntimeError, match="ARGUS_DATABASE_AUTH"):
        engine_from_env(URL, environ={"ARGUS_DATABASE_AUTH": "password"})


def test_the_url_comes_from_the_environment_mapping() -> None:
    engine = engine_from_env(environ={"ARGUS_DATABASE_URL": URL})

    assert engine.url.username == "id-argus-app"


def test_a_missing_url_is_refused() -> None:
    with pytest.raises(RuntimeError, match="ARGUS_DATABASE_URL"):
        engine_from_env(environ={})


def test_the_managed_identity_is_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.db

    made: list[dict[str, str]] = []

    def fake_managed_identity(environ: Any) -> FakeCredential:
        made.append(dict(environ))
        return FakeCredential()

    monkeypatch.setattr(argus.db, "managed_identity_credential", fake_managed_identity)
    environ = {"ARGUS_DATABASE_AUTH": "entra", "AZURE_CLIENT_ID": "client"}
    engine = engine_from_env(URL, environ=environ)

    assert connect_params(engine)["password"] == FIRST
    assert made == [environ]

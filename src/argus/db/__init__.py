"""Database access: the engine and the Alembic migrations (ADR 0001, ADR 0005)."""

import os
from collections.abc import Mapping
from typing import Any, Protocol

from psycopg import sql as pgsql
from sqlalchemy import Connection, Engine, create_engine, event

DATABASE_URL_ENV = "ARGUS_DATABASE_URL"
# "entra": sign in with an Entra token instead of a password (Azure, #81).
DATABASE_AUTH_ENV = "ARGUS_DATABASE_AUTH"
# The role every session switches to right after signing in, for example argus_app (#81).
DATABASE_ROLE_ENV = "ARGUS_DATABASE_ROLE"
# Azure Database for PostgreSQL accepts Entra tokens issued for this resource.
ENTRA_TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"  # noqa: S105


class AccessToken(Protocol):
    token: str


class TokenCredential(Protocol):
    """The part of an ``azure.identity`` credential the engine needs."""

    def get_token(self, *scopes: str) -> AccessToken: ...


def managed_identity_credential(environ: Mapping[str, str]) -> TokenCredential:
    """The app's managed identity. ``AZURE_CLIENT_ID`` picks the user assigned one."""
    from azure.identity import ManagedIdentityCredential

    return ManagedIdentityCredential(client_id=environ.get("AZURE_CLIENT_ID") or None)


def engine_from_env(
    url: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    credential: TokenCredential | None = None,
) -> Engine:
    """Build the engine from ``ARGUS_DATABASE_URL``, for example
    ``postgresql+psycopg://argus_app@localhost/argus``. Credentials come from the environment,
    never from the repo.

    In Azure there is no database password at all (#65, #81):
    - ``ARGUS_DATABASE_AUTH=entra`` fetches a fresh Entra token for every new connection and
      hands it over as the password. Tokens last about an hour, so a pooled connection keeps
      working and the next one gets a new token.
    - ``ARGUS_DATABASE_ROLE=argus_app`` switches every session to that role right after
      sign in. The managed identity signs in as its own Entra role, which is a member of
      ``argus_app``, and then works with exactly the rights of ``argus_app``.
    """
    environ = os.environ if environ is None else environ
    url = url or environ.get(DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not set")
    # Errors never quote the values of a statement: they can hold email data (NF-03).
    engine = create_engine(url, hide_parameters=True)

    auth = environ.get(DATABASE_AUTH_ENV, "").strip().lower()
    if auth == "entra":
        token_source = credential or managed_identity_credential(environ)

        @event.listens_for(engine, "do_connect")
        def entra_token(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
            cparams["password"] = token_source.get_token(ENTRA_TOKEN_SCOPE).token

    elif auth:
        raise RuntimeError(f"{DATABASE_AUTH_ENV} must be 'entra' or unset, not {auth!r}")

    role = environ.get(DATABASE_ROLE_ENV, "").strip()
    if role:

        @event.listens_for(engine, "connect")
        def switch_role(dbapi_connection: Any, connection_record: Any) -> None:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(pgsql.SQL("set role {}").format(pgsql.Identifier(role)))
            # The driver opened a transaction for the statement. Commit it, or the role
            # switch would be undone when the pool rolls the connection back.
            dbapi_connection.commit()

    return engine


def run_script(connection: Connection, sql: str) -> None:
    """Run a multi statement SQL script as it is, without parameter parsing, so ``%`` in
    PL/pgSQL ``raise`` messages stays literal. Used by the migrations."""
    connection.connection.driver_connection.execute(sql)  # type: ignore[union-attr]

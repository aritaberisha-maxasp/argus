"""The ARGUS web app (ADR 0001)."""

import logging
import os
from collections.abc import Callable
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import NoResultFound

from argus import __version__
from argus.auth import (
    CaseApprovers,
    CurrentUser,
    NoApproversYet,
    Role,
    TokenVerifier,
    install_sign_in,
    may_view_case,
    verifier_from_env,
)
from argus.cases.model import CaseKind, CaseStatus
from argus.cases.store import get_case
from argus.db import engine_from_env
from argus.documents.store import AzureDocumentStore, DocumentStore
from argus.queue_page.web import PAGE_PATHS, install_queue_page
from argus.secrets import EnvironmentSecretStore

log = logging.getLogger(__name__)

# The commit the image was built from. The deploy pipeline bakes it in (#81) and the smoke
# test checks it, so a smoke test can never pass on the previous version by mistake.
BUILD_ENV = "ARGUS_BUILD"
MIGRATIONS = Path(__file__).resolve().parent / "db" / "migrations"


class Health(BaseModel):
    status: str
    version: str
    build: str


class DatabaseHealth(BaseModel):
    status: str
    role: str | None = None
    revision: str | None = None
    head: str | None = None


class Me(BaseModel):
    object_id: str
    name: str
    roles: list[Role]


class CaseView(BaseModel):
    """What a signed in person may see of a case: its status at any time (NF-05)."""

    id: int
    kind: CaseKind
    status: CaseStatus
    document_number: str | None
    gross_cents: int | None
    currency: str | None


# Routes anyone may call. They carry no business data: the health checks, and the exception
# queue page with its script and style, which is the same fixed file for everyone and fetches
# its data with the person's token (#126). Every other route needs sign in, and a test walks
# all routes to make sure (#23).
PUBLIC_PATHS = frozenset({"/health", "/health/database"}) | PAGE_PATHS


def migrations_head() -> str | None:
    """The newest migration shipped with this version of ARGUS."""
    return ScriptDirectory(str(MIGRATIONS)).get_current_head()


def documents_from_env() -> DocumentStore:
    """The document store of #20. Its connection string comes from Key Vault in Azure."""
    return AzureDocumentStore.from_secrets(EnvironmentSecretStore())


def create_app(
    engine_factory: Callable[[], Engine] = engine_from_env,
    *,
    sign_in: Callable[[], TokenVerifier | None] = verifier_from_env,
    case_approvers: CaseApprovers | None = None,
    documents_factory: Callable[[], DocumentStore] = documents_from_env,
) -> FastAPI:
    # No generated API pages: they would be routes without sign in.
    app = FastAPI(
        title="ARGUS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    install_sign_in(app, sign_in())
    approvers = case_approvers or NoApproversYet()
    engines: list[Engine] = []
    stores: list[DocumentStore] = []

    def engine() -> Engine:
        if not engines:
            engines.append(engine_factory())
        return engines[0]

    def documents() -> DocumentStore:
        if not stores:
            stores.append(documents_factory())
        return stores[0]

    install_queue_page(app, engine, documents)

    @app.get("/health")
    def health() -> Health:
        return Health(status="ok", version=__version__, build=os.environ.get(BUILD_ENV, "dev"))

    @app.get("/health/database", response_model=DatabaseHealth)
    def database_health() -> JSONResponse:
        """Ready only when the app can sign in as itself and the database runs the migrations
        this version expects (#81). The deploy smoke test calls this."""
        head = migrations_head()
        try:
            with engine().connect() as connection:
                role: str = connection.execute(text("select current_user")).scalar_one()
                revision: str | None = connection.execute(
                    text("select version_num from alembic_version")
                ).scalar_one_or_none()
        except Exception:
            # The reason goes to the log only. The answer is public and names nothing inside.
            log.exception("database health check failed")
            body = DatabaseHealth(status="unreachable", head=head)
            return JSONResponse(body.model_dump(), status_code=503)
        status = "ok" if revision == head else "behind"
        body = DatabaseHealth(status=status, role=role, revision=revision, head=head)
        return JSONResponse(body.model_dump(), status_code=200 if status == "ok" else 503)

    @app.get("/me")
    def me(user: CurrentUser) -> Me:
        """Who is signed in and which ARGUS roles they have."""
        return Me(object_id=user.object_id, name=user.name, roles=sorted(user.roles))

    @app.get("/cases/{case_id}")
    def read_case(case_id: int, user: CurrentUser) -> CaseView:
        """One case, if the signed in person may see it. A case they may not see answers
        exactly like one that does not exist, so nobody learns which IDs are taken."""
        not_found = HTTPException(status.HTTP_404_NOT_FOUND, "no such case")
        with engine().connect() as connection:
            try:
                case = get_case(connection, case_id)
            except NoResultFound:
                raise not_found from None
            if not may_view_case(user, approvers.approvers_of(connection, case_id)):
                log.info("user %s may not see case %s", user.object_id, case_id)
                raise not_found
        return CaseView(
            id=case.id,
            kind=case.kind,
            status=case.status,
            document_number=case.document_number,
            gross_cents=case.gross_cents,
            currency=case.currency,
        )

    return app


app = create_app()

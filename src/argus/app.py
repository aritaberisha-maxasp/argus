"""The ARGUS web app (ADR 0001)."""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import NoResultFound

from argus import __version__
from argus.auth import (
    CaseApprovers,
    CurrentUser,
    NoApproversYet,
    Role,
    SignedInUser,
    TokenVerifier,
    install_sign_in,
    may_view_case,
    require_role,
    verifier_from_env,
)
from argus.cases.model import CaseKind, CaseStatus
from argus.cases.store import get_case
from argus.connections import connections_from_env
from argus.db import engine_from_env
from argus.documents.store import AzureDocumentStore, DocumentStore
from argus.intake.graph import GraphClient
from argus.intake.mode import IntakeMode, SwitchedBy, intake_mode, switch_intake_mode
from argus.intake.shadow import PersonFolders, ShadowReport, shadow_report
from argus.mailbox_page.web import install_mailbox_page
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


class ModeView(BaseModel):
    """Whether ARGUS only proposes routes (shadow) or acts on the mailbox (live) (#72)."""

    mode: IntakeMode


class ModeChange(BaseModel):
    mode: IntakeMode
    # Why, for example "shadow report of 21 Nov, no invoice to Other". Kept in the audit log.
    reason: str = Field(min_length=3, max_length=500, pattern=r"\S")


class ModeChanged(BaseModel):
    mode: IntakeMode
    changed: bool  # False when ARGUS already ran in that mode; nothing was logged then


# Only the admin (Arita) may switch to live mode, and back (#72, ADR 0008).
Admin = Annotated[SignedInUser, Depends(require_role(Role.ADMIN))]
# The shadow report shows which emails ARGUS got wrong, so only finance and admin see it.
FinanceOrAdmin = Annotated[SignedInUser, Depends(require_role(Role.FINANCE, Role.ADMIN))]


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
    demo_graph: Callable[[], GraphClient] | None = None,
) -> FastAPI:
    """``demo_graph`` builds the Graph client for the demo mailbox page; tests pass one wired
    to the fake Graph. By default it comes from the demo settings."""
    # Refuse to start when demo mode and the real Microsoft 365, DATEV or Wise settings mix
    # (#124, ADR 0009). The error names the settings, never their values.
    connections = connections_from_env()
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
    # The fake mailbox page exists in demo mode only; otherwise its routes answer 404 (#135).
    if connections.demo:
        install_mailbox_page(app, demo_graph or connections.graph_client)

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

    @app.get("/intake/mode")
    def read_mode(user: CurrentUser) -> ModeView:
        """Whether ARGUS runs in shadow or live mode."""
        with engine().connect() as connection:
            return ModeView(mode=intake_mode(connection))

    @app.put("/intake/mode")
    def change_mode(change: ModeChange, user: Admin) -> ModeChanged:
        """Switch between shadow and live mode. Only the admin may, and must give a reason. The
        database writes the switch into the audit log with their Entra object ID, their name
        and sign in address as they are now, and the reason (#72, ADR 0008)."""
        by = SwitchedBy(
            actor=user.actor,
            name=user.name,
            reason=change.reason.strip(),
            sign_in_address=user.sign_in_address,
        )
        changed = switch_intake_mode(engine(), by, change.mode)
        if changed:
            # Who and why are in the audit log; the app log only says that it happened.
            log.warning("ARGUS was switched to %s mode", change.mode)
        return ModeChanged(mode=change.mode, changed=changed)

    @app.get("/intake/shadow-report")
    def read_shadow_report(user: FinanceOrAdmin) -> ShadowReport:
        """What ARGUS proposed in shadow mode against what the person did, per category, and
        every invoice ARGUS would have sent to Other (#72). Warns when the folder list is
        missing or incomplete, so an empty list never reads as an all clear by mistake."""
        with engine().connect() as connection:
            return shadow_report(connection, PersonFolders.from_environment())

    return app


app = create_app()

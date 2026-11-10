"""The ARGUS web app (ADR 0001)."""

import logging
import os
from collections.abc import Callable
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, text

from argus import __version__
from argus.db import engine_from_env

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


def migrations_head() -> str | None:
    """The newest migration shipped with this version of ARGUS."""
    return ScriptDirectory(str(MIGRATIONS)).get_current_head()


def create_app(engine_factory: Callable[[], Engine] = engine_from_env) -> FastAPI:
    app = FastAPI(title="ARGUS", version=__version__)
    engines: list[Engine] = []

    def engine() -> Engine:
        if not engines:
            engines.append(engine_factory())
        return engines[0]

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

    return app


app = create_app()

"""The ARGUS web app (ADR 0001)."""

from fastapi import FastAPI
from pydantic import BaseModel

from argus import __version__


class Health(BaseModel):
    status: str
    version: str


def create_app() -> FastAPI:
    app = FastAPI(title="ARGUS", version=__version__)

    @app.get("/health")
    def health() -> Health:
        return Health(status="ok", version=__version__)

    return app


app = create_app()

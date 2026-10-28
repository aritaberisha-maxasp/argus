"""Shared setup for the Wise tests: a client wired to the in-process Wise stand in."""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.wise import WiseClient

PROFILE_ID = 16_000_001
TOKEN = "synthetic-wise-token-for-tests"  # noqa: S105  not a real token
BASE_URL = "https://api.wise.example"


def standin_transport(app: FastAPI) -> httpx.MockTransport:
    """An httpx transport that hands each request to the stand in app."""
    standin = TestClient(app)

    def forward(request: httpx.Request) -> httpx.Response:
        answer = standin.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            content=request.read(),
            headers=dict(request.headers),
        )
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    return httpx.MockTransport(forward)


def client_for(transport: httpx.BaseTransport) -> WiseClient:
    return WiseClient(SecretStr(TOKEN), PROFILE_ID, BASE_URL, transport=transport)

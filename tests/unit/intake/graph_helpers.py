"""Shared setup for the Graph tests: a client wired to the in-process fake Graph."""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.intake.graph import GraphClient
from standins.graph.app import MAILBOX, fake_token

BASE_URL = "https://graph.example/v1.0"
TOKEN = fake_token(["Mail.ReadWrite", "Mail.Send"])


class StaticTokens:
    """Hands out one fixed token and counts how often it was asked, and for what."""

    def __init__(self, token: str = TOKEN) -> None:
        self._token = token
        self.scopes: list[str] = []

    def token(self, scope: str) -> SecretStr:
        self.scopes.append(scope)
        return SecretStr(self._token)


def standin_transport(app: FastAPI) -> httpx.MockTransport:
    """An httpx transport that hands each request, with its full URL, to the fake Graph, so
    the paging links it writes point at the same host as the client."""
    standin = TestClient(app)

    def forward(request: httpx.Request) -> httpx.Response:
        answer = standin.request(
            request.method, str(request.url), content=request.read(), headers=request.headers
        )
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    return httpx.MockTransport(forward)


def client_for(
    app: FastAPI, tokens: StaticTokens | None = None, mailbox: str = MAILBOX
) -> GraphClient:
    return GraphClient(
        tokens or StaticTokens(),
        mailbox,
        BASE_URL,
        transport=standin_transport(app),
        sleep=lambda _: None,
    )

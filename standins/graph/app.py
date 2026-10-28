"""A fake Microsoft Graph for development and CI (ADR 0003).

It serves the accounts mailbox from the synthetic fixture emails in ``tests/fixtures/emails``
and answers the Graph calls ARGUS makes the way Graph does: listing the Inbox with paging,
reading an email as its raw .eml file, and forwarding an email through a saved draft that can
be sent only once (#27). Like Graph with RBAC for Applications, it only lets the
app into its one mailbox. IDs are immutable only when the request asks for them with
``Prefer: IdType="ImmutableId"``, so a client that forgets the header gets different IDs.

Everything is synthetic and kept in memory. Control calls under ``/_standin`` let tests add or
remove emails, make the next calls fail with throttling, and read what was asked.
"""

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "emails"
MAILBOX = "accounts@maxasp.example"
IMMUTABLE = 'IdType="ImmutableId"'


@dataclass
class Message:
    raw: bytes
    immutable_id: str
    mutable_id: str
    internet_message_id: str | None
    received: datetime


def _message(raw: bytes) -> Message:
    parsed = message_from_bytes(raw)
    internet_id = parsed.get("Message-ID")
    key = hashlib.sha256((internet_id or "").encode() + raw).digest()
    date = parsed.get("Date")
    return Message(
        raw=raw,
        # Shaped like Graph IDs: base64url. The mutable one would change on a move.
        immutable_id="AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0A"
        + base64.urlsafe_b64encode(key).decode(),
        mutable_id="AQMkADAwATM0MDAAMS1iNTcwLWI2NTEtMDACLTAwCgBGAAAD"
        + base64.urlsafe_b64encode(key[:16]).decode(),
        internet_message_id=internet_id,
        received=parsedate_to_datetime(date).astimezone(UTC) if date else datetime.now(UTC),
    )


@dataclass
class Draft:
    """A forward saved as a draft. Like Graph, it carries every attachment of the original."""

    id: str
    original_id: str
    to: list[str]
    comment: str
    attachments: list[tuple[str, bytes]]
    is_draft: bool = True


def attachments_of(raw: bytes) -> list[tuple[str, bytes]]:
    parsed = message_from_bytes(raw, policy=policy.default)
    found: list[tuple[str, bytes]] = []
    for part in parsed.walk():
        if part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True)
            found.append((part.get_filename() or "", data if isinstance(data, bytes) else b""))
    return found


def fake_token(roles: list[str] | None = None) -> str:
    """An unsigned token shaped like an Entra app token, for the fake Graph only. ``roles`` are
    the application permissions it claims; the real Graph client checks them."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    claims = {"aud": "https://graph.microsoft.com", "iss": "fake-graph", "roles": roles or []}
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.fake-signature"


def fixture_emails() -> list[bytes]:
    return [path.read_bytes() for path in sorted(FIXTURES.glob("*.eml"))]


@dataclass
class Mailbox:
    inbox: dict[str, Message] = field(default_factory=dict)
    by_mutable: dict[str, str] = field(default_factory=dict)
    raw_reads: dict[str, int] = field(default_factory=dict)
    prefer_headers: list[str | None] = field(default_factory=list)
    fail_next: list[tuple[int, int]] = field(default_factory=list)
    drafts: dict[str, Draft] = field(default_factory=dict)
    sent: list[Draft] = field(default_factory=list)

    def add(self, raw: bytes) -> Message:
        message = _message(raw)
        self.inbox[message.immutable_id] = message
        self.by_mutable[message.mutable_id] = message.immutable_id
        return message

    def remove(self, message_id: str) -> None:
        self.inbox.pop(message_id, None)

    def reset(self, emails: list[bytes]) -> None:
        self.inbox.clear()
        self.by_mutable.clear()
        self.raw_reads.clear()
        self.prefer_headers.clear()
        self.fail_next.clear()
        self.drafts.clear()
        self.sent.clear()
        for raw in emails:
            self.add(raw)

    def find(self, message_id: str) -> Message | None:
        return self.inbox.get(message_id) or self.inbox.get(self.by_mutable.get(message_id, ""))


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


def create_app(
    token: str | None = None, mailbox: str = MAILBOX, emails: list[bytes] | None = None
) -> FastAPI:
    """The fake Graph. ``token`` is the bearer token it accepts (``GRAPH_STANDIN_TOKEN``)."""
    expected = token or os.environ.get("GRAPH_STANDIN_TOKEN", "")
    if not expected:
        raise RuntimeError("set GRAPH_STANDIN_TOKEN, the fake Graph accepts no calls without one")
    app = FastAPI(title="ARGUS fake Microsoft Graph")
    box = Mailbox()
    box.reset(fixture_emails() if emails is None else emails)
    app.state.mailbox = box

    def check(
        request: Request, user: str, authorization: str | None, prefer: str | None
    ) -> Response | None:
        box.prefer_headers.append(prefer)
        if authorization != f"Bearer {expected}":
            return _error(401, "InvalidAuthenticationToken", "Access token is empty or invalid.")
        if user.lower() != mailbox.lower():
            # What Exchange answers when the app is scoped to another mailbox.
            return _error(403, "ErrorAccessDenied", "Access is denied. Check credentials.")
        if box.fail_next:
            status, retry_after = box.fail_next.pop(0)
            return _error(
                status, "TooManyRequests", "Throttled.", {"Retry-After": str(retry_after)}
            )
        return None

    def wire_id(message: Message, prefer: str | None) -> str:
        return message.immutable_id if prefer == IMMUTABLE else message.mutable_id

    @app.get("/v1.0/users/{user}/mailFolders/inbox/messages")
    def list_inbox(
        request: Request,
        user: str,
        top: Annotated[int, Query(alias="$top", ge=1, le=1000)] = 10,
        skip: Annotated[int, Query(alias="$skip", ge=0)] = 0,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Any:
        refused = check(request, user, authorization, prefer)
        if refused:
            return refused
        ordered = sorted(box.inbox.values(), key=lambda m: (m.received, m.immutable_id))
        page = ordered[skip : skip + top]
        body: dict[str, Any] = {
            "value": [
                {
                    "id": wire_id(m, prefer),
                    "internetMessageId": m.internet_message_id,
                    "receivedDateTime": m.received.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                for m in page
            ]
        }
        if skip + top < len(ordered):
            base = str(request.base_url).rstrip("/")
            body["@odata.nextLink"] = (
                f"{base}/v1.0/users/{user}/mailFolders/inbox/messages"
                f"?%24top={top}&%24skip={skip + top}"
            )
        headers = {"Preference-Applied": IMMUTABLE} if prefer == IMMUTABLE else {}
        return JSONResponse(body, headers=headers)

    @app.get("/v1.0/users/{user}/messages/{message_id}/$value")
    def get_raw(
        request: Request,
        user: str,
        message_id: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        refused = check(request, user, authorization, prefer)
        if refused:
            return refused
        message = box.find(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", "The specified object was not found.")
        box.raw_reads[message.immutable_id] = box.raw_reads.get(message.immutable_id, 0) + 1
        return Response(message.raw, media_type="text/plain")

    @app.post("/v1.0/users/{user}/messages/{message_id}/createForward")
    async def create_forward(
        request: Request,
        user: str,
        message_id: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        refused = check(request, user, authorization, prefer)
        if refused:
            return refused
        message = box.find(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", "The specified object was not found.")
        body = await request.json()
        draft = Draft(
            id="AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0B" + uuid.uuid4().hex,
            original_id=message.immutable_id,
            to=[r["emailAddress"]["address"] for r in body.get("toRecipients", [])],
            comment=body.get("comment", ""),
            attachments=attachments_of(message.raw),
        )
        box.drafts[draft.id] = draft
        return JSONResponse({"id": draft.id, "isDraft": True}, status_code=201)

    @app.get("/v1.0/users/{user}/messages/{message_id}")
    def get_message(
        request: Request,
        user: str,
        message_id: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        refused = check(request, user, authorization, prefer)
        if refused:
            return refused
        draft = box.drafts.get(message_id)
        if draft is not None:
            return JSONResponse({"id": draft.id, "isDraft": draft.is_draft})
        message = box.find(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", "The specified object was not found.")
        return JSONResponse({"id": wire_id(message, prefer), "isDraft": False})

    @app.post("/v1.0/users/{user}/messages/{message_id}/send")
    def send(
        request: Request,
        user: str,
        message_id: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        refused = check(request, user, authorization, prefer)
        if refused:
            return refused
        draft = box.drafts.get(message_id)
        if draft is None:
            return _error(404, "ErrorItemNotFound", "The specified object was not found.")
        if not draft.is_draft:
            # Graph sends a draft once; after that the message is in Sent Items, not a draft.
            return _error(400, "ErrorInvalidRequest", "Only draft messages can be sent.")
        draft.is_draft = False
        box.sent.append(draft)
        return Response(status_code=202)

    @app.post("/_standin/messages")
    async def add_message(request: Request) -> dict[str, str]:
        return {"id": box.add(await request.body()).immutable_id}

    @app.delete("/_standin/messages/{message_id}")
    def remove_message(message_id: str) -> dict[str, str]:
        box.remove(message_id)
        return {"removed": message_id}

    @app.post("/_standin/throttle")
    def throttle(status: int = 429, retry_after: int = 1, times: int = 1) -> dict[str, int]:
        box.fail_next.extend([(status, retry_after)] * times)
        return {"queued": len(box.fail_next)}

    @app.post("/_standin/reset")
    def reset() -> dict[str, int]:
        box.reset(fixture_emails() if emails is None else emails)
        return {"inbox": len(box.inbox)}

    @app.get("/_standin/records")
    def records() -> dict[str, Any]:
        return {
            "inbox": len(box.inbox),
            "raw_reads": box.raw_reads,
            "drafts": len(box.drafts),
            "sent": [
                {
                    "original_id": d.original_id,
                    "to": d.to,
                    "attachments": [
                        {"filename": name, "sha256": hashlib.sha256(data).hexdigest()}
                        for name, data in d.attachments
                    ],
                }
                for d in box.sent
            ],
        }

    return app


def __getattr__(name: str) -> FastAPI:
    # ``uvicorn standins.graph.app:app`` builds the app on first use, from the environment.
    if name == "app":
        return create_app()
    raise AttributeError(name)

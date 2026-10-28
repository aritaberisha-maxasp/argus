"""A fake Microsoft Graph for development and CI (ADR 0003).

It serves the accounts mailbox from the synthetic fixture emails in ``tests/fixtures/emails``
and answers the Graph calls ARGUS makes the way Graph does: listing the Inbox with paging,
reading an email as its raw .eml file, finding and creating folders, moving an email into a
folder and deleting it (into Deleted Items, as Graph does). Like Graph with RBAC for
Applications, it only lets the app into its one mailbox. IDs are immutable only when the
request asks for them with ``Prefer: IdType="ImmutableId"``, so a client that forgets the header
gets different IDs.

Everything is synthetic and kept in memory. Control calls under ``/_standin`` let tests add or
remove emails, make the next calls fail with throttling, and read what was asked.
"""

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "emails"
MAILBOX = "accounts@maxasp.example"
IMMUTABLE = 'IdType="ImmutableId"'
INBOX_ID = "AAMkAGInbox0000"
_NAME_FILTER = re.compile(r"displayName eq '([^']*)'")


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
class Folder:
    id: str
    name: str
    parent_id: str | None  # None at the top of the mailbox


@dataclass
class Mailbox:
    messages: dict[str, Message] = field(default_factory=dict)
    location: dict[str, str] = field(default_factory=dict)  # message ID to folder ID
    folders: dict[str, Folder] = field(default_factory=dict)
    deleted: dict[str, Message] = field(default_factory=dict)  # Deleted Items
    by_mutable: dict[str, str] = field(default_factory=dict)
    raw_reads: dict[str, int] = field(default_factory=dict)
    moves: list[tuple[str, str]] = field(default_factory=list)
    prefer_headers: list[str | None] = field(default_factory=list)
    fail_next: list[tuple[int, int]] = field(default_factory=list)

    @property
    def inbox(self) -> dict[str, Message]:
        return {i: m for i, m in self.messages.items() if self.location[i] == INBOX_ID}

    def add(self, raw: bytes) -> Message:
        message = _message(raw)
        self.messages[message.immutable_id] = message
        self.location[message.immutable_id] = INBOX_ID
        self.by_mutable[message.mutable_id] = message.immutable_id
        return message

    def remove(self, message_id: str) -> None:
        self.messages.pop(message_id, None)
        self.location.pop(message_id, None)

    def reset(self, emails: list[bytes]) -> None:
        for each in (self.messages, self.location, self.folders, self.deleted, self.by_mutable):
            each.clear()
        self.raw_reads.clear()
        self.moves.clear()
        self.prefer_headers.clear()
        self.fail_next.clear()
        self.folders[INBOX_ID] = Folder(INBOX_ID, "Inbox", None)
        for raw in emails:
            self.add(raw)

    def find(self, message_id: str) -> Message | None:
        immutable = message_id if message_id in self.messages else self.by_mutable.get(message_id)
        return self.messages.get(immutable or "")

    def in_folder(self, path: str) -> set[str]:
        """The IDs of the emails in the folder with this path, such as ``ARGUS/Removed``."""
        parent: str | None = None
        for name in path.split("/"):
            parent = next(
                (f.id for f in self.folders.values() if f.parent_id == parent and f.name == name),
                None,
            )
            if parent is None:
                return set()
        return {i for i, folder in self.location.items() if folder == parent}


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
        message = box.find(message_id)
        if message is None:
            return _error(404, "ErrorItemNotFound", "The specified object was not found.")
        return JSONResponse(
            {
                "id": wire_id(message, prefer),
                "parentFolderId": box.location[message.immutable_id],
            }
        )

    def list_folders(parent_id: str | None, filter_: str | None) -> Response:
        if parent_id is not None and parent_id not in box.folders:
            return _error(404, "ErrorItemNotFound", "The specified folder was not found.")
        match = _NAME_FILTER.fullmatch(filter_ or "")
        if filter_ and not match:
            return _error(400, "ErrorInvalidUrlQueryFilter", "Only displayName eq is faked.")
        found = [
            {"id": f.id, "displayName": f.name}
            for f in box.folders.values()
            if f.parent_id == parent_id and (match is None or f.name == match[1])
        ]
        return JSONResponse({"value": found})

    async def create_folder(parent_id: str | None, request: Request) -> Response:
        if parent_id is not None and parent_id not in box.folders:
            return _error(404, "ErrorItemNotFound", "The specified folder was not found.")
        name = (await request.json()).get("displayName", "")
        if any(f.parent_id == parent_id and f.name == name for f in box.folders.values()):
            return _error(409, "ErrorFolderExists", "A folder with the specified name exists.")
        folder = Folder(f"AAMkAGFolder{len(box.folders):04d}", name, parent_id)
        box.folders[folder.id] = folder
        return JSONResponse({"id": folder.id, "displayName": name}, status_code=201)

    @app.get("/v1.0/users/{user}/mailFolders")
    def top_folders(
        request: Request,
        user: str,
        filter_: Annotated[str | None, Query(alias="$filter")] = None,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        return check(request, user, authorization, prefer) or list_folders(None, filter_)

    @app.get("/v1.0/users/{user}/mailFolders/{folder_id}/childFolders")
    def child_folders(
        request: Request,
        user: str,
        folder_id: str,
        filter_: Annotated[str | None, Query(alias="$filter")] = None,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        return check(request, user, authorization, prefer) or list_folders(folder_id, filter_)

    @app.post("/v1.0/users/{user}/mailFolders")
    async def new_top_folder(
        request: Request,
        user: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        return check(request, user, authorization, prefer) or await create_folder(None, request)

    @app.post("/v1.0/users/{user}/mailFolders/{folder_id}/childFolders")
    async def new_child_folder(
        request: Request,
        user: str,
        folder_id: str,
        authorization: Annotated[str | None, Header()] = None,
        prefer: Annotated[str | None, Header()] = None,
    ) -> Response:
        refused = check(request, user, authorization, prefer)
        return refused or await create_folder(folder_id, request)

    @app.post("/v1.0/users/{user}/messages/{message_id}/move")
    async def move(
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
        destination = (await request.json()).get("destinationId", "")
        if destination not in box.folders:
            return _error(404, "ErrorItemNotFound", "The specified folder was not found.")
        box.location[message.immutable_id] = destination
        box.moves.append((message.immutable_id, destination))
        return JSONResponse(
            {"id": wire_id(message, prefer), "parentFolderId": destination}, status_code=201
        )

    @app.delete("/v1.0/users/{user}/messages/{message_id}")
    def delete(
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
        box.remove(message.immutable_id)
        box.deleted[message.immutable_id] = message
        return Response(status_code=204)

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
            "folders": {f.id: f.name for f in box.folders.values()},
            "deleted": len(box.deleted),
        }

    return app


def __getattr__(name: str) -> FastAPI:
    # ``uvicorn standins.graph.app:app`` builds the app on first use, from the environment.
    if name == "app":
        return create_app()
    raise AttributeError(name)

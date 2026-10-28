"""The Microsoft Graph client that reads the accounts mailbox (ADR 0003, IN-01, NF-06).

It signs in as the ARGUS app and needs only two application permissions, Mail.ReadWrite and
Mail.Send, limited by Exchange to the one mailbox. Mail.Send is used for one thing only:
forwarding payment reminders to the one address set for them (IN-05, #27). Every token is
checked before use: if it carries any other Graph permission, the client refuses to send
anything, so a too wide grant is noticed at once instead of going unused and unseen.

Every request asks for immutable message IDs, which stay the same when an email moves to
another folder, and passes the allowlist below inside the HTTP transport. Calls for another
mailbox or another host are refused before they leave ARGUS.
"""

import base64
import binascii
import json
import logging
import re
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

log = logging.getLogger("argus.intake.graph")

GRAPH_URL = "https://graph.microsoft.com/v1.0"
# The app asks Entra for a token with whatever was granted to it. What is granted is checked
# against PERMISSIONS on every token.
TOKEN_SCOPE = "https://graph.microsoft.com/.default"  # noqa: S105  a scope, not a secret
PERMISSIONS = frozenset({"Mail.ReadWrite", "Mail.Send"})
IMMUTABLE_IDS = 'IdType="ImmutableId"'
PAGE_SIZE = 50
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 120.0

# Graph message IDs are base64url like strings. Anything else in a path is refused.
_MESSAGE_ID = r"[A-Za-z0-9_\-=]+"


class GraphCallRefused(PermissionError):
    """ARGUS refused to send a Graph request: not on the allowlist, or the token is too wide."""


class GraphApiError(RuntimeError):
    """Graph answered with an error. Holds Graph's error code, never the email."""

    def __init__(self, method: str, path: str, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"Graph {method} {path} failed with {status}: {code or '-'}")


class TokenProvider(Protocol):
    """Gets an app token for Graph. In Azure the managed identity signs in through a federated
    credential (ADR 0001); no client secret exists."""

    def token(self, scope: str) -> SecretStr: ...


@dataclass(frozen=True)
class InboxMessage:
    """An email in the Inbox, as listed. The content is read separately, as the raw file."""

    id: str
    internet_message_id: str | None
    received_at: datetime | None


def token_permissions(token: SecretStr) -> frozenset[str]:
    """The application permissions (``roles``) an Entra access token carries.

    The token comes straight from Entra over TLS and Graph checks its signature, so it is read
    here without checking the signature again. A token that is not a JWT is refused.
    """
    parts = token.get_secret_value().split(".")
    if len(parts) != 3:
        raise GraphCallRefused("the Graph token is not a JWT, cannot check its permissions")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError) as error:
        raise GraphCallRefused("the Graph token cannot be read") from error
    roles = claims.get("roles", []) if isinstance(claims, dict) else None
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise GraphCallRefused("the Graph token has no readable roles claim")
    return frozenset(roles)


def check_token(token: SecretStr) -> None:
    """Refuse a token with any permission beyond Mail.ReadWrite and Mail.Send.

    With Exchange RBAC for Applications the token carries no mail roles at all, because Exchange
    grants them; that is fine. Extra roles mean someone granted too much in Entra.
    """
    extra = token_permissions(token) - PERMISSIONS
    if extra:
        raise GraphCallRefused(
            "the ARGUS app has Graph permissions it must not have: " + ", ".join(sorted(extra))
        )


def allowed_paths(
    base_path: str, mailbox: str, forward_to: str | None = None
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The only Graph calls ARGUS makes, all on the one mailbox. Later stories add moving and
    deleting (ADR 0003), each with a review.

    Forwarding (#27) is only allowed when a forward address is set: save a forward as a draft,
    check whether a message is still a draft, and send a draft."""
    user = re.escape(f"{base_path.rstrip('/')}/users/{quote(mailbox, safe='@')}")
    paths: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("GET", re.compile(f"{user}/mailFolders/inbox/messages")),
        ("GET", re.compile(f"{user}/messages/{_MESSAGE_ID}/\\$value")),
    )
    if forward_to:
        paths += (
            ("POST", re.compile(f"{user}/messages/{_MESSAGE_ID}/createForward")),
            ("GET", re.compile(f"{user}/messages/{_MESSAGE_ID}")),
            ("POST", re.compile(f"{user}/messages/{_MESSAGE_ID}/send")),
        )
    return paths


def forward_body_problem(content: bytes, forward_to: str) -> str | None:
    """Why a createForward body is refused, or None when it is fine. A forward goes to the one
    set address and nowhere else: no other recipient, no copy, no changed message."""
    try:
        body = json.loads(content)
    except ValueError:
        return "the forward body is not JSON"
    if not isinstance(body, dict) or set(body) - {"comment", "toRecipients"}:
        return "the forward may only carry a comment and its recipient"
    recipients = body.get("toRecipients")
    if not isinstance(recipients, list) or len(recipients) != 1:
        return "the forward must have exactly one recipient"
    recipient = recipients[0]
    address = recipient.get("emailAddress") if isinstance(recipient, dict) else None
    address = address.get("address") if isinstance(address, dict) else None
    if not isinstance(address, str) or not forward_to or address.lower() != forward_to.lower():
        return "the forward goes to an address that is not the one set for reminders"
    return None


class _AppToken(httpx.Auth):
    """Adds a fresh, checked token to each request."""

    def __init__(self, tokens: TokenProvider) -> None:
        self._tokens = tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._tokens.token(TOKEN_SCOPE)
        check_token(token)
        request.headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        yield request


class _GuardedTransport(httpx.BaseTransport):
    """Checks every request against the allowlist, the mailbox and the Graph host, and every
    forward against the one address set for reminders."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        base_url: httpx.URL,
        mailbox: str,
        forward_to: str | None = None,
    ) -> None:
        self._inner = inner
        self._origin = (base_url.scheme, base_url.host, base_url.port)
        self._allowed = allowed_paths(base_url.raw_path.decode("ascii"), mailbox, forward_to)
        self._forward_to = forward_to

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.raw_path.decode("ascii").partition("?")[0]
        allowed = (url.scheme, url.host, url.port) == self._origin and any(
            request.method == method and pattern.fullmatch(path)
            for method, pattern in self._allowed
        )
        if not allowed:
            log.warning("Refused Graph call %s %s%s", request.method, url.host, path)
            raise GraphCallRefused(f"refused Graph call {request.method} {url.host}{path}")
        if path.endswith("/createForward"):
            problem = forward_body_problem(request.read(), self._forward_to or "")
            if problem:
                log.warning("Refused Graph forward: %s", problem)
                raise GraphCallRefused(f"refused Graph forward: {problem}")
        request.headers["Prefer"] = IMMUTABLE_IDS
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphClient:
    """Reads one mailbox through Graph, or through the fake Graph stand in."""

    def __init__(
        self,
        tokens: TokenProvider,
        mailbox: str,
        base_url: str = GRAPH_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
        standin: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        forward_to: str | None = None,
    ) -> None:
        """``standin=True`` allows plain http, for the fake Graph only, never for Graph.

        ``forward_to`` is the one address payment reminders go to (IN-05). Without it the
        client cannot forward or send anything."""
        if urlsplit(base_url).scheme != "https" and not standin:
            raise ValueError("the Graph base URL must use https unless it is the stand in")
        self.mailbox = mailbox
        self.forward_to = forward_to
        url = httpx.URL(base_url.rstrip("/") + "/")
        self._user = f"users/{quote(mailbox, safe='@')}"
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=url,
            transport=_GuardedTransport(
                transport or httpx.HTTPTransport(), url, mailbox, forward_to
            ),
            timeout=timeout,
            follow_redirects=False,
            auth=_AppToken(tokens),
        )

    def __repr__(self) -> str:
        return f"GraphClient(base_url={str(self._http.base_url)!r}, mailbox={self.mailbox!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with retries on 429 and 503, honouring Retry-After (Graph throttling)."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._http.get(url, params=params)
            if response.status_code not in (429, 503) or attempt == MAX_ATTEMPTS:
                break
            try:
                wait = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                wait = 5.0
            self._sleep(min(max(wait, 0.0), MAX_RETRY_AFTER))
        _raise_for_error("GET", response)
        return response

    def _post(self, url: str, body: dict[str, Any] | None = None) -> httpx.Response:
        """POST once, without retries. A retried forward or send could act twice; the caller
        checks the draft instead and tries again on the next sweep (#27)."""
        response = self._http.post(url, json=body or {})
        _raise_for_error("POST", response)
        return response

    def list_inbox(self) -> Iterator[InboxMessage]:
        """Every email in the Inbox, oldest first, following Graph's paging."""
        url = f"{self._user}/mailFolders/inbox/messages"
        params: dict[str, str] | None = {
            "$select": "id,internetMessageId,receivedDateTime",
            "$orderby": "receivedDateTime asc",
            "$top": str(PAGE_SIZE),
        }
        while url:
            page = self._get(url, params).json()
            for item in page.get("value", []):
                yield InboxMessage(
                    id=item["id"],
                    internet_message_id=item.get("internetMessageId"),
                    received_at=_parse_time(item.get("receivedDateTime")),
                )
            # The next link is absolute and carries its own query; the guard checks its host.
            url, params = page.get("@odata.nextLink", ""), None

    def get_raw(self, message_id: str) -> bytes:
        """The email exactly as received, as an .eml file with every attachment in it."""
        _check_message_id(message_id)
        return self._get(f"{self._user}/messages/{message_id}/$value").content

    def create_forward(self, message_id: str, comment: str) -> str:
        """Save a forward of this email to the reminder address as a draft, with every original
        attachment, and return the draft's immutable ID. Nothing is sent yet."""
        _check_message_id(message_id)
        if not self.forward_to:
            raise GraphCallRefused("refused Graph forward: no forward address is set")
        body = {
            "comment": comment,
            "toRecipients": [{"emailAddress": {"address": self.forward_to}}],
        }
        draft = self._post(f"{self._user}/messages/{message_id}/createForward", body).json()
        draft_id = draft.get("id") if isinstance(draft, dict) else None
        if not isinstance(draft_id, str) or not re.fullmatch(_MESSAGE_ID, draft_id):
            raise GraphApiError("POST", "createForward", 200, "no draft ID in the answer")
        return draft_id

    def is_draft(self, message_id: str) -> bool:
        """Whether this message is still an unsent draft."""
        _check_message_id(message_id)
        answer = self._get(f"{self._user}/messages/{message_id}", {"$select": "isDraft"}).json()
        return bool(answer.get("isDraft")) if isinstance(answer, dict) else False

    def send_draft(self, message_id: str) -> None:
        """Send a saved draft. Graph sends a draft only once."""
        _check_message_id(message_id)
        self._post(f"{self._user}/messages/{message_id}/send")


def _check_message_id(message_id: str) -> None:
    if not re.fullmatch(_MESSAGE_ID, message_id):
        raise GraphCallRefused("refused Graph call: not a message ID")


def _raise_for_error(method: str, response: httpx.Response) -> None:
    if response.is_error:
        try:
            code = str(response.json()["error"]["code"])
        except (ValueError, KeyError, TypeError):
            code = ""
        raise GraphApiError(method, response.request.url.path, response.status_code, code)

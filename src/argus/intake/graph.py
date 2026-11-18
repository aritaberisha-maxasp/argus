"""The Microsoft Graph client that reads the accounts mailbox (ADR 0003, IN-01, NF-06).

It signs in as the ARGUS app and needs only two application permissions, Mail.ReadWrite and
Mail.Send, limited by Exchange to the one mailbox. Mail.Send is used for three things only:
forwarding payment reminders to the one address set for them (IN-05, #27), short notices about
Wise statements to that same address (BA-01, BA-08, #56), and sending the daily digest of open
exceptions to the one address set for it (EX-03, #61). Every token is
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
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

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
# How many folders, and emails per folder, the demo mailbox page reads at most (#135).
FOLDER_PAGE = 100
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 120.0

# Graph message IDs are base64url like strings. Anything else in a path is refused.
_MESSAGE_ID = r"[A-Za-z0-9_\-=]+"

# The forms of "the Inbox messages of one mailbox" that Graph writes into a paging link, after
# percent decoding and in lower case: users/<mailbox> or users('<mailbox>'), then
# mailFolders/inbox or mailFolders('inbox'). The mailbox part is compared with ours afterwards.
_INBOX_PAGE_PATH = re.compile(
    r"users(?:/(?P<plain>[^/()']+)|\('(?P<quoted>[^/()']+)'\))"
    r"/mailfolders(?:/inbox|\('inbox'\))/messages"
)
# The same for the emails of one folder by its ID, such as the statement folder (#166).
_FOLDER_PAGE_PATH = re.compile(
    r"users(?:/(?P<plain>[^/()']+)|\('(?P<quoted>[^/()']+)'\))"
    r"/mailfolders(?:/(?P<folder>[a-z0-9_\-=]+)|\('(?P<folder_quoted>[a-z0-9_\-=]+)'\))/messages"
)


class GraphCallRefused(PermissionError):
    """ARGUS refused to send a Graph request: not on the allowlist, or the token is too wide."""


class GraphPagingRefused(GraphCallRefused):
    """Graph gave a paging link that does not point at the Inbox of our mailbox. The sweep stops
    loudly instead of quietly reading only the first page (#90)."""


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
class MailFolder:
    """A folder of the mailbox, as listed."""

    id: str
    name: str


@dataclass(frozen=True)
class FolderMessage:
    """An email in a folder, as the demo mailbox page shows it (#135): who sent it, about what
    and when. Only ever read from the fake Graph."""

    subject: str
    sender_name: str
    sender_address: str
    received_at: datetime | None


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


class GraphCall(StrEnum):
    """One kind of Graph call. Each job builds its client with only the kinds it needs, so a
    bug in one job cannot touch mail it has no business with (#168, NF-03). The lists live
    next to the jobs in :mod:`argus.intake.jobs`."""

    LIST_INBOX = "list the Inbox"
    READ_EMAIL = "read an email as received"
    # Which folder an email is in, whether it is still a draft and who it goes to.
    READ_MESSAGE = "read the details of an email"
    FIND_FOLDERS = "find folders"
    CREATE_FOLDER = "create a folder"
    READ_INBOX_ID = "read the ID of the Inbox"
    READ_DELETED_ITEMS_ID = "read the ID of Deleted Items"
    MOVE = "move an email"
    DELETE = "delete an email"
    # The one statement folder the client was built with (#166).
    LIST_STATEMENT_FOLDER = "list the statement folder"
    # Any folder, against the fake Graph only, for the demo mailbox page (#135). The Inbox and
    # the statement folder are folders too.
    LIST_ANY_FOLDER = "list any folder in the demo"
    FORWARD = "save a forward of a payment reminder"
    SEND_NOTICE = "send a notice about a Wise statement"
    CREATE_DIGEST = "save the daily digest"
    SEND_DRAFT = "send a saved draft"


# Every call ARGUS makes. A client built without its own list gets this one; every job passes
# its own (#168).
ALL_CALLS: frozenset[GraphCall] = frozenset(GraphCall)


def allowed_paths(
    base_path: str,
    mailbox: str,
    forward_to: str | None = None,
    digest_to: str | None = None,
    standin: bool = False,
    calls: frozenset[GraphCall] = ALL_CALLS,
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The only Graph calls ARGUS makes, all on the one mailbox: list the Inbox, read an email,
    find or create the ARGUS folders, move an email into one and delete an email (#26), and
    read the ID of the Inbox, so the shadow run can tell an email a person moved (#72).

    ``calls`` narrows this to what one job needs (#168). A call that is not in it is not on
    the list, whatever else the client was built with.

    Listing the emails of a folder is not on this list. The transport allows it for one folder
    only, the statement folder the client was built with, once the client has found its ID
    (#166, :meth:`GraphClient.list_statement_folder`).

    Forwarding a payment reminder (#27) is only allowed when a forward address is set: save a
    forward as a draft and send a draft. Reading a message, which shows whether it is still a
    draft and who it goes to, is already allowed for #26.

    A notice about a Wise statement (#56) goes to the same forward address with sendMail.

    Sending the daily digest of open exceptions (#61) is only allowed when a digest address is
    set: save a new message as a draft and send a draft.

    Listing the emails of any folder is allowed against the fake Graph only, for the demo
    mailbox page (#135). Against the real Graph that call stays refused."""
    user = re.escape(f"{base_path.rstrip('/')}/users/{quote(mailbox, safe='@')}")
    rules: tuple[tuple[GraphCall, str, str, bool], ...] = (
        (GraphCall.LIST_INBOX, "GET", f"{user}/mailFolders/inbox/messages", True),
        (GraphCall.READ_EMAIL, "GET", f"{user}/messages/{_MESSAGE_ID}/\\$value", True),
        (GraphCall.READ_MESSAGE, "GET", f"{user}/messages/{_MESSAGE_ID}", True),
        (
            GraphCall.FIND_FOLDERS,
            "GET",
            f"{user}/mailFolders(/{_MESSAGE_ID}/childFolders)?",
            True,
        ),
        (GraphCall.READ_DELETED_ITEMS_ID, "GET", f"{user}/mailFolders/deleteditems", True),
        (GraphCall.READ_INBOX_ID, "GET", f"{user}/mailFolders/inbox", True),
        (
            GraphCall.CREATE_FOLDER,
            "POST",
            f"{user}/mailFolders(/{_MESSAGE_ID}/childFolders)?",
            True,
        ),
        (GraphCall.MOVE, "POST", f"{user}/messages/{_MESSAGE_ID}/move", True),
        (GraphCall.DELETE, "DELETE", f"{user}/messages/{_MESSAGE_ID}", True),
        (GraphCall.LIST_ANY_FOLDER, "GET", f"{user}/mailFolders/{_MESSAGE_ID}/messages", standin),
        (
            GraphCall.FORWARD,
            "POST",
            f"{user}/messages/{_MESSAGE_ID}/createForward",
            bool(forward_to),
        ),
        # A notice about a Wise statement (#56); checked by notice_body_problem.
        (GraphCall.SEND_NOTICE, "POST", f"{user}/sendMail", bool(forward_to)),
        (GraphCall.CREATE_DIGEST, "POST", f"{user}/messages", bool(digest_to)),
        (
            GraphCall.SEND_DRAFT,
            "POST",
            f"{user}/messages/{_MESSAGE_ID}/send",
            bool(forward_to or digest_to),
        ),
    )
    return tuple(
        (method, re.compile(pattern)) for call, method, pattern, ok in rules if ok and call in calls
    )


# What a statement notice may carry. No attachment, no copy, no reply address.
MAX_NOTICE_SUBJECT = 200
MAX_NOTICE_TEXT = 5000


def notice_body_problem(content: bytes, forward_to: str) -> str | None:
    """Why a sendMail body is refused, or None when it is fine. A notice (#56) is a plain text
    email with a subject, to the one address set for reminders, and nothing else."""
    try:
        body = _strict_json(content)
    except ValueError:
        return "the notice body is not JSON with each key once"
    if not isinstance(body, dict) or set(body) - {"message", "saveToSentItems"}:
        return "the notice may only carry a message"
    message = body.get("message")
    if not isinstance(message, dict) or set(message) != {"subject", "body", "toRecipients"}:
        return "the notice may only carry a subject, a text and its recipient"
    subject, text_body = message["subject"], message["body"]
    if not isinstance(subject, str) or not 0 < len(subject) <= MAX_NOTICE_SUBJECT:
        return "the notice subject is missing or too long"
    if (
        not isinstance(text_body, dict)
        or set(text_body) != {"contentType", "content"}
        or text_body["contentType"] != "Text"
        or not isinstance(text_body["content"], str)
        or not 0 < len(text_body["content"]) <= MAX_NOTICE_TEXT
    ):
        return "the notice must be plain text of a sensible length"
    recipients = message["toRecipients"]
    if not isinstance(recipients, list) or len(recipients) != 1:
        return "the notice must have exactly one recipient"
    recipient = recipients[0]
    if not isinstance(recipient, dict) or set(recipient) != {"emailAddress"}:
        return "the notice recipient is not an address"
    address = recipient["emailAddress"]
    if not isinstance(address, dict) or set(address) != {"address"}:
        return "the notice recipient is not an address"
    if (
        not isinstance(address["address"], str)
        or not forward_to
        or address["address"].lower() != forward_to.lower()
    ):
        return "the notice goes to an address that is not the one set for reminders"
    return None


class _RepeatedKey(ValueError):
    pass


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _RepeatedKey("a JSON key appears twice")
    return dict(pairs)


def _strict_json(content: bytes) -> Any:
    """JSON with every key once. Plain json.loads keeps the last of two equal keys, so a guard
    would check one value while Graph might act on the other (code review of PR 162)."""
    return json.loads(content, object_pairs_hook=_refuse_repeated_keys)


def forward_body_problem(content: bytes, forward_to: str) -> str | None:
    """Why a createForward body is refused, or None when it is fine. A forward goes to the one
    set address and nowhere else: no other recipient, no copy, no changed message."""
    try:
        body = _strict_json(content)
    except ValueError:
        return "the forward body is not JSON with each key once"
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


def digest_body_problem(content: bytes, digest_to: str) -> str | None:
    """Why the body of a new message is refused, or None when it is fine. The only new message
    ARGUS writes is the daily digest (#61): a subject, a body and the one set address, nothing
    else. No copy, no attachment, no other recipient."""
    try:
        body = json.loads(content)
    except ValueError:
        return "the digest body is not JSON"
    if not isinstance(body, dict) or set(body) != {"subject", "body", "toRecipients"}:
        return "the digest may only carry a subject, a body and its recipient"
    if not isinstance(body["subject"], str):
        return "the digest subject is not text"
    content_part = body["body"]
    if (
        not isinstance(content_part, dict)
        or set(content_part) != {"contentType", "content"}
        or content_part["contentType"] not in ("text", "html")
        or not isinstance(content_part["content"], str)
    ):
        return "the digest body must be plain text or HTML"
    to = _addresses(body["toRecipients"])
    if len(to) != 1 or len(body["toRecipients"]) != 1:
        return "the digest must have exactly one recipient"
    if not isinstance(to[0], str) or not digest_to or to[0].lower() != digest_to.lower():
        return "the digest goes to an address that is not the one set for it"
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
    """Checks every request against the allowlist, the mailbox and the Graph host, every
    forward against the one address set for reminders and every new message against the one
    address set for the digest."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        base_url: httpx.URL,
        mailbox: str,
        forward_to: str | None = None,
        digest_to: str | None = None,
        standin: bool = False,
        calls: frozenset[GraphCall] = ALL_CALLS,
    ) -> None:
        self._inner = inner
        self._origin = (base_url.scheme, base_url.host, base_url.port)
        base_path = base_url.raw_path.decode("ascii")
        self._allowed = allowed_paths(base_path, mailbox, forward_to, digest_to, standin, calls)
        self._list_statement_folder = GraphCall.LIST_STATEMENT_FOLDER in calls
        self._new_message = re.compile(
            re.escape(f"{base_path.rstrip('/')}/users/{quote(mailbox, safe='@')}/messages")
        )
        self._forward_to = forward_to
        self._digest_to = digest_to
        self._folder_messages = re.compile(
            re.escape(f"{base_path.rstrip('/')}/users/{quote(mailbox, safe='@')}/mailFolders/")
            + f"(?P<folder>{_MESSAGE_ID})/messages"
        )
        # The one folder whose emails may be listed: the statement folder, once found (#166).
        self.listable_folder: str | None = None

    def _listing_allowed(self, method: str, path: str) -> bool:
        found = self._folder_messages.fullmatch(path)
        return (
            method == "GET"
            and self._list_statement_folder
            and found is not None
            and self.listable_folder is not None
            and found["folder"] == self.listable_folder
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.raw_path.decode("ascii").partition("?")[0]
        allowed = (url.scheme, url.host, url.port) == self._origin and (
            any(
                request.method == method and pattern.fullmatch(path)
                for method, pattern in self._allowed
            )
            or self._listing_allowed(request.method, path)
        )
        if not allowed:
            log.warning("Refused Graph call %s %s%s", request.method, url.host, path)
            raise GraphCallRefused(f"refused Graph call {request.method} {url.host}{path}")
        if path.endswith("/createForward"):
            problem = forward_body_problem(request.read(), self._forward_to or "")
            if problem:
                log.warning("Refused Graph forward: %s", problem)
                raise GraphCallRefused(f"refused Graph forward: {problem}")
        if path.endswith("/sendMail"):
            problem = notice_body_problem(request.read(), self._forward_to or "")
            if problem:
                log.warning("Refused Graph notice: %s", problem)
                raise GraphCallRefused(f"refused Graph notice: {problem}")
        if request.method == "POST" and self._new_message.fullmatch(path):
            problem = digest_body_problem(request.read(), self._digest_to or "")
            if problem:
                log.warning("Refused Graph digest: %s", problem)
                raise GraphCallRefused(f"refused Graph digest: {problem}")
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
        digest_to: str | None = None,
        statement_folder: str | None = None,
        calls: frozenset[GraphCall] = ALL_CALLS,
    ) -> None:
        """``standin=True`` allows plain http, for the fake Graph only, never for Graph.

        ``statement_folder`` is the path of the one folder whose emails the client may list,
        such as ``ARGUS/Kontoauszüge`` (#166). Without it no folder can be listed.

        ``forward_to`` is the one address payment reminders go to (IN-05), ``digest_to`` the
        one address the daily digest of open exceptions goes to (EX-03). Without either the
        client cannot send anything.

        ``calls`` are the kinds of Graph call this client may make (#168). Every job passes its
        own list (:mod:`argus.intake.jobs`); anything else is refused before it leaves ARGUS."""
        if urlsplit(base_url).scheme != "https" and not standin:
            raise ValueError("the Graph base URL must use https unless it is the stand in")
        self.mailbox = mailbox
        self.forward_to = forward_to
        self.digest_to = digest_to
        url = httpx.URL(base_url.rstrip("/") + "/")
        self._user = f"users/{quote(mailbox, safe='@')}"
        self._deleted_items: str | None = None
        self._inbox: str | None = None
        self._sleep = sleep
        self.standin = standin
        self.statement_folder = statement_folder
        self.calls = frozenset(calls)
        self._transport = _GuardedTransport(
            transport or httpx.HTTPTransport(),
            url,
            mailbox,
            forward_to,
            digest_to,
            standin,
            self.calls,
        )
        self._http = httpx.Client(
            base_url=url,
            transport=self._transport,
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

    def _send(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send with retries on 429 and 503, honouring Retry-After (Graph throttling). Every
        call ARGUS makes is safe to repeat: a move to the folder the email is already in, or a
        second folder of the same name, changes nothing (see :meth:`create_folder`)."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._http.request(method, url, params=params, json=body)
            if response.status_code not in (429, 503) or attempt == MAX_ATTEMPTS:
                break
            try:
                wait = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                wait = 5.0
            self._sleep(min(max(wait, 0.0), MAX_RETRY_AFTER))
        return response

    def _post(self, url: str, body: dict[str, Any] | None = None) -> httpx.Response:
        """POST once, without retries. A retried forward or send could act twice; the caller
        checks the draft instead and tries again on the next sweep (#27)."""
        response = self._http.post(url, json=body or {})
        if response.is_error:
            raise self._error(response)
        return response

    @staticmethod
    def _error(response: httpx.Response) -> GraphApiError:
        try:
            code = str(response.json()["error"]["code"])
        except (ValueError, KeyError, TypeError):
            code = ""
        return GraphApiError(
            response.request.method, response.request.url.path, response.status_code, code
        )

    def _checked(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = self._send(method, url, params, body)
        if response.is_error:
            raise self._error(response)
        return response

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self._checked("GET", url, params)

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
            # The next link is absolute and carries its own query (the skip or skip token).
            link = page.get("@odata.nextLink")
            url, params = (self._next_page(link) if link else ""), None

    def list_statement_folder(self) -> Iterator[InboxMessage]:
        """Every email in the statement folder the client was built with, oldest first,
        following Graph's paging: IDs and received times only, like :meth:`list_inbox` (#166).
        Nothing when the folder does not exist. Only this folder can be listed: the transport
        refuses any other."""
        if self.statement_folder is None:
            raise GraphCallRefused("refused Graph call: no statement folder is set")
        folder_id: str | None = None
        for name in self.statement_folder.strip("/").split("/"):
            folder_id = self.find_folder(folder_id, name)
            if folder_id is None:
                return
        if folder_id is None:
            return
        _check_id(folder_id)
        self._transport.listable_folder = folder_id
        url = f"{self._user}/mailFolders/{folder_id}/messages"
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
            link = page.get("@odata.nextLink")
            url, params = (self._next_page(link, folder_id) if link else ""), None

    def _next_page(self, link: Any, folder_id: str | None = None) -> str:
        """The URL for the next page of the Inbox, built from Graph's ``@odata.nextLink``.

        Real Graph may write the mailbox as ``users('...')`` or percent encode the ``@``, and
        the Inbox as ``mailFolders('Inbox')``. Those all mean our Inbox, so the next page is
        asked for on our own allowlisted path with Graph's query. Any other link (another host,
        a mailbox GUID, another mailbox or folder) is refused with a clear error, so a sweep
        never silently stops after the first page (#90). With ``folder_id`` the link must name
        that folder of our mailbox instead of the Inbox (#166)."""
        if not isinstance(link, str):
            raise GraphPagingRefused("refused Graph paging link: it is not text")
        url = httpx.URL(link)
        base = self._http.base_url
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
            raise GraphPagingRefused(
                f"refused Graph paging link to another host: {url.host or '(none)'}"
            )
        path = url.raw_path.decode("ascii").partition("?")[0]
        prefix = base.raw_path.decode("ascii").rstrip("/") + "/"
        pattern = _INBOX_PAGE_PATH if folder_id is None else _FOLDER_PAGE_PATH
        found = (
            pattern.fullmatch(unquote(path[len(prefix) :]).lower())
            if path.startswith(prefix)
            else None
        )
        if (
            folder_id is not None
            and found is not None
            and (found["folder"] or found["folder_quoted"]) != folder_id.lower()
        ):
            found = None
        if found is None or (found["plain"] or found["quoted"]) != self.mailbox.lower():
            log.error("Refused Graph paging link %s, the sweep stops after this page", path)
            raise GraphPagingRefused(
                f"refused Graph paging link {path}: it does not name the Inbox of the mailbox"
                " as ARGUS knows it, or the folder asked for, so the sweep stops here instead"
                " of skipping emails"
                " (see docs/runbooks/go-live.md)"
            )
        query = url.query.decode("ascii")
        folder = "inbox" if folder_id is None else folder_id
        return f"{self._user}/mailFolders/{folder}/messages" + (f"?{query}" if query else "")

    def get_raw(self, message_id: str) -> bytes:
        """The email exactly as received, as an .eml file with every attachment in it."""
        _check_id(message_id)
        return self._get(f"{self._user}/messages/{message_id}/$value").content

    def create_forward(self, message_id: str, comment: str) -> str:
        """Save a forward of this email to the reminder address as a draft, with every original
        attachment, and return the draft's immutable ID. Nothing is sent yet."""
        _check_id(message_id)
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

    def send_notice(self, subject: str, text: str) -> None:
        """Send a short plain text notice about a Wise statement to the reminder address
        (Benjamin, #56). Sent once, without retries: a notice sent twice is better than none,
        but the caller decides about trying again."""
        if not self.forward_to:
            raise GraphCallRefused("refused Graph notice: no reminder address is set")
        body = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": self.forward_to}}],
            },
            "saveToSentItems": True,
        }
        self._post(f"{self._user}/sendMail", body)

    def is_draft(self, message_id: str) -> bool:
        """Whether this message is still an unsent draft."""
        _check_id(message_id)
        answer = self._get(f"{self._user}/messages/{message_id}", {"$select": "isDraft"}).json()
        return bool(answer.get("isDraft")) if isinstance(answer, dict) else False

    def create_digest(self, subject: str, html: str) -> str:
        """Save the daily digest to the digest address as a new draft and return the draft's
        immutable ID. Nothing is sent yet (#61)."""
        if not self.digest_to:
            raise GraphCallRefused("refused Graph digest: no digest address is set")
        body = {
            "subject": subject,
            "body": {"contentType": "html", "content": html},
            "toRecipients": [{"emailAddress": {"address": self.digest_to}}],
        }
        draft = self._post(f"{self._user}/messages", body).json()
        draft_id = draft.get("id") if isinstance(draft, dict) else None
        if not isinstance(draft_id, str) or not re.fullmatch(_MESSAGE_ID, draft_id):
            raise GraphApiError("POST", "messages", 200, "no draft ID in the answer")
        return draft_id

    def send_draft(
        self, message_id: str, to: str | None = None, *, no_attachments: bool = False
    ) -> None:
        """Send a saved draft. Graph sends a draft only once.

        ``to`` is the one address the draft must go to: the forward address unless named, or
        the digest address for the digest.

        The draft sits in the shared mailbox until it is sent, so a person could change it in
        the meantime. Right before sending, ARGUS reads its recipients again and refuses unless
        it goes to that one address only, with no copy (compliance review of PR 93).
        ``no_attachments`` also refuses a draft that has an attachment: the digest never has
        one, so an attachment means a person added it (compliance review of PR 161)."""
        _check_id(message_id)
        expected = self.forward_to if to is None else to
        if not expected or expected.lower() not in {
            a.lower() for a in (self.forward_to, self.digest_to) if a
        }:
            raise GraphCallRefused("refused to send the draft: not an address set for ARGUS")
        answer = self._get(
            f"{self._user}/messages/{message_id}",
            {"$select": "isDraft,toRecipients,ccRecipients,bccRecipients,hasAttachments"},
        ).json()
        problem = draft_recipient_problem(answer, expected)
        if problem is None and no_attachments and answer.get("hasAttachments") is not False:
            problem = "it has an attachment"
        if problem:
            log.warning("Refused to send draft %s: %s", message_id, problem)
            raise GraphCallRefused(f"refused to send the draft: {problem}")
        self._post(f"{self._user}/messages/{message_id}/send")

    def _folders_url(self, parent_id: str | None) -> str:
        if parent_id is None:
            return f"{self._user}/mailFolders"
        _check_id(parent_id)
        return f"{self._user}/mailFolders/{parent_id}/childFolders"

    def find_folder(self, parent_id: str | None, name: str) -> str | None:
        """The ID of the folder called ``name`` directly under ``parent_id``, or at the top of
        the mailbox when ``parent_id`` is None. None when there is no such folder."""
        _check_folder_name(name)
        page = self._get(
            self._folders_url(parent_id),
            {"$filter": f"displayName eq '{name}'", "$select": "id,displayName"},
        ).json()
        for item in page.get("value", []):
            if item.get("displayName") == name:
                return str(item["id"])
        return None

    def folders(self, parent_id: str | None) -> list[MailFolder]:
        """The folders directly under ``parent_id``, or at the top of the mailbox when it is
        None. Reads at most FOLDER_PAGE of them, which is plenty for the demo mailbox (#135)."""
        page = self._get(
            self._folders_url(parent_id), {"$select": "id,displayName", "$top": str(FOLDER_PAGE)}
        ).json()
        return [
            MailFolder(id=str(item["id"]), name=str(item.get("displayName") or ""))
            for item in page.get("value", [])
        ]

    def folder_messages(self, folder_id: str) -> tuple[list[FolderMessage], bool]:
        """The newest emails in the folder, newest first, and whether there are more than were
        read. Only against the fake Graph: the demo mailbox page (#135) uses it, and for the
        real mailbox people look in Outlook."""
        if not self.standin:
            raise GraphCallRefused("refused Graph call: folder listings are for the demo only")
        _check_id(folder_id)
        page = self._get(
            f"{self._user}/mailFolders/{folder_id}/messages",
            {
                "$select": "subject,from,receivedDateTime",
                "$orderby": "receivedDateTime desc",
                "$top": str(FOLDER_PAGE),
            },
        ).json()
        found = []
        for item in page.get("value", []):
            sender = item.get("from") if isinstance(item.get("from"), dict) else {}
            address = sender.get("emailAddress") if isinstance(sender, dict) else None
            address = address if isinstance(address, dict) else {}
            found.append(
                FolderMessage(
                    subject=str(item.get("subject") or ""),
                    sender_name=str(address.get("name") or ""),
                    sender_address=str(address.get("address") or ""),
                    received_at=_parse_time(item.get("receivedDateTime")),
                )
            )
        return found, bool(page.get("@odata.nextLink"))

    def create_folder(self, parent_id: str | None, name: str) -> str:
        """Create the folder and return its ID. If it was created meanwhile (Graph answers
        409 ErrorFolderExists), return the one that is there."""
        _check_folder_name(name)
        response = self._send("POST", self._folders_url(parent_id), body={"displayName": name})
        if response.status_code == 409:
            found = self.find_folder(parent_id, name)
            if found is not None:
                return found
        if response.is_error:
            raise self._error(response)
        return str(response.json()["id"])

    def folder_of(self, message_id: str) -> str | None:
        """The ID of the folder the email is in, or None when it is not in the mailbox."""
        _check_id(message_id)
        response = self._send(
            "GET", f"{self._user}/messages/{message_id}", {"$select": "parentFolderId"}
        )
        if response.status_code == 404:
            return None
        if response.is_error:
            raise self._error(response)
        return str(response.json()["parentFolderId"])

    def move(self, message_id: str, folder_id: str) -> None:
        """Move the email into the folder. With immutable IDs it keeps its ID."""
        _check_id(message_id)
        _check_id(folder_id)
        moved = self._checked(
            "POST", f"{self._user}/messages/{message_id}/move", body={"destinationId": folder_id}
        ).json()
        if moved.get("id") != message_id:
            raise GraphApiError(
                "POST", f"{self._user}/messages/{message_id}/move", 200, "IdChangedOnMove"
            )

    def inbox(self) -> str:
        """The ID of the well known folder Inbox, read once (#72)."""
        if self._inbox is None:
            folder = self._get(f"{self._user}/mailFolders/inbox", {"$select": "id"})
            self._inbox = str(folder.json()["id"])
        return self._inbox

    def deleted_items(self) -> str:
        """The ID of the well known folder Deleted Items, read once."""
        if self._deleted_items is None:
            folder = self._get(f"{self._user}/mailFolders/deleteditems", {"$select": "id"})
            self._deleted_items = str(folder.json()["id"])
        return self._deleted_items

    def delete_from(self, message_id: str, folder_id: str) -> bool:
        """Delete the email, but only if it is in ``folder_id``. Graph moves it to Deleted
        Items, where Exchange keeps it recoverable for at least 14 days (ADR 0003).

        Returns False when the email was already deleted: it is in Deleted Items, or no longer
        in the mailbox at all. An email in any other folder is refused, so a mistake can never
        delete an invoice."""
        parent = self.folder_of(message_id)
        if parent is None or parent == self.deleted_items():
            return False
        if parent != folder_id:
            raise GraphCallRefused("refused to delete an email that is not in ARGUS/Removed")
        response = self._send("DELETE", f"{self._user}/messages/{message_id}")
        if response.status_code == 404:
            return False
        if response.is_error:
            raise self._error(response)
        return True


def _addresses(recipients: Any) -> list[Any]:
    if not isinstance(recipients, list):
        return []
    found = []
    for recipient in recipients:
        address = recipient.get("emailAddress") if isinstance(recipient, dict) else None
        found.append(address.get("address") if isinstance(address, dict) else None)
    return found


def draft_recipient_problem(message: Any, forward_to: str) -> str | None:
    """Why a draft must not be sent, or None when it goes to ``forward_to`` only. Used for the
    digest address too (#61), with the same rules."""
    if not isinstance(message, dict) or message.get("isDraft") is not True:
        return "it is not a draft"
    if _addresses(message.get("ccRecipients")) or _addresses(message.get("bccRecipients")):
        return "it has a copy recipient"
    to = _addresses(message.get("toRecipients"))
    if len(to) != 1:
        return "it must have exactly one recipient"
    if not isinstance(to[0], str) or not forward_to or to[0].lower() != forward_to.lower():
        return "it goes to an address that is not the one set for it"
    return None


def _check_id(value: str) -> None:
    if not re.fullmatch(_MESSAGE_ID, value):
        raise GraphCallRefused("refused Graph call: not a Graph ID")


# A folder name goes into a $filter, so only plain names are allowed.
FOLDER_NAME = re.compile(r"[A-Za-z][A-Za-z ]{0,63}")
# What a lookup by name accepts: the same plus the German letters, for the statement folder
# ARGUS/Kontoauszüge (#166). None of them can end the quoted value in the $filter.
_LOOKUP_NAME = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß ]{0,63}")


def lookup_name_ok(name: str) -> bool:
    """Whether a folder of this name can be looked up (:meth:`GraphClient.find_folder`)."""
    return _LOOKUP_NAME.fullmatch(name) is not None


def _check_folder_name(name: str) -> None:
    if not _LOOKUP_NAME.fullmatch(name):
        raise GraphCallRefused("refused Graph call: not a plain folder name")

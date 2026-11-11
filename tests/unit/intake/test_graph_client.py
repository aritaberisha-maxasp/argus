"""The Graph client reads the one mailbox with immutable IDs and the least permissions (#24).

Covers IN-01 and NF-06: it lists the Inbox and reads each email through the supported Graph
interface, and it refuses to work with more than Mail.ReadWrite and Mail.Send.
"""

import hashlib

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from argus.intake.graph import (
    IMMUTABLE_IDS,
    PERMISSIONS,
    TOKEN_SCOPE,
    GraphApiError,
    GraphCallRefused,
    GraphClient,
    GraphPagingRefused,
    check_token,
    token_permissions,
)
from standins.graph.app import DELETED_ITEMS_ID, FIXTURES, MAILBOX, create_app, fake_token
from tests.unit.intake.graph_helpers import (
    BASE_URL,
    TOKEN,
    StaticTokens,
    client_for,
    standin_transport,
)


@pytest.fixture
def app() -> FastAPI:
    return create_app(token=TOKEN)


@pytest.mark.req("NF-06")
def test_the_client_asks_only_for_mail_readwrite_and_mail_send() -> None:
    assert frozenset({"Mail.ReadWrite", "Mail.Send"}) == PERMISSIONS
    tokens = StaticTokens()
    with client_for(create_app(token=TOKEN), tokens) as graph:
        next(graph.list_inbox())
    # An app token for Graph: the permissions themselves are granted to the app, not asked for.
    assert set(tokens.scopes) == {TOKEN_SCOPE}


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    "roles",
    [[], ["Mail.ReadWrite"], ["Mail.ReadWrite", "Mail.Send"]],
    ids=["exchange-rbac-no-roles", "read-only", "both"],
)
def test_a_token_within_the_two_permissions_is_accepted(roles: list[str]) -> None:
    check_token(SecretStr(fake_token(roles)))


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    "extra", ["Mail.Read", "User.Read.All", "Files.ReadWrite.All", "Mail.ReadWrite.Shared"]
)
def test_a_token_with_any_other_permission_is_refused_before_a_call(
    app: FastAPI, extra: str
) -> None:
    tokens = StaticTokens(fake_token(["Mail.ReadWrite", "Mail.Send", extra]))
    with client_for(app, tokens) as graph, pytest.raises(GraphCallRefused, match=extra):
        next(graph.list_inbox())
    assert app.state.mailbox.prefer_headers == [], "nothing may reach Graph"


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("token", ["not-a-jwt", "a.%%%.c", "a.bnVsbA.c"])
def test_a_token_that_cannot_be_read_is_refused(token: str) -> None:
    with pytest.raises(GraphCallRefused):
        token_permissions(SecretStr(token))


@pytest.mark.req("IN-01")
def test_the_inbox_is_listed_with_immutable_ids_across_pages(app: FastAPI) -> None:
    with client_for(app) as graph:
        messages = list(graph.list_inbox())
    assert len(messages) == 50
    assert {m.id for m in messages} == set(app.state.mailbox.inbox)
    assert len({m.id for m in messages}) == 50
    assert all(h == IMMUTABLE_IDS for h in app.state.mailbox.prefer_headers)


@pytest.mark.req("IN-01")
def test_paging_follows_every_next_link(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("argus.intake.graph.PAGE_SIZE", 7)
    with client_for(app) as graph:
        messages = list(graph.list_inbox())
    assert len({m.id for m in messages}) == 50
    assert len(app.state.mailbox.prefer_headers) == 8  # 7 full pages and one of 1
    received = [m.received_at for m in messages if m.received_at is not None]
    assert len(received) == 50
    assert received == sorted(received), "oldest first"


@pytest.mark.req("IN-01", "NF-01")
def test_each_email_is_read_byte_for_byte(app: FastAPI) -> None:
    expected = {hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.glob("*.eml")}
    with client_for(app) as graph:
        got = {hashlib.sha256(graph.get_raw(m.id)).hexdigest() for m in graph.list_inbox()}
    assert got == expected
    assert all(h == IMMUTABLE_IDS for h in app.state.mailbox.prefer_headers)


@pytest.mark.req("IN-01")
def test_throttling_is_retried_after_the_time_graph_asks_for(app: FastAPI) -> None:
    app.state.mailbox.fail_next.extend([(429, 7), (503, 2)])
    waits: list[float] = []
    graph = GraphClient(
        StaticTokens(),
        MAILBOX,
        "https://graph.example/v1.0",
        transport=standin_transport(app),
        sleep=waits.append,
    )
    with graph:
        assert len(list(graph.list_inbox())) == 50
    assert waits == [7.0, 2.0]


@pytest.mark.req("IN-01")
def test_throttling_that_does_not_end_raises_with_the_graph_code(app: FastAPI) -> None:
    app.state.mailbox.fail_next.extend([(429, 1)] * 10)
    with client_for(app) as graph, pytest.raises(GraphApiError) as error:
        next(graph.list_inbox())
    assert error.value.status == 429
    assert error.value.code == "TooManyRequests"


@pytest.mark.req("NF-06")
def test_another_mailbox_is_refused_by_the_client(app: FastAPI) -> None:
    with client_for(app) as graph, pytest.raises(GraphCallRefused):
        graph._http.get("users/ceo@maxasp.example/mailFolders/inbox/messages")
    assert app.state.mailbox.prefer_headers == []


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "users/accounts@maxasp.example/mailFolders/drafts/messages"),
        ("GET", "users/accounts@maxasp.example/calendar/events"),
        ("POST", "users/accounts@maxasp.example/sendMail"),
        ("DELETE", "users/accounts@maxasp.example/mailFolders/AAk"),
        ("PATCH", "users/accounts@maxasp.example/messages/AAk"),
        ("POST", "users/accounts@maxasp.example/messages/AAk/forward"),
        ("POST", "users/accounts@maxasp.example/messages/AAk/copy"),
        ("POST", "users/accounts@maxasp.example/messages/AAk/permanentDelete"),
        ("DELETE", "users/accounts@maxasp.example/messages/AAk/attachments/AAk"),
        ("GET", "users"),
        (
            "GET",
            "https://evil.example/v1.0/users/accounts@maxasp.example/mailFolders/inbox/messages",
        ),
    ],
)
def test_calls_outside_the_allowlist_are_refused(app: FastAPI, method: str, path: str) -> None:
    with client_for(app) as graph, pytest.raises(GraphCallRefused):
        graph._http.request(method, path)
    assert app.state.mailbox.prefer_headers == []


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("bad_id", ["../users/ceo", "AAk/../../x", "", "AAk?$select=x"])
def test_a_message_id_cannot_change_the_path(app: FastAPI, bad_id: str) -> None:
    with client_for(app) as graph, pytest.raises(GraphCallRefused):
        graph.get_raw(bad_id)


@pytest.mark.req("NF-06")
def test_plain_http_is_only_for_the_stand_in() -> None:
    with pytest.raises(ValueError, match="https"):
        GraphClient(StaticTokens(), MAILBOX, "http://graph.microsoft.com/v1.0")


@pytest.mark.req("NF-06")
def test_the_token_never_shows_in_errors_or_repr(app: FastAPI) -> None:
    app.state.mailbox.fail_next.extend([(429, 1)] * 10)
    with client_for(app) as graph:
        assert TOKEN not in repr(graph)
        with pytest.raises(GraphApiError) as error:
            next(graph.list_inbox())
    assert TOKEN not in str(error.value)


def test_the_fake_rejects_a_wrong_token(app: FastAPI) -> None:
    tokens = StaticTokens(fake_token(["Mail.Send"]))
    with client_for(app, tokens) as graph, pytest.raises(GraphApiError) as error:
        next(graph.list_inbox())
    assert error.value.status == 401


def test_without_the_prefer_header_the_fake_gives_other_ids(app: FastAPI) -> None:
    transport = standin_transport(app)
    with httpx.Client(transport=transport) as http:
        answer = http.get(
            f"https://graph.example/v1.0/users/{MAILBOX}/mailFolders/inbox/messages",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    ids = {m["id"] for m in answer.json()["value"]}
    assert ids and not ids & set(app.state.mailbox.inbox)


# Folders, moving and deleting (#26) --------------------------------------------------------


@pytest.mark.req("IN-06")
def test_a_folder_is_found_or_created_under_its_parent(app: FastAPI) -> None:
    with client_for(app) as graph:
        assert graph.find_folder(None, "ARGUS") is None
        argus = graph.create_folder(None, "ARGUS")
        removed = graph.create_folder(argus, "Removed")
        assert graph.find_folder(None, "ARGUS") == argus
        assert graph.find_folder(argus, "Removed") == removed
        assert graph.find_folder(None, "Removed") is None
        # Created meanwhile by another run: Graph answers 409, the client returns the folder.
        assert graph.create_folder(argus, "Removed") == removed
    assert sorted(f.name for f in app.state.mailbox.folders.values()) == [
        "ARGUS",
        "Deleted Items",
        "Inbox",
        "Removed",
    ]


@pytest.mark.req("IN-06")
def test_a_move_keeps_the_immutable_id_and_takes_the_email_out_of_the_inbox(
    app: FastAPI,
) -> None:
    with client_for(app) as graph:
        message = next(graph.list_inbox())
        folder = graph.create_folder(None, "ARGUS")
        graph.move(message.id, folder)
        graph.move(message.id, folder)  # again: nothing changes
        assert graph.folder_of(message.id) == folder
        assert message.id not in {m.id for m in graph.list_inbox()}
        assert graph.get_raw(message.id) == app.state.mailbox.messages[message.id].raw
    assert all(h == IMMUTABLE_IDS for h in app.state.mailbox.prefer_headers)


@pytest.mark.req("IN-06")
def test_delete_only_works_on_an_email_in_the_given_folder(app: FastAPI) -> None:
    with client_for(app) as graph:
        first, second = [m.id for m in graph.list_inbox()][:2]
        removed = graph.create_folder(None, "Removed")
        graph.move(first, removed)

        with pytest.raises(GraphCallRefused, match="not in ARGUS/Removed"):
            graph.delete_from(second, removed)
        assert graph.delete_from(first, removed) is True
        # Like Graph, the email is now in Deleted Items and can still be found there.
        assert graph.folder_of(first) == graph.deleted_items() == DELETED_ITEMS_ID
        assert graph.delete_from(first, removed) is False  # already deleted
    assert set(app.state.mailbox.deleted) == {first}


@pytest.mark.req("IN-06")
def test_an_email_gone_from_the_mailbox_counts_as_already_deleted(app: FastAPI) -> None:
    with client_for(app) as graph:
        first = next(graph.list_inbox()).id
        removed = graph.create_folder(None, "Removed")
        graph.move(first, removed)
        app.state.mailbox.remove(first)  # purged from Deleted Items, Graph answers 404
        assert graph.folder_of(first) is None
        assert graph.delete_from(first, removed) is False


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("name", ["", "ARGUS' or true", "a/b", "Inbox)", "x" * 65])
def test_a_folder_name_cannot_change_the_query(app: FastAPI, name: str) -> None:
    with client_for(app) as graph, pytest.raises(GraphCallRefused):
        graph.find_folder(None, name)
    with client_for(app) as graph, pytest.raises(GraphCallRefused):
        graph.create_folder(None, name)
    assert app.state.mailbox.prefer_headers == []


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("bad_id", ["../users/ceo", "AAk/../../x", "", "AAk?$select=x"])
def test_ids_cannot_change_the_path_of_a_move_or_delete(app: FastAPI, bad_id: str) -> None:
    with client_for(app) as graph:
        for call in (
            lambda: graph.move(bad_id, "AAk"),
            lambda: graph.move("AAk", bad_id),
            lambda: graph.delete_from(bad_id, "AAk"),
            lambda: graph.folder_of(bad_id),
            lambda: graph.find_folder(bad_id, "ARGUS"),
        ):
            with pytest.raises(GraphCallRefused):
                call()
    assert app.state.mailbox.prefer_headers == []


FAKE_PAGE_PATH = f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages"


def reshaping_transport(app: FastAPI, shape: str) -> httpx.MockTransport:
    """The fake Graph, but each paging link it writes is rewritten to ``shape`` (scheme, host
    and path), the way real Graph might write it. The query (the skip) is kept (#90)."""
    inner = standin_transport(app)

    def forward(request: httpx.Request) -> httpx.Response:
        answer = inner.handle_request(request)
        body = answer.json() if answer.headers.get("content-type") == "application/json" else {}
        link = body.get("@odata.nextLink") if isinstance(body, dict) else None
        if not link:
            return answer
        _, _, query = link.partition("?")
        body["@odata.nextLink"] = f"{shape}?{query}"
        return httpx.Response(answer.status_code, json=body)

    return httpx.MockTransport(forward)


def reshaped_client(app: FastAPI, shape: str) -> GraphClient:
    return GraphClient(StaticTokens(), MAILBOX, BASE_URL, transport=reshaping_transport(app, shape))


@pytest.mark.req("IN-01")
@pytest.mark.parametrize(
    "path",
    [
        FAKE_PAGE_PATH,
        f"/v1.0/users('{MAILBOX}')/mailFolders('Inbox')/messages",
        f"/v1.0/users('{MAILBOX.replace('@', '%40')}')/mailFolders('inbox')/messages",
        f"/v1.0/users/{MAILBOX.replace('@', '%40')}/mailFolders/Inbox/messages",
        f"/v1.0/Users/{MAILBOX.upper()}/MailFolders/inbox/messages",
    ],
    ids=["as-the-fake", "quoted", "quoted-encoded", "encoded", "other-case"],
)
def test_a_paging_link_for_our_inbox_in_another_form_reads_every_page(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr("argus.intake.graph.PAGE_SIZE", 7)
    with reshaped_client(app, f"https://graph.example{path}") as graph:
        messages = list(graph.list_inbox())
    assert len({m.id for m in messages}) == 50
    assert len(app.state.mailbox.prefer_headers) == 8


@pytest.mark.req("IN-01", "NF-06")
@pytest.mark.parametrize(
    "link",
    [
        "https://graph.example/v1.0/users/0f6b9c1e-2a7d-4c55-9a43-5d2e8b7c1f00"
        "/mailFolders/inbox/messages",
        "https://graph.example/v1.0/users('0f6b9c1e-2a7d-4c55-9a43-5d2e8b7c1f00')"
        "/mailFolders('AAMkAGInbox0000')/messages",
        "https://graph.example/v1.0/users/someone@maxasp.example/mailFolders/inbox/messages",
        f"https://graph.example/v1.0/users/{MAILBOX}/mailFolders/sentitems/messages",
        f"https://graph.example/beta/users/{MAILBOX}/mailFolders/inbox/messages",
        f"https://elsewhere.example/v1.0/users/{MAILBOX}/mailFolders/inbox/messages",
        f"http://graph.example/v1.0/users/{MAILBOX}/mailFolders/inbox/messages",
        f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages",
        # The whole path must match, not just a part of it.
        f"https://graph.example/v1.0/x/users/{MAILBOX}/mailFolders/inbox/messages",
        f"https://graph.example/v1.0/users/{MAILBOX}/mailFolders/inbox/messages/delta",
    ],
    ids=[
        "mailbox-guid",
        "guid-and-folder-id",
        "other-mailbox",
        "other-folder",
        "other-version",
        "other-host",
        "plain-http",
        "relative",
        "extra-segment-before",
        "extra-segment-after",
    ],
)
def test_a_paging_link_of_an_unknown_shape_stops_the_listing_with_a_clear_refusal(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    monkeypatch.setattr("argus.intake.graph.PAGE_SIZE", 7)
    with reshaped_client(app, link) as graph:
        listing = graph.list_inbox()
        first_page = [next(listing) for _ in range(7)]
        with pytest.raises(GraphPagingRefused, match="paging link"):
            next(listing)
    assert len(first_page) == 7
    assert len(app.state.mailbox.prefer_headers) == 1, "the odd link never reached Graph"

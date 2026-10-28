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
    check_token,
    token_permissions,
)
from standins.graph.app import FIXTURES, MAILBOX, create_app, fake_token
from tests.unit.intake.graph_helpers import TOKEN, StaticTokens, client_for, standin_transport


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
        ("DELETE", "users/accounts@maxasp.example/messages/AAk"),
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

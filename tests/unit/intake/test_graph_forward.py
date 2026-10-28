"""The Graph client forwards only to the one reminder address, and only when it is set (#27).

Covers IN-05 and NF-06: Mail.Send is used for nothing but forwarding payment reminders, and a
forward to any other address is refused before it leaves ARGUS.
"""

import json
from collections.abc import Iterable

import pytest
from fastapi import FastAPI

from argus.intake.graph import (
    GraphCallRefused,
    GraphClient,
    draft_recipient_problem,
    forward_body_problem,
)
from argus.intake.reminders import FORWARD_TO_ENV, forward_address_from_environment
from standins.graph.app import MAILBOX, Mailbox, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

FORWARD_TO = "benjamin@maxasp.example"


@pytest.fixture
def app() -> FastAPI:
    return create_app(token=TOKEN)


def client(app: FastAPI, forward_to: str | None = FORWARD_TO) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(app),
        sleep=lambda _: None,
        forward_to=forward_to,
    )


def first_id(graph: GraphClient) -> str:
    return next(graph.list_inbox()).id


def body(*addresses: str, **extra: object) -> bytes:
    return json.dumps(
        {
            "comment": "x",
            "toRecipients": [{"emailAddress": {"address": a}} for a in addresses],
            **extra,
        }
    ).encode()


@pytest.mark.req("IN-05", "NF-06")
def test_without_a_forward_address_the_client_cannot_forward_or_send(app: FastAPI) -> None:
    graph = client(app, forward_to=None)
    message_id = first_id(graph)
    with pytest.raises(GraphCallRefused):
        graph.create_forward(message_id, "x")
    with pytest.raises(GraphCallRefused):
        graph.send_draft(message_id)
    box: Mailbox = app.state.mailbox
    assert box.drafts == {}


@pytest.mark.req("IN-05", "NF-06")
def test_a_forward_to_another_address_is_refused_before_it_leaves(app: FastAPI) -> None:
    graph = client(app)
    message_id = first_id(graph)
    graph.forward_to = "someone.else@example.test"  # the guard keeps the address it was given
    with pytest.raises(GraphCallRefused, match="not the one set"):
        graph.create_forward(message_id, "x")
    box: Mailbox = app.state.mailbox
    assert box.drafts == {}


@pytest.mark.req("IN-05")
def test_a_forward_is_saved_as_a_draft_and_sent_once(app: FastAPI) -> None:
    graph = client(app)
    draft_id = graph.create_forward(first_id(graph), "x")
    assert graph.is_draft(draft_id)
    graph.send_draft(draft_id)
    assert not graph.is_draft(draft_id)
    box: Mailbox = app.state.mailbox
    assert [d.to for d in box.sent] == [[FORWARD_TO]]


@pytest.mark.req("IN-05", "NF-06")
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (body(FORWARD_TO), None),
        (body(FORWARD_TO.upper()), None),
        (body("other@example.test"), "not the one set"),
        (body(FORWARD_TO, "other@example.test"), "exactly one recipient"),
        (body(), "exactly one recipient"),
        (body(FORWARD_TO, ccRecipients=[]), "only carry"),
        (body(FORWARD_TO, bccRecipients=[]), "only carry"),
        (body(FORWARD_TO, message={"subject": "x"}), "only carry"),
        (b"not json", "not JSON"),
        (json.dumps({"toRecipients": [{"emailAddress": "x"}]}).encode(), "not the one set"),
    ],
)
def test_the_forward_body_names_only_the_reminder_address(
    content: bytes, problem: str | None
) -> None:
    found = forward_body_problem(content, FORWARD_TO)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


@pytest.mark.req("IN-05")
def test_the_forward_address_comes_from_the_settings() -> None:
    assert forward_address_from_environment({FORWARD_TO_ENV: f" {FORWARD_TO} "}) == FORWARD_TO
    for value in ("", "not an address", "a@b.example, c@d.example"):
        with pytest.raises(ValueError, match=FORWARD_TO_ENV):
            forward_address_from_environment({FORWARD_TO_ENV: value})
    with pytest.raises(ValueError):
        forward_address_from_environment({})


def draft(
    to: list[str | None],
    cc: tuple[str, ...] = (),
    bcc: tuple[str, ...] = (),
    **extra: object,
) -> dict[str, object]:
    def recipients(items: Iterable[str | None]) -> list[dict[str, dict[str, str | None]]]:
        return [{"emailAddress": {"address": a}} for a in items]

    return {
        "isDraft": True,
        "toRecipients": recipients(to),
        "ccRecipients": recipients(cc),
        "bccRecipients": recipients(bcc),
        **extra,
    }


@pytest.mark.req("IN-05", "NF-06")
@pytest.mark.parametrize(
    ("message", "problem"),
    [
        (draft([FORWARD_TO]), None),
        (draft([FORWARD_TO.upper()]), None),
        (draft([FORWARD_TO], cc=("x@example.test",)), "copy"),
        (draft([FORWARD_TO], bcc=("x@example.test",)), "copy"),
        (draft(["x@example.test"]), "not the one set"),
        (draft([FORWARD_TO, "x@example.test"]), "exactly one"),
        (draft([]), "exactly one"),
        (draft([None]), "not the one set"),
        (draft([FORWARD_TO], isDraft=False), "not a draft"),
        ({"toRecipients": []}, "not a draft"),
        ("not a message", "not a draft"),
    ],
)
def test_a_draft_is_sent_only_when_it_goes_to_the_reminder_address_alone(
    message: object, problem: str | None
) -> None:
    found = draft_recipient_problem(message, FORWARD_TO)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


@pytest.mark.req("IN-05", "NF-06")
def test_send_reads_the_recipients_again_and_refuses_a_changed_draft(app: FastAPI) -> None:
    graph = client(app)
    draft_id = graph.create_forward(first_id(graph), "x")
    box: Mailbox = app.state.mailbox
    box.drafts[draft_id].bcc = ["x@example.test"]
    with pytest.raises(GraphCallRefused, match="copy"):
        graph.send_draft(draft_id)
    assert box.sent == []

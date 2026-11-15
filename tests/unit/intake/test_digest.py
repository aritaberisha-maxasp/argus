"""The daily digest of open exceptions (#61): what the email says, and that the Graph client
sends it to the one digest address and nowhere else.

Covers EX-03, NF-03 (no detail text, sender or email content in the digest or the logs) and
NF-06 (Mail.Send only for the reminder forward and the digest, each to its one address).
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus.cases.model import ExceptionReason
from argus.connections import connections_from_env
from argus.intake.digest import (
    APP_URL_ENV,
    MAX_LINES,
    DigestSettingsError,
    app_url_from_environment,
    render_digest,
    send_daily_digest,
)
from argus.intake.graph import (
    GraphCallRefused,
    GraphClient,
    allowed_paths,
    digest_body_problem,
)
from argus.intake.jobs import FAILED_MARK, main
from argus.queue_page.views import REASON_TEXT, QueueRow
from standins.graph.app import MAILBOX, Mailbox, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

DIGEST_TO = "benjamin@maxasp.example"
FORWARD_TO = "reminders@maxasp.example"
APP_URL = "https://argus.maxasp.example"
DAY = date(2026, 11, 16)


@pytest.fixture
def app() -> FastAPI:
    return create_app(token=TOKEN)


def client(
    app: FastAPI, digest_to: str | None = DIGEST_TO, forward_to: str | None = None
) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(app),
        sleep=lambda _: None,
        forward_to=forward_to,
        digest_to=digest_to,
    )


def mailbox(app: FastAPI) -> Mailbox:
    box: Mailbox = app.state.mailbox
    return box


def row(exception_id: int, age: int = 0, reason: ExceptionReason | None = None) -> QueueRow:
    reason = reason or ExceptionReason.DUPLICATE
    return QueueRow(
        exception_id=exception_id,
        reason=reason,
        reason_text=REASON_TEXT[reason],
        opened_at=datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
        age_working_days=age,
        case_id=exception_id,
        case_kind=None,
        email_id=None,
        statement_id=None,
        sender="lieferant-test.example",
    )


def body(to: list[str], **extra: object) -> bytes:
    message = {
        "subject": "ARGUS: 1 open exception",
        "body": {"contentType": "html", "content": "<p>x</p>"},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    return json.dumps({**message, **extra}).encode()


# What the digest says


@pytest.mark.req("EX-03")
def test_the_digest_lists_reason_age_opened_day_and_link() -> None:
    subject, html = render_digest([row(7, age=1), row(9, age=3)], APP_URL, DAY)

    assert subject == "ARGUS: 2 open exceptions on 16.11.2026"
    assert html.count("This may be a duplicate invoice") == 2
    assert "1 working day<" in html and "3 working days<" in html
    assert html.count("12.11.2026") == 2
    assert f'href="{APP_URL}/queue?exception=7"' in html
    assert f'href="{APP_URL}/queue?exception=9"' in html
    assert f'href="{APP_URL}/queue"' in html


@pytest.mark.req("EX-03")
def test_one_exception_reads_naturally() -> None:
    subject, html = render_digest([row(1)], APP_URL, DAY)

    assert subject == "ARGUS: 1 open exception on 16.11.2026"
    assert "1 open exception in ARGUS" in html
    assert "less than 1 working day" in html


@pytest.mark.req("EX-03", "NF-03")
def test_the_digest_holds_no_sender_or_detail() -> None:
    _, html = render_digest([row(1)], APP_URL, DAY)

    assert "lieferant-test.example" not in html


@pytest.mark.req("EX-03")
def test_a_long_queue_lists_the_oldest_and_points_to_the_rest() -> None:
    rows = [row(n) for n in range(1, MAX_LINES + 6)]

    subject, html = render_digest(rows, APP_URL, DAY)

    assert subject.startswith(f"ARGUS: {MAX_LINES + 5} open exceptions")
    assert f'queue?exception={MAX_LINES}"' in html
    assert f'queue?exception={MAX_LINES + 1}"' not in html
    assert "5 more open exceptions are on the" in html


@pytest.mark.req("EX-03")
def test_every_reason_has_words_in_the_digest() -> None:
    for reason in ExceptionReason:
        _, html = render_digest([row(1, reason=reason)], APP_URL, DAY)
        assert REASON_TEXT[reason] in html


# The app address for the links


@pytest.mark.req("EX-03")
def test_the_app_address_is_read_without_a_trailing_slash() -> None:
    assert app_url_from_environment({APP_URL_ENV: APP_URL + "/"}) == APP_URL


@pytest.mark.req("EX-03")
@pytest.mark.parametrize(
    "value", ["", "http://argus.maxasp.example", "https://", "https://a.example/?x=1", "argus"]
)
def test_a_missing_or_odd_app_address_is_refused(value: str) -> None:
    with pytest.raises(DigestSettingsError, match=APP_URL_ENV) as raised:
        app_url_from_environment({APP_URL_ENV: value})
    if value:
        assert value not in str(raised.value)


# The Graph client sends the digest to the one address and nowhere else


@pytest.mark.req("EX-03", "NF-06")
def test_the_digest_is_saved_as_a_draft_and_sent_once(app: FastAPI) -> None:
    graph = client(app)

    draft_id = graph.create_digest("ARGUS: 1 open exception", "<p>hello</p>")
    assert graph.is_draft(draft_id)
    graph.send_draft(draft_id, to=DIGEST_TO)

    (sent,) = mailbox(app).sent
    assert sent.to == [DIGEST_TO]
    assert sent.subject == "ARGUS: 1 open exception"
    assert not graph.is_draft(draft_id)


@pytest.mark.req("NF-06")
def test_without_a_digest_address_no_new_message_can_be_written(app: FastAPI) -> None:
    graph = client(app, digest_to=None, forward_to=FORWARD_TO)

    with pytest.raises(GraphCallRefused):
        graph.create_digest("subject", "<p>x</p>")
    with pytest.raises(GraphCallRefused):
        graph._post(f"users/{MAILBOX}/messages", json.loads(body([FORWARD_TO])))
    assert mailbox(app).drafts == {}


@pytest.mark.req("NF-06")
def test_new_messages_are_only_allowed_with_a_digest_address() -> None:
    def methods(**kwargs: str | None) -> set[tuple[str, str]]:
        return {(m, p.pattern) for m, p in allowed_paths("/v1.0", MAILBOX, **kwargs)}

    plain, forward, digest = methods(), methods(forward_to=FORWARD_TO), methods(digest_to=DIGEST_TO)
    assert not any(p.endswith("/messages") and m == "POST" for m, p in plain | forward)
    assert any(p.endswith("/messages") and m == "POST" for m, p in digest)
    assert not any(p.endswith("/createForward") for _, p in digest)
    assert not any(p.endswith("/send") for _, p in plain)


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (body([DIGEST_TO]), None),
        (body(["BENJAMIN@maxasp.example"]), None),
        (body(["someone@elsewhere.example"]), "not the one set for it"),
        (body([DIGEST_TO, "someone@elsewhere.example"]), "exactly one recipient"),
        (body([]), "exactly one recipient"),
        (body([DIGEST_TO], ccRecipients=[]), "only carry a subject"),
        (body([DIGEST_TO], attachments=[]), "only carry a subject"),
        (body([DIGEST_TO], body={"contentType": "html"}), "plain text or HTML"),
        (body([DIGEST_TO], subject=None), "subject is not text"),
        (b"not json", "not JSON"),
    ],
)
def test_the_digest_body_goes_to_the_digest_address_only(
    content: bytes, problem: str | None
) -> None:
    found = digest_body_problem(content, DIGEST_TO)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


@pytest.mark.req("NF-06")
def test_the_transport_refuses_a_digest_to_another_address(app: FastAPI) -> None:
    graph = client(app)

    with pytest.raises(GraphCallRefused, match="refused Graph digest"):
        graph._post(f"users/{MAILBOX}/messages", json.loads(body(["someone@elsewhere.example"])))
    assert mailbox(app).drafts == {}


@pytest.mark.req("NF-06")
def test_a_changed_digest_draft_is_not_sent(app: FastAPI) -> None:
    graph = client(app)
    draft_id = graph.create_digest("subject", "<p>x</p>")
    TestClient(app).patch(f"/_standin/drafts/{draft_id}", json={"bcc": ["x@elsewhere.example"]})

    with pytest.raises(GraphCallRefused, match="copy recipient"):
        graph.send_draft(draft_id, to=DIGEST_TO)
    assert mailbox(app).sent == []


@pytest.mark.req("NF-06")
def test_a_draft_is_only_sent_to_an_address_set_for_argus(app: FastAPI) -> None:
    graph = client(app)
    draft_id = graph.create_digest("subject", "<p>x</p>")

    with pytest.raises(GraphCallRefused, match="not an address set for ARGUS"):
        graph.send_draft(draft_id, to="someone@elsewhere.example")
    with pytest.raises(GraphCallRefused):
        graph.send_draft(draft_id)  # no forward address, so nothing to send to
    assert mailbox(app).sent == []


# Each job's client sends only what its job sends


DEMO = {
    "ARGUS_DEMO": "true",
    "ARGUS_GRAPH_URL": "http://ca-argus-graph/v1.0",
    "ARGUS_MAILBOX": MAILBOX,
    "ARGUS_GRAPH_STANDIN_TOKEN": "standin-token-for-tests",
    "ARGUS_REMINDER_FORWARD_TO": FORWARD_TO,
    "ARGUS_DIGEST_TO": DIGEST_TO,
}


@pytest.mark.req("NF-06")
def test_the_digest_client_cannot_forward_and_the_intake_client_cannot_send_digests() -> None:
    connections = connections_from_env(DEMO)

    digest = connections.graph_client(digest=True)
    intake = connections.graph_client()

    assert (digest.digest_to, digest.forward_to) == (DIGEST_TO, None)
    assert (intake.digest_to, intake.forward_to) == (None, FORWARD_TO)


@pytest.mark.req("EX-03")
def test_the_digest_refuses_a_client_without_a_digest_address(app: FastAPI) -> None:
    with pytest.raises(DigestSettingsError, match="ARGUS_DIGEST_TO"):
        send_daily_digest(None, client(app, digest_to=None), APP_URL)  # type: ignore[arg-type]


# The job


@pytest.mark.req("EX-03", "NF-03")
@pytest.mark.parametrize(
    ("missing", "named"),
    [(APP_URL_ENV, APP_URL_ENV), ("ARGUS_DIGEST_TO", "ARGUS_DIGEST_TO")],
)
def test_the_digest_job_fails_and_names_a_missing_setting(
    missing: str, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {
        **DEMO,
        APP_URL_ENV: APP_URL,
        "ARGUS_DATABASE_URL": "postgresql+psycopg://argus@localhost/argus_demo",
    }
    del environ[missing]

    with caplog.at_level(logging.INFO):
        assert main(["digest"], environ=environ) == 1

    assert named in caplog.text
    assert f"{FAILED_MARK} digest" in caplog.text
    assert DEMO["ARGUS_GRAPH_STANDIN_TOKEN"] not in caplog.text
    assert DIGEST_TO not in caplog.text


@pytest.mark.req("EX-03")
def test_the_days_in_the_digest_are_berlin_days() -> None:
    late = row(1)
    late = QueueRow(**{**late.__dict__, "opened_at": datetime(2026, 11, 12, 23, 30, tzinfo=UTC)})

    _, html = render_digest([late], APP_URL, DAY + timedelta(days=0))

    assert "13.11.2026" in html

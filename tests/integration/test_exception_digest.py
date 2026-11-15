"""Benjamin gets one email a day with every open exception (#61, EX-03).

Runs the digest against the fake Graph and a real PostgreSQL. The groups follow the acceptance
criteria of #61: the digest lists each open exception with reason, age and a link; no digest
without open exceptions; once a day even if the job starts twice. Covers EX-03, NF-01, NF-03.
"""

import threading
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import open_exception, unit_of_work
from argus.intake.digest import (
    DRAFT_CHANGED,
    DRAFT_GONE,
    DRAFTED,
    FOUND_SENT,
    SENT,
    SENT_NOW,
    DigestMailbox,
    send_daily_digest,
)
from argus.intake.graph import GraphClient
from argus.intake.jobs import DigestServices, main
from standins.graph.app import MAILBOX, Mailbox, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

pytestmark = pytest.mark.req("EX-03")

DIGEST_TO = "benjamin@maxasp.example"  # synthetic
APP_URL = "https://argus.maxasp.example"
OPENER = Actor.system("digest-test")
# Free text on the exception that must never reach the digest (NF-03).
DETAIL = "IBAN DE89 3704 0044 0532 0130 00 von Erika Mustermann"
_days = iter(range(1, 400))


def next_day() -> date:
    """A day no other test uses, so the once a day guard of one test never meets another's."""
    return date.fromordinal(date(2031, 1, 1).toordinal() + next(_days))


@pytest.fixture(autouse=True)
def empty_queue(owner: Engine) -> Iterator[None]:
    """Each test starts and ends with no open exception, as if a person resolved them all.
    Test cleanup only: the owner skips the triggers for it."""

    def resolve_all() -> None:
        with owner.begin() as connection:
            connection.execute(text("set local session_replication_role = replica"))
            connection.execute(
                text(
                    "update exception_item set outcome = 'retried', resolved_by = 'test',"
                    " resolved_at = clock_timestamp() where resolved_at is null"
                )
            )

    resolve_all()
    yield
    resolve_all()


@pytest.fixture
def fake() -> FastAPI:
    return create_app(token=TOKEN)


def mailbox(fake: FastAPI) -> Mailbox:
    box: Mailbox = fake.state.mailbox
    return box


def client(fake: FastAPI, digest_to: str | None = DIGEST_TO) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        sleep=lambda _: None,
        digest_to=digest_to,
    )


def open_audit_exceptions(app: Engine) -> list[int]:
    """Two open exceptions that belong to no case or email (#83)."""
    with unit_of_work(app, OPENER) as db:
        return [
            open_exception(db, reason, DETAIL)
            for reason in (ExceptionReason.AUDIT_COPY_FAILED, ExceptionReason.AUDIT_CHECK_FAILED)
        ]


def digest_events(app: Engine, day: date) -> list[tuple[str, dict[str, Any], str]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select event_type, details, actor from audit_event"
                " where event_type in (:d, :s) and details ->> 'day' = :day order by chain_seq"
            ),
            {"d": DRAFTED, "s": SENT, "day": day.isoformat()},
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


# The digest lists each open exception with reason, age and a link


def test_the_digest_lists_each_open_exception_with_reason_age_and_link(
    app: Engine, fake: FastAPI
) -> None:
    ids = open_audit_exceptions(app)
    day = next_day()

    result = send_daily_digest(app, client(fake), APP_URL, today=day)

    assert result.outcome == SENT_NOW
    assert result.exception_count == 2
    sent = mailbox(fake).sent
    assert len(sent) == 1
    assert sent[0].to == [DIGEST_TO]
    assert sent[0].cc == [] and sent[0].bcc == [] and sent[0].attachments == []
    assert sent[0].subject == f"ARGUS: 2 open exceptions on {day:%d.%m.%Y}"
    body = sent[0].body
    assert "The nightly copy of the audit log failed" in body
    assert "The audit log did not match its copies" in body
    assert body.count("less than 1 working day") == 2
    for exception_id in ids:
        assert f'href="{APP_URL}/queue?exception={exception_id}"' in body


@pytest.mark.req("EX-03", "NF-03")
def test_the_digest_holds_no_detail_text(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)

    send_daily_digest(app, client(fake), APP_URL, today=next_day())

    body = mailbox(fake).sent[0].body
    assert "Erika" not in body and "DE89" not in body


@pytest.mark.req("EX-03", "NF-01")
def test_the_audit_log_records_the_draft_and_the_send(app: Engine, fake: FastAPI) -> None:
    ids = open_audit_exceptions(app)
    day = next_day()

    result = send_daily_digest(app, client(fake), APP_URL, today=day)

    events = digest_events(app, day)
    assert [e[0] for e in events] == [DRAFTED, SENT]
    assert events[0][1]["exception_ids"] == ids
    assert events[0][1]["draft_message_id"] == result.draft_message_id
    assert events[1][1]["outcome"] == SENT_NOW
    assert all(e[2] == "exception-digest" for e in events)


# No digest when there are no exceptions


def test_no_digest_is_sent_when_no_exception_is_open(app: Engine, fake: FastAPI) -> None:
    day = next_day()

    result = send_daily_digest(app, client(fake), APP_URL, today=day)

    assert result.outcome == "nothing_open"
    assert result.ok
    assert mailbox(fake).drafts == {}
    assert digest_events(app, day) == []


def test_a_resolved_exception_is_not_listed(app: Engine, fake: FastAPI, owner: Engine) -> None:
    open_audit_exceptions(app)
    with owner.begin() as connection:
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(
            text(
                "update exception_item set outcome = 'retried', resolved_by = 'test',"
                " resolved_at = clock_timestamp()"
                " where resolved_at is null and reason = 'audit_check_failed'"
            )
        )

    send_daily_digest(app, client(fake), APP_URL, today=next_day())

    body = mailbox(fake).sent[0].body
    assert "The nightly copy of the audit log failed" in body
    assert "did not match its copies" not in body


# Once a day, even if the job starts twice


def test_a_second_run_on_the_same_day_sends_nothing(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    graph = client(fake)

    first = send_daily_digest(app, graph, APP_URL, today=day)
    second = send_daily_digest(app, graph, APP_URL, today=day)

    assert first.outcome == SENT_NOW
    assert second.outcome == "already_finished"
    assert second.ok
    assert len(mailbox(fake).sent) == 1
    assert len(mailbox(fake).drafts) == 1
    assert [e[0] for e in digest_events(app, day)] == [DRAFTED, SENT]


def test_the_next_day_sends_again(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    graph = client(fake)

    send_daily_digest(app, graph, APP_URL, today=next_day())
    send_daily_digest(app, graph, APP_URL, today=next_day())

    assert len(mailbox(fake).sent) == 2


def test_two_runs_at_the_same_time_send_one_digest(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def run() -> None:
        start.wait()
        outcomes.append(send_daily_digest(app, client(fake), APP_URL, today=day).outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(mailbox(fake).sent) == 1
    assert sorted(outcomes) in (["busy", SENT_NOW], ["already_finished", SENT_NOW])
    assert [e[0] for e in digest_events(app, day)] == [DRAFTED, SENT]


class CrashAfterSend:
    """Sends the draft, then breaks off before ARGUS can record it, as a crash would."""

    def __init__(self, graph: DigestMailbox) -> None:
        self._graph = graph
        self.digest_to = graph.digest_to

    def create_digest(self, subject: str, html: str) -> str:
        return self._graph.create_digest(subject, html)

    def is_draft(self, message_id: str) -> bool:
        return self._graph.is_draft(message_id)

    def send_draft(self, message_id: str, to: str | None = None) -> None:
        self._graph.send_draft(message_id, to)
        raise ConnectionError("crashed right after sending")


class CrashAfterDraft(CrashAfterSend):
    """Saves the draft in Graph, then breaks off before ARGUS can record its ID."""

    def create_digest(self, subject: str, html: str) -> str:
        self._graph.create_digest(subject, html)
        raise ConnectionError("crashed right after saving the draft")


@pytest.mark.req("EX-03", "NF-01")
def test_a_crash_after_sending_does_not_send_twice(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    graph = client(fake)

    with pytest.raises(ConnectionError):
        send_daily_digest(app, CrashAfterSend(graph), APP_URL, today=day)
    again = send_daily_digest(app, graph, APP_URL, today=day)

    assert again.outcome == FOUND_SENT
    assert len(mailbox(fake).sent) == 1
    assert digest_events(app, day)[-1][1]["outcome"] == FOUND_SENT


@pytest.mark.req("EX-03", "NF-01")
def test_a_draft_saved_before_a_crash_is_never_sent(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    graph = client(fake)

    with pytest.raises(ConnectionError):
        send_daily_digest(app, CrashAfterDraft(graph), APP_URL, today=day)
    unrecorded = set(mailbox(fake).drafts)
    again = send_daily_digest(app, graph, APP_URL, today=day)

    assert again.outcome == SENT_NOW
    assert again.draft_message_id not in unrecorded
    sent = mailbox(fake).sent
    assert len(sent) == 1
    assert sent[0].id == again.draft_message_id


def test_a_gone_draft_sends_nothing_and_fails_the_run(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    graph = client(fake)
    with pytest.raises(ConnectionError):
        send_daily_digest(app, CrashAfterSend(_NoSend(graph)), APP_URL, today=day)
    (draft_id,) = mailbox(fake).drafts
    TestClient(fake).delete(f"/_standin/drafts/{draft_id}")

    result = send_daily_digest(app, graph, APP_URL, today=day)

    assert result.outcome == DRAFT_GONE
    assert not result.ok
    assert mailbox(fake).sent == []
    # The day is finished: a later run that day sends nothing either.
    assert send_daily_digest(app, graph, APP_URL, today=day).outcome == "already_finished"


@pytest.mark.req("EX-03", "NF-06")
def test_a_changed_draft_is_not_sent(app: Engine, fake: FastAPI) -> None:
    open_audit_exceptions(app)
    day = next_day()
    graph = client(fake)
    with pytest.raises(ConnectionError):
        send_daily_digest(app, CrashAfterSend(_NoSend(graph)), APP_URL, today=day)
    (draft_id,) = mailbox(fake).drafts
    TestClient(fake).patch(
        f"/_standin/drafts/{draft_id}", json={"cc": ["someone@elsewhere.example"]}
    )

    result = send_daily_digest(app, graph, APP_URL, today=day)

    assert result.outcome == DRAFT_CHANGED
    assert not result.ok
    assert mailbox(fake).sent == []


class _NoSend:
    """A Graph client that saves drafts but never sends, to leave a recorded draft behind."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph
        self.digest_to = graph.digest_to

    def create_digest(self, subject: str, html: str) -> str:
        return self._graph.create_digest(subject, html)

    def is_draft(self, message_id: str) -> bool:
        return self._graph.is_draft(message_id)

    def send_draft(self, message_id: str, to: str | None = None) -> None:
        pass


# The job


@pytest.mark.req("EX-03", "NF-01")
def test_the_digest_job_sends_once_and_logs_ok(
    app: Engine, fake: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    open_audit_exceptions(app)

    def build(environ: Any) -> DigestServices:
        return DigestServices(app, client(fake), APP_URL)

    with caplog.at_level("INFO"):
        assert main(["digest"], environ={}, build_digest=build) == 0
        assert main(["digest"], environ={}, build_digest=build) == 0

    assert len(mailbox(fake).sent) == 1
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("argus-job")]
    assert lines == ["argus-job ok digest", "argus-job ok digest"]
    assert "Erika" not in caplog.text and DIGEST_TO not in caplog.text

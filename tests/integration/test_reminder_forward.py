"""Payment reminders go to Benjamin once, with the original attachment (#27, ADR 0003).

Runs the forward against the fake Graph and a real PostgreSQL. The groups below follow the
acceptance criteria of #27. Covers IN-05, PY-06, NF-01 and NF-03.

The emails are picked up by the real sweep steps of #24 and then moved to ``classified`` the
way the sweep records them since #91, so the forward starts from the same record it gets in
production.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.cases.model import Actor, CaseKind, CaseStatus, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    change_case,
    create_case,
    get_case,
    move_case,
    record_event,
    resolve_exception,
    trace,
    unit_of_work,
    verify_chain,
)
from argus.documents.email import attachments_of
from argus.documents.store import MemoryDocumentStore
from argus.intake.graph import GraphCallRefused, GraphClient
from argus.intake.records import EmailRecord, EmailStep, get_email, mark_classified
from argus.intake.reminders import (
    FORWARD_ACTOR,
    ForwardRefused,
    forward_reminder,
)
from argus.intake.sweep import SWEEP_ACTOR, DocumentArchive, see, store
from argus.sorting import Attachment, Category, EmailToSort, Route, SortingDecision
from standins.graph.app import MAILBOX, Mailbox, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport
from tests.unit.sorting_helpers import EMAILS, decision_for, read_fixture_email

# Synthetic, like every address in the fixtures. The real one is a setting (IN-05).
FORWARD_TO = "benjamin@maxasp.example"
REMINDERS = ["E25", "E26", "E27", "E28", "E29", "E30", "E31"]
INTAKE = Actor.system("intake")
APPROVER = Actor.user("approver-entra-id")
DECISION = SortingDecision(
    route=Route.FORWARD_REMINDER,
    category=Category.PAYMENT_REMINDER,
    reasons=(),
    model_category=Category.PAYMENT_REMINDER,
    model_confidence=0.97,
    check_category=Category.PAYMENT_REMINDER,
    evidence=(),
)
_numbers = count(1)


def fixture_path(email_id: str) -> Path:
    return next(EMAILS.glob(f"{email_id}_*.eml"))


@pytest.fixture
def clean(owner: Engine) -> Iterator[None]:
    """No forwards and no email records before and after each test. Both cannot be deleted, so
    the owner turns the guards off for this cleanup only. The audit log stays."""

    def wipe() -> None:
        with owner.begin() as connection:
            # Stored documents point at the records and are append only. The replica role lets
            # this test cleanup leave them behind; record IDs are never reused (#90).
            connection.execute(text("set local session_replication_role = replica"))
            for table, trigger in (
                ("reminder_forward", "reminder_forward_no_delete"),
                ("mailbox_email", "mailbox_email_no_delete"),
            ):
                connection.execute(text(f"alter table {table} disable trigger {trigger}"))
                connection.execute(text(f"delete from {table}"))  # noqa: S608  fixed names
                connection.execute(text(f"alter table {table} enable trigger {trigger}"))

    wipe()
    yield
    wipe()


def fake_graph_with(*email_ids: str) -> FastAPI:
    return create_app(token=TOKEN, emails=[fixture_path(e).read_bytes() for e in email_ids])


def graph_for(fake: FastAPI, forward_to: str | None = FORWARD_TO) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        sleep=lambda _: None,
        forward_to=forward_to,
    )


def mailbox_of(fake: FastAPI) -> Mailbox:
    box: Mailbox = fake.state.mailbox
    return box


def classified(app: Engine, graph: GraphClient) -> EmailRecord:
    """The first Inbox email, seen and stored by the sweep of #24 and classified as a payment
    reminder the way the sweep records it since #91."""
    message = next(graph.list_inbox())
    record, _ = see(app, message)
    store(app, graph, DocumentArchive(MemoryDocumentStore()), record)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert mark_classified(db, record.id, decision_for(Route.FORWARD_REMINDER))
        sorted_record = get_email(db, message.id)
    assert sorted_record is not None
    return sorted_record


def email_events(app: Engine, email_id: int) -> list[tuple[str, dict[str, Any]]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select event_type, details from audit_event"
                " where email_id = :id and case_id is null order by chain_seq"
            ),
            {"id": email_id},
        ).all()
    return [(r[0], r[1]) for r in rows]


def new_invoice(app: Engine, number: str, kind: CaseKind = CaseKind.INCOMING_INVOICE) -> int:
    with unit_of_work(app, INTAKE) as db:
        return create_case(
            db,
            kind,
            source_email_id=1_000_000 + next(_numbers),
            partner_account="70001",
            document_number=number,
            gross_cents=21240,
            currency="EUR",
        )


def reminder_received_at(app: Engine, case_id: int) -> datetime | None:
    with app.connect() as connection:
        value: datetime | None = connection.execute(
            text("select reminder_received_at from invoice_case where id = :id"), {"id": case_id}
        ).scalar_one()
    return value


def unique_number() -> str:
    return f"RE-27-{next(_numbers):05d}"


def reminder_naming(number: str, where: str = "body") -> EmailToSort:
    """E30, a reminder in the mail text, rewritten to name ``number`` in one place only."""
    base = read_fixture_email(fixture_path("E30"))
    text_with_number = f"Die Rechnung {number} vom 07.09.2026 ist noch offen."
    if where == "subject":
        return replace(base, subject=f"Zahlungserinnerung {number}", body="Bitte zahlen.")
    if where == "attachment":
        letter = Attachment("Mahnung.pdf", "application/pdf", text_with_number)
        return replace(base, subject="Mahnung", body="Siehe Anhang.", attachments=(letter,))
    return replace(base, subject="Zahlungserinnerung", body=text_with_number)


@dataclass
class CrashingMailbox:
    """The real Graph client, made to stop at one point of the forward like a crash would."""

    graph: GraphClient
    crash: str = ""

    def _maybe_crash(self, point: str) -> None:
        if self.crash == point:
            self.crash = ""
            raise RuntimeError(f"crash {point}")

    def create_forward(self, message_id: str, comment: str) -> str:
        draft = self.graph.create_forward(message_id, comment)
        self._maybe_crash("after saving the draft, before recording it")
        return draft

    def is_draft(self, message_id: str) -> bool:
        self._maybe_crash("after recording the draft, before sending it")
        return self.graph.is_draft(message_id)

    def send_draft(self, message_id: str) -> None:
        self.graph.send_draft(message_id)
        self._maybe_crash("after sending, before recording it")


# 1. A reminder fixture is forwarded to Benjamin's address with the original attachment


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_a_reminder_is_forwarded_to_benjamin_with_its_original_attachment(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)

    result = forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)

    box = mailbox_of(fake)
    assert result.sent_now
    assert len(box.sent) == 1
    forward = box.sent[0]
    assert forward.to == [FORWARD_TO]
    assert forward.original_id == email.graph_message_id
    original = attachments_of(fixture_path("E25").read_bytes())
    assert [data for _, data in forward.attachments] == [a.data for a in original]
    with app.connect() as connection:
        routed = get_email(connection, email.graph_message_id)
        # With #26's migration 0006 in place, a routed email must carry its route.
        route: str = connection.execute(
            text("select route from mailbox_email where id = :id"), {"id": email.id}
        ).scalar_one()
    assert routed is not None and routed.step is EmailStep.ROUTED
    assert route == Route.FORWARD_REMINDER.value


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("email_id", REMINDERS)
def test_every_reminder_fixture_goes_out_once_with_all_its_attachments(
    app: Engine, email_id: str
) -> None:
    fake = fake_graph_with(email_id)
    graph = graph_for(fake)
    email = classified(app, graph)

    forward_reminder(app, graph, email, read_fixture_email(fixture_path(email_id)), DECISION)

    sent = mailbox_of(fake).sent
    original = attachments_of(fixture_path(email_id).read_bytes())
    assert len(sent) == 1
    assert sent[0].to == [FORWARD_TO]
    assert [data for _, data in sent[0].attachments] == [a.data for a in original]


# 2. A crash between draft and send never sends two forwards


@pytest.mark.req("IN-05", "NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    "crash",
    [
        "after saving the draft, before recording it",
        "after recording the draft, before sending it",
        "after sending, before recording it",
    ],
)
def test_a_crash_at_any_point_never_sends_two_forwards(app: Engine, crash: str) -> None:
    fake = fake_graph_with("E26")
    mailbox = CrashingMailbox(graph_for(fake), crash)
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E26"))

    with pytest.raises(RuntimeError, match="crash"):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    forward_reminder(app, mailbox, email, reminder, DECISION)  # the next sweep
    forward_reminder(app, mailbox, email, reminder, DECISION)  # and the one after

    box = mailbox_of(fake)
    assert len(box.sent) == 1
    events = [event for event, _ in email_events(app, email.id)]
    assert events.count("reminder_forward_drafted") == 1
    assert events.count("reminder_forwarded") == 1
    assert events.count("email_routed") == 1
    with app.connect() as connection:
        recorded: str = connection.execute(
            text("select draft_message_id from reminder_forward where email_id = :id"),
            {"id": email.id},
        ).scalar_one()
    assert box.sent[0].id == recorded


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_a_draft_saved_but_not_recorded_is_never_sent(app: Engine) -> None:
    fake = fake_graph_with("E31")
    mailbox = CrashingMailbox(graph_for(fake), "after saving the draft, before recording it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E31"))

    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    forward_reminder(app, mailbox, email, reminder, DECISION)

    box = mailbox_of(fake)
    assert len(box.drafts) == 2  # the lost one stays in Drafts
    assert [d.id for d in box.drafts.values() if not d.is_draft] == [box.sent[0].id]


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_after_a_crash_past_sending_the_next_run_records_without_sending(app: Engine) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after sending, before recording it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E25"))

    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    result = forward_reminder(app, mailbox, email, reminder, DECISION)

    assert not result.sent_now
    assert len(mailbox_of(fake).sent) == 1
    assert forwarded_details(app, email.id)["send_outcome"] == "found_sent"


def forwarded_details(app: Engine, email_id: int) -> dict[str, Any]:
    return next(d for e, d in email_events(app, email_id) if e == "reminder_forwarded")


def recorded_draft(app: Engine, email_id: int) -> tuple[str, str]:
    with app.connect() as connection:
        row = connection.execute(
            text("select draft_message_id, step from reminder_forward where email_id = :id"),
            {"id": email_id},
        ).one()
    return row[0], row[1]


@pytest.mark.req("IN-05", "NF-06")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    "change",
    [
        {"cc": ["someone@example.test"]},
        {"bcc": ["someone@example.test"]},
        {"to": ["someone@example.test"]},
        {"to": [FORWARD_TO, "someone@example.test"]},
        {"to": []},
    ],
    ids=["cc added", "bcc added", "other recipient", "second recipient", "no recipient"],
)
def test_a_draft_changed_before_sending_is_not_sent(app: Engine, change: dict[str, Any]) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E25"))
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    draft_id, _ = recorded_draft(app, email.id)
    # A person with access to the shared mailbox changes the draft before the next sweep.
    TestClient(fake).patch(f"/_standin/drafts/{draft_id}", json=change)

    result = forward_reminder(app, mailbox, email, reminder, DECISION)

    assert mailbox_of(fake).sent == []
    assert recorded_draft(app, email.id) == (draft_id, "drafted")
    assert "reminder_forwarded" not in [e for e, _ in email_events(app, email.id)]
    assert open_forward_exceptions(app, email.id) == [(result.exception_id, "draft_changed")]


def open_forward_exceptions(app: Engine, email_id: int) -> list[tuple[int, str]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select id, facts ->> 'problem' from exception_item where email_id = :id"
                " and reason = 'forward_failed' and resolved_at is null order by id"
            ),
            {"id": email_id},
        ).all()
    return [(r[0], r[1]) for r in rows]


@pytest.mark.req("IN-05", "EX-01")
@pytest.mark.usefixtures("clean")
def test_a_recorded_draft_that_is_gone_opens_an_exception_and_sends_nothing(
    app: Engine,
) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E25"))
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    draft_id, _ = recorded_draft(app, email.id)
    TestClient(fake).delete(f"/_standin/drafts/{draft_id}")  # a person deleted it

    result = forward_reminder(app, mailbox, email, reminder, DECISION)
    again = forward_reminder(app, mailbox, email, reminder, DECISION)  # the next sweep

    box = mailbox_of(fake)
    assert box.sent == []
    assert list(box.drafts) == []  # no second draft saved on its own
    assert result.exception_id is not None
    assert again.exception_id == result.exception_id  # one exception, not one per sweep
    assert open_forward_exceptions(app, email.id) == [(result.exception_id, "draft_gone")]
    assert recorded_draft(app, email.id) == (draft_id, "drafted")
    with app.connect() as connection:
        step: str = connection.execute(
            text("select step from mailbox_email where id = :id"), {"id": email.id}
        ).scalar_one()
        opened: dict[str, Any] = connection.execute(
            text(
                "select details from audit_event where exception_id = :id"
                " and event_type = 'exception_opened'"
            ),
            {"id": result.exception_id},
        ).scalar_one()
    assert step == "classified"
    assert FORWARD_TO not in json.dumps(opened)


@pytest.mark.req("IN-05", "PY-06", "EX-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_after_a_person_resolves_a_gone_draft_the_email_is_routed_without_sending(
    app: Engine,
) -> None:
    fake = fake_graph_with("E30")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    number = unique_number()
    case_id = new_invoice(app, number)
    reminder = reminder_naming(number)
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    draft_id, _ = recorded_draft(app, email.id)
    TestClient(fake).delete(f"/_standin/drafts/{draft_id}")
    waiting = forward_reminder(app, mailbox, email, reminder, DECISION)
    assert waiting.exception_id is not None
    # The person forwards the reminder by hand and resolves the exception.
    with unit_of_work(app, Actor.user("benjamin-entra-id")) as db:
        resolve_exception(db, waiting.exception_id, Outcome.ANSWERED)

    result = forward_reminder(app, mailbox, email, reminder, DECISION)
    again = forward_reminder(app, mailbox, email, reminder, DECISION)

    assert result.exception_id is None and not result.sent_now
    assert result.match.case_ids == (case_id,)
    assert again.exception_id is None
    assert mailbox_of(fake).sent == []
    assert list(mailbox_of(fake).drafts) == []
    assert open_forward_exceptions(app, email.id) == []
    assert recorded_draft(app, email.id) == (draft_id, "sent")
    assert forwarded_details(app, email.id)["send_outcome"] == "by_person"
    assert reminder_received_at(app, case_id) is not None
    with app.connect() as connection:
        routed = connection.execute(
            text("select step, route from mailbox_email where id = :id"), {"id": email.id}
        ).one()
        exceptions: int = connection.execute(
            text("select count(*) from exception_item where email_id = :id"), {"id": email.id}
        ).scalar_one()
    assert tuple(routed) == ("routed", Route.FORWARD_REMINDER.value)
    assert exceptions == 1


@pytest.mark.req("IN-05", "EX-01")
@pytest.mark.usefixtures("clean")
def test_after_a_person_resolves_a_changed_draft_argus_sends_it_once(app: Engine) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E25"))
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    draft_id, _ = recorded_draft(app, email.id)
    standin = TestClient(fake)
    standin.patch(f"/_standin/drafts/{draft_id}", json={"cc": ["someone@example.test"]})
    waiting = forward_reminder(app, mailbox, email, reminder, DECISION)
    assert waiting.exception_id is not None

    standin.patch(f"/_standin/drafts/{draft_id}", json={"cc": []})  # the person fixes it
    with unit_of_work(app, Actor.user("benjamin-entra-id")) as db:
        resolve_exception(db, waiting.exception_id, Outcome.DATA_CORRECTED)
    result = forward_reminder(app, mailbox, email, reminder, DECISION)

    assert result.sent_now and result.exception_id is None
    assert len(mailbox_of(fake).sent) == 1
    assert mailbox_of(fake).sent[0].cc == []


@pytest.mark.req("IN-05", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_draft_a_person_sent_is_logged_as_found_sent_not_as_sent_by_argus(app: Engine) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    reminder = read_fixture_email(fixture_path("E25"))
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, reminder, DECISION)
    draft_id, _ = recorded_draft(app, email.id)
    TestClient(fake).post(f"/_standin/drafts/{draft_id}/send")  # sent from Outlook

    result = forward_reminder(app, mailbox, email, reminder, DECISION)

    assert not result.sent_now
    assert len(mailbox_of(fake).sent) == 1  # the person's send, none from ARGUS
    assert forwarded_details(app, email.id)["send_outcome"] == "found_sent"


@pytest.mark.req("IN-05", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_draft_argus_sent_is_logged_as_sent(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)

    result = forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)

    assert result.sent_now
    assert forwarded_details(app, email.id)["send_outcome"] == "sent"


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_a_sent_forward_needs_its_outcome(app: Engine) -> None:
    fake = fake_graph_with("E25")
    mailbox = CrashingMailbox(graph_for(fake), "after recording the draft, before sending it")
    email = classified(app, mailbox.graph)
    with pytest.raises(RuntimeError):
        forward_reminder(app, mailbox, email, read_fixture_email(fixture_path("E25")), DECISION)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, FORWARD_ACTOR) as db:
        db.execute(
            text(
                "update reminder_forward set step = 'sent', matched_case_ids = '{}',"
                " unclear_numbers = 0 where email_id = :id"
            ),
            {"id": email.id},
        )


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_a_sent_draft_cannot_be_sent_again(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)
    forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)
    sent_id = mailbox_of(fake).sent[0].id

    # ARGUS refuses before asking Graph, and Graph would refuse too.
    with pytest.raises(GraphCallRefused, match="not a draft"):
        graph.send_draft(sent_id)
    answer = TestClient(fake).post(
        f"/v1.0/users/{MAILBOX}/messages/{sent_id}/send",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert answer.status_code == 400
    assert len(mailbox_of(fake).sent) == 1


# 3. A reminder that matches an open invoice case sets reminder_received_at on that case


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
def test_the_reminder_fixture_marks_its_open_invoice_for_the_next_run(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)
    case_id = new_invoice(app, "BB-2026-1101")  # named in E25's subject and letter
    other = new_invoice(app, unique_number())

    result = forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)

    with app.connect() as connection:
        received: datetime = connection.execute(
            text("select received_at from mailbox_email where id = :id"), {"id": email.id}
        ).scalar_one()
    assert result.match.case_ids == (case_id,)
    assert reminder_received_at(app, case_id) == received
    assert reminder_received_at(app, other) is None
    with unit_of_work(app, INTAKE) as db:
        changes = [e for e in trace(db, case_id) if e.event_type == "case_changed"]
    assert changes[-1].actor == str(FORWARD_ACTOR)
    assert "reminder_received_at" in json.dumps(changes[-1].details)


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("where", ["subject", "body", "attachment"])
def test_the_invoice_number_counts_wherever_the_reminder_names_it(app: Engine, where: str) -> None:
    fake = fake_graph_with("E30")
    graph = graph_for(fake)
    email = classified(app, graph)
    number = unique_number()
    case_id = new_invoice(app, number)

    result = forward_reminder(app, graph, email, reminder_naming(number, where), DECISION)

    assert result.match.case_ids == (case_id,)
    assert reminder_received_at(app, case_id) is not None


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
def test_a_number_two_open_invoices_carry_marks_neither(app: Engine) -> None:
    fake = fake_graph_with("E30")
    graph = graph_for(fake)
    email = classified(app, graph)
    number = unique_number()
    first, second = new_invoice(app, number), new_invoice(app, number)

    result = forward_reminder(app, graph, email, reminder_naming(number), DECISION)

    assert result.match.case_ids == ()
    assert result.match.unclear_numbers == 1
    assert reminder_received_at(app, first) is None
    assert reminder_received_at(app, second) is None
    assert len(mailbox_of(fake).sent) == 1  # Benjamin still gets the reminder


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
def test_part_of_a_longer_number_is_no_match(app: Engine) -> None:
    fake = fake_graph_with("E30")
    graph = graph_for(fake)
    email = classified(app, graph)
    number = unique_number()
    shorter = new_invoice(app, number[:-1])

    result = forward_reminder(app, graph, email, reminder_naming(number), DECISION)

    assert result.match.case_ids == ()
    assert reminder_received_at(app, shorter) is None


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
def test_paid_invoices_and_our_own_invoices_are_not_marked(app: Engine) -> None:
    fake = fake_graph_with("E30")
    graph = graph_for(fake)
    email = classified(app, graph)
    number = unique_number()
    paid = new_invoice(app, number)
    for to in (
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.APPROVED,
        CaseStatus.BOOKED,
        CaseStatus.IN_PAYMENT_RUN,
        CaseStatus.PAID,
    ):
        with unit_of_work(app, APPROVER if to is CaseStatus.APPROVED else INTAKE) as db:
            case = get_case(db, paid)
            ref = f"DATEV-27-{paid}" if to is CaseStatus.BOOKED else None
            assert move_case(db, paid, expected=case.status, to=to, datev_booking_ref=ref)
    outgoing = new_invoice(app, number, CaseKind.OUTGOING_INVOICE)

    result = forward_reminder(app, graph, email, reminder_naming(number), DECISION)

    assert result.match.case_ids == ()
    assert reminder_received_at(app, paid) is None
    assert reminder_received_at(app, outgoing) is None


@pytest.mark.req("PY-06")
@pytest.mark.usefixtures("clean")
def test_a_later_reminder_keeps_the_date_of_the_first(app: Engine) -> None:
    fake = fake_graph_with("E30")
    graph = graph_for(fake)
    email = classified(app, graph)
    number = unique_number()
    case_id = new_invoice(app, number)
    first = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    with unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, reminder_received_at=first)

    result = forward_reminder(app, graph, email, reminder_naming(number), DECISION)

    assert result.match.case_ids == (case_id,)
    assert reminder_received_at(app, case_id) == first


# 4. The forward is written to the audit log


@pytest.mark.req("IN-05", "NF-01", "NF-03")
@pytest.mark.usefixtures("clean")
def test_the_forward_is_in_the_audit_log_with_ids_only(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)
    case_id = new_invoice(app, unique_number())
    reminder = reminder_naming(get_number(app, case_id))

    forward_reminder(app, graph, email, reminder, DECISION)

    events = [e for e in email_events(app, email.id) if not e[0].startswith("document_")]
    assert [e for e, _ in events] == [
        "email_seen",
        "email_stored",
        "email_classified",
        "reminder_forward_drafted",
        "reminder_forwarded",
        "email_routed",
    ]
    draft_id = mailbox_of(fake).sent[0].id
    assert events[3][1] == {"draft_message_id": draft_id}
    assert events[4][1] == {
        "draft_message_id": draft_id,
        "matched_case_ids": [case_id],
        "unclear_numbers": 0,
        "send_outcome": "sent",
    }
    written = json.dumps([d for _, d in events])
    assert FORWARD_TO not in written
    assert "muster" not in written.lower()  # no sender or vendor name
    with app.connect() as connection:
        actors: Iterator[str] = connection.execute(
            text(
                "select distinct actor from audit_event where email_id = :id"
                " and event_type like 'reminder%'"
            ),
            {"id": email.id},
        ).scalars()
        assert set(actors) == {FORWARD_ACTOR.name}
        assert verify_chain(connection) is None


def get_number(app: Engine, case_id: int) -> str:
    with unit_of_work(app, INTAKE) as db:
        number = get_case(db, case_id).document_number
    assert number is not None
    return number


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("event_type", ["reminder_forwarded", "reminder_forward_drafted"])
def test_the_app_cannot_write_forward_events_itself(app: Engine, event_type: str) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, FORWARD_ACTOR) as db,
    ):
        record_event(db, event_type, email_id=1, details={})


@pytest.mark.req("IN-05", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_forward_record_cannot_be_deleted_or_sent_back_to_draft(
    app: Engine, owner: Engine
) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)
    forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)

    with pytest.raises(NotAllowed), unit_of_work(app, FORWARD_ACTOR) as db:
        db.execute(text("delete from reminder_forward where email_id = :id"), {"id": email.id})
    with (
        pytest.raises(CaseRuleBroken, match="already sent"),
        unit_of_work(app, FORWARD_ACTOR) as db,
    ):
        db.execute(
            text(
                "update reminder_forward set step = 'drafted', matched_case_ids = null,"
                " unclear_numbers = null where email_id = :id"
            ),
            {"id": email.id},
        )
    with (
        pytest.raises(CaseRuleBroken, match="cannot be deleted"),
        unit_of_work(owner, FORWARD_ACTOR) as db,
    ):
        db.execute(text("delete from reminder_forward where email_id = :id"), {"id": email.id})


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_a_second_draft_for_the_same_email_is_refused(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)
    forward_reminder(app, graph, email, read_fixture_email(fixture_path("E25")), DECISION)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, FORWARD_ACTOR) as db:
        db.execute(
            text("insert into reminder_forward (email_id, draft_message_id) values (:id, 'X')"),
            {"id": email.id},
        )


# Only sorted reminders are forwarded


@pytest.mark.req("IN-05", "IN-09")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("route", [r for r in Route if r is not Route.FORWARD_REMINDER])
def test_an_email_with_another_route_is_not_forwarded(app: Engine, route: Route) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    email = classified(app, graph)

    with pytest.raises(ForwardRefused):
        forward_reminder(
            app,
            graph,
            email,
            read_fixture_email(fixture_path("E25")),
            replace(DECISION, route=route),
        )
    assert mailbox_of(fake).drafts == {}


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean")
def test_an_email_that_is_not_sorted_yet_is_not_forwarded(app: Engine) -> None:
    fake = fake_graph_with("E25")
    graph = graph_for(fake)
    message = next(graph.list_inbox())
    record, _ = see(app, message)
    store(app, graph, DocumentArchive(MemoryDocumentStore()), record)
    with app.connect() as connection:
        stored = get_email(connection, message.id)
    assert stored is not None

    with pytest.raises(ForwardRefused):
        forward_reminder(app, graph, stored, read_fixture_email(fixture_path("E25")), DECISION)
    with (
        pytest.raises(CaseRuleBroken, match="not sorted yet"),
        unit_of_work(app, FORWARD_ACTOR) as db,
    ):
        db.execute(
            text("insert into reminder_forward (email_id, draft_message_id) values (:id, 'X')"),
            {"id": stored.id},
        )
    assert mailbox_of(fake).drafts == {}

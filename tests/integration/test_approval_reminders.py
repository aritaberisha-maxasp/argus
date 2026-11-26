"""Reminders after 3 working days, escalations after 5, and a new request when the person asked
can no longer decide (#40), against a real PostgreSQL with the rights of argus_app and the fake
Graph.

Covers AP-05 (reminder and escalation on Baden-Württemberg working days), AP-06 (the deputy
while the approver is away), AP-08 (only the person on the newest request decides, also after
an escalation), NF-01 (every reminder, escalation and new request is in the audit log and never
sent twice) and NF-03 (only the approval client sends them). Requests are emailed for real by
the pass, then their sent time is moved back to a known day, so the working days can be
checked against a fixed calendar. Names and addresses are made up.
"""

import itertools
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.approvers.table import ChangedBy, save_approver, set_absent, set_vendor_approver
from argus.cases.model import Actor, CaseKind, CaseStatus
from argus.cases.store import CaseRuleBroken, change_case, create_case, get_case, unit_of_work
from argus.intake.approval_reminders import due_dates, stored_nudges
from argus.intake.approvals import Approvals, request_approvals, stored_requests
from argus.intake.graph import GraphApiError, GraphCall, GraphCallRefused, GraphClient, NudgeMail
from argus.intake.mode import IntakeMode
from argus.invoices_page.views import case_row
from argus.queue_page.views import open_exception_detail
from standins.graph.app import MAILBOX, create_app
from tests.integration.approval_helpers import decide
from tests.integration.conftest import set_mode
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

pytestmark = pytest.mark.req("AP-05")

BERLIN = ZoneInfo("Europe/Berlin")
INTAKE = Actor.system("intake")
JOB = Actor.system("approval-request")
ADMIN = ChangedBy(Actor.user("44444444-4444-4444-8444-444444444444"), "Arita Test")
APP_URL = "https://argus.example"
DOMAIN = "maxasp-test.example"
FINANCE = "finance@maxasp-test.example"

# Vendor accounts nobody else uses.
_numbers = itertools.count(860_000_000 + uuid.uuid4().int % 1_000_000 * 100)

# A Monday with no holiday near it, and the days the rule gives for it.
MONDAY = datetime(2026, 11, 16, 9, 30, tzinfo=BERLIN)


def at(day: date, hour: int = 8) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN)


# The rule on its own (note from #22 on #40)


def test_a_request_sent_on_monday_is_reminded_on_thursday_and_escalated_next_monday() -> None:
    assert due_dates(MONDAY) == (date(2026, 11, 19), date(2026, 11, 23))


def test_a_request_sent_on_a_weekend_starts_counting_on_monday() -> None:
    saturday = datetime(2026, 11, 14, 11, 0, tzinfo=BERLIN)
    assert due_dates(saturday) == (date(2026, 11, 18), date(2026, 11, 20))


def test_a_request_sent_on_a_holiday_starts_counting_the_next_working_day() -> None:
    # Ascension Day, Thursday 14 May 2026, is a holiday in Baden-Württemberg.
    ascension = datetime(2026, 5, 14, 10, 0, tzinfo=BERLIN)
    assert due_dates(ascension) == (date(2026, 5, 19), date(2026, 5, 21))


def test_late_on_friday_utc_already_counts_as_the_berlin_saturday() -> None:
    late = datetime(2026, 11, 13, 23, 30, tzinfo=UTC)  # 00:30 on Saturday in Berlin
    assert due_dates(late) == (date(2026, 11, 18), date(2026, 11, 20))


# Fixtures and helpers


@pytest.fixture
def fake() -> FastAPI:
    return create_app(token=TOKEN)


def _client(app: Engine, fake: FastAPI) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        calls=frozenset({GraphCall.SEND_APPROVAL_REQUEST}),
        approval_lookup=stored_requests(app, APP_URL),
        approval_domain=DOMAIN,
        nudge_lookup=stored_nudges(app, APP_URL),
    )


@pytest.fixture
def approvals(app: Engine, fake: FastAPI) -> Approvals:
    client = _client(app, fake)
    return Approvals(client, APP_URL, DOMAIN, nudges=client, escalate_to=FINANCE)


def mails(fake: FastAPI) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = fake.state.mailbox.notices
    return found


def to(mail: dict[str, Any]) -> str:
    address: str = mail["toRecipients"][0]["emailAddress"]["address"]
    return address


def person(app: Engine, name: str) -> tuple[str, str]:
    object_id = str(uuid.uuid4())
    address = f"{name.split()[0].lower()}.{object_id[:8]}@{DOMAIN}"
    save_approver(app, ADMIN, object_id, name, address)
    return object_id, address


def vendor(app: Engine, *, deputy: bool = False) -> tuple[str, str, str | None]:
    account = str(next(_numbers))
    approver, _ = person(app, "Alice Approver")
    deputy_id = person(app, "Dora Deputy")[0] if deputy else None
    set_vendor_approver(app, ADMIN, account, approver, deputy_id, False)
    return account, approver, deputy_id


def propose(app: Engine, case_id: int, account: str) -> None:
    with unit_of_work(app, Actor.system("account-proposal")) as db:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, from_document_number, from_posting_date)"
                " values (:id, :account, '4930', '9', 'SKR03', 'booking_history', 'RE-1',"
                " date '2026-03-01')"
            ),
            {"id": case_id, "account": account},
        )


def waiting_case(app: Engine, approvals: Approvals, account: str) -> int:
    """A checked case, sent for approval and emailed by the pass."""
    number = f"RM-{uuid.uuid4().hex[:8]}"
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            partner_account=account,
            document_number=number,
            gross_cents=500000,
            currency="EUR",
            duplicate_key=f"dup-{number}",
        )
    propose(app, case_id, account)
    first = request_approvals(app, approvals, cases=[case_id], now=lambda: MONDAY)
    assert (first.requested, first.sent) == (1, 1)
    return case_id


def move_clock(owner: Engine, request_id: int, sent_at: datetime, column: str = "sent_at") -> None:
    """Move a request's sent time (or clock_from) back to a known day. The database never lets
    anyone change it, so this goes around its triggers as the owner, for the test only."""
    with owner.begin() as db:
        db.execute(text("set local session_replication_role = replica"))
        db.execute(
            text(f"update approval_request set {column} = :at where id = :id"),  # noqa: S608
            {"at": sent_at, "id": request_id},
        )


def requests_of(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text("select * from approval_request where case_id = :id order by id"),
            {"id": case_id},
        ).mappings()
        return [dict(r) for r in rows]


def nudges_of(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text("select * from approval_nudge where case_id = :id order by id"), {"id": case_id}
        ).mappings()
        return [dict(r) for r in rows]


def events_of(app: Engine, case_id: int, *types: str) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text(
                "select event_type, actor_type, actor, details from audit_event"
                " where case_id = :id and event_type = any(:types) order by chain_seq"
            ),
            {"id": case_id, "types": list(types)},
        ).mappings()
        return [dict(r) for r in rows]


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with app.connect() as db:
        return get_case(db, case_id).status


def open_reasons(app: Engine, case_id: int) -> list[str]:
    with app.connect() as db:
        return list(
            db.execute(
                text(
                    "select reason::text from exception_item"
                    " where case_id = :id and resolved_at is null order by id"
                ),
                {"id": case_id},
            ).scalars()
        )


def run(app: Engine, approvals: Approvals, case_id: int, day: date) -> Any:
    return request_approvals(app, approvals, cases=[case_id], now=lambda: at(day))


@pytest.fixture
def clean_mails(fake: FastAPI) -> Iterator[None]:
    yield
    fake.state.mailbox.notices.clear()


# Reminder and escalation


@pytest.mark.usefixtures("live")
def test_the_reminder_comes_on_thursday_and_the_escalation_next_monday_each_once(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    (request,) = requests_of(app, case_id)
    move_clock(owner, request["id"], MONDAY)
    mails(fake).clear()

    wednesday = run(app, approvals, case_id, date(2026, 11, 18))
    assert (wednesday.reminded, wednesday.escalated) == (0, 0) and mails(fake) == []

    thursday = run(app, approvals, case_id, date(2026, 11, 19))
    again = run(app, approvals, case_id, date(2026, 11, 20))
    assert (thursday.reminded, again.reminded, again.escalated) == (1, 0, 0)
    (reminder,) = mails(fake)
    assert to(reminder) == request["recipient_address"]
    body = reminder["body"]["content"]
    assert "waited for your approval since 16.11.2026, 3 working days now" in body
    assert f"{APP_URL}/approvals/{case_id}" in body
    assert "Gross amount: 5,000.00 EUR" in body
    assert reminder["subject"].startswith("ARGUS reminder: please approve invoice")

    monday = run(app, approvals, case_id, date(2026, 11, 23))
    later = run(app, approvals, case_id, date(2026, 11, 25))
    assert (monday.escalated, later.escalated, later.reminded) == (1, 0, 0)
    assert len(mails(fake)) == 2
    escalation = mails(fake)[1]
    assert to(escalation) == FINANCE
    text_body = escalation["body"]["content"]
    assert "Alice Approver was asked on 16.11.2026" in text_body
    assert "Only Alice Approver can approve or reject it." in text_body
    assert f"{APP_URL}/approvals/{case_id}" in text_body

    rows = nudges_of(app, case_id)
    assert [(r["kind"], r["due_on"]) for r in rows] == [
        ("reminder", date(2026, 11, 19)),
        ("escalation", date(2026, 11, 23)),
    ]
    assert all(r["sent_at"] is not None and r["failed_at"] is None for r in rows)
    reminded, escalated = events_of(
        app, case_id, "approval_reminder_sent", "approval_escalation_sent"
    )
    assert reminded["event_type"] == "approval_reminder_sent"
    assert reminded["details"]["address"] == request["recipient_address"]
    assert reminded["details"]["request_id"] == request["id"]
    assert (reminded["actor_type"], reminded["actor"]) == ("system", "approval-request")
    assert escalated["details"]["address"] == FINANCE
    # Escalated: shown on the invoices page; the case still waits and only Alice decides.
    with app.connect() as db:
        row = case_row(db, case_id)
    assert row is not None and row.escalated_at is not None
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    with app.begin() as db:
        assert decide(db, case_id) is True
    with app.connect() as db:
        assert (
            db.execute(
                text("select decided_by from approval_decision where case_id = :id"),
                {"id": case_id},
            ).scalar_one()
            == f"user:{approver}"
        )


@pytest.mark.usefixtures("live")
def test_a_decided_invoice_gets_no_reminder(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    with app.begin() as db:
        decide(db, case_id)
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 25))

    assert (result.reminded, result.escalated) == (0, 0) and mails(fake) == []


@pytest.mark.usefixtures("live")
def test_the_clock_counts_from_clock_from_when_the_request_keeps_an_older_clock(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    # #192: a question the asker answered themselves keeps the clock of the old request in
    # clock_from; a normal answer leaves it empty, so the clock starts at the new sent time.
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    request_id = requests_of(app, case_id)[0]["id"]
    move_clock(owner, request_id, datetime(2026, 11, 23, 9, 0, tzinfo=BERLIN))
    move_clock(owner, request_id, MONDAY, column="clock_from")
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 23))

    assert (result.reminded, result.escalated) == (1, 1)
    assert {r["clock_from"] for r in nudges_of(app, case_id)} == {MONDAY}


@pytest.mark.usefixtures("shadow")
def test_nothing_goes_out_in_shadow_mode(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI, live_then_shadow: Any
) -> None:
    case_id, _ = live_then_shadow
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 25))

    assert (result.reminded, result.escalated) == (0, 0)
    assert mails(fake) == [] and nudges_of(app, case_id) == []


@pytest.fixture
def live_then_shadow(app: Engine, owner: Engine, approvals: Approvals) -> tuple[int, int]:
    set_mode(owner, IntakeMode.LIVE)
    try:
        account, _, _ = vendor(app)
        case_id = waiting_case(app, approvals, account)
        request_id = requests_of(app, case_id)[0]["id"]
        move_clock(owner, request_id, MONDAY)
    finally:
        set_mode(owner, IntakeMode.SHADOW)
    return case_id, request_id


@pytest.mark.usefixtures("live")
def test_no_escalation_without_the_address_but_the_reminder_still_goes(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    mails(fake).clear()
    no_finance = Approvals(approvals.mailbox, APP_URL, DOMAIN, nudges=approvals.nudges)
    outside = Approvals(
        approvals.mailbox, APP_URL, DOMAIN, nudges=approvals.nudges, escalate_to="b@evil.example"
    )

    first = run(app, no_finance, case_id, date(2026, 11, 25))
    second = run(app, outside, case_id, date(2026, 11, 25))

    assert (first.reminded, first.escalated, second.escalated) == (1, 0, 0)
    assert [to(m) for m in mails(fake)] == [requests_of(app, case_id)[0]["recipient_address"]]


# Never twice (NF-01)


class Flaky:
    """The approval client for reminders, failing as told: ``refuse`` times with an error
    answer from Graph, then ``unknown`` times after sending, as a timeout would."""

    def __init__(self, inner: Approvals, *, refuse: int = 0, unknown: int = 0) -> None:
        assert inner.nudges is not None
        self.inner = inner.nudges
        self.refuse = refuse
        self.unknown = unknown
        self.tries: list[NudgeMail] = []

    def send_approval_nudge(self, mail: NudgeMail) -> None:
        self.tries.append(mail)
        if self.refuse > 0:
            self.refuse -= 1
            raise GraphApiError("POST", "sendMail", 503, "ServiceUnavailable")
        self.inner.send_approval_nudge(mail)
        if self.unknown > 0:
            self.unknown -= 1
            raise httpx.ReadTimeout("the answer never came")


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("live")
def test_a_refused_reminder_is_sent_again_and_then_never_again(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    mails(fake).clear()
    flaky = Flaky(approvals, refuse=1)
    with_flaky = Approvals(approvals.mailbox, APP_URL, DOMAIN, nudges=flaky)

    first = run(app, with_flaky, case_id, date(2026, 11, 19))
    second = run(app, with_flaky, case_id, date(2026, 11, 19))
    third = run(app, with_flaky, case_id, date(2026, 11, 20))

    assert (first.reminded, first.failed) == (0, 1)
    assert (second.reminded, third.reminded) == (1, 0)
    assert len(flaky.tries) == 2 and len(mails(fake)) == 1
    failed, sent_row = nudges_of(app, case_id)
    assert failed["failed_at"] is not None and failed["sent_at"] is None
    assert sent_row["sent_at"] is not None
    assert [
        e["event_type"]
        for e in events_of(app, case_id, "approval_nudge_failed", "approval_reminder_sent")
    ] == ["approval_nudge_failed", "approval_reminder_sent"]


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("live")
def test_a_reminder_whose_fate_is_unknown_is_never_sent_again(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    # The sweep fails halfway: Graph got the email, but ARGUS never heard back.
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    mails(fake).clear()
    flaky = Flaky(approvals, unknown=1)
    with_flaky = Approvals(approvals.mailbox, APP_URL, DOMAIN, nudges=flaky)

    first = run(app, with_flaky, case_id, date(2026, 11, 19))
    second = run(app, with_flaky, case_id, date(2026, 11, 20))

    assert first.failed == 1 and second.reminded == 0
    assert len(flaky.tries) == 1 and len(mails(fake)) == 1
    (row,) = nudges_of(app, case_id)
    assert row["sent_at"] is None and row["failed_at"] is None


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("live")
def test_the_database_allows_one_reminder_per_request(
    app: Engine, owner: Engine, approvals: Approvals
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    request_id = requests_of(app, case_id)[0]["id"]
    move_clock(owner, request_id, MONDAY)
    run(app, approvals, case_id, date(2026, 11, 19))
    insert = (
        "insert into approval_nudge (request_id, kind, recipient_address, due_on)"
        " values (:id, :kind, :address, :due)"
    )

    with pytest.raises(CaseRuleBroken), unit_of_work(app, JOB) as db:
        db.execute(
            text(insert),
            {"id": request_id, "kind": "reminder", "address": None, "due": date(2026, 11, 19)},
        )
    (row,) = nudges_of(app, case_id)
    with pytest.raises(CaseRuleBroken, match="never changes again"), unit_of_work(app, JOB) as db:
        db.execute(
            text("update approval_nudge set failed_at = now() where id = :id"), {"id": row["id"]}
        )


@pytest.mark.usefixtures("live")
def test_the_database_refuses_a_reminder_too_early_or_from_a_person(
    app: Engine, owner: Engine, approvals: Approvals
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    request_id = requests_of(app, case_id)[0]["id"]
    move_clock(owner, request_id, MONDAY)
    insert = (
        "insert into approval_nudge (request_id, kind, recipient_address, due_on)"
        " values (:id, :kind, :address, :due)"
    )
    for actor, kind, due, words in (
        (JOB, "reminder", date(2026, 11, 18), "not due before 3 working days"),
        (JOB, "escalation", date(2026, 11, 20), "not due before 5 working days"),
        (JOB, "reminder", date(2099, 1, 1), "not due yet"),
        (Actor.user(str(uuid.uuid4())), "reminder", date(2026, 11, 19), "only ARGUS"),
    ):
        with pytest.raises(CaseRuleBroken, match=words), unit_of_work(app, actor) as db:
            db.execute(
                text(insert), {"id": request_id, "kind": kind, "address": FINANCE, "due": due}
            )
    assert nudges_of(app, case_id) == []


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("live")
def test_the_client_sends_only_the_stored_reminder(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    flaky = Flaky(approvals, refuse=1)
    run(
        app,
        Approvals(approvals.mailbox, APP_URL, DOMAIN, nudges=flaky),
        case_id,
        date(2026, 11, 19),
    )
    (mail,) = flaky.tries
    client = approvals.nudges
    assert isinstance(client, GraphClient)
    mails(fake).clear()

    # The failed row is done; a made up or changed reminder never goes out either.
    for wrong in (
        mail,
        replace(mail, nudge_id=mail.nudge_id + 10_000_000),
        replace(mail, recipient_address="mallory@evil.example"),
    ):
        with pytest.raises(GraphCallRefused):
            client.send_approval_nudge(wrong)
    assert mails(fake) == []


# A new request for a waiting invoice (AP-06)


@pytest.mark.req("AP-06")
@pytest.mark.usefixtures("live")
def test_an_approver_marked_away_hands_the_waiting_invoice_to_the_deputy_with_a_new_clock(
    app: Engine, owner: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, deputy = vendor(app, deputy=True)
    case_id = waiting_case(app, approvals, account)
    old = requests_of(app, case_id)[0]
    move_clock(owner, old["id"], MONDAY)
    run(app, approvals, case_id, date(2026, 11, 19))  # the reminder to Alice
    set_absent(app, ADMIN, approver, True)
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 23))

    assert (result.renewed, result.sent, result.escalated) == (1, 1, 0)
    _, new = requests_of(app, case_id)
    assert new["renews_id"] == old["id"]
    assert (new["recipient_id"], new["recipient_role"], new["on_behalf_of"]) == (
        deputy,
        "deputy",
        approver,
    )
    (mail,) = mails(fake)
    assert to(mail) == new["recipient_address"]
    assert "deputy of Alice Approver" in mail["body"]["content"]
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    # Only the deputy decides now; the old request is refused.
    with (
        pytest.raises(CaseRuleBroken, match="newest"),
        unit_of_work(app, Actor.user(approver, "Alice Approver", old["recipient_address"])) as db,
    ):
        db.execute(
            text(
                "insert into approval_decision (case_id, request_id, decision, comment)"
                " values (:case, :request, 'rejected', 'not ours')"
            ),
            {"case": case_id, "request": old["id"]},
        )
    with app.begin() as db:
        assert decide(db, case_id) is True
    (requested,) = events_of(app, case_id, "approval_requested")[1:]
    assert requested["details"]["as"] == "deputy"


@pytest.mark.req("AP-06")
@pytest.mark.usefixtures("live")
def test_an_approver_away_without_a_deputy_sends_the_waiting_invoice_to_the_queue(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    set_absent(app, ADMIN, approver, True)
    mails(fake).clear()

    away = run(app, approvals, case_id, date(2026, 11, 17))

    assert (away.absent, away.renewed) == (1, 0)
    assert status_of(app, case_id) is CaseStatus.EXCEPTION
    assert open_reasons(app, case_id) == ["approver_absent"]

    set_absent(app, ADMIN, approver, False)
    back = run(app, approvals, case_id, date(2026, 11, 18))

    # Back from the queue with a new request, so a new email and a new clock.
    assert (back.resumed, back.renewed, back.sent) == (1, 1, 1)
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    first, again = requests_of(app, case_id)
    assert again["renews_id"] == first["id"] and again["recipient_id"] == approver
    assert [to(m) for m in mails(fake)] == [again["recipient_address"]]


@pytest.mark.usefixtures("live")
def test_a_new_approver_in_the_approver_table_gets_the_waiting_invoice(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    # What Benjamin does after an escalation (Arita on #40, 23 Nov).
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    newcomer, address = person(app, "Nina Newcomer")
    set_vendor_approver(app, ADMIN, account, newcomer, None, False)
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 17))
    again = run(app, approvals, case_id, date(2026, 11, 17))

    assert (result.renewed, result.sent, again.renewed) == (1, 1, 0)
    assert requests_of(app, case_id)[-1]["recipient_id"] == newcomer
    assert [to(m) for m in mails(fake)] == [address]


@pytest.mark.usefixtures("live")
def test_a_vendor_change_sends_the_waiting_invoice_to_the_new_vendors_approver(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    # Follow up from the PR 205 review, posted on #40.
    account, old_approver, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    other, new_approver, _ = vendor(app)
    with unit_of_work(app, Actor.system("vendor-match")) as db:
        change_case(db, case_id, partner_account=other)
    mails(fake).clear()

    result = run(app, approvals, case_id, date(2026, 11, 17))

    assert (result.renewed, result.sent) == (1, 1)
    old, new = requests_of(app, case_id)
    assert (new["vendor_account"], new["recipient_id"]) == (other, new_approver)
    assert old["recipient_id"] == old_approver
    # The new approver enters the account for the new vendor on the approval page, then decides.
    approver = Actor.user(new_approver, "Alice Approver", new["recipient_address"])
    with unit_of_work(app, approver) as db:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, proposed_by_name, reason)"
                " values (:c, :v, '4980', '9', 'SKR03', 'person', 'Alice Approver', 'New vendor')"
            ),
            {"c": case_id, "v": other},
        )
    with app.begin() as db:
        assert decide(db, case_id) is True
    assert status_of(app, case_id) is CaseStatus.APPROVED


@pytest.mark.usefixtures("live")
def test_a_vendor_change_to_a_vendor_without_approver_goes_to_the_queue(
    app: Engine, approvals: Approvals
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    with unit_of_work(app, Actor.system("vendor-match")) as db:
        change_case(db, case_id, partner_account=str(next(_numbers)))

    result = run(app, approvals, case_id, date(2026, 11, 17))

    assert (result.absent, result.renewed) == (1, 0)
    assert open_reasons(app, case_id) == ["vendor_without_approver"]


@pytest.mark.usefixtures("live")
def test_the_database_refuses_a_new_request_while_the_person_can_still_decide(
    app: Engine, approvals: Approvals
) -> None:
    account, _, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    old = requests_of(app, case_id)[0]
    insert = "insert into approval_request (case_id, renews_id) values (:case, :old)"

    with pytest.raises(CaseRuleBroken, match="can still decide"), unit_of_work(app, JOB) as db:
        db.execute(text(insert), {"case": case_id, "old": old["id"]})
    with (
        pytest.raises(CaseRuleBroken, match="only ARGUS"),
        unit_of_work(app, Actor.user(str(uuid.uuid4()))) as db,
    ):
        db.execute(text(insert), {"case": case_id, "old": old["id"]})
    assert len(requests_of(app, case_id)) == 1


@pytest.mark.usefixtures("live")
def test_the_queue_shows_an_escalated_case_that_went_to_the_queue(
    app: Engine, owner: Engine, approvals: Approvals
) -> None:
    account, approver, _ = vendor(app)
    case_id = waiting_case(app, approvals, account)
    move_clock(owner, requests_of(app, case_id)[0]["id"], MONDAY)
    run(app, approvals, case_id, date(2026, 11, 23))
    set_absent(app, ADMIN, approver, True)
    run(app, approvals, case_id, date(2026, 11, 24))

    with app.connect() as db:
        exception_id: int = db.execute(
            text("select id from exception_item where case_id = :id and resolved_at is null"),
            {"id": case_id},
        ).scalar_one()
        detail = open_exception_detail(db, exception_id)
    assert detail is not None and detail.case is not None
    assert detail.case.escalated_at is not None

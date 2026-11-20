"""Propose the G/L account and tax key from the vendor's booking history (#37), against a real
PostgreSQL with the rights of argus_app.

Covers PR-04 (the proposal), EX-01 (no history goes to the queue) and NF-01 (every proposal is
kept and in the audit log). Each test opens its own cases with a made up vendor account nobody
else uses and limits the pass to those cases, because the database is shared with the other
tests. DATEV is a small stand in that only answers ``list_postings``; one test also runs against
the fake DATEV with the synthetic fixture postings. Names and numbers are made up.
"""

import itertools
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    change_case,
    create_case,
    get_case,
    move_case,
    open_exception,
    record_event,
    resolve_exception,
    unit_of_work,
)
from argus.datev import Account, DatevConnectGateway, DatevError, DatevUnavailable
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev
from argus.datev.models import Booking, BookingRecord, Posting
from argus.documents.store import MemoryDocumentStore
from argus.intake.accounts import (
    Chart,
    EnteredAccount,
    EnteredBy,
    NotEnterable,
    ProposalSource,
    current_proposal,
    enter_account,
    propose_accounts,
)
from argus.invoices_page.web import DATA_PATH as SEARCH
from standins.entra.app import FakeEntra
from tests.integration.conftest import migrate

pytestmark = pytest.mark.req("PR-04")

INTAKE = Actor.system("intake")
BENJAMIN_ID = "33333333-3333-4333-8333-333333333333"  # finance, made up
BENJAMIN = EnteredBy(Actor.user(BENJAMIN_ID), "Benjamin Test", "benjamin@max-asp-test.example")
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up
TODAY = date(2026, 11, 20)

# Vendor accounts nobody else uses.
_numbers = itertools.count(830_000_000 + uuid.uuid4().int % 1_000_000 * 100)


class History:
    """DATEV as far as the proposal needs it: the postings of each vendor account."""

    def __init__(self, postings: dict[str, list[Posting]] | None = None) -> None:
        self.postings = postings or {}
        self.asked: list[tuple[int, date, date]] = []
        self.fail: Exception | None = None

    def list_postings(self, account: int, start: date, end: date) -> list[Posting]:
        self.asked.append((account, start, end))
        if self.fail is not None:
            raise self.fail
        return [p for p in self.postings.get(str(account), []) if start <= p.date <= end]

    # Not used by the proposal.
    def list_vendors(self) -> list[Account]:
        raise AssertionError("not used")

    def get_vendor(self, number: int) -> Account | None:
        raise AssertionError("not used")

    def list_customers(self) -> list[Account]:
        raise AssertionError("not used")

    def get_customer(self, number: int) -> Account | None:
        raise AssertionError("not used")

    def submit_booking(self, booking: Booking) -> BookingRecord:
        raise AssertionError("ARGUS must not book while proposing")

    def get_booking(self, reference: str) -> BookingRecord | None:
        raise AssertionError("not used")


def posting(account: str, day: str, contra: int, tax_key: str = "9", number: str = "") -> Posting:
    return Posting(
        account=int(account),
        date=date.fromisoformat(day),
        document_number=number or f"RE-{day}",
        contra_account=contra,
        tax_key=tax_key,
        gross=Decimal("119.00"),
    )


def vendor() -> str:
    return str(next(_numbers))


def matched_case(
    app: Engine, account: str | None, kind: CaseKind = CaseKind.INCOMING_INVOICE
) -> int:
    """An incoming case matched to its vendor account, as #35 leaves it."""
    with unit_of_work(app, INTAKE) as db:
        return create_case(
            db,
            kind,
            partner_account=account,
            document_number=f"T-{uuid.uuid4().hex[:8]}",
            gross_cents=11900,
            currency="EUR",
        )


def open_of(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select id, reason::text as reason, detail, facts from exception_item"
                " where case_id = :id and resolved_at is null order by id"
            ),
            {"id": case_id},
        ).mappings()
        return [dict(r) for r in rows]


def proposal_events(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select actor_type || ':' || actor as actor, details from audit_event"
                " where case_id = :id and event_type = 'account_proposed' order by chain_seq"
            ),
            {"id": case_id},
        ).all()
    return [{"actor": r.actor, **r.details} for r in rows]


def proposal_of(app: Engine, case_id: int):  # type: ignore[no-untyped-def]
    with app.connect() as connection:
        return current_proposal(connection, case_id)


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with app.connect() as connection:
        return get_case(connection, case_id).status


def run(app: Engine, datev: History, *cases: int, chart: Chart = Chart.SKR03):  # type: ignore[no-untyped-def]
    return propose_accounts(app, datev, chart, cases=list(cases), today=TODAY)


# ------------------------------------------------------------------------- proposing


def test_a_vendor_with_history_gets_its_most_recent_account_and_tax_key(app: Engine) -> None:
    account = vendor()
    datev = History(
        {
            account: [
                posting(account, "2026-07-14", 4930, "9", "BB-1"),
                posting(account, "2026-09-15", 4806, "9", "BB-3"),
                posting(account, "2026-10-02", 1200, ""),  # the payment, not history
            ]
        }
    )
    case_id = matched_case(app, account)

    done = run(app, datev, case_id)

    assert (done.proposed, done.no_proposal, done.failed) == (1, 0, 0)
    proposal = proposal_of(app, case_id)
    assert proposal is not None
    assert (proposal.gl_account, proposal.tax_key, proposal.chart) == ("4806", "9", Chart.SKR03)
    assert proposal.source is ProposalSource.BOOKING_HISTORY
    assert (proposal.from_document_number, proposal.from_posting_date) == (
        "BB-3",
        date(2026, 9, 15),
    )
    assert (proposal.vendor_account, proposal.proposed_by_name) == (account, None)
    # The case carries on as it was; the proposal does not stop it.
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    assert open_of(app, case_id) == []
    # DATEV was asked for this year and the two before.
    assert datev.asked == [(int(account), date(2024, 1, 1), TODAY)]


def test_every_proposal_is_in_the_audit_log(app: Engine) -> None:
    account = vendor()
    datev = History({account: [posting(account, "2026-09-01", 4210, "", "MV-9")]})
    case_id = matched_case(app, account)

    run(app, datev, case_id)

    [event] = proposal_events(app, case_id)
    assert event == {
        "actor": "system:account-proposal",
        "proposal_id": proposal_of(app, case_id).id,
        "vendor_account": account,
        "gl_account": "4210",
        "tax_key": "",
        "chart": "SKR03",
        "source": "booking_history",
        "from_document_number": "MV-9",
        "from_posting_date": "2026-09-01",
    }


def test_a_case_gets_one_proposal_and_later_passes_leave_it(app: Engine) -> None:
    account = vendor()
    datev = History({account: [posting(account, "2026-09-01", 4930)]})
    case_id = matched_case(app, account)
    run(app, datev, case_id)

    datev.postings[account].append(posting(account, "2026-11-01", 4806))
    again = run(app, datev, case_id)

    assert again.proposed == 0
    assert proposal_of(app, case_id).gl_account == "4930"
    assert len(proposal_events(app, case_id)) == 1


def test_the_vendor_is_read_once_for_all_its_cases(app: Engine) -> None:
    account = vendor()
    datev = History({account: [posting(account, "2026-09-01", 4930)]})
    first, second = matched_case(app, account), matched_case(app, account)

    done = run(app, datev, first, second)

    assert done.proposed == 2
    assert len(datev.asked) == 1


def test_a_credit_note_gets_a_proposal_too(app: Engine) -> None:
    account = vendor()
    datev = History({account: [posting(account, "2026-09-01", 4930)]})
    case_id = matched_case(app, account, CaseKind.INCOMING_CREDIT_NOTE)

    run(app, datev, case_id)

    assert proposal_of(app, case_id).gl_account == "4930"


def test_cases_without_a_vendor_account_or_outgoing_are_left_alone(app: Engine) -> None:
    datev = History()
    unmatched = matched_case(app, None)
    outgoing = matched_case(app, vendor(), CaseKind.OUTGOING_INVOICE)

    done = run(app, datev, unmatched, outgoing)

    assert (done.proposed, done.no_proposal) == (0, 0)
    assert datev.asked == []


def test_the_chart_setting_decides_what_a_payment_is(app: Engine) -> None:
    account = vendor()
    datev = History(
        {account: [posting(account, "2026-08-01", 6815), posting(account, "2026-09-01", 1800, "")]}
    )
    case_id = matched_case(app, account)

    run(app, datev, case_id, chart=Chart.SKR04)

    proposal = proposal_of(app, case_id)
    assert (proposal.gl_account, proposal.chart) == ("6815", Chart.SKR04)


# --------------------------------------------------------------------- no proposal


@pytest.mark.req("EX-01")
def test_a_vendor_without_history_goes_to_the_queue_and_nothing_is_guessed(app: Engine) -> None:
    account = vendor()
    datev = History({account: [posting(account, "2026-09-20", 1200, "")]})
    case_id = matched_case(app, account)

    done = run(app, datev, case_id)

    assert (done.proposed, done.no_proposal) == (0, 1)
    assert proposal_of(app, case_id) is None
    [item] = open_of(app, case_id)
    assert item["reason"] == "no_account_proposal"
    assert f"Vendor account {account} has no booking history" in item["detail"]
    assert "does not guess" in item["detail"]
    assert item["facts"] == {
        "account": account,
        "why": "no_history",
        "postings_read": 1,
        "chart": "SKR03",
        "candidates": [],
    }
    assert status_of(app, case_id) is CaseStatus.EXCEPTION
    assert proposal_events(app, case_id) == []


@pytest.mark.req("EX-01")
def test_an_invoice_split_over_two_accounts_goes_to_the_queue(app: Engine) -> None:
    account = vendor()
    datev = History(
        {
            account: [
                posting(account, "2026-09-01", 4806, "9", "SPLIT"),
                posting(account, "2026-09-01", 4930, "9", "SPLIT"),
            ]
        }
    )
    case_id = matched_case(app, account)

    run(app, datev, case_id)

    [item] = open_of(app, case_id)
    assert "4806 (tax key 9), 4930 (tax key 9)" in item["detail"]
    assert item["facts"]["why"] == "several_accounts"
    assert item["facts"]["candidates"] == [
        {"gl_account": "4806", "tax_key": "9"},
        {"gl_account": "4930", "tax_key": "9"},
    ]


def test_the_queue_item_is_opened_once(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)
    run(app, History(), case_id)

    again = run(app, History(), case_id)

    assert again.no_proposal == 0
    assert len(open_of(app, case_id)) == 1


def test_it_carries_on_by_itself_once_the_vendor_has_history(app: Engine) -> None:
    """For example the vendor's first invoice got its account by hand and is booked now."""
    account = vendor()
    datev = History()
    case_id = matched_case(app, account)
    run(app, datev, case_id)
    [item] = open_of(app, case_id)

    datev.postings[account] = [posting(account, "2026-11-10", 4930, "9", "FIRST-1")]
    done = run(app, datev, case_id)

    assert (done.proposed, done.resolved) == (1, 1)
    assert open_of(app, case_id) == []
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    assert proposal_of(app, case_id).gl_account == "4930"
    with app.connect() as connection:
        outcome, resolution, linked = connection.execute(
            text(
                "select e.outcome, e.resolution,"
                " (select p.exception_id from account_proposal p where p.case_id = e.case_id)"
                " from exception_item e where e.id = :id"
            ),
            {"id": item["id"]},
        ).one()
    assert outcome == "fixed_in_datev"
    assert "proposed G/L account 4930 with tax key 9" in str(resolution)
    assert linked == item["id"]


def test_a_case_with_another_open_exception_still_gets_its_proposal(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)
    with unit_of_work(app, Actor.system("vendor-match")) as db:
        open_exception(db, ExceptionReason.VENDOR_WITHOUT_APPROVER, "no approver", case_id=case_id)

    run(app, History({account: [posting(account, "2026-09-01", 4930)]}), case_id)

    assert proposal_of(app, case_id).gl_account == "4930"
    assert [i["reason"] for i in open_of(app, case_id)] == ["vendor_without_approver"]


def test_when_datev_does_not_answer_the_cases_wait(app: Engine) -> None:
    datev = History()
    datev.fail = DatevUnavailable("down")
    case_id = matched_case(app, vendor())

    done = run(app, datev, case_id)

    assert done.skipped_datev_unavailable and done.waiting == [case_id]
    assert open_of(app, case_id) == [] and proposal_of(app, case_id) is None


def test_a_datev_setup_problem_is_never_read_as_no_history(app: Engine) -> None:
    datev = History()
    datev.fail = DatevError("unknown fiscal year")
    case_id = matched_case(app, vendor())

    done = run(app, datev, case_id)

    assert done.failed == 1
    assert open_of(app, case_id) == []


def test_against_the_fake_datev_with_the_fixture_postings(app: Engine) -> None:
    """Vendor 70001 (synthetic) was booked to 4930 with tax key 9 three times."""
    with TestClient(create_fake_datev()) as fake:
        datev = DatevConnectGateway(fake, DEFAULT_CLIENT_ID, today=lambda: TODAY)
        case_id = matched_case(app, "70001")
        propose_accounts(app, datev, Chart.SKR03, cases=[case_id], today=TODAY)

    proposal = proposal_of(app, case_id)
    assert (proposal.gl_account, proposal.tax_key, proposal.from_document_number) == (
        "4930",
        "9",
        "BB-2026-1110",
    )


# ------------------------------------------------------------- entered by a person


def missing_account(app: Engine) -> tuple[int, int]:
    account = vendor()
    case_id = matched_case(app, account)
    run(app, History(), case_id)
    [item] = open_of(app, case_id)
    return case_id, int(item["id"])


@pytest.mark.req("EX-01", "NF-01")
def test_a_person_enters_the_account_and_the_case_carries_on(app: Engine) -> None:
    case_id, exception_id = missing_account(app)

    with unit_of_work(app, BENJAMIN.actor) as db:
        done = enter_account(
            db, exception_id, EnteredAccount("4930", "9", "Office supplies"), BENJAMIN, Chart.SKR03
        )

    assert done.case_id == case_id
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    proposal = proposal_of(app, case_id)
    assert (proposal.gl_account, proposal.tax_key, proposal.source) == (
        "4930",
        "9",
        ProposalSource.PERSON,
    )
    assert proposal.proposed_by_name == "Benjamin Test"
    [event] = proposal_events(app, case_id)
    assert event["actor"] == f"user:{BENJAMIN_ID}"
    assert (event["source"], event["name"], event["reason"]) == (
        "person",
        "Benjamin Test",
        "Office supplies",
    )
    assert event["sign_in_address"] == "benjamin@max-asp-test.example"


def test_only_an_open_missing_account_takes_an_entry(app: Engine) -> None:
    case_id, exception_id = missing_account(app)
    with unit_of_work(app, BENJAMIN.actor) as db:
        enter_account(db, exception_id, EnteredAccount("4930", "9", "first"), BENJAMIN, Chart.SKR03)

    with pytest.raises(NotEnterable), unit_of_work(app, BENJAMIN.actor) as db:
        enter_account(db, exception_id, EnteredAccount("4806", "9", "again"), BENJAMIN, Chart.SKR03)
    assert proposal_of(app, case_id).gl_account == "4930"


# ----------------------------------------------------------------- database rules


def insert(db: Any, case_id: int, account: str, **extra: Any) -> None:
    values = {
        "case_id": case_id,
        "exception_id": None,
        "vendor": account,
        "gl": "4930",
        "tax_key": "9",
        "chart": "SKR03",
        "source": "booking_history",
        "doc": "RE-1",
        "day": date(2026, 9, 1),
        "reason": None,
        "name": None,
        **extra,
    }
    db.execute(
        text(
            """
            insert into account_proposal (case_id, exception_id, vendor_account, gl_account,
                                          tax_key, chart, source, from_document_number,
                                          from_posting_date, reason, proposed_by_name)
            values (:case_id, :exception_id, :vendor, :gl, :tax_key, :chart, :source, :doc,
                    :day, :reason, :name)
            """
        ),
        values,
    )


@pytest.mark.req("NF-01")
def test_a_proposal_is_never_changed_or_removed(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)
    run(app, History({account: [posting(account, "2026-09-01", 4930)]}), case_id)

    for sql in (
        "update account_proposal set gl_account = '4806' where case_id = :id",
        "delete from account_proposal where case_id = :id",
    ):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
            db.execute(text(sql), {"id": case_id})
    assert proposal_of(app, case_id).gl_account == "4930"


def test_a_person_cannot_write_a_history_proposal_and_a_job_cannot_pose_as_a_person(
    app: Engine,
) -> None:
    account = vendor()
    case_id = matched_case(app, account)

    with pytest.raises(CaseRuleBroken, match="only ARGUS"), unit_of_work(app, BENJAMIN.actor) as db:
        insert(db, case_id, account)
    with pytest.raises(CaseRuleBroken, match="only a person"), unit_of_work(app, INTAKE) as db:
        insert(
            db,
            case_id,
            account,
            source="person",
            doc=None,
            day=None,
            reason="typed",
            name="Robot",
        )


def test_a_person_needs_a_name_and_a_reason(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)
    person = {"source": "person", "doc": None, "day": None, "name": "Benjamin Test"}

    for reason in ("\t \n", "ab"):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, BENJAMIN.actor) as db:
            insert(db, case_id, account, **person, reason=reason)


def test_the_proposal_names_the_vendor_account_of_the_case(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)

    with (
        pytest.raises(CaseRuleBroken, match="another vendor account"),
        unit_of_work(app, INTAKE) as db,
    ):
        insert(db, case_id, vendor())


def test_a_proposal_after_received_is_refused(app: Engine) -> None:
    """The account is settled before approval and cannot change once the case moved on."""
    account = vendor()
    case_id = matched_case(app, account)
    with unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, duplicate_key=f"dup-{uuid.uuid4().hex}")
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)

    with (
        pytest.raises(CaseRuleBroken, match="settled before approval"),
        unit_of_work(app, INTAKE) as db,
    ):
        insert(db, case_id, account)


def test_only_incoming_cases_get_a_proposal(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account, CaseKind.OUTGOING_INVOICE)

    with pytest.raises(CaseRuleBroken, match="not an incoming"), unit_of_work(app, INTAKE) as db:
        insert(db, case_id, account)


@pytest.mark.parametrize(("gl", "tax_key"), [("49", "9"), ("4930", "12345"), ("49a0", "")])
def test_the_database_refuses_an_account_or_tax_key_that_cannot_be(
    app: Engine, gl: str, tax_key: str
) -> None:
    account = vendor()
    case_id = matched_case(app, account)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
        insert(db, case_id, account, gl=gl, tax_key=tax_key)


@pytest.mark.req("EX-01")
def test_a_missing_account_ends_only_as_entered_found_or_rejected(app: Engine) -> None:
    _, exception_id = missing_account(app)

    for outcome in (Outcome.RETRIED, Outcome.ACCEPTED_WITH_COMMENT):
        with (
            pytest.raises(CaseRuleBroken, match="can only end as"),
            unit_of_work(app, BENJAMIN.actor) as db,
        ):
            resolve_exception(db, exception_id, outcome)
    # data_corrected needs the entry, fixed_in_datev a proposal from the history.
    with (
        pytest.raises(CaseRuleBroken, match="after a person entered"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        resolve_exception(db, exception_id, Outcome.DATA_CORRECTED)
    with (
        pytest.raises(CaseRuleBroken, match="after ARGUS proposed"),
        unit_of_work(app, Actor.system("account-proposal")) as db,
    ):
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV)


@pytest.mark.req("EX-01")
def test_a_person_may_reject_the_invoice_instead(app: Engine) -> None:
    case_id, exception_id = missing_account(app)

    with unit_of_work(app, BENJAMIN.actor) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )

    assert status_of(app, case_id) is CaseStatus.CLOSED


def test_an_entry_names_an_open_missing_account_of_the_same_case(app: Engine) -> None:
    _, exception_id = missing_account(app)
    other_case, _ = missing_account(app)
    with app.connect() as connection:
        other_account = get_case(connection, other_case).partner_account
    person = {"source": "person", "doc": None, "day": None, "name": "B", "reason": "typed"}

    with (
        pytest.raises(CaseRuleBroken, match="not an open missing account"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        insert(db, other_case, str(other_account), exception_id=exception_id, **person)


def test_the_application_cannot_write_the_audit_event_itself(app: Engine) -> None:
    case_id = matched_case(app, vendor())

    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, INTAKE) as db,
    ):
        record_event(db, "account_proposed", case_id=case_id, details={"gl_account": "4930"})


def test_the_app_may_not_set_who_or_when(app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)

    with pytest.raises(NotAllowed), unit_of_work(app, INTAKE) as db:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, chart,"
                " source, from_document_number, from_posting_date, proposed_by)"
                " values (:id, :v, '4930', 'SKR03', 'booking_history', 'X', '2026-01-01',"
                " 'user:someone')"
            ),
            {"id": case_id, "v": account},
        )


# ------------------------------------------------------------------- on the pages


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    store = MemoryDocumentStore()
    return TestClient(
        create_app(lambda: app, sign_in=entra.verifier, documents_factory=lambda: store)
    )


def as_role(entra: FakeEntra, role: Role, who: str = BENJAMIN_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {entra.token(who, roles=[role])}"}


ENTRY = {"gl_account": "4930", "tax_key": "9", "reason": "Office supplies, as on the invoice"}


@pytest.mark.req("EX-01", "EX-02")
def test_benjamin_enters_the_account_on_the_queue_page(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    case_id, exception_id = missing_account(app)

    shown = client.get(f"/exceptions/{exception_id}", headers=as_role(entra, Role.FINANCE))
    assert shown.status_code == 200
    assert shown.json()["account_enterable"] is True
    assert shown.json()["reason_text"] == "ARGUS cannot propose a G/L account and tax key"

    answer = client.post(
        f"/exceptions/{exception_id}/account", json=ENTRY, headers=as_role(entra, Role.FINANCE)
    )

    assert answer.status_code == 200, answer.text
    assert answer.json()["case_id"] == case_id
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    proposal = proposal_of(app, case_id)
    assert (proposal.gl_account, proposal.tax_key) == ("4930", "9")
    assert proposal.source is ProposalSource.PERSON


def test_a_value_the_person_can_fix_comes_back_with_a_sentence(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    _, exception_id = missing_account(app)

    answer = client.post(
        f"/exceptions/{exception_id}/account",
        json={**ENTRY, "gl_account": "1200"},
        headers=as_role(entra, Role.FINANCE),
    )

    assert answer.status_code == 422
    assert "not an account an invoice is booked to" in answer.json()["problem"]


def test_an_approver_cannot_enter_an_account(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    case_id, exception_id = missing_account(app)

    answer = client.post(
        f"/exceptions/{exception_id}/account",
        json=ENTRY,
        headers=as_role(entra, Role.APPROVER, ALICE),
    )

    assert answer.status_code == 403
    assert proposal_of(app, case_id) is None


def test_only_missing_account_exceptions_take_an_account(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    case_id = matched_case(app, vendor())
    with unit_of_work(app, INTAKE) as db:
        other = open_exception(db, ExceptionReason.DUPLICATE, "maybe twice", case_id=case_id)

    answer = client.post(
        f"/exceptions/{other}/account", json=ENTRY, headers=as_role(entra, Role.FINANCE)
    )

    assert answer.status_code == 404


def test_the_invoices_page_shows_the_proposal_and_offers_only_reject_for_a_missing_one(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    account = vendor()
    with_history = matched_case(app, account)
    run(app, History({account: [posting(account, "2026-09-01", 4930, "9")]}), with_history)
    missing, exception_id = missing_account(app)

    body = client.post(SEARCH, json={"q": account}, headers=as_role(entra, Role.FINANCE)).json()
    [row] = [c for c in body["cases"] if c["case_id"] == with_history]
    assert (row["gl_account"], row["tax_key"]) == ("4930", "9")

    choices = client.get(
        f"/exceptions/{exception_id}/choices", headers=as_role(entra, Role.FINANCE)
    ).json()
    assert [c["outcome"] for c in choices["choices"]] == ["rejected"]
    assert "exception queue" in choices["note"]
    assert missing != with_history


def test_the_trail_says_who_proposed_which_account(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    case_id, exception_id = missing_account(app)
    client.post(
        f"/exceptions/{exception_id}/account", json=ENTRY, headers=as_role(entra, Role.FINANCE)
    )

    steps = client.get(f"/cases/{case_id}/trail", headers=as_role(entra, Role.FINANCE)).json()[
        "steps"
    ]

    [entered] = [s for s in steps if s["event_type"] == "account_proposed"]
    assert entered["what"] == "A person entered G/L account 4930 with tax key 9"
    assert entered["comment"] == ENTRY["reason"]
    # The name the person had at that moment, from the step itself (ADR 0008).
    assert entered["actor_name"] == "Test User"


# ------------------------------------------------------------------- the migration


@pytest.mark.req("NF-01")
def test_a_downgrade_keeps_the_proposals_and_their_rules(owner: Engine, app: Engine) -> None:
    account = vendor()
    case_id = matched_case(app, account)
    run(app, History({account: [posting(account, "2026-09-01", 4930)]}), case_id)
    before = proposal_of(app, case_id)

    migrate(owner, "0017", down=True)
    try:
        with owner.connect() as connection:
            kept: str = connection.execute(
                text("select gl_account from account_proposal where case_id = :id"),
                {"id": case_id},
            ).scalar_one()
        assert kept == "4930"
        # Still never changed, also while the code from before 0019 runs.
        with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
            db.execute(text("delete from account_proposal where case_id = :id"), {"id": case_id})
    finally:
        migrate(owner, "head")

    assert proposal_of(app, case_id) == before
    other = matched_case(app, account)
    run(app, History({account: [posting(account, "2026-09-01", 4806)]}), other)
    assert proposal_of(app, other).gl_account == "4806"


def test_a_fiscal_year_datev_has_not_set_up_holds_no_postings(app: Engine) -> None:
    """Early January, before the new year is opened in DATEV, or a client with less than three
    years in DATEV: the years that exist still count (code review of PR 190)."""
    with TestClient(create_fake_datev()) as fake:
        fake.put("/_fake/fiscal-years", json={"open": ["20260101"]})
        datev = DatevConnectGateway(fake, DEFAULT_CLIENT_ID, today=lambda: TODAY)
        case_id = matched_case(app, "70001")
        done = propose_accounts(app, datev, Chart.SKR03, cases=[case_id], today=date(2027, 1, 5))

    assert (done.proposed, done.failed) == (1, 0)
    assert proposal_of(app, case_id).gl_account == "4930"


def test_no_fiscal_year_set_up_at_all_is_a_setup_problem(app: Engine) -> None:
    with TestClient(create_fake_datev()) as fake:
        fake.put("/_fake/fiscal-years", json={"open": ["20200101"]})
        datev = DatevConnectGateway(fake, DEFAULT_CLIENT_ID, today=lambda: TODAY)
        case_id = matched_case(app, "70001")
        done = propose_accounts(app, datev, Chart.SKR03, cases=[case_id], today=TODAY)

    assert done.failed == 1
    assert open_of(app, case_id) == [] and proposal_of(app, case_id) is None

"""The invoice status page against a real PostgreSQL (#62, NF-05, NF-03, EX-01).

The app reads with the rights of argus_app. Cases from other tests stay in the database, so each
test finds its own cases by ID or by a search word made for this run. Every name, number and
amount below is made up.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason, ExceptionReason
from argus.cases.store import create_case, move_case, open_exception, unit_of_work
from argus.documents.store import MemoryDocumentStore
from argus.invoices_page.views import MAX_ROWS
from argus.invoices_page.web import DATA_PATH as SEARCH
from standins.entra.app import FakeEntra
from tests.integration.test_queue_page import received_email

# Exceptions open only once ARGUS runs live (#72), like in the queue page tests.
pytestmark = [pytest.mark.req("NF-05", "NF-03", "EX-01"), pytest.mark.usefixtures("live")]

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up

VENDOR = "Musterlieferant Erika Mustermann e.K."
CUSTOMER = "Kundin Beispiel GmbH"
INTAKE = Actor.system("intake")


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def store() -> MemoryDocumentStore:
    return MemoryDocumentStore()


@pytest.fixture
def client(app: Engine, entra: FakeEntra, store: MemoryDocumentStore) -> TestClient:
    return TestClient(
        create_app(lambda: app, sign_in=entra.verifier, documents_factory=lambda: store)
    )


@pytest.fixture
def tag() -> str:
    """A word only this test's cases carry, in their document number."""
    return f"ST{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


def as_role(entra: FakeEntra, role: Role | None, who: str = BENJAMIN) -> dict[str, str]:
    token = entra.token(who, roles=[role] if role else [])
    return {"Authorization": f"Bearer {token}"}


def read_on_body(app: Engine, case_id: int, **fields: str) -> None:
    """What ARGUS read on the invoice, as intake stores it (#30)."""
    reading = {
        "document": "body",
        "kind": "invoice",
        "reader": "text",
        "pages": 1,
        "unreadable": None,
        "problems": [],
        "fields": {
            name: {
                "value": value,
                "source": "text",
                "confidence": 0.95,
                "page": 1,
                "words": None,
                "flag": None,
                "note": None,
            }
            for name, value in fields.items()
        },
    }
    with unit_of_work(app, INTAKE) as db:
        db.execute(
            text(
                """
                insert into invoice_reading (case_id, document, role, reading)
                values (:case_id, 'body', 'invoice', cast(:reading as jsonb))
                """
            ),
            {"case_id": case_id, "reading": json.dumps(reading)},
        )


def new_case(app: Engine, kind: CaseKind, number: str, cents: int, **extra: Any) -> int:
    with unit_of_work(app, INTAKE) as db:
        return create_case(
            db, kind, document_number=number, gross_cents=cents, currency="EUR", **extra
        )


def listed(client: TestClient, entra: FakeEntra, **query: str) -> dict[str, Any]:
    response = client.post(SEARCH, json=query, headers=as_role(entra, Role.FINANCE))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def by_id(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {c["case_id"]: c for c in body["cases"]}


# --- AC: a page lists cases with status, vendor or customer, amount and dates ---


def test_a_clean_invoice_shows_as_received_with_vendor_amount_and_dates(
    app: Engine, client: TestClient, entra: FakeEntra, store: MemoryDocumentStore, tag: str
) -> None:
    email = received_email(app, store)
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email.email_id,
            document_number=f"RE-{tag}",
            gross_cents=119000,
            currency="EUR",
        )
    read_on_body(app, case_id, issuer_name=VENDOR, invoice_date="2026-11-09", due_date="2026-11-23")

    row = by_id(listed(client, entra, q=tag))[case_id]

    assert row["status"] == "received"
    assert row["status_text"] == "Received"
    assert row["kind"] == "incoming_invoice"
    assert row["partner_name"] == VENDOR
    assert row["sender"] == "lieferant-test.example"
    assert row["document_number"] == f"RE-{tag}"
    assert (row["gross_cents"], row["currency"]) == (119000, "EUR")
    assert row["received_at"].startswith("2026-11-10T08:00:00")
    assert (row["invoice_date"], row["due_date"]) == ("2026-11-09", "2026-11-23")
    assert row["exception_id"] is None


def test_every_status_shows_and_an_outgoing_invoice_names_the_customer(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    received = new_case(app, CaseKind.INCOMING_INVOICE, f"A-{tag}", 10000)
    waiting = new_case(
        app, CaseKind.INCOMING_INVOICE, f"B-{tag}", 500000, duplicate_key=f"dup-B-{tag}"
    )
    outgoing = new_case(app, CaseKind.OUTGOING_INVOICE, f"C-{tag}", 2500)
    # A credit note that was rejected while it waited for approval is closed.
    closed = new_case(
        app, CaseKind.INCOMING_CREDIT_NOTE, f"D-{tag}", 999, duplicate_key=f"dup-D-{tag}"
    )
    read_on_body(app, outgoing, issuer_name="Max ASP Test GmbH", recipient_name=CUSTOMER)
    with unit_of_work(app, INTAKE) as db:
        assert move_case(db, waiting, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
        assert move_case(
            db,
            outgoing,
            expected=CaseStatus.RECEIVED,
            to=CaseStatus.BOOKED,
            datev_booking_ref=f"DATEV-{tag}",
        )
        assert move_case(db, closed, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
        assert move_case(
            db,
            closed,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )

    rows = by_id(listed(client, entra, q=tag))

    assert rows[received]["status"] == "received"
    assert rows[waiting]["status_text"] == "Awaiting approval"
    assert rows[outgoing]["status"] == "booked"
    assert rows[outgoing]["partner_name"] == CUSTOMER  # the customer, not Max ASP itself
    assert rows[closed]["status"] == "closed"
    assert rows[closed]["closed_reason"] == "rejected"
    # A case with no email came in when it was created.
    assert rows[received]["sender"] is None
    assert rows[received]["received_at"] is not None


def test_the_newest_case_comes_first(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    first = new_case(app, CaseKind.INCOMING_INVOICE, f"OLD-{tag}", 100)
    second = new_case(app, CaseKind.INCOMING_INVOICE, f"NEW-{tag}", 100)

    ids = [c["case_id"] for c in listed(client, entra, q=tag)["cases"]]

    assert ids == [second, first]


def test_a_bank_transaction_shows_the_amount_of_the_transaction(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    with unit_of_work(app, Actor.system("bank")) as db:
        case_id = create_case(db, CaseKind.BANK_TRANSACTION, partner_account=f"WISE-{tag}")
        db.execute(
            text(
                """
                insert into bank_transaction (case_id, wise_balance_currency,
                                              wise_transaction_id, booked_on, amount_minor)
                values (:id, 'USD', :tx, date '2026-11-12', -4250)
                """
            ),
            {"id": case_id, "tx": f"TX-{tag}"},
        )

    row = by_id(listed(client, entra, q=tag, kind="bank_transaction"))[case_id]

    assert (row["gross_cents"], row["currency"]) == (-4250, "USD")


# --- AC: the exception queue is shown with its reason ---


def test_a_case_in_exception_links_to_its_open_exception_with_the_reason(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    case_id = new_case(app, CaseKind.INCOMING_INVOICE, f"X-{tag}", 892500)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db,
            ExceptionReason.INVOICE_CHECK_FAILED,
            "The invoice has no delivery date (§14 UStG).",
            case_id=case_id,
        )

    row = by_id(listed(client, entra, q=tag))[case_id]

    assert row["status"] == "exception"
    assert row["resume_status"] == "received"
    assert row["exception_id"] == exception_id
    assert row["exception_reason"] == "invoice_check_failed"
    assert row["exception_text"] == "The invoice failed a required check"


# --- Filters ---


def test_the_filters_narrow_the_list_and_combine(
    app: Engine, client: TestClient, entra: FakeEntra, store: MemoryDocumentStore, tag: str
) -> None:
    email = received_email(app, store)  # came in on 10 Nov 2026
    with unit_of_work(app, INTAKE) as db:
        from_email = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email.email_id,
            document_number=f"M-{tag}",
            gross_cents=100,
            currency="EUR",
        )
    credit = new_case(app, CaseKind.INCOMING_CREDIT_NOTE, f"N-{tag}", 200)
    waiting = new_case(
        app,
        CaseKind.INCOMING_INVOICE,
        f"O-{tag}",
        300,
        partner_account="70042",
        duplicate_key=f"dup-O-{tag}",
    )
    with unit_of_work(app, INTAKE) as db:
        move_case(db, waiting, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)

    def ids(**query: str) -> set[int]:
        return set(by_id(listed(client, entra, q=tag, **query)))

    assert ids() == {from_email, credit, waiting}
    assert ids(status="awaiting_approval") == {waiting}
    assert ids(kind="incoming_credit_note") == {credit}
    assert ids(received_from="2026-11-10", received_to="2026-11-10") == {from_email}
    assert ids(received_to="2026-11-09") == set()
    assert ids(status="received", kind="incoming_invoice") == {from_email}
    # The search also finds the DATEV account, the sender domain and the case number.
    assert set(by_id(listed(client, entra, q="70042"))) >= {waiting}
    assert set(by_id(listed(client, entra, q="lieferant-test"))) >= {from_email}
    assert by_id(listed(client, entra, q=f"#{credit}")).keys() >= {credit}


def test_the_status_counts_follow_the_other_filters_but_not_the_status(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    new_case(app, CaseKind.INCOMING_INVOICE, f"P-{tag}", 100)
    waiting = new_case(
        app, CaseKind.INCOMING_INVOICE, f"Q-{tag}", 100, duplicate_key=f"dup-Q-{tag}"
    )
    with unit_of_work(app, INTAKE) as db:
        move_case(db, waiting, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)

    body = listed(client, entra, q=tag, status="awaiting_approval")
    counts = {s["status"]: s["count"] for s in body["statuses"]}

    assert [c["case_id"] for c in body["cases"]] == [waiting]
    assert counts["received"] == 1 and counts["awaiting_approval"] == 1
    assert set(counts) == {s.value for s in CaseStatus}  # every tab, also the empty ones
    assert body["total"] == 1
    assert body["truncated"] is False


def test_a_search_word_is_taken_literally(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    new_case(app, CaseKind.INCOMING_INVOICE, f"R-{tag}", 100)

    assert listed(client, entra, q=f"{tag}%")["cases"] == []
    assert listed(client, entra, q=f"R_{tag[1:]}")["cases"] == []
    assert listed(client, entra, q="' or 1=1 --")["cases"] == []


def test_a_filter_that_is_not_a_status_kind_or_date_is_refused(
    client: TestClient, entra: FakeEntra
) -> None:
    finance = as_role(entra, Role.FINANCE)
    for query in (
        {"status": "lost"},
        {"kind": "receipt"},
        {"received_from": "10.11.2026"},
        {"q": "x" * 101},
        {"vendor": "anything"},
    ):
        assert client.post(SEARCH, json=query, headers=finance).status_code == 422, query


def test_a_long_list_stops_at_the_limit_and_says_so(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    with unit_of_work(app, INTAKE) as db:
        for n in range(MAX_ROWS + 2):
            create_case(db, CaseKind.INCOMING_INVOICE, document_number=f"L{n}-{tag}")

    body = listed(client, entra, q=tag)

    assert len(body["cases"]) == MAX_ROWS
    assert body["truncated"] is True
    assert body["total"] == MAX_ROWS + 2


# --- AC: only finance and admin see all cases ---


def test_finance_and_admin_see_every_case(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    case_id = new_case(app, CaseKind.INCOMING_INVOICE, f"S-{tag}", 100)

    for role, who in ((Role.FINANCE, BENJAMIN), (Role.ADMIN, ARITA)):
        response = client.post(SEARCH, json={"q": tag}, headers=as_role(entra, role, who))
        assert response.status_code == 200
        assert [c["case_id"] for c in response.json()["cases"]] == [case_id]


def test_an_approver_or_a_person_without_a_role_gets_no_list(
    client: TestClient, entra: FakeEntra
) -> None:
    assert client.post(SEARCH, headers=as_role(entra, Role.APPROVER, ALICE)).status_code == 403
    assert client.post(SEARCH, headers=as_role(entra, None, ALICE)).status_code == 403
    assert client.post(SEARCH).status_code == 401


def test_the_log_names_no_vendor_customer_or_search_word(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    captured: pytest.LogCaptureFixture,
    tag: str,
) -> None:
    case_id = new_case(app, CaseKind.INCOMING_INVOICE, f"T-{tag}", 100)
    read_on_body(app, case_id, issuer_name=VENDOR)

    listed(client, entra, q=tag)
    listed(client, entra, q="Mustermann")

    logged = "\n".join(r.getMessage() for r in captured.records)
    assert f"user {BENJAMIN} listed cases" in logged
    for personal in ("Mustermann", "Erika", tag):
        assert personal not in logged

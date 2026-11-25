"""The share of invoices from known vendors that needed no manual work (#64, NF-07).

Cases from other tests stay in the database, so each test works in a year of its own, far in
the future, where only its own emails come in. Every vendor account and number is made up.
"""

import secrets
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, ExceptionReason, Outcome
from argus.cases.store import (
    create_case,
    open_exception,
    resolve_exception,
    unit_of_work,
)
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from argus.intake.sweep import SWEEP_ACTOR
from argus.invoices_page.rate import MonthRate, month_rate, straight_through
from argus.invoices_page.web import RATE_PATH
from standins.entra.app import FakeEntra
from tests.integration.approval_helpers import send_for_approval

# Exceptions open only once ARGUS runs live (#72).
pytestmark = [pytest.mark.req("NF-07"), pytest.mark.usefixtures("live")]

INTAKE = Actor.system("intake")
BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up


@pytest.fixture
def year() -> int:
    """A year no other test or run uses, so the counts are this test's alone."""
    return 2200 + secrets.randbelow(6800)


def email_at(app: Engine, received_at: datetime) -> int:
    with unit_of_work(app, SWEEP_ACTOR) as db:
        record, _ = see_email(
            db,
            InboxMessage(
                id=f"AAMk-{uuid.uuid4().hex}",
                internet_message_id=f"<{uuid.uuid4().hex}@lieferant-test.example>",
                received_at=received_at,
            ),
        )
    return record.id


def invoice(
    app: Engine,
    received_at: datetime,
    *,
    account: str | None = "70001",
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
    email_id: int | None = None,
    duplicate_key: str | None = None,
) -> int:
    """A case whose email came in at ``received_at`` (UTC)."""
    source = email_id if email_id is not None else email_at(app, received_at)
    with unit_of_work(app, INTAKE) as db:
        return create_case(
            db,
            kind,
            source_email_id=source,
            partner_account=account,
            document_number=f"RE-{uuid.uuid4().hex[:8]}",
            gross_cents=11900,
            currency="EUR",
            duplicate_key=duplicate_key,
        )


def exception_on(app: Engine, case_id: int, reason: ExceptionReason, *, resolve: bool) -> None:
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, reason, "made up detail", case_id=case_id)
    if resolve:
        with unit_of_work(app, Actor.user(BENJAMIN, "Finance Test")) as db:
            resolve_exception(
                db,
                exception_id,
                {
                    ExceptionReason.UNKNOWN_VENDOR: Outcome.FIXED_IN_DATEV,
                    ExceptionReason.DUPLICATE: Outcome.NOT_A_DUPLICATE,
                }.get(reason, Outcome.ACCEPTED_WITH_COMMENT),
                resolution="checked",
            )


def rate(app: Engine, year: int, month: int) -> MonthRate:
    with app.connect() as connection:
        return month_rate(connection, year, month)


# --- AC: the share of invoices from known vendors that never entered the exception queue ---


def test_a_fixed_set_of_cases_gives_the_share_without_manual_work(app: Engine, year: int) -> None:
    day = datetime(year, 2, 10, 9, 0, tzinfo=UTC)
    # Nine clean invoices from known vendors.
    for _ in range(9):
        invoice(app, day)
    # One with an exception that someone resolved: a manual step, even though it is done.
    resolved = invoice(app, day)
    exception_on(app, resolved, ExceptionReason.DUPLICATE, resolve=True)
    # One still in the queue.
    still_open = invoice(app, day)
    exception_on(app, still_open, ExceptionReason.IBAN_MISMATCH, resolve=False)

    # Left out: not from a known vendor.
    invoice(app, day, account=None)  # never matched
    unknown = invoice(app, day)  # matched only after a person looked at it
    exception_on(app, unknown, ExceptionReason.UNKNOWN_VENDOR, resolve=True)
    # Left out: not an incoming invoice.
    invoice(app, day, kind=CaseKind.INCOMING_CREDIT_NOTE)
    invoice(app, day, kind=CaseKind.OUTGOING_INVOICE, account="10001")

    found = rate(app, year, 2)

    assert (found.straight, found.total) == (9, 11)
    assert found.percent == 81  # 81.8 rounded down
    assert found.below_target
    assert (found.month, found.label) == (f"{year:04d}-02", f"February {year}")


def test_an_exception_on_the_email_before_the_case_counts_as_manual_work(
    app: Engine, year: int
) -> None:
    day = datetime(year, 2, 10, 9, 0, tzinfo=UTC)
    email = email_at(app, day)
    with unit_of_work(app, INTAKE) as db:
        open_exception(
            db, ExceptionReason.UNCERTAIN_CLASSIFICATION, "made up detail", email_id=email
        )
    invoice(app, day, email_id=email)
    invoice(app, day)

    found = rate(app, year, 2)

    assert (found.straight, found.total) == (1, 2)


def test_approval_is_not_manual_work_and_exactly_90_percent_meets_the_target(
    app: Engine, year: int
) -> None:
    day = datetime(year, 2, 10, 9, 0, tzinfo=UTC)
    cases = [invoice(app, day, duplicate_key=f"dup-{uuid.uuid4().hex}") for _ in range(10)]
    # Waiting for approval, with the approval request emailed: not an exception item, so the
    # case still counts as one without manual work.
    with unit_of_work(app, INTAKE) as db:
        assert send_for_approval(db, cases[0])
    exception_on(app, cases[1], ExceptionReason.BOOKING_ERROR, resolve=False)

    found = rate(app, year, 2)

    assert (found.straight, found.total, found.percent) == (9, 10, 90)
    assert not found.below_target


def test_the_month_boundary_follows_berlin_time(app: Engine, year: int) -> None:
    # 31 January 22:30 UTC is 23:30 in Berlin (winter time): still January.
    invoice(app, datetime(year, 1, 31, 22, 30, tzinfo=UTC))
    # 31 January 23:30 UTC is already 1 February 00:30 in Berlin.
    late = invoice(app, datetime(year, 1, 31, 23, 30, tzinfo=UTC))
    exception_on(app, late, ExceptionReason.BOOKING_ERROR, resolve=False)
    # Summer time: 31 July 22:30 UTC is 1 August 00:30 in Berlin.
    invoice(app, datetime(year, 7, 31, 22, 30, tzinfo=UTC))

    with app.connect() as connection:
        current, last = straight_through(connection, datetime(year, 2, 1, 0, 30, tzinfo=UTC))

    assert (current.label, current.straight, current.total) == (f"February {year}", 0, 1)
    assert (last.label, last.straight, last.total) == (f"January {year}", 1, 1)
    assert (rate(app, year, 7).total, rate(app, year, 8).total) == (0, 1)


def test_a_month_without_invoices_has_no_share_and_no_warning(app: Engine, year: int) -> None:
    found = rate(app, year, 3)

    assert (found.straight, found.total, found.percent, found.below_target) == (0, 0, None, False)


def test_the_last_month_of_a_year_comes_before_january(app: Engine, year: int) -> None:
    invoice(app, datetime(year - 1, 12, 15, 12, 0, tzinfo=UTC))

    with app.connect() as connection:
        current, last = straight_through(connection, datetime(year, 1, 5, 12, 0, tzinfo=UTC))

    assert (current.month, last.month) == (f"{year:04d}-01", f"{year - 1:04d}-12")
    assert last.total == 1


# --- AC: finance and admin see the figure on the invoices page ---


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    return TestClient(create_app(lambda: app, sign_in=entra.verifier))


def headers(entra: FakeEntra, role: Role | None) -> dict[str, str]:
    token = entra.token(BENJAMIN, roles=[role] if role else [])
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", [Role.FINANCE, Role.ADMIN])
def test_finance_and_admin_get_the_current_and_the_last_month(
    client: TestClient, entra: FakeEntra, role: Role
) -> None:
    response = client.get(RATE_PATH, headers=headers(entra, role))

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["target"] == 90
    assert len(body["months"]) == 2
    for month in body["months"]:
        assert set(month) == {"month", "label", "straight", "total", "percent", "below_target"}
    now = datetime.now(UTC)
    assert body["months"][0]["month"] in {f"{now.year:04d}-{now.month:02d}"} | {
        # Just after midnight on the 1st in Berlin, UTC is still in the month before.
        f"{now.year + (now.month == 12):04d}-{now.month % 12 + 1:02d}"
    }


def test_an_approver_or_someone_without_a_role_is_refused(
    client: TestClient, entra: FakeEntra
) -> None:
    assert client.get(RATE_PATH, headers=headers(entra, Role.APPROVER)).status_code == 403
    assert client.get(RATE_PATH, headers=headers(entra, None)).status_code == 403
    assert client.get(RATE_PATH).status_code == 401

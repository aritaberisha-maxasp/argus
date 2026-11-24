"""The invoice case store against a real PostgreSQL (issue #19, ADR 0005).

The first six groups match the acceptance criteria of #19. The middle part turns the review
checks of docs/design/invoice-case/checks.sql into tests, so no later story can loosen a rule
unnoticed. The last group matches the acceptance criteria of #21, the exception queue.
"""

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from psycopg import errors as pg
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
    Outcome,
)
from argus.cases.money import from_cents, to_cents
from argus.cases.store import (
    CaseRuleBroken,
    DuplicateRefused,
    NotAllowed,
    change_case,
    correct_case,
    create_case,
    get_case,
    list_open_exceptions,
    move_case,
    open_exception,
    record_event,
    resolve_exception,
    trace,
    unit_of_work,
    verify_chain,
)
from tests.integration.approval_helpers import prepare_for_approval
from tests.integration.conftest import head, migrate

S = CaseStatus
INTAKE = Actor.system("intake")
APPROVER = Actor.user("approver-entra-id", "Approver Test")
BENJAMIN = Actor.user("benjamin-entra-id", "Benjamin Test", "benjamin@max-asp-test.example")

# The vendor accounts, numbers and amounts below are made up (CLAUDE.md: synthetic data only).
_counter = iter(range(1, 1_000_000))


def statement(app: Engine, outcome: str = "imported") -> int:
    """A statement of the EUR balance for November 2026 (#56): since migration 0014 a bank
    transaction belongs to an imported statement of its currency and month. An imported one
    is reused, because there is only one per month."""
    with unit_of_work(app, Actor.system("bank-import")) as db:
        if outcome == "imported":
            found = db.execute(
                text(
                    "select id from bank_statement where currency = 'EUR'"
                    " and period = '2026-11-01' and outcome = 'imported'"
                )
            ).scalar_one_or_none()
            if found is not None:
                return int(found)
        sha = f"{next(_counter):064x}"
        statement_id: int = db.execute(
            text(
                "insert into bank_statement (sha256, location, size_bytes, source, currency,"
                " period, opening_minor, closing_minor, transaction_count, outcome)"
                " values (:sha, 'sha256/' || :sha, 1, 'folder', 'EUR', '2026-11-01', 0, 0, 1,"
                " :outcome) returning id"
            ),
            {"sha": sha, "outcome": outcome},
        ).scalar_one()
    return statement_id


def new_case(app: Engine, kind: CaseKind = CaseKind.INCOMING_INVOICE, **fields: object) -> int:
    n = next(_counter)
    defaults: dict[str, object] = {
        "source_email_id": n,
        "partner_account": "70001",
        "document_number": f"RE-TEST-{n}",
        "gross_cents": 892500,
        "currency": "EUR",
    }
    if kind in (CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE):
        # Checked for duplicates, as intake does (#31): no approval without a duplicate key.
        defaults["duplicate_key"] = f"vat:DE700000000|RETEST{n}|892500|EUR"
    with unit_of_work(app, INTAKE) as db:
        return create_case(db, kind, **{**defaults, **fields})  # type: ignore[arg-type]


def walk(app: Engine, case_id: int, *statuses: CaseStatus, actor: Actor = INTAKE) -> None:
    """Move a case step by step, each step in its own transaction. Approval is given by a
    person and booking sets a DATEV reference, as the database requires."""
    for to in statuses:
        with unit_of_work(app, APPROVER if to is S.APPROVED else actor) as db:
            case = get_case(db, case_id)
            if to is S.AWAITING_APPROVAL and case.status is S.RECEIVED:
                # Proposal, duplicate key and approval request first, as the sweep does (#191).
                prepare_for_approval(db, case_id)
            ref = f"DATEV-TEST-{case_id}" if to is S.BOOKED and not case.datev_booking_ref else None
            assert move_case(db, case_id, expected=case.status, to=to, datev_booking_ref=ref)


def booked_invoice(app: Engine) -> int:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED)
    return case_id


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with unit_of_work(app, INTAKE) as db:
        return get_case(db, case_id).status


def events_of(app: Engine, case_id: int) -> list[str]:
    with unit_of_work(app, INTAKE) as db:
        return [e.event_type for e in trace(db, case_id) if e.case_id == case_id]


# 1. Migrations create the tables from the ADR 0005 sketch


@pytest.mark.req("NF-01", "NF-05")
def test_migrations_create_the_case_store_tables(owner: Engine) -> None:
    with owner.connect() as db:
        tables: set[str] = set(
            db.execute(
                text(
                    "select table_name from information_schema.tables where table_schema = 'public'"
                )
            ).scalars()
        )

    assert {
        "invoice_case",
        "case_status_transition",
        "audit_event",
        "exception_item",
        "exception_exit",
        "bank_transaction",
        "case_trace",
    } <= tables


@pytest.mark.req("NF-05")
def test_python_names_match_the_database(owner: Engine) -> None:
    def enum_values(name: str) -> list[str]:
        with owner.connect() as db:
            return list(
                db.execute(text(f"select unnest(enum_range(null::{name}))::text")).scalars()
            )

    assert enum_values("case_kind") == [k.value for k in CaseKind]
    assert enum_values("case_status") == [s.value for s in CaseStatus]
    assert enum_values("exception_reason") == [r.value for r in ExceptionReason]


@pytest.mark.req("NF-01")
def test_migrations_refuse_to_drop_a_filled_audit_log(owner: Engine, app: Engine) -> None:
    new_case(app)

    with pytest.raises(pg.RaiseException, match="kept for ten years"):
        migrate(owner, "base", down=True)

    with owner.connect() as db:
        assert db.execute(text("select version_num from alembic_version")).scalar() == head()


# 2. A move that is not in the transition table is refused


@pytest.mark.req("NF-05")
def test_skipping_from_approved_straight_to_paid_is_refused(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED)

    with (
        pytest.raises(CaseRuleBroken, match="approved -> paid not allowed"),
        unit_of_work(app, INTAKE) as db,
    ):
        move_case(db, case_id, expected=S.APPROVED, to=S.PAID)

    assert status_of(app, case_id) is S.APPROVED


@pytest.mark.req("NF-05")
def test_the_refusal_comes_from_the_database_not_from_python(app: Engine) -> None:
    case_id = new_case(app, CaseKind.OUTGOING_INVOICE)

    # Outgoing invoices never go through approval (AR-02).
    with (
        pytest.raises(DBAPIError, match="received -> awaiting_approval not allowed"),
        app.begin() as db,
    ):
        db.execute(text("set local argus.actor = 'system:test'"))
        db.execute(
            text("update invoice_case set status = 'awaiting_approval' where id = :id"),
            {"id": case_id},
        )


@pytest.mark.req("NF-05")
def test_every_kind_follows_its_own_path(app: Engine) -> None:
    outgoing = new_case(app, CaseKind.OUTGOING_INVOICE)
    walk(app, outgoing, S.BOOKED, S.PAID)
    bank = new_case(app, CaseKind.BANK_TRANSACTION)
    walk(app, bank, S.BOOKED)

    with pytest.raises(CaseRuleBroken, match="not allowed for bank_transaction"):
        walk(app, bank, S.PAID)


@pytest.mark.req("NF-05")
def test_a_worker_that_lost_the_race_changes_nothing(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)

    with unit_of_work(app, INTAKE) as db:
        moved = move_case(db, case_id, expected=S.RECEIVED, to=S.AWAITING_APPROVAL)

    assert moved is False
    assert events_of(app, case_id).count("status_changed") == 1


# 3. A status change without an actor is refused


@pytest.mark.req("NF-01")
def test_a_status_change_without_an_actor_is_refused(app: Engine) -> None:
    case_id = new_case(app)

    with pytest.raises(DBAPIError, match=r"argus\.actor is not set"), app.begin() as db:
        db.execute(
            text("update invoice_case set status = 'awaiting_approval' where id = :id"),
            {"id": case_id},
        )

    assert status_of(app, case_id) is S.RECEIVED


@pytest.mark.req("NF-01")
def test_a_new_case_without_an_actor_is_refused(app: Engine) -> None:
    with pytest.raises(DBAPIError, match=r"argus\.actor is not set"), app.begin() as db:
        db.execute(text("insert into invoice_case (kind) values ('incoming_invoice')"))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("actor", ["", "intake", "robot:intake", "system:", "user:", "user: "])
def test_a_malformed_actor_is_refused(app: Engine, actor: str) -> None:
    case_id = new_case(app)

    with pytest.raises(DBAPIError, match=r"argus\.actor is not set"), app.begin() as db:
        db.execute(text("select set_config('argus.actor', :a, true)"), {"a": actor})
        db.execute(
            text("update invoice_case set status = 'awaiting_approval' where id = :id"),
            {"id": case_id},
        )


@pytest.mark.req("NF-01")
def test_python_refuses_an_actor_without_a_name() -> None:
    with pytest.raises(ValueError, match="non empty name"):
        Actor.user(" ")
    assert str(Actor.system("intake")) == "system:intake"


# 4. Every status change writes an audit event in the same transaction


@pytest.mark.req("NF-01", "NF-05")
def test_every_status_change_is_logged_with_actor_and_statuses(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, actor=Actor.system("approval"))

    with unit_of_work(app, INTAKE) as db:
        events = [e for e in trace(db, case_id) if e.case_id == case_id]

    assert [(e.event_type, e.actor, e.from_status, e.to_status) for e in events] == [
        ("case_created", "system:intake", None, S.RECEIVED),
        # Since #191 a case goes for approval only with a proposal and an approval request.
        ("account_proposed", "system:approval-request", None, None),
        ("approval_requested", "system:approval-request", None, None),
        ("status_changed", "system:approval", S.RECEIVED, S.AWAITING_APPROVAL),
    ]


@pytest.mark.req("NF-01")
def test_the_audit_event_is_written_in_the_same_transaction(app: Engine, owner: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)

    with owner.connect() as db:
        case_xmin = db.execute(
            text("select xmin::text from invoice_case where id = :id"), {"id": case_id}
        ).scalar()
        event_xmin = db.execute(
            text(
                "select xmin::text from audit_event where case_id = :id "
                "and event_type = 'status_changed'"
            ),
            {"id": case_id},
        ).scalar()

    assert case_xmin == event_xmin


@pytest.fixture
def failing_audit(owner: Engine) -> Iterator[None]:
    """Makes every status_changed audit write fail, as a full disk would, then removes itself."""
    with owner.begin() as db:
        db.execute(
            text(
                "create function test_fail_audit() returns trigger language plpgsql as "
                "$$ begin raise exception 'audit write failed (test)'; end $$"
            )
        )
        db.execute(
            text(
                "create trigger test_fail_audit before insert on audit_event for each row "
                "when (new.event_type = 'status_changed') execute function test_fail_audit()"
            )
        )
    yield
    with owner.begin() as db:
        db.execute(text("drop trigger test_fail_audit on audit_event"))
        db.execute(text("drop function test_fail_audit()"))


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("failing_audit")
def test_when_the_audit_write_fails_the_status_change_is_undone(app: Engine) -> None:
    case_id = new_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="audit write failed"),
        unit_of_work(app, INTAKE) as db,
    ):
        prepare_for_approval(db, case_id)
        move_case(db, case_id, expected=S.RECEIVED, to=S.AWAITING_APPROVAL)

    assert status_of(app, case_id) is S.RECEIVED
    assert events_of(app, case_id) == ["case_created"]


@pytest.mark.req("NF-01")
def test_a_correction_before_approval_is_logged_with_old_and_new_value(app: Engine) -> None:
    case_id = new_case(app, gross_cents=10000)
    walk(app, case_id, S.AWAITING_APPROVAL)

    with unit_of_work(app, Actor.user("arita-entra-id", "Arita Test")) as db:
        case = get_case(db, case_id)
        correct_case(
            db,
            case_id,
            document_number=str(case.document_number),
            gross_cents=11900,
            currency=str(case.currency),
            reason="amount checked against the invoice",
            name="Arita",
        )
        changed = [e for e in trace(db, case_id) if e.event_type == "case_changed"]

    assert len(changed) == 1
    assert changed[0].actor == "user:arita-entra-id"
    # The corrected amount also clears the duplicate key, so the case is checked again (#31).
    assert changed[0].details["gross_minor"] == {"old": 10000, "new": 11900}
    assert changed[0].details["duplicate_key"]["new"] is None
    assert set(changed[0].details) == {"gross_minor", "duplicate_key"}


# 5. Audit events cannot be updated or deleted by the application's database user


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "statement",
    [
        "update audit_event set actor = 'someone else'",
        "delete from audit_event",
        "truncate audit_event",
    ],
)
def test_the_app_user_cannot_change_or_delete_audit_events(app: Engine, statement: str) -> None:
    new_case(app)

    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(text(statement))


@pytest.mark.req("NF-01")
def test_even_the_owner_is_stopped_by_the_append_only_trigger(owner: Engine, app: Engine) -> None:
    new_case(app)

    with pytest.raises(DBAPIError, match="append only"), owner.begin() as db:
        db.execute(text("update audit_event set actor = 'someone else'"))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "event_type", ["case_created", "status_changed", "case_changed", "exception_resolved"]
)
def test_the_app_cannot_write_events_only_the_database_writes(app: Engine, event_type: str) -> None:
    case_id = new_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, INTAKE) as db,
    ):
        record_event(db, event_type, case_id=case_id)


@pytest.mark.req("NF-01")
def test_the_app_cannot_name_its_own_actor_or_time(app: Engine) -> None:
    case_id = new_case(app)

    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(
            text(
                "insert into audit_event (case_id, event_type, actor_type, actor, occurred_at) "
                "values (:id, 'approval_decision', 'user', 'benjamin', '2020-01-01')"
            ),
            {"id": case_id},
        )


# 6. Money is stored as whole cents


@pytest.mark.req("NF-01", "BK-07")
def test_eur_5000_01_is_stored_as_500001_cents(app: Engine, owner: Engine) -> None:
    cents = to_cents("5000.01")
    case_id = new_case(app, gross_cents=cents, currency="EUR")

    with unit_of_work(app, INTAKE) as db:
        stored = get_case(db, case_id)
    with owner.connect() as db:
        column_type = db.execute(
            text(
                "select data_type from information_schema.columns "
                "where table_name = 'invoice_case' and column_name = 'gross_minor'"
            )
        ).scalar()

    assert cents == 500001
    assert stored.gross_cents == 500001
    assert from_cents(stored.gross_cents) == Decimal("5000.01")
    assert column_type == "bigint"


@pytest.mark.req("NF-01")
def test_a_float_amount_is_refused(app: Engine) -> None:
    with pytest.raises(TypeError):
        to_cents(5000.01)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="whole cents"):
        new_case(app, gross_cents=5000.01)
    with pytest.raises(ValueError, match="more than two decimals"):
        to_cents("5000.001")


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    ("amount", "cents"),
    [
        ("0.01", 1),
        ("5000.00", 500000),
        ("-892.50", -89250),
        (Decimal("1234567.89"), 123456789),
        (7, 700),
    ],
)
def test_amounts_convert_to_cents_exactly(amount: Decimal | str | int, cents: int) -> None:
    assert to_cents(amount) == cents
    assert from_cents(cents) == Decimal(amount)


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("amount", ["", "abc", "NaN", "Infinity", True])
def test_things_that_are_not_amounts_are_refused(amount: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        to_cents(amount)  # type: ignore[arg-type]


# The booking reference (BK-07)


@pytest.mark.req("BK-07")
def test_the_datev_booking_reference_is_stored_once(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED)

    with (
        pytest.raises(CaseRuleBroken, match="booked_needs_datev_reference"),
        unit_of_work(app, Actor.system("booking")) as db,
    ):
        move_case(db, case_id, expected=S.APPROVED, to=S.BOOKED)

    with unit_of_work(app, Actor.system("booking")) as db:
        move_case(db, case_id, expected=S.APPROVED, to=S.BOOKED, datev_booking_ref="DATEV-TEST-1")

    with unit_of_work(app, INTAKE) as db:
        assert get_case(db, case_id).datev_booking_ref == "DATEV-TEST-1"
        booked = next(e for e in trace(db, case_id) if e.to_status is S.BOOKED)
    assert booked.details == {"datev_booking_ref": {"old": None, "new": "DATEV-TEST-1"}}

    with pytest.raises(CaseRuleBroken, match="already set"), unit_of_work(app, INTAKE) as db:
        db.execute(
            text("update invoice_case set datev_booking_ref = 'DATEV-OTHER' where id = :id"),
            {"id": case_id},
        )


# Approval decisions are logged (AP-08)


@pytest.mark.req("AP-08")
def test_an_approval_decision_is_logged_under_the_approver(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)

    with unit_of_work(app, APPROVER) as db:
        record_event(
            db,
            "approval_decision",
            case_id=case_id,
            details={"step": "first", "decision": "approved"},
        )
        move_case(db, case_id, expected=S.AWAITING_APPROVAL, to=S.APPROVED)
        decision = next(e for e in trace(db, case_id) if e.event_type == "approval_decision")

    assert decision.actor == "user:approver-entra-id"
    assert decision.details == {"step": "first", "decision": "approved"}


# The rest of the ADR 0005 rules (from docs/design/invoice-case/checks.sql)


@pytest.mark.req("EX-01")
def test_an_exception_stops_the_case_and_it_goes_back_where_it_came_from(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED)

    with unit_of_work(app, Actor.system("booking")) as db:
        exception_id = open_exception(
            db,
            ExceptionReason.BOOKING_ERROR,
            "period 10/2026 is closed in DATEV",
            case_id=case_id,
            facts={"datev_error": "period_closed"},
        )
    assert status_of(app, case_id) is S.EXCEPTION

    with (
        pytest.raises(CaseRuleBroken, match="open exception"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        move_case(db, case_id, expected=S.EXCEPTION, to=S.APPROVED)

    with (
        pytest.raises(CaseRuleBroken, match="must go back to approved"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(
            db, exception_id, Outcome.RETRIED, resolution="period reopened", case_to=S.BOOKED
        )

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.RETRIED, resolution="period reopened")
    assert status_of(app, case_id) is S.APPROVED
    assert events_of(app, case_id)[-4:] == [
        "exception_opened",
        "status_changed",
        "exception_resolved",
        "status_changed",
    ]


@pytest.mark.req("EX-01")
def test_going_to_exception_without_an_open_exception_is_refused(app: Engine) -> None:
    case_id = new_case(app)

    with pytest.raises(CaseRuleBroken, match="needs an open exception"):
        walk(app, case_id, S.EXCEPTION)


@pytest.mark.req("EX-01")
def test_an_open_exception_that_does_not_stop_its_case_cannot_be_committed(app: Engine) -> None:
    case_id = new_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="open exception but is in received"),
        unit_of_work(app, INTAKE) as db,
    ):
        db.execute(
            text(
                "insert into exception_item (case_id, reason, detail) "
                "values (:id, 'unknown_vendor', 'not in DATEV')"
            ),
            {"id": case_id},
        )

    assert status_of(app, case_id) is S.RECEIVED


@pytest.mark.req("EX-01")
def test_skipping_approval_through_the_exception_queue_is_refused(app: Engine) -> None:
    case_id = new_case(app)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, ExceptionReason.UNKNOWN_VENDOR, "x", case_id=case_id)

    with (
        pytest.raises(CaseRuleBroken, match="must go back to received"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(
            db, exception_id, Outcome.FIXED_IN_DATEV, resolution="added", case_to=S.APPROVED
        )


@pytest.mark.req("PR-05")
def test_an_iban_mismatch_ends_only_fixed_in_datev_or_rejected(app: Engine) -> None:
    case_id = booked_invoice(app)
    with unit_of_work(app, Actor.system("iban-check")) as db:
        exception_id = open_exception(
            db,
            ExceptionReason.IBAN_MISMATCH,
            "IBAN on invoice differs from DATEV",
            case_id=case_id,
            facts={"iban_last4": "4321", "matches_datev": False},
        )

    with (
        pytest.raises(CaseRuleBroken, match="fixed_in_datev or rejected"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV)
    assert status_of(app, case_id) is S.BOOKED


@pytest.mark.req("PR-05")
def test_a_rejected_iban_mismatch_can_only_be_closed_as_rejected(app: Engine) -> None:
    # Waiting for approval: a booked case is not closed through the queue (migration 0016).
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)
    with unit_of_work(app, Actor.system("iban-check")) as db:
        exception_id = open_exception(db, ExceptionReason.IBAN_MISMATCH, "x", case_id=case_id)

    for to, reason in [(S.AWAITING_APPROVAL, None), (S.CLOSED, ClosedReason.WITHDRAWN)]:
        with (
            pytest.raises(CaseRuleBroken, match="can only be closed as rejected"),
            unit_of_work(app, BENJAMIN) as db,
        ):
            resolve_exception(db, exception_id, Outcome.REJECTED, case_to=to, closed_reason=reason)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    assert status_of(app, case_id) is S.CLOSED

    with (
        pytest.raises(CaseRuleBroken, match="closed and cannot change"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        change_case(db, case_id, payment_hold=True)


@pytest.mark.req("PY-11")
def test_a_failed_payment_goes_back_to_the_pool(app: Engine) -> None:
    case_id = booked_invoice(app)
    walk(app, case_id, S.IN_PAYMENT_RUN, S.PAID)
    with unit_of_work(app, Actor.system("wise-status")) as db:
        exception_id = open_exception(
            db, ExceptionReason.PAYMENT_FAILED, "transfer bounced", case_id=case_id
        )

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.PUT_BACK_IN_POOL,
            resolution="Wise returned it, pay again",
            case_to=S.BOOKED,
        )

    assert status_of(app, case_id) is S.BOOKED


@pytest.mark.req("PY-13", "PY-10")
@pytest.mark.parametrize("field", ["direct_debit", "payment_hold"])
def test_direct_debit_and_held_invoices_never_go_into_a_payment_run(
    app: Engine, field: str
) -> None:
    case_id = booked_invoice(app)

    with (
        pytest.raises(CaseRuleBroken, match="cannot go into a payment run"),
        unit_of_work(app, Actor.system("payment-run")) as db,
    ):
        change_case(db, case_id, **{field: True})
        move_case(db, case_id, expected=S.BOOKED, to=S.IN_PAYMENT_RUN)


@pytest.mark.req("NF-01")
def test_booking_data_is_frozen_after_approval(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED)

    with pytest.raises(CaseRuleBroken, match="frozen"), unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, gross_cents=99999999, partner_account="79999")


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "statement",
    [
        "update invoice_case set kind = 'outgoing_invoice' where id = :id",
        "update invoice_case set resume_status = 'paid' where id = :id",
        "update invoice_case set updated_at = now() where id = :id",
    ],
)
def test_fields_only_the_database_writes_are_out_of_reach(app: Engine, statement: str) -> None:
    case_id = new_case(app)

    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(text(statement), {"id": case_id})


@pytest.mark.req("IN-10")
def test_the_same_invoice_twice_is_refused(app: Engine) -> None:
    key = f"incoming|70001|RETEST{next(_counter)}|892500|EUR"
    new_case(app, duplicate_key=key)

    with pytest.raises(DuplicateRefused):
        new_case(app, duplicate_key=key)


@pytest.mark.req("IN-10")
def test_a_confirmed_duplicate_can_only_be_closed_as_duplicate(app: Engine) -> None:
    case_id = new_case(app)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.DUPLICATE, "same as before", case_id=case_id
        )

    with (
        pytest.raises(CaseRuleBroken, match="closed as duplicate"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(db, exception_id, Outcome.CONFIRMED_DUPLICATE)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.CONFIRMED_DUPLICATE,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.DUPLICATE,
        )
    assert status_of(app, case_id) is S.CLOSED


@pytest.mark.req("BA-07")
def test_the_same_wise_transaction_twice_is_refused(app: Engine) -> None:
    statement_id = statement(app)

    def import_once() -> None:
        with unit_of_work(app, Actor.system("bank-import")) as db:
            case_id = create_case(db, CaseKind.BANK_TRANSACTION)
            db.execute(
                text(
                    "insert into bank_transaction values (:id, 'EUR', 'TRANSFER-TEST-1', "
                    "'2026-11-03', -892500, :statement_id)"
                ),
                {"id": case_id, "statement_id": statement_id},
            )

    import_once()
    with pytest.raises(DuplicateRefused):
        import_once()


@pytest.mark.req("NF-03", "NF-01")
def test_exception_text_stays_out_of_the_audit_log(app: Engine, owner: Engine) -> None:
    case_id = new_case(app)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.UNKNOWN_VENDOR, "Vendor Musterfirma not in DATEV", case_id=case_id
        )
    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV, resolution="added by hand")

    with owner.connect() as db:
        leaks = db.execute(
            text(
                "select count(*) from audit_event "
                "where details::text like '%Musterfirma%' or details::text like '%by hand%'"
            )
        ).scalar()
    assert leaks == 0


@pytest.mark.req("AC-8")
def test_the_trace_shows_the_email_and_every_step(app: Engine) -> None:
    # The email steps are logged by the database as the mailbox sweep records them (#24);
    # the application cannot write those events itself.
    n = next(_counter)
    with unit_of_work(app, INTAKE) as db:
        email_id: int = db.execute(
            text("insert into mailbox_email (graph_message_id) values (:id) returning id"),
            {"id": f"AAk-trace-{n}"},
        ).scalar_one()
        db.execute(
            text(
                "update mailbox_email set step = 'stored', raw_sha256 = :sha,"
                " raw_location = 'sha256/' || :sha where id = :id"
            ),
            {"id": email_id, "sha": f"{n:064x}"},
        )
        # A stored email has its raw file recorded in the same transaction (#90).
        db.execute(
            text(
                "insert into stored_document (email_id, kind, sha256, location, size_bytes,"
                " content_type) values (:id, 'email', :sha, 'sha256/' || :sha, 1,"
                " 'message/rfc822')"
            ),
            {"id": email_id, "sha": f"{n:064x}"},
        )
    case_id = new_case(app, source_email_id=email_id)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED, S.IN_PAYMENT_RUN, S.PAID)

    with unit_of_work(app, INTAKE) as db:
        events = trace(db, case_id)

    assert [e.event_type for e in events] == [
        "email_seen",
        "email_stored",
        "document_stored",
        "case_created",
        # A case goes for approval with its proposal and approval request (#191).
        "account_proposed",
        "approval_requested",
    ] + ["status_changed"] * 5
    assert events[-1].to_status is S.PAID
    assert all(e.actor and e.occurred_at for e in events)


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("timezone", ["UTC", "Europe/Berlin", "America/New_York"])
def test_the_chain_checks_out_in_any_time_zone(app: Engine, timezone: str) -> None:
    new_case(app)

    with app.connect() as db:
        db.execute(text("select set_config('timezone', :tz, false)"), {"tz": timezone})
        assert verify_chain(db) is None


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "tamper",
    [
        "update audit_event set actor = 'someone-else' where chain_seq = :seq",
        "update audit_event set email_id = case_id, case_id = null where chain_seq = :seq",
        "delete from audit_event where chain_seq = :seq",
    ],
)
def test_a_change_behind_the_triggers_is_found(owner: Engine, app: Engine, tamper: str) -> None:
    # The tampered event has a later event after it. Cutting off the newest events leaves a
    # valid chain; the nightly copy in the locked store is what catches that (design note 3).
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)
    with owner.connect() as db:
        seq = db.execute(
            text("select min(chain_seq) from audit_event where case_id = :id"), {"id": case_id}
        ).scalar()

    # The owner switches the guard off, as an attacker with full rights could. Rolled back after.
    with owner.connect() as db, db.begin() as tx:
        db.execute(text("alter table audit_event disable trigger audit_no_change"))
        db.execute(text(tamper), {"seq": seq})
        broken = verify_chain(db)
        tx.rollback()

    assert seq is not None
    assert broken is not None
    assert broken <= seq + 1


@pytest.mark.req("BK-07")
@pytest.mark.parametrize("kind", [CaseKind.OUTGOING_INVOICE, CaseKind.BANK_TRANSACTION])
def test_no_kind_is_booked_without_a_datev_reference(app: Engine, kind: CaseKind) -> None:
    case_id = new_case(app, kind)

    with (
        pytest.raises(CaseRuleBroken, match="booked_needs_datev_reference"),
        unit_of_work(app, Actor.system("booking")) as db,
    ):
        move_case(db, case_id, expected=S.RECEIVED, to=S.BOOKED)


@pytest.mark.req("AP-08", "NF-05")
def test_a_system_job_cannot_approve(app: Engine) -> None:
    # AP-10: nothing is approved automatically.
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)

    with (
        pytest.raises(CaseRuleBroken, match="only be approved by a person"),
        unit_of_work(app, Actor.system("approval")) as db,
    ):
        move_case(db, case_id, expected=S.AWAITING_APPROVAL, to=S.APPROVED)

    assert status_of(app, case_id) is S.AWAITING_APPROVAL


@pytest.mark.req("BA-07", "NF-01")
def test_a_bank_transaction_import_is_logged(app: Engine) -> None:
    statement_id = statement(app)
    with unit_of_work(app, Actor.system("bank-import")) as db:
        case_id = create_case(db, CaseKind.BANK_TRANSACTION)
        db.execute(
            text(
                "insert into bank_transaction values (:id, 'EUR', 'TRANSFER-TEST-LOG', "
                "'2026-11-03', -892500, :statement_id)"
            ),
            {"id": case_id, "statement_id": statement_id},
        )
        imported = [e for e in trace(db, case_id) if e.event_type == "bank_transaction_imported"]

    assert len(imported) == 1
    assert imported[0].actor == "system:bank-import"
    assert imported[0].details == {
        "wise_transaction_id": "TRANSFER-TEST-LOG",
        "wise_balance_currency": "EUR",
        "amount_minor": -892500,
        "booked_on": "2026-11-03",
    }


@pytest.mark.req("BA-07")
def test_a_bank_transaction_cannot_hang_on_an_invoice_case(app: Engine) -> None:
    case_id = new_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="not a bank transaction"),
        unit_of_work(app, Actor.system("bank-import")) as db,
    ):
        db.execute(
            text(
                "insert into bank_transaction values (:id, 'EUR', 'TRANSFER-TEST-WRONG', "
                "'2026-11-03', -100)"
            ),
            {"id": case_id},
        )


@pytest.mark.req("PY-11", "EX-01")
@pytest.mark.parametrize("last", [S.IN_PAYMENT_RUN, S.PAID])
def test_a_case_in_a_run_or_paid_cannot_be_closed(app: Engine, last: CaseStatus) -> None:
    case_id = booked_invoice(app)
    walk(
        app,
        case_id,
        *([S.IN_PAYMENT_RUN] if last is S.IN_PAYMENT_RUN else [S.IN_PAYMENT_RUN, S.PAID]),
    )
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.INVOICE_CHECK_FAILED, "x", case_id=case_id
        )

    with (
        pytest.raises(CaseRuleBroken, match="cannot be closed"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(
            db,
            exception_id,
            Outcome.ACCEPTED_WITH_COMMENT,
            resolution="checked",
            case_to=S.CLOSED,
            closed_reason=ClosedReason.WITHDRAWN,
        )


@pytest.mark.req("NF-01")
def test_the_app_user_cannot_create_temporary_tables(app: Engine) -> None:
    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(text("create temp table audit_event (id bigint)"))


@pytest.fixture
def app_with_temp_tables(owner: Engine) -> Iterator[None]:
    """Gives argus_app temporary tables back, to prove the pinned search_path holds on its own."""
    with owner.begin() as db:
        db.execute(
            text(
                "do $$ begin execute format('grant temporary on database %I to argus_app', "
                "current_database()); end $$"
            )
        )
    yield
    with owner.begin() as db:
        db.execute(
            text(
                "do $$ begin execute format('revoke temporary on database %I from argus_app', "
                "current_database()); end $$"
            )
        )


@pytest.mark.req("NF-01", "NF-05")
@pytest.mark.usefixtures("app_with_temp_tables")
def test_a_temporary_table_cannot_stand_in_for_the_real_ones(app: Engine, owner: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL, S.APPROVED)

    with app.connect() as db:
        # QA's attacks on PR #79: a fake transition list and a fake audit log.
        db.execute(
            text(
                "create temp table case_status_transition "
                "(kind case_kind, from_status case_status, to_status case_status)"
            )
        )
        db.execute(
            text(
                "insert into pg_temp.case_status_transition "
                "values ('incoming_invoice', 'approved', 'paid')"
            )
        )
        db.execute(
            text(
                "create temp table audit_event (case_id bigint, email_id bigint, "
                "statement_id bigint, exception_id bigint, event_type text, "
                "from_status case_status, to_status case_status, details jsonb)"
            )
        )
        db.commit()
        with pytest.raises(DBAPIError, match="approved -> paid not allowed"):
            db.execute(text("select set_config('argus.actor', 'system:qa', true)"))
            db.execute(
                text("update invoice_case set status = 'paid' where id = :id"), {"id": case_id}
            )
        db.rollback()
        db.execute(text("select set_config('argus.actor', 'system:qa', true)"))
        db.execute(
            text(
                "update invoice_case set status = 'booked', datev_booking_ref = 'DATEV-TEMP' "
                "where id = :id"
            ),
            {"id": case_id},
        )
        db.commit()
        fake_events = db.execute(text("select count(*) from pg_temp.audit_event")).scalar()
        # Temporary tables live as long as the session, so keep this one out of the pool.
        db.invalidate()

    assert fake_events == 0
    assert events_of(app, case_id)[-1] == "status_changed"
    with unit_of_work(app, INTAKE) as db:
        assert trace(db, case_id)[-1].to_status is S.BOOKED


# 7. The exception queue (issue #21)

# EX-02 word for word, mapped to the reason ARGUS stores. "Unknown vendor or customer" is two
# reasons, so the queue can say which master data is missing.
EX_02_REASONS = {
    "uncertain classification": [ExceptionReason.UNCERTAIN_CLASSIFICATION],
    "unreadable document": [ExceptionReason.UNREADABLE_DOCUMENT],
    "unknown vendor or customer": [
        ExceptionReason.UNKNOWN_VENDOR,
        ExceptionReason.UNKNOWN_CUSTOMER,
    ],
    "vendor without approver": [ExceptionReason.VENDOR_WITHOUT_APPROVER],
    "duplicate": [ExceptionReason.DUPLICATE],
    "IBAN mismatch": [ExceptionReason.IBAN_MISMATCH],
    "failed §14 UStG check": [ExceptionReason.INVOICE_CHECK_FAILED],
    "booking error": [ExceptionReason.BOOKING_ERROR],
    "unmatched bank transaction": [ExceptionReason.UNMATCHED_BANK_TRANSACTION],
    "bank balance difference": [ExceptionReason.BANK_BALANCE_DIFFERENCE],
}


def open_ids(app: Engine, reason: ExceptionReason | None = None) -> list[int]:
    with unit_of_work(app, BENJAMIN) as db:
        return [e.id for e in list_open_exceptions(db, reason=reason)]


@pytest.mark.req("EX-02")
def test_every_ex_02_reason_and_customer_query_can_be_queued(app: Engine) -> None:
    assert len(EX_02_REASONS) == 10
    wanted = [r for reasons in EX_02_REASONS.values() for r in reasons]
    wanted.append(ExceptionReason.CUSTOMER_QUERY)  # manual review for customer questions, #9

    email_id = 800_000 + next(_counter)
    with unit_of_work(app, INTAKE) as db:
        opened = {
            reason: open_exception(db, reason, f"test {reason}", email_id=email_id)
            for reason in wanted
        }

    with unit_of_work(app, BENJAMIN) as db:
        queued = {e.id: e.reason for e in list_open_exceptions(db) if e.email_id == email_id}
    assert queued == {exception_id: reason for reason, exception_id in opened.items()}


@pytest.mark.req("EX-01")
@pytest.mark.parametrize(
    "left", [S.RECEIVED, S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED, S.IN_PAYMENT_RUN, S.PAID]
)
def test_opening_an_exception_stores_the_status_the_case_left(
    app: Engine, left: CaseStatus
) -> None:
    case_id = new_case(app)
    path = [S.RECEIVED, S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED, S.IN_PAYMENT_RUN, S.PAID]
    walk(app, case_id, *path[1 : path.index(left) + 1])

    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.BOOKING_ERROR, "test", case_id=case_id)
        case = get_case(db, case_id)

    assert (case.status, case.resume_status) == (S.EXCEPTION, left)
    with unit_of_work(app, INTAKE) as db:
        last = trace(db, case_id)[-1]
    assert (last.event_type, last.from_status, last.to_status) == (
        "status_changed",
        left,
        S.EXCEPTION,
    )


@pytest.mark.req("EX-01", "NF-01")
def test_resolving_returns_the_case_and_both_steps_are_logged(app: Engine) -> None:
    case_id = new_case(app)
    walk(app, case_id, S.AWAITING_APPROVAL)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.VENDOR_WITHOUT_APPROVER, "no approver for 70001", case_id=case_id
        )
    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.DATA_CORRECTED, resolution="approver added")
        case = get_case(db, case_id)
        events = [e for e in trace(db, case_id) if e.exception_id == exception_id or e.to_status]

    assert (case.status, case.resume_status) == (S.AWAITING_APPROVAL, None)
    opened, stopped, resolved, returned = events[-4:]
    assert (opened.event_type, opened.actor) == ("exception_opened", str(INTAKE))
    assert opened.details["reason"] == "vendor_without_approver"
    assert (stopped.from_status, stopped.to_status) == (S.AWAITING_APPROVAL, S.EXCEPTION)
    assert (resolved.event_type, resolved.actor) == ("exception_resolved", str(BENJAMIN))
    assert resolved.details == {
        "reason": "vendor_without_approver",
        "outcome": "data_corrected",
        "name": "Benjamin Test",  # as they were at that moment (ADR 0008, migration 0016)
        "sign_in_address": "benjamin@max-asp-test.example",
        "resolution_sha256": hashlib.sha256(b"approver added").hexdigest(),
    }
    assert (returned.from_status, returned.to_status, returned.actor) == (
        S.EXCEPTION,
        S.AWAITING_APPROVAL,
        str(BENJAMIN),
    )


@pytest.mark.req("EX-01", "NF-01")
def test_resolving_can_close_the_case_and_both_steps_are_logged(app: Engine) -> None:
    case_id = new_case(app)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.INVOICE_CHECK_FAILED, "no VAT ID on invoice", case_id=case_id
        )
    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
        case = get_case(db, case_id)
        events = trace(db, case_id)

    assert (case.status, case.closed_reason) == (S.CLOSED, ClosedReason.REJECTED)
    assert [e.event_type for e in events][-2:] == ["exception_resolved", "status_changed"]
    assert events[-2].details["outcome"] == "rejected"
    assert (events[-1].from_status, events[-1].to_status) == (S.EXCEPTION, S.CLOSED)
    assert events[-1].details["closed_reason"]["new"] == "rejected"


@pytest.mark.req("EX-01")
def test_a_case_with_a_second_open_exception_stays_in_exception(app: Engine) -> None:
    case_id = booked_invoice(app)
    with unit_of_work(app, INTAKE) as db:
        first = open_exception(db, ExceptionReason.DUPLICATE, "maybe RE-1 again", case_id=case_id)
        second = open_exception(db, ExceptionReason.IBAN_MISMATCH, "IBAN differs", case_id=case_id)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, first, Outcome.NOT_A_DUPLICATE, resolution="two deliveries")
    assert status_of(app, case_id) is S.EXCEPTION

    for to in (S.BOOKED, S.IN_PAYMENT_RUN, S.CLOSED):
        with (
            pytest.raises(CaseRuleBroken, match="open exception"),
            unit_of_work(app, BENJAMIN) as db,
        ):
            move_case(
                db, case_id, expected=S.EXCEPTION, to=to, closed_reason=ClosedReason.WITHDRAWN
            )
    assert status_of(app, case_id) is S.EXCEPTION

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, second, Outcome.FIXED_IN_DATEV)
    assert status_of(app, case_id) is S.BOOKED


@pytest.mark.req("EX-01")
def test_the_queue_lists_open_exceptions_with_case_reason_age_and_detail(app: Engine) -> None:
    case_id = booked_invoice(app)
    email_id = 800_000 + next(_counter)
    statement_id = statement(app, "blocked")
    with unit_of_work(app, INTAKE) as db:
        on_case = open_exception(
            db, ExceptionReason.BOOKING_ERROR, "account 4980 is locked", case_id=case_id
        )
    with unit_of_work(app, INTAKE) as db:
        on_email = open_exception(
            db, ExceptionReason.UNREADABLE_DOCUMENT, "PDF is password protected", email_id=email_id
        )
        done = open_exception(
            db, ExceptionReason.UNCERTAIN_CLASSIFICATION, "invoice or reminder?", email_id=email_id
        )
        on_statement = open_exception(
            db,
            ExceptionReason.BANK_BALANCE_DIFFERENCE,
            "EUR 12.00 apart",
            statement_id=statement_id,
        )
    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, done, Outcome.CATEGORY_CHOSEN, resolution="it is an invoice")

    with unit_of_work(app, BENJAMIN) as db:
        queue = {e.id: e for e in list_open_exceptions(db)}

    assert done not in queue
    mine = [i for i in queue if i in (on_case, on_email, on_statement)]
    assert mine == [on_case, on_email, on_statement], "oldest first"

    case_entry = queue[on_case]
    assert case_entry.case_id == case_id
    assert case_entry.case_kind is CaseKind.INCOMING_INVOICE
    assert case_entry.resume_status is S.BOOKED
    assert case_entry.reason is ExceptionReason.BOOKING_ERROR
    assert case_entry.detail == "account 4980 is locked"
    assert (case_entry.email_id, case_entry.statement_id) == (None, None)

    email_entry, statement_entry = queue[on_email], queue[on_statement]
    assert (email_entry.email_id, email_entry.case_id, email_entry.case_kind) == (
        email_id,
        None,
        None,
    )
    assert email_entry.detail == "PDF is password protected"
    assert statement_entry.statement_id == statement_id

    assert case_entry.age >= email_entry.age >= statement_entry.age >= timedelta(0)
    assert case_entry.age > timedelta(0)
    with unit_of_work(app, BENJAMIN) as db:
        now: datetime = db.execute(text("select clock_timestamp()")).scalar_one()
    assert timedelta(0) <= now - (case_entry.opened_at + case_entry.age) < timedelta(seconds=5)


@pytest.mark.req("EX-01")
def test_the_queue_can_show_one_reason(app: Engine) -> None:
    email_id = 800_000 + next(_counter)
    with unit_of_work(app, INTAKE) as db:
        query = open_exception(
            db, ExceptionReason.CUSTOMER_QUERY, "asks for a copy", email_id=email_id
        )
        other = open_exception(
            db, ExceptionReason.UNKNOWN_CUSTOMER, "not in DATEV", email_id=email_id
        )

    only_queries = open_ids(app, ExceptionReason.CUSTOMER_QUERY)
    assert query in only_queries
    assert other not in only_queries
    with unit_of_work(app, BENJAMIN) as db:
        assert all(
            e.reason is ExceptionReason.CUSTOMER_QUERY
            for e in list_open_exceptions(db, reason=ExceptionReason.CUSTOMER_QUERY)
        )

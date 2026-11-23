"""Catch an invoice that arrives twice, against a real PostgreSQL (#31, IN-10).

The first group runs the sweep on the fixture emails E01 and E15, the same invoice sent twice.
The other groups open cases straight in the case store with made up issuers, numbers and
amounts (CLAUDE.md: synthetic data only), so each test owns its keys. The key format itself is
tested in ``tests/unit/test_duplicate_key.py``.

Cases are never deleted, so a fixture invoice swept in an earlier test is still in the database.
The sweep test therefore checks that both copies point to the same first case, not that E01 is
the first one ever seen.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

import argus.intake.duplicates as duplicates
from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    DuplicateRefused,
    change_case,
    correct_case,
    create_case,
    get_case,
    move_case,
    open_exception,
    resolve_exception,
    unit_of_work,
)
from argus.intake.duplicates import duplicate_key, flag_if_duplicate, resolve_duplicate
from argus.intake.sweep import DocumentArchive, sweep
from tests.integration.test_invoice_split import model as recorded_model
from tests.integration.test_invoice_split import record_for, setup
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
    fixture_path,
)

INTAKE = Actor.system("intake")
CLERK = Actor.user("accounts-clerk-entra-id", "Clerk Test")

_counter = iter(range(1, 1_000_000))


def issuer() -> str:
    """A made up VAT ID no other test uses."""
    return f"vat:DE7{next(_counter):08d}"


def invoice(
    app: Engine,
    case_key: str,
    gross_cents: int | None = 11900,
    currency: str | None = "EUR",
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
) -> tuple[int, int | None]:
    """Open a case as the sweep does, in one unit of work. Returns the case and its first."""
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            kind,
            source_email_id=900_000 + next(_counter),
            document_number=case_key.split("|", 1)[1],
            gross_cents=gross_cents,
            currency=currency,
            case_key=case_key,
        )
        return case_id, flag_if_duplicate(db, case_id)


def correct(db: Connection, case_id: int, **changes: Any) -> None:
    """A person corrects some of the number, amount and currency; the others stay (#112)."""
    case = get_case(db, case_id)
    values = {
        "document_number": case.document_number,
        "gross_cents": case.gross_cents,
        "currency": case.currency,
        **changes,
    }
    correct_case(db, case_id, **values, reason="checked against the invoice", name="A. Clerk")


def row_of(app: Engine, case_id: int) -> dict[str, Any]:
    with app.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "select status, duplicate_key, duplicate_of, closed_reason"
                    " from invoice_case where id = :id"
                ),
                {"id": case_id},
            )
            .mappings()
            .one()
        )


def duplicate_exceptions(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select id, detail, facts, outcome from exception_item"
                " where case_id = :id and reason = 'duplicate' order by id"
            ),
            {"id": case_id},
        ).mappings()
        return [dict(row) for row in rows]


def changes(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select details from audit_event where case_id = :id"
                    " and event_type in ('case_changed', 'status_changed') order by chain_seq"
                ),
                {"id": case_id},
            ).scalars()
        )


# AC: a second invoice with the same vendor, number and amount is flagged and goes to the
# exception queue as duplicate. AC: the duplicate links to the first case.


@pytest.mark.req("IN-10", "EX-01")
# Sweeps the mailbox and routes on its own, so it runs in live mode (#72).
@pytest.mark.usefixtures("clean", "live")
def test_e15_the_second_copy_of_e01_goes_to_the_exception_queue_linked_to_the_first(
    app: Engine,
) -> None:
    e01, e15 = fixture_path("E01").read_bytes(), fixture_path("E15").read_bytes()
    documents, sorting, graph = setup([e01, e15], recorded_model())
    sweep(app, graph, DocumentArchive(documents), sorting)

    first = case_of(app, record_for(app, e01))
    second = case_of(app, record_for(app, e15))
    # E01's case holds the key, or is itself the copy of the same invoice from an earlier test.
    first_of_all = first["duplicate_of"] or first["id"]
    assert second["duplicate_of"] == first_of_all
    assert second["status"] == CaseStatus.EXCEPTION
    assert second["duplicate_key"] is None
    [exception] = duplicate_exceptions(app, second["id"])
    assert exception["facts"] == {"duplicate_of": first_of_all}
    assert f"case {first_of_all}" in exception["detail"]


def case_of(app: Engine, email_id: int) -> dict[str, Any]:
    """The case opened for the email record. Other tests open cases with made up email IDs
    that may equal a real record's, so the case is found through its stored document."""
    with app.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "select id, status, duplicate_key, duplicate_of from invoice_case c"
                    " where source_email_id = :id and exists (select 1 from case_document l"
                    " join stored_document d on d.id = l.document_id"
                    " where l.case_id = c.id and d.email_id = :id)"
                ),
                {"id": email_id},
            )
            .mappings()
            .one()
        )


@pytest.mark.req("IN-10")
def test_the_first_invoice_gets_its_key_and_carries_on(app: Engine) -> None:
    key = f"{issuer()}|RE-2026/0042"
    case_id, first = invoice(app, key)

    assert first is None
    row = row_of(app, case_id)
    assert row["status"] == CaseStatus.RECEIVED
    assert row["duplicate_key"] == duplicate_key(key, 11900, "EUR")
    assert duplicate_exceptions(app, case_id) == []


@pytest.mark.req("IN-10", "EX-01")
def test_the_same_vendor_number_and_amount_again_is_flagged_and_linked(app: Engine) -> None:
    vendor = issuer()
    first_id, _ = invoice(app, f"{vendor}|RE-2026/0042")
    # The same number written another way still matches (design note, section 5).
    second_id, first = invoice(app, f"{vendor}|RE2026-0042")

    assert first == first_id
    row = row_of(app, second_id)
    assert (row["status"], row["duplicate_of"], row["duplicate_key"]) == (
        CaseStatus.EXCEPTION,
        first_id,
        None,
    )
    [exception] = duplicate_exceptions(app, second_id)
    assert exception["outcome"] is None
    # Only the case number goes into the audit log, no name, number or amount (NF-03).
    assert exception["facts"] == {"duplicate_of": first_id}
    assert {"duplicate_of": {"old": None, "new": first_id}} in changes(app, second_id)


@pytest.mark.req("IN-10")
def test_a_third_copy_links_to_the_first_case_too(app: Engine) -> None:
    key = f"{issuer()}|7781"
    first_id, _ = invoice(app, key)
    invoice(app, key)
    _, first = invoice(app, key)

    assert first == first_id


@pytest.mark.req("IN-10")
def test_a_flagged_duplicate_is_not_sent_for_approval(app: Engine) -> None:
    key = f"{issuer()}|7782"
    invoice(app, key)
    second_id, _ = invoice(app, key)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
        move_case(db, second_id, expected=CaseStatus.EXCEPTION, to=CaseStatus.AWAITING_APPROVAL)
    assert row_of(app, second_id)["status"] == CaseStatus.EXCEPTION


# AC: a different amount or a different vendor is not a duplicate.


@pytest.mark.req("IN-10")
@pytest.mark.parametrize(
    ("gross_cents", "currency"),
    [(11901, "EUR"), (11900, "USD")],
    ids=["one cent more", "another currency"],
)
def test_a_different_amount_is_not_a_duplicate(
    app: Engine, gross_cents: int, currency: str
) -> None:
    key = f"{issuer()}|RE-2026-0050"
    invoice(app, key)
    second_id, first = invoice(app, key, gross_cents, currency)

    assert first is None
    assert row_of(app, second_id)["status"] == CaseStatus.RECEIVED


@pytest.mark.req("IN-10")
def test_a_different_vendor_with_the_same_number_and_amount_is_not_a_duplicate(
    app: Engine,
) -> None:
    invoice(app, f"{issuer()}|1001")
    second_id, first = invoice(app, f"{issuer()}|1001")

    assert first is None
    assert row_of(app, second_id)["status"] == CaseStatus.RECEIVED


@pytest.mark.req("IN-10")
def test_a_credit_note_with_the_same_number_and_amount_is_not_a_duplicate_of_the_invoice(
    app: Engine,
) -> None:
    key = f"{issuer()}|G-1001"
    invoice(app, key)
    _, first = invoice(app, key, kind=CaseKind.INCOMING_CREDIT_NOTE)

    assert first is None


@pytest.mark.req("IN-10")
def test_an_amount_a_person_still_has_to_check_gets_its_key_once_confirmed(app: Engine) -> None:
    key = f"{issuer()}|RE-77"
    first_id, _ = invoice(app, key)
    second_id, first = invoice(app, key, gross_cents=None)
    assert first is None
    assert row_of(app, second_id)["duplicate_key"] is None

    with unit_of_work(app, CLERK) as db:
        correct(db, second_id, gross_cents=11900)
        assert flag_if_duplicate(db, second_id) == first_id


@pytest.mark.req("IN-10")
def test_checking_a_case_twice_changes_nothing_the_second_time(app: Engine) -> None:
    key = f"{issuer()}|RE-78"
    first_id, _ = invoice(app, key)
    second_id, _ = invoice(app, key)

    with unit_of_work(app, INTAKE) as db:
        assert flag_if_duplicate(db, first_id) is None
        assert flag_if_duplicate(db, second_id) == first_id
    assert len(duplicate_exceptions(app, second_id)) == 1


# AC: the database refuses two active cases with the same duplicate key.


@pytest.mark.req("IN-10")
def test_the_database_refuses_a_second_case_with_the_same_duplicate_key(app: Engine) -> None:
    key = duplicate_key(f"{issuer()}|RE-79", 11900, "EUR")
    first_id, _ = invoice(app, f"{issuer()}|RE-80")
    with unit_of_work(app, INTAKE) as db:
        change_case(db, first_id, duplicate_key=key)

    second_id, _ = invoice(app, f"{issuer()}|RE-81")
    with pytest.raises(DuplicateRefused), unit_of_work(app, INTAKE) as db:
        change_case(db, second_id, duplicate_key=key)
    with pytest.raises(DuplicateRefused), unit_of_work(app, INTAKE) as db:
        create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=1, duplicate_key=key)


@pytest.fixture
def lost_race(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The first look finds no holder, as if a parallel sweep took the key a moment later."""
    real = duplicates._holder
    calls = iter(range(1_000))

    def holder(connection: Connection, kind: CaseKind, key: str) -> int | None:
        return None if next(calls) == 0 else real(connection, kind, key)

    monkeypatch.setattr(duplicates, "_holder", holder)
    yield


@pytest.mark.req("IN-10")
def test_a_copy_that_loses_the_race_for_the_key_is_still_flagged(
    app: Engine, lost_race: None
) -> None:
    """The refusal from the unique index rolls back to a savepoint only, so the same unit of
    work can still link the case and open the exception (review of PR 79)."""
    key = f"{issuer()}|RE-82"
    with unit_of_work(app, INTAKE) as db:
        first_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=1,
            gross_cents=11900,
            currency="EUR",
            case_key=key,
        )
        change_case(db, first_id, duplicate_key=duplicate_key(key, 11900, "EUR"))

    second_id, first = invoice(app, key)

    assert first == first_id
    assert row_of(app, second_id)["status"] == CaseStatus.EXCEPTION
    assert len(duplicate_exceptions(app, second_id)) == 1


# The link to the first case is kept honest by the database (migration 0010).


@pytest.mark.req("IN-10", "NF-01")
def test_the_link_to_the_first_case_never_changes(app: Engine) -> None:
    key = f"{issuer()}|RE-83"
    invoice(app, key)
    second_id, _ = invoice(app, key)
    other_id, _ = invoice(app, f"{issuer()}|RE-84")

    for new in (other_id, None):
        with (
            pytest.raises(CaseRuleBroken, match="link never changes"),
            unit_of_work(app, INTAKE) as db,
        ):
            change_case(db, second_id, duplicate_of=new)


@pytest.mark.req("IN-10")
def test_a_case_is_never_a_duplicate_of_itself(app: Engine) -> None:
    case_id, _ = invoice(app, f"{issuer()}|RE-85")

    with pytest.raises(CaseRuleBroken, match="itself"), unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, duplicate_of=case_id)


@pytest.mark.req("IN-10")
def test_a_case_is_only_a_duplicate_of_a_case_of_the_same_kind(app: Engine) -> None:
    invoice_id, _ = invoice(app, f"{issuer()}|RE-86")
    credit_id, _ = invoice(app, f"{issuer()}|G-86", kind=CaseKind.INCOMING_CREDIT_NOTE)

    with pytest.raises(CaseRuleBroken, match="same kind"), unit_of_work(app, INTAKE) as db:
        change_case(db, credit_id, duplicate_of=invoice_id)


# What a person decides in the exception queue.


@pytest.mark.req("IN-10", "EX-01")
def test_a_confirmed_duplicate_is_closed(app: Engine) -> None:
    key = f"{issuer()}|RE-87"
    invoice(app, key)
    second_id, _ = invoice(app, key)
    [exception] = duplicate_exceptions(app, second_id)

    with unit_of_work(app, CLERK) as db:
        resolve_duplicate(db, exception["id"], confirmed=True)

    row = row_of(app, second_id)
    assert (row["status"], row["closed_reason"]) == (CaseStatus.CLOSED, "duplicate")


@pytest.mark.req("IN-10", "EX-01")
def test_not_a_duplicate_carries_on_with_its_own_key(app: Engine) -> None:
    key = f"{issuer()}|RE-88"
    first_id, _ = invoice(app, key)
    second_id, _ = invoice(app, key)
    [exception] = duplicate_exceptions(app, second_id)

    with unit_of_work(app, CLERK) as db:
        resolve_duplicate(db, exception["id"], confirmed=False, resolution="two deliveries")
        assert get_case(db, second_id).status is CaseStatus.RECEIVED

    row = row_of(app, second_id)
    assert row["duplicate_key"] == f"{duplicate_key(key, 11900, 'EUR')}|case:{second_id}"
    # The link stays, so the trail shows which case it was taken for.
    assert row["duplicate_of"] == first_id
    # A further copy still links to the first case.
    _, first = invoice(app, key)
    assert first == first_id


@pytest.mark.req("IN-10")
def test_only_a_duplicate_exception_can_be_resolved_as_one(app: Engine) -> None:
    case_id, _ = invoice(app, f"{issuer()}|RE-89")
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.UNKNOWN_VENDOR, "no vendor yet", case_id=case_id
        )

    with pytest.raises(ValueError, match="not a duplicate"), unit_of_work(app, CLERK) as db:
        resolve_duplicate(db, exception_id, confirmed=True)


# Review of PR 109: no approval without a key, and a corrected amount clears the key.


def walk_to_approval(app: Engine, case_id: int) -> None:
    with unit_of_work(app, INTAKE) as db:
        assert move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)


@pytest.mark.req("IN-10")
@pytest.mark.parametrize("kind", [CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE])
def test_a_case_without_a_duplicate_key_is_not_sent_for_approval(
    app: Engine, kind: CaseKind
) -> None:
    case_id, _ = invoice(app, f"{issuer()}|RE-90", gross_cents=None, kind=kind)

    with pytest.raises(CaseRuleBroken, match="no duplicate key"):
        walk_to_approval(app, case_id)
    assert row_of(app, case_id)["status"] == CaseStatus.RECEIVED


@pytest.mark.req("IN-10")
def test_a_copy_whose_amount_is_confirmed_later_cannot_reach_approval_unchecked(
    app: Engine,
) -> None:
    """The reviewer's case: copy A has a flagged amount, copy B is clean and takes the key,
    then a person confirms A's amount. A must be checked before approval and is then flagged."""
    key = f"{issuer()}|RE-91"
    copy_a, _ = invoice(app, key, gross_cents=None)
    copy_b, _ = invoice(app, key)
    with unit_of_work(app, CLERK) as db:
        correct(db, copy_a, gross_cents=11900)

    with pytest.raises(CaseRuleBroken, match="no duplicate key"):
        walk_to_approval(app, copy_a)

    with unit_of_work(app, INTAKE) as db:
        assert flag_if_duplicate(db, copy_a) == copy_b
    assert row_of(app, copy_a)["status"] == CaseStatus.EXCEPTION


@pytest.mark.req("IN-10")
@pytest.mark.parametrize(
    "correction",
    [{"gross_cents": 11900}, {"currency": "CHF"}, {"document_number": "RE-93"}],
    ids=["amount", "currency", "number"],
)
def test_a_correction_clears_the_key_so_the_case_is_checked_again(
    app: Engine, correction: dict[str, Any]
) -> None:
    case_id, _ = invoice(app, f"{issuer()}|RE-92", gross_cents=10000)
    assert row_of(app, case_id)["duplicate_key"] is not None

    with unit_of_work(app, CLERK) as db:
        correct(db, case_id, **correction)
    assert row_of(app, case_id)["duplicate_key"] is None
    with pytest.raises(CaseRuleBroken, match="no duplicate key"):
        walk_to_approval(app, case_id)

    with unit_of_work(app, INTAKE) as db:
        assert flag_if_duplicate(db, case_id) is None
    walk_to_approval(app, case_id)


@pytest.mark.req("IN-10")
def test_a_copy_at_the_corrected_amount_is_caught(app: Engine) -> None:
    """A first copy read as 100.00 EUR and corrected to 119.00 EUR no longer keeps the stale
    key, so a second copy at 119.00 EUR is flagged against it."""
    key = f"{issuer()}|RE-94"
    first_id, _ = invoice(app, key, gross_cents=10000)
    with unit_of_work(app, CLERK) as db:
        correct(db, first_id, gross_cents=11900)
        flag_if_duplicate(db, first_id)
    assert row_of(app, first_id)["duplicate_key"] == duplicate_key(key, 11900, "EUR")

    _, first = invoice(app, key, gross_cents=11900)
    assert first == first_id


@pytest.mark.req("IN-10")
def test_an_approved_case_whose_key_is_cleared_cannot_be_approved_again_unchecked(
    app: Engine,
) -> None:
    case_id, _ = invoice(app, f"{issuer()}|RE-95")
    walk_to_approval(app, case_id)
    with unit_of_work(app, CLERK) as db:
        correct(db, case_id, gross_cents=12000)

    with (
        pytest.raises(CaseRuleBroken, match="no duplicate key"),
        unit_of_work(app, Actor.user("approver-entra-id")) as db,
    ):
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)


@pytest.mark.req("IN-10")
def test_a_cleared_case_whose_amount_changes_is_checked_again(app: Engine) -> None:
    key = f"{issuer()}|RE-96"
    first_id, _ = invoice(app, key)
    second_id, _ = invoice(app, key, gross_cents=12000)
    with unit_of_work(app, CLERK) as db:
        correct(db, second_id, gross_cents=11900)
        assert flag_if_duplicate(db, second_id) == first_id
    [exception] = duplicate_exceptions(app, second_id)
    with unit_of_work(app, CLERK) as db:
        resolve_duplicate(db, exception["id"], confirmed=False, resolution="credit and rebill")
        # Cleared: it holds its own key and is no longer reported as a duplicate.
        assert flag_if_duplicate(db, second_id) is None
        correct(db, second_id, gross_cents=12000)
        correct(db, second_id, gross_cents=11900)
        # Same amount as the first again: back to the queue, the first link stays.
        assert flag_if_duplicate(db, second_id) == first_id
    assert row_of(app, second_id)["duplicate_of"] == first_id
    assert len(duplicate_exceptions(app, second_id)) == 2


# Compliance on PR 109: only a person decides "not a duplicate", and with a reason.


def flagged(app: Engine) -> int:
    key = f"{issuer()}|RE-97"
    invoice(app, key)
    second_id, _ = invoice(app, key)
    [exception] = duplicate_exceptions(app, second_id)
    return int(exception["id"])


@pytest.mark.req("IN-10", "NF-01")
def test_argus_itself_cannot_decide_that_a_case_is_not_a_duplicate(app: Engine) -> None:
    exception_id = flagged(app)

    with (
        pytest.raises(CaseRuleBroken, match="only a person"),
        unit_of_work(app, INTAKE) as db,
    ):
        resolve_exception(db, exception_id, Outcome.NOT_A_DUPLICATE, resolution="looks fine")


@pytest.mark.req("IN-10", "NF-01")
@pytest.mark.parametrize("resolution", [None, "", "   "], ids=["none", "empty", "blank"])
def test_not_a_duplicate_needs_a_reason(app: Engine, resolution: str | None) -> None:
    exception_id = flagged(app)

    with (
        pytest.raises(CaseRuleBroken, match="needs a reason"),
        unit_of_work(app, CLERK) as db,
    ):
        resolve_exception(db, exception_id, Outcome.NOT_A_DUPLICATE, resolution=resolution)
    with pytest.raises(ValueError, match="needs a reason"), unit_of_work(app, CLERK) as db:
        resolve_duplicate(db, exception_id, confirmed=False, resolution=resolution)


@pytest.mark.req("IN-10")
def test_a_duplicate_ends_only_confirmed_not_a_duplicate_or_rejected(app: Engine) -> None:
    exception_id = flagged(app)

    with (
        pytest.raises(CaseRuleBroken, match="can only end as"),
        unit_of_work(app, CLERK) as db,
    ):
        resolve_exception(db, exception_id, Outcome.DATA_CORRECTED, resolution="fixed")


@pytest.mark.req("IN-10")
def test_argus_may_still_confirm_a_duplicate(app: Engine) -> None:
    """Confirming only stops processing, so any actor may do it."""
    exception_id = flagged(app)

    with unit_of_work(app, INTAKE) as db:
        resolve_duplicate(db, exception_id, confirmed=True)

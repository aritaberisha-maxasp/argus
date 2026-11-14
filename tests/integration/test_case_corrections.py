"""A person corrects the number, amount or currency of a flagged invoice (#112), against a real
PostgreSQL with the rights of argus_app.

Covers EX-01 (the case waits in the queue with a reason), IN-10 (the duplicate check runs again)
and PR-01 (number, gross amount and currency are captured, by a person when ARGUS cannot).
Issuers, numbers, amounts and names are made up (CLAUDE.md: synthetic data only).
"""

from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    change_case,
    correct_case,
    create_case,
    get_case,
    move_case,
    open_exception,
    resolve_exception,
    trace,
    unit_of_work,
)
from argus.intake.corrections import (
    CorrectedBy,
    Correction,
    NotCorrectable,
    correct_flagged_values,
    queue_values_to_check,
)
from argus.intake.duplicates import duplicate_key, flag_if_duplicate
from argus.intake.invoices import EmailInvoices, FoundInvoice, Source, open_cases
from argus.reading.fields import Flag, InvoiceReading, ReadField
from argus.reading.fields import Source as FieldSource

pytestmark = pytest.mark.req("EX-01", "IN-10", "PR-01")

INTAKE = Actor.system("intake")
BENJAMIN_ID = "33333333-3333-4333-8333-333333333333"  # made up
BENJAMIN = CorrectedBy(Actor.user(BENJAMIN_ID), "Benjamin Test", "benjamin@max-asp-test.example")
APPROVER = Actor.user("approver-entra-id")

_counter = iter(range(1, 1_000_000))


def issuer() -> str:
    """A made up VAT ID no other test uses."""
    return f"vat:DE8{next(_counter):08d}"


def reading(number: str, gross: str | None, currency: str | None = "EUR") -> InvoiceReading:
    """A reading of an invoice in the email text. Without ``gross`` the amount is flagged as
    only located by the language model, which a person must confirm (ADR 0006 point 3)."""
    fields = {
        "invoice_number": ReadField("invoice_number", number, FieldSource.PAGE_TEXT),
        "gross_amount": ReadField("gross_amount", gross, FieldSource.PAGE_TEXT)
        if gross is not None
        else ReadField(
            "gross_amount",
            "119.00",
            FieldSource.LANGUAGE_MODEL,
            page=1,
            words="Gesamt 119,00",
            flag=Flag.FROM_MODEL,
            note="only the language model located this value, a person confirms it",
        ),
    }
    if currency is not None:
        fields["currency"] = ReadField("currency", currency, FieldSource.PAGE_TEXT)
    return InvoiceReading("body", "invoice", "test", 1, "made up", fields)


def intake(app: Engine, number: str, gross: str | None, vat: str | None = None) -> int:
    """Open a case the way the sweep does (#30) and return it."""
    found = EmailInvoices(
        invoices=(
            FoundInvoice(
                f"{vat or issuer()}|{number}",
                CaseKind.INCOMING_INVOICE,
                Source(reading(number, gross), None),
            ),
        ),
        doubts=(),
        attachment_sha256=(),
    )
    with unit_of_work(app, INTAKE) as db:
        [case_id] = open_cases(db, 800_000 + next(_counter), found)
    return case_id


def open_items(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select id, reason, detail, facts from exception_item"
                " where case_id = :id and resolved_at is null order by id"
            ),
            {"id": case_id},
        ).mappings()
        return [dict(r) for r in rows]


def values_exception(app: Engine, case_id: int) -> int:
    [item] = [i for i in open_items(app, case_id) if i["reason"] == "values_to_check"]
    return int(item["id"])


def row_of(app: Engine, case_id: int) -> dict[str, Any]:
    with app.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "select status, document_number, gross_minor, currency, duplicate_key,"
                    " duplicate_of from invoice_case where id = :id"
                ),
                {"id": case_id},
            )
            .mappings()
            .one()
        )


def correct(
    app: Engine, exception_id: int, number: str, cents: int, reason: str = "checked the PDF"
) -> Any:
    with unit_of_work(app, BENJAMIN.actor) as db:
        return correct_flagged_values(
            db, exception_id, Correction(number, cents, "EUR", reason), BENJAMIN
        )


def cannot_reach_approval(app: Engine, case_id: int) -> None:
    """The case waits in exception, and the database refuses to send it for approval."""
    assert row_of(app, case_id)["status"] == CaseStatus.EXCEPTION
    with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
        move_case(db, case_id, expected=CaseStatus.EXCEPTION, to=CaseStatus.AWAITING_APPROVAL)


def to_approval(app: Engine, case_id: int) -> None:
    with unit_of_work(app, INTAKE) as db:
        assert move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)


# The case waits in the exception queue instead of unseen


def test_an_invoice_with_an_amount_to_confirm_waits_in_the_queue_with_the_reason(
    app: Engine,
) -> None:
    case_id = intake(app, "RC-1", gross=None)

    assert row_of(app, case_id)["status"] == CaseStatus.EXCEPTION
    assert row_of(app, case_id)["gross_minor"] is None
    [item] = open_items(app, case_id)
    assert item["reason"] == ExceptionReason.VALUES_TO_CHECK
    assert item["facts"] == {"fields": ["gross_amount"]}
    assert "the gross amount" in item["detail"]
    assert "only the language model located this value" in item["detail"]


def test_a_readable_invoice_gets_no_exception(app: Engine) -> None:
    case_id = intake(app, "RC-2", gross="119.00")

    assert row_of(app, case_id)["status"] == CaseStatus.RECEIVED
    assert open_items(app, case_id) == []


def test_asking_twice_opens_one_exception(app: Engine) -> None:
    case_id = intake(app, "RC-3", gross=None)
    with unit_of_work(app, INTAKE) as db:
        assert queue_values_to_check(db, case_id) is None
    assert len(open_items(app, case_id)) == 1


def test_a_missing_currency_is_named_too(app: Engine) -> None:
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=800_000 + next(_counter),
            document_number="RC-4",
            gross_cents=11900,
            case_key=f"{issuer()}|RC-4",
        )
        assert flag_if_duplicate(db, case_id) is None
        queue_values_to_check(db, case_id)
    [item] = open_items(app, case_id)
    assert item["facts"] == {"fields": ["currency"]}


# Shown by a test: corrected, gets a key and can go to approval; or caught as a duplicate


def test_a_corrected_case_gets_its_key_and_can_be_sent_for_approval(app: Engine) -> None:
    vat = issuer()
    case_id = intake(app, "RC-10", gross=None, vat=vat)
    cannot_reach_approval(app, case_id)

    done = correct(app, values_exception(app, case_id), "RC-10", 11900)

    assert done.case_id == case_id and done.duplicate_of is None
    row = row_of(app, case_id)
    assert row["status"] == CaseStatus.RECEIVED
    assert row["gross_minor"] == 11900 and row["currency"] == "EUR"
    assert row["duplicate_key"] == duplicate_key(f"{vat}|RC-10", 11900, "EUR")
    assert open_items(app, case_id) == []
    to_approval(app, case_id)
    assert row_of(app, case_id)["status"] == CaseStatus.AWAITING_APPROVAL


def test_a_case_corrected_into_a_known_invoice_is_caught_as_a_duplicate(app: Engine) -> None:
    vat = issuer()
    first_id = intake(app, "RC-11", gross="119.00", vat=vat)
    case_id = intake(app, "RC-11", gross=None, vat=vat)

    done = correct(app, values_exception(app, case_id), "RC-11", 11900)

    assert done.duplicate_of == first_id
    row = row_of(app, case_id)
    assert row["status"] == CaseStatus.EXCEPTION
    assert row["duplicate_of"] == first_id and row["duplicate_key"] is None
    [item] = open_items(app, case_id)
    assert item["reason"] == ExceptionReason.DUPLICATE
    cannot_reach_approval(app, case_id)


def test_a_corrected_number_counts_for_the_duplicate_check(app: Engine) -> None:
    vat = issuer()
    first_id = intake(app, "RC-12", gross="119.00", vat=vat)
    case_id = intake(app, "RC-99", gross=None, vat=vat)

    done = correct(app, values_exception(app, case_id), "RC-12", 11900)

    assert done.duplicate_of == first_id


# The audit log keeps old value, new value, who, when and why


def test_the_audit_log_keeps_old_and_new_values_who_when_and_why(app: Engine) -> None:
    case_id = intake(app, "RC-20", gross=None)
    exception_id = values_exception(app, case_id)

    done = correct(app, exception_id, "RC-20a", 11900, reason="amount on page 2, number typo")

    with app.connect() as connection:
        events = trace(connection, case_id)
    [corrected] = [e for e in events if e.event_type == "case_corrected"]
    assert corrected.actor == f"user:{BENJAMIN_ID}"
    assert corrected.exception_id == exception_id
    assert corrected.occurred_at is not None
    assert corrected.details == {
        "correction_id": done.correction_id,
        "document_number": {"old": "RC-20", "new": "RC-20a"},
        "gross_minor": {"old": None, "new": 11900},
        "currency": {"old": "EUR", "new": "EUR"},
        "name": "Benjamin Test",
        "sign_in_address": "benjamin@max-asp-test.example",
        "reason": "amount on page 2, number typo",
    }
    changed = [e for e in events if e.event_type == "case_changed" and "gross_minor" in e.details]
    assert changed[0].details["gross_minor"] == {"old": None, "new": 11900}
    assert changed[0].actor == f"user:{BENJAMIN_ID}"
    resolved = [e for e in events if e.event_type == "exception_resolved"]
    assert resolved[-1].details["outcome"] == Outcome.DATA_CORRECTED
    with app.connect() as connection:
        stored = (
            connection.execute(
                text("select * from case_correction where id = :id"), {"id": done.correction_id}
            )
            .mappings()
            .one()
        )
    assert stored["corrected_by"] == f"user:{BENJAMIN_ID}"
    assert stored["old_gross_minor"] is None and stored["gross_minor"] == 11900
    assert stored["reason"] == "amount on page 2, number typo"


def test_the_app_cannot_write_a_correction_event_itself(app: Engine) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        db.execute(text("insert into audit_event (event_type) values ('case_corrected')"))


# The database refuses a job, a blank reason and any other way to change the values


def a_flagged_case(app: Engine) -> tuple[int, int]:
    case_id = intake(app, f"RC-{next(_counter)}", gross=None)
    return case_id, values_exception(app, case_id)


def insert_correction(db: Connection, case_id: int, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "document_number": get_case(db, case_id).document_number,
        "gross_cents": 11900,
        "currency": "EUR",
        "reason": "checked the PDF",
        "name": "Benjamin Test",
        **overrides,
    }
    return correct_case(db, case_id, **values)


def test_a_job_cannot_correct_the_values(app: Engine) -> None:
    case_id, _ = a_flagged_case(app)

    with pytest.raises(CaseRuleBroken, match="only a person"), unit_of_work(app, INTAKE) as db:
        insert_correction(db, case_id)
    assert row_of(app, case_id)["gross_minor"] is None


def test_python_also_refuses_a_job_before_asking_the_database(app: Engine) -> None:
    _, exception_id = a_flagged_case(app)
    job = CorrectedBy(INTAKE, "intake")

    with pytest.raises(PermissionError), unit_of_work(app, INTAKE) as db:
        correct_flagged_values(db, exception_id, Correction("X", 1, "EUR", "why"), job)


@pytest.mark.parametrize("reason", ["", "   ", "ok", None])
def test_a_blank_or_missing_reason_is_refused_by_the_database(
    app: Engine, reason: str | None
) -> None:
    case_id, _ = a_flagged_case(app)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, BENJAMIN.actor) as db:
        insert_correction(db, case_id, reason=reason)
    assert row_of(app, case_id)["gross_minor"] is None


def test_a_correction_without_the_persons_name_is_refused(app: Engine) -> None:
    case_id, _ = a_flagged_case(app)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, BENJAMIN.actor) as db:
        insert_correction(db, case_id, name=" ")


@pytest.mark.parametrize(
    "statement",
    [
        "update invoice_case set gross_minor = 12345 where id = :id",
        "update invoice_case set currency = 'USD' where id = :id",
        "update invoice_case set document_number = 'OTHER' where id = :id",
    ],
)
def test_the_values_never_change_without_a_correction_even_for_a_person(
    app: Engine, statement: str
) -> None:
    case_id = intake(app, f"RC-{next(_counter)}", gross="119.00")

    with (
        pytest.raises(CaseRuleBroken, match="only change through a correction"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        db.execute(text(statement), {"id": case_id})


def test_change_case_cannot_set_the_amount_either(app: Engine) -> None:
    case_id = intake(app, f"RC-{next(_counter)}", gross="119.00")

    with (
        pytest.raises(CaseRuleBroken, match="only change through a correction"),
        unit_of_work(app, INTAKE) as db,
    ):
        change_case(db, case_id, gross_cents=1)


def test_a_correction_in_one_transaction_does_not_open_the_door_for_other_values(
    app: Engine,
) -> None:
    case_id, _ = a_flagged_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="only change through a correction"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        insert_correction(db, case_id)
        db.execute(text("update invoice_case set gross_minor = 1 where id = :id"), {"id": case_id})


def test_a_correction_that_changes_nothing_is_refused(app: Engine) -> None:
    case_id = intake(app, f"RC-{next(_counter)}", gross="119.00")

    with (
        pytest.raises(CaseRuleBroken, match="changes nothing"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        insert_correction(db, case_id)


def test_the_values_are_frozen_after_approval(app: Engine) -> None:
    case_id = intake(app, f"RC-{next(_counter)}", gross="119.00")
    to_approval(app, case_id)
    with unit_of_work(app, APPROVER) as db:
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)

    with pytest.raises(CaseRuleBroken, match="frozen"), unit_of_work(app, BENJAMIN.actor) as db:
        insert_correction(db, case_id, gross_cents=12000)


def test_a_correction_cannot_name_the_exception_of_another_case(app: Engine) -> None:
    case_id, _ = a_flagged_case(app)
    _, other_exception = a_flagged_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="not an open exception of case"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        insert_correction(db, case_id, exception_id=other_exception)


@pytest.mark.parametrize(
    "statement",
    [
        "update case_correction set reason = 'something else' where id = :id",
        "delete from case_correction where id = :id",
    ],
)
def test_a_correction_is_kept_as_it_was_made(app: Engine, owner: Engine, statement: str) -> None:
    case_id, exception_id = a_flagged_case(app)
    done = correct(app, exception_id, str(row_of(app, case_id)["document_number"]), 11900)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, BENJAMIN.actor) as db:
        db.execute(text(statement), {"id": done.correction_id})
    with pytest.raises(Exception, match="cannot change"), owner.begin() as connection:
        connection.execute(text(statement), {"id": done.correction_id})


def test_the_app_cannot_set_who_or_the_old_values_itself(app: Engine) -> None:
    case_id, _ = a_flagged_case(app)

    with pytest.raises(NotAllowed), unit_of_work(app, BENJAMIN.actor) as db:
        db.execute(
            text(
                "insert into case_correction (case_id, document_number, gross_minor, currency,"
                " reason, corrected_by_name, corrected_by) values (:id, 'X', 1, 'EUR', 'why',"
                " 'Benjamin Test', 'user:someone-else')"
            ),
            {"id": case_id},
        )


# How the exception ends


def test_the_exception_does_not_end_as_corrected_without_a_correction(app: Engine) -> None:
    case_id, exception_id = a_flagged_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="after a person corrected"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        resolve_exception(db, exception_id, Outcome.DATA_CORRECTED, resolution="done")
    assert row_of(app, case_id)["status"] == CaseStatus.EXCEPTION


def test_the_exception_ends_only_as_corrected_or_rejected(app: Engine) -> None:
    case_id, exception_id = a_flagged_case(app)

    with (
        pytest.raises(CaseRuleBroken, match="data_corrected or rejected"),
        unit_of_work(app, BENJAMIN.actor) as db,
    ):
        resolve_exception(db, exception_id, Outcome.RETRIED)

    with unit_of_work(app, BENJAMIN.actor) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            resolution="not our invoice",
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    assert row_of(app, case_id)["status"] == CaseStatus.CLOSED


def test_values_to_check_always_belong_to_a_case(app: Engine) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="belong to a case"),
        unit_of_work(app, INTAKE) as db,
    ):
        open_exception(db, ExceptionReason.VALUES_TO_CHECK, "no case", email_id=1)


def test_only_an_open_values_to_check_exception_can_be_corrected(app: Engine) -> None:
    case_id, exception_id = a_flagged_case(app)
    number = str(row_of(app, case_id)["document_number"])
    correct(app, exception_id, number, 11900)

    with pytest.raises(NotCorrectable):
        correct(app, exception_id, number, 12000)

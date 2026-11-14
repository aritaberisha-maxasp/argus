"""A person corrects the number, amount or currency of a flagged invoice (#112, EX-01, IN-10).

When ARGUS cannot read the gross amount or the currency of an incoming invoice or credit note
with confidence, the case gets no duplicate key, and the database refuses approval without one
(migration 0010). Such a case would wait forever. So:

1. :func:`queue_values_to_check` puts it in the exception queue with the reason
   ``values_to_check`` when the intake opens it, naming the values that are missing.
2. Benjamin (finance role) or Arita (admin) opens it on the queue page, looks at the invoice and
   enters the right number, amount and currency with a short reason.
   :func:`correct_flagged_values` stores the correction (migration 0013 keeps the old values,
   who, when and why, and writes them into the audit log), resolves the exception and runs the
   duplicate check again, all in one unit of work.
3. If the corrected invoice is a duplicate, the case goes to the duplicate exception. If not,
   it gets its duplicate key and goes back to where it was, ready for approval like any other
   case. Nobody else has to touch it.

Logs name only IDs, never the values or the reason (NF-03).
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text

from argus.cases.model import Actor, ExceptionReason, Outcome
from argus.cases.store import correct_case, open_exception, resolve_exception
from argus.intake.duplicates import flag_if_duplicate

# The values a correction sets, in the words the reading uses for them.
FIELDS = ("invoice_number", "gross_amount", "currency")
_FIELD_WORDS = {
    "invoice_number": "the invoice number",
    "gross_amount": "the gross amount",
    "currency": "the currency",
}


class NotCorrectable(Exception):
    """The exception is not open, or it is not about values to check."""


@dataclass(frozen=True)
class CorrectedBy:
    """The person who corrects, as they were at that moment (ADR 0008): the object ID alone
    names nobody once the Entra account is gone."""

    actor: Actor  # user:<Entra object ID>
    name: str
    sign_in_address: str | None = None


@dataclass(frozen=True)
class Correction:
    document_number: str
    gross_cents: int
    currency: str
    reason: str


@dataclass(frozen=True)
class Corrected:
    case_id: int
    correction_id: int
    # The case it collides with when the corrected invoice is a duplicate, else None and the
    # case carries on toward approval.
    duplicate_of: int | None


def missing_values(
    document_number: str | None, gross_cents: int | None, currency: str | None
) -> list[str]:
    """Which of the three values a case still lacks, in the reading's words."""
    found = {
        "invoice_number": bool(document_number and document_number.strip()),
        "gross_amount": gross_cents is not None,
        "currency": bool(currency),
    }
    return [name for name in FIELDS if not found[name]]


def queue_values_to_check(
    connection: Connection, case_id: int, notes: dict[str, str] | None = None
) -> int | None:
    """Put a case in the exception queue when it has no duplicate key because its number,
    amount or currency is missing (EX-01). Run in the unit of work that opened the case, after
    :func:`flag_if_duplicate`. Returns the exception ID, or None when the case needs nothing
    (it has its key, waits as a duplicate already, or is closed).

    ``notes`` are the reading's notes per field, for the person (never into the audit log)."""
    row = (
        connection.execute(
            text(
                """
                select status, document_number, gross_minor, currency, duplicate_key,
                       exists (select 1 from exception_item x where x.case_id = c.id
                               and x.resolved_at is null
                               and x.reason in ('duplicate', 'values_to_check')) as waiting
                from invoice_case c where id = :id
                """
            ),
            {"id": case_id},
        )
        .mappings()
        .one()
    )
    if row["duplicate_key"] is not None or row["waiting"] or row["status"] == "closed":
        return None
    missing = missing_values(row["document_number"], row["gross_minor"], row["currency"])
    if not missing:
        # All three are there, yet no key: the issuer is not known (the intake does not open
        # such cases, #30). Name all three so the person checks them together.
        missing = list(FIELDS)
    words = [_FIELD_WORDS[name] for name in missing]
    listed = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    detail = (
        f"ARGUS could not read {listed} of this invoice with confidence, so it cannot check it"
        " for duplicates or send it for approval. Look at the invoice, enter the invoice number,"
        " the gross amount and the currency as printed, and say briefly why. ARGUS then checks"
        " for duplicates again and lets the case carry on."
    )
    notes = notes or {}
    extra = [f"{_FIELD_WORDS[n].capitalize()}: {notes[n]}." for n in missing if notes.get(n)]
    if extra:
        detail += " " + " ".join(extra)
    return open_exception(
        connection,
        ExceptionReason.VALUES_TO_CHECK,
        detail,
        case_id=case_id,
        # Field names only: no values or free text in the audit log (NF-03).
        facts={"fields": missing},
    )


def correct_flagged_values(
    connection: Connection, exception_id: int, correction: Correction, by: CorrectedBy
) -> Corrected:
    """Correct the values of the case behind a ``values_to_check`` exception, resolve it and
    check the case for duplicates again, in the caller's unit of work, which must run under
    ``by.actor``. The database refuses a job, a blank reason and a correction that changes
    nothing (migration 0013)."""
    if by.actor.kind != "user":
        raise PermissionError("only a person can correct the values of an invoice")
    row = (
        connection.execute(
            text(
                "select reason, case_id from exception_item"
                " where id = :id and resolved_at is null for update"
            ),
            {"id": exception_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["case_id"] is None
        or row["reason"] != ExceptionReason.VALUES_TO_CHECK.value
    ):
        raise NotCorrectable(f"exception {exception_id} is not an open values to check exception")
    case_id = int(row["case_id"])
    correction_id = correct_case(
        connection,
        case_id,
        document_number=correction.document_number.strip(),
        gross_cents=correction.gross_cents,
        currency=correction.currency.strip().upper(),
        reason=correction.reason.strip(),
        name=by.name,
        sign_in_address=by.sign_in_address,
        exception_id=exception_id,
    )
    # The reason is on the correction and in the audit log; the exception points to it.
    resolve_exception(
        connection,
        exception_id,
        Outcome.DATA_CORRECTED,
        resolution=f"Corrected by correction {correction_id}.",
    )
    duplicate_of = flag_if_duplicate(connection, case_id)
    return Corrected(case_id, correction_id, duplicate_of)

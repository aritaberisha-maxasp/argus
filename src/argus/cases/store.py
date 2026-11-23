"""Create cases, move them between statuses, open and resolve exceptions, write audit events.

Every change runs inside :func:`unit_of_work`, which opens one transaction and names its actor.
The database checks every rule (ADR 0005, design note section 2) and writes the audit events
itself, in the same transaction. This module only gives those rules readable names: whatever the
database refuses comes back as :class:`CaseRuleBroken`.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import errors as pg
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome


class CaseRuleBroken(Exception):
    """The database refused a change because it breaks a rule of the case model."""


class DuplicateRefused(CaseRuleBroken):
    """The same invoice, credit note or bank transaction is already there (IN-10, BA-07)."""


class NotAllowed(CaseRuleBroken):
    """The application's database user has no right to make this change, for example to
    rewrite the audit log or set a field only the database writes."""


@dataclass(frozen=True)
class Case:
    id: int
    kind: CaseKind
    status: CaseStatus
    resume_status: CaseStatus | None
    partner_account: str | None
    document_number: str | None
    gross_cents: int | None
    currency: str | None
    datev_booking_ref: str | None
    closed_reason: ClosedReason | None


@dataclass(frozen=True)
class TraceEvent:
    chain_seq: int
    occurred_at: datetime
    event_type: str
    actor: str
    case_id: int | None
    email_id: int | None
    exception_id: int | None
    from_status: CaseStatus | None
    to_status: CaseStatus | None
    details: dict[str, Any]


@dataclass(frozen=True)
class OpenException:
    """One entry in the exception queue that still needs a person (EX-01). Exactly one of
    ``case_id``, ``email_id`` and ``statement_id`` is set, except for a failed nightly copy of
    the audit log or its check (#83), which has none of them."""

    id: int
    reason: ExceptionReason
    detail: str
    opened_at: datetime
    age: timedelta
    case_id: int | None
    case_kind: CaseKind | None
    resume_status: CaseStatus | None
    email_id: int | None
    statement_id: int | None


# Fields the application may change on a case, mapped to their column. Status, closed reason and
# booking reference go through move_case so a move and its data are one audited change.
CHANGEABLE = {
    "partner_account": "partner_account",
    "document_number": "document_number",
    "gross_cents": "gross_minor",
    "currency": "currency",
    "duplicate_key": "duplicate_key",
    "duplicate_of": "duplicate_of",
    "payment_hold": "payment_hold",
    "direct_debit": "direct_debit",
    "reminder_received_at": "reminder_received_at",
}


def _refusal(error: DBAPIError) -> CaseRuleBroken | None:
    cause = error.orig
    message = str(cause).splitlines()[0] if cause is not None else str(error)
    if isinstance(cause, pg.UniqueViolation):
        return DuplicateRefused(message)
    if isinstance(cause, pg.InsufficientPrivilege):
        return NotAllowed(message)
    if isinstance(cause, pg.RaiseException | pg.IntegrityError):
        return CaseRuleBroken(message)
    return None


@contextmanager
def unit_of_work(engine: Engine, actor: Actor) -> Iterator[Connection]:
    """One transaction with its actor set. Commits at the end, rolls back on any error.

    Some rules are checked only at commit (an open exception must stop its case), so a refusal
    can also come from leaving the ``with`` block.
    """
    try:
        with engine.begin() as connection:
            set_actor(connection, actor)
            yield connection
    except DBAPIError as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from error


def _run(connection: Connection, sql: str, params: Mapping[str, Any]) -> Any:
    try:
        return connection.execute(text(sql), dict(params))
    except DBAPIError as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from error


def set_actor(connection: Connection, actor: Actor) -> None:
    """Name who makes the changes in this transaction (``SET LOCAL argus.actor``).

    For a person it also sets their name and sign in address (``argus.actor_name`` and
    ``argus.actor_address``), which the database keeps with their decisions (ADR 0008). A job
    sets them empty, so nothing from an earlier transaction carries over."""
    person = actor.kind == "user"
    _run(
        connection,
        """
        select set_config('argus.actor', :actor, true),
               set_config('argus.actor_name', :name, true),
               set_config('argus.actor_address', :address, true)
        """,
        {
            "actor": str(actor),
            "name": (actor.display_name or "") if person else "",
            "address": (actor.sign_in_address or "") if person else "",
        },
    )


def name_person(connection: Connection, name: str, sign_in_address: str | None) -> None:
    """Name the person of this transaction, as they were at that moment (ADR 0008), when the
    caller knows the name better than the actor does. The actor itself stays as it was."""
    _run(
        connection,
        """
        select set_config('argus.actor_name', :name, true),
               set_config('argus.actor_address', :address, true)
        """,
        {"name": name, "address": sign_in_address or ""},
    )


def create_case(
    connection: Connection,
    kind: CaseKind,
    *,
    source_email_id: int | None = None,
    partner_account: str | None = None,
    document_number: str | None = None,
    gross_cents: int | None = None,
    currency: str | None = None,
    duplicate_key: str | None = None,
    direct_debit: bool = False,
    case_key: str | None = None,
) -> int:
    """Create a case in Received. The database logs ``case_created``. Returns the case ID.

    ``case_key`` names one invoice of the source email (issuer plus number, #30); the database
    refuses a second case with the same key for the same email."""
    if gross_cents is not None and (
        isinstance(gross_cents, bool) or not isinstance(gross_cents, int)
    ):
        raise TypeError("gross_cents must be whole cents, use argus.cases.money.to_cents")
    result = _run(
        connection,
        """
        insert into invoice_case (kind, source_email_id, partner_account, document_number,
                                  gross_minor, currency, duplicate_key, direct_debit, case_key)
        values (:kind, :source_email_id, :partner_account, :document_number,
                :gross_minor, :currency, :duplicate_key, :direct_debit, :case_key)
        returning id
        """,
        {
            "kind": kind.value,
            "source_email_id": source_email_id,
            "partner_account": partner_account,
            "document_number": document_number,
            "gross_minor": gross_cents,
            "currency": currency,
            "duplicate_key": duplicate_key,
            "direct_debit": direct_debit,
            "case_key": case_key,
        },
    )
    case_id: int = result.scalar_one()
    return case_id


def get_case(connection: Connection, case_id: int) -> Case:
    row = (
        _run(
            connection,
            """
            select id, kind, status, resume_status, partner_account, document_number,
                   gross_minor, currency, datev_booking_ref, closed_reason
            from invoice_case where id = :id
            """,
            {"id": case_id},
        )
        .mappings()
        .one()
    )
    return Case(
        id=row["id"],
        kind=CaseKind(row["kind"]),
        status=CaseStatus(row["status"]),
        resume_status=CaseStatus(row["resume_status"]) if row["resume_status"] else None,
        partner_account=row["partner_account"],
        document_number=row["document_number"],
        gross_cents=row["gross_minor"],
        currency=row["currency"],
        datev_booking_ref=row["datev_booking_ref"],
        closed_reason=ClosedReason(row["closed_reason"]) if row["closed_reason"] else None,
    )


def move_case(
    connection: Connection,
    case_id: int,
    *,
    expected: CaseStatus,
    to: CaseStatus,
    closed_reason: ClosedReason | None = None,
    datev_booking_ref: str | None = None,
) -> bool:
    """Move a case from ``expected`` to ``to``. The database refuses any move that is not
    allowed and logs ``status_changed``.

    Returns False when the case is no longer in ``expected`` because another worker moved it
    first (design note rule 10). The caller then reads the case again and decides.
    """
    result = _run(
        connection,
        """
        update invoice_case
        set status = :to,
            closed_reason = coalesce(:closed_reason, closed_reason),
            datev_booking_ref = coalesce(:datev_booking_ref, datev_booking_ref)
        where id = :id and status = :expected
        returning id
        """,
        {
            "id": case_id,
            "expected": expected.value,
            "to": to.value,
            "closed_reason": closed_reason.value if closed_reason else None,
            "datev_booking_ref": datev_booking_ref,
        },
    )
    return result.first() is not None


def change_case(connection: Connection, case_id: int, **fields: Any) -> None:
    """Change data on a case without moving it, for example the matched DATEV account. The
    database refuses changes it does not allow (booking data is frozen after approval) and logs
    ``case_changed`` with old and new values. The number, amount and currency only change
    through :func:`correct_case` (migration 0013); the database refuses them here."""
    unknown = set(fields) - set(CHANGEABLE)
    if unknown:
        raise ValueError(f"cannot change {', '.join(sorted(unknown))} with change_case")
    if not fields:
        return
    gross = fields.get("gross_cents")
    if gross is not None and (isinstance(gross, bool) or not isinstance(gross, int)):
        raise TypeError("gross_cents must be whole cents, use argus.cases.money.to_cents")
    # Column names come from the CHANGEABLE allow list only, values are bound parameters.
    assignments = ", ".join(f"{CHANGEABLE[name]} = :{name}" for name in fields)
    sql = f"update invoice_case set {assignments} where id = :id"  # noqa: S608
    _run(connection, sql, {**fields, "id": case_id})


def correct_case(
    connection: Connection,
    case_id: int,
    *,
    document_number: str,
    gross_cents: int,
    currency: str,
    reason: str,
    name: str,
    sign_in_address: str | None = None,
    exception_id: int | None = None,
) -> int:
    """A person corrects the invoice number, gross amount and currency of a case (#112).
    Returns the correction's ID.

    Run in a unit of work under that person. The database refuses a job as actor, a blank
    reason and a correction that changes nothing, keeps the old values, puts the new ones on
    the case and writes ``case_corrected`` into the audit log with the person's name (ADR 0008)
    and the reason. The duplicate key is cleared, so call
    :func:`argus.intake.duplicates.flag_if_duplicate` in the same unit of work afterwards.
    """
    if isinstance(gross_cents, bool) or not isinstance(gross_cents, int):
        raise TypeError("gross_cents must be whole cents, use argus.cases.money.to_cents")
    result = _run(
        connection,
        """
        insert into case_correction (case_id, exception_id, document_number, gross_minor,
                                     currency, reason, corrected_by_name, corrected_by_address)
        values (:case_id, :exception_id, :document_number, :gross_minor, :currency, :reason,
                :name, :address)
        returning id
        """,
        {
            "case_id": case_id,
            "exception_id": exception_id,
            "document_number": document_number,
            "gross_minor": gross_cents,
            "currency": currency,
            "reason": reason,
            "name": name,
            "address": sign_in_address,
        },
    )
    correction_id: int = result.scalar_one()
    return correction_id


def open_exception(
    connection: Connection,
    reason: ExceptionReason,
    detail: str,
    *,
    case_id: int | None = None,
    email_id: int | None = None,
    statement_id: int | None = None,
    facts: Mapping[str, Any] | None = None,
) -> int:
    """Put something in the exception queue (EX-01). For a case, the case moves to Exception
    in the same transaction, because an open exception must stop it.

    ``detail`` is for the person who acts on it and stays on the exception. ``facts`` go into
    the audit log, so they must hold no personal data or free text (NF-03).
    """
    result = _run(
        connection,
        """
        insert into exception_item (case_id, email_id, statement_id, reason, detail, facts)
        values (:case_id, :email_id, :statement_id, :reason, :detail, cast(:facts as jsonb))
        returning id
        """,
        {
            "case_id": case_id,
            "email_id": email_id,
            "statement_id": statement_id,
            "reason": reason.value,
            "detail": detail,
            "facts": json.dumps(dict(facts or {})),
        },
    )
    exception_id: int = result.scalar_one()
    if case_id is not None:
        current = get_case(connection, case_id).status
        if current is not CaseStatus.EXCEPTION:
            move_case(connection, case_id, expected=current, to=CaseStatus.EXCEPTION)
    return exception_id


def resolve_exception(
    connection: Connection,
    exception_id: int,
    outcome: Outcome,
    *,
    resolution: str | None = None,
    case_to: CaseStatus | None = None,
    closed_reason: ClosedReason | None = None,
) -> None:
    """Resolve an exception. The database records who and when.

    For a case with no other open exception, the case leaves Exception in the same
    transaction: back to the status it came from, or to ``case_to`` (closed, or an exit listed
    for the reason, such as payment failed back to Booked). The database refuses anything else.
    """
    _run(
        connection,
        "update exception_item set outcome = :outcome, resolution = :resolution where id = :id",
        {"id": exception_id, "outcome": outcome.value, "resolution": resolution},
    )
    row = (
        _run(
            connection,
            """
            select c.id, c.resume_status,
                   exists (select 1 from exception_item o
                           where o.case_id = c.id and o.resolved_at is null) as still_open
            from exception_item e join invoice_case c on c.id = e.case_id
            where e.id = :id
            """,
            {"id": exception_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["still_open"]:
        return
    target = case_to or CaseStatus(row["resume_status"])
    move_case(
        connection,
        row["id"],
        expected=CaseStatus.EXCEPTION,
        to=target,
        closed_reason=closed_reason,
    )


def list_open_exceptions(
    connection: Connection, *, reason: ExceptionReason | None = None
) -> list[OpenException]:
    """Everything in the exception queue that is not resolved yet, oldest first (EX-01).

    Each entry names its case (or email or bank statement), the reason, how long it has been
    open and the detail for the person who acts on it. For a case it also says the status the
    case goes back to once the exception is resolved. ``reason`` narrows the list to one reason.
    The age is measured by the database clock, the same clock that set ``opened_at``.
    """
    rows = _run(
        connection,
        """
        select e.id, e.reason, e.detail, e.opened_at, clock_timestamp() - e.opened_at as age,
               e.case_id, c.kind as case_kind, c.resume_status, e.email_id, e.statement_id
        from exception_item e left join invoice_case c on c.id = e.case_id
        where e.resolved_at is null
          and (cast(:reason as exception_reason) is null or e.reason = :reason)
        order by e.opened_at, e.id
        """,
        {"reason": reason.value if reason else None},
    ).mappings()
    return [
        OpenException(
            id=r["id"],
            reason=ExceptionReason(r["reason"]),
            detail=r["detail"],
            opened_at=r["opened_at"],
            age=r["age"],
            case_id=r["case_id"],
            case_kind=CaseKind(r["case_kind"]) if r["case_kind"] else None,
            resume_status=CaseStatus(r["resume_status"]) if r["resume_status"] else None,
            email_id=r["email_id"],
            statement_id=r["statement_id"],
        )
        for r in rows
    ]


def record_event(
    connection: Connection,
    event_type: str,
    *,
    case_id: int | None = None,
    email_id: int | None = None,
    statement_id: int | None = None,
    exception_id: int | None = None,
    payment_run_id: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> int:
    """Add an event to the audit log, for example an approval decision (AP-08) or an email
    being classified. Time and actor come from the database. Returns its chain number.

    Case and exception changes are logged by the database itself and cannot be written here.
    """
    result = _run(
        connection,
        """
        insert into audit_event (case_id, email_id, statement_id, exception_id, payment_run_id,
                                 event_type, details)
        values (:case_id, :email_id, :statement_id, :exception_id, :payment_run_id,
                :event_type, cast(:details as jsonb))
        returning chain_seq
        """,
        {
            "case_id": case_id,
            "email_id": email_id,
            "statement_id": statement_id,
            "exception_id": exception_id,
            "payment_run_id": payment_run_id,
            "event_type": event_type,
            "details": json.dumps(dict(details or {})),
        },
    )
    chain_seq: int = result.scalar_one()
    return chain_seq


def trace(connection: Connection, case_id: int) -> list[TraceEvent]:
    """Everything that happened to a case, oldest first, including its email (AC-8)."""
    rows = _run(
        connection,
        """
        select chain_seq, occurred_at, event_type, actor_type || ':' || actor as actor,
               case_id, email_id, exception_id, from_status, to_status, details
        from case_trace where trace_case_id = :id order by chain_seq
        """,
        {"id": case_id},
    ).mappings()
    return [
        TraceEvent(
            chain_seq=r["chain_seq"],
            occurred_at=r["occurred_at"],
            event_type=r["event_type"],
            actor=r["actor"],
            case_id=r["case_id"],
            email_id=r["email_id"],
            exception_id=r["exception_id"],
            from_status=CaseStatus(r["from_status"]) if r["from_status"] else None,
            to_status=CaseStatus(r["to_status"]) if r["to_status"] else None,
            details=r["details"],
        )
        for r in rows
    ]


def verify_chain(connection: Connection) -> int | None:
    """Check the whole audit chain. Returns the chain number of the first broken link, or None
    when the chain is intact."""
    broken: int | None = _run(connection, "select broken_at from audit_verify()", {}).scalar()
    return broken

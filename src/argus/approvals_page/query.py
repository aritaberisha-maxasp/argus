"""The approver sends an invoice back with a question, and Benjamin answers it (#192).

AP-03 gives the approver a third way next to approve and reject: send the invoice back with a
question. The case goes to the exception queue (EX-01), where Benjamin (finance) or Arita
(admin) answers it, or rejects the invoice. An answer sends the case back to Awaiting approval
with a new approval request (#191) to the person who asked, or to the deputy when that person
is now away. The new request starts a new 3 and 5 working day clock for #40 (AP-05).

Each step is one unit of work as the signed in person. The database checks everything again
(migration 0022): only the person the newest request went to may ask, only while the case
waits; only a person may answer, with at least three characters, or reject; a job never can.
It keeps who asked and who answered, with name and sign in address as they were then, and
writes ``approval_queried`` and ``approval_query_answered`` to the audit log.

Nothing here approves, books or pays (NF-04). Logs name only IDs, never a question, answer or
name (NF-03).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from argus.approvals_page.decide import _still_waits
from argus.cases.model import CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import move_case, resolve_exception
from argus.intake.mode import trimmed_reason

TEXT_MIN, TEXT_MAX = 3, 500

# Shown in the queue as why ARGUS stopped. The question itself is shown next to it.
DETAIL = "The approver sent the invoice back with a question instead of deciding."


class BadText(ValueError):
    """A question, answer or reason the person can fix, with a sentence that says how."""


class NotAnswerable(Exception):
    """No open approver question with this exception ID."""


class OtherExceptionOpen(Exception):
    """The case has another open exception. It is resolved first, so the answer can send the
    case straight back to the approver."""


def checked_text(value: str | None, what: str) -> str:
    """The text as stored, trimmed like the database does (correction_trim), or BadText."""
    trimmed = trimmed_reason(value or "")
    if len(trimmed) < TEXT_MIN:
        raise BadText(f"Write {what} of at least {TEXT_MIN} characters.")
    if len(trimmed) > TEXT_MAX:
        raise BadText(f"Keep {what} to {TEXT_MAX} characters.")
    return trimmed


@dataclass(frozen=True)
class Asked:
    case_id: int
    query_id: int
    exception_id: int


def ask(db: Connection, case_id: int, request_id: int, question: str | None) -> Asked:
    """Send the case back with a question, in the caller's unit of work (whose actor is the
    person asked). The case moves from Awaiting approval to Exception with the reason
    ``approver_query``. Raises NotWaiting or VendorChanged like a decision does."""
    stored = checked_text(question, "a question")
    _still_waits(db, case_id, request_id)
    exception_id: int = db.execute(
        text(
            """
            insert into exception_item (case_id, reason, detail, facts)
            values (:case_id, :reason, :detail, cast(:facts as jsonb))
            returning id
            """
        ),
        {
            "case_id": case_id,
            "reason": ExceptionReason.APPROVER_QUERY.value,
            "detail": DETAIL,
            "facts": f'{{"request_id": {int(request_id)}}}',
        },
    ).scalar_one()
    query_id: int = db.execute(
        text(
            """
            insert into approval_query (case_id, request_id, exception_id, question)
            values (:case_id, :request_id, :exception_id, :question)
            returning id
            """
        ),
        {
            "case_id": case_id,
            "request_id": request_id,
            "exception_id": exception_id,
            "question": stored,
        },
    ).scalar_one()
    move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.EXCEPTION)
    return Asked(case_id, query_id, exception_id)


@dataclass(frozen=True)
class Answered:
    case_id: int
    outcome: Outcome
    request_id: int | None  # the new approval request, after an answer
    recipient_name: str | None  # who it goes to


def _open_query(db: Connection, exception_id: int) -> tuple[int, int]:
    """Lock the open approver question. Returns the case ID and how many other exceptions of
    the case are open."""
    row = db.execute(
        text(
            """
            select e.case_id,
                   (select count(*) from exception_item o
                    where o.case_id = e.case_id and o.resolved_at is null and o.id <> e.id)
                     as others
            from exception_item e
            where e.id = :id and e.resolved_at is null and e.reason = 'approver_query'
            for update of e
            """
        ),
        {"id": exception_id},
    ).one_or_none()
    if row is None:
        raise NotAnswerable(exception_id)
    return int(row.case_id), int(row.others)


def answer(db: Connection, exception_id: int, reply: str | None) -> Answered:
    """Answer the approver's question, in the caller's unit of work (whose actor is the person
    answering). The answer is the exception's resolution. A new approval request goes to the
    person who asked (the database picks who), and the case goes back to Awaiting approval.
    The next sweep sends the request email (#191)."""
    stored = checked_text(reply, "an answer")
    case_id, others = _open_query(db, exception_id)
    if others:
        raise OtherExceptionOpen(case_id)
    db.execute(
        text(
            "update exception_item set outcome = :outcome, resolution = :resolution where id = :id"
        ),
        {"id": exception_id, "outcome": Outcome.ANSWERED.value, "resolution": stored},
    )
    request = db.execute(
        text("insert into approval_request (case_id) values (:id) returning id, recipient_name"),
        {"id": case_id},
    ).one()
    move_case(db, case_id, expected=CaseStatus.EXCEPTION, to=CaseStatus.AWAITING_APPROVAL)
    return Answered(case_id, Outcome.ANSWERED, int(request.id), str(request.recipient_name))


def reject(db: Connection, exception_id: int, reason: str | None) -> Answered:
    """Reject the invoice instead of answering, with a reason, in the caller's unit of work.
    The case closes as rejected (AP-07). Only when it is the case's only open exception."""
    stored = checked_text(reason, "a reason")
    case_id, others = _open_query(db, exception_id)
    if others:
        raise OtherExceptionOpen(case_id)
    resolve_exception(
        db,
        exception_id,
        Outcome.REJECTED,
        resolution=stored,
        case_to=CaseStatus.CLOSED,
        closed_reason=ClosedReason.REJECTED,
    )
    return Answered(case_id, Outcome.REJECTED, None, None)


@dataclass(frozen=True)
class Query:
    """One question and, once there is one, its answer, as the pages show them."""

    query_id: int
    request_id: int
    exception_id: int
    question: str
    asked_by: str  # user:<Entra object ID>; not for the pages
    asked_by_name: str
    asked_as: str
    on_behalf_of_name: str | None
    asked_at: datetime
    outcome: str | None  # answered or rejected, None while open
    answer: str | None  # the answer, or the reason for rejecting
    answered_by_name: str | None
    answered_at: datetime | None


def queries_of(db: Connection, case_id: int) -> list[Query]:
    """Every question of the case, oldest first, with its answer."""
    rows = db.execute(
        text(
            """
            select q.id, q.request_id, q.exception_id, q.question, q.asked_by, q.asked_by_name,
                   q.asked_as, q.on_behalf_of_name, q.asked_at, e.outcome, e.resolution,
                   e.resolved_by_name, e.resolved_at
            from approval_query q join exception_item e on e.id = q.exception_id
            where q.case_id = :id
            order by q.id
            """
        ),
        {"id": case_id},
    ).mappings()
    return [
        Query(
            query_id=r["id"],
            request_id=r["request_id"],
            exception_id=r["exception_id"],
            question=r["question"],
            asked_by=r["asked_by"],
            asked_by_name=r["asked_by_name"],
            asked_as=r["asked_as"],
            on_behalf_of_name=r["on_behalf_of_name"],
            asked_at=r["asked_at"],
            outcome=r["outcome"],
            answer=r["resolution"],
            answered_by_name=r["resolved_by_name"],
            answered_at=r["resolved_at"],
        )
        for r in rows
    ]


def query_of_exception(db: Connection, exception_id: int) -> Query | None:
    row = db.execute(
        text("select case_id from approval_query where exception_id = :id"), {"id": exception_id}
    ).scalar_one_or_none()
    if row is None:
        return None
    return next((q for q in queries_of(db, int(row)) if q.exception_id == exception_id), None)

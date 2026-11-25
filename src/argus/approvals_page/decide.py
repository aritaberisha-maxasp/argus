"""The approver approves, rejects or changes the account (#38, AP-03, AP-07, AP-08, PR-04).

Each step is one unit of work as the signed in person. The database checks everything again
(migration 0021): only the person the newest approval request went to, only while the case
waits, one decision per request, a comment for a rejection, and a current account proposal for
an approval. It records who, with their name and sign in address as they were then, and when,
and writes ``approval_decided`` or ``account_proposed`` to the audit log.

Nothing here books or pays (NF-04). Logs name only IDs, never a name, comment or reason (NF-03).
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, text

from argus.cases.model import CaseStatus, ClosedReason
from argus.cases.store import move_case
from argus.intake.accounts import Chart, EnteredAccount, EnteredBy, store_entered
from argus.intake.mode import trimmed_reason

COMMENT_MIN, COMMENT_MAX = 3, 500


class Choice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class BadComment(ValueError):
    """A comment the person can fix, with a sentence that says how."""


class NotWaiting(Exception):
    """The case no longer waits for this request: someone decided already, or a newer request
    went out. The page shows what stands now."""


def checked_comment(choice: Choice, comment: str | None) -> str | None:
    """The comment as stored, or BadComment. A rejection needs one of at least three characters
    other than spaces, tabs or line breaks, trimmed like the database does (correction_trim)."""
    trimmed = trimmed_reason(comment or "")
    if choice is Choice.REJECT and len(trimmed) < COMMENT_MIN:
        raise BadComment("Say briefly why you reject the invoice (at least 3 characters).")
    if len(trimmed) > COMMENT_MAX:
        raise BadComment(f"Keep the comment to {COMMENT_MAX} characters.")
    if trimmed and len(trimmed) < COMMENT_MIN:
        raise BadComment("Write at least 3 characters, or leave the comment empty.")
    return trimmed or None


@dataclass(frozen=True)
class Decided:
    case_id: int
    decision_id: int
    status: CaseStatus


def _still_waits(db: Connection, case_id: int, request_id: int) -> None:
    """Lock the case and check it still waits for this request, so a second click or tab gets
    a plain answer instead of a database refusal."""
    row = db.execute(
        text(
            """
            select c.status,
                   (select max(id) from approval_request where case_id = c.id) as newest,
                   exists (select 1 from approval_decision where request_id = :request)
                     as decided
            from invoice_case c where c.id = :id for update
            """
        ),
        {"id": case_id, "request": request_id},
    ).one_or_none()
    if (
        row is None
        or row.status != CaseStatus.AWAITING_APPROVAL.value
        or row.newest != request_id
        or row.decided
    ):
        raise NotWaiting(case_id)


def decide(
    db: Connection, case_id: int, request_id: int, choice: Choice, comment: str | None
) -> Decided:
    """Record the decision and move the case, in the caller's unit of work (whose actor is the
    person). Approve moves it to Approved, reject closes it as rejected (AP-07)."""
    stored = checked_comment(choice, comment)
    _still_waits(db, case_id, request_id)
    decision_id: int = db.execute(
        text(
            """
            insert into approval_decision (case_id, request_id, decision, comment)
            values (:case_id, :request_id, :decision, :comment)
            returning id
            """
        ),
        {
            "case_id": case_id,
            "request_id": request_id,
            "decision": "approved" if choice is Choice.APPROVE else "rejected",
            "comment": stored,
        },
    ).scalar_one()
    if choice is Choice.APPROVE:
        to, closed = CaseStatus.APPROVED, None
    else:
        to, closed = CaseStatus.CLOSED, ClosedReason.REJECTED
    if not move_case(
        db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=to, closed_reason=closed
    ):
        raise NotWaiting(case_id)
    return Decided(case_id, decision_id, to)


def change_account(
    db: Connection,
    case_id: int,
    request_id: int,
    entered: EnteredAccount,
    by: EnteredBy,
    chart: Chart,
) -> int:
    """Store the account the approver chose as the case's new current proposal, before they
    decide, in the caller's unit of work. Same path as Benjamin's entries on the queue page.
    Returns the proposal ID."""
    _still_waits(db, case_id, request_id)
    account: str = db.execute(
        text("select partner_account from invoice_case where id = :id"), {"id": case_id}
    ).scalar_one()
    return store_entered(db, case_id, account, entered, by, chart)

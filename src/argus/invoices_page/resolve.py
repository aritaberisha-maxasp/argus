"""Resolving an exception from the invoice status page (#62, EX-01, NF-05).

Benjamin or Arita picks how the exception ends and says why in a short comment. The page only
offers endings that are safe for the case as it stands.

Who checks what:

- **The database** (migrations 0002, 0010, 0013, 0016 and 0019) records who resolved it and
  when, and for a person also their name and sign in address as they were at that moment
  (ADR 0008). It refuses any change to a resolved exception. It lets an IBAN mismatch end only
  as fixed in DATEV or rejected, lets "not a duplicate" come only from a person with a reason,
  and lets values to check end as data corrected only after a correction. Since 0016 it also
  holds the rules this page used to hold alone:

  - Every ending that lets the case carry on ("Checked, carry on", "answered", "fixed in
    DATEV" and every other one that does not close it) comes only from a person, with a
    comment that is not blank. A short list of automatic endings stays with the job that
    checks again: the IBAN recheck (#36, its own rule follows in #186), an unknown vendor the
    vendor match found, a vendor that now has an approver, an approver who is back or has a
    Max ASP address again, a missing account found in the booking history, and a bank balance
    difference after the corrected statement.
  - Rejecting or confirming a duplicate closes the case, so it is refused for an outgoing
    invoice or credit note, a bank transaction, and a case that was booked, in a payment run
    or paid before the exception.
  - A person who resolves must be named.

- **This page only**: which endings fit which reason, that a comment of at least three
  visible characters is given for every ending (also closing ones), and that the case has no
  other open exception before it closes. The database stays the last word: if it refuses,
  the page shows its sentence.

What each exception can end as here:

- **Duplicate**: it is a duplicate (the case closes as a duplicate), it is not a duplicate (the
  case carries on, the comment says why), or reject the invoice.
- **Values to check**: reject the invoice. Correcting the values stays on the exception queue
  page, which has the form for it (#112).
- **IBAN mismatch**: reject the invoice. "Fixed in DATEV" is never offered here: ARGUS checks
  the IBAN against DATEV again on every sweep and ends the block by itself once DATEV holds the
  invoice's IBAN (#36, PR-05, VM-04). A click cannot stand in for that check.
- **Payment failed**: nothing yet. Where the case goes depends on the payment run (#40 onwards).
- **Customer question**: answered, the case carries on.
- **Booking error**: fixed in DATEV, the case carries on.
- **Every other reason**: checked, the case carries on, or reject the invoice.

Rejecting or confirming a duplicate closes the case, so it is offered only when:

- the case is an incoming invoice or credit note (an outgoing invoice needs a credit note, and a
  bank transaction has already happened),
- the case was not booked, in a payment run or paid before the exception (a booking or money
  has already moved), and
- this is the case's only open exception (a closed case cannot carry on with the next one).

The comment stays on the exception, never in the audit log, which records who, when, the reason,
the outcome and a SHA 256 of the comment, so a later change to it shows (NF-03). Nothing here
logs.
"""

import unicodedata
from dataclasses import dataclass

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import get_case, resolve_exception
from argus.intake.duplicates import resolve_duplicate
from argus.queue_page.views import REASON_TEXT

MIN_COMMENT = 3
MAX_COMMENT = 500

# Kinds a person may reject from the page. See the module text for why the others may not.
REJECTABLE_KINDS = frozenset({CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE})
# Statuses after which a booking or money has moved, so the case may not be closed from here.
TOO_FAR = frozenset({CaseStatus.BOOKED, CaseStatus.IN_PAYMENT_RUN, CaseStatus.PAID})


@dataclass(frozen=True)
class Choice:
    """One way an exception can end, as the page offers it."""

    outcome: Outcome
    label: str  # what the person picks
    explain: str  # what happens then
    closes: ClosedReason | None = None  # the case closes for this reason, or carries on


CARRY_ON = Choice(
    Outcome.ACCEPTED_WITH_COMMENT,
    "Checked, carry on",
    "The case goes back to where it was and carries on.",
)
REJECT = Choice(
    Outcome.REJECTED,
    "Reject the invoice",
    "The case closes as rejected. It is never approved, booked or paid.",
    ClosedReason.REJECTED,
)
CONFIRM_DUPLICATE = Choice(
    Outcome.CONFIRMED_DUPLICATE,
    "It is a duplicate",
    "The case closes as a duplicate. It is never approved, booked or paid.",
    ClosedReason.DUPLICATE,
)
NOT_A_DUPLICATE = Choice(
    Outcome.NOT_A_DUPLICATE,
    "Not a duplicate, carry on",
    "The case goes back to where it was and carries on. Say why it is not a duplicate.",
)
ANSWERED = Choice(
    Outcome.ANSWERED,
    "Answered, carry on",
    "The question is answered. The case goes back to where it was and carries on.",
)
FIXED_IN_DATEV = Choice(
    Outcome.FIXED_IN_DATEV,
    "Fixed in DATEV, carry on",
    "The problem is fixed in DATEV. The case goes back to where it was and carries on.",
)

SAME_PERSON_CARRY_ON = Choice(
    Outcome.ACCEPTED_WITH_COMMENT,
    "Benjamin gives the second approval too",
    "The second approval request goes to Benjamin, who gave the first approval as the vendor's"
    ' approver. His second approval is marked "same person" in the audit trail.',
)

# The endings that let the case carry on, per reason. A reason missing here gets CARRY_ON;
# an empty tuple means none. Closing endings are added by choices_for when they are safe.
_CARRY_ON: dict[ExceptionReason, tuple[Choice, ...]] = {
    ExceptionReason.DUPLICATE: (NOT_A_DUPLICATE,),
    ExceptionReason.VALUES_TO_CHECK: (),  # corrected on the queue page (#112)
    ExceptionReason.NO_ACCOUNT_PROPOSAL: (),  # the account is entered on the queue page (#37)
    ExceptionReason.IBAN_MISMATCH: (),  # ARGUS ends it itself after checking DATEV (#36)
    # ARGUS ends it itself once the approver is back or the vendor has a deputy (#191).
    ExceptionReason.APPROVER_ABSENT: (),
    # ARGUS ends it itself once the approver table holds a Max ASP address (#191).
    ExceptionReason.APPROVER_ADDRESS_OUTSIDE: (),
    # Once a person lets it carry on, the second request goes to the same person (#39).
    ExceptionReason.SECOND_APPROVER_SAME_PERSON: (SAME_PERSON_CARRY_ON,),
    ExceptionReason.PAYMENT_FAILED: (),  # depends on the payment run
    ExceptionReason.CUSTOMER_QUERY: (ANSWERED,),
    ExceptionReason.BOOKING_ERROR: (FIXED_IN_DATEV,),
}
# Reasons that never close the case from here.
_NEVER_CLOSE = frozenset({ExceptionReason.PAYMENT_FAILED})

# A sentence for the page when the exception cannot end here, or only partly.
NOTES: dict[ExceptionReason, str] = {
    ExceptionReason.VALUES_TO_CHECK: (
        "To correct the invoice number, amount or currency, open it in the exception queue."
    ),
    ExceptionReason.NO_ACCOUNT_PROPOSAL: (
        "To enter the G/L account and tax key, open it in the exception queue."
    ),
    ExceptionReason.IBAN_MISMATCH: (
        "Possible fraud. Once the IBAN is right in DATEV, ARGUS sees it on the next sweep and"
        " carries on by itself, so this page can only reject the invoice."
    ),
    ExceptionReason.APPROVER_ABSENT: (
        "Mark the approver back, or give the vendor a deputy, in the approver table. ARGUS sees"
        " it on the next sweep and sends the invoice for approval by itself, so this page can"
        " only reject the invoice."
    ),
    ExceptionReason.APPROVER_ADDRESS_OUTSIDE: (
        "Correct the approver's or deputy's email address in the approver table to a Max ASP"
        " address. ARGUS sees it on the next sweep and sends the invoice for approval by itself,"
        " so this page can only reject the invoice."
    ),
    ExceptionReason.SECOND_APPROVER_MISSING: (
        "Set the second approver for invoices above EUR 5,000 (an admin does this in ARGUS), or"
        ' mark him back, then pick "Checked, carry on". ARGUS then sends him the second'
        " approval request."
    ),
    ExceptionReason.SECOND_APPROVER_SAME_PERSON: (
        "Benjamin approved this invoice as the vendor's approver, and he also gives the second"
        " approval above EUR 5,000. Arita decided he may give both. Carry on to send him the"
        " second approval request, or reject the invoice."
    ),
    ExceptionReason.PAYMENT_FAILED: (
        "A failed payment cannot be resolved from this page yet. It depends on the payment run."
    ),
}


class NotResolvable(Exception):
    """No open exception of a case with this ID."""


class AlreadyResolved(Exception):
    """Someone resolved the exception a moment ago."""


class BadResolution(ValueError):
    """The person's answer cannot be taken. The message is a sentence for them."""


@dataclass(frozen=True)
class OpenCaseException:
    exception_id: int
    case_id: int
    kind: CaseKind
    reason: ExceptionReason
    reason_text: str
    detail: str
    resume_status: CaseStatus | None
    others_open: int  # other open exceptions of the same case
    choices: tuple[Choice, ...]
    note: str | None


@dataclass(frozen=True)
class Resolved:
    case_id: int
    outcome: Outcome
    case_status: CaseStatus
    others_open: int


def choices_for(
    reason: ExceptionReason,
    kind: CaseKind,
    resume_status: CaseStatus | None,
    others_open: int,
) -> tuple[Choice, ...]:
    """The endings the page offers for this exception, safest first."""
    carry_on = _CARRY_ON.get(reason, (CARRY_ON,))
    may_close = (
        reason not in _NEVER_CLOSE
        and kind in REJECTABLE_KINDS
        and resume_status not in TOO_FAR
        and others_open == 0
    )
    if not may_close:
        return carry_on
    if reason is ExceptionReason.DUPLICATE:
        return (CONFIRM_DUPLICATE, *carry_on, REJECT)
    return (*carry_on, REJECT)


def _note(
    reason: ExceptionReason, choices: tuple[Choice, ...], could_close_alone: bool
) -> str | None:
    if reason is ExceptionReason.IBAN_MISMATCH and not choices:
        # Rejecting is the only ending here, and it is not possible for this case.
        why = (
            "it has another open exception"
            if could_close_alone
            else "it is not an incoming invoice still waiting before booking"
        )
        return (
            "Possible fraud. Once the IBAN is right in DATEV, ARGUS sees it on the next sweep and"
            f" carries on by itself. This case cannot be rejected here because {why}."
        )
    if reason in NOTES:
        return NOTES[reason]
    if not choices:
        return "This exception cannot be resolved from this page."
    if could_close_alone and not any(c.closes for c in choices):
        return (
            "The case has another open exception. Rejecting it is possible once that one is"
            " resolved."
        )
    return None


_LOAD = """
    select e.id, e.reason, e.detail, e.resolved_at, e.case_id, c.kind, c.resume_status,
           (select count(*) from exception_item o
            where o.case_id = e.case_id and o.resolved_at is null
              and o.id <> e.id) as others_open
    from exception_item e join invoice_case c on c.id = e.case_id
    where e.id = :id
"""


def _load(connection: Connection, exception_id: int, *, lock: bool) -> dict[str, object] | None:
    # Two fixed texts; only the bound :id comes from outside.
    sql = _LOAD + " for update of e" if lock else _LOAD
    row = connection.execute(text(sql), {"id": exception_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def _describe(row: dict[str, object]) -> OpenCaseException:
    reason = ExceptionReason(str(row["reason"]))
    kind = CaseKind(str(row["kind"]))
    resume = CaseStatus(str(row["resume_status"])) if row["resume_status"] else None
    others = int(str(row["others_open"]))
    choices = choices_for(reason, kind, resume, others)
    return OpenCaseException(
        exception_id=int(str(row["id"])),
        case_id=int(str(row["case_id"])),
        kind=kind,
        reason=reason,
        reason_text=REASON_TEXT[reason],
        detail=str(row["detail"]),
        resume_status=resume,
        others_open=others,
        choices=choices,
        note=_note(reason, choices, any(c.closes for c in choices_for(reason, kind, resume, 0))),
    )


def open_case_exception(connection: Connection, exception_id: int) -> OpenCaseException:
    """An open exception of a case, with the endings the page offers for it."""
    row = _load(connection, exception_id, lock=False)
    if row is None or row["resolved_at"] is not None:
        raise NotResolvable(exception_id)
    return _describe(row)


def clean_comment(comment: str) -> str:
    """The comment as stored, or BadResolution with a sentence for the person.

    Control characters other than line breaks and tabs are refused (PostgreSQL refuses a NUL
    anyway). Invisible characters do not count toward the length, so a comment of only zero
    width spaces is refused like an empty one."""
    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\r\t" for ch in comment):
        raise BadResolution("The comment holds a character that cannot be stored. Type it again.")
    cleaned = comment.strip()
    visible = "".join(
        ch for ch in cleaned if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc")
    )
    if len(visible) < MIN_COMMENT or len(cleaned) > MAX_COMMENT:
        raise BadResolution(
            f"Say briefly why ({MIN_COMMENT} to {MAX_COMMENT} characters). Leave out personal"
            " details."
        )
    return cleaned


def resolve_from_page(
    connection: Connection, exception_id: int, outcome: Outcome, comment: str
) -> Resolved:
    """Resolve one open exception of a case as the person chose. Run in a unit of work under
    that person, so the database records who did it.

    The exception row is locked first, so two people resolving the same exception at once
    cannot both succeed: the second one gets AlreadyResolved.
    """
    cleaned = clean_comment(comment)
    row = _load(connection, exception_id, lock=True)
    if row is None:
        raise NotResolvable(exception_id)
    if row["resolved_at"] is not None:
        raise AlreadyResolved(exception_id)
    found = _describe(row)
    choice = next((c for c in found.choices if c.outcome is outcome), None)
    if choice is None:
        raise BadResolution("This exception cannot end that way here. Reload the list.")

    if found.reason is ExceptionReason.DUPLICATE and outcome in (
        Outcome.CONFIRMED_DUPLICATE,
        Outcome.NOT_A_DUPLICATE,
    ):
        # The duplicate rules live in one place: "not a duplicate" also gives the case its own
        # key, so it is not flagged against the same invoice again.
        resolve_duplicate(
            connection,
            exception_id,
            confirmed=outcome is Outcome.CONFIRMED_DUPLICATE,
            resolution=cleaned,
        )
    elif choice.closes is not None:
        resolve_exception(
            connection,
            exception_id,
            outcome,
            resolution=cleaned,
            case_to=CaseStatus.CLOSED,
            closed_reason=choice.closes,
        )
    else:
        resolve_exception(connection, exception_id, outcome, resolution=cleaned)

    case = get_case(connection, found.case_id)
    return Resolved(found.case_id, outcome, case.status, found.others_open)

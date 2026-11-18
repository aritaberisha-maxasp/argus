"""The audit trail of one case, to read on the invoices page and to export as a file (#63, NF-01,
AP-08, AC-8).

Anyone checking the books, a tax auditor too, must see what happened to an invoice: every step
from the email it came in with to its last status, each with its time and who did it. This
module only reads. The steps come from the append only audit log (``case_trace``, ADR 0005), and
the page offers no way to change them; the database refuses any change to the audit log anyway.

What each step shows:

- **When**: the database clock of the event, in Berlin time on the page and in the file.
- **Who**: a job of ARGUS (``system:intake``) or a person by Entra object ID, the way the audit
  log records them. Where ARGUS knows that person's name (the approver table, or the name a
  person gave with a correction), it is shown next to the ID.
- **What**: a plain sentence for the event, the status before and after, and the recorded
  details.
- **Why**: the comment a person gave. The audit log keeps no free text (ADR 0005), so the
  comment of a resolved exception comes from the exception. A correction carries its reason in
  the audit log itself (migration 0013). An approval comment (AP-08) will show the same way
  once approvals are built.
- **Chain number and hash**: so an auditor can match each step with the nightly copy of the
  audit log (#83). The file also says whether the whole hash chain was intact when it was made.

Nothing here logs. Names and comments are personal data and stay out of the log (NF-03).
"""

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from argus.cases.model import CaseStatus, ExceptionReason, Outcome
from argus.invoices_page.views import STATUS_TEXT, CaseRow, case_row
from argus.queue_page.views import REASON_TEXT

BERLIN = ZoneInfo("Europe/Berlin")

# The most steps one answer holds. A case gathers a few dozen at most; this only keeps a broken
# loop from filling the page. The answer says when it stopped.
MAX_STEPS = 2000

# A plain sentence for each kind of event. A kind missing here shows its name in words, so a
# new event never hides from the trail.
EVENT_TEXT: dict[str, str] = {
    "email_seen": "ARGUS found the email in the accounts inbox",
    "email_stored": "ARGUS stored the original email unchanged",
    "email_classified": "ARGUS sorted the email",
    "email_routed": "ARGUS sent the email on its way",
    "email_filed": "ARGUS filed the email",
    "email_moved": "The email was moved to another folder",
    "email_proposed": "ARGUS proposed where the email goes",
    "email_sorted_by_person": "A person sorted the email",
    "email_deleted": "The email was deleted from the mailbox",
    "document_stored": "ARGUS stored a document of the email unchanged",
    "document_linked": "A stored document was linked to the case",
    "invoice_read": "ARGUS read the invoice",
    "case_created": "ARGUS opened the case",
    "case_changed": "The case data changed",
    "case_corrected": "A person corrected the invoice number, amount or currency",
    "status_changed": "The status changed",
    "exception_opened": "An exception stopped the case",
    "exception_resolved": "The exception was resolved",
    "bank_transaction_imported": "ARGUS imported the bank transaction from Wise",
    "reminder_forward_drafted": "ARGUS drafted the forward of a payment reminder",
    "reminder_forwarded": "ARGUS forwarded a payment reminder",
    "bank_import_refused": "ARGUS refused a bank statement",
    "bank_import_from_folder": "ARGUS imported a bank statement from the folder",
}

OUTCOME_TEXT: dict[Outcome, str] = {
    Outcome.FIXED_IN_DATEV: "fixed in DATEV",
    Outcome.DATA_CORRECTED: "data corrected",
    Outcome.RETRIED: "tried again",
    Outcome.CONFIRMED_DUPLICATE: "confirmed as a duplicate",
    Outcome.NOT_A_DUPLICATE: "not a duplicate",
    Outcome.REJECTED: "rejected",
    Outcome.ACCEPTED_WITH_COMMENT: "checked, carry on",
    Outcome.ANSWERED: "answered",
    Outcome.RULE_SAVED: "rule saved",
    Outcome.CATEGORY_CHOSEN: "category chosen",
    Outcome.PUT_BACK_IN_POOL: "put back in the pool",
}

# The columns of the exported file, in order.
FILE_COLUMNS = (
    "Step",
    "Time (Berlin)",
    "What happened",
    "Event",
    "Actor kind",
    "Actor",
    "Actor name",
    "Status before",
    "Status after",
    "Details",
    "Comment",
    "Chain number",
    "Hash",
)


@dataclass(frozen=True)
class TrailStep:
    chain_seq: int
    occurred_at: datetime
    event_type: str
    what: str  # a plain sentence
    actor_kind: str  # "system" or "user"
    actor: str  # the job name or the Entra object ID
    actor_name: str | None  # the person's name, when ARGUS knows it
    from_status: CaseStatus | None
    to_status: CaseStatus | None
    facts: list[str]  # the recorded details as short lines
    details: dict[str, Any]  # the recorded details as stored
    comment: str | None  # what a person wrote, from the exception or correction
    hash_hex: str


@dataclass(frozen=True)
class CaseTrail:
    case: CaseRow
    steps: list[TrailStep]
    truncated: bool


def _words(name: str) -> str:
    return name.replace("_", " ")


def _status_text(value: Any) -> str:
    try:
        return STATUS_TEXT[CaseStatus(value)]
    except ValueError:
        return _words(str(value))


def _reason_text(value: Any) -> str:
    try:
        return REASON_TEXT[ExceptionReason(value)]
    except ValueError:
        return _words(str(value))


def _outcome_text(value: Any) -> str:
    try:
        return OUTCOME_TEXT[Outcome(value)]
    except ValueError:
        return _words(str(value))


def _plain(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


def describe(event_type: str, from_status: Any, to_status: Any, details: dict[str, Any]) -> str:
    """One plain sentence for an event of the trail."""
    if event_type == "status_changed" and from_status and to_status:
        return f"The status changed from {_status_text(from_status)} to {_status_text(to_status)}"
    if event_type == "case_created" and to_status:
        return f"ARGUS opened the case as {_status_text(to_status)}"
    if event_type == "exception_opened" and details.get("reason"):
        return f"An exception stopped the case: {_reason_text(details['reason'])}"
    if event_type == "exception_resolved" and details.get("reason"):
        ending = _outcome_text(details["outcome"]) if details.get("outcome") else "resolved"
        return f"The exception was resolved as {ending}: {_reason_text(details['reason'])}"
    text_ = EVENT_TEXT.get(event_type)
    if text_:
        return text_
    return _words(event_type).capitalize()


def fact_lines(details: dict[str, Any]) -> list[str]:
    """The recorded details as short lines. A change shows its old and new value."""
    lines: list[str] = []
    for key in sorted(details):
        value = details[key]
        if isinstance(value, dict) and set(value) == {"old", "new"}:
            lines.append(f"{_words(key)}: {_plain(value['old'])} → {_plain(value['new'])}")
        else:
            lines.append(f"{_words(key)}: {_plain(value)}")
    return lines


_STEPS = """
    select t.chain_seq, t.occurred_at, t.event_type, t.actor_type, t.actor,
           t.from_status, t.to_status, t.details, encode(t.hash, 'hex') as hash_hex,
           case when t.event_type = 'exception_resolved' then x.resolution
                when t.event_type = 'case_corrected' then t.details ->> 'reason' end as comment
    from case_trace t
    left join exception_item x on x.id = t.exception_id
    where t.trace_case_id = :id
    order by t.chain_seq
    limit :limit
"""

# The names ARGUS knows for the people in the trail: the approver table, and the name a person
# gave with a correction (the newest one wins).
_NAMES = """
    select 1 as rank, object_id as who, name from approver where object_id = any(:ids)
    union all
    select * from (
      select distinct on (corrected_by) 2, substr(corrected_by, 6), corrected_by_name
      from case_correction where corrected_by = any(:users)
      order by corrected_by, id desc
    ) newest
    order by rank
"""


def case_trail(connection: Connection, case_id: int) -> CaseTrail | None:
    """Every step of the case, oldest first, or None when there is no such case."""
    case = case_row(connection, case_id)
    if case is None:
        return None
    rows = list(
        connection.execute(text(_STEPS), {"id": case_id, "limit": MAX_STEPS + 1}).mappings()
    )
    people = sorted({r["actor"] for r in rows if r["actor_type"] == "user"})
    names: dict[str, str] = {}
    if people:
        for known in connection.execute(
            text(_NAMES), {"ids": people, "users": [f"user:{p}" for p in people]}
        ).mappings():
            names.setdefault(str(known["who"]), str(known["name"]))
    steps = []
    for r in rows[:MAX_STEPS]:
        details = r["details"] or {}
        steps.append(
            TrailStep(
                chain_seq=r["chain_seq"],
                occurred_at=r["occurred_at"],
                event_type=r["event_type"],
                what=describe(r["event_type"], r["from_status"], r["to_status"], details),
                actor_kind=r["actor_type"],
                actor=r["actor"],
                actor_name=names.get(r["actor"]) if r["actor_type"] == "user" else None,
                from_status=CaseStatus(r["from_status"]) if r["from_status"] else None,
                to_status=CaseStatus(r["to_status"]) if r["to_status"] else None,
                facts=fact_lines(details),
                details=details,
                comment=r["comment"],
                hash_hex=r["hash_hex"],
            )
        )
    return CaseTrail(case=case, steps=steps, truncated=len(rows) > MAX_STEPS)


def berlin(moment: datetime) -> str:
    """A time as ISO 8601 in Berlin time, with its offset, to the second."""
    return moment.astimezone(BERLIN).isoformat(timespec="seconds")


def _cell(value: Any) -> str:
    """A value for a spreadsheet cell. Text that a spreadsheet would run as a formula gets a
    leading apostrophe, so a comment or a value read from an invoice can never run there."""
    out = "" if value is None else str(value)
    if out and out[0] in "=+-@\t\r":
        return "'" + out
    return out


def _steps(count: int) -> str:
    return "1 step" if count == 1 else f"{count} steps"


def trail_file(
    trail: CaseTrail, *, chain_broken_at: int | None, exported_at: datetime, exported_by: str
) -> str:
    """The trail as a CSV file for a spreadsheet: a few lines saying what it is, then one row
    per step. Semicolons, because German Excel expects them; the page sends it as UTF-8 with a
    byte order mark so umlauts open correctly."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    case = trail.case
    writer.writerow([_cell(f"ARGUS audit trail of case {case.case_id}")])
    writer.writerow(
        [_cell(f"Exported {berlin(exported_at)} by {exported_by}, read from the audit log")]
    )
    writer.writerow(
        [
            _cell(
                "Hash chain of the whole audit log: intact"
                if chain_broken_at is None
                else f"Hash chain of the whole audit log: BROKEN at chain number {chain_broken_at}"
            )
        ]
    )
    writer.writerow(
        [_cell(f"Status now: {case.status_text}, {_steps(len(trail.steps))}")]
        + ([_cell(f"Only the first {MAX_STEPS} steps are in this file")] if trail.truncated else [])
    )
    writer.writerow([])
    writer.writerow(FILE_COLUMNS)
    for number, step in enumerate(trail.steps, start=1):
        writer.writerow(
            [
                number,
                berlin(step.occurred_at),
                _cell(step.what),
                step.event_type,
                step.actor_kind,
                _cell(step.actor),
                _cell(step.actor_name),
                step.from_status.value if step.from_status else "",
                step.to_status.value if step.to_status else "",
                _cell(json.dumps(step.details, ensure_ascii=False, sort_keys=True)),
                _cell(step.comment),
                step.chain_seq,
                step.hash_hex,
            ]
        )
    return out.getvalue()

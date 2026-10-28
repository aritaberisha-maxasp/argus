"""Forward payment reminders and dunning letters to Benjamin (IN-05, PY-06, ADR 0003, #27).

An email the sorting (#25) routes to ``forward_reminder`` is forwarded with its original
attachments to the one address set in ``ARGUS_REMINDER_FORWARD_TO``. If the reminder names an
open supplier invoice, that invoice case gets ``reminder_received_at``, so it always goes into
the next payment run (PY-06).

Never two forwards, even after a crash:

1. Graph saves the forward as a draft. Nothing is sent yet.
2. A short unit of work records the draft's ID. The email can have only one forward record, so
   a second worker's draft is never recorded. A draft whose ID was not recorded (a crash
   between 1 and 2) is never sent; it stays in the mailbox's Drafts folder.
3. The recorded draft is sent. Before sending, ARGUS asks Graph whether it is still a draft, so
   a crash after sending (between 3 and 4) does not send again. Graph itself sends a draft once.
4. A short unit of work marks the forward sent, marks the matched invoice cases and moves the
   email to step ``routed``. The database writes the audit events.

Graph is never called inside a unit of work (PR #79 review). The application logs hold IDs
only, never the sender, subject, content or the forward address (NF-03).
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor, CaseKind, CaseStatus
from argus.cases.store import change_case, unit_of_work
from argus.intake.records import EmailRecord, EmailStep
from argus.sorting.models import EmailToSort, Route, SortingDecision

log = logging.getLogger("argus.intake.reminders")

FORWARD_ACTOR = Actor.system("reminder-forward")
FORWARD_TO_ENV = "ARGUS_REMINDER_FORWARD_TO"
# Fixed text above the forwarded email. It holds nothing from the email itself.
FORWARD_COMMENT = (
    "Von ARGUS weitergeleitet: Zahlungserinnerung oder Mahnung aus dem Postfach der "
    "Buchhaltung. Die Originalanhänge sind beigefügt."
)

# Supplier invoices that are not paid or closed yet. A reminder sets reminder_received_at on
# them so they go into the next payment run (PY-06).
OPEN_STATUSES = tuple(
    status.value
    for status in (
        CaseStatus.RECEIVED,
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.APPROVED,
        CaseStatus.BOOKED,
        CaseStatus.IN_PAYMENT_RUN,
        CaseStatus.EXCEPTION,
    )
)
# Invoice numbers shorter than this, or without a digit, are too likely to turn up by chance.
MIN_NUMBER_LENGTH = 4
_ADDRESS = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


class ReminderMailbox(Protocol):
    """The Graph calls a forward needs. :class:`argus.intake.graph.GraphClient` is the real
    one; tests wrap it to simulate crashes."""

    def create_forward(self, message_id: str, comment: str) -> str: ...

    def is_draft(self, message_id: str) -> bool: ...

    def send_draft(self, message_id: str) -> None: ...


@dataclass(frozen=True)
class ReminderMatch:
    """Which open invoice cases a reminder names. ``unclear_numbers`` counts invoice numbers
    found in the reminder that more than one open case carries; those cases are not marked."""

    case_ids: tuple[int, ...]
    unclear_numbers: int


@dataclass(frozen=True)
class ForwardResult:
    draft_message_id: str
    sent_now: bool  # False when an earlier run had already sent it
    match: ReminderMatch


class ForwardRefused(RuntimeError):
    """The email cannot be forwarded as a reminder."""


def forward_address_from_environment(environ: Mapping[str, str]) -> str:
    """The one address reminders go to, from ``ARGUS_REMINDER_FORWARD_TO``. There is no
    default: the address belongs to a person, so it lives in the settings, not in the repo."""
    address = environ.get(FORWARD_TO_ENV, "").strip()
    if not _ADDRESS.fullmatch(address):
        raise ValueError(f"{FORWARD_TO_ENV} must be set to one email address")
    return address


def reminder_text(reminder: EmailToSort) -> str:
    """Everything a reminder says: subject, body and the text read from each attachment."""
    parts = [reminder.subject, reminder.body]
    parts += [a.text for a in reminder.attachments if a.text]
    return "\n".join(parts)


def _names(number: str, text: str) -> bool:
    """Whether ``text`` holds ``number`` as a whole number, not as part of a longer one."""
    pattern = r"(?<![\w/-])" + re.escape(number) + r"(?![\w/-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def match_open_invoices(connection: Connection, reminder: EmailToSort) -> ReminderMatch:
    """The open supplier invoice cases whose invoice number the reminder names.

    A number that more than one open case carries (two vendors can use the same number) is
    not matched, because the reminder alone cannot tell which invoice it means. The sender is
    not used yet: vendor email addresses are not in the master data.
    """
    rows = connection.execute(
        text(
            """
            select id, document_number from invoice_case
            where kind = :kind and status::text = any(:statuses) and document_number is not null
            order by id
            """
        ),
        {"kind": CaseKind.INCOMING_INVOICE.value, "statuses": list(OPEN_STATUSES)},
    ).all()
    words = reminder_text(reminder)
    by_number: dict[str, list[int]] = {}
    for row in rows:
        case_id: int = row[0]
        number: str = row[1]
        key = number.strip().casefold()
        if len(key) < MIN_NUMBER_LENGTH or not any(c.isdigit() for c in key):
            continue
        if _names(number.strip(), words):
            by_number.setdefault(key, []).append(case_id)
    matched = sorted(ids[0] for ids in by_number.values() if len(ids) == 1)
    unclear = sum(1 for ids in by_number.values() if len(ids) > 1)
    return ReminderMatch(case_ids=tuple(matched), unclear_numbers=unclear)


@dataclass(frozen=True)
class _Forward:
    draft_message_id: str
    sent: bool


def _get_forward(connection: Connection, email_id: int) -> _Forward | None:
    row = connection.execute(
        text("select draft_message_id, step from reminder_forward where email_id = :id"),
        {"id": email_id},
    ).first()
    return None if row is None else _Forward(row[0], row[1] == "sent")


def _received_at(connection: Connection, email_id: int) -> datetime:
    """When the reminder arrived: as Exchange reports it, else when ARGUS first saw it."""
    value: datetime = connection.execute(
        text("select coalesce(received_at, seen_at) from mailbox_email where id = :id"),
        {"id": email_id},
    ).scalar_one()
    return value


def _draft(engine: Engine, graph: ReminderMailbox, email: EmailRecord) -> _Forward:
    """Steps 1 and 2: the recorded draft of this email, saved now if it has none yet."""
    with engine.connect() as connection:
        existing = _get_forward(connection, email.id)
    if existing is not None:
        return existing
    draft_id = graph.create_forward(email.graph_message_id, FORWARD_COMMENT)
    with unit_of_work(engine, FORWARD_ACTOR) as db:
        created = db.execute(
            text(
                """
                insert into reminder_forward (email_id, draft_message_id)
                values (:email_id, :draft)
                on conflict (email_id) do nothing
                returning id
                """
            ),
            {"email_id": email.id, "draft": draft_id},
        ).first()
        recorded = _get_forward(db, email.id)
    if created is None:
        # Another worker recorded its draft first. Ours is never sent.
        log.warning("Reminder forward for email %s already recorded, draft left unsent", email.id)
    if recorded is None:
        raise RuntimeError(f"reminder forward for email {email.id} is missing right after insert")
    return recorded


def _finish(
    engine: Engine, email: EmailRecord, reminder: EmailToSort, draft_id: str
) -> ReminderMatch | None:
    """Step 4: mark the forward sent, mark the matched cases and move the email to routed, in
    one unit of work. Returns None when another worker finished it first."""
    with unit_of_work(engine, FORWARD_ACTOR) as db:
        match = match_open_invoices(db, reminder)
        done = db.execute(
            text(
                """
                update reminder_forward
                set step = 'sent', matched_case_ids = :cases, unclear_numbers = :unclear
                where email_id = :email_id and draft_message_id = :draft and step = 'drafted'
                returning id
                """
            ),
            {
                "cases": list(match.case_ids),
                "unclear": match.unclear_numbers,
                "email_id": email.id,
                "draft": draft_id,
            },
        ).first()
        if done is None:
            return None
        received = _received_at(db, email.id)
        for case_id in match.case_ids:
            first: bool = db.execute(
                text("select reminder_received_at is null from invoice_case where id = :id"),
                {"id": case_id},
            ).scalar_one()
            if first:  # the first reminder counts; a later one changes nothing
                change_case(db, case_id, reminder_received_at=received)
        db.execute(
            text("update mailbox_email set step = :routed where id = :id and step = :classified"),
            {
                "routed": EmailStep.ROUTED.value,
                "classified": EmailStep.CLASSIFIED.value,
                "id": email.id,
            },
        )
    return match


def forward_reminder(
    engine: Engine,
    graph: ReminderMailbox,
    email: EmailRecord,
    reminder: EmailToSort,
    decision: SortingDecision,
) -> ForwardResult:
    """Forward one sorted reminder to the reminder address, with its original attachments.

    Safe to call again after a crash at any point: it carries on from the last recorded step
    and never sends a second forward. ``reminder`` is the email as the sorting read it, used
    to find the invoice numbers it names.
    """
    if decision.route is not Route.FORWARD_REMINDER:
        raise ForwardRefused(f"email {email.id} is routed to {decision.route}, not forwarded")
    if email.step is not EmailStep.CLASSIFIED:
        raise ForwardRefused(f"email {email.id} is at step {email.step}, not classified")
    forward = _draft(engine, graph, email)
    sent_now = False
    if not forward.sent:
        if graph.is_draft(forward.draft_message_id):
            graph.send_draft(forward.draft_message_id)
            sent_now = True
        else:
            log.info("Reminder forward for email %s was already sent, recording it", email.id)
        match = _finish(engine, email, reminder, forward.draft_message_id)
        if match is not None:
            log.info(
                "Reminder email %s forwarded, %d open invoice(s) marked, %d unclear number(s)",
                email.id,
                len(match.case_ids),
                match.unclear_numbers,
            )
            return ForwardResult(forward.draft_message_id, sent_now, match)
    with engine.connect() as connection:
        recorded = connection.execute(
            text(
                "select matched_case_ids, unclear_numbers from reminder_forward"
                " where email_id = :id"
            ),
            {"id": email.id},
        ).one()
    return ForwardResult(
        forward.draft_message_id,
        sent_now,
        ReminderMatch(tuple(recorded[0] or ()), recorded[1] or 0),
    )

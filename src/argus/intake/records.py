"""The email records of the mailbox sweep (ADR 0003, migrations 0004 to 0007).

Each function runs inside a short :func:`argus.cases.store.unit_of_work`. None of them calls
Graph or the document store: those calls happen first, outside the transaction, because every
transaction that writes an audit event holds the audit lock until it commits (PR #79 review).
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, RowMapping, text

from argus.intake.graph import InboxMessage
from argus.sorting.models import Route, SortingDecision

# Other emails stay this long in ARGUS/Removed before ARGUS deletes them (IN-06, ADR 0003).
# The database refuses an earlier deletion too (migration 0006).
REMOVAL_DELAY = timedelta(days=30)


class DeletionOutcome(StrEnum):
    """How an email left ARGUS/Removed, so the audit log never claims ARGUS deleted an email it
    found already gone (migration 0006)."""

    DELETED = "deleted"  # ARGUS deleted it
    ALREADY_GONE = "already_gone"  # gone before ARGUS asked: a person, or a crash after delete


class EmailStep(StrEnum):
    """The steps of ADR 0003, in order. The database refuses to skip one."""

    SEEN = "seen"
    STORED = "stored"
    CLASSIFIED = "classified"
    ROUTED = "routed"
    FILED = "filed"


@dataclass(frozen=True)
class EmailRecord:
    id: int
    graph_message_id: str
    step: EmailStep
    raw_sha256: str | None
    route: Route | None = None
    folder: str | None = None


def see_email(connection: Connection, message: InboxMessage) -> tuple[EmailRecord, bool]:
    """The record for this email, created at step seen if it has none yet, and whether it was
    created now. Two sweeps that see the same email at the same time get the same record
    (unique immutable ID); only one of them creates it."""
    created = connection.execute(
        text(
            """
            insert into mailbox_email (graph_message_id, internet_message_id, received_at)
            values (:id, :internet_message_id, :received_at)
            on conflict (graph_message_id) do nothing
            returning id
            """
        ),
        {
            "id": message.id,
            "internet_message_id": message.internet_message_id,
            "received_at": message.received_at,
        },
    ).first()
    record = get_email(connection, message.id)
    if record is None:
        raise RuntimeError(f"email record for {message.id} is missing right after insert")
    return record, created is not None


def get_email(connection: Connection, graph_message_id: str) -> EmailRecord | None:
    row = (
        connection.execute(
            text(
                "select id, graph_message_id, step, raw_sha256, route, folder"
                " from mailbox_email where graph_message_id = :id"
            ),
            {"id": graph_message_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else _record(row)


def _record(row: RowMapping) -> EmailRecord:
    return EmailRecord(
        id=row["id"],
        graph_message_id=row["graph_message_id"],
        step=EmailStep(row["step"]),
        raw_sha256=row["raw_sha256"],
        route=Route(row["route"]) if row["route"] else None,
        folder=row["folder"],
    )


def mark_stored(connection: Connection, email_id: int, raw_sha256: str, raw_location: str) -> bool:
    """Record that the raw email is in the document store. The database logs
    ``email_stored``. Returns False when another worker recorded it first."""
    result = connection.execute(
        text(
            """
            update mailbox_email
            set step = 'stored', raw_sha256 = :sha256, raw_location = :location
            where id = :id and step = 'seen'
            returning id
            """
        ),
        {"id": email_id, "sha256": raw_sha256, "location": raw_location},
    )
    return result.first() is not None


def mark_classified(connection: Connection, email_id: int, decision: SortingDecision) -> bool:
    """Record the sorting decision of a stored email. The database logs ``email_classified``
    with :meth:`SortingDecision.audit_details` (migration 0005). Returns False when the email
    is not at step stored, so sorting the same email twice records nothing the second time."""
    result = connection.execute(
        text(
            """
            update mailbox_email set step = 'classified', classification = cast(:details as jsonb)
            where id = :id and step = 'stored'
            returning id
            """
        ),
        {"id": email_id, "details": json.dumps(decision.audit_details())},
    )
    return result.first() is not None


def classification_of(connection: Connection, email_id: int) -> dict[str, Any] | None:
    """The recorded sorting decision, as written into ``email_classified``."""
    found = connection.execute(
        text("select classification from mailbox_email where id = :id"), {"id": email_id}
    ).scalar()
    return dict(found) if found is not None else None


def mark_routed(connection: Connection, email_id: int, route: Route) -> bool:
    """Record where the sorting sent a classified email. The database logs ``email_routed``.
    Returns False when the email is not at step classified (already routed by another worker)."""
    result = connection.execute(
        text(
            """
            update mailbox_email set step = 'routed', route = :route
            where id = :id and step = 'classified'
            returning id
            """
        ),
        {"id": email_id, "route": route.value},
    )
    return result.first() is not None


def mark_filed(connection: Connection, email_id: int, folder: str) -> bool:
    """Record that the email was moved out of the Inbox into ``folder``. The database sets the
    time and logs ``email_filed`` and ``email_moved``. Returns False when it was already filed."""
    result = connection.execute(
        text(
            """
            update mailbox_email set step = 'filed', folder = :folder
            where id = :id and step = 'routed'
            returning id
            """
        ),
        {"id": email_id, "folder": folder},
    )
    return result.first() is not None


def mark_deleted(connection: Connection, email_id: int, outcome: DeletionOutcome) -> bool:
    """Record that the email is gone from the mailbox, and whether ARGUS deleted it. The
    database sets the time, checks the 30 days in ARGUS/Removed by its own clock and logs
    ``email_deleted`` with the outcome. Returns False when it was already recorded."""
    result = connection.execute(
        text(
            """
            update mailbox_email set deleted_at = clock_timestamp(), deletion_outcome = :outcome
            where id = :id and deleted_at is null
            returning id
            """
        ),
        {"id": email_id, "outcome": outcome.value},
    )
    return result.first() is not None


def routed_emails(connection: Connection) -> list[EmailRecord]:
    """Every email that is routed but not filed yet, oldest first. Filing works from these
    records, not from the Inbox, so an email moved just before a crash still gets recorded."""
    rows = connection.execute(
        text(
            "select id, graph_message_id, step, raw_sha256, route, folder"
            " from mailbox_email where step = 'routed' order by id"
        )
    ).mappings()
    return [_record(row) for row in rows]


def due_for_deletion(connection: Connection, folder: str, now: datetime) -> list[EmailRecord]:
    """The emails in ``folder`` that ARGUS moved there at least 30 days before ``now`` and has
    not deleted yet, oldest first."""
    rows = connection.execute(
        text(
            """
            select id, graph_message_id, step, raw_sha256, route, folder from mailbox_email
            where folder = :folder and deleted_at is null and filed_at <= :cutoff
            order by filed_at, id
            """
        ),
        {"folder": folder, "cutoff": now - REMOVAL_DELAY},
    ).mappings()
    return [_record(row) for row in rows]


def still_due(connection: Connection, email_id: int) -> bool:
    """Whether the email is in ARGUS/Removed, not deleted yet, and has been there for 30 days
    by the database clock. Asked right before each delete, so the database rule holds before
    the step that cannot be undone, not only when it is recorded (review of PR 92)."""
    found = connection.execute(
        text(
            """
            select 1 from mailbox_email
            where id = :id and folder = 'ARGUS/Removed' and deleted_at is null
              and filed_at <= clock_timestamp() - make_interval(days => :days)
            """
        ),
        {"id": email_id, "days": REMOVAL_DELAY.days},
    ).first()
    return found is not None

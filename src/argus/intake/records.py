"""The email records of the mailbox sweep (ADR 0003, migration 0004).

Each function runs inside a short :func:`argus.cases.store.unit_of_work`. None of them calls
Graph or the document store: those calls happen first, outside the transaction, because every
transaction that writes an audit event holds the audit lock until it commits (PR #79 review).
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, text

from argus.intake.graph import InboxMessage


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
                "select id, graph_message_id, step, raw_sha256 from mailbox_email"
                " where graph_message_id = :id"
            ),
            {"id": graph_message_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return EmailRecord(
        id=row["id"],
        graph_message_id=row["graph_message_id"],
        step=EmailStep(row["step"]),
        raw_sha256=row["raw_sha256"],
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

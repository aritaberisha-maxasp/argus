"""The mailbox sweep: picks up every email in the accounts Inbox (ADR 0003, IN-01, NF-01).

It runs every 5 minutes and is the only thing that decides what to process. For each email in
the Inbox it makes sure there is one record (step ``seen``), then stores the raw email and its
attachments in the document store (step ``stored``) before anything else happens to it. Later
stories add classifying and routing. After the pass over the Inbox it files every routed email
into its ARGUS folder (#26, :mod:`argus.intake.filing`).

Crash safety: every step is recorded before the next starts. After a crash the next sweep finds
the email still in the Inbox, reads its record and carries on from the last finished step.
Storing is content addressed, so storing the same email again returns the copy already there.

One sweep at a time: the sweep holds a PostgreSQL advisory lock while it runs. A sweep that
starts while another runs does nothing. The record's unique immutable ID and the step check in
:func:`argus.intake.records.mark_stored` keep each email processed once even without the lock.

Graph and the document store are never called inside a unit of work; their results are
recorded afterwards in a short one (PR #79 review).
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.documents.email import StoredEmail, store_email
from argus.documents.records import record_email_documents
from argus.documents.store import DocumentStore
from argus.intake.filing import Folders, file_routed
from argus.intake.graph import GraphClient, InboxMessage
from argus.intake.records import EmailRecord, EmailStep, mark_stored, see_email

log = logging.getLogger("argus.intake.sweep")

SWEEP_INTERVAL = timedelta(minutes=5)
SWEEP_ACTOR = Actor.system("mailbox-sweep")
SWEEP_LOCK = "argus_mailbox_sweep"


class EmailArchive(Protocol):
    """Where the sweep keeps each email: ``store`` puts the files in blob storage, outside any
    transaction, and ``record`` records them inside the short unit of work that also marks the
    email stored. :class:`DocumentArchive` is the real one; tests wrap it to simulate crashes."""

    def store(self, raw: bytes) -> StoredEmail: ...

    def record(self, connection: Connection, email_id: int, stored: StoredEmail) -> None: ...


class DocumentArchive:
    """The document store of #20: the raw email and every attachment, byte for byte, named by
    their SHA-256 and never overwritten."""

    def __init__(self, documents: DocumentStore) -> None:
        self._documents = documents

    def store(self, raw: bytes) -> StoredEmail:
        return store_email(self._documents, raw)

    def record(self, connection: Connection, email_id: int, stored: StoredEmail) -> None:
        record_email_documents(connection, email_id, stored)


@dataclass
class SweepResult:
    ran: bool
    listed: int = 0
    new: int = 0
    stored: int = 0
    filed: int = 0
    failed: int = 0


@contextmanager
def sweep_lock(engine: Engine) -> Iterator[bool]:
    """Try to take the sweep lock. Yields False when another sweep holds it.

    A session lock on its own connection, outside any transaction, so it is held while Graph
    and the document store are called without keeping a transaction open."""
    with engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT")
        got = bool(
            connection.execute(
                text("select pg_try_advisory_lock(hashtext(:key))"), {"key": SWEEP_LOCK}
            ).scalar()
        )
        try:
            yield got
        finally:
            if got:
                connection.execute(
                    text("select pg_advisory_unlock(hashtext(:key))"), {"key": SWEEP_LOCK}
                )


def see(engine: Engine, message: InboxMessage) -> tuple[EmailRecord, bool]:
    """Step seen: make sure the email has its record. Returns it and whether it is new."""
    with unit_of_work(engine, SWEEP_ACTOR) as db:
        return see_email(db, message)


def store(engine: Engine, graph: GraphClient, archive: EmailArchive, record: EmailRecord) -> bool:
    """Step stored: read the raw email from Graph, put it in the document store, then record
    both in one short unit of work. Returns False when there was nothing to do."""
    if record.step is not EmailStep.SEEN:
        return False
    raw = graph.get_raw(record.graph_message_id)
    stored = archive.store(raw)
    with unit_of_work(engine, SWEEP_ACTOR) as db:
        if not mark_stored(db, record.id, stored.raw.sha256, stored.raw.location):
            return False  # another worker recorded it first; nothing to undo
        archive.record(db, record.id, stored)
    return True


def process_email(
    engine: Engine, graph: GraphClient, archive: EmailArchive, message: InboxMessage
) -> tuple[bool, bool]:
    """Take one email as far as this story goes. Returns (new record, stored now)."""
    record, new = see(engine, message)
    return new, store(engine, graph, archive, record)


def sweep(engine: Engine, graph: GraphClient, archive: EmailArchive) -> SweepResult:
    """One pass over the Inbox. An email that fails is logged and retried by the next sweep;
    the others carry on."""
    with sweep_lock(engine) as got:
        if not got:
            log.info("Mailbox sweep skipped, another sweep is running")
            return SweepResult(ran=False)
        result = SweepResult(ran=True)
        for message in graph.list_inbox():
            result.listed += 1
            try:
                record, new = see(engine, message)
                result.new += new
                result.stored += store(engine, graph, archive, record)
            except Exception:
                # The ID only: no sender, subject or content in the log (NF-03).
                log.exception("Mailbox sweep failed on email %s, next sweep retries", message.id)
                result.failed += 1
        filing = file_routed(engine, graph, Folders(graph))
        result.filed, result.failed = filing.filed, result.failed + filing.failed
        log.info(
            "Mailbox sweep: %d in Inbox, %d new, %d stored, %d filed, %d failed",
            result.listed,
            result.new,
            result.stored,
            result.filed,
            result.failed,
        )
        return result


def run_every(
    engine: Engine,
    graph: GraphClient,
    archive: EmailArchive,
    stop: threading.Event,
    interval: timedelta = SWEEP_INTERVAL,
) -> None:
    """Sweep now and then every ``interval`` until ``stop`` is set. A sweep that fails as a
    whole (Graph or the database is down) is logged and the next one tries again."""
    while not stop.is_set():
        try:
            sweep(engine, graph, archive)
        except Exception:
            log.exception("Mailbox sweep failed, next sweep in %s", interval)
        stop.wait(interval.total_seconds())

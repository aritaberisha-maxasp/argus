"""The mailbox sweep: picks up every email in the accounts Inbox (ADR 0003, IN-01, NF-01).

It runs every 5 minutes and is the only thing that decides what to process. For each email in
the Inbox it makes sure there is one record (step ``seen``), then stores the raw email and its
attachments in the document store (step ``stored``) before anything else happens to it. When
it is given :class:`Sorting`, it then sorts the stored copy (step ``classified``, #91; an email
in doubt also gets an exception for a person, IN-09) and
routes it (step ``routed``): a payment reminder by forwarding it (#27), every other email by
recording its route. After the pass over the Inbox it files every routed email into its ARGUS
folder (#26, :mod:`argus.intake.filing`).

Sorting calls the language model and the document reader outside any transaction. Only the
decision is recorded afterwards, and the database writes ``email_classified`` with it
(migration 0005). If the model or the reader cannot be reached, the email stays at ``stored``
and the next sweep tries again. An email that is classified but not routed yet (its forward
failed, or the sweep stopped in between) is routed by the next sweep from the recorded
decision, without asking the model again.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import open_exception, unit_of_work
from argus.documents.email import StoredEmail, store_email
from argus.documents.records import record_email_documents
from argus.documents.store import DocumentStore
from argus.intake.filing import Folders, file_routed
from argus.intake.graph import GraphClient, InboxMessage
from argus.intake.invoices import (
    Invoicing,
    doubts_about,
    find_invoices,
    open_cases,
    to_manual_review,
)
from argus.intake.records import (
    EmailRecord,
    EmailStep,
    classification_of,
    get_email,
    mark_classified,
    mark_routed,
    mark_stored,
    see_email,
)
from argus.intake.reminders import ReminderMailbox, forward_reminder
from argus.sorting import EmailToSort, LanguageModel, Route, SortingConfig, SortingDecision
from argus.sorting.gates import gate_labels
from argus.sorting.router import sort_email

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


@dataclass(frozen=True)
class Sorting:
    """What the sweep needs to sort and route each stored email (#91).

    ``read`` turns the stored raw email into what the sorting reads:
    :class:`argus.intake.email_text.EmailReader` in production. ``reminders`` is the Graph
    client allowed to forward to the reminder address (#27). Without it, payment reminders
    stay classified in the Inbox until it is set."""

    documents: DocumentStore
    model: LanguageModel
    config: SortingConfig
    read: Callable[[bytes], EmailToSort]
    reminders: ReminderMailbox | None = None
    # Reads the invoices of an email sent to invoice processing and opens one case per invoice
    # (#30). Without it, such emails are routed and filed without cases.
    invoicing: Invoicing | None = None


@dataclass
class SweepResult:
    ran: bool
    listed: int = 0
    new: int = 0
    stored: int = 0
    classified: int = 0
    routed: int = 0
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


def classify(engine: Engine, sorting: Sorting, record: EmailRecord) -> SortingDecision | None:
    """Step classified: sort the stored copy, then record the decision in one short unit of
    work. Returns None when the email is not at step stored or another worker recorded it
    first. Raises when the model or the reader cannot be reached; nothing is recorded then."""
    if record.step is not EmailStep.STORED or record.raw_sha256 is None:
        return None
    raw = sorting.documents.get(record.raw_sha256)
    decision = sort_email(
        sorting.read(raw), sorting.model, sorting.config
    )  # outside any transaction
    invoices = None
    if decision.route is Route.INVOICE_PROCESSING and sorting.invoicing is not None:
        # Also outside any transaction: the reader and the model may be called (#30).
        invoices = find_invoices(raw, sorting.invoicing, sorting.config.own_company)
        decision = replace(decision, invoices_found=len(invoices.invoices))
        doubts = doubts_about(invoices, decision.model_invoices)
        if doubts:
            decision, invoices = to_manual_review(decision, doubts, invoices.gates), None
    with unit_of_work(engine, SWEEP_ACTOR) as db:
        if not mark_classified(db, record.id, decision):
            return None
        if invoices is not None:
            # In the same unit of work, so sorting twice never opens a case twice (IN-08).
            open_cases(db, record.id, invoices)
        if decision.route is Route.MANUAL_REVIEW:
            # In the same unit of work, so sorting twice never opens a second one (IN-09, EX-01).
            open_exception(
                db,
                ExceptionReason.UNCERTAIN_CLASSIFICATION,
                manual_review_detail(decision),
                email_id=record.id,
            )
    # The record ID and the route only: no Message-ID, sender or subject in the log (NF-03).
    log.info("Email record %s classified: %s", record.id, decision.route)
    return decision


def manual_review_detail(decision: SortingDecision) -> str:
    """What the person in the exception queue reads: why ARGUS could not sort the email. It
    stays on the exception; the audit log gets the decision from ``email_classified``."""
    reasons = "; ".join(decision.reasons) or "no reason given"
    detail = f"ARGUS could not sort this email on its own. Check it and route it by hand: {reasons}"
    if decision.gates:
        # The gates first, so the person sees at a glance which rule stopped the email (#71).
        detail = f"Safety gate: {gate_labels(decision.gates)}. {detail}"
    return detail


def route(engine: Engine, sorting: Sorting, record: EmailRecord) -> bool:
    """Step routed: a payment reminder is forwarded (#27), which routes it once the forward is
    sent; every other email gets its route recorded. Works from the recorded decision.
    Returns False when nothing was routed now."""
    if record.step is not EmailStep.CLASSIFIED:
        return False
    with engine.connect() as connection:
        details = classification_of(connection, record.id)
    if details is None:
        raise RuntimeError(f"email record {record.id} is classified without a decision")
    decision = SortingDecision.from_audit_details(details)
    if decision.route is Route.FORWARD_REMINDER:
        if sorting.reminders is None:
            log.warning("Email record %s is a payment reminder, forwarding is not set", record.id)
            return False
        if record.raw_sha256 is None:
            raise RuntimeError(f"email record {record.id} is classified without a stored copy")
        reminder = sorting.read(sorting.documents.get(record.raw_sha256))
        forward_reminder(engine, sorting.reminders, record, reminder, decision)
        routed = _step_of(engine, record) is EmailStep.ROUTED
    else:
        with unit_of_work(engine, SWEEP_ACTOR) as db:
            routed = mark_routed(db, record.id, decision.route)
    if routed:
        log.info("Email record %s routed: %s", record.id, decision.route)
    return routed


def _step_of(engine: Engine, record: EmailRecord) -> EmailStep | None:
    with engine.connect() as connection:
        found = get_email(connection, record.graph_message_id)
    return found.step if found else None


def _current(engine: Engine, record: EmailRecord) -> EmailRecord:
    with engine.connect() as connection:
        found = get_email(connection, record.graph_message_id)
    if found is None:
        raise RuntimeError(f"email record {record.id} is missing")
    return found


def process_email(
    engine: Engine, graph: GraphClient, archive: EmailArchive, message: InboxMessage
) -> tuple[bool, bool]:
    """Take one email as far as this story goes. Returns (new record, stored now)."""
    record, new = see(engine, message)
    return new, store(engine, graph, archive, record)


def sweep(
    engine: Engine, graph: GraphClient, archive: EmailArchive, sorting: Sorting | None = None
) -> SweepResult:
    """One pass over the Inbox. An email that fails is logged and retried by the next sweep;
    the others carry on. Without ``sorting`` the sweep stops at the step stored."""
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
                if sorting is not None:
                    record = _current(engine, record)
                    if classify(engine, sorting, record) is not None:
                        result.classified += 1
                        record = _current(engine, record)
                    result.routed += route(engine, sorting, record)
            except Exception:
                # The ID only: no sender, subject or content in the log (NF-03).
                log.exception("Mailbox sweep failed on email %s, next sweep retries", message.id)
                result.failed += 1
        filing = file_routed(engine, graph, Folders(graph))
        result.filed, result.failed = filing.filed, result.failed + filing.failed
        log.info(
            "Mailbox sweep: %d in Inbox, %d new, %d stored, %d classified, %d routed, %d filed,"
            " %d failed",
            result.listed,
            result.new,
            result.stored,
            result.classified,
            result.routed,
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
    sorting: Sorting | None = None,
) -> None:
    """Sweep now and then every ``interval`` until ``stop`` is set. A sweep that fails as a
    whole (Graph or the database is down) is logged and the next one tries again."""
    while not stop.is_set():
        try:
            sweep(engine, graph, archive, sorting)
        except Exception:
            log.exception("Mailbox sweep failed, next sweep in %s", interval)
        stop.wait(interval.total_seconds())

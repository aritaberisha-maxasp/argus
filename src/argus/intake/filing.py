"""Filing processed emails out of the Inbox, and deleting Other 30 days later (#26, IN-06).

ADR 0003: the Inbox is ARGUS's work queue. Once an email is routed, ARGUS moves it into the
ARGUS folder for its route, so the Inbox only holds what is still to do. Emails sorted as Other
go to ARGUS/Removed and are never deleted straight away: a daily job deletes them 30 days after
ARGUS moved them there, so a misclassified invoice can still be found (note on IN-06). Exchange
keeps a deleted email recoverable for at least 14 days more.

The database checks the same rules (migration 0006): only an email routed to the deletion folder
can be filed in ARGUS/Removed, and only an email that has been there 30 days by the database
clock can be recorded as deleted. It writes the ``email_moved`` and ``email_deleted`` audit
events itself. Right before each delete the job asks the database, by its own clock, whether
the email is still due, so the rule holds before the step that cannot be undone. The Graph
client then checks that the email really is in ARGUS/Removed, so a mistake can never delete an
invoice.

Crash safety: Graph is called first, outside any transaction, and the result is recorded after
in a short unit of work (PR #79 review). Both steps work from the database records, not from
the Inbox, so an email moved or deleted just before a crash is recorded on the next run. Moving
an email into the folder it is already in, or deleting one that is already gone, changes
nothing.

Shadow mode (#72): ARGUS moves and deletes nothing. Both steps ask the database for the mode
right before each Graph call, so switching back to shadow mode stops them within one email.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.intake.graph import GraphClient
from argus.intake.mode import is_live
from argus.intake.records import (
    DeletionOutcome,
    EmailRecord,
    EmailStep,
    due_for_deletion,
    mark_deleted,
    mark_filed,
    routed_emails,
    still_due,
)
from argus.sorting.models import Route

log = logging.getLogger("argus.intake.filing")

FILING_ACTOR = Actor.system("mailbox-filing")
REMOVAL_ACTOR = Actor.system("mailbox-removal")
REMOVAL_INTERVAL = timedelta(days=1)

REMOVED_FOLDER = "ARGUS/Removed"
# One folder per route. Changing a name here only affects emails filed afterwards.
FOLDER_FOR: Mapping[Route, str] = {
    Route.INVOICE_PROCESSING: "ARGUS/Incoming invoices",
    Route.OUTGOING_BOOKING: "ARGUS/Outgoing invoices",
    Route.FORWARD_REMINDER: "ARGUS/Payment reminders",
    Route.DELETION_FOLDER: REMOVED_FOLDER,
    Route.MANUAL_REVIEW: "ARGUS/Manual review",
}


class Folders:
    """The Graph IDs of the ARGUS folders, found or created on first use and then remembered
    for the rest of the run."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph
        self._ids: dict[str, str] = {}

    def _resolve(self, path: str, create: bool) -> str | None:
        parent: str | None = None
        walked: list[str] = []
        for name in path.split("/"):
            walked.append(name)
            key = "/".join(walked)
            if key not in self._ids:
                found = self._graph.find_folder(parent, name)
                if found is None:
                    if not create:
                        return None
                    found = self._graph.create_folder(parent, name)
                self._ids[key] = found
            parent = self._ids[key]
        return parent

    def id_of(self, path: str) -> str:
        """The folder's ID, creating it and its parent when they are missing."""
        found = self._resolve(path, create=True)
        if found is None:
            raise RuntimeError(f"folder {path} could not be created")
        return found

    def existing(self, path: str) -> str | None:
        """The folder's ID, or None when it does not exist. Never creates anything."""
        return self._resolve(path, create=False)


@dataclass
class FilingResult:
    filed: int = 0
    failed: int = 0


@dataclass
class RemovalResult:
    due: int = 0
    deleted: int = 0
    skipped: int = 0  # picked by the clock passed in, but not due by the database clock
    failed: int = 0
    shadow: bool = False  # nothing was deleted because ARGUS runs in shadow mode (#72)


def file_email(engine: Engine, graph: GraphClient, folders: Folders, record: EmailRecord) -> bool:
    """Move one routed email into the ARGUS folder for its route, then record it. Returns False
    when there was nothing to do."""
    if record.step is not EmailStep.ROUTED or record.route is None:
        return False
    if not is_live(engine):
        return False  # shadow mode: nothing moves (#72)
    folder = FOLDER_FOR[record.route]
    graph.move(record.graph_message_id, folders.id_of(folder))
    with unit_of_work(engine, FILING_ACTOR) as db:
        return mark_filed(db, record.id, folder)


def file_routed(engine: Engine, graph: GraphClient, folders: Folders) -> FilingResult:
    """File every routed email. One that fails is logged and tried again on the next run."""
    with engine.connect() as connection:
        pending = routed_emails(connection)
    result = FilingResult()
    for record in pending:
        try:
            result.filed += file_email(engine, graph, folders, record)
        except Exception:
            # The record ID only: no sender, subject or content in the log (NF-03).
            log.exception("Filing failed on email record %s, next run retries", record.id)
            result.failed += 1
    return result


def database_clock(engine: Engine) -> Callable[[], datetime]:
    """The database clock, the one migration 0006 checks the 30 days against."""

    def now() -> datetime:
        with engine.connect() as connection:
            value: datetime = connection.execute(text("select clock_timestamp()")).scalar_one()
            return value

    return now


def remove_due(
    engine: Engine,
    graph: GraphClient,
    folders: Folders,
    clock: Callable[[], datetime] | None = None,
) -> RemovalResult:
    """Delete every email that has been in ARGUS/Removed for 30 days, as of ``clock()``, then
    record each deletion. One that fails is logged and tried again the next day."""
    if not is_live(engine):
        log.info("Removal skipped, ARGUS runs in shadow mode")
        return RemovalResult(shadow=True)
    now = (clock or database_clock(engine))()
    with engine.connect() as connection:
        due = due_for_deletion(connection, REMOVED_FOLDER, now)
    result = RemovalResult(due=len(due))
    if not due:
        return result
    removed_folder = folders.existing(REMOVED_FOLDER)
    for record in due:
        try:
            if removed_folder is None:
                raise RuntimeError("ARGUS/Removed is missing from the mailbox")
            # The database decides, by its own clock, right before the delete.
            with unit_of_work(engine, REMOVAL_ACTOR) as db:
                due_now = still_due(db, record.id)
            if not due_now:
                log.warning("Email record %s is not due by the database clock, skipped", record.id)
                result.skipped += 1
                continue
            if not is_live(engine):
                log.info("Removal stopped, ARGUS was switched to shadow mode")
                result.shadow = True
                break
            # False means it was already deleted (in Deleted Items or gone): by a person, or
            # by ARGUS just before a crash. It is recorded as such, so the job stops trying and
            # the log stays true.
            if graph.delete_from(record.graph_message_id, removed_folder):
                outcome = DeletionOutcome.DELETED
            else:
                outcome = DeletionOutcome.ALREADY_GONE
            with unit_of_work(engine, REMOVAL_ACTOR) as db:
                result.deleted += mark_deleted(db, record.id, outcome)
        except Exception:
            log.exception("Removal failed on email record %s, next run retries", record.id)
            result.failed += 1
    log.info(
        "Removal: %d due, %d deleted, %d skipped, %d failed",
        result.due,
        result.deleted,
        result.skipped,
        result.failed,
    )
    return result


def run_daily(
    engine: Engine,
    graph: GraphClient,
    stop: threading.Event,
    interval: timedelta = REMOVAL_INTERVAL,
) -> None:
    """Run the removal now and then every day until ``stop`` is set. A run that fails as a
    whole (Graph or the database is down) is logged and the next one tries again."""
    while not stop.is_set():
        try:
            remove_due(engine, graph, Folders(graph))
        except Exception:
            log.exception("Removal failed, next run in %s", interval)
        stop.wait(interval.total_seconds())

"""The two week shadow run: what ARGUS proposed, what the person did, and the report (#72).

Covers IN-04, IN-06 and IN-09. In shadow mode (:mod:`argus.intake.mode`) the sweep stores
every email and records the route ARGUS would have taken as its proposal, without classifying,
routing or moving it. A person keeps sorting the Inbox.

When an email with a proposal is no longer in the Inbox, the sweep asks Graph which folder it is
in now and records what that means, using Max ASP's folder list (:class:`PersonFolders`): a
folder for incoming invoices, one for outgoing invoices, one for payment reminders and so on.
An email in Deleted Items, or gone from the mailbox, counts as Other. A folder that is not on
the list is recorded as such, without a category, so the report shows it instead of guessing.
Its Graph ID is kept (never its name), so when the list is set late or fixed, the next sweep
gives those emails their category from the same folder.

The report compares the two per category and lists every email the person treated as an
invoice that ARGUS would have sent to Other, and every email in a folder not on the list that
ARGUS would have sent to Other, because that one may be an invoice too. Both lists must be
empty before Arita switches to live mode; the thresholds
(:class:`argus.sorting.config.Thresholds`) are set from them. The report warns when no folder
list is set, so an empty list never reads as an all clear by mistake.

The proposal and the person's folder are written once each; the database checks it and writes
``email_proposed`` and ``email_sorted_by_person`` into the audit log (migration 0012). Graph is
called outside any transaction, and the result is recorded after in a short unit of work.
"""

import json
import logging
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.intake.filing import Folders
from argus.intake.graph import FOLDER_NAME, GraphClient
from argus.intake.mode import IntakeMode, intake_mode
from argus.intake.records import EmailRecord, EmailStep
from argus.sorting.models import ROUTE_FOR, Category, Route, SortingDecision

log = logging.getLogger("argus.intake.shadow")

SHADOW_ACTOR = Actor.system("mailbox-shadow")
FOLDERS_ENV = "ARGUS_SHADOW_FOLDERS"

# What the person did, when it is not one of the folders on the list.
DELETED_ITEMS = "Deleted Items"
GONE = "Deleted for good"
NOT_LISTED = "A folder not on the list"
# The report's row for emails in a folder that is not on the list.
NOT_LISTED_ROW = "not_listed"
INVOICES = frozenset({Category.INCOMING_INVOICE, Category.OUTGOING_INVOICE})


@dataclass(frozen=True)
class PersonFolders:
    """Max ASP's mail folders and what an email filed in each means, for example
    ``{"Rechnungen/Eingang": Category.INCOMING_INVOICE}``. Paths use ``/`` between a folder and
    its subfolder. Deleted Items always means Other and need not be listed."""

    folders: Mapping[str, Category] = field(default_factory=dict)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> "PersonFolders":
        """Read ``ARGUS_SHADOW_FOLDERS``: a JSON object of folder path to category, such as
        ``{"Invoices/Incoming": "incoming_invoice", "Reminders": "payment_reminder"}``."""
        raw = environ.get(FOLDERS_ENV, "").strip()
        if not raw:
            return cls()
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ValueError(f"{FOLDERS_ENV} must be a JSON object") from None
        if not isinstance(parsed, dict):
            raise ValueError(f"{FOLDERS_ENV} must be a JSON object")
        folders: dict[str, Category] = {}
        for path, category in parsed.items():
            if not isinstance(path, str) or not path.strip("/ "):
                raise ValueError(f"{FOLDERS_ENV} has an empty folder path")
            # Refused at start up, so one bad name never makes each sweep fail later.
            if not all(FOLDER_NAME.fullmatch(name) for name in path.strip("/").split("/")):
                raise ValueError(
                    f"{FOLDERS_ENV}: {path!r} may only use letters and spaces in folder names"
                )
            try:
                folders[path.strip("/")] = Category(category)
            except ValueError:
                raise ValueError(f"{FOLDERS_ENV}: {category!r} is not a category") from None
        return cls(folders)

    def resolve(self, folders: Folders) -> dict[str, tuple[str, Category]]:
        """The Graph ID of each listed folder that exists, with its path and category. Never
        creates a folder: in shadow mode ARGUS changes nothing in the mailbox."""
        found: dict[str, tuple[str, Category]] = {}
        for path, category in self.folders.items():
            folder_id = folders.existing(path)
            if folder_id is None:
                log.warning("Shadow run: folder %s from the folder list does not exist", path)
                continue
            found[folder_id] = (path, category)
        return found


def has_proposal(connection: Connection, email_id: int) -> bool:
    found = connection.execute(
        text("select proposal is not null from mailbox_email where id = :id"), {"id": email_id}
    ).scalar()
    return bool(found)


def mark_proposed(connection: Connection, email_id: int, decision: SortingDecision) -> bool:
    """Record the route ARGUS would have taken. The database checks it runs in shadow mode and
    the email is at step stored, and logs ``email_proposed``. Returns False when the email
    already has a proposal or is past step stored."""
    result = connection.execute(
        text(
            """
            update mailbox_email set proposal = cast(:details as jsonb)
            where id = :id and step = 'stored' and proposal is null
            returning id
            """
        ),
        {"id": email_id, "details": json.dumps(decision.audit_details())},
    )
    return result.first() is not None


def awaiting_person(connection: Connection) -> list[EmailRecord]:
    """Every email with a proposal whose folder after the person's sorting is not known yet,
    oldest first. Only emails still at step stored: one ARGUS itself took on in live mode is
    never counted as the person's work."""
    rows = connection.execute(
        text(
            """
            select id, graph_message_id, raw_sha256 from mailbox_email
            where proposal is not null and person_folder is null and step = 'stored'
            order by id
            """
        )
    ).mappings()
    return [
        EmailRecord(
            id=row["id"],
            graph_message_id=row["graph_message_id"],
            step=EmailStep.STORED,
            raw_sha256=row["raw_sha256"],
        )
        for row in rows
    ]


def mark_sorted_by_person(
    connection: Connection,
    email_id: int,
    folder: str,
    category: Category | None,
    folder_id: str | None,
) -> bool:
    """Record where the person put the email, the folder's Graph ID and what it means. The
    database logs ``email_sorted_by_person``. Returns False when it was already recorded."""
    result = connection.execute(
        text(
            """
            update mailbox_email
            set person_folder = :folder, person_category = :category, person_folder_id = :fid
            where id = :id and person_folder is null and proposal is not null
            returning id
            """
        ),
        {
            "id": email_id,
            "folder": folder,
            "category": category.value if category else None,
            "fid": folder_id,
        },
    )
    return result.first() is not None


def not_listed(connection: Connection) -> list[tuple[int, str]]:
    """The emails recorded in a folder not on the list, with that folder's Graph ID."""
    rows = connection.execute(
        text(
            """
            select id, person_folder_id from mailbox_email
            where person_folder = :folder and person_category is null
              and person_folder_id is not null
            order by id
            """
        ),
        {"folder": NOT_LISTED},
    )
    return [(row.id, row.person_folder_id) for row in rows]


def mark_mapped_later(
    connection: Connection, email_id: int, folder_id: str, folder: str, category: Category
) -> bool:
    """Give an email recorded as "not on the list" the category of its folder, now that the
    folder is on the list. Only once, and only for the same folder (migration 0012)."""
    result = connection.execute(
        text(
            """
            update mailbox_email set person_folder = :folder, person_category = :category
            where id = :id and person_folder = :not_listed and person_category is null
              and person_folder_id = :fid
            returning id
            """
        ),
        {
            "id": email_id,
            "folder": folder,
            "category": category.value,
            "not_listed": NOT_LISTED,
            "fid": folder_id,
        },
    )
    return result.first() is not None


@dataclass
class ObservationResult:
    sorted_by_person: int = 0
    mapped_later: int = 0  # emails "not on the list" whose folder is on the list now
    failed: int = 0


def observe_person(
    engine: Engine,
    graph: GraphClient,
    person_folders: PersonFolders,
    in_inbox: set[str],
) -> ObservationResult:
    """Record where the person put each proposed email that left the Inbox. ``in_inbox`` holds
    the Graph IDs this sweep listed; an email still listed is not asked about. Reads only:
    folder lookups, never a move or a new folder."""
    with engine.connect() as connection:
        pending = [r for r in awaiting_person(connection) if r.graph_message_id not in in_inbox]
        unlisted = not_listed(connection)
    result = ObservationResult()
    if not pending and not unlisted:
        return result
    try:
        known = person_folders.resolve(Folders(graph))
        inbox, deleted = graph.inbox(), graph.deleted_items()
    except Exception:
        # Graph throttled or down: counted as failed, the next sweep tries again.
        log.exception("Shadow run: folder lookup failed, next sweep tries again")
        result.failed += 1
        return result
    for email_id, folder_id in unlisted:
        if folder_id not in known:
            continue
        path, listed = known[folder_id]
        try:
            with unit_of_work(engine, SHADOW_ACTOR) as db:
                result.mapped_later += mark_mapped_later(db, email_id, folder_id, path, listed)
        except Exception:
            log.exception("Shadow run: email record %s not mapped, next sweep", email_id)
            result.failed += 1
    for record in pending:
        try:
            where = graph.folder_of(record.graph_message_id)
            if where == inbox:
                continue  # still there; the listing just missed it
            folder: str
            category: Category | None
            if where is None:
                folder, category = GONE, Category.OTHER
            elif where == deleted:
                folder, category = DELETED_ITEMS, Category.OTHER
            elif where in known:
                folder, category = known[where]
            else:
                folder, category = NOT_LISTED, None
            with unit_of_work(engine, SHADOW_ACTOR) as db:
                result.sorted_by_person += mark_sorted_by_person(
                    db, record.id, folder, category, where
                )
        except Exception:
            # The record ID only: no sender, subject or content in the log (NF-03).
            log.exception("Shadow run: folder of email record %s not read, next sweep", record.id)
            result.failed += 1
    return result


# The report


@dataclass(frozen=True)
class CategoryRow:
    """The emails the person put in one category, and what ARGUS proposed for them."""

    category: str  # a Category value, or NOT_LISTED_ROW
    emails: int
    same_route: int  # ARGUS proposed the route for this category
    manual_review: int  # ARGUS would have handed it to a person (IN-09)
    other_route: int  # ARGUS proposed a different route
    proposed: dict[str, int]  # route to number of emails


@dataclass(frozen=True)
class MissedInvoice:
    """An email ARGUS would have sent to Other that the person treated as an invoice (IN-06),
    or filed in a folder not on the list, so it may be one."""

    email_id: int
    internet_message_id: str | None
    received_at: datetime | None
    person_folder: str
    person_category: str | None  # None for a folder not on the list
    model_category: str | None
    model_confidence: float | None
    check_category: str | None


@dataclass(frozen=True)
class ShadowReport:
    mode: IntakeMode
    proposed: int  # emails with a proposal
    sorted_by_person: int  # of those, the ones the person has sorted
    waiting: int  # proposed, not sorted by the person yet
    categories: list[CategoryRow]
    invoices_to_other: list[MissedInvoice]
    # Emails in a folder not on the list that ARGUS would have sent to Other. Must be empty
    # too: such an email may be an invoice.
    not_listed_to_other: list[MissedInvoice]
    folder_list: list[str]  # the folder paths on the list when the report ran
    warnings: list[str]  # why the two lists above may be empty for the wrong reason


def shadow_report(connection: Connection, person_folders: PersonFolders) -> ShadowReport:
    """Compare ARGUS's proposals with what the person did, per category, and list every
    invoice ARGUS would have sent to Other, and every email in a folder not on the list that
    it would have sent to Other."""
    rows = connection.execute(
        text(
            """
            select id, internet_message_id, received_at, proposal, person_folder, person_category
            from mailbox_email where proposal is not null order by id
            """
        )
    ).mappings()
    proposed = 0
    by_row: dict[str, Counter[str]] = {}
    missed: list[MissedInvoice] = []
    unlisted_missed: list[MissedInvoice] = []
    for row in rows:
        proposed += 1
        if row["person_folder"] is None:
            continue
        proposal: dict[str, Any] = dict(row["proposal"])
        route = str(proposal["route"])
        key = row["person_category"] or NOT_LISTED_ROW
        by_row.setdefault(key, Counter())[route] += 1
        to_other = route == Route.DELETION_FOLDER
        if to_other and (row["person_category"] in INVOICES or row["person_category"] is None):
            (missed if row["person_category"] else unlisted_missed).append(
                MissedInvoice(
                    email_id=row["id"],
                    internet_message_id=row["internet_message_id"],
                    received_at=row["received_at"],
                    person_folder=row["person_folder"],
                    person_category=row["person_category"],
                    model_category=proposal.get("model_category"),
                    model_confidence=proposal.get("model_confidence"),
                    check_category=proposal.get("check_category"),
                )
            )
    order = [c.value for c in Category] + [NOT_LISTED_ROW]
    categories = [_row(key, by_row[key]) for key in order if key in by_row]
    sorted_by_person = sum(row.emails for row in categories)
    warnings: list[str] = []
    if not person_folders.folders:
        warnings.append(
            f"No folder list is set ({FOLDERS_ENV}), so ARGUS cannot tell which emails the"
            " person treated as invoices. The lists of emails sent to Other prove nothing yet."
        )
    if NOT_LISTED_ROW in by_row:
        warnings.append(
            f"{sum(by_row[NOT_LISTED_ROW].values())} emails are in folders not on the list."
            " Add those folders to the list; the next sweep then gives them their category."
        )
    return ShadowReport(
        mode=intake_mode(connection),
        proposed=proposed,
        sorted_by_person=sorted_by_person,
        waiting=proposed - sorted_by_person,
        categories=categories,
        invoices_to_other=missed,
        not_listed_to_other=unlisted_missed,
        folder_list=sorted(person_folders.folders),
        warnings=warnings,
    )


def _row(key: str, routes: Counter[str]) -> CategoryRow:
    expected = ROUTE_FOR[Category(key)].value if key != NOT_LISTED_ROW else None
    emails = sum(routes.values())
    same = routes[expected] if expected else 0
    manual = routes[Route.MANUAL_REVIEW.value]
    return CategoryRow(
        category=key,
        emails=emails,
        same_route=same,
        manual_review=manual,
        other_route=emails - same - manual,
        proposed=dict(sorted(routes.items())),
    )

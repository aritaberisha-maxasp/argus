"""Hand Wise statement emails from the mailbox sweep to the statement import (#166).

Covers BA-01, BA-07 and BA-08. The import itself is :mod:`argus.bank.importer` (#56, PR 162);
this module only decides which email files go to it and from which source.

Two ways in, as agreed on #56:

1. **From the Inbox.** Before an email is sorted, the sweep looks for CSV files in it. When the
   From address is one of ``ARGUS_STATEMENT_SENDERS`` and the email has an aligned SPF, DKIM or
   DMARC pass for that address's domain (:func:`argus.sorting.sender.authenticate`, the strict
   check of #70), every CSV file goes to the import. ``Authentication.passed`` is passed, never
   ``SenderCheck.treated_as``: the sorting treats mail from Max ASP's own domain as internal
   unless DMARC fails, so a forged From line with Benjamin's address would pass there
   (compliance review of PR 162).
2. **From the statement folder** ``ARGUS_STATEMENT_FOLDER`` (default ``ARGUS/Kontoauszüge``).
   Once per sweep the sweep lists that folder and hands every CSV file of each email in it to
   the import, whoever sent it. Only finance staff can move mail there (go live runbook).

A CSV file with Wise's columns from anyone else, or from a statement sender whose email fails
the strict check, is not imported. The import writes the refusal to the audit log with the
file's SHA-256, and the email gets one exception ``uncertain_classification`` that says why and
what to do: move the email into the statement folder if the statement is genuine. When that
email is then imported from the folder, the exception is resolved. Mail Benjamin sends from
inside the tenant often carries no SPF, DKIM or DMARC result, so this is the expected way for
his genuine internal mail until the go live check shows otherwise (docs/design/bank-import.md).

Every file goes to the import with the ``email_id`` of its email, so each imported statement
links back to the email it came in and an unreadable file always lands in the queue (QA on PR
162). Handing the same email over twice imports nothing twice (BA-07): the import knows each
file by its SHA-256.

Shadow mode works as in PR 162: the import runs, because it changes nothing outside ARGUS, and
the notices to Benjamin are recorded as ``not_sent_shadow`` instead of being sent.

Nothing here logs a sender, subject or file name (NF-03).
"""

import csv
import email
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from email import policy
from email.utils import parseaddr

from sqlalchemy import Connection, Engine, text

from argus.bank.importer import (
    BankSettings,
    ImportOutcome,
    ImportResult,
    StatementSource,
    import_statement,
)
from argus.bank.notices import Notifier
from argus.bank.statement import REQUIRED_COLUMNS
from argus.cases.model import Actor, ExceptionReason, Outcome
from argus.cases.store import open_exception, record_event, resolve_exception, unit_of_work
from argus.documents.email import Attachment, attachments_of
from argus.documents.store import DocumentStore
from argus.intake.digest import berlin_today
from argus.sorting.sender import authenticate, first_headers

log = logging.getLogger("argus.intake.statements")

STATEMENTS_ACTOR = Actor.system("statement-handover")
# The audit event that marks an email in the statement folder as handed over, so the sweep
# does not read it again every five minutes.
FROM_FOLDER_EVENT = "bank_import_from_folder"
# What the exception about a refused statement carries in its facts, to find it again.
SENDER_NOT_CONFIRMED = "statement_sender_not_confirmed"

CSV_TYPES = frozenset({"text/csv", "application/csv", "text/comma-separated-values"})
# How much of a file is read to see whether it has Wise's columns. The header is one line.
HEADER_BYTES = 4096


@dataclass(frozen=True)
class Statements:
    """What the sweep needs to hand statements to the import. ``notifier`` sends the notice
    about a blocked statement right away; without it the daily statement job sends it.
    ``today`` gives the Berlin date of the run."""

    documents: DocumentStore
    settings: BankSettings
    notifier: Notifier | None = None
    today: Callable[[], date] = field(default=berlin_today)


def csv_files(raw: bytes) -> list[Attachment]:
    """The CSV files attached to the email, by file name or content type."""
    return [
        a
        for a in attachments_of(raw)
        if (a.filename or "").strip().lower().endswith(".csv") or a.content_type in CSV_TYPES
    ]


def has_wise_columns(data: bytes) -> bool:
    """Whether the first line of the file names every column a Wise statement must have."""
    head = data[:HEADER_BYTES].decode("utf-8-sig", errors="replace")
    first = next(csv.reader(io.StringIO(head)), [])
    return set(REQUIRED_COLUMNS) <= {cell.strip() for cell in first}


def _sender(raw: bytes) -> tuple[str, bool]:
    """The From address in lower case, and whether the email passed the strict check for it."""
    message = email.message_from_bytes(raw, policy=policy.default)
    _, address = parseaddr(str(message.get("From", "")))
    address = address.strip().lower()
    passed = authenticate(address, first_headers(message.items())).passed
    return address, passed


def _import_all(
    engine: Engine,
    statements: Statements,
    source: StatementSource,
    files: list[Attachment],
) -> list[ImportResult]:
    today = statements.today()
    return [
        import_statement(
            engine,
            statements.documents,
            statements.settings,
            source,
            f.data,
            today=today,
            filename=f.filename,
            notifier=statements.notifier,
        )
        for f in files
    ]


def refused_detail(folder: str) -> str:
    """What the person in the exception queue reads. In German, like the other statement texts."""
    return (
        "Diese E-Mail enthält einen Wise Kontoauszug, kommt aber nicht nachweislich von einer "
        "für Kontoauszüge freigegebenen Adresse (Absenderprüfung nicht bestanden). Der Auszug "
        f"wurde nicht importiert. Ist er echt, die E-Mail in den Ordner {folder} verschieben; "
        "ARGUS importiert ihn dann von dort."
    )


def _flag_refused(engine: Engine, statements: Statements, email_id: int, files: int) -> None:
    """One exception per email, ever: a person who resolved it is not asked again."""
    with unit_of_work(engine, STATEMENTS_ACTOR) as db:
        flagged: bool = db.execute(
            text(
                "select exists (select 1 from exception_item where email_id = :id"
                " and facts ->> 'reason' = :reason)"
            ),
            {"id": email_id, "reason": SENDER_NOT_CONFIRMED},
        ).scalar_one()
        if flagged:
            return
        # The sorting may have opened one already for this email; one is enough (EX-01).
        open_one: bool = db.execute(
            text(
                "select exists (select 1 from exception_item where email_id = :id"
                " and reason = :reason and resolved_at is null)"
            ),
            {"id": email_id, "reason": ExceptionReason.UNCERTAIN_CLASSIFICATION.value},
        ).scalar_one()
        if open_one:
            return
        open_exception(
            db,
            ExceptionReason.UNCERTAIN_CLASSIFICATION,
            refused_detail(statements.settings.statement_folder),
            email_id=email_id,
            facts={"bank_statement": True, "reason": SENDER_NOT_CONFIRMED, "files": files},
        )
    log.warning("Email record %s holds a statement from an unconfirmed sender", email_id)


def hand_over_from_inbox(
    engine: Engine, statements: Statements, email_id: int, raw: bytes
) -> list[ImportResult]:
    """Hand the CSV files of an email in the Inbox to the import, when its sender is a
    confirmed statement sender. Safe to repeat. Returns one result per file handed over."""
    files = csv_files(raw)
    if not files:
        return []
    address, passed = _sender(raw)
    confirmed = passed and address in statements.settings.statement_senders
    if not confirmed:
        # Only files that look like a Wise statement matter here; any other CSV is left to the
        # sorting, as before.
        files = [f for f in files if has_wise_columns(f.data)]
        if not files:
            return []
    source = StatementSource.email(address, authenticated=passed, email_id=email_id)
    results = _import_all(engine, statements, source, files)
    if not confirmed:
        _flag_refused(engine, statements, email_id, len(files))
    log.info("Email record %s: %d statement files handed over", email_id, len(results))
    return results


def handed_over_from_folder(connection: Connection, email_id: int) -> bool:
    """Whether the email was already handed over from the statement folder."""
    found: bool = connection.execute(
        text(
            "select exists (select 1 from audit_event where email_id = :id and event_type = :event)"
        ),
        {"id": email_id, "event": FROM_FOLDER_EVENT},
    ).scalar_one()
    return found


def hand_over_from_folder(
    engine: Engine, statements: Statements, email_id: int, raw: bytes
) -> list[ImportResult]:
    """Hand every CSV file of an email in the statement folder to the import, then mark the
    email as handed over and resolve the exception about its unconfirmed sender, if any."""
    files = csv_files(raw)
    source = StatementSource.folder(statements.settings.statement_folder, email_id=email_id)
    results = _import_all(engine, statements, source, files)
    with unit_of_work(engine, STATEMENTS_ACTOR) as db:
        record_event(
            db,
            FROM_FOLDER_EVENT,
            email_id=email_id,
            # Fingerprints and outcomes only (NF-03).
            details={"files": [{"sha256": r.sha256, "outcome": r.outcome} for r in results]},
        )
        if any(r.outcome is not ImportOutcome.REFUSED for r in results):
            _resolve_refused(db, email_id)
    log.info("Email record %s: %d files handed over from the folder", email_id, len(results))
    return results


def _resolve_refused(db: Connection, email_id: int) -> None:
    open_ids = db.execute(
        text(
            "select id from exception_item where email_id = :id and resolved_at is null"
            " and facts ->> 'reason' = :reason"
        ),
        {"id": email_id, "reason": SENDER_NOT_CONFIRMED},
    ).scalars()
    for exception_id in list(open_ids):
        resolve_exception(
            db,
            exception_id,
            Outcome.CATEGORY_CHOSEN,
            resolution="Der Auszug wurde aus dem Ordner für Kontoauszüge übernommen.",
        )

"""Import one Wise statement file: once, stored unaltered, only when its balances add up.

Covers BA-01, BA-07, BA-08 and NF-01 (issue #56, ADR 0004). The steps:

1. **Who sent it.** A statement counts only when it comes from one of the addresses set in
   ``ARGUS_STATEMENT_SENDERS`` (Benjamin) and the email has an aligned SPF, DKIM or DMARC
   pass for that address's domain (the sender check of #70), or when it sits in the agreed
   mailbox folder ``ARGUS_STATEMENT_FOLDER``. Anything else is refused before the file is stored or read.
2. **Read it.** :mod:`argus.bank.statement` reads the CSV. A file that is not one currency and
   one finished month is refused. A refusal is written to the audit log with the file's
   fingerprint and a reason code.
3. **Store it.** The file goes into the document store under its SHA-256, exactly as received,
   before anything is recorded. The store is content addressed, so storing twice is harmless.
4. **Record it, in one unit of work.** Under one lock, so two imports never run side by side:
   the same file again adds nothing. A different file for a month already imported is recorded
   as ``month_already_imported`` and adds nothing either. Otherwise the balances are checked
   against the last imported month. If anything does not add up, the statement is recorded as
   ``blocked``, nothing is imported, an exception goes into the queue and a notice to Benjamin
   is queued (BA-08). If everything adds up, the statement is recorded as ``imported`` and each
   transaction becomes a bank transaction case, skipping any transaction already in from an
   earlier file (BA-07). An open balance difference for the same balance and month is then
   resolved as ``data_corrected``, because the corrected statement is in.
5. **Tell Benjamin**, after the commit (:mod:`argus.bank.notices`). Sending is never inside a
   unit of work.

The import runs in shadow mode too: it changes nothing outside ARGUS. Only the email to
Benjamin waits for live mode.

The application log holds IDs, currency, month and counts only, never names, references or
the sender's address (NF-03).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sqlalchemy import Connection, Engine, text

from argus.bank.notices import NoticeKind, Notifier, queue_notice, send_pending_notices
from argus.bank.statement import (
    Difference,
    StatementUnreadable,
    Unreadable,
    WiseStatement,
    balance_differences,
    read_wise_csv,
)
from argus.cases.model import Actor, CaseKind, ExceptionReason, Outcome
from argus.cases.store import (
    create_case,
    open_exception,
    record_event,
    resolve_exception,
    unit_of_work,
)
from argus.documents.store import DocumentStore, fingerprint

log = logging.getLogger("argus.bank.importer")

IMPORT_ACTOR = Actor.system("bank-import")
IMPORT_LOCK = "argus_bank_import"
CONTENT_TYPE = "text/csv"

SENDERS_ENV = "ARGUS_STATEMENT_SENDERS"
FOLDER_ENV = "ARGUS_STATEMENT_FOLDER"
BALANCES_ENV = "ARGUS_WISE_BALANCES"
DEFAULT_FOLDER = "ARGUS/Kontoauszüge"
DEFAULT_BALANCES = frozenset({"EUR"})


class SourceKind(StrEnum):
    EMAIL = "email"
    FOLDER = "folder"


@dataclass(frozen=True)
class StatementSource:
    """Where a statement file came from. Use :meth:`email` or :meth:`folder`."""

    kind: SourceKind
    email_id: int | None = None
    sender_address: str | None = None
    sender_authenticated: bool = False
    folder_path: str | None = None

    @classmethod
    def email(
        cls, sender_address: str, *, authenticated: bool, email_id: int | None = None
    ) -> "StatementSource":
        """An email from ``sender_address``. ``authenticated`` is True only when the email has
        an aligned SPF, DKIM or DMARC pass for that address's domain.

        The caller must pass ``Authentication.passed`` from
        :func:`argus.sorting.sender.authenticate` for the sender's own address. Never pass
        ``SenderCheck.treated_as`` or "not a failed claim": mail from Max ASP's own domain is
        not checked there and counts as internal unless DMARC fails, so a forged From line
        with Benjamin's address could pass (compliance review of PR 162)."""
        return cls(
            SourceKind.EMAIL,
            email_id=email_id,
            sender_address=sender_address,
            sender_authenticated=authenticated,
        )

    @classmethod
    def folder(cls, folder_path: str, *, email_id: int | None = None) -> "StatementSource":
        """A file found in the mailbox folder ``folder_path``, such as ``ARGUS/Kontoauszüge``."""
        return cls(SourceKind.FOLDER, email_id=email_id, folder_path=folder_path)


@dataclass(frozen=True)
class BankSettings:
    """Who may send statements and which Wise balances Max ASP holds."""

    statement_senders: frozenset[str] = frozenset()
    statement_folder: str = DEFAULT_FOLDER
    balances: frozenset[str] = DEFAULT_BALANCES

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "BankSettings":
        senders = frozenset(
            s.strip().lower() for s in environ.get(SENDERS_ENV, "").split(",") if s.strip()
        )
        if any(s.count("@") != 1 or s.startswith("@") or s.endswith("@") for s in senders):
            raise ValueError(f"{SENDERS_ENV} must be a comma separated list of email addresses")
        balances = frozenset(
            b.strip().upper() for b in environ.get(BALANCES_ENV, "").split(",") if b.strip()
        )
        if any(len(b) != 3 or not b.isalpha() for b in balances):
            raise ValueError(f"{BALANCES_ENV} must be a comma separated list of currency codes")
        folder = environ.get(FOLDER_ENV, "").strip() or DEFAULT_FOLDER
        return cls(senders, folder, balances or DEFAULT_BALANCES)

    def accepts(self, source: StatementSource) -> bool:
        """Whether a statement from ``source`` counts (#56, first acceptance criterion)."""
        if source.kind is SourceKind.FOLDER:
            return (source.folder_path or "").strip("/") == self.statement_folder.strip("/")
        address = (source.sender_address or "").strip().lower()
        return source.sender_authenticated and address in self.statement_senders


class ImportOutcome(StrEnum):
    IMPORTED = "imported"
    BLOCKED = "blocked"
    MONTH_ALREADY_IMPORTED = "month_already_imported"
    ALREADY_SEEN = "already_seen"  # this very file was looked at before; nothing new
    REFUSED = "refused"  # not from Benjamin or the folder, or not a statement ARGUS can read


@dataclass(frozen=True)
class ImportResult:
    outcome: ImportOutcome
    statement_id: int | None = None
    sha256: str | None = None
    cases_created: int = 0
    already_in: int = 0  # transactions an earlier file already imported (BA-07)
    differences: tuple[Difference, ...] = ()
    refused_because: str | None = None  # a reason code, for a refused file
    case_ids: tuple[int, ...] = field(default=(), repr=False)


def unreadable_detail(unreadable: StatementUnreadable) -> str:
    """What the person in the exception queue reads when a statement from Benjamin or the
    agreed folder cannot be read. In German, like the notices to Benjamin."""
    why = UNREADABLE_REASONS.get(unreadable.reason, "die Datei kann nicht gelesen werden")
    return (
        f"Der Wise Kontoauszug wurde nicht importiert: {why}. Bitte den Auszug für genau einen "
        "abgeschlossenen Kalendermonat und eine Währung als CSV aus Wise herunterladen und "
        "erneut senden."
    )


UNREADABLE_REASONS = {
    Unreadable.TOO_LARGE: "die Datei ist zu groß für einen Monatsauszug",
    Unreadable.NOT_TEXT: "die Datei ist keine CSV Datei in UTF-8",
    Unreadable.NOT_A_WISE_STATEMENT: "die Spalten eines Wise Kontoauszugs fehlen",
    Unreadable.NO_TRANSACTIONS: "der Auszug enthält keine Buchungen",
    Unreadable.BAD_ROW: "eine Zeile kann nicht gelesen werden",
    Unreadable.SAME_ID_TWICE: "dieselbe Buchung steht zweimal im Auszug",
    Unreadable.MORE_THAN_ONE_CURRENCY: "der Auszug enthält mehr als eine Währung",
    Unreadable.MORE_THAN_ONE_MONTH: "der Auszug reicht über mehr als einen Kalendermonat",
    Unreadable.MONTH_NOT_OVER: "der Monat des Auszugs ist noch nicht vorbei",
}


def _refuse(
    engine: Engine,
    source: StatementSource,
    sha256: str,
    reason: str,
    unreadable: StatementUnreadable | None = None,
) -> ImportResult:
    """Log the refusal. A file from an accepted source that cannot be read also goes into the
    queue on its email, so a person sees why (EX-01, code review of PR 162). A file from
    anyone else stays in the audit log only."""
    with unit_of_work(engine, IMPORT_ACTOR) as db:
        record_event(
            db,
            "bank_import_refused",
            email_id=source.email_id,
            details={"sha256": sha256, "source": source.kind.value, "reason": reason},
        )
        if unreadable is not None and source.email_id is not None:
            already_open: bool = db.execute(
                text(
                    "select exists (select 1 from exception_item where email_id = :id"
                    " and reason = 'unreadable_document' and resolved_at is null)"
                ),
                {"id": source.email_id},
            ).scalar_one()
            if not already_open:
                open_exception(
                    db,
                    ExceptionReason.UNREADABLE_DOCUMENT,
                    unreadable_detail(unreadable),
                    email_id=source.email_id,
                    facts={"bank_statement": True, "reason": reason, "sha256": sha256},
                )
    log.warning("Statement %s refused: %s", sha256[:12], reason)
    return ImportResult(ImportOutcome.REFUSED, sha256=sha256, refused_because=reason)


def import_statement(
    engine: Engine,
    documents: DocumentStore,
    settings: BankSettings,
    source: StatementSource,
    data: bytes,
    *,
    today: date,
    filename: str | None = None,
    notifier: Notifier | None = None,
) -> ImportResult:
    """Import one statement file. ``today`` is the Berlin date of the run. With ``notifier``
    a notice about a blocked statement goes out right after the import; without it, the next
    :func:`argus.bank.notices.send_pending_notices` sends it."""
    sha256 = fingerprint(data)
    if not settings.accepts(source):
        return _refuse(engine, source, sha256, "unknown_source")
    try:
        statement = read_wise_csv(data, today=today)
    except StatementUnreadable as unreadable:
        return _refuse(engine, source, sha256, unreadable.reason.value, unreadable)

    stored = documents.put(data, CONTENT_TYPE)  # outside any transaction
    with unit_of_work(engine, IMPORT_ACTOR) as db:
        db.execute(text("select pg_advisory_xact_lock(hashtext(:lock))"), {"lock": IMPORT_LOCK})
        result = _record(db, statement, source, stored.sha256, stored.location, len(data), filename)

    log.info(
        "Statement %s %s %s: %s, %d cases created, %d already in",
        result.statement_id,
        statement.currency,
        statement.period.strftime("%Y-%m"),
        result.outcome,
        result.cases_created,
        result.already_in,
    )
    if result.outcome is ImportOutcome.BLOCKED and notifier is not None:
        send_pending_notices(engine, notifier)
    return result


def _record(
    db: Connection,
    statement: WiseStatement,
    source: StatementSource,
    sha256: str,
    location: str,
    size: int,
    filename: str | None,
) -> ImportResult:
    earlier = db.execute(
        text("select id, outcome from bank_statement where sha256 = :sha order by id"),
        {"sha": sha256},
    ).all()
    settled = [row for row in earlier if row.outcome != "blocked"]
    if settled:
        return ImportResult(ImportOutcome.ALREADY_SEEN, settled[0].id, sha256)

    month_taken = db.execute(
        text(
            "select id from bank_statement where currency = :currency and period = :period"
            " and outcome = 'imported'"
        ),
        {"currency": statement.currency, "period": statement.period},
    ).scalar_one_or_none()
    if month_taken is not None:
        statement_id = _insert(
            db, statement, source, sha256, location, size, filename, "month_already_imported"
        )
        return ImportResult(ImportOutcome.MONTH_ALREADY_IMPORTED, statement_id, sha256)

    previous = db.execute(
        text(
            "select closing_minor from bank_statement where currency = :currency"
            " and period < :period and outcome = 'imported' order by period desc limit 1"
        ),
        {"currency": statement.currency, "period": statement.period},
    ).scalar_one_or_none()
    later: bool = db.execute(
        text(
            "select exists (select 1 from bank_statement where currency = :currency"
            " and period > :period and outcome = 'imported')"
        ),
        {"currency": statement.currency, "period": statement.period},
    ).scalar_one()
    differences = balance_differences(statement, previous, later_month_imported=later)

    if differences:
        if earlier:
            # The same blocked file again, and it still does not add up: nothing new.
            return ImportResult(
                ImportOutcome.ALREADY_SEEN, earlier[-1].id, sha256, differences=tuple(differences)
            )
        statement_id = _insert(db, statement, source, sha256, location, size, filename, "blocked")
        _block(db, statement, statement_id, differences)
        return ImportResult(
            ImportOutcome.BLOCKED, statement_id, sha256, differences=tuple(differences)
        )

    statement_id = _insert(db, statement, source, sha256, location, size, filename, "imported")
    created, already_in = _import_transactions(db, statement, statement_id)
    _resolve_corrected(db, statement, statement_id)
    return ImportResult(
        ImportOutcome.IMPORTED,
        statement_id,
        sha256,
        cases_created=len(created),
        already_in=already_in,
        case_ids=tuple(created),
    )


def _insert(
    db: Connection,
    statement: WiseStatement,
    source: StatementSource,
    sha256: str,
    location: str,
    size: int,
    filename: str | None,
    outcome: str,
) -> int:
    statement_id: int = db.execute(
        text(
            """
            insert into bank_statement (sha256, location, size_bytes, filename, source,
                                        source_email_id, currency, period, opening_minor,
                                        closing_minor, transaction_count, outcome)
            values (:sha256, :location, :size, :filename, :source, :email_id, :currency,
                    :period, :opening, :closing, :count, :outcome)
            returning id
            """
        ),
        {
            "sha256": sha256,
            "location": location,
            "size": size,
            "filename": filename,
            "source": source.kind.value,
            "email_id": source.email_id,
            "currency": statement.currency,
            "period": statement.period,
            "opening": statement.opening_cents,
            "closing": statement.closing_cents,
            "count": len(statement.transactions),
            "outcome": outcome,
        },
    ).scalar_one()
    return statement_id


def difference_detail(statement: WiseStatement, differences: list[Difference]) -> str:
    """What the person in the exception queue reads, in German like the email to Benjamin."""
    month = statement.period.strftime("%m/%Y")
    lines = "; ".join(d.describe(statement.currency) for d in differences[:10])
    more = f" und {len(differences) - 10} weitere" if len(differences) > 10 else ""
    return (
        f"Der Wise Kontoauszug {statement.currency} für {month} wurde nicht importiert, weil die "
        f"Salden nicht aufgehen: {lines}{more}. Bitte prüfen und einen korrigierten Auszug "
        "senden."
    )


def _block(
    db: Connection, statement: WiseStatement, statement_id: int, differences: list[Difference]
) -> None:
    open_exception(
        db,
        ExceptionReason.BANK_BALANCE_DIFFERENCE,
        difference_detail(statement, differences),
        statement_id=statement_id,
        facts={
            "currency": statement.currency,
            "period": statement.period.isoformat(),
            "checks": sorted({d.check.value for d in differences}),
            "differences": len(differences),
        },
    )
    queue_notice(
        db,
        NoticeKind.BALANCE_DIFFERENCE,
        statement.period,
        (statement.currency,),
        statement_id=statement_id,
    )


def _import_transactions(
    db: Connection, statement: WiseStatement, statement_id: int
) -> tuple[list[int], int]:
    existing: set[str] = set(
        db.execute(
            text(
                "select wise_transaction_id from bank_transaction"
                " where wise_balance_currency = :currency"
                " and wise_transaction_id = any(:ids)"
            ),
            {
                "currency": statement.currency,
                "ids": [t.wise_id for t in statement.transactions],
            },
        ).scalars()
    )
    created: list[int] = []
    for transaction in statement.transactions:
        if transaction.wise_id in existing:
            continue
        case_id = create_case(
            db,
            CaseKind.BANK_TRANSACTION,
            document_number=transaction.wise_id,
            gross_cents=transaction.amount_cents,
            currency=statement.currency,
        )
        db.execute(
            text(
                """
                insert into bank_transaction (case_id, wise_balance_currency, wise_transaction_id,
                                              booked_on, amount_minor, statement_id,
                                              counterparty, payment_reference, description)
                values (:case_id, :currency, :wise_id, :booked_on, :amount, :statement_id,
                        :counterparty, :reference, :description)
                """
            ),
            {
                "case_id": case_id,
                "currency": statement.currency,
                "wise_id": transaction.wise_id,
                "booked_on": transaction.booked_on,
                "amount": transaction.amount_cents,
                "statement_id": statement_id,
                "counterparty": transaction.counterparty,
                "reference": transaction.reference,
                "description": transaction.description,
            },
        )
        created.append(case_id)
    return created, len(existing)


def _resolve_corrected(db: Connection, statement: WiseStatement, statement_id: int) -> None:
    """The corrected statement is in, so the open differences of the same balance and month
    are done."""
    open_ids: list[int] = list(
        db.execute(
            text(
                """
            select e.id from exception_item e join bank_statement s on s.id = e.statement_id
            where e.reason = 'bank_balance_difference' and e.resolved_at is null
              and s.currency = :currency and s.period = :period
            """
            ),
            {"currency": statement.currency, "period": statement.period},
        ).scalars()
    )
    for exception_id in open_ids:
        resolve_exception(
            db,
            exception_id,
            Outcome.DATA_CORRECTED,
            resolution=f"Der korrigierte Auszug ist importiert (Auszug {statement_id}).",
        )

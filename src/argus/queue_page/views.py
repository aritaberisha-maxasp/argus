"""What the exception queue pages show (#126, EX-01, EX-02).

The list shows every open exception with its case, reason, age in working days and sender. One
exception opens with why ARGUS stopped, the case, and the email it came from with every file as
stored (#20). These functions only read: the queue is changed by the stories that resolve
exceptions, such as correcting the values of an invoice (#112, :mod:`argus.intake.corrections`).

The sender in the list is the From domain that the sorting recorded and checked (#70), so the
list needs no file from the document store. The full From address is in the stored email, which
the page for one exception reads.

Nothing here logs. Names, addresses, subjects and file names are personal data and stay out of
the log (NF-03).
"""

from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any

from sqlalchemy import Connection, text

from argus.approvals_page.query import query_of_exception
from argus.cases.model import CaseKind, CaseStatus, ExceptionReason
from argus.cases.store import list_open_exceptions
from argus.intake.corrections import FIELDS
from argus.intake.email_text import body_of
from argus.queue_page.facts import fact_lines
from argus.working_days import working_days_between

# Plain words for each reason, for Benjamin. A test checks every reason has one.
REASON_TEXT: dict[ExceptionReason, str] = {
    ExceptionReason.UNCERTAIN_CLASSIFICATION: "ARGUS could not sort the email with certainty",
    ExceptionReason.UNREADABLE_DOCUMENT: "A document could not be read",
    ExceptionReason.UNKNOWN_VENDOR: "The vendor is not known yet",
    ExceptionReason.UNKNOWN_CUSTOMER: "The customer is not known yet",
    ExceptionReason.VENDOR_WITHOUT_APPROVER: "The vendor has no approver",
    ExceptionReason.DUPLICATE: "This may be a duplicate invoice",
    ExceptionReason.IBAN_MISMATCH: "Possible fraud: the bank account differs from DATEV",
    ExceptionReason.INVOICE_CHECK_FAILED: "The invoice failed a required check",
    ExceptionReason.BOOKING_ERROR: "Booking in DATEV failed",
    ExceptionReason.UNMATCHED_BANK_TRANSACTION: "A bank transaction matches no invoice",
    ExceptionReason.BANK_BALANCE_DIFFERENCE: "The bank balance does not add up",
    ExceptionReason.NUMBER_SEQUENCE_GAP: "An invoice number is missing in the sequence",
    ExceptionReason.PAYMENT_FAILED: "A payment failed",
    ExceptionReason.CUSTOMER_QUERY: "A customer asked a question",
    ExceptionReason.FORWARD_FAILED: "A payment reminder could not be forwarded",
    ExceptionReason.AUDIT_COPY_FAILED: "The nightly copy of the audit log failed",
    ExceptionReason.AUDIT_CHECK_FAILED: "The audit log did not match its copies",
    ExceptionReason.VALUES_TO_CHECK: "The invoice number, amount or currency needs checking",
    ExceptionReason.NO_ACCOUNT_PROPOSAL: "ARGUS cannot propose a G/L account and tax key",
    ExceptionReason.APPROVER_ABSENT: "The approver is away and the vendor has no deputy",
    ExceptionReason.APPROVER_ADDRESS_OUTSIDE: "The approver has an email address outside Max ASP",
    ExceptionReason.APPROVER_QUERY: "The approver sent the invoice back with a question",
}


@dataclass(frozen=True)
class QueueRow:
    exception_id: int
    reason: ExceptionReason
    reason_text: str
    opened_at: datetime
    age_working_days: int
    case_id: int | None
    case_kind: CaseKind | None
    email_id: int | None
    statement_id: int | None
    sender: str | None  # the From domain, or None when there is no email or it is not sorted yet


@dataclass(frozen=True)
class CaseSummary:
    id: int
    kind: CaseKind
    status: CaseStatus
    resume_status: CaseStatus | None
    document_number: str | None
    gross_cents: int | None
    currency: str | None


@dataclass(frozen=True)
class FileRow:
    document_id: int
    kind: str  # "email" for the raw email, "attachment" for each attached file
    filename: str | None
    content_type: str
    size_bytes: int
    sha256: str
    linked_to_case: bool


@dataclass(frozen=True)
class ExceptionDetail:
    exception_id: int
    reason: ExceptionReason
    reason_text: str
    detail: str
    facts: dict[str, Any]
    opened_at: datetime
    age_working_days: int
    case: CaseSummary | None
    email_id: int | None
    statement_id: int | None
    received_at: datetime | None
    sender: str | None
    files: list[FileRow]
    # The person can correct the invoice number, amount and currency here (#112).
    correctable: bool = False
    # What ARGUS read for those values, per field: value, flag and note. Empty without a case.
    read_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The person can enter the G/L account and tax key here (#37).
    account_enterable: bool = False
    # The facts in plain words, label and value, for the box "Why ARGUS stopped" (#188).
    fact_lines: list[tuple[str, str]] = field(default_factory=list)
    # The approver's question, who asked and when, for an approver question (#192). The person
    # answers it here, or rejects the invoice.
    approver_query: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmailView:
    """The original email as a person reads it, from the copy stored byte for byte."""

    sender: str
    to: str
    cc: str
    subject: str
    date: str
    body: str


def _senders(connection: Connection, email_ids: set[int]) -> dict[int, str | None]:
    if not email_ids:
        return {}
    rows = connection.execute(
        text(
            """
            select id, classification -> 'sender' ->> 'from_domain' as from_domain
            from mailbox_email where id = any(:ids)
            """
        ),
        {"ids": sorted(email_ids)},
    ).mappings()
    return {r["id"]: r["from_domain"] for r in rows}


def _source_emails(connection: Connection, case_ids: set[int]) -> dict[int, int | None]:
    if not case_ids:
        return {}
    rows = connection.execute(
        text("select id, source_email_id from invoice_case where id = any(:ids)"),
        {"ids": sorted(case_ids)},
    ).mappings()
    return {r["id"]: r["source_email_id"] for r in rows}


def queue_rows(connection: Connection) -> list[QueueRow]:
    """Every open exception, oldest first (EX-01). The age counts working days in
    Baden-Württemberg, by the database clock that set the opening time."""
    items = list_open_exceptions(connection)
    sources = _source_emails(connection, {i.case_id for i in items if i.case_id is not None})
    email_of = {i.id: i.email_id if i.case_id is None else sources.get(i.case_id) for i in items}
    senders = _senders(connection, {e for e in email_of.values() if e is not None})
    return [
        QueueRow(
            exception_id=i.id,
            reason=i.reason,
            reason_text=REASON_TEXT[i.reason],
            opened_at=i.opened_at,
            age_working_days=working_days_between(i.opened_at, i.opened_at + i.age),
            case_id=i.case_id,
            case_kind=i.case_kind,
            email_id=email_of[i.id],
            statement_id=i.statement_id,
            sender=senders.get(email_of[i.id] or 0),  # record IDs start at 1
        )
        for i in items
    ]


def open_exception_detail(connection: Connection, exception_id: int) -> ExceptionDetail | None:
    """One open exception with its case and the files of its email (EX-02). None when there is
    no such exception or it is resolved already: the queue shows only open ones."""
    row = (
        connection.execute(
            text(
                """
                select e.id, e.reason, e.detail, e.facts, e.opened_at,
                       clock_timestamp() as now, e.case_id, e.email_id, e.statement_id,
                       c.kind, c.status, c.resume_status, c.document_number, c.gross_minor,
                       c.currency, c.source_email_id
                from exception_item e left join invoice_case c on c.id = e.case_id
                where e.id = :id and e.resolved_at is null
                """
            ),
            {"id": exception_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    case = None
    if row["case_id"] is not None:
        case = CaseSummary(
            id=row["case_id"],
            kind=CaseKind(row["kind"]),
            status=CaseStatus(row["status"]),
            resume_status=CaseStatus(row["resume_status"]) if row["resume_status"] else None,
            document_number=row["document_number"],
            gross_cents=row["gross_minor"],
            currency=row["currency"],
        )
    email_id: int | None = row["email_id"] if case is None else row["source_email_id"]
    received_at, sender = None, None
    if email_id is not None:
        found = (
            connection.execute(
                text(
                    """
                    select received_at, classification -> 'sender' ->> 'from_domain' as sender
                    from mailbox_email where id = :id
                    """
                ),
                {"id": email_id},
            )
            .mappings()
            .one_or_none()
        )
        if found is not None:
            received_at, sender = found["received_at"], found["sender"]
    reason = ExceptionReason(row["reason"])
    asked = None
    if reason is ExceptionReason.APPROVER_QUERY:
        found_query = query_of_exception(connection, row["id"])
        if found_query is not None:
            others: int = connection.execute(
                text(
                    "select count(*) from exception_item"
                    " where case_id = :case and resolved_at is null and id <> :id"
                ),
                {"case": row["case_id"], "id": row["id"]},
            ).scalar_one()
            asked = {
                "question": found_query.question,
                "asked_by_name": found_query.asked_by_name,
                "asked_as": found_query.asked_as,
                "on_behalf_of_name": found_query.on_behalf_of_name,
                "asked_at": found_query.asked_at.isoformat(),
                "others_open": others,
            }
    return ExceptionDetail(
        exception_id=row["id"],
        reason=reason,
        reason_text=REASON_TEXT[reason],
        detail=row["detail"],
        facts=dict(row["facts"]),
        opened_at=row["opened_at"],
        age_working_days=working_days_between(row["opened_at"], row["now"]),
        case=case,
        email_id=email_id,
        statement_id=row["statement_id"],
        received_at=received_at,
        sender=sender,
        files=email_files(connection, email_id, row["case_id"]) if email_id is not None else [],
        correctable=reason is ExceptionReason.VALUES_TO_CHECK and case is not None,
        read_values=read_values(connection, case.id) if case is not None else {},
        account_enterable=reason is ExceptionReason.NO_ACCOUNT_PROPOSAL and case is not None,
        fact_lines=fact_lines(row["facts"]),
        approver_query=asked,
    )


def read_values(connection: Connection, case_id: int) -> dict[str, dict[str, Any]]:
    """What ARGUS read for the invoice number, gross amount and currency of a case, from the
    reading of its invoice (#30): the value, the flag when a person has to check it, and the
    note that says why. Empty when the case has no stored reading."""
    reading: dict[str, Any] | None = connection.execute(
        text(
            "select reading from invoice_reading where case_id = :id and role = 'invoice'"
            " order by id limit 1"
        ),
        {"id": case_id},
    ).scalar_one_or_none()
    fields: dict[str, Any] = (reading or {}).get("fields") or {}
    out: dict[str, dict[str, Any]] = {}
    for name in FIELDS:
        found = fields.get(name)
        if not isinstance(found, dict):
            continue
        value = found.get("value")
        out[name] = {
            "value": None if value is None else str(value),
            "flag": found.get("flag"),
            "note": found.get("note"),
        }
    return out


def email_files(connection: Connection, email_id: int, case_id: int | None) -> list[FileRow]:
    """The raw email and every attachment as stored (#20), the email first. For a case it also
    says which files the case is based on, since one email can hold several invoices (#30)."""
    rows = connection.execute(
        text(
            """
            select d.id, d.kind, d.filename, d.content_type, d.size_bytes, d.sha256,
                   exists (select 1 from case_document l
                           where l.document_id = d.id and l.case_id = :case_id) as linked
            from stored_document d
            where d.email_id = :email_id
            order by d.kind = 'attachment', d.id
            """
        ),
        {"email_id": email_id, "case_id": case_id},
    ).mappings()
    return [
        FileRow(
            document_id=r["id"],
            kind=str(r["kind"]),
            filename=r["filename"],
            content_type=r["content_type"],
            size_bytes=r["size_bytes"],
            sha256=r["sha256"],
            linked_to_case=bool(r["linked"]),
        )
        for r in rows
    ]


def read_email(raw: bytes) -> EmailView:
    """The headers a person needs and the text of the email. The page shows this as text and
    never as HTML, so nothing in an email can run in Benjamin's browser."""
    message = message_from_bytes(raw, policy=policy.default)
    if not isinstance(message, EmailMessage):  # never with policy.default, but say so to mypy
        raise TypeError("the stored email cannot be parsed")

    def header(name: str) -> str:
        try:
            value = message.get(name)
        except Exception:  # a broken header must not hide the rest of the email
            return ""
        return str(value) if value is not None else ""

    try:
        body = body_of(message)
    except Exception:  # an odd encoding: the file itself is still there to download
        body = ""
    return EmailView(
        sender=header("From"),
        to=header("To"),
        cc=header("Cc"),
        subject=header("Subject"),
        date=header("Date"),
        body=body,
    )

"""What the approval page shows (#38, AP-02, AP-03, AP-08, PR-04, NF-03).

These functions only read; nothing here changes a case.

- **Who decides** is the person on the newest approval request of the case (#191, ADR 0010):
  the vendor's approver, or the deputy while the approver was away. Nobody else decides, also
  not finance or admin. They see every waiting invoice, but read only.
- **The invoice**: vendor and DATEV account, invoice number, invoice and due date, net, VAT and
  gross amount and currency. The number, gross amount and currency come from the case, which
  a person may have corrected (#112). The rest comes from what ARGUS read on the invoice
  (``invoice_reading``).
- **The invoice image**: the stored file the invoice was read from (``invoice_reading``), or
  else the file linked to the case. An invoice that came in the email text has no file.
- **The proposal**: the current G/L account and tax key (``current_proposal``, #37), with where
  it came from: the DATEV booking it was copied from, or the person who entered it and why. One
  entered after the request went out was changed at approval.
- **The decision**, once there is one: approved or rejected, by whom (name as it was then),
  when, and the comment.
- **The second approval above EUR 5,000** (#39, AP-09): on a second request, who gave the first
  approval and when; the amount in EUR the line was checked with (and the Wise rate for another
  currency); and, after a first approval that does not settle the case, what it waits for.
  A second approval given by the same person is marked "same person".

Nothing here logs. Names and comments are personal data and stay out of the log (NF-03).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind, CaseStatus
from argus.cases.money import to_cents
from argus.intake.accounts import ProposalSource, current_proposal
from argus.invoices_page.views import STATUS_TEXT

# Files a browser can show safely on the page. Anything else is offered as a download.
VIEWABLE = frozenset({"application/pdf", "image/png", "image/jpeg"})
# The most rows the list holds. An approver has a handful; this only bounds a broken state.
MAX_ROWS = 500


@dataclass(frozen=True)
class Request:
    """The newest approval request of a case: who was asked."""

    request_id: int
    recipient_id: str
    recipient_name: str
    recipient_role: str  # approver or deputy
    on_behalf_of_name: str | None
    requested_at: datetime
    sent_at: datetime | None
    step: str = "first"  # first, or second for the second approval above EUR 5,000 (#39)
    same_person: bool = False  # the second approver also gave the first approval


@dataclass(frozen=True)
class FirstApproval:
    """Who gave the first approval, shown on the second request (#39)."""

    decided_by_name: str
    decided_as: str
    on_behalf_of_name: str | None
    decided_at: datetime


@dataclass(frozen=True)
class AmountCheck:
    """The gross amount in EUR a first approval was checked with against EUR 5,000 (#39)."""

    currency: str
    gross_cents: int
    rate: str  # EUR for one unit of the currency, as text so no digit is lost
    rate_time: datetime
    eur_cents: int
    above_line: bool
    from_wise: bool


@dataclass(frozen=True)
class Decision:
    decision_id: int
    request_id: int
    decision: str  # approved or rejected
    comment: str | None
    decided_by: str  # user:<Entra object ID>
    decided_by_name: str
    decided_as: str
    on_behalf_of_name: str | None
    decided_at: datetime
    same_person: bool = False


@dataclass(frozen=True)
class Proposal:
    proposal_id: int  # sent back with the decision, so only what the page showed is approved
    gl_account: str
    tax_key: str  # blank means no tax key: no input VAT is booked
    chart: str
    source: str
    origin: str  # a plain sentence: where it came from
    changed_at_approval: bool
    by_name: str | None
    reason: str | None
    proposed_at: datetime | None


@dataclass(frozen=True)
class InvoiceFile:
    document_id: int
    content_type: str
    filename: str | None
    pages: list[int]
    viewable: bool  # shown on the page; otherwise only downloaded


@dataclass(frozen=True)
class ApprovalRow:
    case_id: int
    kind: CaseKind
    vendor_name: str | None
    vendor_account: str | None
    document_number: str | None
    gross_cents: int | None
    currency: str | None
    due_date: str | None
    recipient_id: str
    recipient_name: str
    recipient_role: str
    on_behalf_of_name: str | None
    requested_at: datetime
    sent_at: datetime | None
    step: str = "first"


@dataclass(frozen=True)
class ApprovalView:
    case_id: int
    kind: CaseKind
    status: CaseStatus
    status_text: str
    closed_reason: str | None
    vendor_name: str | None
    vendor_account: str | None
    document_number: str | None
    invoice_date: str | None
    due_date: str | None
    net_cents: int | None
    vat_cents: int | None
    gross_cents: int | None
    currency: str | None
    request: Request
    proposal: Proposal | None
    decision: Decision | None
    file: InvoiceFile | None
    in_email_text: bool  # the invoice was in the email text, so there is no file to show
    first_approval: FirstApproval | None = None  # on a second request (#39)
    amount: AmountCheck | None = None  # the first approval's amount against EUR 5,000
    next_step: str | None = None  # what a first approval that did not settle the case waits for

    @property
    def waiting(self) -> bool:
        return self.status is CaseStatus.AWAITING_APPROVAL and self.decision is None


def _value(fields: dict[str, Any], name: str) -> str | None:
    found = fields.get(name)
    if not isinstance(found, dict) or found.get("value") is None:
        return None
    return str(found["value"])


def _cents(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return to_cents(Decimal(value.replace(",", ".")))
    except (InvalidOperation, ValueError, TypeError):
        return None


def newest_request(connection: Connection, case_id: int) -> Request | None:
    row = (
        connection.execute(
            text(
                """
                select id, recipient_id, recipient_name, recipient_role, on_behalf_of_name,
                       requested_at, sent_at, step, same_person
                from approval_request where case_id = :id order by id desc limit 1
                """
            ),
            {"id": case_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return Request(
        request_id=row["id"],
        recipient_id=row["recipient_id"],
        recipient_name=row["recipient_name"],
        recipient_role=row["recipient_role"],
        on_behalf_of_name=row["on_behalf_of_name"],
        requested_at=row["requested_at"],
        sent_at=row["sent_at"],
        step=row["step"],
        same_person=row["same_person"],
    )


def decision_of(connection: Connection, request_id: int) -> Decision | None:
    row = (
        connection.execute(
            text(
                """
                select id, request_id, decision, comment, decided_by, decided_by_name,
                       decided_as, on_behalf_of_name, decided_at, same_person
                from approval_decision where request_id = :id
                """
            ),
            {"id": request_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return Decision(
        decision_id=row["id"],
        request_id=row["request_id"],
        decision=row["decision"],
        comment=row["comment"],
        decided_by=row["decided_by"],
        decided_by_name=row["decided_by_name"],
        decided_as=row["decided_as"],
        on_behalf_of_name=row["on_behalf_of_name"],
        decided_at=row["decided_at"],
        same_person=row["same_person"],
    )


def first_approval_of(connection: Connection, request_id: int) -> FirstApproval | None:
    """The first approval a second request follows, or None for a first request (#39)."""
    row = connection.execute(
        text(
            "select d.decided_by_name, d.decided_as, d.on_behalf_of_name, d.decided_at"
            " from approval_request q join approval_decision d on d.id = q.after_decision_id"
            " where q.id = :id"
        ),
        {"id": request_id},
    ).one_or_none()
    if row is None:
        return None
    return FirstApproval(row.decided_by_name, row.decided_as, row.on_behalf_of_name, row.decided_at)


def amount_check(connection: Connection, case_id: int) -> AmountCheck | None:
    """The newest first approval's amount against EUR 5,000, or None while it has none (#39)."""
    row = connection.execute(
        text(
            "select m.currency, m.gross_minor, m.rate, m.rate_time, m.eur_minor, m.above_line,"
            " m.source from approval_amount m where m.case_id = :id"
            " order by m.decision_id desc limit 1"
        ),
        {"id": case_id},
    ).one_or_none()
    if row is None:
        return None
    return AmountCheck(
        currency=row.currency,
        gross_cents=row.gross_minor,
        rate=format(row.rate.normalize(), "f"),
        rate_time=row.rate_time,
        eur_cents=row.eur_minor,
        above_line=row.above_line,
        from_wise=row.source == "wise",
    )


def _second_approver_name(connection: Connection) -> str | None:
    name: str | None = connection.execute(
        text("select a.name from second_approver s join approver a on a.object_id = s.approver_id")
    ).scalar_one_or_none()
    return name


def _next_step(
    connection: Connection,
    status: CaseStatus,
    request: Request,
    decision: "Decision | None",
    amount: AmountCheck | None,
) -> str | None:
    """What a first approval that did not settle the case waits for (#39), in a sentence."""
    if (
        status is not CaseStatus.AWAITING_APPROVAL
        or request.step != "first"
        or decision is None
        or decision.decision != "approved"
    ):
        return None
    if amount is None:
        return (
            "ARGUS reads the Wise rate at the time of the approval to compare the invoice with"
            " EUR 5,000. The invoice waits until then."
        )
    if amount.above_line:
        who = _second_approver_name(connection) or "the second approver"
        return f"The invoice is above EUR 5,000, so it waits for the second approval by {who}."
    return None


def is_asked(request: Request | None, object_id: str) -> bool:
    """Whether this person is the one the newest request went to (ADR 0010)."""
    return request is not None and request.recipient_id.lower() == object_id.lower()


def _proposal(connection: Connection, case_id: int, request: Request) -> Proposal | None:
    found = current_proposal(connection, case_id)
    if found is None:
        return None
    by_person = found.source is ProposalSource.PERSON
    changed = (
        by_person and found.proposed_at is not None and found.proposed_at > request.requested_at
    )
    if by_person:
        verb = "changed" if changed else "entered"
        origin = f"{verb} by {found.proposed_by_name}: {found.reason}"
    else:
        day = found.from_posting_date.strftime("%d.%m.%Y") if found.from_posting_date else "?"
        origin = f"from the booking of {found.from_document_number} on {day}"
    return Proposal(
        proposal_id=found.id,
        gl_account=found.gl_account,
        tax_key=found.tax_key,
        chart=found.chart.value,
        source=found.source.value,
        origin=origin,
        changed_at_approval=changed,
        by_name=found.proposed_by_name,
        reason=found.reason,
        proposed_at=found.proposed_at,
    )


def invoice_file(connection: Connection, case_id: int) -> tuple[InvoiceFile | None, bool]:
    """The stored file the invoice stands on, and whether it was in the email text instead."""
    reading = connection.execute(
        text(
            "select document_id, pages from invoice_reading"
            " where case_id = :id and role = 'invoice' order by id limit 1"
        ),
        {"id": case_id},
    ).one_or_none()
    pages: list[int] = list(reading.pages or []) if reading is not None else []
    if reading is not None and reading.document_id is None:
        return None, True
    row = connection.execute(
        text(
            """
            select d.id, d.content_type, d.filename
            from stored_document d
            where d.kind = 'attachment'
              and (d.id = :document_id
                   or (cast(:document_id as bigint) is null and d.id in (
                         select document_id from case_document where case_id = :case_id)))
            order by d.id limit 1
            """
        ),
        {"document_id": reading.document_id if reading is not None else None, "case_id": case_id},
    ).one_or_none()
    if row is None:
        return None, False
    content_type = (row.content_type or "").lower().split(";")[0].strip()
    return (
        InvoiceFile(
            document_id=row.id,
            content_type=content_type,
            filename=row.filename,
            pages=pages,
            viewable=content_type in VIEWABLE,
        ),
        False,
    )


def file_of_case(connection: Connection, case_id: int, document_id: int) -> tuple[str, str] | None:
    """The fingerprint and content type of the invoice file of this case, or None when this
    document is not the case's invoice file. Only that one file is served (NF-03)."""
    found, _ = invoice_file(connection, case_id)
    if found is None or found.document_id != document_id:
        return None
    sha: str = connection.execute(
        text("select sha256 from stored_document where id = :id"), {"id": document_id}
    ).scalar_one()
    return sha, found.content_type


_CASE = """
    select c.id, c.kind, c.status, c.closed_reason, c.partner_account, c.document_number,
           c.gross_minor, c.currency, r.reading -> 'fields' as fields
    from invoice_case c
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    where c.id = :id and c.kind in ('incoming_invoice', 'incoming_credit_note')
"""


def approval_view(connection: Connection, case_id: int) -> ApprovalView | None:
    """The approval page of one case, or None when the case never went for approval."""
    row = connection.execute(text(_CASE), {"id": case_id}).mappings().one_or_none()
    if row is None:
        return None
    request = newest_request(connection, case_id)
    if request is None:
        return None
    fields: dict[str, Any] = row["fields"] or {}
    found, in_text = invoice_file(connection, case_id)
    status = CaseStatus(row["status"])
    decision = decision_of(connection, request.request_id)
    amount = amount_check(connection, case_id)
    return ApprovalView(
        case_id=row["id"],
        kind=CaseKind(row["kind"]),
        status=status,
        status_text=STATUS_TEXT[status],
        closed_reason=row["closed_reason"],
        vendor_name=_value(fields, "issuer_name"),
        vendor_account=row["partner_account"],
        document_number=row["document_number"],
        invoice_date=_value(fields, "invoice_date"),
        due_date=_value(fields, "due_date"),
        net_cents=_cents(_value(fields, "net_amount")),
        vat_cents=_cents(_value(fields, "vat_amount")),
        gross_cents=row["gross_minor"],
        currency=row["currency"],
        request=request,
        proposal=_proposal(connection, case_id, request),
        decision=decision,
        file=found,
        in_email_text=in_text,
        first_approval=first_approval_of(connection, request.request_id),
        amount=amount,
        next_step=_next_step(connection, status, request, decision, amount),
    )


def waiting_rows(connection: Connection, only_for: str | None) -> list[ApprovalRow]:
    """The invoices that wait for a decision, oldest request first. ``only_for`` limits them to
    those whose newest request went to this person (an approver); None lists all of them
    (finance and admin). Filtered in the database, so another approver's never leave it."""
    rows = connection.execute(
        text(
            """
            select c.id, c.kind, c.partner_account, c.document_number, c.gross_minor,
                   c.currency, q.recipient_id, q.recipient_name, q.recipient_role,
                   q.on_behalf_of_name, q.requested_at, q.sent_at, q.step,
                   r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name,
                   r.reading -> 'fields' -> 'due_date' ->> 'value' as due_date
            from invoice_case c
            join lateral (
              select * from approval_request where case_id = c.id order by id desc limit 1
            ) q on true
            left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
            where c.status = 'awaiting_approval'
              and c.kind in ('incoming_invoice', 'incoming_credit_note')
              and (cast(:me as text) is null or lower(q.recipient_id) = lower(cast(:me as text)))
            order by q.requested_at, c.id
            limit :limit
            """
        ),
        {"me": only_for, "limit": MAX_ROWS},
    ).mappings()
    return [
        ApprovalRow(
            case_id=r["id"],
            kind=CaseKind(r["kind"]),
            vendor_name=r["vendor_name"],
            vendor_account=r["partner_account"],
            document_number=r["document_number"],
            gross_cents=r["gross_minor"],
            currency=r["currency"],
            due_date=r["due_date"],
            recipient_id=r["recipient_id"],
            recipient_name=r["recipient_name"],
            recipient_role=r["recipient_role"],
            on_behalf_of_name=r["on_behalf_of_name"],
            requested_at=r["requested_at"],
            sent_at=r["sent_at"],
            step=r["step"],
        )
        for r in rows
    ]

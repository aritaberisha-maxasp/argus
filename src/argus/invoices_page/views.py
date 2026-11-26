"""What the invoice status page shows (#62, NF-05, NF-03, EX-01).

One row per case: its status, who it is from or for, the amount and the dates that matter. The
list can be narrowed by status, kind, a search word and the day it came in. These functions only
read; nothing here changes a case.

Where each column comes from:

- **Vendor or customer**: the name ARGUS read on the invoice (``invoice_reading``, the issuer for
  incoming invoices, the recipient for outgoing ones). The DATEV account number once the case is
  matched, and the checked From domain of the email, are shown next to it, so a case whose name
  could not be read still says something.
- **Amount**: the gross amount of the case. A bank transaction case has none, so it shows the
  amount of the transaction.
- **Dates**: when the email came in (the case's creation time when there is no email), the
  invoice and due date as read, and when the case last changed.
- **Open exception**: the oldest open exception of the case, so the page can link to it in the
  exception queue.
- **G/L account and tax key**: the current proposal of an incoming invoice or credit note
  (#37, ``account_proposal``), for the vendor account the case has now.
- **Approval**: who the newest approval request went to and when it was emailed (#191,
  ``approval_request``), while the case waits for approval. Once the person decided (#38,
  ``approval_decision``): approved or rejected, by whom and when.

Nothing here logs. Names are personal data and stay out of the log (NF-03).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind, CaseStatus, ExceptionReason
from argus.queue_page.views import REASON_TEXT

# The most rows one answer holds. The page says when there are more, so nobody thinks the list
# is complete when it is not; the filters narrow it down.
MAX_ROWS = 500
MAX_SEARCH = 100

# Kinds where the other party is the customer: the name is the recipient on the invoice.
OUTGOING = frozenset({CaseKind.OUTGOING_INVOICE, CaseKind.OUTGOING_CREDIT_NOTE})

# Plain words for each status, for Benjamin. A test checks every status has one.
STATUS_TEXT: dict[CaseStatus, str] = {
    CaseStatus.RECEIVED: "Received",
    CaseStatus.AWAITING_APPROVAL: "Awaiting approval",
    CaseStatus.APPROVED: "Approved",
    CaseStatus.BOOKED: "Booked",
    CaseStatus.IN_PAYMENT_RUN: "In payment run",
    CaseStatus.PAID: "Paid",
    CaseStatus.EXCEPTION: "Exception",
    CaseStatus.CLOSED: "Closed",
}


@dataclass(frozen=True)
class CaseFilter:
    """What the person narrowed the list to. Every part is optional."""

    status: CaseStatus | None = None
    kind: CaseKind | None = None
    search: str | None = None  # part of the name, number, account, sender domain or case ID
    received_from: date | None = None  # Berlin days, both ends included
    received_to: date | None = None


@dataclass(frozen=True)
class CaseRow:
    case_id: int
    kind: CaseKind
    status: CaseStatus
    status_text: str
    resume_status: CaseStatus | None  # where the case goes back to after its exception
    closed_reason: str | None
    partner_name: str | None  # vendor or customer as read on the document
    partner_account: str | None  # the DATEV Kreditor or Debitor number once matched
    sender: str | None  # the checked From domain of the email the case came from
    document_number: str | None
    gross_cents: int | None
    currency: str | None
    received_at: datetime  # the email's arrival, or the case's creation without an email
    invoice_date: str | None  # as read on the document
    due_date: str | None
    updated_at: datetime
    exception_id: int | None  # the oldest open exception of the case
    exception_reason: ExceptionReason | None
    exception_text: str | None
    gl_account: str | None = None  # the proposed G/L account (#37)
    tax_key: str | None = None  # its tax key; blank when the history has none
    # The newest approval request (#191): who it went to, and when it was emailed (None while
    # the email has not gone out yet).
    approval_to: str | None = None
    approval_sent_at: datetime | None = None
    # The decision on the newest request (#38): approved or rejected, by whom, when.
    decision: str | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class CaseList:
    rows: list[CaseRow]
    counts: dict[CaseStatus, int]  # per status, for every other filter but the status
    total: int  # how many cases match, also when the rows stop at MAX_ROWS
    truncated: bool


_BASE = """
    from invoice_case c
    left join bank_transaction b on b.case_id = c.id
    left join mailbox_email m on m.id = c.source_email_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    cross join lateral (
      select coalesce(m.received_at, c.created_at) as received_at,
             case when c.kind in ('outgoing_invoice', 'outgoing_credit_note')
                  then r.reading -> 'fields' -> 'recipient_name' ->> 'value'
                  else r.reading -> 'fields' -> 'issuer_name' ->> 'value' end as partner_name,
             m.classification -> 'sender' ->> 'from_domain' as sender
    ) d
"""


_LIST_HEAD = """
    select c.id, c.kind, c.status, c.resume_status, c.closed_reason, c.partner_account,
           c.document_number, coalesce(c.gross_minor, b.amount_minor) as gross_minor,
           coalesce(c.currency, b.wise_balance_currency) as currency,
           c.updated_at, d.received_at, d.partner_name, d.sender,
           r.reading -> 'fields' -> 'invoice_date' -> 'value' as invoice_date,
           r.reading -> 'fields' -> 'due_date' -> 'value' as due_date,
           x.id as exception_id, x.reason as exception_reason,
           a.gl_account, a.tax_key, q.recipient_name as approval_to,
           q.sent_at as approval_sent_at, k.decision, k.decided_by_name, k.decided_at
    """
_OPEN_EXCEPTION = """
    left join lateral (
      select e.id, e.reason from exception_item e
      where e.case_id = c.id and e.resolved_at is null
      order by e.opened_at, e.id limit 1
    ) x on true
    left join lateral (
      select p.gl_account, p.tax_key from account_proposal p
      where p.case_id = c.id and p.vendor_account = c.partner_account
      order by p.id desc limit 1
    ) a on true
    left join lateral (
      select q.id, q.recipient_name, q.sent_at from approval_request q
      where q.case_id = c.id
      order by q.id desc limit 1
    ) q on true
    left join approval_decision k on k.request_id = q.id
    """
_LIST_TAIL = " order by d.received_at desc, c.id desc limit :limit"
_COUNT_HEAD = "select c.status, count(*) as n "


def _like(word: str) -> str:
    """A search word as a LIKE pattern that matches it literally anywhere."""
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(f: CaseFilter, *, with_status: bool) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    if with_status and f.status is not None:
        parts.append("c.status = cast(:status as case_status)")
        params["status"] = f.status.value
    if f.kind is not None:
        parts.append("c.kind = cast(:kind as case_kind)")
        params["kind"] = f.kind.value
    if f.received_from is not None:
        parts.append("(d.received_at at time zone 'Europe/Berlin')::date >= :received_from")
        params["received_from"] = f.received_from
    if f.received_to is not None:
        parts.append("(d.received_at at time zone 'Europe/Berlin')::date <= :received_to")
        params["received_to"] = f.received_to
    word = (f.search or "").strip()[:MAX_SEARCH]
    if word:
        parts.append(
            """(d.partner_name ilike :search or c.document_number ilike :search
                or c.partner_account ilike :search or d.sender ilike :search
                or c.id::text = :search_exact)"""
        )
        params["search"] = _like(word)
        params["search_exact"] = word.lstrip("#")
    return (" where " + " and ".join(parts) if parts else ""), params


def _text(value: Any) -> str | None:
    """A field value as read: strings stay, anything else becomes its plain text."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def case_rows(connection: Connection, f: CaseFilter | None = None) -> CaseList:
    """Every case that matches, the newest arrival first (NF-05)."""
    f = f or CaseFilter()
    where, params = _where(f, with_status=True)
    # The SQL text is fixed: only the where clause comes from _where, and it holds bound
    # parameters, never a value.
    rows = connection.execute(
        text(_LIST_HEAD + _BASE + _OPEN_EXCEPTION + where + _LIST_TAIL),
        {**params, "limit": MAX_ROWS + 1},
    ).mappings()
    found = [_row(r) for r in rows]

    where_all, params_all = _where(f, with_status=False)
    counts = {
        CaseStatus(r["status"]): int(r["n"])
        for r in connection.execute(
            text(_COUNT_HEAD + _BASE + where_all + " group by c.status"),
            params_all,
        ).mappings()
    }
    total = counts.get(f.status, 0) if f.status is not None else sum(counts.values())
    return CaseList(
        rows=found[:MAX_ROWS], counts=counts, total=total, truncated=len(found) > MAX_ROWS
    )


def _row(r: Any) -> CaseRow:
    status = CaseStatus(r["status"])
    reason = ExceptionReason(r["exception_reason"]) if r["exception_reason"] else None
    return CaseRow(
        case_id=r["id"],
        kind=CaseKind(r["kind"]),
        status=status,
        status_text=STATUS_TEXT[status],
        resume_status=CaseStatus(r["resume_status"]) if r["resume_status"] else None,
        closed_reason=r["closed_reason"],
        partner_name=_text(r["partner_name"]),
        partner_account=r["partner_account"],
        sender=r["sender"],
        document_number=r["document_number"],
        gross_cents=r["gross_minor"],
        currency=r["currency"].strip() if r["currency"] else None,
        received_at=r["received_at"],
        invoice_date=_text(r["invoice_date"]),
        due_date=_text(r["due_date"]),
        updated_at=r["updated_at"],
        exception_id=r["exception_id"],
        exception_reason=reason,
        exception_text=REASON_TEXT[reason] if reason else None,
        gl_account=r["gl_account"],
        tax_key=r["tax_key"],
        approval_to=r["approval_to"],
        approval_sent_at=r["approval_sent_at"],
        decision=r["decision"],
        decided_by_name=r["decided_by_name"],
        decided_at=r["decided_at"],
    )


def case_row(connection: Connection, case_id: int) -> CaseRow | None:
    """One case as the list shows it, or None when there is no such case."""
    row = (
        connection.execute(
            text(_LIST_HEAD + _BASE + _OPEN_EXCEPTION + " where c.id = :id"), {"id": case_id}
        )
        .mappings()
        .first()
    )
    return _row(row) if row else None

"""Send each checked invoice to its approver (#191, AP-02, AP-06, ADR 0010).

Once an incoming invoice or credit note has passed every check, ARGUS moves it from Received to
Awaiting approval and emails the vendor's approver a link to the approval page (#38). Every
check means:

- it is matched to a vendor account (#35), and that vendor has a row in the approver table (#34);
- it has a duplicate key, so the duplicate check ran (IN-10);
- it has a current account proposal for that vendor account (#37, PR-04);
- it has no open exception, for example an IBAN hold (#36, PR-05) or ``no_account_proposal``.

The database holds the same rules (migration 0020), so no other code path can skip them. It also
picks who is asked: the approver, or the deputy while the approver is marked away (AP-06). It
keeps that person on an approval request record that never changes, and writes
``approval_requested`` into the audit log. #38 decides who may approve from that record.

When the approver is away and the vendor has no deputy, nobody is asked: the case goes to the
exception queue as ``approver_absent``. Every pass looks at those again and carries on by itself
once the approver is back or the vendor has a deputy.

The email goes out after the move, outside any unit of work (note from PR 79 on #41). It is sent
with a Graph client that may send approval requests and nothing else (#168), and that refuses
any other recipient or text than the stored request. A request is marked sent, with
``approval_request_sent`` in the audit log, only after Graph accepted it. If sending fails, the
next sweep tries again. A request marked sent is never sent again.

The pass runs at the end of every live sweep, after the account proposal. In shadow mode it
moves and sends nothing: it reads the mode at the start and again before each email
(``docs/design/shadow-mode.md``). Logs name only case and request IDs (NF-03).
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason, Outcome
from argus.cases.store import get_case, move_case, open_exception, resolve_exception, unit_of_work
from argus.intake.graph import ApprovalMail, GraphApiError, GraphCallRefused
from argus.intake.mode import is_live

log = logging.getLogger("argus.intake.approvals")

APPROVAL_ACTOR = Actor.system("approval-request")
# Where the approval page of a case lives (#38): <ARGUS address>/approvals/<case ID>.
APPROVAL_PATH = "/approvals"


class ApprovalMailbox(Protocol):
    """The one Graph call the pass needs. :class:`argus.intake.graph.GraphClient` built with
    only ``SEND_APPROVAL_REQUEST`` is the real one; tests wrap it to make it fail."""

    def send_approval_request(self, mail: ApprovalMail) -> None: ...


@dataclass(frozen=True)
class Approvals:
    """What the pass needs: the approval client and the address of the ARGUS app."""

    mailbox: ApprovalMailbox
    app_url: str

    def link(self, case_id: int) -> str:
        return f"{self.app_url.rstrip('/')}{APPROVAL_PATH}/{case_id}"


@dataclass
class ApprovalPass:
    """What one pass did. ``failed`` counts cases and emails that hit an error; the next pass
    retries them."""

    requested: int = 0  # cases moved to Awaiting approval
    absent: int = 0  # cases sent to the queue because nobody can be asked
    resumed: int = 0  # absent approver exceptions ARGUS resolved itself
    sent: int = 0
    failed: int = 0
    not_sent: list[int] = field(default_factory=list)  # request IDs still waiting to be sent


@dataclass(frozen=True)
class _Asked:
    """Who the database would ask for a vendor right now."""

    approver_name: str
    available: bool  # the approver is in, or a deputy stands in


def _who(connection: Connection, vendor_account: str) -> _Asked | None:
    row = connection.execute(
        text(
            "select a.name, (not a.absent or v.deputy_id is not null) as available"
            " from vendor_approver v join approver a on a.object_id = v.approver_id"
            " where v.vendor_account = :account"
        ),
        {"account": vendor_account},
    ).first()
    return _Asked(row.name, row.available) if row else None


def _only(cases: Collection[int] | None) -> list[int] | None:
    return list(cases) if cases is not None else None


def request_approvals(
    engine: Engine, approvals: Approvals, *, cases: Collection[int] | None = None
) -> ApprovalPass:
    """One pass: carry on with absent approver exceptions that can go on, move every fully
    checked case to Awaiting approval, then send every request not sent yet. ``cases`` limits
    the pass to these case IDs (tests share one database). Each case and each email is its own
    step, so one failure leaves the others alone."""
    result = ApprovalPass()
    if not is_live(engine):
        log.info("Approval requests wait, ARGUS runs in shadow mode")
        return result
    only = _only(cases)
    _look_again(engine, only, result)
    _move_checked(engine, only, result)
    _send_unsent(engine, approvals, only, result)
    return result


# ------------------------------------------------------------------------- absent approvers


def _look_again(engine: Engine, only: list[int] | None, result: ApprovalPass) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select e.id, e.case_id from exception_item e"
                " where e.resolved_at is null and e.reason::text = 'approver_absent'"
                " and (cast(:only as bigint[]) is null or e.case_id = any(cast(:only as bigint[])))"
                " order by e.id"
            ),
            {"only": only},
        ).all()
    for row in rows:
        exception_id: int = row.id
        case_id: int = row.case_id
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                still_open = db.execute(
                    text(
                        "select resolved_at is null from exception_item where id = :id for update"
                    ),
                    {"id": exception_id},
                ).scalar()
                account = get_case(db, case_id).partner_account
                asked = _who(db, account) if account else None
                if not still_open or asked is None or not asked.available:
                    continue
                resolve_exception(
                    db,
                    exception_id,
                    Outcome.RETRIED,
                    resolution=(
                        f"Someone can approve the invoices of vendor account {account} again. "
                        "ARGUS carried on and sends the invoice for approval."
                    ),
                )
                result.resumed += 1
        except Exception:
            log.exception("Absent approver check failed on case %s, next sweep retries", case_id)
            result.failed += 1


# ------------------------------------------------------------------------------ the move

_CHECKED = """
    select c.id from invoice_case c
    where c.kind in ('incoming_invoice', 'incoming_credit_note')
      and c.status = 'received' and c.partner_account is not null
      and c.duplicate_key is not null
      and exists (select 1 from vendor_approver v where v.vendor_account = c.partner_account)
      and exists (select 1 from account_proposal p
                  where p.case_id = c.id and p.vendor_account = c.partner_account)
      and not exists (select 1 from exception_item e
                      where e.case_id = c.id and e.resolved_at is null)
      and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
    order by c.id
"""


def _move_checked(engine: Engine, only: list[int] | None, result: ApprovalPass) -> None:
    with engine.connect() as connection:
        work = [row.id for row in connection.execute(text(_CHECKED), {"only": only})]
    for case_id in work:
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                _move_one(db, case_id, result)
        except Exception:
            log.exception("Approval request failed on case %s, next sweep retries", case_id)
            result.failed += 1


def _move_one(db: Connection, case_id: int, result: ApprovalPass) -> None:
    db.execute(text("select id from invoice_case where id = :id for update"), {"id": case_id})
    case = get_case(db, case_id)
    if case.status is not CaseStatus.RECEIVED or case.partner_account is None:
        return  # moved on since the work was listed
    asked = _who(db, case.partner_account)
    if asked is None:
        return  # the approver row was removed a moment ago; vendor matching queues it
    if not asked.available:
        open_exception(
            db,
            ExceptionReason.APPROVER_ABSENT,
            (
                f"The approver of vendor account {case.partner_account}, {asked.approver_name}, "
                "is marked away and the vendor has no deputy, so ARGUS cannot ask anyone to "
                "approve this invoice. Mark the approver back, or give the vendor a deputy, in "
                "the approver table. ARGUS carries on by itself then."
            ),
            case_id=case_id,
            facts={"account": case.partner_account},
        )
        result.absent += 1
        return
    db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
    move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
    result.requested += 1


# ----------------------------------------------------------------------------- the email

_UNSENT = """
    select q.id as request_id, q.case_id, q.vendor_account, q.recipient_address,
           q.recipient_name, q.on_behalf_of_name, c.kind, c.document_number, c.gross_minor,
           c.currency, r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name
    from approval_request q
    join invoice_case c on c.id = q.case_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    where q.sent_at is null and c.status = 'awaiting_approval'
      and (cast(:only as bigint[]) is null or q.case_id = any(cast(:only as bigint[])))
    order by q.id
"""


def mail_for(row: Any, approvals: Approvals) -> ApprovalMail:
    """The email of one stored request. The vendor name is the one read on the invoice, or the
    vendor account when none was read."""
    vendor = row["vendor_name"]
    return ApprovalMail(
        request_id=row["request_id"],
        case_id=row["case_id"],
        recipient_address=row["recipient_address"],
        recipient_name=row["recipient_name"],
        on_behalf_of_name=row["on_behalf_of_name"],
        vendor_name=vendor
        if isinstance(vendor, str) and vendor.strip()
        else f"vendor account {row['vendor_account']}",
        document_number=row["document_number"] or "without a number",
        gross_cents=row["gross_minor"],
        currency=(row["currency"] or "").strip(),
        credit_note=CaseKind(row["kind"]) is CaseKind.INCOMING_CREDIT_NOTE,
        link=approvals.link(row["case_id"]),
    )


def _send_unsent(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    with engine.connect() as connection:
        rows = list(connection.execute(text(_UNSENT), {"only": only}).mappings())
    for row in rows:
        request_id = row["request_id"]
        if row["gross_minor"] is None or not row["currency"]:
            # The database lets no case go for approval without them; never send a blank.
            log.error("Approval request %s has no amount, not sent", request_id)
            result.failed += 1
            result.not_sent.append(request_id)
            continue
        if not is_live(engine):
            log.info("Approval requests stop, ARGUS was switched to shadow mode")
            result.not_sent += [r["request_id"] for r in rows if r["request_id"] >= request_id]
            return
        try:
            approvals.mailbox.send_approval_request(mail_for(row, approvals))
        except (GraphApiError, GraphCallRefused, httpx.HTTPError):
            log.exception("Approval request %s not sent, next sweep retries", request_id)
            result.failed += 1
            result.not_sent.append(request_id)
            continue
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                db.execute(
                    text(
                        "update approval_request set sent_at = clock_timestamp()"
                        " where id = :id and sent_at is null"
                    ),
                    {"id": request_id},
                )
        except Exception:
            # Sent but not marked: the next sweep sends it again (ADR 0010, harmless).
            log.exception("Approval request %s sent but not marked, may go out twice", request_id)
            result.failed += 1
            continue
        result.sent += 1

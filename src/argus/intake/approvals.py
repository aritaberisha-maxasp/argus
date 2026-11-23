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

When the approver is away and the vendor has no deputy, or the deputy is away too, nobody is
asked: the case goes to the
exception queue as ``approver_absent``. Every pass looks at those again and carries on by itself
once the approver is back or the vendor has a deputy.

When the person who would be asked has an address outside Max ASP's own domain
(``ARGUS_OWN_DOMAIN``), nothing is written and nothing is sent: the case goes to the exception
queue as ``approver_address_outside`` and carries on by itself once the approver table holds a
Max ASP address (compliance review of PR 200).

The email goes out after the move, outside any unit of work (note from PR 79 on #41). It is sent
with a Graph client that may send approval requests and nothing else (#168). Right before it
sends, that client reads the request again from the database (:func:`stored_requests`) and
refuses unless it exists, is not sent yet, its case waits for approval, and the email goes to
the person on it with exactly the text built from it. A request is marked sent, with
``approval_request_sent`` in the audit log, only after Graph accepted it. If sending fails, the
next sweep tries again. A request marked sent is never sent again.

The pass runs at the end of every live sweep, after the account proposal. In shadow mode it
moves and sends nothing: it reads the mode at the start and again before each email
(``docs/design/shadow-mode.md``). Logs name only case and request IDs (NF-03).
"""

import logging
from collections.abc import Callable, Collection
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
    """What the pass needs: the approval client, the address of the ARGUS app and Max ASP's
    own email domain (``ARGUS_OWN_DOMAIN``), the only one approval requests go to."""

    mailbox: ApprovalMailbox
    app_url: str
    own_domain: str

    def inside(self, address: str) -> bool:
        return is_own_address(address, self.own_domain)

    def link(self, case_id: int) -> str:
        return approval_link(self.app_url, case_id)


def is_own_address(address: str, own_domain: str) -> bool:
    """Whether an email address is at Max ASP's own domain, ignoring case."""
    return address.rpartition("@")[2].lower() == own_domain.strip().lower()


def approval_link(app_url: str, case_id: int) -> str:
    """Where the approval page of a case lives (#38)."""
    return f"{app_url.rstrip('/')}{APPROVAL_PATH}/{case_id}"


@dataclass
class ApprovalPass:
    """What one pass did. ``failed`` counts cases and emails that hit an error; the next pass
    retries them."""

    requested: int = 0  # cases moved to Awaiting approval
    absent: int = 0  # cases sent to the queue because nobody can be asked
    outside: int = 0  # cases sent to the queue because the address is not a Max ASP one
    resumed: int = 0  # absent approver exceptions ARGUS resolved itself
    sent: int = 0
    failed: int = 0
    not_sent: list[int] = field(default_factory=list)  # request IDs still waiting to be sent


@dataclass(frozen=True)
class _Asked:
    """Who the database would ask for a vendor right now."""

    approver_name: str
    available: bool  # the approver is in, or a deputy who is in stands in
    name: str | None  # who would get the email, the same way migration 0020 picks them
    address: str | None


def _who(connection: Connection, vendor_account: str) -> _Asked | None:
    row = connection.execute(
        text(
            "select a.name as approver_name,"
            " (not a.absent or (d.object_id is not null and not d.absent)) as available,"
            " case when not a.absent then a.name when d.object_id is not null and not d.absent"
            " then d.name end as name,"
            " case when not a.absent then a.email when d.object_id is not null and not d.absent"
            " then d.email end as address"
            " from vendor_approver v join approver a on a.object_id = v.approver_id"
            " left join approver d on d.object_id = v.deputy_id"
            " where v.vendor_account = :account"
        ),
        {"account": vendor_account},
    ).first()
    return _Asked(row.approver_name, row.available, row.name, row.address) if row else None


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
    _look_again(engine, approvals, only, result)
    _move_checked(engine, approvals, only, result)
    _send_unsent(engine, approvals, only, result)
    return result


# ------------------------------------------------------------------------- absent approvers


def _look_again(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select e.id, e.case_id, e.reason::text as reason from exception_item e"
                " where e.resolved_at is null"
                " and e.reason::text in ('approver_absent', 'approver_address_outside')"
                " and (cast(:only as bigint[]) is null or e.case_id = any(cast(:only as bigint[])))"
                " order by e.id"
            ),
            {"only": only},
        ).all()
    for row in rows:
        exception_id: int = row.id
        case_id: int = row.case_id
        outside = row.reason == ExceptionReason.APPROVER_ADDRESS_OUTSIDE
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
                if outside and not (asked.address and approvals.inside(asked.address)):
                    continue
                # An absent approver who is back with an outside address goes on to Received;
                # the move then opens approver_address_outside instead.
                resolution = (
                    f"The approver table holds a Max ASP address for vendor account {account} "
                    "now. ARGUS carried on and sends the invoice for approval."
                    if outside
                    else f"Someone can approve the invoices of vendor account {account} again. "
                    "ARGUS carried on and sends the invoice for approval."
                )
                resolve_exception(db, exception_id, Outcome.RETRIED, resolution=resolution)
                result.resumed += 1
        except Exception:
            log.exception("Approver check failed on case %s, next sweep retries", case_id)
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


def _move_checked(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    with engine.connect() as connection:
        work = [row.id for row in connection.execute(text(_CHECKED), {"only": only})]
    for case_id in work:
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                _move_one(db, approvals, case_id, result)
        except Exception:
            log.exception("Approval request failed on case %s, next sweep retries", case_id)
            result.failed += 1


def _move_one(db: Connection, approvals: Approvals, case_id: int, result: ApprovalPass) -> None:
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
                "is marked away and the vendor has no deputy who is in, so ARGUS cannot ask "
                "anyone to approve this invoice. Mark the approver or the deputy back, or give "
                "the vendor another approver or deputy, in the approver table. ARGUS carries on "
                "by itself then. If the vendor has no approver row any more, add one, or reject "
                "the invoice."
            ),
            case_id=case_id,
            facts={"account": case.partner_account},
        )
        result.absent += 1
        return
    if not (asked.address and approvals.inside(asked.address)):
        # Compliance review of PR 200: a typo or a wrong entry in the approver table must not
        # send invoice details from the accounts mailbox outside Max ASP, nor leave the
        # invoice waiting for an email that never goes out.
        open_exception(
            db,
            ExceptionReason.APPROVER_ADDRESS_OUTSIDE,
            (
                f"{asked.name}, who approves the invoices of vendor account "
                f"{case.partner_account}, has the email address {asked.address} in the "
                f"approver table, which is not at {approvals.own_domain}. ARGUS sends approval "
                "requests only to Max ASP addresses. Correct the address in the approver "
                "table, and ARGUS carries on by itself. Or reject the invoice."
            ),
            case_id=case_id,
            facts={"account": case.partner_account},
        )
        result.outside += 1
        return
    stored: str = db.execute(
        text("insert into approval_request (case_id) values (:id) returning recipient_address"),
        {"id": case_id},
    ).scalar_one()
    if not approvals.inside(stored):
        # The approver table changed between the two reads; the next pass queues it.
        raise RuntimeError(f"the person on approval request for case {case_id} changed")
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


def mail_for(row: Any, app_url: str) -> ApprovalMail:
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
        link=approval_link(app_url, row["case_id"]),
    )


_STORED = """
    select q.id as request_id, q.case_id, q.vendor_account, q.recipient_address,
           q.recipient_name, q.on_behalf_of_name, c.kind, c.document_number, c.gross_minor,
           c.currency, r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name
    from approval_request q
    join invoice_case c on c.id = q.case_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    where q.id = :id and q.sent_at is null and c.status = 'awaiting_approval'
      and c.gross_minor is not null and c.currency is not null
"""


def stored_requests(engine: Engine, app_url: str) -> Callable[[int], ApprovalMail | None]:
    """What the approval client checks each email against, right before it sends (code review
    of PR 200): the email of the stored request with this ID, read fresh from the database, or
    None when there is no such request, it was sent already, or its case no longer waits for
    approval. So whatever the caller hands the client, only a stored, unsent request goes out,
    and only to the person on it."""

    def lookup(request_id: int) -> ApprovalMail | None:
        with engine.connect() as connection:
            row = connection.execute(text(_STORED), {"id": request_id}).mappings().first()
        return mail_for(row, app_url) if row is not None else None

    return lookup


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
            approvals.mailbox.send_approval_request(mail_for(row, approvals.app_url))
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

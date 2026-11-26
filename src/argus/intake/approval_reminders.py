"""Remind the approver after 3 working days and tell finance after 5 (#40, AP-05, AP-06).

Two steps of the approval pass (:func:`argus.intake.approvals.request_approvals`):

**A new request for a waiting invoice** (:func:`renew_waiting`). The newest request of a case in
Awaiting approval goes to someone who can no longer decide: the approver was marked away
(AP-06), the approver table gives the vendor another approver or deputy (what Benjamin does
after an escalation, Arita on #40, 23 Nov), or the case moved to another vendor account
(follow up from the PR 205 review). The next pass writes a new request; the database picks who
is asked, as for the first one, and the approval pass emails it. The old request can no longer
be decided (only the newest one can, migration 0021), and the clock starts again. When nobody
can be asked (the approver is away and there is no deputy who is in), or the person has an
address outside Max ASP, or the new vendor has no approver, the case goes to the exception
queue with the same reasons as before. When an absent approver or approver address exception
of a waiting case is resolved by ARGUS, the case gets a new request in the same step, so
whoever is asked now gets a fresh email and a fresh clock (:func:`renew_after_exception`).

**Reminders and escalations** (:func:`send_nudges`). The clock starts at
``coalesce(clock_from, sent_at)`` of the newest request (migration 0022 keeps the old clock
when the asker answered their own question). 3 working days later the person on the request
gets a reminder, 5 working days later the escalation address (``ARGUS_DIGEST_TO``, Benjamin)
gets an email with the link. Working days are counted with
:func:`argus.working_days.add_working_days`, which does not count the start day: a request
emailed on Monday gets its reminder on Thursday and its escalation on the following Monday; one
emailed on a weekend or a Baden-Württemberg holiday starts counting on the next working day.
The email goes out on the first sweep of the day it is due. Benjamin cannot decide on an
escalated case (Arita on #40, 23 Nov): only the person on the newest request can.

Never twice: a row is written and committed before the email goes out, and the database allows
one row per request and kind. After Graph accepted the email the row is marked sent; when Graph
refused it for certain (an error answer, or the client refused it before sending) the row is
marked failed and the next pass writes a new one. A row with neither (a crash or a timeout in
between) is never sent again and is logged as an error, so the job's failed line shows it.

Both steps run only in live mode, like the rest of the pass, and send only with the approval
client (#168). Logs name only case, request and reminder IDs (NF-03). Nothing here approves,
books or pays anything (NF-04).
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, text

from argus.cases.model import CaseKind, CaseStatus, ExceptionReason
from argus.cases.store import get_case, open_exception, unit_of_work
from argus.intake.approvals import (
    APPROVAL_ACTOR,
    ApprovalPass,
    Approvals,
    NudgeMailbox,
    approval_link,
)
from argus.intake.approvals import _who as who
from argus.intake.graph import (
    ESCALATION_DAYS,
    REMINDER_DAYS,
    GraphApiError,
    GraphCallRefused,
    NudgeMail,
)
from argus.intake.mode import is_live
from argus.working_days import add_working_days, berlin_date

log = logging.getLogger("argus.intake.approval_reminders")

REMINDER = "reminder"
ESCALATION = "escalation"
_DAYS = {REMINDER: REMINDER_DAYS, ESCALATION: ESCALATION_DAYS}


def due_dates(clock_from: datetime | date) -> tuple[date, date]:
    """The Berlin working days a reminder and an escalation are due for a clock that started
    at ``clock_from``."""
    return add_working_days(clock_from, REMINDER_DAYS), add_working_days(
        clock_from, ESCALATION_DAYS
    )


# ------------------------------------------------------------------- new requests (AP-06)

# The newest request of each waiting case whose person can no longer decide on it. The same
# test as approval_request_renewal of migration 0024.
_STALE = """
    select c.id as case_id, q.id as request_id
    from invoice_case c
    join lateral (select * from approval_request r where r.case_id = c.id
                  order by r.id desc limit 1) q on true
    left join vendor_approver va on va.vendor_account = c.partner_account
    left join approver ap on ap.object_id = va.approver_id
    left join approver dp on dp.object_id = va.deputy_id
    where c.status = 'awaiting_approval'
      and c.kind in ('incoming_invoice', 'incoming_credit_note')
      and not exists (select 1 from approval_decision d where d.request_id = q.id)
      and not (
        c.partner_account is not distinct from q.vendor_account
        and (coalesce(lower(q.recipient_id) = lower(ap.object_id) and not ap.absent, false)
             or coalesce(lower(q.recipient_id) = lower(dp.object_id) and not dp.absent, false)))
      and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
    order by c.id
"""


def renew_waiting(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    """Send a waiting invoice to whoever can decide now, when the person on its newest request
    no longer can. Each case is its own step."""
    with engine.connect() as connection:
        work = [row.case_id for row in connection.execute(text(_STALE), {"only": only})]
    for case_id in work:
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                db.execute(
                    text("select id from invoice_case where id = :id for update"), {"id": case_id}
                )
                stale = db.execute(text(_STALE), {"only": [case_id]}).first()
                if stale is None:
                    continue  # decided or moved on since the work was listed
                _renew(db, approvals, case_id, stale.request_id, result)
        except Exception:
            log.exception("New approval request failed on case %s, next sweep retries", case_id)
            result.failed += 1


def renew_after_exception(
    db: Connection, approvals: Approvals, case_id: int, result: ApprovalPass
) -> None:
    """Called in the unit of work that resolved an absent approver or approver address
    exception: when that sent the case back to Awaiting approval, its newest request gets a new
    one, so whoever is asked now gets a fresh email and a fresh clock."""
    if get_case(db, case_id).status is not CaseStatus.AWAITING_APPROVAL:
        return
    newest = db.execute(
        text(
            "select q.id from approval_request q where q.case_id = :id"
            " and not exists (select 1 from approval_decision d where d.request_id = q.id)"
            " and q.id = (select max(id) from approval_request where case_id = :id)"
        ),
        {"id": case_id},
    ).scalar()
    if newest is not None:
        _renew(db, approvals, case_id, newest, result)


def _renew(
    db: Connection,
    approvals: Approvals,
    case_id: int,
    request_id: int,
    result: ApprovalPass,
) -> None:
    case = get_case(db, case_id)
    account = case.partner_account
    asked = who(db, account) if account else None
    if asked is None:
        open_exception(
            db,
            ExceptionReason.VENDOR_WITHOUT_APPROVER,
            (
                f"This invoice waits for approval, but its vendor account {account} has no "
                "approver in the approver table, so nobody can decide on it. Add an approver "
                "for the vendor in the approver table, or reject the invoice."
            ),
            case_id=case_id,
            facts={"account": account},
        )
        result.absent += 1
        return
    if not asked.available:
        open_exception(
            db,
            ExceptionReason.APPROVER_ABSENT,
            (
                f"This invoice waits for approval, but the approver of vendor account {account}, "
                f"{asked.approver_name}, is marked away and the vendor has no deputy who is in, "
                "so nobody can decide on it. Mark the approver or the deputy back, or give the "
                "vendor another approver or deputy, in the approver table. ARGUS then sends it "
                "to that person by itself. Or reject the invoice."
            ),
            case_id=case_id,
            facts={"account": account},
        )
        result.absent += 1
        return
    if not (asked.address and approvals.inside(asked.address)):
        open_exception(
            db,
            ExceptionReason.APPROVER_ADDRESS_OUTSIDE,
            (
                f"{asked.name}, who approves the invoices of vendor account {account} now, has "
                f"the email address {asked.address} in the approver table, which is not at "
                f"{approvals.own_domain}. ARGUS sends approval requests only to Max ASP "
                "addresses. Correct the address in the approver table, and ARGUS carries on by "
                "itself. Or reject the invoice."
            ),
            case_id=case_id,
            facts={"account": account},
        )
        result.outside += 1
        return
    stored: str = db.execute(
        text(
            "insert into approval_request (case_id, renews_id) values (:id, :old)"
            " returning recipient_address"
        ),
        {"id": case_id, "old": request_id},
    ).scalar_one()
    if not approvals.inside(stored):
        raise RuntimeError(f"the person on the new approval request for case {case_id} changed")
    result.renewed += 1


# --------------------------------------------------------------- reminders and escalations

_WAITING = """
    select q.id as request_id, q.case_id, coalesce(q.clock_from, q.sent_at) as clock_from
    from approval_request q join invoice_case c on c.id = q.case_id
    where c.status = 'awaiting_approval' and q.sent_at is not null
      and q.id = (select max(id) from approval_request where case_id = q.case_id)
      and not exists (select 1 from approval_decision d where d.request_id = q.id)
      and (cast(:only as bigint[]) is null or q.case_id = any(cast(:only as bigint[])))
    order by q.id
"""

_HAS_ROW = """
    select 1 from approval_nudge
    where request_id = :request and kind = :kind and failed_at is null
"""

_STORED = """
    select n.id as nudge_id, n.case_id, n.kind, n.recipient_address, n.recipient_name,
           n.clock_from, q.recipient_name as asked_name, q.on_behalf_of_name, q.vendor_account,
           c.kind as case_kind, c.document_number, c.gross_minor, c.currency,
           r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name
    from approval_nudge n
    join approval_request q on q.id = n.request_id
    join invoice_case c on c.id = n.case_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    where n.id = :id and n.sent_at is null and n.failed_at is null
      and c.status = 'awaiting_approval'
      and q.id = (select max(id) from approval_request where case_id = q.case_id)
      and not exists (select 1 from approval_decision d where d.request_id = q.id)
      and c.gross_minor is not null and c.currency is not null
"""


def nudge_mail(row: Any, app_url: str) -> NudgeMail:
    """The email of one stored reminder or escalation."""
    vendor = row["vendor_name"]
    return NudgeMail(
        nudge_id=row["nudge_id"],
        case_id=row["case_id"],
        kind=row["kind"],
        recipient_address=row["recipient_address"],
        recipient_name=row["recipient_name"],
        asked_name=row["asked_name"],
        on_behalf_of_name=row["on_behalf_of_name"],
        asked_on=berlin_date(row["clock_from"]),
        vendor_name=vendor
        if isinstance(vendor, str) and vendor.strip()
        else f"vendor account {row['vendor_account']}",
        document_number=row["document_number"] or "without a number",
        gross_cents=row["gross_minor"],
        currency=(row["currency"] or "").strip(),
        credit_note=CaseKind(row["case_kind"]) is CaseKind.INCOMING_CREDIT_NOTE,
        link=approval_link(app_url, row["case_id"]),
    )


def stored_nudges(engine: Engine, app_url: str) -> Callable[[int], NudgeMail | None]:
    """What the approval client checks each reminder against, right before it sends: the email
    of the stored row with this ID, read fresh, or None when there is no such row, it was sent
    or failed already, or its request no longer waits (decided, replaced or case moved on)."""

    def lookup(nudge_id: int) -> NudgeMail | None:
        with engine.connect() as connection:
            row = connection.execute(text(_STORED), {"id": nudge_id}).mappings().first()
        return nudge_mail(row, app_url) if row is not None else None

    return lookup


def send_nudges(
    engine: Engine,
    approvals: Approvals,
    only: list[int] | None,
    result: ApprovalPass,
    now: Callable[[], datetime],
) -> None:
    """Write and send every reminder and escalation that is due and not written yet."""
    mailbox = approvals.nudges
    if mailbox is None:
        return
    today = berlin_date(now())
    escalate_to = approvals.escalate_to
    if escalate_to and not approvals.inside(escalate_to):
        log.error("The escalation address is not a Max ASP address, escalations are not sent")
        escalate_to = None
    with engine.connect() as connection:
        waiting = list(connection.execute(text(_WAITING), {"only": only}).mappings())
    for row in waiting:
        reminder_on, escalation_on = due_dates(row["clock_from"])
        for kind, due_on in ((REMINDER, reminder_on), (ESCALATION, escalation_on)):
            if due_on > today:
                continue
            if kind == ESCALATION and not escalate_to:
                continue
            if not is_live(engine):
                log.info("Approval reminders stop, ARGUS was switched to shadow mode")
                return
            _one(engine, approvals, mailbox, row, kind, due_on, escalate_to, result)


def _one(
    engine: Engine,
    approvals: Approvals,
    mailbox: NudgeMailbox,
    row: Any,
    kind: str,
    due_on: date,
    escalate_to: str | None,
    result: ApprovalPass,
) -> None:
    request_id = row["request_id"]
    try:
        with unit_of_work(engine, APPROVAL_ACTOR) as db:
            if db.execute(text(_HAS_ROW), {"request": request_id, "kind": kind}).first():
                return  # written already: sent, or its fate unknown, so never again
            nudge_id: int = db.execute(
                text(
                    "insert into approval_nudge (request_id, kind, recipient_address, due_on)"
                    " values (:request, :kind, :address, :due) returning id"
                ),
                {
                    "request": request_id,
                    "kind": kind,
                    # The database sets the person of the request for a reminder.
                    "address": escalate_to if kind == ESCALATION else None,
                    "due": due_on,
                },
            ).scalar_one()
    except Exception:
        log.exception("Approval %s of request %s not written, next sweep tries", kind, request_id)
        result.failed += 1
        return
    with engine.connect() as connection:
        stored = connection.execute(text(_STORED), {"id": nudge_id}).mappings().first()
    mail = nudge_mail(stored, approvals.app_url) if stored is not None else None
    try:
        if mail is None:
            raise GraphCallRefused("the reminder no longer waits")
        mailbox.send_approval_nudge(mail)
    except (GraphApiError, GraphCallRefused):
        # Graph refused it, or the client did before sending: certainly not sent.
        log.exception("Approval %s %s not sent, next sweep tries again", kind, nudge_id)
        _mark(engine, nudge_id, "failed_at")
        result.failed += 1
        return
    except httpx.HTTPError:
        log.error(
            "Approval %s %s may or may not have gone out, ARGUS does not send it again",
            kind,
            nudge_id,
        )
        result.failed += 1
        return
    if not _mark(engine, nudge_id, "sent_at"):
        log.error("Approval %s %s sent but not marked, never sent again", kind, nudge_id)
        result.failed += 1
        return
    if kind == REMINDER:
        result.reminded += 1
    else:
        result.escalated += 1


_MARK = {
    "sent_at": "update approval_nudge set sent_at = clock_timestamp() where id = :id",
    "failed_at": "update approval_nudge set failed_at = clock_timestamp() where id = :id",
}


def _mark(engine: Engine, nudge_id: int, column: str) -> bool:
    try:
        with unit_of_work(engine, APPROVAL_ACTOR) as db:
            db.execute(text(_MARK[column]), {"id": nudge_id})
        return True
    except Exception:
        log.exception("Approval reminder %s could not be marked", nudge_id)
        return False


def utc_now() -> datetime:
    return datetime.now(UTC)

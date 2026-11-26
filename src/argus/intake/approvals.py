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

**Second approval above EUR 5,000 (#39, AP-09).** After the vendor's approver approved on the
approval page, the pass checks the gross amount against EUR 5,000.00:

- An invoice in EUR is checked by the database with the decision; at or below the line the
  page already moved it to Approved.
- An invoice in another currency is converted at the Wise rate at the time of the first
  approval (#18 question 9), read with the Wise client's ``get_rate``. The rate, its time and
  the EUR amount are stored with the decision (``approval_amount``). At or below the line the
  pass completes the move to Approved the person decided; the database allows this job, and
  only this one, to do that. If the rate cannot be read, the case waits in Awaiting approval and
  the next sweep tries again. It never counts as below the line.
- Above the line the pass writes a second approval request to the second approver (Benjamin,
  the ``second_approver`` setting an admin keeps) and the same email and page as the first one
  go out. When nobody is set or he is away, the case goes to the exception queue as
  ``second_approver_missing``. When he gave the first approval himself, as the vendor's
  approver, it goes there as ``second_approver_same_person``; once a person lets it carry on,
  the second request goes to him and is marked "same person" (Arita on #39, 23 Nov).

The pass runs at the end of every live sweep, after the account proposal. In shadow mode it
moves and sends nothing: it reads the mode at the start and again before each email
(``docs/design/shadow-mode.md``). Logs name only case and request IDs (NF-03).
"""

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
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


class RateSource(Protocol):
    """The one Wise call the pass needs (#39): the rate from one currency to another at a time.
    :class:`argus.wise.client.WiseClient` is the real one."""

    def get_rate(self, source: str, target: str, at: str | None = None) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Approvals:
    """What the pass needs: the approval client, the address of the ARGUS app and Max ASP's
    own email domain (``ARGUS_OWN_DOMAIN``), the only one approval requests go to. ``rates``
    reads the Wise rate for invoices in another currency than EUR (#39); without it they wait
    after the first approval."""

    mailbox: ApprovalMailbox
    app_url: str
    own_domain: str
    rates: RateSource | None = None

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
    # The second approval above EUR 5,000 (#39).
    rates_read: int = 0  # Wise rates stored for a first approval in another currency
    rate_waiting: int = 0  # first approvals whose Wise rate could not be read yet
    completed: int = 0  # cases at or below the line moved to Approved after the rate
    second_requested: int = 0  # second requests written
    second_queued: int = 0  # cases sent to the queue: no second approver, or the same person


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
    _read_rates(engine, approvals, only, result)
    _second_requests(engine, approvals, only, result)
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


# ------------------------------------------------------- the line at EUR 5,000 (#39, AP-09)

# First approvals of cases that still wait, in another currency than EUR, with no amount yet.
_NO_AMOUNT = """
    select d.id as decision_id, d.case_id, d.decided_at, upper(btrim(c.currency)) as currency
    from invoice_case c
    join lateral (
      select * from approval_request where case_id = c.id order by id desc limit 1
    ) q on true
    join approval_decision d on d.request_id = q.id and d.decision = 'approved'
    where c.status = 'awaiting_approval' and q.step = 'first'
      and not exists (select 1 from approval_amount m where m.decision_id = d.id)
      and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
    order by d.id
"""


class RateUnreadable(ValueError):
    """Wise answered, but not with a rate above 0 and its time."""


def _rate_of(answer: Mapping[str, Any]) -> tuple[Decimal, str]:
    """The rate and its time from a Wise answer, or RateUnreadable. The time stays as Wise wrote
    it; the database reads it."""
    try:
        rate = Decimal(str(answer["rate"]))
        at = str(answer["time"]).strip()
    except (KeyError, TypeError, InvalidOperation):
        raise RateUnreadable("Wise gave no rate and time") from None
    if not rate.is_finite() or rate <= 0 or not at:
        raise RateUnreadable("Wise gave no rate above 0")
    return rate, at


def _read_rates(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    with engine.connect() as connection:
        rows = connection.execute(text(_NO_AMOUNT), {"only": only}).all()
    for row in rows:
        decision_id: int = row.decision_id
        case_id: int = row.case_id
        decided_at: datetime = row.decided_at
        if row.currency == "EUR":
            continue  # the database checks EUR with the decision; nothing to read
        if approvals.rates is None:
            log.info("Case %s waits for the Wise rate, Wise is not set", case_id)
            result.rate_waiting += 1
            continue
        try:
            rate, at = _rate_of(
                approvals.rates.get_rate(row.currency, "EUR", at=decided_at.isoformat())
            )
        except Exception:
            # Never below the line by default: the case waits and the next sweep tries again.
            log.exception("Wise rate for case %s not read, next sweep retries", case_id)
            result.rate_waiting += 1
            result.failed += 1
            continue
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                _store_rate(db, case_id, decision_id, rate, at, result)
        except Exception:
            log.exception("Wise rate for case %s not stored, next sweep retries", case_id)
            result.rate_waiting += 1
            result.failed += 1


def _store_rate(
    db: Connection, case_id: int, decision_id: int, rate: Decimal, at: str, result: ApprovalPass
) -> None:
    db.execute(text("select id from invoice_case where id = :id for update"), {"id": case_id})
    if get_case(db, case_id).status is not CaseStatus.AWAITING_APPROVAL:
        return  # moved on since the work was listed
    above: bool = db.execute(
        text(
            "insert into approval_amount (decision_id, rate, rate_time)"
            " values (:decision, :rate, cast(:at as timestamptz)) returning above_line"
        ),
        {"decision": decision_id, "rate": rate, "at": at},
    ).scalar_one()
    result.rates_read += 1
    if not above:
        # The person approved; at or below the line that settles it (AP-09). The database lets
        # this job, and no other, complete the move (migration 0023).
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
        result.completed += 1


# First approvals above the line whose case waits with no second request and no open exception.
_SECOND_DUE = """
    select c.id as case_id, d.id as decision_id
    from invoice_case c
    join lateral (
      select * from approval_request where case_id = c.id order by id desc limit 1
    ) q on true
    join approval_decision d on d.request_id = q.id and d.decision = 'approved'
    join approval_amount m on m.decision_id = d.id and m.above_line
    where c.status = 'awaiting_approval' and q.step = 'first'
      and not exists (select 1 from exception_item e
                      where e.case_id = c.id and e.resolved_at is null)
      and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
    order by c.id
"""


@dataclass(frozen=True)
class _Second:
    """The second approver and what the pass needs to know about this case."""

    object_id: str
    name: str
    address: str
    absent: bool
    gave_first: bool  # he gave the first approval of this case too
    may_give_both: bool  # a person let the same person exception carry on since then


def _second_for(db: Connection, case_id: int, decision_id: int) -> _Second | None:
    row = db.execute(
        text(
            "select a.object_id, a.name, a.email, a.absent,"
            " lower(d.decided_by) = lower('user:' || a.object_id) as gave_first,"
            " exists (select 1 from exception_item e where e.case_id = :case"
            "   and e.reason::text = 'second_approver_same_person'"
            "   and e.outcome = 'accepted_with_comment' and e.resolved_at > d.decided_at)"
            "   as may_give_both"
            " from second_approver s join approver a on a.object_id = s.approver_id"
            " cross join approval_decision d where d.id = :decision"
        ),
        {"case": case_id, "decision": decision_id},
    ).first()
    if row is None:
        return None
    return _Second(
        row.object_id, row.name, row.email, row.absent, row.gave_first, row.may_give_both
    )


def _second_requests(
    engine: Engine, approvals: Approvals, only: list[int] | None, result: ApprovalPass
) -> None:
    with engine.connect() as connection:
        work = connection.execute(text(_SECOND_DUE), {"only": only}).all()
    for row in work:
        case_id: int = row.case_id
        try:
            with unit_of_work(engine, APPROVAL_ACTOR) as db:
                _second_one(db, approvals, case_id, row.decision_id, result)
        except Exception:
            log.exception("Second approval request failed on case %s, next sweep retries", case_id)
            result.failed += 1


_ABOVE = "The invoice is above EUR 5,000 gross, so it needs a second approval after the first."


def _second_one(
    db: Connection, approvals: Approvals, case_id: int, decision_id: int, result: ApprovalPass
) -> None:
    db.execute(text("select id from invoice_case where id = :id for update"), {"id": case_id})
    if get_case(db, case_id).status is not CaseStatus.AWAITING_APPROVAL:
        return  # moved on since the work was listed
    second = _second_for(db, case_id, decision_id)
    if second is None or second.absent or not approvals.inside(second.address):
        if second is None:
            why = "No second approver is set in ARGUS."
        elif second.absent:
            why = f"The second approver, {second.name}, is marked away."
        else:
            why = (
                f"The second approver, {second.name}, has the email address {second.address},"
                f" which is not at {approvals.own_domain}."
            )
        open_exception(
            db,
            ExceptionReason.SECOND_APPROVER_MISSING,
            (
                f"{_ABOVE} {why} An admin sets the second approver, or marks him back, or"
                ' corrects his address in the approver table. Then pick "Checked, carry on"'
                " and ARGUS sends him the request. Or reject the invoice."
            ),
            case_id=case_id,
        )
        result.second_queued += 1
        return
    if second.gave_first and not second.may_give_both:
        open_exception(
            db,
            ExceptionReason.SECOND_APPROVER_SAME_PERSON,
            (
                f"{_ABOVE} {second.name} is the approver of this vendor and also the second"
                " approver, and gave the first approval. Arita decided he may give the second"
                " approval too. Carry on to send him the second request; his second approval"
                ' is then marked "same person" in the audit trail. Or reject the invoice.'
            ),
            case_id=case_id,
        )
        result.second_queued += 1
        return
    stored: str = db.execute(
        text(
            "insert into approval_request (case_id, step) values (:id, 'second')"
            " returning recipient_address"
        ),
        {"id": case_id},
    ).scalar_one()
    if not approvals.inside(stored):
        raise RuntimeError(f"the second approver of case {case_id} changed")
    result.second_requested += 1


# ----------------------------------------------------------------------------- the email

_UNSENT = """
    select q.id as request_id, q.case_id, q.vendor_account, q.recipient_address,
           q.recipient_name, q.on_behalf_of_name, c.kind, c.document_number, c.gross_minor,
           c.currency, r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name,
           f.decided_by_name as first_approval_by, q.same_person
    from approval_request q
    join invoice_case c on c.id = q.case_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    left join approval_decision f on f.id = q.after_decision_id
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
        first_approval_by=row["first_approval_by"],
        same_person=bool(row["same_person"]),
    )


_STORED = """
    select q.id as request_id, q.case_id, q.vendor_account, q.recipient_address,
           q.recipient_name, q.on_behalf_of_name, c.kind, c.document_number, c.gross_minor,
           c.currency, r.reading -> 'fields' -> 'issuer_name' ->> 'value' as vendor_name,
           f.decided_by_name as first_approval_by, q.same_person
    from approval_request q
    join invoice_case c on c.id = q.case_id
    left join invoice_reading r on r.case_id = c.id and r.role = 'invoice'
    left join approval_decision f on f.id = q.after_decision_id
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

"""Get a case ready to go for approval, for tests that need a case in Awaiting approval but are
about something else (#191).

Since migration 0020 the database moves a case from Received to Awaiting approval only with a
vendor account that has an approver, a duplicate key, a current account proposal and an
approval request written in the same transaction. These helpers add what is missing, inside
the caller's transaction, the way the sweep would. The step that really does this is tested in
tests/integration/test_approval_requests.py. Names and numbers are made up.
"""

from typing import Any

from sqlalchemy import Connection, text

from argus.cases.model import Actor, CaseStatus, ClosedReason
from argus.cases.store import change_case, get_case, move_case, set_actor

# A made up approver who is never marked away, for the vendors these helpers set up.
HELPER_APPROVER = "a1910000-0000-4000-8000-000000000191"
HELPER_ADMIN = Actor.user("44444444-4444-4444-8444-444444444444")
HELPER_VENDOR = "70191"
# The made up second approver for invoices above EUR 5,000 (#39), never a vendor's approver.
HELPER_SECOND = "a0390000-0000-4000-8000-000000000039"
# A made up Wise rate, EUR for one unit of any other currency, for the helper's decisions.
HELPER_RATE = "1.10"
SYSTEM = Actor.system("approval-request")


def _as(db: Connection, actor: Actor) -> None:
    set_actor(db, actor)


def prepare_for_approval(db: Connection, case_id: int) -> None:
    """Add a vendor account with an approver, an account proposal and an approval request to
    the case, as far as each is missing. Leaves the actor as it was. The duplicate key stays
    as the test made it, because migration 0010 has its own tests for it."""
    before: str | None = db.execute(
        text("select current_setting('argus.actor', true)")
    ).scalar_one()
    case = get_case(db, case_id)
    account = case.partner_account or HELPER_VENDOR
    if case.partner_account is None:
        _as(db, SYSTEM)
        change_case(db, case_id, partner_account=account)
    has_someone = db.execute(
        text(
            "select not a.absent or (d.object_id is not null and not d.absent)"
            " from vendor_approver v join approver a on a.object_id = v.approver_id"
            " left join approver d on d.object_id = v.deputy_id"
            " where v.vendor_account = :account"
        ),
        {"account": account},
    ).scalar_one_or_none()
    if not has_someone:
        _as(db, HELPER_ADMIN)
        db.execute(text("select set_config('argus.changed_by_name', 'Test Admin', true)"))
        db.execute(
            text(
                "insert into approver (object_id, name, email) values"
                " (:id, 'Helper Approver', 'helper.approver@maxasp-test.example')"
                " on conflict (object_id) do update set absent = false"
            ),
            {"id": HELPER_APPROVER},
        )
        db.execute(
            text(
                "insert into vendor_approver (vendor_account, approver_id) values (:account, :id)"
                " on conflict (vendor_account) do update set approver_id = excluded.approver_id"
            ),
            {"account": account, "id": HELPER_APPROVER},
        )
    _as(db, SYSTEM)
    has_proposal = db.execute(
        text("select 1 from account_proposal where case_id = :id and vendor_account = :account"),
        {"id": case_id, "account": account},
    ).first()
    if not has_proposal:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, from_document_number, from_posting_date)"
                " values (:id, :account, '4930', '9', 'SKR03', 'booking_history', 'RE-HELPER',"
                " date '2026-01-15')"
            ),
            {"id": case_id, "account": account},
        )
    db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
    db.execute(
        text("select set_config('argus.actor', :actor, true)"), {"actor": before or str(SYSTEM)}
    )


def send_for_approval(db: Connection, case_id: int) -> bool:
    """Move a case from Received to Awaiting approval, with everything the database asks for.
    Returns False when the case is no longer in Received, like move_case."""
    if get_case(db, case_id).status is not CaseStatus.RECEIVED:
        return False
    prepare_for_approval(db, case_id)
    return move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)


def _restore(db: Connection, before: Any) -> None:
    db.execute(
        text(
            "select set_config('argus.actor', :actor, true),"
            " set_config('argus.actor_name', :name, true),"
            " set_config('argus.actor_address', :address, true)"
        ),
        {
            "actor": before.actor or str(SYSTEM),
            "name": before.name or "",
            "address": before.address or "",
        },
    )


def _decide_newest(db: Connection, case_id: int, decision: str, comment: str | None) -> int:
    """Insert a decision on the newest request as the person it went to. Returns its ID."""
    request = db.execute(
        text(
            "select id, recipient_id, recipient_name, recipient_address from approval_request"
            " where case_id = :id order by id desc limit 1"
        ),
        {"id": case_id},
    ).one()
    set_actor(
        db, Actor.user(request.recipient_id, request.recipient_name, request.recipient_address)
    )
    decision_id: int = db.execute(
        text(
            "insert into approval_decision (case_id, request_id, proposal_id, decision, comment)"
            " values (:case_id, :request_id,"
            " (select p.id from account_proposal p join invoice_case c on c.id = p.case_id"
            "  where p.case_id = :case_id and p.vendor_account = c.partner_account"
            "  order by p.id desc limit 1),"
            " :decision, :comment) returning id"
        ),
        {
            "case_id": case_id,
            "request_id": request.id,
            "decision": decision,
            "comment": comment,
        },
    ).scalar_one()
    return decision_id


def set_helper_second_approver(db: Connection) -> None:
    """Make the helper's second approver the one who gives second approvals (#39)."""
    _as(db, HELPER_ADMIN)
    db.execute(text("select set_config('argus.changed_by_name', 'Test Admin', true)"))
    db.execute(
        text(
            "insert into approver (object_id, name, email) values"
            " (:id, 'Helper Second Approver', 'helper.second@maxasp-test.example')"
            " on conflict (object_id) do update set absent = false"
        ),
        {"id": HELPER_SECOND},
    )
    db.execute(
        text(
            "insert into second_approver (approver_id) values (:id)"
            " on conflict (only_one) do update set approver_id = excluded.approver_id"
        ),
        {"id": HELPER_SECOND},
    )


def decide(
    db: Connection, case_id: int, *, approve: bool = True, comment: str | None = None
) -> bool:
    """Decide on a case in Awaiting approval as the person its newest request went to, and move
    it to Approved or close it as rejected, the way the approval page does (#38). Since
    migration 0021 only that person can, and only with a decision row in the same transaction.
    The decision names the case's current account proposal, as the page sends it.

    Since migration 0023 (#39) an approval only settles the case once its amount in EUR is at
    or below EUR 5,000. Above it, or in another currency, this helper does what ARGUS and
    Benjamin would: it stores a made up Wise rate (HELPER_RATE) for another currency, and
    above the line it sets a helper second approver, sends the second request and approves
    it as that person. Leaves the actor as it was. Returns False when the case no longer
    waits, like move_case."""
    before = db.execute(
        text(
            "select current_setting('argus.actor', true) as actor,"
            " current_setting('argus.actor_name', true) as name,"
            " current_setting('argus.actor_address', true) as address"
        )
    ).one()
    if not approve:
        _decide_newest(db, case_id, "rejected", comment or "Not ordered by us")
        moved = move_case(
            db,
            case_id,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
        _restore(db, before)
        return moved
    first = _decide_newest(db, case_id, "approved", comment)
    above = db.execute(
        text("select above_line from approval_amount where decision_id = :id"), {"id": first}
    ).scalar_one_or_none()
    if above is False:
        moved = move_case(
            db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED
        )
        _restore(db, before)
        return moved
    if above is None:
        _as(db, SYSTEM)
        above = db.execute(
            text(
                "insert into approval_amount (decision_id, rate, rate_time)"
                " values (:id, :rate, clock_timestamp()) returning above_line"
            ),
            {"id": first, "rate": HELPER_RATE},
        ).scalar_one()
        if not above:
            moved = move_case(
                db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED
            )
            _restore(db, before)
            return moved
    set_helper_second_approver(db)
    _as(db, SYSTEM)
    db.execute(
        text("insert into approval_request (case_id, step) values (:id, 'second')"),
        {"id": case_id},
    )
    _decide_newest(db, case_id, "approved", None)
    moved = move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    _restore(db, before)
    return moved

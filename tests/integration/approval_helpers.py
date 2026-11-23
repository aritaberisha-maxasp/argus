"""Get a case ready to go for approval, for tests that need a case in Awaiting approval but are
about something else (#191).

Since migration 0020 the database moves a case from Received to Awaiting approval only with a
vendor account that has an approver, a duplicate key, a current account proposal and an
approval request written in the same transaction. These helpers add what is missing, inside
the caller's transaction, the way the sweep would. The step that really does this is tested in
tests/integration/test_approval_requests.py. Names and numbers are made up.
"""

from sqlalchemy import Connection, text

from argus.cases.model import Actor, CaseStatus
from argus.cases.store import change_case, get_case, move_case, set_actor

# A made up approver who is never marked away, for the vendors these helpers set up.
HELPER_APPROVER = "a1910000-0000-4000-8000-000000000191"
HELPER_ADMIN = Actor.user("44444444-4444-4444-8444-444444444444")
HELPER_VENDOR = "70191"
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

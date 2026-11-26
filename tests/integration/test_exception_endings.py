"""Who may end an exception and how, held by the database (issue #180, migration 0016).

Covers NF-01, AP-08, EX-01, EX-02, PR-05, VM-04 and NF-04. Every rule is tested with the rights
of argus_app, so no code path can get round it. Every name, number and address is made up.
"""

import hashlib
import itertools
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
    Outcome,
)
from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    create_case,
    get_case,
    move_case,
    name_person,
    open_exception,
    resolve_exception,
    unit_of_work,
)
from tests.integration.approval_helpers import decide, prepare_for_approval
from tests.integration.conftest import migrate

pytestmark = pytest.mark.req("EX-01", "NF-01")

S = CaseStatus
INTAKE = Actor.system("intake")
VENDOR_MATCH = Actor.system("vendor-match")
BENJAMIN = Actor.user(
    "33333333-3333-4333-8333-333333333333", "Benjamin Test", "benjamin@max-asp-test.example"
)
APPROVER = Actor.user("11111111-1111-4111-8111-111111111111", "Alice Test")
NAMELESS = Actor.user("22222222-2222-4222-8222-222222222222")
COMMENT = "Rang Erika Mustermann, the number is right"  # made up, stays out of the audit log
# Every ending that does not close the case lets it carry on toward payment (compliance review
# of PR 199): the case goes back where it came from whatever the word.
CLOSING = {Outcome.REJECTED, Outcome.CONFIRMED_DUPLICATE}
CARRY_ON = [o for o in Outcome if o not in CLOSING and o is not Outcome.NOT_A_DUPLICATE]

_counter = itertools.count(1)


def new_case(
    app: Engine,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
    *,
    partner_account: str | None = "70001",
) -> int:
    n = next(_counter)
    with unit_of_work(app, INTAKE) as db:
        return create_case(
            db,
            kind,
            partner_account=partner_account,
            document_number=f"RE-ENDINGS-{n}",
            gross_cents=11900,
            currency="EUR",
            duplicate_key=f"endings-{n}",
        )


def walk(app: Engine, case_id: int, *statuses: CaseStatus) -> None:
    for to in statuses:
        with unit_of_work(app, INTAKE) as db:
            case = get_case(db, case_id)
            if to is S.AWAITING_APPROVAL:
                # Proposal, duplicate key and approval request first, as the sweep does (#191).
                prepare_for_approval(db, case_id)
            if to is S.APPROVED:
                # Only the person asked approves, on the approval page (#38).
                assert decide(db, case_id)
                continue
            ref = f"DATEV-ENDINGS-{case_id}" if to is S.BOOKED else None
            move_case(db, case_id, expected=case.status, to=to, datev_booking_ref=ref)


def stopped(
    app: Engine,
    reason: ExceptionReason,
    *,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
    after: tuple[CaseStatus, ...] = (),
    partner_account: str | None = "70001",
) -> tuple[int, int]:
    case_id = new_case(app, kind, partner_account=partner_account)
    walk(app, case_id, *after)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, reason, "Detail for the person.", case_id=case_id)
    return case_id, exception_id


def row_of(owner: Engine, exception_id: int) -> dict[str, Any]:
    with owner.connect() as db:
        return dict(
            db.execute(
                text(
                    "select outcome, resolution, resolved_by, resolved_by_name,"
                    " resolved_by_address from exception_item where id = :id"
                ),
                {"id": exception_id},
            )
            .mappings()
            .one()
        )


def resolved_event(owner: Engine, exception_id: int) -> dict[str, Any]:
    with owner.connect() as db:
        details: dict[str, Any] = db.execute(
            text(
                "select details from audit_event"
                " where exception_id = :id and event_type = 'exception_resolved'"
            ),
            {"id": exception_id},
        ).scalar_one()
    return details


# --- Carrying on needs a person ---


@pytest.mark.req("AP-08", "EX-02")
@pytest.mark.parametrize("outcome", CARRY_ON)
@pytest.mark.parametrize(
    "reason",
    [
        ExceptionReason.INVOICE_CHECK_FAILED,  # failed §14 UStG check (#33)
        ExceptionReason.UNKNOWN_VENDOR,
        ExceptionReason.VENDOR_WITHOUT_APPROVER,
    ],
)
def test_a_job_cannot_let_a_case_carry_on(
    app: Engine, reason: ExceptionReason, outcome: Outcome
) -> None:
    case_id, exception_id = stopped(app, reason)

    with (
        pytest.raises(CaseRuleBroken, match=f"only a person can end exception {exception_id}"),
        unit_of_work(app, INTAKE) as db,
    ):
        resolve_exception(db, exception_id, outcome, resolution="the job says so")

    assert get_case(app.connect(), case_id).status is S.EXCEPTION


@pytest.mark.req("EX-02")
def test_an_exception_without_a_case_answered_by_a_job_is_refused_too(app: Engine) -> None:
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, ExceptionReason.AUDIT_COPY_FAILED, "Copy failed.")

    with (
        pytest.raises(CaseRuleBroken, match="only a person"),
        unit_of_work(app, INTAKE) as db,
    ):
        resolve_exception(db, exception_id, Outcome.ANSWERED, resolution="auto")


@pytest.mark.req("AP-08")
@pytest.mark.parametrize("outcome", CARRY_ON)
@pytest.mark.parametrize("blank", [None, "", "   ", "\t", "\n\r\n", " \t\n ", "\u00a0\u2003"])
def test_a_person_must_say_why_a_case_carries_on(
    app: Engine, outcome: Outcome, blank: str | None
) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with (
        pytest.raises(
            CaseRuleBroken, match=f"ending exception {exception_id} as .* needs a reason"
        ),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(db, exception_id, outcome, resolution=blank)


@pytest.mark.req("AP-08", "EX-02")
@pytest.mark.parametrize("outcome", CARRY_ON)
def test_a_person_with_a_reason_lets_a_failed_check_carry_on(
    app: Engine, owner: Engine, outcome: Outcome
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, outcome, resolution=COMMENT)

    assert get_case(app.connect(), case_id).status is S.RECEIVED
    assert row_of(owner, exception_id)["outcome"] == outcome.value


@pytest.mark.req("EX-02")
@pytest.mark.parametrize("outcome", CARRY_ON)
def test_a_job_cannot_end_an_exception_without_a_case_either(app: Engine, outcome: Outcome) -> None:
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, ExceptionReason.AUDIT_COPY_FAILED, "Copy failed.")

    with (
        pytest.raises(CaseRuleBroken, match="only a person"),
        unit_of_work(app, Actor.system("bank-import")) as db,
    ):
        resolve_exception(db, exception_id, outcome, resolution="the job says so")


# --- Automatic endings after ARGUS checked again stay possible ---


@pytest.mark.parametrize(
    ("reason", "outcome", "job"),
    [
        (ExceptionReason.VENDOR_WITHOUT_APPROVER, Outcome.RETRIED, "vendor-match"),
        (ExceptionReason.UNKNOWN_VENDOR, Outcome.FIXED_IN_DATEV, "vendor-match"),
        # The two approval-request endings are shown end to end in test_approval_requests,
        # which needs an approver who can approve again (migration 0020).
    ],
)
def test_the_listed_automatic_endings_need_no_person(
    app: Engine, reason: ExceptionReason, outcome: Outcome, job: str
) -> None:
    case_id, exception_id = stopped(app, reason)

    with unit_of_work(app, Actor.system(job)) as db:
        resolve_exception(db, exception_id, outcome)

    assert get_case(app.connect(), case_id).status is S.RECEIVED


@pytest.mark.parametrize(
    ("reason", "outcome", "job"),
    [
        # The right job, but another outcome or reason than listed.
        (ExceptionReason.VENDOR_WITHOUT_APPROVER, Outcome.FIXED_IN_DATEV, "vendor-match"),
        (ExceptionReason.UNKNOWN_VENDOR, Outcome.RETRIED, "vendor-match"),
        (ExceptionReason.INVOICE_CHECK_FAILED, Outcome.RETRIED, "vendor-match"),
        (ExceptionReason.INVOICE_CHECK_FAILED, Outcome.DATA_CORRECTED, "bank-import"),
        (ExceptionReason.INVOICE_CHECK_FAILED, Outcome.FIXED_IN_DATEV, "account-proposal"),
        # The listed reason and outcome, but another job.
        (ExceptionReason.VENDOR_WITHOUT_APPROVER, Outcome.RETRIED, "intake"),
        (ExceptionReason.APPROVER_ADDRESS_OUTSIDE, Outcome.RETRIED, "vendor-match"),
    ],
)
def test_only_the_listed_job_reason_and_outcome_go_together(
    app: Engine, reason: ExceptionReason, outcome: Outcome, job: str
) -> None:
    _, exception_id = stopped(app, reason)

    with (
        pytest.raises(CaseRuleBroken, match="only a person"),
        unit_of_work(app, Actor.system(job)) as db,
    ):
        resolve_exception(db, exception_id, outcome, resolution="checked again")


@pytest.mark.req("PR-05", "VM-04")
def test_the_iban_recheck_still_ends_a_hold_by_itself(app: Engine, owner: Engine) -> None:
    """An IBAN mismatch stays out of the person only rule: PR 185's recheck ends it as fixed in
    DATEV once DATEV holds every IBAN of the invoice. #186 (migration 0018) lets only that job
    do it, see test_iban_hold_ending.py."""
    case_id, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)

    with unit_of_work(app, VENDOR_MATCH) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV)

    assert get_case(app.connect(), case_id).status is S.RECEIVED
    row = row_of(owner, exception_id)
    assert row["resolved_by"] == "system:vendor-match"
    assert (row["resolved_by_name"], row["resolved_by_address"]) == (None, None)


def test_the_vendor_match_ends_an_unknown_vendor_once_the_case_has_its_account(
    app: Engine,
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.UNKNOWN_VENDOR)

    with unit_of_work(app, VENDOR_MATCH) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV, resolution="matched")

    assert get_case(app.connect(), case_id).status is S.RECEIVED


@pytest.mark.parametrize(
    ("actor", "account"),
    [(VENDOR_MATCH, None), (INTAKE, "70001"), (Actor.system("account-proposal"), "70001")],
)
def test_no_other_job_ends_an_unknown_vendor_and_not_without_an_account(
    app: Engine, actor: Actor, account: str | None
) -> None:
    _, exception_id = stopped(app, ExceptionReason.UNKNOWN_VENDOR, partner_account=account)

    with pytest.raises(CaseRuleBroken, match="only a person"), unit_of_work(app, actor) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV, resolution="matched")


# --- Closing only where nothing has moved yet ---


@pytest.mark.req("NF-04")
@pytest.mark.parametrize("outcome", [Outcome.REJECTED, Outcome.CONFIRMED_DUPLICATE])
@pytest.mark.parametrize(
    "kind",
    [CaseKind.OUTGOING_INVOICE, CaseKind.OUTGOING_CREDIT_NOTE, CaseKind.BANK_TRANSACTION],
)
def test_an_outgoing_invoice_or_bank_transaction_is_not_closed_through_the_queue(
    app: Engine, kind: CaseKind, outcome: Outcome
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.UNKNOWN_CUSTOMER, kind=kind)

    with (
        pytest.raises(CaseRuleBroken, match=f"case {case_id} is an {kind.value} and cannot be"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(
            db,
            exception_id,
            outcome,
            resolution=COMMENT,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )


@pytest.mark.req("NF-04", "PY-11")
@pytest.mark.parametrize("outcome", [Outcome.REJECTED, Outcome.CONFIRMED_DUPLICATE])
@pytest.mark.parametrize(
    "after",
    [
        (S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED),
        (S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED, S.IN_PAYMENT_RUN),
        (S.AWAITING_APPROVAL, S.APPROVED, S.BOOKED, S.IN_PAYMENT_RUN, S.PAID),
    ],
    ids=["booked", "in_payment_run", "paid"],
)
def test_a_booked_or_paid_invoice_is_not_closed_through_the_queue(
    app: Engine, after: tuple[CaseStatus, ...], outcome: Outcome
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED, after=after)

    with (
        pytest.raises(CaseRuleBroken, match=f"case {case_id} was {after[-1].value} and cannot"),
        unit_of_work(app, BENJAMIN) as db,
    ):
        resolve_exception(
            db,
            exception_id,
            outcome,
            resolution=COMMENT,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    assert get_case(app.connect(), case_id).status is S.EXCEPTION


@pytest.mark.parametrize("kind", [CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE])
@pytest.mark.parametrize("after", [(), (S.AWAITING_APPROVAL,), (S.AWAITING_APPROVAL, S.APPROVED)])
def test_an_incoming_invoice_before_booking_may_still_be_rejected(
    app: Engine, kind: CaseKind, after: tuple[CaseStatus, ...]
) -> None:
    case_id, exception_id = stopped(
        app, ExceptionReason.INVOICE_CHECK_FAILED, kind=kind, after=after
    )

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            resolution=COMMENT,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )

    assert get_case(app.connect(), case_id).status is S.CLOSED


# --- The person's name and sign in address (ADR 0008) ---


@pytest.mark.req("AP-08")
def test_a_persons_resolution_keeps_their_name_and_sign_in_address(
    app: Engine, owner: Engine
) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT, resolution=COMMENT)

    row = row_of(owner, exception_id)
    assert row["resolved_by"] == "user:33333333-3333-4333-8333-333333333333"
    assert row["resolved_by_name"] == "Benjamin Test"
    assert row["resolved_by_address"] == "benjamin@max-asp-test.example"
    details = resolved_event(owner, exception_id)
    assert details["name"] == "Benjamin Test"
    assert details["sign_in_address"] == "benjamin@max-asp-test.example"


@pytest.mark.req("AP-08")
@pytest.mark.parametrize("name", [None, "", "  \t\n "])
def test_a_person_without_a_name_cannot_resolve(app: Engine, name: str | None) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)
    who = Actor.user(NAMELESS.name, name, "someone@max-asp-test.example")

    with (
        pytest.raises(CaseRuleBroken, match=f"exception {exception_id} needs the name"),
        unit_of_work(app, who) as db,
    ):
        resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT, resolution=COMMENT)


def test_a_closing_by_a_person_needs_their_name_too(app: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with pytest.raises(CaseRuleBroken, match="needs the name"), unit_of_work(app, NAMELESS) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            resolution=COMMENT,
            case_to=S.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )


def test_the_sign_in_address_is_kept_when_the_sign_in_gives_one(app: Engine, owner: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with unit_of_work(app, APPROVER) as db:
        resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT, resolution=COMMENT)

    row = row_of(owner, exception_id)
    assert (row["resolved_by_name"], row["resolved_by_address"]) == ("Alice Test", None)
    assert "sign_in_address" not in resolved_event(owner, exception_id)


def test_a_job_resolution_carries_no_name(app: Engine, owner: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.VENDOR_WITHOUT_APPROVER)

    with unit_of_work(app, VENDOR_MATCH) as db:
        resolve_exception(db, exception_id, Outcome.RETRIED, resolution="approver added")

    row = row_of(owner, exception_id)
    assert row["resolved_by"] == "system:vendor-match"
    assert (row["resolved_by_name"], row["resolved_by_address"]) == (None, None)
    assert set(resolved_event(owner, exception_id)) == {"reason", "outcome", "resolution_sha256"}


def test_a_job_cannot_borrow_a_persons_name(app: Engine, owner: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.VENDOR_WITHOUT_APPROVER)

    with unit_of_work(app, VENDOR_MATCH) as db:
        name_person(db, "Benjamin Test", "benjamin@max-asp-test.example")
        resolve_exception(db, exception_id, Outcome.RETRIED, resolution="approver added")

    row = row_of(owner, exception_id)
    assert (row["resolved_by_name"], row["resolved_by_address"]) == (None, None)


def test_the_app_cannot_write_the_name_itself(app: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with pytest.raises(NotAllowed), unit_of_work(app, BENJAMIN) as db:
        db.execute(
            text("update exception_item set resolved_by_name = 'Someone else' where id = :id"),
            {"id": exception_id},
        )


# --- The comment hash in the audit log ---


@pytest.mark.req("NF-03")
def test_the_audit_log_holds_a_hash_of_the_comment_but_not_the_comment(
    app: Engine, owner: Engine
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.INVOICE_CHECK_FAILED)

    with unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT, resolution=COMMENT)

    details = resolved_event(owner, exception_id)
    assert details["resolution_sha256"] == hashlib.sha256(COMMENT.encode()).hexdigest()
    with owner.connect() as db:
        leaks: int = db.execute(
            text(
                "select count(*) from audit_event"
                " where case_id = :id and details::text like '%Mustermann%'"
            ),
            {"id": case_id},
        ).scalar_one()
    assert leaks == 0


def test_a_resolution_without_a_comment_has_no_hash(app: Engine, owner: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.VENDOR_WITHOUT_APPROVER)

    with unit_of_work(app, VENDOR_MATCH) as db:  # an automatic ending, no comment needed
        resolve_exception(db, exception_id, Outcome.RETRIED)

    assert "resolution_sha256" not in resolved_event(owner, exception_id)


# --- Rows from before the migration ---


def test_rows_resolved_before_0016_stay_as_they_were(app: Engine, owner: Engine) -> None:
    """The migration runs on a database that already holds resolved exceptions and fills
    nothing in afterwards."""
    case_id = new_case(app)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.INVOICE_CHECK_FAILED, "x", case_id=case_id
        )

    migrate(owner, "0020", down=True)
    try:
        # Code from before 0016: a person without a name, and no comment.
        with unit_of_work(app, NAMELESS) as db:
            resolve_exception(db, exception_id, Outcome.ACCEPTED_WITH_COMMENT)
    finally:
        migrate(owner, "head")

    row = row_of(owner, exception_id)
    assert row["outcome"] == "accepted_with_comment"
    assert row["resolved_by"] == f"user:{NAMELESS.name}"
    assert (row["resolution"], row["resolved_by_name"], row["resolved_by_address"]) == (
        None,
        None,
        None,
    )
    assert resolved_event(owner, exception_id) == {
        "reason": "invoice_check_failed",
        "outcome": "accepted_with_comment",
    }


def test_the_name_and_address_stay_out_of_the_actors_repr() -> None:
    """A log line or a traceback with an actor in it never shows the person (NF-03)."""
    shown = repr(BENJAMIN) + str(BENJAMIN)
    assert "Benjamin Test" not in shown
    assert "benjamin@max-asp-test.example" not in shown
    assert Actor.user(BENJAMIN.name) == BENJAMIN  # the object ID alone says who it is

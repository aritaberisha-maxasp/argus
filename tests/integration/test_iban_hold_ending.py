"""Only ARGUS's own DATEV check ends an IBAN hold as fixed in DATEV (issue #186, migration 0018).

Covers PR-05, VM-04 and NF-01. Since PR 185 an invoice whose IBAN is not on the vendor account in
DATEV waits as ``iban_mismatch``. The vendor match pass (``system:vendor-match``) ends it as
``fixed_in_datev`` once DATEV holds every IBAN of the invoice; a person can only reject it. The
database refuses ``fixed_in_datev`` from anyone else. Every rule is tested with the rights of
argus_app. Every name, number and address is made up.
"""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from argus.cases.model import Actor, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    get_case,
    resolve_exception,
    unit_of_work,
)
from tests.integration.conftest import migrate
from tests.integration.test_exception_endings import (
    BENJAMIN,
    COMMENT,
    VENDOR_MATCH,
    resolved_event,
    row_of,
    stopped,
)

pytestmark = pytest.mark.req("PR-05", "VM-04", "NF-01")

S = CaseStatus
REFUSED = r"only ARGUS's own DATEV check can end IBAN hold \d+ as fixed in DATEV"


def is_open(owner: Engine, exception_id: int) -> bool:
    with owner.connect() as db:
        return bool(
            db.execute(
                text("select resolved_at is null from exception_item where id = :id"),
                {"id": exception_id},
            ).scalar_one()
        )


def test_a_person_cannot_end_an_iban_hold_as_fixed_in_datev(app: Engine, owner: Engine) -> None:
    """Not even with a name and a reason: "fixed in DATEV" is ARGUS's check, never a click."""
    case_id, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)

    with pytest.raises(CaseRuleBroken, match=REFUSED), unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV, resolution=COMMENT)

    assert is_open(owner, exception_id)
    assert get_case(app.connect(), case_id).status is S.EXCEPTION


@pytest.mark.parametrize(
    "job", ["intake", "account-proposal", "approval-request", "bank-import", "vendor-matcher"]
)
def test_no_other_job_ends_an_iban_hold_as_fixed_in_datev(
    app: Engine, owner: Engine, job: str
) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)

    with pytest.raises(CaseRuleBroken, match=REFUSED), unit_of_work(app, Actor.system(job)) as db:
        resolve_exception(
            db, exception_id, Outcome.FIXED_IN_DATEV, resolution="DATEV checked, says the job"
        )

    assert is_open(owner, exception_id)
    assert get_case(app.connect(), case_id).status is S.EXCEPTION


def test_an_iban_hold_cannot_end_with_no_actor_set(app: Engine, owner: Engine) -> None:
    _, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)

    with pytest.raises(DBAPIError, match=r"argus\.actor is not set"), app.begin() as db:
        db.execute(
            text("update exception_item set outcome = 'fixed_in_datev' where id = :id"),
            {"id": exception_id},
        )

    assert is_open(owner, exception_id)


def test_the_vendor_match_still_ends_the_hold_after_a_refused_try(
    app: Engine, owner: Engine
) -> None:
    """A refused try changes nothing, and the recheck from PR 185 still ends the hold."""
    case_id, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)
    with pytest.raises(CaseRuleBroken, match=REFUSED), unit_of_work(app, BENJAMIN) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV, resolution=COMMENT)

    with unit_of_work(app, VENDOR_MATCH) as db:
        resolve_exception(db, exception_id, Outcome.FIXED_IN_DATEV)

    assert get_case(app.connect(), case_id).status is S.RECEIVED
    row = row_of(owner, exception_id)
    assert (row["outcome"], row["resolved_by"]) == ("fixed_in_datev", "system:vendor-match")
    assert resolved_event(owner, exception_id) == {
        "reason": "iban_mismatch",
        "outcome": "fixed_in_datev",
    }


def test_a_person_can_still_reject_an_iban_hold(app: Engine, owner: Engine) -> None:
    case_id, exception_id = stopped(app, ExceptionReason.IBAN_MISMATCH)

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
    row = row_of(owner, exception_id)
    assert (row["outcome"], row["resolved_by_name"]) == ("rejected", "Benjamin Test")


def test_holds_resolved_before_0018_stay_as_they_were(app: Engine, owner: Engine) -> None:
    """The migration runs on a database that already holds resolved IBAN holds, including one a
    person ended as fixed in DATEV before the rule existed, and changes none of them."""
    _, by_person = stopped(app, ExceptionReason.IBAN_MISMATCH)
    _, by_job = stopped(app, ExceptionReason.IBAN_MISMATCH)

    migrate(owner, "0016", down=True)
    try:
        with unit_of_work(app, BENJAMIN) as db:
            resolve_exception(db, by_person, Outcome.FIXED_IN_DATEV, resolution=COMMENT)
        with unit_of_work(app, VENDOR_MATCH) as db:
            resolve_exception(db, by_job, Outcome.FIXED_IN_DATEV)
        before = {i: row_of(owner, i) for i in (by_person, by_job)}
    finally:
        migrate(owner, "head")

    assert {i: row_of(owner, i) for i in (by_person, by_job)} == before
    assert before[by_person]["resolved_by"] == f"user:{BENJAMIN.name}"
    assert before[by_job]["resolved_by"] == "system:vendor-match"

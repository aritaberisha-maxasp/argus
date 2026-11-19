"""AC-5: an invoice from an unknown vendor and one with a changed IBAN land in the exception queue.

E12 (unknown vendor) and E06 (changed IBAN) go through intake. Passes when both are in the
exception queue with the right reason and neither is sent for approval or added to a payment
run (test strategy).
"""

from typing import Any

import pytest
from sqlalchemy import Engine, text

from tests.acceptance.intake import IntakeRun


def cases_of(app: Engine, intake_run: IntakeRun, fixture_id: str) -> list[dict[str, Any]]:
    """The cases of one fixture email, with their open exceptions."""
    [record] = [e.record_id for e in intake_run.emails if e.fixture_id == fixture_id]
    with app.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "select c.id, c.status, c.partner_account, array(select x.reason::text"
                    " from exception_item x where x.case_id = c.id and x.resolved_at is null"
                    " order by x.id) as open"
                    " from invoice_case c where c.source_email_id = :id order by c.id"
                ),
                {"id": record},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@pytest.mark.req("AC-5", "VM-03", "EX-02")
def test_e12_from_an_unknown_vendor_waits_in_the_exception_queue(
    app: Engine, intake_run: IntakeRun
) -> None:
    """After intake E12 has one open exception with reason unknown_vendor, no approval request
    and no place in a payment run (#35). It stays in Exception, so neither can happen until
    the vendor exists in DATEV."""
    [case] = cases_of(app, intake_run, "E12")
    assert (case["status"], case["partner_account"], case["open"]) == (
        "exception",
        None,
        ["unknown_vendor"],
    )


@pytest.mark.req("AC-5", "VM-02", "EX-02")
def test_e13_from_a_vendor_without_an_approver_waits_in_the_exception_queue(
    app: Engine, intake_run: IntakeRun
) -> None:
    """E13 comes from vendor 70008, which DATEV knows but the approver table does not (#35)."""
    [case] = cases_of(app, intake_run, "E13")
    assert (case["status"], case["partner_account"], case["open"]) == (
        "exception",
        "70008",
        ["vendor_without_approver"],
    )


@pytest.mark.req("AC-5", "PR-02")
def test_e01_from_a_known_vendor_with_an_approver_is_matched_and_carries_on(
    app: Engine, intake_run: IntakeRun
) -> None:
    [case] = cases_of(app, intake_run, "E01")
    assert (case["status"], case["partner_account"], case["open"]) == ("received", "70001", [])


@pytest.mark.req("AC-5", "PR-05")
@pytest.mark.waits_on(36)
def test_e06_with_a_changed_iban_is_blocked_in_the_exception_queue() -> None:
    """After intake E06 has one open exception with reason iban_mismatch, flagged as a possible
    fraud case, no approval request and no place in a payment run."""
    pytest.fail("write this scenario when #36 is built")

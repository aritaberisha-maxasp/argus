"""AC-2: an approved test invoice appears in DATEV with correct account, tax key and voucher.

E01 is approved through the approval stand in. Passes when the DATEV stand in holds one booking
on vendor account 70001 with the G/L account, tax key and amounts of ``expected.json`` and the
invoice PDF attached as voucher (test strategy).
"""

import pytest
from sqlalchemy import Engine, text

from tests.acceptance.intake import IntakeRun
from tests.acceptance.test_ac5_exceptions import cases_of


@pytest.mark.req("AC-2", "BK-01", "BK-03")
@pytest.mark.waits_on(38, 41, 42)
def test_approved_e01_is_booked_on_vendor_70001_with_its_pdf_as_voucher() -> None:
    """Take E01 through intake, approve it as its approver, then read the DATEV stand in:
    exactly one booking on 70001 with G/L account, tax key, net, VAT and gross as in
    expected.json, and the original PDF (same SHA-256 as stored) attached."""
    pytest.fail("write this scenario when #38, #41 and #42 are built")


@pytest.mark.req("AC-2", "PR-04")
def test_matched_invoices_get_the_account_and_tax_key_of_their_booking_history(
    app: Engine, intake_run: IntakeRun
) -> None:
    """The account and tax key AC-2 books come from the proposal (#37). E01 (Muster Bürobedarf,
    70001) was booked to 4930 with tax key 9 before, and E13 (70008) to 4730. The proposal does
    not wait for the approver, so E13 has one too."""
    for fixture_id, expected in (("E01", ("4930", "9")), ("E13", ("4730", "9"))):
        [case] = cases_of(app, intake_run, fixture_id)
        with app.connect() as connection:
            proposal = connection.execute(
                text(
                    "select gl_account, tax_key, source from account_proposal"
                    " where case_id = :id order by id desc limit 1"
                ),
                {"id": case["id"]},
            ).one()
        assert (proposal.gl_account, proposal.tax_key) == expected, fixture_id
        assert proposal.source == "booking_history"

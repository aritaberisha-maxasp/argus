"""AC-2: an approved test invoice appears in DATEV with correct account, tax key and voucher.

E01 is approved through the approval stand in. Passes when the DATEV stand in holds one booking
on vendor account 70001 with the G/L account, tax key and amounts of ``expected.json`` and the
invoice PDF attached as voucher (test strategy).
"""

import pytest


@pytest.mark.req("AC-2", "BK-01", "BK-03")
@pytest.mark.waits_on(38, 41, 42)
def test_approved_e01_is_booked_on_vendor_70001_with_its_pdf_as_voucher() -> None:
    """Take E01 through intake, approve it as its approver, then read the DATEV stand in:
    exactly one booking on 70001 with G/L account, tax key, net, VAT and gross as in
    expected.json, and the original PDF (same SHA-256 as stored) attached."""
    pytest.fail("write this scenario when #38, #41 and #42 are built")

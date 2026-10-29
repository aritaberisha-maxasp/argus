"""AC-3: an invoice above EUR 5,000 is only booked after the second approval by Benjamin.

E05 (EUR 8,925.00 gross) is approved by the vendor's approver, then by the second approver.
Passes when nothing is booked after the first approval and the booking appears only after the
second (test strategy).
"""

import pytest


@pytest.mark.req("AC-3", "AP-09")
@pytest.mark.waits_on(39, 41)
def test_e05_is_booked_only_after_the_second_approval() -> None:
    """Approve E05 as its approver: the case waits for the second approver and the DATEV stand
    in holds no booking. Approve it as the second approver from roles.csv: one booking."""
    pytest.fail("write this scenario when #39 and #41 are built")


@pytest.mark.req("AC-3", "AP-09")
@pytest.mark.waits_on(39)
def test_exactly_eur_5000_gross_needs_one_approval_and_one_cent_more_needs_two() -> None:
    """The limit is above EUR 5,000.00 gross: exactly 5,000.00 needs one approval, 5,000.01
    needs the second one."""
    pytest.fail("write this scenario when #39 is built")

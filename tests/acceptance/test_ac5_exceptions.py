"""AC-5: an invoice from an unknown vendor and one with a changed IBAN land in the exception queue.

E12 (unknown vendor) and E06 (changed IBAN) go through intake. Passes when both are in the
exception queue with the right reason and neither is sent for approval or added to a payment
run (test strategy).
"""

import pytest


@pytest.mark.req("AC-5", "VM-03")
@pytest.mark.waits_on(35)
def test_e12_from_an_unknown_vendor_waits_in_the_exception_queue() -> None:
    """After intake E12 has one open exception with reason unknown_vendor, no approval request
    and no place in a payment run."""
    pytest.fail("write this scenario when #35 is built")


@pytest.mark.req("AC-5", "PR-05")
@pytest.mark.waits_on(36)
def test_e06_with_a_changed_iban_is_blocked_in_the_exception_queue() -> None:
    """After intake E06 has one open exception with reason iban_mismatch, flagged as a possible
    fraud case, no approval request and no place in a payment run."""
    pytest.fail("write this scenario when #36 is built")

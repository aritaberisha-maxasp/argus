"""AC-7: a monthly Wise CSV is imported, balances reconcile and paid invoices are cleared.

Passes when the balances per currency reconcile, the outgoing payments from AC-4 and the
incoming payment for MA-2026-0101 are cleared, and a partial payment leaves the rest open (test
strategy). The Wise CSV fixture comes with #56.
"""

import pytest


@pytest.mark.req("AC-7", "BA-01")
@pytest.mark.waits_on(56)
def test_the_monthly_wise_csv_is_imported_and_balances_reconcile_per_currency() -> None:
    """Import the statement: opening balance plus transactions equals the closing balance in
    each currency, and a second import of the same file changes nothing."""
    pytest.fail("write this scenario when #56 is built")


@pytest.mark.req("AC-7", "BA-02", "BA-03")
@pytest.mark.waits_on(57)
def test_the_outgoing_payments_of_the_payment_run_are_cleared() -> None:
    """Every transfer of the AC-4 batch that appears in the statement clears its invoices,
    which move to paid."""
    pytest.fail("write this scenario when #57 is built")


@pytest.mark.req("AC-7", "BA-04", "AR-09")
@pytest.mark.waits_on(58)
def test_customer_payments_clear_their_invoices_and_a_partial_payment_leaves_the_rest_open() -> (
    None
):
    """The incoming payment for MA-2026-0101 clears that invoice; a partial payment leaves the
    rest of its invoice open."""
    pytest.fail("write this scenario when #58 is built")

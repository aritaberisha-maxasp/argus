"""AC-6: an outgoing invoice is booked to the right customer account with the right tax treatment.

E17, E18 and E19 go through intake. Passes when each is booked on its customer account (10001,
10002, 10003) with 19 % VAT, EU reverse charge and not taxable, and E22 (unknown customer) goes
to the exception queue (test strategy).
"""

import pytest


@pytest.mark.req("AC-6", "AR-03", "AR-04")
@pytest.mark.waits_on(52)
@pytest.mark.parametrize(
    ("email_id", "customer_account", "tax_treatment"),
    [
        ("E17", "10001", "19 % VAT"),
        ("E18", "10002", "EU reverse charge"),
        ("E19", "10003", "not taxable"),
    ],
)
def test_an_outgoing_invoice_is_booked_on_its_customer_account(
    email_id: str, customer_account: str, tax_treatment: str
) -> None:
    """After intake the DATEV stand in holds one booking for the email on the customer account
    with the tax key for the tax treatment."""
    pytest.fail("write this scenario when #52 is built")


@pytest.mark.req("AC-6", "AR-03")
@pytest.mark.waits_on(52)
def test_e22_for_an_unknown_customer_goes_to_the_exception_queue() -> None:
    """After intake E22 has one open exception with reason unknown_customer and no booking."""
    pytest.fail("write this scenario when #52 is built")

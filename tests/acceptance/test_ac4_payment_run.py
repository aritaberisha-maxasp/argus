"""AC-4: a test run on the 15th creates the correct batch in Wise, without releasing it.

A test clock is set to a run date with approved invoices in the pool, including 15 Nov 2026, a
Sunday, so the run happens on Friday 13 Nov (PY-03). Passes when the Wise stand in receives one
batch with the invoices PY-04 to PY-07 select, one transfer per vendor net of credit notes, no
direct debit invoices (PY-13), and no funding or release call (test strategy).

What runs today: the run date rule, and a batch handed to the Wise stand in through the Wise
client of #43 stays unfunded. Choosing the invoices and building the batch waits on #46 to #50.
"""

from datetime import date

import pytest

from argus.wise import WiseCallRefused
from argus.working_days import working_day_on_or_before
from standins.wise.app import create_app
from tests.unit.wise_helpers import PROFILE_ID, TOKEN, client_for, standin_transport


@pytest.mark.req("AC-4", "PY-02", "PY-03")
def test_the_run_due_on_sunday_15_november_2026_happens_on_friday_13_november() -> None:
    assert working_day_on_or_before(date(2026, 11, 15)) == date(2026, 11, 13)
    assert working_day_on_or_before(date(2026, 11, 30)) == date(2026, 11, 30)


@pytest.mark.req("AC-4", "PY-08", "NF-04")
def test_a_batch_handed_to_wise_waits_for_a_person_to_fund_it() -> None:
    standin = create_app(token=TOKEN)
    with client_for(standin_transport(standin)) as wise:
        recipient = wise.create_recipient(
            {
                "currency": "EUR",
                "type": "iban",
                "accountHolderName": "Muster Bürobedarf GmbH",
                "details": {"legalType": "BUSINESS", "iban": "DE02000010100006820101"},
            }
        )
        group = wise.create_batch_group("ARGUS acceptance run 2026-11-13 EUR", "EUR")
        quote = wise.create_quote("EUR", "EUR", "250.00")
        transfer = wise.add_transfer(group["id"], recipient["id"], quote["id"], "ac4-1", "RE-1")
        group = wise.get_batch_group(group["id"])
        assert wise.complete_batch_group(group["id"], group["version"])["status"] == "COMPLETED"

        funding = f"/v3/profiles/{PROFILE_ID}/batch-payments/{group['id']}/payments"
        with pytest.raises(WiseCallRefused):
            wise._send("POST", funding, {"type": "BALANCE"})
        assert wise.get_transfer(transfer["id"])["status"] == "incoming_payment_waiting"


@pytest.mark.req("AC-4", "PY-04", "PY-05", "PY-06", "PY-07", "PY-13")
@pytest.mark.waits_on(46, 47, 48, 50)
def test_the_run_on_13_november_hands_wise_one_batch_with_the_right_transfers() -> None:
    """Set the test clock to 13 Nov 2026 with the approved fixture invoices in the pool. The
    Wise stand in then holds one batch per currency with exactly the invoices PY-04 to PY-06
    select, one transfer per vendor net of its credit notes with every invoice number in the
    reference (PY-07), nothing for E03 (direct debit, PY-13), and every transfer unfunded."""
    pytest.fail("write this scenario when #46, #47, #48 and #50 are built")

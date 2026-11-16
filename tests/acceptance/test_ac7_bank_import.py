"""AC-7: a monthly Wise CSV is imported, balances reconcile and paid invoices are cleared.

Passes when the balances per currency reconcile, the outgoing payments from AC-4 and the
incoming payment for MA-2026-0101 are cleared, and a partial payment leaves the rest open (test
strategy). The Wise CSV fixtures are in tests/fixtures/wise (#56).
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.bank.importer import BankSettings, ImportOutcome, StatementSource, import_statement
from argus.documents.store import AzureDocumentStore

WISE = Path(__file__).resolve().parents[1] / "fixtures" / "wise"
BENJAMIN = "benjamin@maxasp.example"


@pytest.mark.req("AC-7", "BA-01", "BA-07", "BA-08")
def test_the_monthly_wise_csv_is_imported_and_balances_reconcile_per_currency(
    owner: Engine, app: Engine, documents: AzureDocumentStore
) -> None:
    """Import the statement: opening balance plus transactions equals the closing balance in
    each currency, and a second import of the same file changes nothing."""
    with owner.begin() as connection:  # start without statements; the tables are append only
        connection.execute(text("set local session_replication_role = replica"))
        connection.execute(text("delete from bank_notice"))
        connection.execute(text("delete from exception_item where statement_id is not null"))
        connection.execute(text("delete from bank_transaction"))
        connection.execute(text("delete from bank_statement"))
    settings = BankSettings(statement_senders=frozenset({BENJAMIN}))
    source = StatementSource.email(BENJAMIN, authenticated=True)
    today = date(2026, 12, 2)

    for month, cases in (("2026-10", 6), ("2026-11", 3)):
        data = (WISE / f"statement_EUR_{month}.csv").read_bytes()
        first = import_statement(app, documents, settings, source, data, today=today)
        again = import_statement(app, documents, settings, source, data, today=today)

        assert (first.outcome, first.cases_created) == (ImportOutcome.IMPORTED, cases)
        assert (again.outcome, again.cases_created) == (ImportOutcome.ALREADY_SEEN, 0)
        assert documents.get(first.sha256 or "") == data

    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select s.opening_minor, s.closing_minor, sum(t.amount_minor) as moved"
                " from bank_statement s join bank_transaction t on t.statement_id = s.id"
                " group by s.id order by s.period"
            )
        ).all()
    assert [(r.opening_minor + r.moved, r.closing_minor) for r in rows] == [
        (1_420_986, 1_420_986),
        (1_445_836, 1_445_836),
    ]
    assert rows[1].opening_minor == rows[0].closing_minor


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

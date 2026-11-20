"""The rule that proposes a G/L account and tax key from the vendor's booking history (#37).

Covers PR-04. Postings, accounts and document numbers are made up.
"""

from datetime import date
from decimal import Decimal

import pytest

from argus.datev.models import Posting
from argus.intake.accounts import (
    CHART_ENV,
    BadAccount,
    Chart,
    NoProposalReason,
    NotProposed,
    Proposed,
    chart_from_env,
    check_entered,
    history_window,
    is_invoice_account,
    propose,
)

pytestmark = pytest.mark.req("PR-04")

VENDOR = 70001


def posting(day: str, contra: int, tax_key: str = "9", number: str | None = None) -> Posting:
    return Posting(
        account=VENDOR,
        date=date.fromisoformat(day),
        document_number=number or f"RE-{day}-{contra}",
        contra_account=contra,
        tax_key=tax_key,
        gross=Decimal("119.00"),
    )


def test_the_most_recent_account_and_tax_key_are_proposed() -> None:
    found = propose(
        [
            posting("2026-07-14", 4930, "9", "BB-1"),
            posting("2026-09-15", 4806, "9", "BB-3"),
            posting("2026-08-12", 4930, "9", "BB-2"),
        ],
        Chart.SKR03,
    )

    assert found == Proposed("4806", "9", "BB-3", date(2026, 9, 15), postings_used=3)


def test_a_vendor_without_history_gets_no_guess() -> None:
    found = propose([], Chart.SKR03)

    assert found == NotProposed(NoProposalReason.NO_HISTORY, postings_read=0)


def test_payments_and_discounts_on_the_vendor_account_are_not_history() -> None:
    """A payment (contra account bank) or a received discount is newer than the invoice, but
    says nothing about where the invoice belongs."""
    postings = [
        posting("2026-09-01", 4210, "", "MV-9"),
        posting("2026-09-20", 1200, ""),  # SKR03 bank: the payment
        posting("2026-09-20", 1360, ""),  # SKR03 money in transit
        posting("2026-09-20", 3736, "9"),  # SKR03 received discount 19 %
        posting("2026-09-21", 70002, ""),  # another personal account
    ]

    found = propose(postings, Chart.SKR03)

    assert isinstance(found, Proposed)
    assert (found.gl_account, found.tax_key, found.from_document_number) == ("4210", "", "MV-9")
    assert found.postings_used == 1


def test_only_payments_count_as_no_history() -> None:
    found = propose([posting("2026-09-20", 1200, "")], Chart.SKR03)

    assert found == NotProposed(NoProposalReason.NO_HISTORY, postings_read=1)


def test_the_chart_decides_which_accounts_are_money() -> None:
    """4930 is an expense in SKR03 but revenue in SKR04, and 6815 the other way round."""
    postings = [posting("2026-08-01", 6815, "9", "OLD"), posting("2026-09-01", 4930, "9", "NEW")]

    in_skr04 = propose(postings, Chart.SKR04)
    in_skr03 = propose(postings, Chart.SKR03)

    assert isinstance(in_skr04, Proposed) and in_skr04.gl_account == "6815"
    assert isinstance(in_skr03, Proposed) and in_skr03.gl_account == "4930"


@pytest.mark.parametrize(
    ("chart", "account", "usable"),
    [
        (Chart.SKR03, 4930, True),  # Bürobedarf
        (Chart.SKR03, 420, True),  # Büroeinrichtung (fixed asset)
        (Chart.SKR03, 3400, True),  # Wareneingang
        (Chart.SKR03, 1000, False),  # Kasse
        (Chart.SKR03, 1200, False),  # Bank
        (Chart.SKR03, 1360, False),  # Geldtransit
        (Chart.SKR03, 1730, False),  # Kreditkartenabrechnung
        (Chart.SKR03, 1800, False),  # Privatentnahmen
        (Chart.SKR03, 1600, False),  # Verbindlichkeiten
        (Chart.SKR03, 3730, False),  # Erhaltene Skonti
        (Chart.SKR03, 3739, False),
        (Chart.SKR03, 3740, True),
        (Chart.SKR03, 9009, False),  # Saldenvorträge Kreditoren
        (Chart.SKR04, 6815, True),  # Bürobedarf
        (Chart.SKR04, 650, True),  # Büroeinrichtung (fixed asset)
        (Chart.SKR04, 5400, True),  # Wareneingang
        (Chart.SKR04, 1800, False),  # Bank
        (Chart.SKR04, 1600, False),  # Kasse
        (Chart.SKR04, 1460, False),  # Geldtransit
        (Chart.SKR04, 3610, False),  # Kreditkartenabrechnung
        (Chart.SKR04, 5736, False),  # Erhaltene Skonti
        (Chart.SKR04, 4930, False),  # revenue in SKR04, not an expense
        (Chart.SKR04, 9009, False),
        (Chart.SKR03, 10000, False),  # a customer account
        (Chart.SKR03, 70001, False),  # a vendor account
        (Chart.SKR03, 0, False),
    ],
)
def test_which_accounts_an_invoice_can_be_booked_to(
    chart: Chart, account: int, usable: bool
) -> None:
    assert is_invoice_account(chart, account) is usable


def test_a_carry_forward_at_the_start_of_the_year_is_not_history() -> None:
    """An invoice still open on 1 January is carried into the new year against 9009, and paid
    later. The proposal is the account of the invoice, from December (code review of PR 190)."""
    found = propose(
        [
            posting("2025-12-10", 4930, "9", "BB-DEC"),
            posting("2026-01-01", 9009, ""),
            posting("2026-01-15", 1200, ""),
        ],
        Chart.SKR03,
    )

    assert isinstance(found, Proposed)
    assert (found.gl_account, found.from_document_number) == ("4930", "BB-DEC")


@pytest.mark.parametrize(
    ("chart", "expense", "card"), [(Chart.SKR03, 4930, 1730), (Chart.SKR04, 6815, 3610)]
)
def test_a_credit_card_payment_is_not_history(chart: Chart, expense: int, card: int) -> None:
    found = propose([posting("2026-09-01", expense), posting("2026-09-20", card, "")], chart)

    assert isinstance(found, Proposed) and found.gl_account == str(expense)


def test_two_accounts_on_the_most_recent_day_are_no_guess() -> None:
    """An invoice split over two accounts: ARGUS does not pick one."""
    found = propose(
        [
            posting("2026-08-01", 4930, "9"),
            posting("2026-09-01", 4806, "9", "SPLIT"),
            posting("2026-09-01", 4930, "9", "SPLIT"),
        ],
        Chart.SKR03,
    )

    assert found == NotProposed(
        NoProposalReason.SEVERAL_ACCOUNTS,
        postings_read=3,
        candidates=(("4806", "9"), ("4930", "9")),
    )


def test_two_tax_keys_on_the_most_recent_day_are_no_guess() -> None:
    found = propose(
        [posting("2026-09-01", 4930, "9"), posting("2026-09-01", 4930, "8")], Chart.SKR03
    )

    assert isinstance(found, NotProposed)
    assert found.candidates == (("4930", "8"), ("4930", "9"))


def test_the_same_account_twice_on_one_day_is_one_proposal() -> None:
    found = propose(
        [posting("2026-09-01", 4930, "9", "A-1"), posting("2026-09-01", 4930, " 9 ", "A-2")],
        Chart.SKR03,
    )

    assert isinstance(found, Proposed)
    assert (found.gl_account, found.tax_key, found.from_document_number) == ("4930", "9", "A-2")


def test_a_credit_note_counts_like_an_invoice() -> None:
    """A credit note is booked to the same account as the invoice it corrects."""
    credit = Posting(
        account=VENDOR,
        date=date(2026, 9, 30),
        document_number="GS-1",
        contra_account=4806,
        tax_key="9",
        gross=Decimal("-50.00"),
    )

    found = propose([posting("2026-09-01", 4930), credit], Chart.SKR03)

    assert isinstance(found, Proposed) and found.gl_account == "4806"


def test_the_history_covers_this_year_and_the_two_before() -> None:
    assert history_window(date(2026, 11, 20)) == (date(2024, 1, 1), date(2026, 11, 20))
    assert history_window(date(2027, 1, 1)) == (date(2025, 1, 1), date(2027, 1, 1))


@pytest.mark.parametrize(
    ("value", "chart"),
    [("", Chart.SKR03), ("  ", Chart.SKR03), ("skr04", Chart.SKR04), ("SKR03", Chart.SKR03)],
)
def test_the_chart_is_a_setting_with_skr03_as_default(value: str, chart: Chart) -> None:
    assert chart_from_env({CHART_ENV: value}) is chart
    assert chart_from_env({}) is Chart.SKR03


def test_an_unknown_chart_stops_the_job() -> None:
    with pytest.raises(ValueError, match="SKR03 or SKR04"):
        chart_from_env({CHART_ENV: "SKR49"})


def test_an_entered_account_is_cleaned() -> None:
    entered = check_entered(Chart.SKR03, " 4930 ", " 9 ", "  Office supplies as always  ")

    assert (entered.gl_account, entered.tax_key, entered.reason) == (
        "4930",
        "9",
        "Office supplies as always",
    )
    assert check_entered(Chart.SKR03, "4210", "", "Rent, tax free").tax_key == ""


@pytest.mark.parametrize(
    ("gl", "tax_key", "reason", "says"),
    [
        ("493", "9", "fine reason", "four digits"),
        ("49300", "9", "fine reason", "four digits"),
        ("4g30", "9", "fine reason", "four digits"),
        ("٤٩٣٠", "9", "fine reason", "four digits"),  # digits, but not ASCII ones
        ("1000", "9", "fine reason", "not an account an invoice"),
        ("9009", "9", "fine reason", "not an account an invoice"),
        ("1730", "9", "fine reason", "3000 to 4999"),
        ("4930", "12345", "fine reason", "tax key"),
        ("4930", "9a", "fine reason", "tax key"),
        ("4930", "9", "  ", "why"),
        ("4930", "9", "x" * 501, "why"),
    ],
)
def test_an_entered_account_the_person_can_fix_says_how(
    gl: str, tax_key: str, reason: str, says: str
) -> None:
    with pytest.raises(BadAccount, match=says):
        check_entered(Chart.SKR03, gl, tax_key, reason)


def test_skr04_refuses_its_own_bank_accounts_when_entered() -> None:
    with pytest.raises(BadAccount, match="SKR04"):
        check_entered(Chart.SKR04, "1800", "", "paid by bank")
    assert check_entered(Chart.SKR04, "6815", "9", "office supplies").gl_account == "6815"

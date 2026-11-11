"""The duplicate key of IN-10: vendor, invoice number and amount (#31).

The database side (the unique index, the link to the first case, the exception) is tested in
``tests/integration/test_duplicate_invoices.py``. All values are made up.
"""

import pytest

from argus.intake.duplicates import duplicate_key


@pytest.mark.req("IN-10")
def test_the_key_is_issuer_number_amount_and_currency() -> None:
    assert duplicate_key("vat:DE700000001|RE-2026/0042", 11900, "EUR") == (
        "vat:DE700000001|RE20260042|11900|EUR"
    )


@pytest.mark.req("IN-10")
@pytest.mark.parametrize("number", ["RE-2026/0042", "RE2026-0042", "RE.2026_0042", "re 2026 0042"])
def test_the_same_number_written_another_way_gives_the_same_key(number: str) -> None:
    assert duplicate_key(f"vat:DE700000001|{number}", 11900, "eur") == (
        "vat:DE700000001|RE20260042|11900|EUR"
    )


@pytest.mark.req("IN-10")
def test_an_issuer_without_vat_id_is_keyed_by_its_name() -> None:
    assert duplicate_key("name:muster buerobedarf|BB-1", 500001, "EUR") == (
        "name:muster buerobedarf|BB1|500001|EUR"
    )


@pytest.mark.req("IN-10")
def test_a_credit_note_keeps_its_minus_sign() -> None:
    assert duplicate_key("vat:DE700000001|G-1", -11900, "EUR") == "vat:DE700000001|G1|-11900|EUR"


@pytest.mark.req("IN-10")
def test_different_amounts_currencies_and_vendors_give_different_keys() -> None:
    keys = {
        duplicate_key("vat:DE700000001|1001", 11900, "EUR"),
        duplicate_key("vat:DE700000001|1001", 11901, "EUR"),
        duplicate_key("vat:DE700000001|1001", 11900, "USD"),
        duplicate_key("vat:DE700000002|1001", 11900, "EUR"),
    }
    assert len(keys) == 4


@pytest.mark.req("IN-10")
@pytest.mark.parametrize(
    ("case_key", "gross_cents", "currency"),
    [
        (None, 11900, "EUR"),
        ("vat:DE700000001|1001", None, "EUR"),
        ("vat:DE700000001|1001", 11900, None),
        ("vat:DE700000001|-/.", 11900, "EUR"),
        ("no separator", 11900, "EUR"),
    ],
    ids=["no case key", "amount not known", "currency not known", "number only noise", "bad"],
)
def test_no_key_while_a_part_is_missing(
    case_key: str | None, gross_cents: int | None, currency: str | None
) -> None:
    """A key without one of its parts could match a different invoice."""
    assert duplicate_key(case_key, gross_cents, currency) is None

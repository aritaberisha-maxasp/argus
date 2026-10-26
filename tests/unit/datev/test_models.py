"""Bookings that cannot be right never reach DATEV (BK-02, BK-04, BK-05, AR-05, AR-08)."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from argus.datev import Booking, BookingKind

VALID: dict[str, object] = {
    "case_id": "case-1",
    "account": 70001,
    "contra_account": 4930,
    "tax_key": "9",
    "document_date": date(2026, 10, 1),
    "document_number": "BB-2026-1187",
    "net": Decimal("100.00"),
    "vat": Decimal("19.00"),
    "gross": Decimal("119.00"),
}


@pytest.mark.req("BK-02")
def test_a_complete_booking_is_accepted() -> None:
    assert Booking.model_validate(VALID).gross == Decimal("119.00")


@pytest.mark.req("BK-02", "AR-05")
@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"gross": Decimal("119.01")}, "gross must equal net plus VAT"),
        ({"net": Decimal("-1.00"), "vat": Decimal("120.00")}, "greater than or equal to 0"),
        ({"net": Decimal("0"), "vat": Decimal("0"), "gross": Decimal("0")}, "greater than 0"),
        ({"case_id": ""}, "at least 1 character"),
        ({"document_number": "X" * 37}, "at most 36 characters"),
        ({"original_currency": "GBP"}, "original currency and original amount go together"),
        ({"original_gross": Decimal("50.00")}, "original currency and original amount go"),
        ({"kind": BookingKind.CREDIT_NOTE}, "a credit note names the invoice it corrects"),
        (
            {"service_period_start": date(2026, 9, 30), "service_period_end": date(2026, 9, 1)},
            "service period ends before it starts",
        ),
    ],
)
def test_impossible_bookings_are_refused(changes: dict[str, object], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        Booking.model_validate({**VALID, **changes})


@pytest.mark.req("BK-05", "AR-08")
def test_a_credit_note_naming_its_invoice_is_accepted() -> None:
    booking = Booking.model_validate(
        {**VALID, "kind": BookingKind.CREDIT_NOTE, "original_document_number": "BB-2026-1000"}
    )

    assert booking.kind is BookingKind.CREDIT_NOTE


@pytest.mark.req("BK-04")
def test_a_foreign_currency_booking_keeps_the_original_amount() -> None:
    booking = Booking.model_validate(
        {**VALID, "original_currency": "GBP", "original_gross": Decimal("101.50")}
    )

    assert (booking.original_currency, booking.original_gross) == ("GBP", Decimal("101.50"))

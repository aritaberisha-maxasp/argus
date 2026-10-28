"""What every DATEV behind the gateway must do (ADR 0002).

These tests only use data that the fixtures put into DATEV and bookings with fresh
document numbers, so they can run against the fake and against a DATEV test client.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from argus.datev import (
    BookingKind,
    BookingRejected,
    BookingStatus,
    DatevConnectGateway,
    RejectionReason,
)
from argus.datev.models import Booking

KNOWN_VENDOR = 70001
KNOWN_VENDOR_IBAN = "DE44000000020000700001"
UK_VENDOR = 70004
KNOWN_CUSTOMER = 10001
MISSING_ACCOUNT = 79999


def fresh_number(prefix: str = "CT") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def a_booking(**changes: object) -> Booking:
    values: dict[str, object] = {
        "case_id": f"case-{uuid.uuid4()}",
        "account": KNOWN_VENDOR,
        "contra_account": 4930,
        "tax_key": "9",
        "document_date": date(2026, 10, 1),
        "document_number": fresh_number(),
        "net": Decimal("100.00"),
        "vat": Decimal("19.00"),
        "gross": Decimal("119.00"),
        "due_date": date(2026, 10, 31),
        "voucher_link": "https://argus.example/documents/0001",
        "description": "Bürobedarf",
    }
    values.update(changes)
    return Booking.model_validate(values)


@pytest.mark.req("VM-02", "VM-04", "PR-05")
def test_reads_vendor_accounts_with_their_ibans(gateway: DatevConnectGateway) -> None:
    vendors = {vendor.number: vendor for vendor in gateway.list_vendors()}

    assert KNOWN_VENDOR in vendors
    assert [bank.iban for bank in vendors[KNOWN_VENDOR].bank_accounts] == [KNOWN_VENDOR_IBAN]
    assert vendors[UK_VENDOR].bank_accounts[0].currency == "GBP"


@pytest.mark.req("VM-04", "PR-05")
def test_reads_one_vendor_account(gateway: DatevConnectGateway) -> None:
    vendor = gateway.get_vendor(KNOWN_VENDOR)

    assert vendor is not None
    assert vendor.name == "Muster Bürobedarf GmbH"
    assert vendor.vat_id == "DE012345601"
    assert vendor.bank_accounts[0].iban == KNOWN_VENDOR_IBAN


@pytest.mark.req("VM-03")
def test_a_missing_vendor_reads_as_none(gateway: DatevConnectGateway) -> None:
    assert gateway.get_vendor(MISSING_ACCOUNT) is None


@pytest.mark.req("AR-03")
def test_reads_customer_accounts(gateway: DatevConnectGateway) -> None:
    customers = {customer.number: customer for customer in gateway.list_customers()}

    assert customers[KNOWN_CUSTOMER].name == "Beispiel Handel GmbH"
    assert gateway.get_customer(KNOWN_CUSTOMER) == customers[KNOWN_CUSTOMER]
    assert gateway.get_customer(19999) is None


@pytest.mark.req("PR-04")
def test_reads_booking_history_within_the_dates(gateway: DatevConnectGateway) -> None:
    postings = gateway.list_postings(KNOWN_VENDOR, date(2026, 7, 1), date(2026, 8, 31))

    assert {p.document_number for p in postings} >= {"BB-2026-0981", "BB-2026-1042"}
    assert "BB-2026-1110" not in {p.document_number for p in postings}
    assert all(p.contra_account == 4930 and p.tax_key == "9" for p in postings)


@pytest.mark.req("PR-04")
def test_reads_booking_history_across_fiscal_years(gateway: DatevConnectGateway) -> None:
    postings = gateway.list_postings(KNOWN_VENDOR, date(2025, 12, 1), date(2026, 7, 31))

    assert "BB-2026-0981" in {p.document_number for p in postings}


@pytest.mark.req("BK-01", "BK-02", "BK-03", "BK-07")
def test_writes_a_booking_and_gives_back_a_reference(gateway: DatevConnectGateway) -> None:
    booking = a_booking()

    record = gateway.submit_booking(booking)

    assert record.reference
    assert record.status is BookingStatus.UNCOMMITTED
    assert record.booking == booking
    assert gateway.get_booking(record.reference) == record


@pytest.mark.req("BK-01", "PR-04")
def test_a_new_booking_shows_up_in_the_history(gateway: DatevConnectGateway) -> None:
    booking = a_booking()

    gateway.submit_booking(booking)

    postings = gateway.list_postings(KNOWN_VENDOR, booking.document_date, booking.document_date)
    assert booking.document_number in {p.document_number for p in postings}


@pytest.mark.req("BK-04", "BK-05")
def test_writes_a_foreign_currency_credit_note(gateway: DatevConnectGateway) -> None:
    booking = a_booking(
        kind=BookingKind.CREDIT_NOTE,
        account=UK_VENDOR,
        contra_account=4806,
        tax_key="94",
        net=Decimal("57.80"),
        vat=Decimal("0.00"),
        gross=Decimal("57.80"),
        original_currency="GBP",
        original_gross=Decimal("50.00"),
        original_document_number="ESL-INV-10188",
    )

    record = gateway.submit_booking(booking)

    assert record.booking == booking


@pytest.mark.req("AR-04", "AR-05", "AR-06", "AR-08")
def test_writes_outgoing_invoice_and_its_cancellation(gateway: DatevConnectGateway) -> None:
    invoice = a_booking(
        account=KNOWN_CUSTOMER,
        contra_account=8400,
        tax_key="3",
        document_number=fresh_number("MA"),
        service_period_start=date(2026, 9, 1),
        service_period_end=date(2026, 9, 30),
    )
    cancellation = a_booking(
        kind=BookingKind.CREDIT_NOTE,
        account=KNOWN_CUSTOMER,
        contra_account=8400,
        tax_key="3",
        document_number=fresh_number("GS"),
        original_document_number=invoice.document_number,
    )

    assert gateway.submit_booking(invoice).booking == invoice
    assert gateway.submit_booking(cancellation).booking == cancellation


@pytest.mark.req("BK-06", "VM-03")
def test_a_booking_on_a_missing_account_is_rejected(gateway: DatevConnectGateway) -> None:
    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking(account=MISSING_ACCOUNT))

    assert rejected.value.reason is RejectionReason.UNKNOWN_ACCOUNT


@pytest.mark.req("BK-07")
def test_an_unknown_reference_reads_as_none(gateway: DatevConnectGateway) -> None:
    assert gateway.get_booking("20260101:nonexistent") is None


@pytest.mark.req("VM-01", "VM-04")
def test_bookings_leave_the_vendor_account_unchanged(gateway: DatevConnectGateway) -> None:
    before = gateway.get_vendor(KNOWN_VENDOR)

    gateway.submit_booking(a_booking())

    assert gateway.get_vendor(KNOWN_VENDOR) == before

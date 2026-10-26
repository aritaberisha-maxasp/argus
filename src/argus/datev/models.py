"""What ARGUS reads from and writes to DATEV (ADR 0002).

These are ARGUS's own shapes. How they map to DATEVconnect's JSON lives in
``datevconnect.py`` only, so a change in DATEV's field names touches one file.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BankAccount(_Frozen):
    iban: str
    currency: str = "EUR"


class Account(_Frozen):
    """A vendor (Kreditor) or customer (Debitor) account as DATEV holds it."""

    number: int
    name: str
    vat_id: str | None = None
    country: str | None = None
    bank_accounts: tuple[BankAccount, ...] = ()


class Posting(_Frozen):
    """One past booking on an account, read for G/L account proposals (PR-04)."""

    account: int
    date: date
    document_number: str
    contra_account: int
    tax_key: str
    gross: Decimal
    description: str = ""


class BookingKind(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"


class Booking(_Frozen):
    """One booking ARGUS asks DATEV to record (BK-02, AR-05).

    Amounts are in EUR, positive and in cents at most, because DATEV rounds to cents.
    A credit note is marked by ``kind``, not by a sign.
    """

    case_id: str = Field(min_length=1)
    kind: BookingKind = BookingKind.INVOICE
    account: int
    contra_account: int
    tax_key: str
    document_date: date
    document_number: str = Field(min_length=1, max_length=36)
    net: Decimal = Field(ge=0, decimal_places=2)
    vat: Decimal = Field(ge=0, decimal_places=2)
    gross: Decimal = Field(gt=0, decimal_places=2)
    due_date: date | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    original_currency: str | None = None
    original_gross: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    voucher_link: str | None = None
    original_document_number: str | None = None
    description: str = Field(default="", max_length=60)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.net + self.vat != self.gross:
            raise ValueError("gross must equal net plus VAT")
        if (self.original_currency is None) != (self.original_gross is None):
            raise ValueError("original currency and original amount go together")
        if self.kind is BookingKind.CREDIT_NOTE and not self.original_document_number:
            raise ValueError("a credit note names the invoice it corrects")
        if (
            self.service_period_start
            and self.service_period_end
            and self.service_period_start > self.service_period_end
        ):
            raise ValueError("service period ends before it starts")
        return self


class BookingStatus(StrEnum):
    """Where a booking stands in DATEV. Uncommitted until someone commits the batch."""

    UNCOMMITTED = "uncommitted"
    COMMITTED = "committed"


class BookingRecord(_Frozen):
    """A booking as DATEV reports it back (BK-07)."""

    reference: str
    status: BookingStatus
    booking: Booking

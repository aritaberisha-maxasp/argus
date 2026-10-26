"""The one way ARGUS talks to DATEV (ADR 0002, NF-06).

Every part of ARGUS that needs DATEV uses a ``DatevGateway`` and never calls
DATEVconnect directly. The gateway can read accounts and postings and write
bookings. It has no operation that creates or changes a vendor or customer
account (VM-01, VM-04), and nothing that touches payments (NF-04).
"""

from datetime import date
from enum import StrEnum
from typing import Protocol

from argus.datev.models import Account, Booking, BookingRecord, Posting


class DatevError(Exception):
    """Base class for everything the gateway raises."""


class DatevUnavailable(DatevError):
    """DATEV did not answer. Worth trying again later."""


class ForbiddenDatevOperation(DatevError):
    """A request outside the gateway's allow list. Never sent to DATEV."""


class RejectionReason(StrEnum):
    PERIOD_CLOSED = "period_closed"
    UNKNOWN_ACCOUNT = "unknown_account"
    DUPLICATE_DOCUMENT_NUMBER = "duplicate_document_number"
    INVALID = "invalid"


class BookingRejected(DatevError):
    """DATEV refused the booking. The case goes to the exception queue (BK-06)."""

    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(f"{reason.value}: {message}")
        self.reason = reason
        self.message = message


class DatevGateway(Protocol):
    def list_vendors(self) -> list[Account]:
        """All vendor accounts (Kreditoren) with their bank accounts."""
        ...

    def get_vendor(self, number: int) -> Account | None:
        """One vendor account, or None when DATEV has no such account (VM-03)."""
        ...

    def list_customers(self) -> list[Account]:
        """All customer accounts (Debitoren)."""
        ...

    def get_customer(self, number: int) -> Account | None:
        """One customer account, or None when DATEV has no such account (AR-03)."""
        ...

    def list_postings(self, account: int, start: date, end: date) -> list[Posting]:
        """Past bookings on an account between two dates, both included (PR-04)."""
        ...

    def submit_booking(self, booking: Booking) -> BookingRecord:
        """Write one booking. Raises BookingRejected or DatevUnavailable."""
        ...

    def get_booking(self, reference: str) -> BookingRecord | None:
        """A booking by the reference DATEV gave it (BK-07)."""
        ...

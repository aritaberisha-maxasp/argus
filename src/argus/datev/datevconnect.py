"""The DATEV gateway over DATEVconnect's accounting API (ADR 0002).

In production this talks to the bridge at Max ASP, which passes the calls on to
DATEVconnect. In development and CI it talks to the fake DATEV (``fake.py``),
which serves the same paths and JSON.

Provisional: the paths and field names follow public DATEVconnect material
(see docs/spikes/datev-access.md on PR #12). Once we have DATEV's own API
specification, fix them here and in the fake, and run the contract tests again.
"""

import logging
import re
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

import httpx

from argus.datev.gateway import (
    BookingRejected,
    DatevError,
    DatevUnavailable,
    ForbiddenDatevOperation,
    RejectionReason,
)
from argus.datev.models import (
    Account,
    BankAccount,
    Booking,
    BookingKind,
    BookingRecord,
    BookingStatus,
    Posting,
)

log = logging.getLogger(__name__)

API_ROOT = "/datev/api/accounting/v1"
FIRST_VENDOR_ACCOUNT = 70000

# Every request the gateway may send. Anything else is refused before it leaves
# ARGUS, so there is no way to create or change an account (VM-01, VM-04) or to
# reach DATEV's payment functions (NF-04).
_FISCAL_YEAR = rf"{API_ROOT}/clients/[^/]+/fiscal-years/\d{{8}}"
ALLOWED_REQUESTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(rf"{_FISCAL_YEAR}/(creditors|debitors)(/\d+)?")),
    ("GET", re.compile(rf"{_FISCAL_YEAR}/account-postings")),
    ("POST", re.compile(rf"{_FISCAL_YEAR}/accounting-sequences")),
    ("GET", re.compile(rf"{_FISCAL_YEAR}/accounting-sequences/[A-Za-z0-9-]+")),
)

_REJECTION_CODES = {reason.value: reason for reason in RejectionReason}


class HttpResponse(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class HttpClient(Protocol):
    """What the gateway needs from an HTTP client: ``httpx.Client`` or a test client."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> HttpResponse: ...


def is_allowed(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in ALLOWED_REQUESTS)


def fiscal_year_id(day: date) -> str:
    """DATEVconnect names a fiscal year by its first day. Max ASP's runs Jan to Dec."""
    return f"{day.year}0101"


class DatevConnectGateway:
    """A ``DatevGateway`` that speaks DATEVconnect's accounting API over HTTP."""

    def __init__(
        self,
        http: HttpClient,
        client_id: str,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._today = today

    @classmethod
    def from_url(
        cls, base_url: str, client_id: str, timeout: float = 30.0
    ) -> "DatevConnectGateway":
        return cls(httpx.Client(base_url=base_url, timeout=timeout), client_id)

    # Reading accounts and postings

    def list_vendors(self) -> list[Account]:
        return [_account(item) for item in self._get_json(self._path("creditors"))]

    def get_vendor(self, number: int) -> Account | None:
        return self._get_account("creditors", number)

    def list_customers(self) -> list[Account]:
        return [_account(item) for item in self._get_json(self._path("debitors"))]

    def get_customer(self, number: int) -> Account | None:
        return self._get_account("debitors", number)

    def list_postings(self, account: int, start: date, end: date) -> list[Posting]:
        postings: list[Posting] = []
        for year in range(start.year, end.year + 1):
            params = {
                "account_number": str(account),
                "date_from": max(start, date(year, 1, 1)).isoformat(),
                "date_to": min(end, date(year, 12, 31)).isoformat(),
            }
            path = self._path("account-postings", date(year, 1, 1))
            postings += [_posting(item) for item in self._get_json(path, params)]
        return postings

    # Writing bookings

    def submit_booking(self, booking: Booking) -> BookingRecord:
        fiscal_year = fiscal_year_id(booking.document_date)
        path = self._path("accounting-sequences", booking.document_date)
        body = {"accounting_records": [_record_json(booking)]}
        # DATEV may accept a booking on an account without master data and so open
        # the account by booking on it. VM-01 forbids that, so check first.
        is_vendor = booking.account >= FIRST_VENDOR_ACCOUNT
        lookup = self.get_vendor if is_vendor else self.get_customer
        if lookup(booking.account) is None:
            kind = "vendor" if is_vendor else "customer"
            log.info("DATEV booking for case %s refused: no %s account", booking.case_id, kind)
            raise BookingRejected(
                RejectionReason.UNKNOWN_ACCOUNT,
                f"{kind} account {booking.account} does not exist in DATEV",
            )
        log.info("DATEV booking for case %s, document %s", booking.case_id, booking.document_number)
        response = self._send("POST", path, json=body)
        if response.status_code in (400, 409, 422):
            rejection = _rejection(response)
            log.info("DATEV rejected case %s: %s", booking.case_id, rejection)
            raise rejection
        _raise_for_status(response)
        record = _booking_record(fiscal_year, response.json())
        log.info("DATEV booked case %s as %s", booking.case_id, record.reference)
        return record

    def get_booking(self, reference: str) -> BookingRecord | None:
        fiscal_year, _, sequence_id = reference.partition(":")
        if not (re.fullmatch(r"\d{8}", fiscal_year) and sequence_id):
            raise ValueError(f"not a DATEV booking reference: {reference!r}")
        path = (
            f"{API_ROOT}/clients/{self._client_id}/fiscal-years/{fiscal_year}"
            f"/accounting-sequences/{sequence_id}"
        )
        response = self._send("GET", path)
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return _booking_record(fiscal_year, response.json())

    # Plumbing

    def _path(self, resource: str, day: date | None = None) -> str:
        fiscal_year = fiscal_year_id(day or self._today())
        return f"{API_ROOT}/clients/{self._client_id}/fiscal-years/{fiscal_year}/{resource}"

    def _get_account(self, resource: str, number: int) -> Account | None:
        response = self._send("GET", f"{self._path(resource)}/{number}")
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        return _account(response.json())

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        response = self._send("GET", path, params=params)
        _raise_for_status(response)
        return response.json()

    def _send(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> HttpResponse:
        if not is_allowed(method, path):
            raise ForbiddenDatevOperation(f"{method} {path} is not on the gateway's allow list")
        try:
            return self._http.request(method, path, params=params, json=json)
        except httpx.TransportError as exc:
            raise DatevUnavailable(f"DATEV did not answer: {exc}") from exc


def _raise_for_status(response: HttpResponse) -> None:
    if response.status_code >= 500:
        raise DatevUnavailable(f"DATEV answered {response.status_code}")
    if response.status_code >= 400:
        raise DatevError(f"DATEV answered {response.status_code}: {response.text[:200]}")


def _rejection(response: HttpResponse) -> BookingRejected:
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        code, message = "", response.text[:200]
    return BookingRejected(_REJECTION_CODES.get(code, RejectionReason.INVALID), message)


def _account(item: dict[str, Any]) -> Account:
    return Account(
        number=int(item["account_number"]),
        name=item["caption"],
        vat_id=item.get("eu_vat_id_number") or None,
        country=item.get("country") or None,
        bank_accounts=tuple(
            BankAccount(iban=bank["iban"], currency=bank.get("currency_code") or "EUR")
            for bank in item.get("bank_accounts", [])
        ),
    )


def _posting(item: dict[str, Any]) -> Posting:
    return Posting(
        account=int(item["account_number"]),
        date=date.fromisoformat(item["date"]),
        document_number=item["document_field1"],
        contra_account=int(item["contra_account_number"]),
        tax_key=item["tax_key"],
        gross=item["amount"],
        description=item.get("posting_description", ""),
    )


def _debit_or_credit(booking: Booking) -> str:
    """S (debit) or H (credit) on the vendor or customer account.

    An invoice credits a vendor and debits a customer. A credit note does the opposite.
    """
    is_vendor = booking.account >= FIRST_VENDOR_ACCOUNT
    is_invoice = booking.kind is BookingKind.INVOICE
    return "H" if is_vendor == is_invoice else "S"


def _record_json(booking: Booking) -> dict[str, Any]:
    def day(value: date | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "date": booking.document_date.isoformat(),
        "document_field1": booking.document_number,
        "account_number": booking.account,
        "contra_account_number": booking.contra_account,
        "tax_key": booking.tax_key,
        "debit_credit_identifier": _debit_or_credit(booking),
        "amount": str(booking.gross),
        "net_amount": str(booking.net),
        "tax_amount": str(booking.vat),
        "due_date": day(booking.due_date),
        "service_period_from": day(booking.service_period_start),
        "service_period_to": day(booking.service_period_end),
        "currency_code_original": booking.original_currency,
        "amount_original": None if booking.original_gross is None else str(booking.original_gross),
        "document_link": booking.voucher_link,
        "original_document_field1": booking.original_document_number,
        "posting_description": booking.description,
        "additional_information": [{"type": "argus_case_id", "content": booking.case_id}],
    }


def _booking_from_record(record: dict[str, Any]) -> Booking:
    def day(value: str | None) -> date | None:
        return date.fromisoformat(value) if value else None

    account = int(record["account_number"])
    is_vendor = account >= FIRST_VENDOR_ACCOUNT
    is_invoice = (record["debit_credit_identifier"] == "H") == is_vendor
    case_ids = [
        info["content"]
        for info in record.get("additional_information", [])
        if info.get("type") == "argus_case_id"
    ]
    return Booking(
        case_id=case_ids[0] if case_ids else "unknown",
        kind=BookingKind.INVOICE if is_invoice else BookingKind.CREDIT_NOTE,
        account=account,
        contra_account=int(record["contra_account_number"]),
        tax_key=record["tax_key"],
        document_date=date.fromisoformat(record["date"]),
        document_number=record["document_field1"],
        net=record["net_amount"],
        vat=record["tax_amount"],
        gross=record["amount"],
        due_date=day(record.get("due_date")),
        service_period_start=day(record.get("service_period_from")),
        service_period_end=day(record.get("service_period_to")),
        original_currency=record.get("currency_code_original"),
        original_gross=record.get("amount_original"),
        voucher_link=record.get("document_link"),
        original_document_number=record.get("original_document_field1"),
        description=record.get("posting_description", ""),
    )


def _booking_record(fiscal_year: str, sequence: dict[str, Any]) -> BookingRecord:
    return BookingRecord(
        reference=f"{fiscal_year}:{sequence['id']}",
        status=BookingStatus.COMMITTED
        if sequence.get("is_committed")
        else BookingStatus.UNCOMMITTED,
        booking=_booking_from_record(sequence["accounting_records"][0]),
    )

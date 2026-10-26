"""How the HTTP gateway turns DATEV's answers into results and errors (BK-06)."""

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.datev import (
    Booking,
    BookingKind,
    BookingRejected,
    DatevConnectGateway,
    DatevError,
    DatevUnavailable,
    RejectionReason,
)
from argus.datev.datevconnect import fiscal_year_id
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev

Handler = Callable[[httpx.Request], httpx.Response]


def gateway_answering(handler: Handler) -> DatevConnectGateway:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://datev")
    return DatevConnectGateway(http, DEFAULT_CLIENT_ID, today=lambda: date(2026, 10, 26))


def a_booking(**changes: object) -> Booking:
    values: dict[str, object] = {
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
    values.update(changes)
    return Booking.model_validate(values)


@pytest.fixture
def fake() -> Iterator[TestClient]:
    with TestClient(create_fake_datev()) as client:
        yield client


@pytest.fixture
def gateway(fake: TestClient) -> DatevConnectGateway:
    return DatevConnectGateway(fake, DEFAULT_CLIENT_ID)


@pytest.mark.req("BK-06")
def test_no_answer_means_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(DatevUnavailable):
        gateway_answering(refuse).list_vendors()


@pytest.mark.req("BK-06")
def test_a_server_error_means_unavailable() -> None:
    with pytest.raises(DatevUnavailable):
        gateway_answering(lambda request: httpx.Response(502)).submit_booking(a_booking())


@pytest.mark.req("NF-06")
def test_refused_credentials_are_an_error_not_a_missing_account() -> None:
    with pytest.raises(DatevError) as error:
        gateway_answering(lambda request: httpx.Response(401)).get_vendor(70001)

    assert not isinstance(error.value, DatevUnavailable)


@pytest.mark.req("BK-06")
@pytest.mark.parametrize(
    "answer, reason",
    [
        ({"error": {"code": "period_closed", "message": "closed"}}, RejectionReason.PERIOD_CLOSED),
        ({"error": {"code": "something_new", "message": "?"}}, RejectionReason.INVALID),
        ({"unexpected": True}, RejectionReason.INVALID),
    ],
)
def test_rejections_carry_a_reason(answer: object, reason: RejectionReason) -> None:
    gateway = gateway_answering(lambda request: httpx.Response(422, json=answer))

    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking())

    assert rejected.value.reason is reason


@pytest.mark.req("BK-06")
def test_a_rejection_without_json_is_invalid() -> None:
    gateway = gateway_answering(lambda request: httpx.Response(400, text="Bad Request"))

    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking())

    assert rejected.value.reason is RejectionReason.INVALID


@pytest.mark.req("BK-02")
@pytest.mark.parametrize(
    "account, kind, side",
    [
        (70001, BookingKind.INVOICE, "H"),
        (70001, BookingKind.CREDIT_NOTE, "S"),
        (10001, BookingKind.INVOICE, "S"),
        (10001, BookingKind.CREDIT_NOTE, "H"),
    ],
)
def test_debit_or_credit_follows_the_account_and_kind(
    account: int, kind: BookingKind, side: str
) -> None:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    booking = a_booking(account=account, kind=kind, original_document_number="X-1")
    with pytest.raises(DatevUnavailable):
        gateway_answering(record).submit_booking(booking)

    assert b'"debit_credit_identifier":"%s"' % side.encode() in sent[0].content


@pytest.mark.req("BK-02")
def test_bookings_go_to_the_fiscal_year_of_the_document_date() -> None:
    paths: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500)

    with pytest.raises(DatevUnavailable):
        gateway_answering(record).submit_booking(a_booking(document_date=date(2025, 12, 31)))

    assert "/fiscal-years/20250101/" in paths[0]


def test_fiscal_year_ids_are_the_first_day() -> None:
    assert fiscal_year_id(date(2026, 12, 31)) == "20260101"


@pytest.mark.parametrize("reference", ["", "000001", "2026:000001", "20260101:"])
def test_a_malformed_reference_is_refused(reference: str) -> None:
    gateway = gateway_answering(lambda request: httpx.Response(500))

    with pytest.raises(ValueError, match="booking reference"):
        gateway.get_booking(reference)


@pytest.mark.req("BK-06")
def test_a_closed_period_is_rejected(fake: TestClient, gateway: DatevConnectGateway) -> None:
    fake.put("/_fake/closed-until", json={"date": "2026-09-30"})

    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking(document_date=date(2026, 9, 30)))

    assert rejected.value.reason is RejectionReason.PERIOD_CLOSED
    assert gateway.submit_booking(a_booking(document_date=date(2026, 10, 1))).reference


@pytest.mark.req("BK-06")
def test_an_unknown_gl_account_is_rejected(gateway: DatevConnectGateway) -> None:
    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking(contra_account=99999))

    assert rejected.value.reason is RejectionReason.UNKNOWN_ACCOUNT


@pytest.mark.req("BK-06")
def test_an_outage_makes_datev_unavailable(fake: TestClient, gateway: DatevConnectGateway) -> None:
    fake.put("/_fake/outage", json={"down": True})

    with pytest.raises(DatevUnavailable):
        gateway.list_vendors()
    assert fake.get("/health").status_code == 200

    fake.put("/_fake/outage", json={"down": False})
    assert gateway.list_vendors()

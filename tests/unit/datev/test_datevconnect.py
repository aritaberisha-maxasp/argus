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
    BookingOutcomeUnknown,
    BookingRejected,
    DatevConnectGateway,
    DatevError,
    DatevUnavailable,
    RejectionReason,
)
from argus.datev.datevconnect import fiscal_year_id
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev

Handler = Callable[[httpx.Request], httpx.Response]


ACCOUNT = {"account_number": 70001, "caption": "Muster Bürobedarf GmbH", "bank_accounts": []}


def with_accounts(handler: Handler) -> Handler:
    """Answer account lookups with an existing account and hand everything else on."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/creditors/" in request.url.path:
            return httpx.Response(200, json=ACCOUNT)
        if request.method == "GET" and "/debitors/" in request.url.path:
            return httpx.Response(200, json={**ACCOUNT, "account_number": 10001})
        return handler(request)

    return answer


def gateway_answering(handler: Handler) -> DatevConnectGateway:
    transport = httpx.MockTransport(with_accounts(handler))
    http = httpx.Client(transport=transport, base_url="http://datev")
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
def test_a_server_error_on_a_read_means_unavailable() -> None:
    with pytest.raises(DatevUnavailable):
        gateway_answering(lambda request: httpx.Response(502)).list_vendors()


@pytest.mark.req("BK-06")
@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.ConnectTimeout("no route")]
)
def test_a_booking_that_never_left_is_unavailable(failure: httpx.TransportError) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(DatevUnavailable) as error:
        gateway_answering(fail).submit_booking(a_booking())

    assert not isinstance(error.value, BookingOutcomeUnknown)


@pytest.mark.req("BK-06", "IN-10")
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("no answer"),
        httpx.WriteTimeout("half sent"),
        httpx.RemoteProtocolError("connection dropped"),
    ],
)
def test_a_booking_whose_answer_got_lost_has_an_unknown_outcome(
    failure: httpx.TransportError,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(BookingOutcomeUnknown, match="look it up before sending it again"):
        gateway_answering(fail).submit_booking(a_booking())


@pytest.mark.req("BK-06", "IN-10")
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_after_a_booking_has_an_unknown_outcome(status: int) -> None:
    with pytest.raises(BookingOutcomeUnknown):
        gateway_answering(lambda request: httpx.Response(status)).submit_booking(a_booking())


@pytest.mark.req("VM-01", "VM-03", "BK-06")
@pytest.mark.parametrize("account, resource", [(70099, "creditors"), (10099, "debitors")])
def test_a_booking_on_a_missing_account_is_never_sent(account: int, resource: str) -> None:
    sent: list[httpx.Request] = []

    def datev_that_accepts_anything(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"error": {"code": "account_not_found"}})
        return httpx.Response(201, json={"id": "000001", "accounting_records": []})

    transport = httpx.MockTransport(datev_that_accepts_anything)
    gateway = DatevConnectGateway(
        httpx.Client(transport=transport, base_url="http://datev"), DEFAULT_CLIENT_ID
    )

    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking(account=account))

    assert rejected.value.reason is RejectionReason.UNKNOWN_ACCOUNT
    assert [(r.method, r.url.path.rsplit("/", 2)[-2:]) for r in sent] == [
        ("GET", [resource, str(account)])
    ]


@pytest.mark.req("NF-06")
def test_refused_credentials_are_an_error_not_a_missing_account() -> None:
    with pytest.raises(DatevError) as error:
        gateway_answering(lambda request: httpx.Response(401)).list_vendors()

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
    with pytest.raises(BookingOutcomeUnknown):
        gateway_answering(record).submit_booking(booking)

    assert b'"debit_credit_identifier":"%s"' % side.encode() in sent[-1].content


@pytest.mark.req("BK-02")
def test_bookings_go_to_the_fiscal_year_of_the_document_date() -> None:
    paths: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500)

    with pytest.raises(BookingOutcomeUnknown):
        gateway_answering(record).submit_booking(a_booking(document_date=date(2025, 12, 31)))

    assert "/fiscal-years/20250101/" in paths[-1]


@pytest.mark.req("PR-04")
def test_history_is_read_one_fiscal_year_at_a_time() -> None:
    asked: list[tuple[str, str, str]] = []

    def record(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        asked.append((request.url.path.split("/")[-2], params["date_from"], params["date_to"]))
        return httpx.Response(200, json=[])

    gateway_answering(record).list_postings(70001, date(2024, 11, 1), date(2026, 2, 15))

    assert asked == [
        ("20240101", "2024-11-01", "2024-12-31"),
        ("20250101", "2025-01-01", "2025-12-31"),
        ("20260101", "2026-01-01", "2026-02-15"),
    ]


@pytest.mark.req("PR-04")
def test_a_fiscal_year_that_is_not_set_up_holds_no_postings() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if "/fiscal-years/20240101/" in request.url.path:
            return httpx.Response(
                404, json={"error": {"code": "fiscal_year_not_found", "message": "no 2024"}}
            )
        return httpx.Response(200, json=[])

    assert gateway_answering(answer).list_postings(70001, date(2024, 1, 1), date(2026, 2, 15)) == []


@pytest.mark.req("PR-04")
@pytest.mark.parametrize("code", ["client_not_found", "something_else"])
def test_an_unknown_client_is_still_a_setup_problem(code: str) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": code, "message": "missing"}})

    with pytest.raises(DatevError):
        gateway_answering(answer).list_postings(70001, date(2024, 1, 1), date(2026, 2, 15))


@pytest.mark.req("PR-04")
def test_no_fiscal_year_set_up_at_all_is_a_setup_problem() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": "fiscal_year_not_found", "message": "none"}}
        )

    with pytest.raises(DatevError, match="no fiscal year"):
        gateway_answering(answer).list_postings(70001, date(2024, 1, 1), date(2026, 2, 15))


@pytest.mark.req("VM-03", "EX-01")
def test_a_wrong_client_is_an_error_not_a_missing_account(fake: TestClient) -> None:
    gateway = DatevConnectGateway(fake, "wrong-client")

    with pytest.raises(DatevError):
        gateway.get_vendor(70001)
    with pytest.raises(DatevError):
        gateway.get_customer(10001)
    with pytest.raises(DatevError):
        gateway.get_booking("20260101:000001")
    with pytest.raises(DatevError):
        gateway.submit_booking(a_booking())


@pytest.mark.req("VM-03", "EX-01")
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404),
        httpx.Response(404, text="Not Found"),
        httpx.Response(404, json={"error": {"code": "fiscal_year_not_found"}}),
    ],
)
def test_any_other_not_found_is_an_error(answer: httpx.Response) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return answer

    transport = httpx.MockTransport(respond)
    gateway = DatevConnectGateway(
        httpx.Client(transport=transport, base_url="http://datev"), DEFAULT_CLIENT_ID
    )

    with pytest.raises(DatevError):
        gateway.get_vendor(70001)
    with pytest.raises(DatevError):
        gateway.get_booking("20260101:000001")


@pytest.mark.req("BK-06", "IN-10")
def test_the_fake_refuses_a_repeated_document_number(gateway: DatevConnectGateway) -> None:
    """Fake only: real DATEV may only warn, so ARGUS checks itself (#41)."""
    gateway.submit_booking(a_booking())

    with pytest.raises(BookingRejected) as rejected:
        gateway.submit_booking(a_booking(case_id="case-2"))

    assert rejected.value.reason is RejectionReason.DUPLICATE_DOCUMENT_NUMBER


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

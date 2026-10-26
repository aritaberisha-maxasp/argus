"""The gateway has no way to create or change a vendor or customer account (VM-01, VM-04).

Three layers show it: the interface has no such operation, the HTTP client refuses
any request outside its allow list before it is sent, and the fake DATEV has no
route that writes accounts.
"""

import inspect

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.datev import DatevConnectGateway, DatevGateway, ForbiddenDatevOperation
from argus.datev.datevconnect import API_ROOT, is_allowed
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev

FY = f"{API_ROOT}/clients/{DEFAULT_CLIENT_ID}/fiscal-years/20260101"
READS = {
    "list_vendors",
    "get_vendor",
    "list_customers",
    "get_customer",
    "list_postings",
    "get_booking",
}
WRITES = {"submit_booking"}


def public_methods(cls: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls, inspect.isfunction) if name[0] != "_"}


@pytest.mark.req("VM-01", "VM-04", "NF-04")
def test_the_interface_only_reads_and_writes_bookings() -> None:
    assert public_methods(DatevGateway) == READS | WRITES


@pytest.mark.req("VM-01", "VM-04", "NF-04")
def test_the_http_gateway_offers_nothing_beyond_the_interface() -> None:
    assert public_methods(DatevConnectGateway) == READS | WRITES


@pytest.mark.req("VM-01", "VM-04")
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("resource", ["creditors", "creditors/70001", "debitors", "debitors/10001"])
def test_account_writes_are_not_on_the_allow_list(method: str, resource: str) -> None:
    assert not is_allowed(method, f"{FY}/{resource}")


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", f"{API_ROOT}/clients"),
        ("POST", f"{FY}/payments"),
        ("POST", "/datev/api/master-data/v1/addressees"),
        ("PUT", f"{FY}/accounting-sequences/000001"),
        ("DELETE", f"{FY}/accounting-sequences/000001"),
        ("GET", f"{FY}/creditors/../../other"),
    ],
)
def test_anything_else_is_not_on_the_allow_list(method: str, path: str) -> None:
    assert not is_allowed(method, path)


@pytest.mark.req("VM-01", "VM-04")
def test_a_forbidden_request_is_never_sent() -> None:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(record)
    gateway = DatevConnectGateway(
        httpx.Client(transport=transport, base_url="http://datev"), DEFAULT_CLIENT_ID
    )

    with pytest.raises(ForbiddenDatevOperation):
        gateway._send("POST", f"{FY}/creditors", json={"caption": "New vendor"})

    assert sent == []


@pytest.mark.req("VM-01", "VM-04")
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("resource", ["creditors", "creditors/70001", "debitors/10001"])
def test_the_fake_datev_refuses_account_writes(method: str, resource: str) -> None:
    with TestClient(create_fake_datev()) as fake:
        response = fake.request(method, f"{FY}/{resource}", json={"caption": "Changed"})

        assert response.status_code == 405
        assert fake.get(f"{FY}/creditors/70001").json()["caption"] == "Muster Bürobedarf GmbH"

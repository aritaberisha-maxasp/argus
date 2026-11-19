"""The fake DATEV itself: its data and its test controls."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from argus.datev.datevconnect import API_ROOT
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev, load_fixtures

FY = f"{API_ROOT}/clients/{DEFAULT_CLIENT_ID}/fiscal-years/20260101"
RECORD = {
    "date": "2026-10-01",
    "document_field1": "BB-2026-1187",
    "account_number": 70001,
    "contra_account_number": 4930,
    "tax_key": "9",
    "debit_credit_identifier": "H",
    "amount": "119.00",
    "net_amount": "100.00",
    "tax_amount": "19.00",
}


@pytest.fixture
def fake() -> Iterator[TestClient]:
    with TestClient(create_fake_datev()) as client:
        yield client


@pytest.mark.req("VM-02", "AR-03")
def test_serves_every_fixture_vendor_and_customer() -> None:
    data = load_fixtures()

    assert sorted(data.creditors) == list(range(70001, 70009))
    assert sorted(data.debitors) == [10001, 10002, 10003, 10004]
    assert all(c["bank_accounts"][0]["iban"] for c in data.creditors.values())


def test_records_bookings_and_reset_forgets_them(fake: TestClient) -> None:
    created = fake.post(f"{FY}/accounting-sequences", json={"accounting_records": [RECORD]})

    assert created.status_code == 201
    assert [b["id"] for b in fake.get("/_fake/bookings").json()] == [created.json()["id"]]

    fake.post("/_fake/reset")

    assert fake.get("/_fake/bookings").json() == []
    retry = fake.post(f"{FY}/accounting-sequences", json={"accounting_records": [RECORD]})
    assert retry.status_code == 201


@pytest.mark.req("VM-03")
def test_a_vendor_created_by_hand_is_served_until_reset(fake: TestClient) -> None:
    """Stands in for Benjamin creating a vendor in DATEV (#35). Not a DATEVconnect route."""
    creditor = {
        "account_number": 70099,
        "caption": "Beispiel Neu GmbH",
        "eu_vat_id_number": "DE012345699",
        "bank_accounts": [{"iban": "DE02000000000000700099"}],
    }

    assert fake.get(f"{FY}/creditors/70099").status_code == 404
    assert fake.post("/_fake/creditors", json=creditor).status_code == 201
    assert fake.post("/_fake/creditors", json=creditor).status_code == 409
    assert fake.get(f"{FY}/creditors/70099").json()["caption"] == "Beispiel Neu GmbH"
    assert 70099 in [c["account_number"] for c in fake.get(f"{FY}/creditors").json()]

    fake.post("/_fake/reset")

    assert fake.get(f"{FY}/creditors/70099").status_code == 404


def test_an_unknown_client_is_not_found(fake: TestClient) -> None:
    other = f"{API_ROOT}/clients/someone-else/fiscal-years/20260101/creditors"

    assert fake.get(other).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"accounting_records": []},
        {"accounting_records": [RECORD, RECORD]},
        {"accounting_records": [{**RECORD, "amount": "abc"}]},
        {"accounting_records": [{k: v for k, v in RECORD.items() if k != "date"}]},
    ],
)
def test_malformed_bookings_are_invalid(fake: TestClient, body: dict[str, object]) -> None:
    response = fake.post(f"{FY}/accounting-sequences", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


def test_a_booking_outside_the_fiscal_year_is_invalid(fake: TestClient) -> None:
    record = {**RECORD, "date": "2025-12-31"}

    response = fake.post(f"{FY}/accounting-sequences", json={"accounting_records": [record]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


def test_a_negative_amount_is_invalid(fake: TestClient) -> None:
    record = {**RECORD, "amount": "-119.00"}

    response = fake.post(f"{FY}/accounting-sequences", json={"accounting_records": [record]})

    assert response.status_code == 422

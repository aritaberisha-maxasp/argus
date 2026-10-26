"""The Wise client prepares a payment run (PY-08) and keeps the token safe (NF-06)."""

import logging

import httpx
import pytest
from pydantic import SecretStr

from argus.secrets import EnvironmentSecretStore, MissingSecretError
from argus.wise import WiseApiError, WiseClient
from argus.wise.client import masked_headers
from standins.wise.app import create_app
from tests.unit.wise_helpers import BASE_URL, PROFILE_ID, TOKEN, client_for, standin_transport

IBAN = "DE02100100100006820101"  # synthetic test IBAN


@pytest.fixture
def standin_and_client() -> tuple[object, WiseClient]:
    app = create_app(token=TOKEN)
    return app.state.records, client_for(standin_transport(app))


@pytest.mark.req("PY-08", "NF-04")
def test_a_run_is_handed_over_as_a_completed_unfunded_batch_group(
    standin_and_client: tuple[object, WiseClient],
) -> None:
    records, client = standin_and_client
    recipient = client.create_recipient(
        {
            "currency": "EUR",
            "type": "iban",
            "accountHolderName": "Muster Bürobedarf GmbH",
            "details": {"legalType": "BUSINESS", "iban": IBAN},
        }
    )
    assert client.get_recipient(recipient["id"])["details"]["iban"] == IBAN

    group = client.create_batch_group("ARGUS 2026-11-13 EUR", "EUR")
    quote = client.create_quote("EUR", "EUR", "1190.00")
    transfer = client.add_transfer(
        group["id"], recipient["id"], quote["id"], "attempt-0001", "RE-2026-0412 RE-2026-0415"
    )
    again = client.add_transfer(
        group["id"], recipient["id"], quote["id"], "attempt-0001", "RE-2026-0412 RE-2026-0415"
    )
    assert again["id"] == transfer["id"], "a retry must not create a second transfer"

    current = client.get_batch_group(group["id"])
    done = client.complete_batch_group(group["id"], current["version"])

    assert done["status"] == "COMPLETED"
    assert client.get_transfer(transfer["id"])["status"] == "incoming_payment_waiting"
    assert [g["id"] for g in client.list_batch_groups()] == [group["id"]]
    assert len(records.transfers) == 1  # type: ignore[attr-defined]


@pytest.mark.req("PY-08")
def test_nothing_can_be_added_after_completion(
    standin_and_client: tuple[object, WiseClient],
) -> None:
    _, client = standin_and_client
    recipient = client.create_recipient({"currency": "USD", "type": "aba"})
    group = client.create_batch_group("ARGUS 2026-11-13 EUR", "EUR")
    client.complete_batch_group(group["id"], group["version"])
    quote = client.create_quote("EUR", "USD", "500.00")
    with pytest.raises(WiseApiError) as error:
        client.add_transfer(group["id"], recipient["id"], quote["id"], "attempt-2", "INV-1")
    assert error.value.status == 422
    assert error.value.codes == ["batch_group_not_open"]


@pytest.mark.req("BK-04")
def test_rate_is_read_from_wise(standin_and_client: tuple[object, WiseClient]) -> None:
    _, client = standin_and_client
    rate = client.get_rate("USD", "EUR", at="2026-10-15T10:00:00Z")
    assert rate["source"] == "USD"
    assert rate["target"] == "EUR"
    assert rate["time"] == "2026-10-15T10:00:00Z"
    assert 0 < rate["rate"] < 1


@pytest.mark.req("NF-06")
def test_the_token_comes_from_the_secret_store() -> None:
    store = EnvironmentSecretStore({"WISE_API_TOKEN": TOKEN})
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"id": 1})

    client = WiseClient.from_secret_store(store, PROFILE_ID, BASE_URL)
    client._http._transport._inner = httpx.MockTransport(answer)  # type: ignore[attr-defined]
    client.get_transfer(1)
    assert seen == [f"Bearer {TOKEN}"]


@pytest.mark.req("NF-06")
def test_a_missing_token_is_named_but_nothing_is_guessed() -> None:
    with pytest.raises(MissingSecretError, match="WISE_API_TOKEN"):
        WiseClient.from_secret_store(EnvironmentSecretStore({}), PROFILE_ID, BASE_URL)


@pytest.mark.req("NF-06")
def test_the_token_is_never_logged_or_shown(
    standin_and_client: tuple[object, WiseClient], caplog: pytest.LogCaptureFixture
) -> None:
    _, client = standin_and_client
    caplog.set_level(logging.DEBUG, logger="argus.wise")
    client.get_rate("GBP", "EUR")
    with pytest.raises(WiseApiError) as error:
        client.get_transfer(1)

    assert "Bearer ****" in caplog.text
    shown = [caplog.text, repr(client), str(error.value), repr(SecretStr(TOKEN))]
    assert not any(TOKEN in text for text in shown)


@pytest.mark.req("NF-06")
def test_masked_headers_hide_only_the_authorization_value() -> None:
    masked = masked_headers({"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    assert masked == {"Authorization": "Bearer ****", "Accept": "application/json"}


@pytest.mark.req("NF-06")
def test_a_wrong_token_is_rejected_by_the_standin() -> None:
    app = create_app(token=TOKEN)
    client = WiseClient(SecretStr("wrong"), PROFILE_ID, BASE_URL, transport=standin_transport(app))
    with pytest.raises(WiseApiError) as error:
        client.get_rate("EUR", "USD")
    assert error.value.status == 401


@pytest.mark.req("NF-06")
def test_errors_keep_only_wise_error_codes_not_bank_details() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = {"errors": [{"code": "invalid_iban", "message": f"IBAN {IBAN} is not valid"}]}
        return httpx.Response(422, json=body)

    client = client_for(httpx.MockTransport(answer))
    with pytest.raises(WiseApiError) as error:
        client.create_recipient({"currency": "EUR"})
    assert error.value.codes == ["invalid_iban"]
    assert IBAN not in str(error.value)


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("url", ["http://api.wise.com", "ftp://api.wise.com"])
def test_plain_http_is_only_allowed_for_a_local_standin(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        WiseClient(SecretStr(TOKEN), PROFILE_ID, url)
    WiseClient(SecretStr(TOKEN), PROFILE_ID, "http://localhost:8080").close()

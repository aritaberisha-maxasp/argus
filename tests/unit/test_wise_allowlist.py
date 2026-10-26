"""The Wise client refuses every call that is not on the allowlist (ADR 0004, NF-04)."""

import json

import httpx
import pytest

from argus.wise import WiseCallRefused
from tests.unit.wise_helpers import PROFILE_ID, client_for

GROUP = "3f0a6c1e-8b7d-4e2a-9c51-0d2b7e6a4f90"
P = f"/v3/profiles/{PROFILE_ID}"

# The funding calls named in ADR 0004 and the Wise spike, plus their neighbours.
FUNDING_CALLS = [
    ("POST", f"{P}/batch-payments/{GROUP}/payments"),
    ("POST", f"{P}/batch-groups/{GROUP}/payment-initiations"),
    ("POST", f"{P}/transfers/70000001/payments"),
    ("POST", "/v1/transfers/70000001/payments"),
    ("PUT", f"{P}/batch-payments/{GROUP}/payments"),
]

OTHER_REFUSED_CALLS = [
    # editing or deleting a recipient
    ("PATCH", "/v1/accounts/70000001"),
    ("PUT", "/v1/accounts/70000001"),
    ("DELETE", "/v1/accounts/70000001"),
    # webhook subscriptions are not approved yet
    ("POST", f"{P}/subscriptions"),
    # balances, statements, conversions, cancelling transfers
    ("GET", f"/v1/profiles/{PROFILE_ID}/balance-statements/1/statement.json"),
    ("POST", f"/v2/profiles/{PROFILE_ID}/balance-movements"),
    ("PUT", "/v1/transfers/70000001/cancel"),
    # an allowed path with the wrong method
    ("DELETE", f"{P}/batch-groups/{GROUP}"),
    ("GET", f"{P}/quotes"),
    # paths dressed up to look allowed
    ("POST", f"{P}/batch-groups/../batch-payments/{GROUP}/payments"),
    ("POST", f"{P}/batch-groups/{GROUP}/transfers/"),
    ("POST", f"{P}/batch-groups/{GROUP}%2Fpayments"),
    ("POST", f"{P}//batch-groups"),
    ("GET", "/v1/accounts/abc"),
    ("POST", f"{P}/batch-groups/{GROUP.upper()}/transfers"),
    # another Wise profile
    ("POST", "/v3/profiles/99/batch-groups"),
]


class Recorder(httpx.BaseTransport):
    """Counts requests that got past the guard. A refused call must never get here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})


@pytest.mark.req("NF-04", "PY-08")
@pytest.mark.parametrize(("method", "path"), FUNDING_CALLS)
def test_funding_calls_never_leave_argus(method: str, path: str) -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused):
        client._send(method, path, {"type": "BALANCE"})
    assert recorder.requests == []


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(("method", "path"), OTHER_REFUSED_CALLS)
def test_calls_off_the_allowlist_are_refused(method: str, path: str) -> None:
    recorder = Recorder()
    with pytest.raises(WiseCallRefused):
        client_for(recorder)._send(method, path, {})
    assert recorder.requests == []


@pytest.mark.req("NF-04")
def test_the_guard_also_stops_code_that_reaches_past_the_client() -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused):
        client._http.post(f"{P}/batch-payments/{GROUP}/payments", json={"type": "BALANCE"})
    assert recorder.requests == []


@pytest.mark.req("NF-04", "NF-06")
def test_the_token_is_never_sent_to_another_host() -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused):
        client._http.get("https://elsewhere.example/v1/rates")
    assert recorder.requests == []


@pytest.mark.req("NF-04", "PY-08")
@pytest.mark.parametrize(
    "body",
    [
        {"status": "CANCELLED", "version": 1},
        {"status": "COMPLETED", "version": 1, "fund": True},
        {"version": 1},
        "COMPLETED",
    ],
)
def test_a_batch_group_can_only_be_marked_completed(body: object) -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused):
        client._http.patch(f"{P}/batch-groups/{GROUP}", content=json.dumps(body))
    assert recorder.requests == []


@pytest.mark.req("PY-08", "NF-04")
def test_every_method_on_the_client_passes_the_allowlist() -> None:
    recorder = Recorder()
    client = client_for(recorder)
    client.create_recipient({"currency": "EUR"})
    client.get_recipient(70000001)
    client.create_quote("EUR", "EUR", "100.00")
    client.list_batch_groups()
    client.create_batch_group("ARGUS 2026-11-13 EUR", "EUR")
    client.get_batch_group(GROUP)
    client.add_transfer(GROUP, 70000001, GROUP, "attempt-1", "R-2026-0001")
    client.complete_batch_group(GROUP, 3)
    client.get_transfer(70000001)
    client._send("GET", "/v1/rates", params={"source": "USD", "target": "EUR"})
    assert len(recorder.requests) == 10


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    "body",
    [
        {"currency": "EUR", "profile": 999},
        {"currency": "EUR", "profile": str(PROFILE_ID)},
        {"currency": "EUR", "profile": True},
        {"currency": "EUR"},
        [PROFILE_ID],
    ],
)
def test_a_recipient_for_another_profile_is_refused(body: object) -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused):
        client._http.post("/v1/accounts", content=json.dumps(body))
    assert recorder.requests == []


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    ("path", "raw_body"),
    [
        ("/v1/accounts", f'{{"currency": "EUR", "profile": 2, "profile": {PROFILE_ID}}}'),
        (
            f"{P}/batch-groups/{GROUP}",
            '{"status": "CANCELLED", "status": "COMPLETED", "version": 1}',
        ),
        (f"{P}/batch-groups/{GROUP}", '{"status": "COMPLETED", "version": 1, "version": 2}'),
    ],
)
def test_a_body_with_a_repeated_key_is_refused(path: str, raw_body: str) -> None:
    recorder = Recorder()
    client = client_for(recorder)
    method = "POST" if path == "/v1/accounts" else "PATCH"
    with pytest.raises(WiseCallRefused):
        client._http.request(method, path, content=raw_body)
    assert recorder.requests == []


@pytest.mark.req("NF-04")
def test_a_write_call_with_a_query_string_is_refused() -> None:
    recorder = Recorder()
    client = client_for(recorder)
    with pytest.raises(WiseCallRefused, match="query string"):
        client._http.post(f"{P}/batch-groups", params={"fund": "true"}, json={"name": "x"})
    assert recorder.requests == []


@pytest.mark.req("NF-04")
def test_a_refused_call_is_logged_without_its_body(caplog: pytest.LogCaptureFixture) -> None:
    client = client_for(Recorder())
    with pytest.raises(WiseCallRefused):
        client._send("POST", f"{P}/transfers/70000001/payments", {"secret": "body-text"})
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [f"Refused Wise call POST {P}/transfers/70000001/payments"]
    assert "body-text" not in caplog.text

"""The Wise client ARGUS uses to prepare payment runs (ADR 0004, PY-08, NF-04, NF-06).

It can create recipients, quotes, batch groups and transfers, complete a batch group
and read things back. It cannot fund or release anything: every request passes the
allowlist in ``argus.wise.allowlist`` inside the HTTP transport, so even code that
reaches past the methods below to the underlying HTTP client is stopped.
"""

import logging
import time
from collections.abc import Generator, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from argus.secrets import SecretStore
from argus.wise.allowlist import WiseCallRefused, check_call

log = logging.getLogger("argus.wise")

PRODUCTION_URL = "https://api.wise.com"
TOKEN_SECRET = "WISE_API_TOKEN"  # noqa: S105  the name of the secret, not a secret

JsonObject = dict[str, Any]


def masked_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers safe to log: the Authorization value is replaced."""
    return {
        name: "Bearer ****" if name.lower() == "authorization" else value
        for name, value in headers.items()
    }


class WiseApiError(RuntimeError):
    """Wise answered with an error. Holds Wise's error codes, never the request body."""

    def __init__(self, method: str, path: str, status: int, codes: list[str]) -> None:
        self.status = status
        self.codes = codes
        super().__init__(f"Wise {method} {path} failed with {status}: {', '.join(codes) or '-'}")


class _BearerToken(httpx.Auth):
    """Adds the token to each request, so it is not kept as a plain string in the client."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        yield request


class _GuardedTransport(httpx.BaseTransport):
    """Checks every outgoing request against the allowlist and the Wise host."""

    def __init__(self, inner: httpx.BaseTransport, base_url: httpx.URL, profile_id: int) -> None:
        self._inner = inner
        self._origin = (base_url.scheme, base_url.host, base_url.port)
        self._profile_id = profile_id

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path, _, query = url.raw_path.decode("ascii").partition("?")
        try:
            if (url.scheme, url.host, url.port) != self._origin:
                raise WiseCallRefused(f"refused Wise call to another host: {url.host}")
            if query and request.method != "GET":
                raise WiseCallRefused(f"refused Wise call {request.method} {path}: query string")
            check_call(request.method, path, request.read(), self._profile_id)
        except WiseCallRefused:
            # Some code tried a call ARGUS must never make. Method and path only, no body.
            log.warning("Refused Wise call %s %s", request.method, path)
            raise
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class WiseClient:
    """Talks to Wise, or to the Wise stand in, for one Wise profile."""

    def __init__(
        self,
        token: SecretStr,
        profile_id: int,
        base_url: str = PRODUCTION_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        standin: bool = False,
    ) -> None:
        """``standin=True`` allows plain http, for the Wise stand in only, never for Wise."""
        if urlsplit(base_url).scheme != "https" and not standin:
            raise ValueError("the Wise base URL must use https unless it is the stand in")
        self.profile_id = profile_id
        url = httpx.URL(base_url)
        guarded = _GuardedTransport(transport or httpx.HTTPTransport(), url, profile_id)
        self._http = httpx.Client(
            base_url=url,
            transport=guarded,
            timeout=timeout,
            follow_redirects=False,
            auth=_BearerToken(token),
        )

    @classmethod
    def from_secret_store(
        cls, secrets: SecretStore, profile_id: int, base_url: str = PRODUCTION_URL
    ) -> "WiseClient":
        return cls(secrets.get(TOKEN_SECRET), profile_id, base_url)

    def __repr__(self) -> str:
        return f"WiseClient(base_url={str(self._http.base_url)!r}, profile_id={self.profile_id})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "WiseClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # Recipients

    def create_recipient(self, recipient: JsonObject) -> JsonObject:
        return self._object("POST", "/v1/accounts", {**recipient, "profile": self.profile_id})

    def get_recipient(self, account_id: int) -> JsonObject:
        return self._object("GET", f"/v1/accounts/{account_id}")

    # Quotes and batch groups

    def create_quote(self, source: str, target: str, target_amount: str) -> JsonObject:
        body = {"sourceCurrency": source, "targetCurrency": target, "targetAmount": target_amount}
        return self._object("POST", f"{self._profile}/quotes", body)

    def create_batch_group(self, name: str, source_currency: str) -> JsonObject:
        body = {"name": name, "sourceCurrency": source_currency}
        return self._object("POST", f"{self._profile}/batch-groups", body)

    def get_batch_group(self, batch_group_id: str) -> JsonObject:
        return self._object("GET", f"{self._profile}/batch-groups/{batch_group_id}")

    def list_batch_groups(self) -> list[JsonObject]:
        return cast(list[JsonObject], self._send("GET", f"{self._profile}/batch-groups"))

    def add_transfer(
        self,
        batch_group_id: str,
        recipient_id: int,
        quote_id: str,
        customer_transaction_id: str,
        reference: str,
    ) -> JsonObject:
        body = {
            "targetAccount": recipient_id,
            "quoteUuid": quote_id,
            "customerTransactionId": customer_transaction_id,
            "details": {"reference": reference},
        }
        path = f"{self._profile}/batch-groups/{batch_group_id}/transfers"
        return self._object("POST", path, body)

    def complete_batch_group(self, batch_group_id: str, version: int) -> JsonObject:
        """Close the batch group for changes. Benjamin funds it in the Wise web app."""
        body = {"status": "COMPLETED", "version": version}
        return self._object("PATCH", f"{self._profile}/batch-groups/{batch_group_id}", body)

    # Status and rates

    def get_transfer(self, transfer_id: int) -> JsonObject:
        return self._object("GET", f"/v1/transfers/{transfer_id}")

    def get_rate(self, source: str, target: str, at: str | None = None) -> JsonObject:
        params = {"source": source, "target": target} | ({"time": at} if at else {})
        rates = cast(list[JsonObject], self._send("GET", "/v1/rates", params=params))
        if len(rates) != 1:
            raise WiseApiError("GET", "/v1/rates", 200, ["expected exactly one rate"])
        return rates[0]

    @property
    def _profile(self) -> str:
        return f"/v3/profiles/{self.profile_id}"

    def _object(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        return cast(JsonObject, self._send(method, path, body))

    def _send(
        self,
        method: str,
        path: str,
        body: JsonObject | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        started = time.monotonic()
        response = self._http.request(method, path, json=body, params=params)
        log.info(
            "Wise %s %s answered %s in %.0f ms",
            method,
            path,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        log.debug("Wise request headers: %s", masked_headers(response.request.headers))
        if not response.is_success:
            raise WiseApiError(method, path, response.status_code, _error_codes(response))
        return response.json()


def _error_codes(response: httpx.Response) -> list[str]:
    """Wise's error codes only. Error bodies can echo bank details, so nothing else is kept."""
    try:
        errors = response.json().get("errors", [])
        return [str(error["code"]) for error in errors if "code" in error]
    except (ValueError, AttributeError, TypeError):
        return []

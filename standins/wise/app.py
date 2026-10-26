"""A Wise stand in for development and CI (ADR 0004).

It answers the calls on ARGUS's Wise allowlist the way the real API does and records
every recipient, quote, transfer and batch group it receives, so tests can check what
ARGUS handed over. Like Wise for a German personal token, it has no way to fund or
release anything: there is no funding endpoint, so such a call gets a 404.

All data is synthetic and kept in memory. ``GET /_standin/records`` shows it and
``POST /_standin/reset`` clears it.
"""

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

JsonObject = dict[str, Any]

# Synthetic rates: units of target currency per one EUR.
_PER_EUR = {"EUR": Decimal(1), "USD": Decimal("1.0850"), "GBP": Decimal("0.8450")}


class Records:
    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.recipients: dict[int, JsonObject] = {}
        self.quotes: dict[str, JsonObject] = {}
        self.batch_groups: dict[str, JsonObject] = {}
        self.transfers: dict[int, JsonObject] = {}
        self._ids = count(70_000_001)

    def next_id(self) -> int:
        return next(self._ids)

    def as_json(self) -> JsonObject:
        return {
            "recipients": list(self.recipients.values()),
            "quotes": list(self.quotes.values()),
            "batchGroups": list(self.batch_groups.values()),
            "transfers": list(self.transfers.values()),
        }


class WiseError(Exception):
    """An error in Wise's shape: ``{"errors": [{"code": ...}]}``."""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code


def _error(status: int, code: str) -> WiseError:
    return WiseError(status, code)


def _rate(source: str, target: str) -> Decimal:
    if source not in _PER_EUR or target not in _PER_EUR:
        raise _error(422, "unsupported_currency")
    return (_PER_EUR[target] / _PER_EUR[source]).quantize(Decimal("0.0001"))


def create_app(token: str | None = None) -> FastAPI:
    """The stand in. ``token`` is the bearer token it accepts; any token if not set."""
    app = FastAPI(title="Wise stand in")
    records = Records()
    app.state.records = records
    expected = token if token is not None else os.environ.get("WISE_STANDIN_TOKEN")

    def authorised(authorization: Annotated[str, Header()] = "") -> None:
        given = authorization.removeprefix("Bearer ")
        if not authorization.startswith("Bearer ") or not given:
            raise _error(401, "unauthorized")
        if expected and given != expected:
            raise _error(401, "unauthorized")

    auth = [Depends(authorised)]

    @app.exception_handler(WiseError)
    def wise_error(_: Request, exc: WiseError) -> JSONResponse:
        return JSONResponse({"errors": [{"code": exc.code}]}, status_code=exc.status)

    def group(profile_id: int, batch_group_id: str) -> JsonObject:
        found = records.batch_groups.get(batch_group_id)
        if found is None or found["profileId"] != profile_id:
            raise _error(404, "batch_group_not_found")
        return found

    @app.get("/_standin/records")
    def show_records() -> JsonObject:
        return records.as_json()

    @app.post("/_standin/reset")
    def reset() -> None:
        records.clear()

    @app.post("/v1/accounts", dependencies=auth)
    def create_recipient(body: JsonObject) -> JsonObject:
        recipient = {"id": records.next_id(), "active": True, **body}
        records.recipients[recipient["id"]] = recipient
        return recipient

    @app.get("/v1/accounts/{account_id}", dependencies=auth)
    def get_recipient(account_id: int) -> JsonObject:
        if account_id not in records.recipients:
            raise _error(404, "recipient_not_found")
        return records.recipients[account_id]

    @app.post("/v3/profiles/{profile_id}/quotes", dependencies=auth)
    def create_quote(profile_id: int, body: JsonObject) -> JsonObject:
        rate = _rate(body["sourceCurrency"], body["targetCurrency"])
        target_amount = Decimal(str(body["targetAmount"]))
        quote = {
            "id": str(uuid.uuid4()),
            "profile": profile_id,
            "sourceCurrency": body["sourceCurrency"],
            "targetCurrency": body["targetCurrency"],
            "targetAmount": float(target_amount),
            "sourceAmount": float((target_amount / rate).quantize(Decimal("0.01"))),
            "rate": float(rate),
        }
        records.quotes[quote["id"]] = quote
        return quote

    @app.get("/v3/profiles/{profile_id}/batch-groups", dependencies=auth)
    def list_batch_groups(profile_id: int) -> list[JsonObject]:
        return [g for g in records.batch_groups.values() if g["profileId"] == profile_id]

    @app.post("/v3/profiles/{profile_id}/batch-groups", dependencies=auth)
    def create_batch_group(profile_id: int, body: JsonObject) -> JsonObject:
        batch_group = {
            "id": str(uuid.uuid4()),
            "version": 0,
            "profileId": profile_id,
            "name": body["name"],
            "sourceCurrency": body["sourceCurrency"],
            "status": "NEW",
            "transferIds": [],
        }
        records.batch_groups[batch_group["id"]] = batch_group
        return batch_group

    @app.get("/v3/profiles/{profile_id}/batch-groups/{batch_group_id}", dependencies=auth)
    def get_batch_group(profile_id: int, batch_group_id: str) -> JsonObject:
        return group(profile_id, batch_group_id)

    @app.patch("/v3/profiles/{profile_id}/batch-groups/{batch_group_id}", dependencies=auth)
    def update_batch_group(profile_id: int, batch_group_id: str, body: JsonObject) -> JsonObject:
        found = group(profile_id, batch_group_id)
        if body.get("version") != found["version"]:
            raise _error(409, "version_conflict")
        if body.get("status") != "COMPLETED" or found["status"] != "NEW":
            raise _error(422, "invalid_status_change")
        found["status"] = "COMPLETED"
        found["version"] += 1
        return found

    @app.post(
        "/v3/profiles/{profile_id}/batch-groups/{batch_group_id}/transfers", dependencies=auth
    )
    def add_transfer(profile_id: int, batch_group_id: str, body: JsonObject) -> JsonObject:
        found = group(profile_id, batch_group_id)
        for transfer in records.transfers.values():
            if transfer["customerTransactionId"] == body["customerTransactionId"]:
                return transfer  # Wise is idempotent on customerTransactionId
        if found["status"] != "NEW":
            raise _error(422, "batch_group_not_open")
        if body["targetAccount"] not in records.recipients:
            raise _error(422, "recipient_not_found")
        quote = records.quotes.get(body["quoteUuid"])
        if quote is None:
            raise _error(422, "quote_not_found")
        if quote["sourceCurrency"] != found["sourceCurrency"]:
            raise _error(422, "source_currency_mismatch")
        transfer = {
            "id": records.next_id(),
            "user": profile_id,
            "targetAccount": body["targetAccount"],
            "quoteUuid": quote["id"],
            "customerTransactionId": body["customerTransactionId"],
            "status": "incoming_payment_waiting",
            "reference": body.get("details", {}).get("reference", ""),
            "rate": quote["rate"],
            "sourceCurrency": quote["sourceCurrency"],
            "sourceValue": quote["sourceAmount"],
            "targetCurrency": quote["targetCurrency"],
            "targetValue": quote["targetAmount"],
            "created": datetime.now(UTC).isoformat(),
        }
        records.transfers[transfer["id"]] = transfer
        found["transferIds"].append(transfer["id"])
        found["version"] += 1
        return transfer

    @app.get("/v1/transfers/{transfer_id}", dependencies=auth)
    def get_transfer(transfer_id: int) -> JsonObject:
        if transfer_id not in records.transfers:
            raise _error(404, "transfer_not_found")
        return records.transfers[transfer_id]

    @app.get("/v1/rates", dependencies=auth)
    def get_rates(
        source: Annotated[str, Query()],
        target: Annotated[str, Query()],
        time: Annotated[str | None, Query()] = None,
    ) -> list[JsonObject]:
        at = time or datetime.now(UTC).isoformat()
        return [
            {"rate": float(_rate(source, target)), "source": source, "target": target, "time": at}
        ]

    return app


app = create_app()

"""A fake DATEV for development and CI (ADR 0002). Never used in production.

It serves the same paths and JSON as DATEVconnect's accounting API, as far as the
gateway uses them, and fills itself from the synthetic fixtures: vendors and
customers from ``master-data/``, past postings from ``datev/postings.csv``.

Like DATEVconnect for ARGUS, it has no way to create or change an account: there
are only GET routes for creditors and debitors.

The routes under ``/_fake`` exist only here. Tests use them to make the fake fail
the way DATEV fails: a closed period or DATEV not answering. ``/_fake/creditors``
stands in for Benjamin creating a vendor account in DATEV by hand, so a test can show
that ARGUS carries on by itself once the vendor exists (#35, VM-03). ARGUS itself never
calls it: the gateway's allow list only knows the DATEVconnect paths.

Run it with ``uvicorn argus.datev.fake:app_from_env --factory`` or with Docker,
see docs/design/datev-gateway.md.
"""

import csv
import os
import threading
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from argus.datev.datevconnect import API_ROOT, fiscal_year_id

DEFAULT_CLIENT_ID = "muster-gmbh"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


@dataclass
class FakeDatevData:
    creditors: dict[int, dict[str, Any]]
    debitors: dict[int, dict[str, Any]]
    postings: list[dict[str, Any]]


@dataclass
class _State:
    seed: FakeDatevData
    closed_until: date | None = None
    down: bool = False
    sequences: dict[str, dict[str, Any]] = field(default_factory=dict)
    postings: list[dict[str, Any]] = field(default_factory=list)
    # Vendor accounts a test created "by hand", as Benjamin does in DATEV (#35).
    added_creditors: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def creditors(self) -> dict[int, dict[str, Any]]:
        return {**self.seed.creditors, **self.added_creditors}

    def reset(self) -> None:
        self.closed_until = None
        self.down = False
        self.added_creditors.clear()
        self.sequences.clear()
        self.postings = [dict(p) for p in self.seed.postings]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_fixtures(data_dir: Path = DEFAULT_DATA_DIR) -> FakeDatevData:
    """Turn the fixture CSV files into the JSON DATEVconnect would serve."""
    creditors = {}
    for row in _read_csv(data_dir / "master-data" / "vendors.csv"):
        number = int(row["vendor_account"])
        creditors[number] = {
            "id": str(number),
            "account_number": number,
            "caption": row["name"],
            "eu_vat_id_number": row["vat_id"],
            "country": row["country"],
            "bank_accounts": [{"iban": row["iban"], "currency_code": row["currency"]}],
        }
    debitors = {}
    for row in _read_csv(data_dir / "master-data" / "customers.csv"):
        number = int(row["customer_account"])
        debitors[number] = {
            "id": str(number),
            "account_number": number,
            "caption": row["name"],
            "eu_vat_id_number": row["vat_id"],
            "country": row["country"],
            "bank_accounts": [],
        }
    postings = [
        {
            "account_number": int(row["account"]),
            "date": row["date"],
            "document_field1": row["document_number"],
            "contra_account_number": int(row["contra_account"]),
            "tax_key": row["tax_key"],
            "amount": row["gross"],
            "posting_description": row["description"],
        }
        for row in _read_csv(data_dir / "datev" / "postings.csv")
    ]
    return FakeDatevData(creditors, debitors, postings)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _not_found(code: str, what: str) -> JSONResponse:
    return _error(404, code, f"{what} not found")


class _ClosedUntil(BaseModel):
    date: date | None


class _Outage(BaseModel):
    down: bool


class _BankAccount(BaseModel):
    iban: str
    currency_code: str = "EUR"


class _NewCreditor(BaseModel):
    """A vendor account as Benjamin would create it in DATEV, in DATEVconnect's shape."""

    account_number: int
    caption: str
    eu_vat_id_number: str | None = None
    country: str | None = None
    bank_accounts: list[_BankAccount] = []


def create_fake_datev(
    data: FakeDatevData | None = None, client_id: str = DEFAULT_CLIENT_ID
) -> FastAPI:
    state = _State(seed=data or load_fixtures())
    state.reset()
    lock = threading.Lock()
    app = FastAPI(title="Fake DATEV", description=__doc__)
    app.state.fake = state
    base = f"{API_ROOT}/clients/{{client}}/fiscal-years/{{fiscal_year}}"

    @app.middleware("http")
    async def outage(request: Request, call_next: Any) -> Any:
        if state.down and request.url.path.startswith(API_ROOT):
            return _error(503, "unavailable", "DATEVconnect is not answering")
        return await call_next(request)

    def unknown_client(client: str) -> JSONResponse | None:
        return None if client == client_id else _not_found("client_not_found", f"client {client}")

    def all_accounts() -> dict[int, dict[str, Any]]:
        return {**state.creditors, **state.seed.debitors}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{base}/creditors", response_model=None)
    def creditors(client: str, fiscal_year: str) -> Any:
        return unknown_client(client) or list(state.creditors.values())

    @app.get(f"{base}/creditors/{{number}}", response_model=None)
    def creditor(client: str, fiscal_year: str, number: int) -> Any:
        found = state.creditors.get(number)
        return (
            unknown_client(client) or found or _not_found("account_not_found", f"creditor {number}")
        )

    @app.get(f"{base}/debitors", response_model=None)
    def debitors(client: str, fiscal_year: str) -> Any:
        return unknown_client(client) or list(state.seed.debitors.values())

    @app.get(f"{base}/debitors/{{number}}", response_model=None)
    def debitor(client: str, fiscal_year: str, number: int) -> Any:
        found = state.seed.debitors.get(number)
        return (
            unknown_client(client) or found or _not_found("account_not_found", f"debitor {number}")
        )

    @app.get(f"{base}/account-postings", response_model=None)
    def account_postings(
        client: str, fiscal_year: str, account_number: int, date_from: date, date_to: date
    ) -> Any:
        return unknown_client(client) or [
            p
            for p in state.postings
            if p["account_number"] == account_number
            and date_from <= date.fromisoformat(p["date"]) <= date_to
        ]

    @app.post(f"{base}/accounting-sequences", response_model=None)
    def create_sequence(client: str, fiscal_year: str, body: dict[str, Any]) -> Any:
        with lock:
            return _create_sequence(client, fiscal_year, body)

    def _create_sequence(client: str, fiscal_year: str, body: dict[str, Any]) -> Any:
        if problem := unknown_client(client):
            return problem
        records = body.get("accounting_records")
        if not isinstance(records, list) or len(records) != 1:
            return _error(400, "invalid", "send exactly one accounting record")
        record = records[0]
        try:
            day = date.fromisoformat(record["date"])
            account = int(record["account_number"])
            contra = int(record["contra_account_number"])
            gross = Decimal(record["amount"])
            document = str(record["document_field1"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return _error(400, "invalid", "accounting record is incomplete or malformed")
        if fiscal_year_id(day) != fiscal_year:
            return _error(422, "invalid", f"date {day} is not in fiscal year {fiscal_year}")
        if state.closed_until and day <= state.closed_until:
            return _error(422, "period_closed", f"the period up to {state.closed_until} is closed")
        if account not in all_accounts():
            return _error(422, "unknown_account", f"account {account} does not exist")
        if not 1000 <= contra <= 9999:
            return _error(422, "unknown_account", f"G/L account {contra} does not exist")
        if gross <= 0:
            return _error(422, "invalid", "amount must be positive")
        if any(
            p["account_number"] == account and p["document_field1"] == document
            for p in state.postings
        ):
            return _error(
                409,
                "duplicate_document_number",
                f"document number {document} is already booked on account {account}",
            )
        sequence_id = f"{len(state.sequences) + 1:06d}"
        sequence = {
            "id": sequence_id,
            "fiscal_year": fiscal_year,
            "is_committed": False,
            "accounting_records": [record],
        }
        state.sequences[f"{fiscal_year}:{sequence_id}"] = sequence
        state.postings.append(
            {
                "account_number": account,
                "date": record["date"],
                "document_field1": document,
                "contra_account_number": contra,
                "tax_key": record.get("tax_key", ""),
                "amount": record["amount"],
                "posting_description": record.get("posting_description", ""),
            }
        )
        return JSONResponse(status_code=201, content=sequence)

    @app.get(f"{base}/accounting-sequences/{{sequence_id}}", response_model=None)
    def get_sequence(client: str, fiscal_year: str, sequence_id: str) -> Any:
        found = state.sequences.get(f"{fiscal_year}:{sequence_id}")
        return (
            unknown_client(client)
            or found
            or _not_found("booking_not_found", f"sequence {sequence_id}")
        )

    # Controls for tests, not part of DATEVconnect

    @app.post("/_fake/reset")
    def reset() -> dict[str, str]:
        state.reset()
        return {"status": "reset"}

    @app.put("/_fake/closed-until")
    def close_period(body: _ClosedUntil) -> _ClosedUntil:
        state.closed_until = body.date
        return body

    @app.put("/_fake/outage")
    def set_outage(body: _Outage) -> _Outage:
        state.down = body.down
        return body

    @app.post("/_fake/creditors", response_model=None)
    def add_creditor(body: _NewCreditor) -> Any:
        with lock:
            if body.account_number in all_accounts():
                return _error(409, "account_exists", f"account {body.account_number} exists")
            creditor = {"id": str(body.account_number), **body.model_dump()}
            state.added_creditors[body.account_number] = creditor
            return JSONResponse(status_code=201, content=creditor)

    @app.get("/_fake/bookings")
    def bookings() -> list[dict[str, Any]]:
        return list(state.sequences.values())

    return app


def app_from_env() -> FastAPI:
    """Entry point for uvicorn and Docker. Reads FAKE_DATEV_DATA and FAKE_DATEV_CLIENT."""
    data_dir = Path(os.environ.get("FAKE_DATEV_DATA", DEFAULT_DATA_DIR))
    client_id = os.environ.get("FAKE_DATEV_CLIENT", DEFAULT_CLIENT_ID)
    return create_fake_datev(load_fixtures(data_dir), client_id)

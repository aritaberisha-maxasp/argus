"""Smoke test of the Wise client against the stand in running in Docker (ADR 0004).

CI starts the container and runs this from the repository root:
``python tools/smoke_wise_standin.py http://localhost:8080 <token>``
"""

import sys

import httpx
from pydantic import SecretStr

from argus.wise import WiseCallRefused, WiseClient

PROFILE_ID = 16_000_001


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        raise SystemExit(1)


def main(base_url: str, token: str) -> None:
    with WiseClient(SecretStr(token), PROFILE_ID, base_url, standin=True) as wise:
        recipient = wise.create_recipient(
            {
                "currency": "EUR",
                "type": "iban",
                "accountHolderName": "Muster Bürobedarf GmbH",
                "details": {"legalType": "BUSINESS", "iban": "DE02100100100006820101"},
            }
        )
        group = wise.create_batch_group("ARGUS smoke test EUR", "EUR")
        quote = wise.create_quote("EUR", "EUR", "250.00")
        transfer = wise.add_transfer(group["id"], recipient["id"], quote["id"], "smoke-1", "RE-1")
        group = wise.get_batch_group(group["id"])
        done = wise.complete_batch_group(group["id"], group["version"])
        check(done["status"] == "COMPLETED", "batch group handed over as COMPLETED")
        status = wise.get_transfer(transfer["id"])["status"]
        check(status == "incoming_payment_waiting", "transfer waits for funding by a person")

        funding = f"/v3/profiles/{PROFILE_ID}/batch-payments/{group['id']}/payments"
        try:
            wise._send("POST", funding, {"type": "BALANCE"})
            check(False, "client refuses the funding call")
        except WiseCallRefused:
            check(True, "client refuses the funding call")

    headers = {"Authorization": f"Bearer {token}"}
    answer = httpx.post(f"{base_url}{funding}", headers=headers)
    check(answer.status_code == 404, "stand in has no funding endpoint")
    records = httpx.get(f"{base_url}/_standin/records").json()
    counts = {name: len(items) for name, items in records.items()}
    check(
        counts == {"recipients": 1, "quotes": 1, "batchGroups": 1, "transfers": 1},
        f"stand in recorded {counts}",
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

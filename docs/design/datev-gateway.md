# The DATEV gateway and the fake DATEV

Issue: #33. Decision: ADR 0002 (on PR #12, provisional until Max ASP answers the DATEV questions on #2).
Requirements: NF-06, VM-01, VM-04, and the reading and writing behind PR-04, PR-05, VM-02, VM-03, AR-03, BK-01 to BK-07, AR-04 to AR-08.

## What it is

ARGUS reaches DATEV in exactly one way: the `DatevGateway` in [src/argus/datev/](../../src/argus/datev/). Nothing else in ARGUS calls DATEV.

The gateway can:

| Operation | What it gives back | Used for |
| --- | --- | --- |
| `list_vendors()`, `get_vendor(number)` | Vendor accounts (Kreditoren) with name, VAT ID and bank accounts, or `None` when the account is missing | PR-02, PR-05, VM-02 to VM-04 |
| `list_customers()`, `get_customer(number)` | Customer accounts (Debitoren), or `None` | AR-03 |
| `list_postings(account, start, end)` | Past bookings on an account, across fiscal years | PR-04 |
| `submit_booking(booking)` | A booking reference and status, or a `BookingRejected` with a reason | BK-01 to BK-07, AR-04 to AR-08 |
| `get_booking(reference)` | The booking DATEV holds under that reference | BK-07 |

It cannot create or change a vendor or customer account, and it cannot reach anything that has to do with payments. Three things make sure of that, and each one has a test:

1. The interface has no such operation.
2. The HTTP client checks every request against a short allow list before sending it. Anything else raises `ForbiddenDatevOperation` and never leaves ARGUS.
3. The fake DATEV has no route that writes accounts, so a test that tries gets HTTP 405.

The bridge at Max ASP (ADR 0002) will carry the same allow list, so the rule holds even if ARGUS had a bug.

## When DATEV says no

| Error | Meaning | What the caller does |
| --- | --- | --- |
| `BookingRejected(PERIOD_CLOSED)` | The booking date is in a closed period | Exception queue (BK-06) |
| `BookingRejected(UNKNOWN_ACCOUNT)` | The vendor, customer or G/L account does not exist | Exception queue (BK-06, VM-03) |
| `BookingRejected(DUPLICATE_DOCUMENT_NUMBER)` | That document number is already booked on the account | Exception queue (BK-06) |
| `BookingRejected(INVALID)` | Anything else DATEV refused | Exception queue (BK-06) |
| `DatevUnavailable` | DATEV did not answer or answered with a server error | Try again later |
| `DatevError` | For example, DATEV refused the credentials | Alert, this is a setup problem |

A booking that cannot be right never reaches DATEV: the `Booking` model refuses a gross amount that is not net plus VAT, a credit note that does not name its invoice, a foreign currency without its original amount, and a service period that ends before it starts.

Bookings land as **uncommitted** in DATEV. Someone at Max ASP commits them there (question 6 on #2).

## The fake DATEV

The fake serves the same paths and JSON as DATEVconnect's accounting API, as far as the gateway uses them. It fills itself from the synthetic fixtures:

- vendors from `tests/fixtures/master-data/vendors.csv`, including their IBANs
- customers from `tests/fixtures/master-data/customers.csv`
- past postings from `tests/fixtures/datev/postings.csv`

It records every booking and refuses the same things DATEV refuses: an unknown account, a closed period, a duplicate document number, a date outside the fiscal year.

It also has a few controls that only exist in the fake, for tests:

| Route | What it does |
| --- | --- |
| `PUT /_fake/closed-until` with `{"date": "2026-09-30"}` | Closes every period up to that date |
| `PUT /_fake/outage` with `{"down": true}` | DATEV stops answering (HTTP 503) |
| `GET /_fake/bookings` | Every booking the fake has recorded |
| `POST /_fake/reset` | Back to the fixtures, no bookings |

### Run it

With Docker, from the repository root:

```bash
docker build -f docker/fake-datev/Dockerfile -t argus-fake-datev .
docker run --rm -p 58452:58452 argus-fake-datev
```

Without Docker:

```bash
.venv/bin/uvicorn argus.datev.fake:app_from_env --factory --port 58452
```

The client ID is `muster-gmbh`. For example, the vendors are at
`http://localhost:58452/datev/api/accounting/v1/clients/muster-gmbh/fiscal-years/20260101/creditors`.

## The contract tests

[tests/contract/datev/](../../tests/contract/datev/) holds the tests every DATEV behind the gateway must pass. They run:

- in every `pytest` run, against an in-process fake
- in CI, against the fake running in Docker (job "DATEV contract tests on the Docker fake")
- later, against Max ASP's DATEV test client through the bridge

To point them at another DATEV, set two environment variables:

```bash
ARGUS_DATEV_CONTRACT_URL=https://bridge.example ARGUS_DATEV_CONTRACT_CLIENT=<client id> .venv/bin/pytest tests/contract
```

That DATEV must hold the fixture vendors, customers and postings. The tests only add bookings with fresh document numbers, so they can run again and again, but they do write bookings: never point them at Max ASP's real books.

When the real DATEV answers differently from the fake, fix the fake and the gateway first, so the fake never drifts from the real thing.

## What is provisional

The paths and field names follow public DATEVconnect material, not DATEV's own API specification (question 9 on #2). They live in two files only, `datevconnect.py` and `fake.py`. Also provisional:

- where a vendor's IBAN sits (we read it from the creditor record)
- that one booking is sent as one accounting sequence (Buchungsstapel)
- that a booking reference is `<fiscal year>:<sequence id>`
- that Max ASP's fiscal year runs January to December
- how the bridge signs in; the gateway sends no credentials yet

Each of these changes in one place once Benjamin's answers or the API specification arrive.

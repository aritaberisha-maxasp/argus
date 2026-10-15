# 0003: Hand payment runs to Wise as unfunded batch groups

Status: proposed
Requirements: PY-07, PY-08, PY-11, BK-04, AR-05, BA-01, BA-08, NF-04, NF-06

## Context

Twice a month ARGUS must hand approved invoices to Wise as a batch in the right currency, without funding or releasing it (PY-08, NF-04). It also needs the payment status (PY-11), Wise's exchange rate (BK-04, AR-05) and a monthly statement of every balance (BA-01, BA-08). Only interfaces Wise supports are allowed (NF-06). Max ASP is a German company with one Wise Business account. Details are in the [spike report](../spikes/wise-payments.md).

## Options

1. **Personal API token on a dedicated Wise team member.** Supported by Wise for small businesses automating their own account. Covers recipients, quotes, transfers, batch groups, transfer status and rates. For German accounts Wise refuses funding and statements through this token. Con: no statement through the API.
2. **Wise partner access (OAuth).** Would open more endpoints, possibly statements. Con: built for platforms with many customers, needs a contract with Wise, and also opens funding, which we do not want.
3. **CSV batch upload in the Wise web app.** ARGUS writes Wise's batch template and a person uploads it. No API needed. Con: a manual upload each run, no status back, and PY-08 asks for the API.

## Decision

Option 1.

- Benjamin creates a Wise team member "ARGUS" that may set up payments, with approval required for all its payments. That user creates a full access personal token, which Arita stores as a secret.
- Per run and source currency ARGUS creates one batch group, adds one transfer per vendor payment (with its own payment ID as `customerTransactionId`) and marks the group `COMPLETED`. Then it stops. Benjamin funds the batch in the Wise web app.
- The ARGUS Wise client only knows an allowlist of endpoints. A CI test fails if a funding endpoint appears anywhere in the code.
- ARGUS polls the status of open transfers and maps it to the case. A webhook can come later.
- ARGUS takes exchange rates from `GET /rates` and stores rate and timestamp with the booking.
- The bank import accepts a statement file (CAMT.053 or CSV) from any source. Until Wise allows statements through the API for Max ASP, Benjamin downloads them once a month. We ask Wise whether they can enable it.

## Consequences

- NF-04 holds in three places: Wise refuses funding for this token, Wise approval rules stop any payment by the ARGUS user, and the code has no funding path.
- Benjamin must fund each batch soon after hand over. Wise cancels unfunded transfers after some days, and ARGUS has to put those invoices back into the pool.
- BA-01 is not fully automatic yet. That needs Arita's agreement, and maybe a change to the requirement.
- Development and CI use a Wise stand in, because the build environment cannot reach Wise and the sandbox does not reproduce German limits or live rates.
- If the token leaks, an attacker can create recipients and unfunded transfers but cannot pay them. Arita can revoke the token in the Wise web app at once.

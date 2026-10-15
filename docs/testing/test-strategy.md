# ARGUS test strategy

Covers: IN-02, IN-03, IN-04, NF-02, AC-1 to AC-8

This is how QA proves that ARGUS does what the [requirements](../requirements/requirements.md) say. It is short on purpose. Details for each story go on the story's issue as test cases.

## Principles

- Tests come from the requirement text and the acceptance criteria, not from the code.
- Only synthetic data. The fixture library is in [tests/fixtures/](../../tests/fixtures/README.md). Nothing real goes in a test, a log or a screenshot.
- Every test names the requirement IDs it covers, so we can see which requirements have no test yet.
- ARGUS must never release a payment (NF-04). Every test that touches Wise also checks that no release happened.

## Test levels

| Level | What it checks | Where it runs |
|---|---|---|
| Unit | One business rule on its own: amounts and VAT, the EUR 5,000 limit, payment run dates, due date selection, working days, IBAN comparison, duplicate and number gap detection. Edge cases, not only the happy path. | Every push, in CI |
| Component | One part of ARGUS with the outside systems replaced by stand-ins: a fake mailbox fed from the `.eml` fixtures, a DATEV stand-in that records bookings, a Wise stand-in that records batches and has no way to release them. | Every push, in CI |
| Contract | Our stand-ins behave like the real Microsoft 365, DATEV and Wise interfaces. Built once the spikes (#2, #3, #4) tell us which interfaces we use. | Started by hand against test accounts that Arita provides. Never in CI with real credentials. |
| Acceptance | AC-1 to AC-8 end to end: fixture emails go in, bookings, payment batches and the audit log come out. | Before every sprint review. Results go into the review. |
| Exploratory | QA tries edge cases on each PR that the automated tests do not cover yet. | On every PR, evidence posted on the PR |

## How each acceptance criterion is proven

| AC | What we run | Fixtures | Passes when |
|---|---|---|---|
| AC-1 | All 50 emails go through intake. We compare the route ARGUS picks with `expected_route` in the manifest. | `emails/` and `manifest.csv` | At least 48 of 50 routes are correct (95 %). An ambiguous email counts as correct only if it goes to manual review (IN-09). We also report every invoice that was sent to the deletion folder, because a lost invoice is the most expensive mistake. |
| AC-2 | E01 is approved through the approval stand-in. | E01, `vendors.csv`, `approvers.csv` | The DATEV stand-in holds one booking on vendor account 70001 with G/L account, tax key, amounts from `expected.json` and the invoice PDF attached as voucher. The expected G/L account and tax key need Max ASP's chart of accounts (see open questions). |
| AC-3 | E05 (EUR 8,925.00 gross) is approved by the vendor's approver, then by the second approver. Unit tests cover EUR 5,000.00 and EUR 5,000.01 gross. | E05, `roles.csv` | Nothing is booked after the first approval. The booking appears only after the second approval. |
| AC-4 | A test clock is set to a run date with approved invoices in the pool. We also test 15 Nov 2026, which is a Sunday, so the run must happen on Friday 13 Nov (PY-03). | Approved invoices from the fixtures | The Wise stand-in receives one batch with the invoices PY-04 to PY-07 select, one transfer per vendor net of credit notes, no direct debit invoices (PY-13), and no funding or release call. |
| AC-5 | E12 (unknown vendor) and E06 (changed IBAN) go through intake. | E06, E12, `vendors.csv` | Both cases are in the exception queue with the right reason. Neither is sent for approval or added to a payment run. |
| AC-6 | E17, E18 and E19 go through intake. | E17 to E19, `customers.csv` | Each is booked on the right customer account (10001, 10002, 10003) with the right tax treatment: 19 % VAT, EU reverse charge, not taxable. E22 (unknown customer) goes to the exception queue. |
| AC-7 | A monthly Wise CSV is imported. | Not built yet, see below | Balances per currency reconcile, outgoing payments from AC-4 and the incoming payment for MA-2026-0101 are cleared, a partial payment leaves the rest open. |
| AC-8 | For every case from AC-1 to AC-7 we read the audit log by case. | Output of the other tests | Every case shows each step from receipt to its last status (NF-05) with time and actor, without gaps. |

## Tagging requirement IDs in tests

- Every test names the requirement IDs it covers. If we use Python and pytest, that is a marker: `@pytest.mark.req("IN-04", "IN-09")`. In another stack the ID goes into the test name, for example `[IN-04] reminder is forwarded`. The final form is fixed together with the tech stack (#6) and the project skeleton (#8).
- A small script will list every requirement ID from the catalog with the tests that cover it, so a requirement without a test shows up. It is built once the skeleton exists.
- The fixtures carry requirement IDs too: see the `requirements` column in `manifest.csv`.

## Evidence on PRs

For every PR QA posts on the PR: what was run, the command, the result in a few lines, and one sample input with its output. For acceptance tests QA also posts the result per AC on the sprint review.

## Open questions

1. **Customer questions about an invoice.** E44 is a customer asking about one of Max ASP's invoices. Under IN-04 that is "Other", which IN-06 removes. Asked Arita on issue #9 whether such emails should go to manual review instead.
2. **Chart of accounts.** AC-2 needs the G/L accounts and tax keys Max ASP uses in DATEV (SKR03 or SKR04). This comes with the DATEV spike (#2).
3. **E-invoice validation.** The XRechnung and ZUGFeRD fixtures follow the standards but have not been run through an official validator (KoSIT validator, a PDF/A-3 checker). We add that once the document spike (#5) picks the tooling.

## Not covered yet

- A Wise CSV export for AC-7. Its exact format comes from the Wise spike (#4).
- Approver replies (approve, reject, query) as fixtures for AP-03.
- A holiday calendar for Baden-Württemberg for PY-03 tests beyond weekends.

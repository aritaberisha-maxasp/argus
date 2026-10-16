# Spike: how ARGUS hands payment runs to Wise without releasing them

Issue: #4
Requirements: PY-08, PY-11, BK-04, AR-05, BA-01, BA-08, NF-04, NF-06 (PY-07, PY-12, BA-03 and BA-07 build on the same interface)
Status: findings from Wise's public API documentation and help centre, read on 15 Oct 2026. Nothing here was tried against Wise yet, because this build environment cannot reach Wise's servers. Points marked **to confirm** need a look at Max ASP's Wise account or a sandbox test.

## The short answer

- **Payment runs (PY-08): yes.** With a Wise personal API token, ARGUS can create recipients, quotes and transfers and group them into a batch group of up to 1,000 transfers. Wise then shows the batch in the web app, and a person funds it there. For a business based in Germany, Wise does not allow funding through a personal token at all, so ARGUS could not release a payment even if its code tried (NF-04).
- **Payment status (PY-11): yes.** ARGUS can read each transfer's status, by polling or by a webhook. `outgoing_payment_sent` means Wise has paid out.
- **Exchange rate (BK-04, AR-05): yes.** `GET /rates` works with a personal token and also returns the rate at a past moment.
- **Monthly statement (BA-01, BA-08): not through a personal token.** Wise does not allow retrieving balance statements through a personal token for a business outside the US, Canada, Australia, New Zealand, Singapore and Malaysia. This needs a decision from Arita (see the questions at the end).
- **Sandbox: partly.** Wise has a sandbox with simulated transfer states and balance top ups, but no live rates and no rate history, and its fallback profiles are UK ones, so it may not show the German restrictions. CI should run against a Wise stand in, like the DATEV one.

ADR [0003](../adr/0003-wise-payment-handover.md) proposes the approach.

## What ARGUS needs from Wise

| Need | Requirements | Direction | Possible with a personal token |
| --- | --- | --- | --- |
| Create a batch of transfers in the right currency, not funded | PY-08 | write | yes |
| Combine payments per vendor with all invoice numbers in the reference | PY-07 | write | yes, reference length **to confirm** per currency |
| Never fund or release a payment | NF-04 | must not write | yes, Wise refuses funding for German accounts |
| Know when a payment was executed, failed or came back | PY-11 | read | yes |
| Latest Wise exchange rate for a foreign currency invoice | BK-04, AR-05 | read | yes |
| Monthly statement of every balance, with opening and closing balance | BA-01, BA-08 | read | **no**, not for German accounts |
| Each transaction imported only once | BA-07 | read | statement reference numbers make this possible |

## How the API works

Base URLs: production `https://api.wise.com`, sandbox `https://api.wise-sandbox.com`. Authentication is a bearer token.

### Personal API token

- Created by a user in the Wise business account under Your account, Connect and manage apps, API tokens, with two step verification. It stays valid until somebody revokes it.
- Wise offers it with full or read only access. ARGUS needs full access to create transfers.
- Wise limits it to quotes, recipients, transfers, batch groups and tracking transfer events. Wise says: "Funding transfers and retrieving balance statements via API are not supported except for accounts based in the US, Canada, Australia, New Zealand, Singapore, and Malaysia."
- The token acts as the user who created it. So whatever that user may do in the web app matters too (see "Proof that ARGUS cannot release a payment").

### Creating a payment run (PY-08)

For each run and each source currency:

1. **Recipient.** `POST /v1/accounts` creates a recipient from the vendor's bank details, which ARGUS takes only from DATEV (VM-04). ARGUS stores the Wise recipient ID with the vendor and creates a new recipient when DATEV's IBAN changes, never edits one.
2. **Quote.** `POST /v3/profiles/{profileId}/quotes` with source currency, target currency (the invoice currency) and the target amount, so the vendor gets exactly the invoice amount.
3. **Batch group.** `POST /v3/profiles/{profileId}/batch-groups` with a name (for example `ARGUS 2026-11-13 EUR`) and the source currency. A batch group has one source currency, so a run that pays from two balances becomes two batch groups.
4. **Transfers.** `POST /v3/profiles/{profileId}/batch-groups/{batchGroupId}/transfers` for each vendor payment, with the recipient, the quote, the payment reference with all invoice numbers (PY-07) and a `customerTransactionId`. ARGUS uses its own payment ID as `customerTransactionId`, so a retry after a timeout cannot create the same transfer twice.
5. **Complete.** `PATCH /v3/profiles/{profileId}/batch-groups/{batchGroupId}` with status `COMPLETED`. After that nobody can add transfers to the batch, and Wise says: "Once your batch group is complete, log into your Wise.com account to fund the batch group."

ARGUS stops here. Each transfer now has the status `incoming_payment_waiting`. Benjamin opens Wise, checks the batch against the run summary (PY-09) and funds it. That is the manual release step PY-08 asks for.

The exact path versions (`v1`, `v3` or the new dated versions such as `2026Q4`) are **to confirm** in the sandbox, because Wise is moving its reference to dated versions.

### Funding endpoints ARGUS must never call

- `POST /v3/profiles/{profileId}/batch-payments/{batchGroupId}/payments` funds a batch group from a balance.
- `POST /v3/profiles/{profileId}/batch-groups/{batchGroupId}/payment-initiations` funds a batch group by direct debit.
- `POST /v3/profiles/{profileId}/transfers/{transferId}/payments` funds a single transfer.

### Payment status (PY-11)

Transfer statuses, in order: `incoming_payment_waiting`, `incoming_payment_initiated`, `processing`, `funds_converted`, `outgoing_payment_sent`. Other end states are `cancelled` (never funded), `funds_refunded` (funded, then cancelled), `bounced_back` and `charged_back`.

What ARGUS does with them:

| Wise status | ARGUS case |
| --- | --- |
| `incoming_payment_waiting` to `funds_converted` | stays "handed to Wise" |
| `outgoing_payment_sent` | marked paid (PY-11); clearing in DATEV still comes from the bank import (PY-12) |
| `cancelled` | back into the open pool for the next run, with an audit entry |
| `funds_refunded`, `bounced_back`, `charged_back` | exception queue (EX-01) |

Two ways to learn about changes:

- **Polling** `GET /v1/transfers/{transferId}` for open transfers, for example every hour. Simple and needs no public endpoint.
- **Webhook** `POST /v3/profiles/{profileId}/subscriptions` for the event `transfers#state-change`. Faster, but needs a public HTTPS endpoint and signature checks. Whether a personal token may create the subscription is **to confirm**; if not, it can be set up once in the Wise web app.

Proposal: start with polling, add the webhook later if it is worth it.

Wise cancels unfunded or unapproved transfers after a while (the help centre names 7 to 10 days for unapproved payments, depending on the currency). If Benjamin funds late, transfers can disappear and the quote rate may change at funding. ARGUS must notice a `cancelled` transfer and put the invoice back into the pool.

### Exchange rate (BK-04, AR-05)

`GET /rates?source=USD&target=EUR` returns the current rate as `rate`, `source`, `target` and `time`. Adding `time=2026-10-15T10:00:00` returns the rate at that moment, and `from`, `to` and `group=day` return a history. A personal token is accepted. ARGUS stores the rate, its timestamp and the original amount and currency with the booking. Whether this is the mid market rate is not stated in the reference; **to confirm**.

### Monthly statement (BA-01, BA-08)

The endpoint exists: `GET /v1/profiles/{profileId}/balance-statements/{balanceId}/statement.{format}` with `currency`, `intervalStart`, `intervalEnd` and `type=COMPACT`. Formats are JSON, CSV, PDF, XLSX, CAMT.053 (`.xml`), MT940 and QIF. One call covers one balance, so "all balances" means one call per balance. For profiles outside the six countries above it also needs strong customer authentication once every 90 days.

The problem: with a personal token a German business cannot call it at all. The ways out:

1. **Ask Wise for more access.** Wise's token page says to contact them for advanced access. If Wise enables statements for Max ASP's token, the 90 day authentication still needs Benjamin's phone, so it would be a quarterly prompt, not fully hands off. **To confirm** with Wise.
2. **Wise partner access (OAuth).** Meant for platforms serving many customers, needs an agreement with Wise. Too heavy for one company.
3. **A person downloads the statement.** Once a month Benjamin downloads the CAMT.053 or CSV statement of each balance in the Wise web app and sends it to a mailbox or folder ARGUS watches. CAMT.053 carries opening and closing balances, which BA-08 needs. This is one manual step, which BA-01 does not allow as written.
4. **Wise's own DATEV export.** Wise offers an accounting export for DATEV. It would skip ARGUS, so BA-03 to BA-06 (matching, rules, questions to Benjamin) would not happen. Not recommended.

Proposal: ask Wise about option 1 now, build the import so it accepts a file from either source, and run option 3 until Wise answers. This changes BA-01, so it is a question for Arita.

## Proof that ARGUS cannot release a payment (NF-04)

Three layers, each enough on its own:

1. **Wise refuses it.** For an account based in Germany, Wise does not support funding through a personal token. Even a bug that called a funding endpoint would get an error. **To confirm** once with the real token (see question 5).
2. **Wise approval rules.** Wise Business lets an admin require approval for all payments a team member sets up, by one or two approvers, and a team member can never approve their own payments. If the token belongs to a dedicated "ARGUS" team member with that rule, every ARGUS payment waits for Benjamin even outside the API. Whether approval rules also catch API created batch transfers is **to confirm** in Max ASP's account.
3. **ARGUS code.** The Wise client in ARGUS only knows an allowlist of endpoints (recipients, quotes, batch groups, transfers read, rates). A test in CI fails if any code contains a funding path (`/payments`, `/payment-initiations`, `batch-payments`). The compliance reviewer checks this on every Wise PR.

## Sandbox

- `https://wise-sandbox.com` for the web app, `https://api.wise-sandbox.com` for the API. The two step verification code there is always `111111`.
- It supports balances, conversions, quotes, transfers, batch groups and statements, plus simulation endpoints such as transfer state change, payout failure and balance top up.
- It does not support live rates, rate history (`from`, `to`, `group`) or real money. Its stable profiles are UK ones, so it may not reproduce the German limits on funding and statements.
- Whether anyone can sign up to the sandbox and create a personal token there is **to confirm**. Arita would hold that token as a secret.
- This build environment cannot reach Wise's hosts. Until the network policy allows `api.wise-sandbox.com`, development and CI use a Wise stand in that answers like the real API for the endpoints above.

## Questions for Arita

Posted on issue #4. On 16 Oct 2026 Arita said to go ahead with the assumptions below for now and revisit them when they matter.

Working assumptions: yes to 1, 2, 3 and 6, and the rate at booking time for 4. Question 5 stays open until the Wise token exists.

1. **Dedicated Wise user.** Can Benjamin add a Wise team member just for ARGUS, which may set up payments, with approval required for all its payments? (yes or no)
2. **Monthly statement.** Until Wise allows statements through the API, is it fine if Benjamin downloads each balance's statement once a month and sends it to ARGUS? (yes or no)
3. **Ask Wise.** May we draft a request to Wise support asking whether Max ASP's token can read balance statements? Arita or Benjamin would send it. (yes or no)
4. **Rate date.** For foreign currency invoices, should ARGUS use the Wise rate at the time of booking (as BK-04 reads) or on the invoice date? (booking or invoice)
5. **Funding test.** Once the token exists, may Arita run one check where ARGUS tries to fund a 1 EUR transfer to Max ASP's own account, expecting Wise to refuse? (yes or no)
6. **Paying balance.** Should invoices in a currency Max ASP holds (for example USD) be paid from that balance, and everything else from EUR? (yes or no)

## Sources

- [Personal API tokens](https://docs.wise.com/guides/developer/auth-and-security/personal-api-token)
- [Payouts for small and medium businesses](https://docs.wise.com/guides/product/send-money/use-cases/payouts-smbs)
- [Batch groups reference](https://docs.wise.com/api-reference/batch-group)
- [Transfer statuses](https://docs.wise.com/guides/product/send-money/tracking/transfer-statuses)
- [Webhook event types](https://docs.wise.com/guides/developer/webhooks/event-types)
- [Rates reference](https://docs.wise.com/api-reference/rate)
- [Balance statement reference](https://docs.wise.com/api-reference/legacy/balance-statement/balancestatementget)
- [Strong customer authentication over the API](https://docs.wise.com/guides/developer/auth-and-security/sca-over-api.md)
- [Sandbox and production environments](https://docs.wise.com/guides/developer/environments)
- [Simulation endpoints](https://docs.wise.com/api-reference/simulation)
- [How payment approvals work](https://wise.com/help/articles/4j9ugqMIeADd89uqy8VRMB/how-do-payment-approvals-work)
- [Guide to batch payments](https://www.wise.com/help/articles/2663240/guide-to-batch-payments)
- [Accounting exports](https://wise.com/help/articles/3eYyJYoIeAGrftknu4ImNC/using-accounting-exports-with-your-accounting-software)

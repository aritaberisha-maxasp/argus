# ARGUS product backlog

Covers: all requirement IDs (issue #10). The issues on GitHub are the source of truth. This page shows their order, the Sprint 1 proposal and what each story waits on. The PO keeps it up to date at every Sunday planning.

Requirement coverage is in [coverage.md](coverage.md). Open business questions are on #18; every story that depends on one uses the default written there until Arita answers.

## How the order was chosen

1. **Foundations first.** The case, the audit trail, the document store and the exception queue are used by every later story.
2. **Intake next.** Nothing else can happen until emails come in, get sorted and get read. This is also where the measurable acceptance criterion AC-1 lives.
3. **The two risky outside systems early.** The Wise client that cannot release payments (NF-04) and the DATEV gateway with its stand in come early, so later stories build on tested interfaces.
4. **Then the money path in order:** approval, booking, payment runs, outgoing invoices, bank import.
5. **Provisional stories.** Stories marked provisional depend on the DATEV spike (#2). Their order and details are revised when it closes. The stories on reading emails and invoices were revised after the document spike (#5) closed with ADR 0006. Stories that only need the stand ins can still start.

Sizes: S is about one night, M is two or three nights. Nothing is L.

## Proposed Sprint 1 (26 Oct to 1 Nov 2026)

**Sprint goal:** every email arriving at the accounts mailbox is picked up once, stored unaltered and recorded with its audit trail. The Wise client and the DATEV stand in are ready so payments and bookings can build on them.

**Stretch:** ranks 8 to 10 sort the emails, then file or forward them. They are in the list so threads have work if the core goes faster than planned.

| Rank | Issue | Story | Size | Depends on | Provisional | Stretch |
|---|---|---|---|---|---|---|
| 1 | [#19](https://github.com/aritaberisha-maxasp/argus/issues/19) | Keep every invoice and transaction as a case with a status and an audit trail | M | #7, #8 |  |  |
| 2 | [#20](https://github.com/aritaberisha-maxasp/argus/issues/20) | Store every received email and attachment unaltered with a fingerprint | S | #8, #19 |  |  |
| 3 | [#21](https://github.com/aritaberisha-maxasp/argus/issues/21) | Put every case ARGUS cannot handle into one exception queue with a reason | S | #19 |  |  |
| 4 | [#24](https://github.com/aritaberisha-maxasp/argus/issues/24) | Pick up every new email in the accounts mailbox | M | #19, #20 |  |  |
| 5 | [#22](https://github.com/aritaberisha-maxasp/argus/issues/22) | Know which days are working days in Baden-Württemberg | S | #8 |  |  |
| 6 | [#43](https://github.com/aritaberisha-maxasp/argus/issues/43) | Talk to Wise only through a client that cannot release payments | M | #8, #4 |  |  |
| 7 | [#33](https://github.com/aritaberisha-maxasp/argus/issues/33) | Give ARGUS one way to read and write DATEV, with a stand in for development | M | #8 | yes, #2 |  |
| 8 | [#25](https://github.com/aritaberisha-maxasp/argus/issues/25) | Sort each email into invoice, outgoing invoice, payment reminder or other | M | #24 |  | yes |
| 9 | [#26](https://github.com/aritaberisha-maxasp/argus/issues/26) | Move processed emails out of the inbox and remove the ones that are not needed | S | #25 |  | yes |
| 10 | [#27](https://github.com/aritaberisha-maxasp/argus/issues/27) | Forward payment reminders and dunning letters to Benjamin | S | #25 |  | yes |

Sprint 1 has seven shifts (five weeknights, Saturday and Sunday) and at most three threads at a time. The core (ranks 1 to 7) is about 13 nights of work. Its longest chain is case store, document store, then mailbox sweep: about six nights one after the other, plus review and Arita's merge between the steps. Sorting would add two or three more nights to that chain, which is why it is a stretch.

**Must happen before Monday 26 Oct for this list to work:**
- #7 (invoice case design, PR #17) and #8 (project skeleton and CI, PR #16) merged. Rank 1 and almost everything else build on them.
- #2 (DATEV spike, PR #12) merged for rank 7. Until then #33 can start from ADR 0002 on that PR's branch, because it only builds the gateway and a stand in.
- #4 (Wise spike) and #5 (document spike) are done: PR #14 and PR #55 are merged, so ranks 6 and 8 can start.

## Full backlog

| Rank | Issue | Story | Area | Size | Depends on | Provisional |
|---|---|---|---|---|---|---|
| 1 | [#19](https://github.com/aritaberisha-maxasp/argus/issues/19) | Keep every invoice and transaction as a case with a status and an audit trail | Foundation | M | #7, #8 |  |
| 2 | [#20](https://github.com/aritaberisha-maxasp/argus/issues/20) | Store every received email and attachment unaltered with a fingerprint | Foundation | S | #8, #19 |  |
| 3 | [#21](https://github.com/aritaberisha-maxasp/argus/issues/21) | Put every case ARGUS cannot handle into one exception queue with a reason | Foundation | S | #19 |  |
| 4 | [#24](https://github.com/aritaberisha-maxasp/argus/issues/24) | Pick up every new email in the accounts mailbox | Intake | M | #19, #20 |  |
| 5 | [#22](https://github.com/aritaberisha-maxasp/argus/issues/22) | Know which days are working days in Baden-Württemberg | Foundation | S | #8 |  |
| 6 | [#43](https://github.com/aritaberisha-maxasp/argus/issues/43) | Talk to Wise only through a client that cannot release payments | Booking | M | #8, #4 |  |
| 7 | [#33](https://github.com/aritaberisha-maxasp/argus/issues/33) | Give ARGUS one way to read and write DATEV, with a stand in for development | DATEV | M | #8 | yes, #2 |
| 8 | [#25](https://github.com/aritaberisha-maxasp/argus/issues/25) | Sort each email into invoice, outgoing invoice, payment reminder or other | Intake | M | #24 |  |
| 9 | [#26](https://github.com/aritaberisha-maxasp/argus/issues/26) | Move processed emails out of the inbox and remove the ones that are not needed | Intake | S | #25 |  |
| 10 | [#27](https://github.com/aritaberisha-maxasp/argus/issues/27) | Forward payment reminders and dunning letters to Benjamin | Intake | S | #25 |  |
| 11 | [#66](https://github.com/aritaberisha-maxasp/argus/issues/66) | Run the acceptance tests AC-1 to AC-8 end to end against the stand ins | Quality | M | #24 |  |
| 12 | [#29](https://github.com/aritaberisha-maxasp/argus/issues/29) | Read the invoice data from PDFs, images and scanned paper invoices | Intake | M | #24 |  |
| 13 | [#28](https://github.com/aritaberisha-maxasp/argus/issues/28) | Read XRechnung and ZUGFeRD e-invoices | Intake | M | #24 |  |
| 14 | [#70](https://github.com/aritaberisha-maxasp/argus/issues/70) | Trust a sender only when the email really comes from their domain | Intake | S | #25 |  |
| 15 | [#71](https://github.com/aritaberisha-maxasp/argus/issues/71) | Send doubtful emails to a person instead of routing them | Intake | M | #25, #29, #28, #70 |  |
| 16 | [#23](https://github.com/aritaberisha-maxasp/argus/issues/23) | Let approvers and Benjamin sign in with their Microsoft account and see only what their role allows | Foundation | M | #8 |  |
| 17 | [#72](https://github.com/aritaberisha-maxasp/argus/issues/72) | Run ARGUS for two weeks in shadow mode before it routes anything | Intake | S | #25, #26, #27, #23 |  |
| 18 | [#30](https://github.com/aritaberisha-maxasp/argus/issues/30) | Split an email with several invoices into one case per invoice | Intake | S | #29 |  |
| 19 | [#31](https://github.com/aritaberisha-maxasp/argus/issues/31) | Catch invoices that arrive twice | Intake | S | #29, #21 |  |
| 20 | [#32](https://github.com/aritaberisha-maxasp/argus/issues/32) | Check incoming invoices for the formal requirements of §14 UStG | Intake | S | #29, #21 |  |
| 21 | [#34](https://github.com/aritaberisha-maxasp/argus/issues/34) | Keep the approver, deputy and direct debit flag for each vendor | DATEV | M | #23, #33 |  |
| 22 | [#35](https://github.com/aritaberisha-maxasp/argus/issues/35) | Match each invoice to its vendor account in DATEV | DATEV | M | #33, #34, #29, #21 | yes, #2 |
| 23 | [#36](https://github.com/aritaberisha-maxasp/argus/issues/36) | Block invoices whose IBAN differs from the one in DATEV | DATEV | S | #35 |  |
| 24 | [#37](https://github.com/aritaberisha-maxasp/argus/issues/37) | Propose the G/L account and tax key from the vendor's booking history | DATEV | M | #35 | yes, #2 |
| 25 | [#38](https://github.com/aritaberisha-maxasp/argus/issues/38) | Let the vendor's approver approve, reject or query an invoice without DATEV or Wise | Approval | M | #23, #35, #37 |  |
| 26 | [#39](https://github.com/aritaberisha-maxasp/argus/issues/39) | Ask Benjamin for a second approval above EUR 5,000 | Approval | S | #38, #43 |  |
| 27 | [#40](https://github.com/aritaberisha-maxasp/argus/issues/40) | Remind approvers after 3 working days and escalate after 5 | Approval | S | #38, #22 |  |
| 28 | [#41](https://github.com/aritaberisha-maxasp/argus/issues/41) | Book approved invoices in DATEV automatically | Booking | M | #33, #38 | yes, #2 |
| 29 | [#42](https://github.com/aritaberisha-maxasp/argus/issues/42) | Attach the invoice image and XML to every DATEV booking | Booking | S | #41 | yes, #2 |
| 30 | [#61](https://github.com/aritaberisha-maxasp/argus/issues/61) | Send Benjamin a daily digest of open exceptions | Visibility | S | #21 |  |
| 31 | [#62](https://github.com/aritaberisha-maxasp/argus/issues/62) | Show the status of every invoice at any time | Visibility | M | #23, #21 |  |
| 32 | [#65](https://github.com/aritaberisha-maxasp/argus/issues/65) | Describe the Azure setup as code so Arita can deploy it | Operations | M | #8 |  |
| 33 | [#44](https://github.com/aritaberisha-maxasp/argus/issues/44) | Convert foreign currency invoices to EUR at the Wise rate | Booking | S | #43, #41 |  |
| 34 | [#45](https://github.com/aritaberisha-maxasp/argus/issues/45) | Book supplier credit notes and offset them in the next payment run | Booking | S | #41 |  |
| 35 | [#46](https://github.com/aritaberisha-maxasp/argus/issues/46) | Collect booked invoices in the payment pool and know the run dates | Payment runs | S | #22, #41 |  |
| 36 | [#47](https://github.com/aritaberisha-maxasp/argus/issues/47) | Choose which invoices go into each payment run | Payment runs | M | #46, #27 |  |
| 37 | [#48](https://github.com/aritaberisha-maxasp/argus/issues/48) | Pay each vendor once per run, net of credit notes | Payment runs | S | #47, #45 |  |
| 38 | [#49](https://github.com/aritaberisha-maxasp/argus/issues/49) | Send Benjamin a summary before each run is handed to Wise | Payment runs | S | #48 |  |
| 39 | [#50](https://github.com/aritaberisha-maxasp/argus/issues/50) | Hand each payment run to Wise as an unfunded batch | Payment runs | M | #49, #43, #36 |  |
| 40 | [#51](https://github.com/aritaberisha-maxasp/argus/issues/51) | Mark invoices as paid once Wise has sent the money | Payment runs | S | #50 |  |
| 41 | [#52](https://github.com/aritaberisha-maxasp/argus/issues/52) | Book outgoing invoices to the right customer account and tax treatment | Outgoing invoices | M | #33, #25, #29, #21, #42, #44 | yes, #2 |
| 42 | [#53](https://github.com/aritaberisha-maxasp/argus/issues/53) | Check outgoing invoice numbers for gaps and duplicates | Outgoing invoices | S | #52 |  |
| 43 | [#54](https://github.com/aritaberisha-maxasp/argus/issues/54) | Book outgoing credit notes and cancellations linked to the original invoice | Outgoing invoices | S | #52 |  |
| 44 | [#56](https://github.com/aritaberisha-maxasp/argus/issues/56) | Import the monthly Wise statement safely, once, with balances that add up | Bank import | M | #19, #20, #21 |  |
| 45 | [#57](https://github.com/aritaberisha-maxasp/argus/issues/57) | Book bank transactions and clear supplier payments | Bank import | M | #56, #33, #51 | yes, #2 |
| 46 | [#58](https://github.com/aritaberisha-maxasp/argus/issues/58) | Match customer payments to open invoices | Bank import | M | #57, #52 |  |
| 47 | [#59](https://github.com/aritaberisha-maxasp/argus/issues/59) | Book fees, conversions, own transfers, taxes and payroll by rule | Bank import | M | #57 |  |
| 48 | [#60](https://github.com/aritaberisha-maxasp/argus/issues/60) | Ask Benjamin about bank transactions ARGUS cannot match | Bank import | M | #59 |  |
| 49 | [#63](https://github.com/aritaberisha-maxasp/argus/issues/63) | Show the full trail of a case from receipt to payment | Visibility | S | #62 |  |
| 50 | [#64](https://github.com/aritaberisha-maxasp/argus/issues/64) | Measure how many invoices go through without manual work | Visibility | S | #62 |  |

## What changes when the DATEV spike closes

- **#2 DATEV.** If Max ASP cannot run DATEVconnect and the bridge, the DATEV gateway falls back to the DATEV booking file (ADR 0002). Then vendor matching, the IBAN check and the account proposal need a different data source, and a story for the bridge at Max ASP is added or dropped.


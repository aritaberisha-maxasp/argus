# ARGUS product backlog

Covers: all requirement IDs (issue #10). The issues on GitHub are the source of truth. This page shows their order, the Sprint 1 proposal and what each story waits on. The PO keeps it up to date at every Sunday planning.

Requirement coverage is in [coverage.md](coverage.md). Open business questions are on #18; every story that depends on one uses the default written there until Arita answers.

## How the order was chosen

1. **Foundations first.** The case, the audit trail, the document store and the exception queue are used by every later story.
2. **Intake next.** Nothing else can happen until emails come in, get sorted and get read. This is also where the measurable acceptance criterion AC-1 lives.
3. **The two risky outside systems early.** The Wise client that cannot release payments (NF-04) and the DATEV gateway with its stand in come early, so later stories build on tested interfaces.
4. **Then the money path in order:** approval, booking, payment runs, outgoing invoices, bank import.
5. **Provisional stories.** Stories marked provisional depend on the DATEV spike (#2). Their order and details are revised when it closes. The stories on reading emails and invoices were revised after the document spike (#5) closed with ADR 0006. Stories that only need the stand ins can still start.

6. **Azure and the database right after Sprint 1.** On 28 Oct 2026 Arita moved the Azure setup (#65), the deploy from CI (#81), database backups (#82) and the nightly audit copy (#83) up to ranks 14 to 17, so ARGUS runs on its real Azure database early. Deploys run only from CI, never by hand. Switching Wise to the sandbox and the real account (#84) sits just before the payment run hand over (#50).

Sizes: S is about one night, M is two or three nights. Nothing is L.

## Sprint 4 (16 to 22 Nov 2026, approved by Arita on 16 Nov)

**Sprint goal:** land the Sprint 3 PRs, then start the approval path on the assumed DATEV answers D1 to D9 (ADR 0002 provisional), so an invoice is matched to its vendor and its IBAN is checked.

Arita merged all waiting Sprint 3 PRs (154 to 162 and the retro PR 164) on the morning of 16 Nov, so new stories can start on the night of 16 Nov. Ranks 1 to 8 are core, 9 to 12 are stretch. #165 is action item 1 of the Sprint 3 retro. #165 to #170 are follow ups found while building and reviewing Sprint 3.

| Rank | Issue | Story | Requirement IDs | Size | Depends on | Stretch |
|---|---|---|---|---|---|---|
| 1 | [#165](https://github.com/aritaberisha-maxasp/argus/issues/165) | Check the whole merge list against main before Arita merges | none, team tooling | S | nothing |  |
| 2 | [#166](https://github.com/aritaberisha-maxasp/argus/issues/166) | Import a Wise statement as soon as Benjamin emails it, and send the daily reminder | BA-01, BA-07, BA-08 | S | #56 done |  |
| 3 | [#34](https://github.com/aritaberisha-maxasp/argus/issues/34) | Keep the approver, deputy and direct debit flag for each vendor | AP-01, AP-06, PY-13, VM-02 | M | #23, #33 done |  |
| 4 | [#35](https://github.com/aritaberisha-maxasp/argus/issues/35) | Match each invoice to its vendor account, on the assumed DATEV answers | PR-02, VM-02, VM-03 | M | #34 |  |
| 5 | [#36](https://github.com/aritaberisha-maxasp/argus/issues/36) | Block invoices whose IBAN differs from the one on file | PR-05, VM-04 | S | #35 |  |
| 6 | [#63](https://github.com/aritaberisha-maxasp/argus/issues/63) | Show the full trail of a case from receipt to payment | NF-01, AP-08 | S | #62 list part done |  |
| 7 | [#62](https://github.com/aritaberisha-maxasp/argus/issues/62) | Invoices page, second part: resolve an exception from the page | EX-01, NF-05 | S | #62 list part and #112 done |  |
| 8 | [#167](https://github.com/aritaberisha-maxasp/argus/issues/167) | Let Arita set the digest and statement reminder addresses for go live | EX-03, BA-08 | S | #61, #56 done |  |
| 9 | [#37](https://github.com/aritaberisha-maxasp/argus/issues/37) | Propose the G/L account and tax key from the vendor's history | PR-04 | M | #35 | yes |
| 10 | [#169](https://github.com/aritaberisha-maxasp/argus/issues/169) | Make the CI check catch a switched off backup alert | NF-01 | S | #115 done | yes |
| 11 | [#168](https://github.com/aritaberisha-maxasp/argus/issues/168) | Give each mailbox job only the mailbox actions it needs | NF-03, NF-01 | S | #61, #56 done | yes |
| 12 | [#170](https://github.com/aritaberisha-maxasp/argus/issues/170) | Refuse a blank mode switch reason made of tabs or line breaks | NF-01, NF-03 | S | #134 done | yes |

Carry over: the DATEV spike #2, open until Benjamin answers. Next in line, refinement only: #38, #64, #116 (#64 and #116 moved back to the backlog from Sprint 3, where they did not start).

Numbers for this sprint: next free ADR is 0010; new migrations start at 0015 and are chained to the highest one in main or any open PR.

## Sprint 3 (9 to 15 Nov 2026, done)

**Sprint goal:** ARGUS runs live in Azure with the stand ins and a fake mailbox, so Arita can show Benjamin everything built so far in a demo, and only the real mailbox, DATEV and Wise are left to plug in.

Arita approved the planning list on 10 Nov and asked to put the live demo first. The demo stories #124 to #126 were added on top; #61 and #56 moved from core to stretch to make room. DATEV stays on hold in Sprint 3. From Sprint 4 the approval and booking path may build on the assumed DATEV answers D1 to D9 (ADR 0002 provisional), as Arita said on #2.

| Rank | Issue | Story | Requirement IDs | Size | Depends on | Stretch |
|---|---|---|---|---|---|---|
| 1 | [#123](https://github.com/aritaberisha-maxasp/argus/issues/123) | Stop a PR in CI when its migration clashes with another one | NF-01 | S | nothing |  |
| 2 | [#124](https://github.com/aritaberisha-maxasp/argus/issues/124) | Run ARGUS in Azure with the stand ins and a fake mailbox for the demo | IN-01, NF-04, NF-06 | M | #81 done |  |
| 3 | [#125](https://github.com/aritaberisha-maxasp/argus/issues/125) | Pick up new emails on its own in Azure every few minutes | IN-01, NF-01 | S | #81 done; #103 is the same pattern, either can go first |  |
| 4 | [#126](https://github.com/aritaberisha-maxasp/argus/issues/126) | Show Benjamin the exception queue on a page after sign in | EX-01, EX-02 | S | #23 |  |
| 5 | [#133](https://github.com/aritaberisha-maxasp/argus/issues/133) | Make Microsoft sign in work for ARGUS in Azure | NF-03 | S | nothing |  |
| 6 | [#134](https://github.com/aritaberisha-maxasp/argus/issues/134) | Let an admin switch the demo between shadow and live mode on the page | IN-01, NF-01, NF-03 | S | #126 done, #133 |  |
| 7 | [#62](https://github.com/aritaberisha-maxasp/argus/issues/62) | Show the status of every invoice at any time (list part first) | NF-05, NF-03, EX-01 | M | #23 done, #21 done, #126 done, #133 |  |
| 8 | [#135](https://github.com/aritaberisha-maxasp/argus/issues/135) | Show the fake mailbox folders on a page during the demo | IN-04, IN-06, NF-03 | S | #126 done, #133 |  |
| 9 | [#152](https://github.com/aritaberisha-maxasp/argus/issues/152) | Give the exception queue page Arita's new design | EX-01, EX-02, NF-03 | M | #126 done, #133 done, after #134 and #135 |  |
| 10 | [#136](https://github.com/aritaberisha-maxasp/argus/issues/136) | Drop one chosen test email into the demo inbox during the demo | IN-01 | S | #124 done, #125 done |  |
| 11 | [#63](https://github.com/aritaberisha-maxasp/argus/issues/63) | Show the full trail of a case from receipt to payment | NF-01, AP-08 | S | #62 |  |
| 12 | [#112](https://github.com/aritaberisha-maxasp/argus/issues/112) | Let Benjamin correct number, amount or currency of a flagged invoice | EX-01, IN-10, PR-01 | M | #31, #23, #126 |  |
| 13 | [#103](https://github.com/aritaberisha-maxasp/argus/issues/103) | Run the nightly audit copy as a scheduled job in Azure | NF-01, NF-05 | S | #81 done, #83 done |  |
| 14 | [#115](https://github.com/aritaberisha-maxasp/argus/issues/115) | Tell Arita when a weekly database backup fails | NF-01, NF-03 | S | #82 |  |
| 15 | [#61](https://github.com/aritaberisha-maxasp/argus/issues/61) | Send Benjamin a daily digest of open exceptions | EX-03 | S | #21 done | yes |
| 16 | [#56](https://github.com/aritaberisha-maxasp/argus/issues/56) | Import the monthly Wise statement safely, once, with balances that add up | BA-01, BA-07, BA-08, NF-01 | M | #19, #20, #21 done | yes |
| 17 | [#64](https://github.com/aritaberisha-maxasp/argus/issues/64) | Measure how many invoices go through without manual work | NF-07 | S | #62 | yes |
| 18 | [#116](https://github.com/aritaberisha-maxasp/argus/issues/116) | Bring lost audit events back from the nightly copies after a restore | NF-01, NF-05 | M | #82, #103 | yes |
| 19 | [#34](https://github.com/aritaberisha-maxasp/argus/issues/34) | Keep the approver, deputy and direct debit flag for each vendor | AP-01, AP-06, PY-13, VM-02 | M | #23, #33 done | yes |

On 12 Nov Arita approved the plan for a demo that can be seen after deploy. Four new stories went in at ranks 5, 6, 8 and 9, ahead of #112, #103, #115 and the stretch stories: #133 makes sign in work in Azure, #134 puts the shadow and live switch on the page, #135 shows the fake mailbox (demo mode only) and #136 drops one chosen test email into the demo inbox. For the demo, #62 builds its list of cases first. #133 and #136 can start right away; #134, #135 and #62 need #133 before they can be seen in Azure.

On 13 Nov Arita redesigned the exception queue page in Claude Design and asked to put it in the sprint. #152 went in at rank 9, right after #134 and #135, so it can land before the demo on 17 Nov if the night shift has room. It builds on the page after #134 and #135 so their parts get the new look too. A copy of the design is in [docs/design/queue-page/](../design/queue-page/).

Carry over: the Sprint 2 stories that wait only for Arita's merge (#23, #31, #71, #106, #90, #72) and the DATEV spike #2. Next in line, refinement only: #117, #118, #84.

Numbers for this sprint: next free ADR is 0009; new migrations start at 0013 and are chained to the highest one in main or any open PR.

## Sprint 1 (26 Oct to 1 Nov 2026, done)

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

This table is the order from October. Stories filed since then (#106, #112, #115 to #118, #123 to #126, #133 to #136) are ranked in the Sprint 3 section above or not planned yet.

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
| 14 | [#65](https://github.com/aritaberisha-maxasp/argus/issues/65) | Describe the Azure setup as code so Arita can deploy it | Operations | M | #8 |  |
| 15 | [#81](https://github.com/aritaberisha-maxasp/argus/issues/81) | Deploy ARGUS to Azure from CI so nobody deploys by hand | Operations | M | #65 |  |
| 16 | [#82](https://github.com/aritaberisha-maxasp/argus/issues/82) | Keep backups of the invoice database for the legal retention period | Operations | S | #65, #81 |  |
| 17 | [#83](https://github.com/aritaberisha-maxasp/argus/issues/83) | Copy the audit log every night to the locked document storage | Foundation | S | #19, #20, #21 |  |
| 18 | [#70](https://github.com/aritaberisha-maxasp/argus/issues/70) | Trust a sender only when the email really comes from their domain | Intake | S | #25 |  |
| 19 | [#71](https://github.com/aritaberisha-maxasp/argus/issues/71) | Send doubtful emails to a person instead of routing them | Intake | M | #25, #29, #28, #70 |  |
| 20 | [#23](https://github.com/aritaberisha-maxasp/argus/issues/23) | Let approvers and Benjamin sign in with their Microsoft account and see only what their role allows | Foundation | M | #8 |  |
| 21 | [#72](https://github.com/aritaberisha-maxasp/argus/issues/72) | Run ARGUS for two weeks in shadow mode before it routes anything | Intake | S | #25, #26, #27, #23 |  |
| 22 | [#30](https://github.com/aritaberisha-maxasp/argus/issues/30) | Split an email with several invoices into one case per invoice | Intake | S | #29 |  |
| 23 | [#31](https://github.com/aritaberisha-maxasp/argus/issues/31) | Catch invoices that arrive twice | Intake | S | #29, #21 |  |
| 24 | [#32](https://github.com/aritaberisha-maxasp/argus/issues/32) | Check incoming invoices for the formal requirements of §14 UStG | Intake | S | #29, #21 |  |
| 25 | [#34](https://github.com/aritaberisha-maxasp/argus/issues/34) | Keep the approver, deputy and direct debit flag for each vendor | DATEV | M | #23, #33 |  |
| 26 | [#35](https://github.com/aritaberisha-maxasp/argus/issues/35) | Match each invoice to its vendor account in DATEV | DATEV | M | #33, #34, #29, #21 | yes, #2 |
| 27 | [#36](https://github.com/aritaberisha-maxasp/argus/issues/36) | Block invoices whose IBAN differs from the one in DATEV | DATEV | S | #35 |  |
| 28 | [#37](https://github.com/aritaberisha-maxasp/argus/issues/37) | Propose the G/L account and tax key from the vendor's booking history | DATEV | M | #35 | yes, #2 |
| 29 | [#38](https://github.com/aritaberisha-maxasp/argus/issues/38) | Let the vendor's approver approve, reject or query an invoice without DATEV or Wise | Approval | M | #23, #35, #37 |  |
| 30 | [#39](https://github.com/aritaberisha-maxasp/argus/issues/39) | Ask Benjamin for a second approval above EUR 5,000 | Approval | S | #38, #43 |  |
| 31 | [#40](https://github.com/aritaberisha-maxasp/argus/issues/40) | Remind approvers after 3 working days and escalate after 5 | Approval | S | #38, #22 |  |
| 32 | [#41](https://github.com/aritaberisha-maxasp/argus/issues/41) | Book approved invoices in DATEV automatically | Booking | M | #33, #38 | yes, #2 |
| 33 | [#42](https://github.com/aritaberisha-maxasp/argus/issues/42) | Attach the invoice image and XML to every DATEV booking | Booking | S | #41 | yes, #2 |
| 34 | [#61](https://github.com/aritaberisha-maxasp/argus/issues/61) | Send Benjamin a daily digest of open exceptions | Visibility | S | #21 |  |
| 35 | [#62](https://github.com/aritaberisha-maxasp/argus/issues/62) | Show the status of every invoice at any time | Visibility | M | #23, #21 |  |
| 36 | [#44](https://github.com/aritaberisha-maxasp/argus/issues/44) | Convert foreign currency invoices to EUR at the Wise rate | Booking | S | #43, #41 |  |
| 37 | [#45](https://github.com/aritaberisha-maxasp/argus/issues/45) | Book supplier credit notes and offset them in the next payment run | Booking | S | #41 |  |
| 38 | [#46](https://github.com/aritaberisha-maxasp/argus/issues/46) | Collect booked invoices in the payment pool and know the run dates | Payment runs | S | #22, #41 |  |
| 39 | [#47](https://github.com/aritaberisha-maxasp/argus/issues/47) | Choose which invoices go into each payment run | Payment runs | M | #46, #27 |  |
| 40 | [#48](https://github.com/aritaberisha-maxasp/argus/issues/48) | Pay each vendor once per run, net of credit notes | Payment runs | S | #47, #45 |  |
| 41 | [#49](https://github.com/aritaberisha-maxasp/argus/issues/49) | Send Benjamin a summary before each run is handed to Wise | Payment runs | S | #48 |  |
| 42 | [#84](https://github.com/aritaberisha-maxasp/argus/issues/84) | Switch the Wise client from the stand in to the Wise sandbox and then to Max ASP's real Wise account | Payment runs | M | #43, #81, #50 for the live run |  |
| 43 | [#50](https://github.com/aritaberisha-maxasp/argus/issues/50) | Hand each payment run to Wise as an unfunded batch | Payment runs | M | #49, #43, #36 |  |
| 44 | [#51](https://github.com/aritaberisha-maxasp/argus/issues/51) | Mark invoices as paid once Wise has sent the money | Payment runs | S | #50 |  |
| 45 | [#52](https://github.com/aritaberisha-maxasp/argus/issues/52) | Book outgoing invoices to the right customer account and tax treatment | Outgoing invoices | M | #33, #25, #29, #21, #42, #44 | yes, #2 |
| 46 | [#53](https://github.com/aritaberisha-maxasp/argus/issues/53) | Check outgoing invoice numbers for gaps and duplicates | Outgoing invoices | S | #52 |  |
| 47 | [#54](https://github.com/aritaberisha-maxasp/argus/issues/54) | Book outgoing credit notes and cancellations linked to the original invoice | Outgoing invoices | S | #52 |  |
| 48 | [#56](https://github.com/aritaberisha-maxasp/argus/issues/56) | Import the monthly Wise statement safely, once, with balances that add up | Bank import | M | #19, #20, #21 |  |
| 49 | [#57](https://github.com/aritaberisha-maxasp/argus/issues/57) | Book bank transactions and clear supplier payments | Bank import | M | #56, #33, #51 | yes, #2 |
| 50 | [#58](https://github.com/aritaberisha-maxasp/argus/issues/58) | Match customer payments to open invoices | Bank import | M | #57, #52 |  |
| 51 | [#59](https://github.com/aritaberisha-maxasp/argus/issues/59) | Book fees, conversions, own transfers, taxes and payroll by rule | Bank import | M | #57 |  |
| 52 | [#60](https://github.com/aritaberisha-maxasp/argus/issues/60) | Ask Benjamin about bank transactions ARGUS cannot match | Bank import | M | #59 |  |
| 53 | [#63](https://github.com/aritaberisha-maxasp/argus/issues/63) | Show the full trail of a case from receipt to payment | Visibility | S | #62 |  |
| 54 | [#64](https://github.com/aritaberisha-maxasp/argus/issues/64) | Measure how many invoices go through without manual work | Visibility | S | #62 |  |

## What changes when the DATEV spike closes

- **#2 DATEV.** If Max ASP cannot run DATEVconnect and the bridge, the DATEV gateway falls back to the DATEV booking file (ADR 0002). Then vendor matching, the IBAN check and the account proposal need a different data source, and a story for the bridge at Max ASP is added or dropped.


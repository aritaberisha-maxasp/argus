# Importing the monthly Wise statement

Covers: BA-01, BA-07, BA-08, NF-01, NF-03. Issue: #56. Decision: [ADR 0004](../adr/0004-wise-payment-handover.md). Code: `src/argus/bank/`. Migration: 0014.

## What happens

Benjamin downloads the statement of each Wise balance once a month as CSV and sends it to ARGUS (Arita's answer on #4). For each file:

1. **Who sent it.** The file counts only when it comes from an address in `ARGUS_STATEMENT_SENDERS` and that email passed SPF, DKIM or DMARC (the sender check of #70), or when it sits in the agreed mailbox folder `ARGUS_STATEMENT_FOLDER` (default `ARGUS/Kontoauszüge`). Anything else is refused before the file is stored or read, and the refusal is in the audit log with the file's SHA-256.
2. **Read it** (`statement.py`). The file must be UTF-8, have Wise's columns (`TransferWise ID`, `Date`, `Amount`, `Currency`, `Running Balance`), hold one currency and one calendar month, and that month must be over. Rows are put oldest first: by Wise's time when every row has one, otherwise by date, turning Wise's newest first order round. A row without an ID gets one built from its content and the running balance after it, so two equal payments on one day stay apart.
3. **Store it.** The file goes into the document store under its SHA-256, exactly as received.
4. **Check and record it** (`importer.py`), in one unit of work and under one lock:

| Situation | Outcome | What happens |
|---|---|---|
| This very file was imported or recorded before | `already_seen` | Nothing new |
| Another file for a month that is already imported | `month_already_imported` | Recorded, nothing imported |
| A running balance is not the one before plus the amount, the opening balance is not last month's closing balance, or a later month is already imported | `blocked` | Recorded, nothing imported, an exception `bank_balance_difference` in the queue and an email to Benjamin |
| Everything adds up | `imported` | Each transaction becomes a bank transaction case in Received. A transaction an earlier file already brought in is skipped (BA-07). An open difference for the same balance and month is resolved as `data_corrected` |

The first statement of a currency has no month before it, so its opening balance becomes the starting point (assumption on #56, see the go live runbook). A blocked file sent again is checked again, so it can import once the month before it is in. A blocked file that still does not add up adds nothing.

The database checks the important rules itself: one imported statement per currency and month, an imported month opens with the closing balance of the last imported month before it, a transaction belongs to an imported statement of its currency and month, statements and notices cannot be changed or deleted, and the statement events are written only by its triggers.

## Telling Benjamin (`notices.py`)

ARGUS emails Benjamin from the accounts mailbox, to the address set for payment reminders (`ARGUS_REMINDER_FORWARD_TO`). The Graph client allows `sendMail` only to that address, with a subject and plain text and nothing else: no copy, no attachment, no HTML.

| Notice | When | How often |
|---|---|---|
| Balance difference | A statement is blocked | Once per statement |
| Statement missing | 2nd working day of the month, last month's statement is missing for a balance in `ARGUS_WISE_BALANCES` | Once per month, naming every missing balance |
| Statement overdue | The 5th, or the working day before it, because the import must be finished by the 5th (BA-01) | Once per month |

Each notice is recorded as pending, then sent, then marked sent, all outside any transaction for the sending. A crash right after sending can send it twice on the next run; nothing is lost. In shadow mode nothing is sent and the notice ends as `not_sent_shadow`. The import itself runs in shadow mode too, because it changes nothing outside ARGUS.

## What stays out of the logs

The audit log gets fingerprints, IDs, currency, month, amounts and reason codes. File names, counterparties, payment references and descriptions stay in the database tables only (NF-03). The application log holds IDs, currency, month and counts.

## Not in this story

- Taking statement emails out of the mailbox sweep and handing them to the import, and running `remind_missing_statements` once a day as a job. Follow up story, see the PR.
- Booking and matching the bank transaction cases (#57, #58, BA-02 to BA-06).
- A month without any transaction: Wise writes an empty CSV, which cannot show the balance, so ARGUS refuses it and the reminder for that balance keeps coming. To be settled with Benjamin.

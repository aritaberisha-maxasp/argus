# Importing the monthly Wise statement

Covers: BA-01, BA-07, BA-08, NF-01, NF-03. Issue: #56. Decision: [ADR 0004](../adr/0004-wise-payment-handover.md). Code: `src/argus/bank/`. Migration: 0014.

## What happens

Benjamin downloads the statement of each Wise balance once a month as CSV and sends it to ARGUS (Arita's answer on #4). For each file:

1. **Who sent it.** The file counts only when it comes from an address in `ARGUS_STATEMENT_SENDERS` and that email has an aligned SPF, DKIM or DMARC pass for that address's domain (the sender check of #70), or when it sits in the agreed mailbox folder `ARGUS_STATEMENT_FOLDER` (default `ARGUS/Kontoauszüge`). Anything else is refused before the file is stored or read, and the refusal is in the audit log with the file's SHA-256.
2. **Read it** (`statement.py`). The file must be UTF-8, have Wise's columns (`TransferWise ID`, `Date`, `Amount`, `Currency`, `Running Balance`), hold one currency and one calendar month, and that month must be over. A file from Benjamin or the agreed folder that fails here is refused with a reason code in the audit log and, when it came with an email, an `unreadable_document` exception on that email with the reason in German, so a person sees why instead of only more reminders (code review of PR 162). One open exception per email, however often the file comes. Rows are put oldest first: a newest first file is turned round, then the rows are sorted by Wise's time when every row has one, otherwise by date, and rows with the same time or date keep that order. A row without an ID gets one built from its content and the running balance after it, so two equal payments on one day stay apart.
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

Both days can be the same, for example in January (1 and 6 January are holidays in Baden-Württemberg, so in 2027 both fall on 5 January), and in months that start on a weekend or holiday, such as April and May 2026. Then only the overdue notice goes out.

Each notice is recorded as pending, then sent, then marked sent, all outside any transaction for the sending. A crash right after sending can send it twice on the next run; nothing is lost. In shadow mode nothing is sent and the notice ends as `not_sent_shadow`. The import itself runs in shadow mode too, because it changes nothing outside ARGUS.

## From the mailbox (#166)

The mailbox sweep hands statement files to the import (`src/argus/intake/statements.py`). Two ways in:

- **An email in the Inbox.** Right before an email is sorted, the sweep looks for CSV files in it. If the From address is in `ARGUS_STATEMENT_SENDERS` and `argus.sorting.sender.authenticate` passes for it (an aligned SPF, DKIM or DMARC pass, the strict check of #70), every CSV file goes to the import. The sorting's own sender check (`SenderCheck.treated_as`) is never used for this, because it treats Max ASP's own domain as internal unless DMARC fails.
- **An email in the statement folder** `ARGUS_STATEMENT_FOLDER`. After the pass over the Inbox the sweep lists that folder and hands every CSV file of each email over once, whoever sent it. An audit event `bank_import_from_folder` marks the email as done. The Graph client can list this one folder only: the transport allows the listing for the folder ID the client found from its configured path, and refuses every other folder.

A CSV file with Wise's columns from anyone else, or from a statement sender whose email fails the strict check (including a forged From line with Benjamin's address), is refused: the import writes `bank_import_refused` with reason `unknown_source`, and the email gets one exception `uncertain_classification` in German that says to move the email into the statement folder if the statement is genuine. When the email is then imported from the folder, the exception stays open: the move overrides the sender check and Graph does not say who moved the email, so a signed in person closes it in the queue with a reason, as for any outcome that lets a flagged item carry on (PR 109, compliance review of PR 176). The audit event `bank_import_from_folder` records whether the email passed the sender check (yes or no, no address). A person who resolved the exception is not asked again for that email. An email in the folder without a CSV file gets one `unreadable_document` exception that says no CSV statement was found. A CSV without Wise's columns from anyone else is left to the sorting.

Mail Benjamin sends from inside the tenant often carries no SPF, DKIM or DMARC result for his domain, so his emails may never pass the strict check. Then the folder is the way in, and the exception says so. No Exchange internal marker such as `X-MS-Exchange-Organization-AuthAs` is trusted: it was not tested on real headers, and the go live runbook has a step to try one real email instead.

Every file goes to the import with the `email_id` of its email. Running the sweep again on the same email, or getting the same file in the Inbox and the folder, imports it once (BA-07). Shadow mode is as above: the import runs, notices are not sent.

The daily job `python -m argus.intake statements` (`caj-argus-statements`, 06:00 UTC) runs `remind_missing_statements` and sends any notice still waiting. It ends with `argus-job ok statements` or `argus-job failed statements` like the other jobs, and fails when a notice could not be sent or `ARGUS_REMINDER_FORWARD_TO` is missing.

## What stays out of the logs

The audit log gets fingerprints, IDs, currency, month, amounts and reason codes. File names, counterparties, payment references and descriptions stay in the database tables only (NF-03). The application log holds IDs, currency, month and counts.

## Not in this story

- Booking and matching the bank transaction cases (#57, #58, BA-02 to BA-06).
- A month without any transaction: Wise writes an empty CSV, which cannot show the balance, so ARGUS refuses it and the reminder for that balance keeps coming. To be settled with Benjamin.

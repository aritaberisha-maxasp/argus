# The invoice case: design note

Covers: NF-01, NF-05, EX-01, EX-02, AP-08, BK-07, IN-10, BA-07 (also touches IN-08, IN-09, PY-06, PY-10, PY-11, PY-13, AR-07, BA-06, BA-08, AC-5, AC-8)

This note holds the detail behind [ADR 0005](../../adr/0005-invoice-case-audit-log-and-exception-queue.md). The ADR says what we decided. This note says how it works, so the developers on the skeleton (#8) and the first stories can build it without guessing.

[schema-sketch.sql](schema-sketch.sql) is the PostgreSQL 16 sketch of the tables and rules below, and [checks.sql](checks.sql) proves the important rules hold. Since #19 the real tables live in the Alembic migrations in `src/argus/db/migrations/versions/`, the checks are tests in `tests/integration/test_case_store.py`, and the Python layer that moves cases is `src/argus/cases/`. The migrations are now the reference; the sketch stays as the record of the design.

## 1. What a case is

A case is one business document or one bank transaction that ARGUS has to see through to the end. There are five kinds:

| Kind | Starts when | Ends when |
|---|---|---|
| Incoming invoice | Intake finds a supplier invoice in an email | Paid, or closed (rejected or duplicate) |
| Incoming credit note | Intake finds a supplier credit note | Offset in a payment run or cleared by the bank import |
| Outgoing invoice | Intake finds an invoice issued by Max ASP | The customer has paid in full |
| Outgoing credit note | Intake finds a credit note issued by Max ASP | Cleared by the bank import |
| Bank transaction | The monthly Wise import reads a transaction | Booked in DATEV |

Things that are **not** cases:

- **The email.** ADR 0003 already keeps one record per email. One email can hold several invoices, so it can create several cases (IN-08). Every case points back to its email.
- **Payment reminders.** A reminder is forwarded to Benjamin (IN-05). If ARGUS can match it to an open invoice case, it stamps `reminder_received_at` on that case and logs it, so the next payment run takes the invoice (PY-06). The reminder itself does not get its own case.
- **Other emails.** They only exist as an email record and are removed as ADR 0003 says.
- **Payment runs and payments.** They have their own tables (section 6) and point to the cases they pay.

### What a case holds

One `invoice_case` table holds what every kind shares. Kind specific detail lives in side tables (`bank_transaction` today, more as the stories need them).

| Field | Why |
|---|---|
| `kind`, `status` | What it is and where it is (NF-05) |
| `source_email_id` | Link to the email it came from, for the trace (AC-8) |
| Document links with SHA-256 | The original files in the locked store (ADR 0001) |
| `partner_account` | DATEV Kreditor or Debitor number once matched (PR-02, AR-03) |
| Document number, dates, net, VAT, gross, currency | Extracted data (PR-01). Money is stored as whole cents in a `bigint`, never as a float |
| `duplicate_key`, `duplicate_of` | Duplicate protection (IN-10), section 5 |
| `payment_hold` | Held back from payment runs (PY-10) |
| `reminder_received_at` | Always in the next run (PY-06) |
| `direct_debit` | Copied from the approver table, never put in a run (PY-13) |
| `datev_booking_ref` | The DATEV reference for reconciliation (BK-07) |
| `closed_reason` | Why a case ended without being paid |

Approval decisions get their own `approval` table (case, step first or second, approver, decision, comment, time) and each decision is also written to the audit log (AP-08).

## 2. Statuses

The seven statuses NF-05 asks for, plus **closed** for cases that end without being paid:

| Status | Meaning |
|---|---|
| Received | ARGUS has the document and is reading and checking it |
| Awaiting approval | Waiting for the vendor's approver, or for Benjamin's second approval above EUR 5,000 (AP-09). Which step is open is shown next to the status, not as its own status |
| Approved | All approvals are in. Booking starts at once (BK-01) |
| Booked | DATEV accepted the booking and returned a reference. The invoice is in the payment pool (PY-01) |
| In payment run | Part of a payment handed to Wise, waiting for Benjamin to release it |
| Paid | Wise reports the transfer as sent, or the bank import cleared it |
| Exception | Stopped and waiting in the exception queue for a person or for missing data |
| Closed | Ended without payment: rejected (AP-07), duplicate (IN-10), not an invoice after all, or withdrawn |

```mermaid
stateDiagram-v2
    [*] --> Received
    Received --> AwaitingApproval: checks passed
    AwaitingApproval --> Approved: all approvals in
    AwaitingApproval --> Closed: rejected
    Approved --> Booked: DATEV accepted
    Booked --> InPaymentRun: handed to Wise
    Booked --> Paid: direct debit cleared
    InPaymentRun --> Paid: Wise sent it
    InPaymentRun --> Booked: Wise cancelled the unfunded transfer
    Received --> Exception
    AwaitingApproval --> Exception
    Approved --> Exception
    Booked --> Exception
    InPaymentRun --> Exception
    Paid --> Exception: transfer bounced
    Exception --> Closed: duplicate or withdrawn
    Exception --> Received: resolved
    Paid --> [*]
    Closed --> [*]
```

An exception returns the case to the status it left, so the step that failed runs again. The diagram shows only "back to Received" to keep it readable. The one listed exception is **payment failed**: a case that was In payment run or Paid goes back to Booked, so it waits for the next run. Exits like this live in a small table (`exception_exit`), reason by reason, so nobody can add one in code.

### Which kinds use which statuses

| Kind | Path |
|---|---|
| Incoming invoice, incoming credit note | Received, Awaiting approval, Approved, Booked, In payment run, Paid |
| Incoming invoice paid by direct debit | Received, Awaiting approval, Approved, Booked, Paid (the bank import clears it, PY-13) |
| Outgoing invoice, outgoing credit note | Received, Booked, Paid (AR-02: never approval or payment run) |
| Bank transaction | Received, Booked |

A partial customer payment (BA-04) leaves an outgoing invoice in Booked with the open amount reduced. It becomes Paid only when the full amount has arrived.

### Rules the database enforces

These live in the database, so no bug in the application can get round them.

1. **Only listed moves are possible.** The table `case_status_transition` lists every allowed move per kind. A trigger refuses anything else, for example Approved straight to Paid.
2. **Every change names who made it.** Each transaction sets `argus.actor` to `system:<job>` or `user:<Entra object ID>`. Without it the change is refused.
3. **Every change is logged in the same transaction.** A trigger writes the audit event for every new case, every move and every change to booking data (account, number, amounts, currency, duplicate fields, payment hold, direct debit, reminder, DATEV reference, closed reason), with old and new value. If the audit write fails, the change is undone too.
4. **The kind never changes**, and a closed case never changes again.
5. **Booking data is frozen after approval.** Once a case is Approved (or Booked, for kinds without approval), its account, number, amount and currency cannot change. A correction then means rejecting it and starting a new approval. The DATEV booking reference can be set once.
6. **Direct debit and held invoices never go into a payment run** (PY-13, PY-10).
7. **An open exception stops the case.** While a case has an open exception it cannot move anywhere except into Exception. At commit the database checks both ways: a case with an open exception is in Exception, and a case in Exception has an open exception. So a job that opens an exception and dies before moving the case cannot leave it running.
8. **An exception goes back to where it came from.** The database itself records the status the case left (`resume_status`) when it enters Exception. The application cannot write it. The case may only return there, be closed, or take an exit listed in `exception_exit`. Two outcomes end the case instead: an exception resolved as `rejected` can only be closed as rejected, and one resolved as `confirmed_duplicate` can only be closed as duplicate. So a rejected IBAN mismatch or a confirmed duplicate can never go on to approval, booking or payment. A case exception can only be resolved while its case is in Exception, so this check can never be skipped by opening and resolving in one step.
9. **The application can only write what it must.** It connects as `argus_app`, which has column level rights: it cannot set `kind` after insert, `resume_status`, any time stamp, or who resolved an exception.
10. **Two workers cannot move the same case at once.** Moves are written as `update ... where id = :id and status = :expected`. If another worker moved it first, nothing changes and the worker starts over.

## 3. The audit log

The audit log answers "what happened to this invoice, when, and who did it" for ten years (NF-01, AC-8).

### What gets logged

| Event | Written by |
|---|---|
| Case created, status changed (with booking reference when it is set) | Database trigger, automatically |
| Exception opened, exception resolved (with reason and resolution) | Database trigger, automatically |
| Email seen, stored, classified, routed, filed, forwarded, removed, purged (ADR 0003) | Intake |
| Document stored, with SHA-256 and storage path | Intake |
| Data extracted, vendor matched, checks run with their result | Processing |
| Approval requested, reminder sent, escalated, decided with comment (AP-03, AP-05, AP-08) | Approval |
| Booking sent, booking reference received or error (BK-06, BK-07) | Booking |
| Added to pool, held, released from hold, added to a run, transfer created, Wise status changed (PY-01 to PY-11) | Payment |
| Bank transaction imported, matched, cleared, booked by a rule, Benjamin's answer applied (BA-02 to BA-06) | Bank import |
| Payment reminder matched to a case (PY-06) | Intake |

Each event has: a running number, the time from the database clock (always, the application cannot give one), the case, email, statement, exception or payment run it belongs to, the event type, who did it (`system` plus job name, or `user` plus Entra object ID, always taken from `argus.actor`, so an approval decision is recorded under the person whose sign in made the request), the status before and after when it changed, and a small JSON with the details.

**What does not go in:** document content, full IBANs (only the last four characters and whether it matched), passwords, tokens or anything from Key Vault, and no free text written for people. The text of an exception and the comment on its resolution stay on the exception itself. The log gets the reason, structured facts (such as `iban_last4`) and a fixed outcome code. The log is kept for ten years, so it holds only what the trace needs (GDPR, NF-03). **One exception, on purpose:** when a person makes a decision the law asks us to explain, the log keeps their short reason and their name as they were at that moment (ADR 0008, GoBD). Today that is the switch between shadow and live mode (`intake_mode_changed`, 0012) and a correction of number, amount or currency (`case_corrected`, 0013). The page asks people not to type personal details into that reason.

### Why nobody can change it

1. **The application cannot change history.** ARGUS connects as `argus_app`, which may only add and read audit events. `UPDATE`, `DELETE` and `TRUNCATE` are not granted, and a trigger refuses them even for roles that have the right.
2. **The application cannot fake an event.** Time and actor are always filled in by the database. Event types that describe a case or exception change (`case_created`, `status_changed`, `case_changed`, `exception_opened`, `exception_resolved`) can only come from the database triggers.
3. **Changes behind the triggers are visible.** Each event carries the SHA-256 of the previous event's hash plus its own content (a hash chain). The content is hashed in one canonical form: every field named, nulls kept, and the time as seconds since 1970, so the session time zone does not matter and a value moved to another column is noticed. One function builds that form for writing and for checking, and each event stores a `hash_version` so the form can change later without breaking old links. Changing or deleting any old event breaks every link after it. `audit_verify()` walks the chain and names the first broken link. A daily job runs it and alerts Arita if it fails.
4. **A copy sits in the locked store.** Every night a job writes the previous day's events as a JSON lines file, plus the last hash of the day, into the locked blob store from ADR 0001. From then on, even someone with full database rights cannot rewrite that day without the difference showing.

Events are numbered one at a time under a short database lock, so the chain has no forks. At a few thousand events a day this costs nothing. [checks.sql](checks.sql) proves each of these, and a load test with 8 parallel writers creating 1,600 cases kept the chain intact.

### Reading a trace (AC-8)

A `case_trace` view returns, in order, every event of the case, of the email it came from, of its exceptions, and of the payment run and bank transaction that paid it. QA runs it for every acceptance test case and checks that each step from receipt to the last status is there, with time and actor, without gaps. The screen for Arita and Benjamin shows the same list.

## 4. The exception queue

One queue holds everything ARGUS cannot finish on its own (EX-01). An exception belongs to exactly one thing: a case, an email (when there is no case yet, such as an uncertain classification) or a bank statement (balance difference).

### Reasons

The ten reasons from EX-02, plus four that other requirements need:

| Reason | Comes from | Belongs to | How it ends |
|---|---|---|---|
| Uncertain classification | IN-09 | Email | A person picks the category. ARGUS then routes the email and creates its cases |
| Unreadable document | IN-03 | Email or case | A person types in the data or asks the vendor for a better copy |
| Unknown vendor | VM-03, PR-02 | Case | **By itself** once the Kreditor exists in DATEV. A daily job checks again |
| Unknown customer | AR-03 | Case | **By itself** once the Debitor exists in DATEV |
| Vendor without approver | VM-02 | Case | **By itself** once Arita adds the approver to the approver table |
| Duplicate | IN-10 | Case | A person confirms (case closed as duplicate) or says it is not one (section 5) |
| IBAN mismatch | PR-05 | Case | **Only** by changing the IBAN in DATEV (VM-04), after which ARGUS checks again, or by rejecting the invoice. Nobody can accept the IBAN from the invoice. Only the vendor match pass may end it as `fixed_in_datev`; a person can only reject (#186, migration 0018) |
| Invoice check failed (§14 UStG) | PR-03 | Case | A person asks the vendor for a corrected invoice, or accepts it with a comment |
| Booking error | BK-06 | Case | ARGUS retries a few times by itself, then a person fixes the cause in DATEV and retries |
| Unmatched bank transaction | BA-06 | Case | Benjamin's reply names the account. ARGUS books it and may save a rule |
| Bank balance difference | BA-08 | Statement | A person finds the difference. The import of that statement stays blocked until then |
| Number sequence gap or duplicate *(added)* | AR-07 | Case | A person confirms the gap is explained or fixes the numbering |
| Payment failed *(added)* | PY-11 | Case | A person checks the vendor's bank details in DATEV and puts the invoice back in the pool. This is the one reason that may leave Exception somewhere other than where it came from: from Paid or In payment run back to Booked |
| Customer query *(added)* | Decision on #9 (E44) | Email | A person answers the customer and closes it |
| Values to check *(added, #112)* | IN-10, PR-01 | Case | A person corrects the invoice number, gross amount and currency with a reason (`data_corrected`), or rejects the invoice. See "Correcting the values" below |

### How an exception works

- An exception records the reason, a plain text detail a person can act on, structured facts without personal data, and when it was opened. When it is resolved the application sets an outcome code (for example `fixed_in_datev`, `rejected`, `confirmed_duplicate`) and an optional comment, and the database adds who and when. A resolved exception can no longer change.
- The same thing cannot have the same open reason twice. A unique index prevents it, so a job that runs again does not pile up copies.
- Opening an exception and moving the case to Exception happen in one transaction. Resolving it and moving the case back also happen in one transaction.
- Benjamin is told at once for the reasons the requirements name (BK-06, AR-03, VM-03, BA-06, BA-08) and gets the daily digest of everything still open (EX-03), grouped by reason, oldest first.
- Benjamin and Arita can resolve exceptions. Approvers cannot. Roles come from Entra groups (ADR 0001).
- An IBAN mismatch keeps the case out of every payment run until it is resolved, whatever its status was before, because an open exception stops every move. It can only end with the outcome `fixed_in_datev` (the IBAN was changed in DATEV and ARGUS checks again) or `rejected`. The database refuses any other outcome. This is the fraud guard of PR-05 and VM-04 and has no shortcut.

### Correcting the values (#112, migration 0013)

When ARGUS cannot read the gross amount or currency of an incoming invoice or credit note with confidence, the case has no duplicate key, and the database will not send it for approval. The intake therefore puts it in the queue as **values to check**, naming the missing values. Benjamin (finance role) or Arita (admin) opens it on the queue page, opens the invoice under "Files as stored" and enters the invoice number, gross amount and currency as printed, with a short reason.

- The number, amount and currency of any case now change **only** through a correction (`case_correction`). The database refuses a job as actor, a reason shorter than three characters and a correction that changes nothing. It keeps the old values, the person's object ID, their name and sign in address as they were at that moment (ADR 0008), the time and the reason. A correction can never be changed or removed.
- Every correction is in the audit log as `case_corrected`, with the old and new values, the name and the reason. Only the database writes that event. The usual `case_changed` event is written as well.
- In the same unit of work the exception is resolved as `data_corrected` (the database refuses that outcome without a correction for it) and `flag_if_duplicate` runs again. A corrected invoice that matches a known one goes to the duplicate exception. Otherwise it gets its key and goes back to Received, ready for approval like any other case.
- After approval nothing can be corrected (rule 5 in section 2).

## 5. Duplicate protection

### Invoices and credit notes (IN-10)

IN-10 says a duplicate is the same vendor, invoice number and amount. ARGUS builds a **duplicate key** from:

- direction (incoming or outgoing) and document type (invoice or credit note),
- the DATEV vendor account (or customer account for outgoing),
- the invoice number in a standard form: upper case, with spaces, `-`, `/`, `.` and `_` removed, so `RE-2026/0042` and `re 2026 0042` match,
- the gross amount in cents and the currency.

A unique index on the key makes the database the final judge, so two copies arriving at the same second cannot both get through.

The key can only be built once the vendor is matched. Until then the case sits in Received or in the Unknown vendor exception, and nothing is sent for approval. When the key is set and already exists, the new case goes to the exception queue as a duplicate, with `duplicate_of` pointing to the first case. It is never sent for approval, booked or paid. If a person decides it is not a duplicate, ARGUS stores the key with the case number added, logs who decided it and why, and carries on.

**As built in #31.** Until vendors are matched to DATEV accounts (#35), the vendor part of the key is the issuer part of the case key from #30: the VAT ID, or the normalised name when the invoice states no VAT ID. The key is `<issuer>|<number>|<gross in cents>|<currency>`; the direction and document type come from the case kind, which is part of the unique index. The key is only set when the amount and currency were read without a flag. An amount a person still has to check gets its key through the same function (`argus.intake.duplicates.flag_if_duplicate`) once it is confirmed. The database also keeps `duplicate_of` honest (migration 0010): it is set once, never points to the case itself, and only to a case of the same kind. After the review of PR 109 it also refuses to send an incoming invoice or credit note for approval, or approve it, while its key is missing, and it clears the key when the number, amount or currency is corrected, so the case has to be checked again. Only a person can decide that a flagged case is not a duplicate, and only with a reason; ARGUS itself may confirm a duplicate, because that only stops processing. Removing `-`, `/` and `.` can make two different numbers of one vendor look the same (`2026-1-12` and `2026-11-2`); with the same amount as well that is rare, and it errs towards asking a person. "Same file again" and "same number, different amount" below are not built yet; the acceptance criteria of #31 say a different amount is not a duplicate.

Two more checks, both cheap:

- **Same file again.** Every stored file has a SHA-256. The same file arriving a second time (for example forwarded twice) is flagged as a duplicate even before the vendor is known.
- **Same number, different amount.** Same vendor and number but a different amount is often a corrected invoice. It goes to the queue as a possible duplicate so a person decides. This goes slightly beyond IN-10, on purpose, because paying both would be the expensive mistake.

Outgoing invoice numbers also get the sequence check from AR-07 (gaps and repeats in Max ASP's own numbering).

### Bank transactions (BA-07)

- Each Wise transaction has an ID in the statement (`TransferWise ID` in the CSV, the bank reference in CAMT.053). ARGUS stores it with the Wise balance currency and a unique constraint on both. A repeated import adds nothing: existing rows are skipped and counted in the import log.
- If a file ever lacks an ID, ARGUS builds one from date, amount, currency, counterparty, reference and the running balance after the transaction. The running balance makes two identical payments on the same day still distinct.
- Each imported file is stored with its SHA-256. Importing the same file twice is spotted before any row is read.

### Bookings and payments

The same idea protects the outside systems:

- **DATEV.** Each booking carries the case number as its idempotency key, and `datev_booking_ref` is set once. A retry after a timeout first asks DATEV whether the booking with that key exists.
- **Wise.** A case can be in only one active payment at a time (unique index on the payment item while the payment is not cancelled). Each Wise transfer carries ARGUS's payment ID as `customerTransactionId`, so Wise itself refuses a second transfer for the same payment (ADR for Wise, #4).

## 6. Payment runs, in brief

The payment run story owns the detail. The case model only needs:

- `payment_run`: run date, type (mid month or month end), status (built, summary sent, handed over).
- `payment`: one per vendor and currency in a run, with the net amount after credit notes (PY-07), the Wise transfer ID and its status.
- `payment_item`: which cases a payment pays.

When a payment is created the cases move to In payment run. When Wise reports it sent, they move to Paid. When Wise cancels an unfunded transfer, they move back to Booked and wait for the next run.

## 7. Left for the approval story

A change to account, number, amount or currency while a case is Awaiting approval is allowed and logged with old and new value. The approval story must then ask for approval again, because an approval given for one amount must not count for another. The approval table it builds is the natural place to enforce that.

## 8. Decisions

Arita answered both questions on 16 Oct 2026.

1. **An invoice whose DATEV booking failed is not paid in the next run.** Only booked invoices go into a payment run, so every payment has a booking reference to reconcile against (BK-07). The booking error waits in the exception queue and Benjamin is told at once.
2. **Incoming credit notes need approval like invoices**, by the vendor's approver. AP-10 says nothing is approved automatically, and a wrong credit note would lower a payment to the vendor.

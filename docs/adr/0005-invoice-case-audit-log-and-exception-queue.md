# 0005: The invoice case, its statuses, the audit log and the exception queue

Status: proposed
Requirements: NF-01, NF-05, EX-01, EX-02, AP-08, BK-07, IN-10, BA-07 (also touches IN-08, IN-09, PY-06, PY-10, PY-11, PY-13, AR-07, BA-06, BA-08, AC-5, AC-8)

The detail, the status diagram and a working database sketch are in the [design note](../design/invoice-case/README.md).

Numbering: 0004 is the Wise ADR and 0006 the document understanding ADR (#55).

## Context

Everything ARGUS does happens to one invoice, credit note or bank transaction at a time. We need one record for each, a status anyone can read at any time (NF-05), a log of every step that nobody can change afterwards and that lets us trace any test case from receipt to payment (NF-01, AC-8), one queue for everything that needs a person (EX-01, EX-02), and protection against processing the same invoice or bank transaction twice (IN-10, BA-07).

ADR 0001 put the data in PostgreSQL and the original files in a locked blob store. ADR 0003 already keeps one record per email.

## Options

**Statuses**
1. Only the seven statuses of NF-05, with substeps (first or second approval, query open) shown next to the status. Pro: matches the requirement and stays readable. Con: a few details live in fields, not in the status.
2. A long list of fine grained statuses. Con: every new detail changes the status list and the screens.

**Who enforces the status rules**
1. The application only. Con: one bug can skip approval or lose a log entry.
2. The database: a table of allowed moves, and triggers that refuse other moves and write the audit event in the same transaction. Pro: no code path can get round it.

**Making the audit log tamper proof**
1. Trust database permissions alone. Con: a database owner could still change history unseen.
2. Permissions, plus a hash chain over all events, plus a nightly copy into the locked blob store. Pro: any change is detectable, and the nightly copy cannot be changed at all.
3. A separate ledger product (for example Azure Confidential Ledger). Con: another service, more cost and setup for our volume.

**Exception queue**
1. One table for all exceptions with a fixed list of reasons, linked to a case, an email or a bank statement. Pro: one place to look, one daily digest.
2. A flag per case. Con: emails with no case yet and bank statements could not be in the queue.

## Decision

- **One case per invoice, credit note or bank transaction**, in one table with a kind field. The email stays its own record (ADR 0003), so one email can create several cases (IN-08).
- **Statuses: the seven from NF-05 plus Closed** (rejected, duplicate, withdrawn). Outgoing invoices skip approval and payment runs. Bank transactions only go from Received to Booked.
- **The database enforces the rules**, and the application role can only write the columns it needs: only listed moves, the kind never changes, booking data is frozen after approval, every change names its actor and writes an audit event with old and new values in the same transaction, an open exception stops the case, and the case goes back to the status the database recorded when it entered Exception (payment failed is the one listed other exit).
- **Audit log:** append only for the application, time and actor always set by the database, a SHA-256 hash chain over a canonical form that does not depend on the time zone, checked daily, and a nightly copy into the locked blob store. No document content, full IBANs, secrets or free text in it.
- **Exception queue:** one table, the ten reasons from EX-02 plus three that other requirements need (number sequence gap AR-07, payment failed PY-11, customer query from the decision on #9). Some reasons end by themselves when the missing data appears in DATEV. An IBAN mismatch can only end by fixing the IBAN in DATEV or rejecting the invoice.
- **Duplicates:** a unique key of vendor, normalised invoice number, gross amount and currency for invoices; the Wise transaction ID with the balance currency for bank transactions; idempotency keys for DATEV bookings and Wise transfers. The database refuses the second copy, so a race cannot slip through.

Why: putting the rules in the database means the parts that matter for GoBD and fraud protection cannot be skipped by a bug in any later story, and the sketch shows they cost little code.

## Consequences

**Easier**
- Every later story uses one way to move a case, and gets its audit entries for free.
- QA can prove AC-8 by reading one trace per case.
- Duplicate protection does not depend on every intake path remembering to check.

**Harder**
- Every database transaction that changes a case must set the actor first. The skeleton (#8) wraps this in one helper.
- Adding a new move means adding a row to the allowed moves table in a migration, on purpose.
- A wrong amount or vendor found after approval cannot be edited. The invoice is rejected and goes through approval again, which is the safe way round.
- All audit writes share one short lock to keep the chain in order. Fine at a few thousand events a day, to watch if volume grows a lot.

**To watch**
- The two open questions in section 8 of the design note (payment of invoices whose booking failed, approval of credit notes).
- The DATEV and Wise ADRs (#2, #4) may name the exact booking reference and transfer statuses. The model stores them as text, so either answer fits.

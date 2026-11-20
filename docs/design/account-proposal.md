# Proposing the G/L account and tax key (#37)

Covers PR-04, with EX-01 and NF-01. Code: `src/argus/intake/accounts.py`, migration 0019.

Approvers should not have to know the chart of accounts. So once an incoming invoice or credit note is matched to its vendor account (#35), ARGUS proposes the G/L account and tax key that vendor was booked to before. The approver sees the proposal (#38), and the booking uses it (#41). Built on the assumed DATEV answers D1 to D9 (Arita, 10 Nov 2026), so it is provisional until the DATEV spike (#2) closes.

## How the proposal is made

At the end of every live sweep, right after vendor matching, ARGUS looks at every matched incoming case in Received or Exception that has no proposal yet. For each vendor account it reads the postings of this year and the two before from DATEV, through the gateway (`list_postings`), once per pass. It then takes the most recent posting and proposes its contra account as the G/L account and its tax key as the tax key. A blank tax key stays blank, for example for tax free rent.

**Payments are not history.** A payment, a received cash discount or a transfer to another personal account also sits on the vendor account and is often the newest posting. It says nothing about where the invoice belongs, so it is skipped. Which accounts those are depends on the chart of accounts:

| Skipped contra accounts | SKR03 | SKR04 |
|---|---|---|
| Cash, Postbank, bank | 1000 to 1299 | 1600 to 1899 |
| Money in transit (Geldtransit) | 1360 | 1460 |
| Received cash discounts (Erhaltene Skonti) | 3730 to 3739 | 5730 to 5739 |
| Personal accounts (customers and vendors) | 10000 and up | 10000 and up |

The chart is a setting, `ARGUS_CHART_OF_ACCOUNTS` (`SKR03` or `SKR04`). It is `SKR03` when not set, the default Arita confirmed on #18. Any other value stops the job and the web app at start, so a typo never proposes a wrong account. The proposal itself only copies what the history says, so it works with either chart.

**Nothing is guessed (PR-04).** The case goes to the exception queue as `no_account_proposal` when:

- the vendor has no usable posting in this year and the two before, or
- the postings of the most recent day point to more than one account or tax key, for example an invoice split over two accounts. The detail names them.

When DATEV does not answer, the cases wait for the next sweep. A setup problem, such as an unknown fiscal year, is never read as "no history": the case waits and the pass counts it as failed, so healthy invoices never land in the queue because of it.

## Stored on the case

Each proposal is one row in `account_proposal`, which can never be changed or removed. It names the case, the vendor account it was made for, the G/L account, the tax key and the chart, and where it came from:

- `booking_history`, with the document number and date of the posting it copied. Only a job can write it.
- `person`, with the person's name as it was at that moment (ADR 0008) and a reason of at least three characters. Only a person can write it.

The database fills in who, when and the transaction, and writes `account_proposed` into the audit log for every row. The application cannot write that event itself.

The current proposal of a case is its newest row for the vendor account the case has now (`current_proposal`). A proposal can only be made while the case is in Received or Exception, so the account is settled before approval and cannot change after.

## When there is no proposal

`no_account_proposal` ends in one of three ways, and the database refuses any other:

- **A person enters the account** (`data_corrected`). Benjamin or Arita opens the item in the exception queue, looks at the invoice and enters the four digit G/L account and the tax key, with a reason. The form refuses a cash, bank or discount account of the chart. The case goes back to where it was and carries on.
- **The vendor gets history** (`fixed_in_datev`). Every sweep looks at these items again. Once the vendor has a usable posting, for example after its first invoice got its account by hand and was booked, ARGUS proposes from it and resolves the item itself.
- **A person rejects the invoice** (`rejected`), on the invoices page.

## Where it shows

- The invoices page shows the G/L account and tax key under the vendor account of each case.
- The case trail says "ARGUS proposed G/L account 4930 with tax key 9" or "A person entered ...", with the person's reason as the comment.
- The approval page (#38) reads `current_proposal` and shows it to the approver.

## Downgrading

Proposals are part of the audit trail. A downgrade below 0019 with proposals in the database keeps the table and its rules, and the upgrade picks them up again. Refusing the downgrade instead would block the check of migration 0017 and every later downgrade. The enum value `no_account_proposal` also stays, because building the enum again breaks open connections.

## What is provisional

- the skipped account ranges are the DATEV standard numbers; Max ASP's own chart may add accounts (#18)
- G/L accounts have four digits and personal accounts five, as the gateway assumes
- reading three fiscal years assumes DATEV has them open for Max ASP's client; if an old year is missing, the gateway raises a setup error and the proposals wait (#2)

## Follow ups

- A database rule that an incoming invoice cannot go for approval without a proposal fits best in #38, which adds the step to approval.

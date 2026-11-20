# Proposing the G/L account and tax key (#37)

Covers PR-04, with EX-01 and NF-01. Code: `src/argus/intake/accounts.py`, migration 0019.

Approvers should not have to know the chart of accounts. So once an incoming invoice or credit note is matched to its vendor account (#35), ARGUS proposes the G/L account and tax key that vendor was booked to before. The approver sees the proposal (#38), and the booking uses it (#41). Built on the assumed DATEV answers D1 to D9 (Arita, 10 Nov 2026), so it is provisional until the DATEV spike (#2) closes.

## How the proposal is made

At the end of every live sweep, right after vendor matching, ARGUS looks at every matched incoming case in Received or Exception that has no proposal yet. For each vendor account it reads the postings of this year and the two before from DATEV, through the gateway (`list_postings`), once per pass. It then takes the most recent posting and proposes its contra account, always with four digits (DATEV gives 0420 as the number 420), as the G/L account and its tax key as the tax key. A blank tax key stays blank, for example for tax free rent.

**Only accounts an invoice is booked to count.** A payment, a card clearing, a carry forward at the start of the year (9009) or a received cash discount also sits on the vendor account and is often the newest posting. It says nothing about where the invoice belongs. So ARGUS uses an allow list per chart and treats every other account as not history (code review of PR 190):

| Counts as history | SKR03 | SKR04 |
|---|---|---|
| Fixed assets (Anlagevermögen) | 0010 to 0499 | 0010 to 0999 |
| Goods and materials (Wareneingang, Material) | 3000 to 3999 | 5000 to 5999 |
| Expenses (Betriebliche Aufwendungen) | 4000 to 4999 | 6000 to 6999 |
| Cut out: received cash discounts (Erhaltene Skonti) | 3730 to 3739 | 5730 to 5739 |

Everything else is skipped, for example cash and bank, money in transit, card clearing (1730 in SKR03, 3610 in SKR04), liabilities, private accounts, carry forwards and statistical accounts in class 9, and other personal accounts. An account nobody thought of is therefore skipped rather than proposed. The queue form uses the same list, so a person cannot enter 9009 or a bank account either.

The chart is a setting, `ARGUS_CHART_OF_ACCOUNTS` (`SKR03` or `SKR04`). It is `SKR03` when not set, the default Arita confirmed on #18. Any other value stops the job and the web app at start, so a typo never proposes a wrong account. The proposal itself only copies what the history says, so it works with either chart.

**Nothing is guessed (PR-04).** The case goes to the exception queue as `no_account_proposal` when:

- the vendor has no usable posting in this year and the two before, or
- the postings of the most recent day point to more than one account or tax key, for example an invoice split over two accounts. The detail names them.

When DATEV does not answer, the cases wait for the next sweep. A fiscal year DATEV has not set up, such as the new year in early January or a year before Max ASP came to DATEV, holds no postings and is skipped; the other years still count. A real setup problem, such as an unknown client or no year in the range set up at all, is never read as "no history": the case waits and the pass counts it as failed, so healthy invoices never land in the queue because of it.

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

- the allowed account ranges are the DATEV standard numbers; Max ASP's own chart may need more, for example neutral expenses in class 2 (SKR03) or 7 (SKR04) (#18)
- SKR03 3700 to 3769 also holds received rebates and bonuses (Nachlässe, Boni), not only the cash discounts 3730 to 3739, so a bonus credit note booked to 3750 would still become the proposal; to be narrowed once Max ASP's chart is known (QA on PR 190, #18)
- that DATEV answers a fiscal year it has not set up with 404 and the code `fiscal_year_not_found`; the fake does, the real code is a question for #2
- G/L accounts have four digits and personal accounts five, as the gateway assumes
- the fixture postings are dated 2026 and the window follows today's date, so from 2029 the demo and AC-2 find no history for them until the fixtures move on

## Follow ups

- #38: a database rule that an incoming invoice cannot go for approval without a proposal, and the approval page shows the proposal.
- #38 (compliance review of PR 190): once a person entered the account for a case, refuse a later proposal from the history for it, so automation never overrides a person's reasoned choice.
- #38 and #41 (compliance review of PR 190): a blank tax key entered by a person shows clearly as "no tax key" on the approval page, and the booking treats it as something to confirm, not a silent default.
- #41: how an invoice split over two accounts is booked; the queue form takes one account.

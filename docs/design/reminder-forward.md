# Forwarding payment reminders

Issue #27, ADR 0003. Requirements IN-05, PY-06, NF-01, NF-03. Code: `src/argus/intake/reminders.py`, migration 0007.

## What it does

When the sorting (#25) routes an email to `forward_reminder`, ARGUS forwards it to Benjamin with every original attachment. If the reminder names an open supplier invoice, that invoice case gets `reminder_received_at`, so it always goes into the next payment run (PY-06). Then the email moves to the step `routed`.

The forward goes to one address only, set in `ARGUS_REMINDER_FORWARD_TO`. There is no default, because the address belongs to a person and so lives in the settings, not in the repo. The Graph client can forward and send only when that address is set, and it refuses any forward with another recipient, a copy, or a changed message before the call leaves ARGUS.

## Why it can never send two forwards

Graph forwards in two calls: first it saves the forward as a draft, then it sends the draft.

1. **Save the draft.** Graph saves the forward with every attachment of the original. Nothing is sent yet.
2. **Record the draft.** One short transaction records the draft's ID. The database allows only one forward record per email, so a second draft is never recorded.
3. **Send the recorded draft.** Before sending, ARGUS asks Graph whether it is still a draft. Graph itself sends a draft only once.
4. **Record that it was sent.** One short transaction marks the forward sent, marks the matched invoice cases and moves the email to `routed`.

| ARGUS stops after | What the next sweep does |
|---|---|
| step 1 | saves a new draft and sends that one. The first draft was never recorded, so it is never sent. It stays in the Drafts folder of the accounts mailbox. |
| step 2 | finds the recorded draft and sends it |
| step 3 | finds the draft is no longer a draft and only records it |

Graph is never called inside a transaction (PR #79 review), and Graph calls that send are not retried on throttling: the next sweep checks the draft and tries again instead.

## Matching the reminder to an open invoice

ARGUS looks for the invoice number of every open supplier invoice case (not paid, not closed) in the subject, the mail text and the text read from each attachment. The number has to stand on its own: `R-2026-08` does not match inside `R-2026-0811`. Numbers shorter than four characters or without a digit are skipped.

- A number that exactly one open case carries marks that case.
- A number that more than one open case carries marks none of them, because two vendors can use the same number. The forward record counts these as unclear numbers.
- `reminder_received_at` is the time Exchange received the reminder. A later reminder keeps the date of the first one.

The sender is not used yet, because vendor email addresses are not in the master data. Benjamin gets every reminder either way.

## Audit log

The database writes every event itself, and the application cannot write them:

- `reminder_forward_drafted` with the draft's Graph ID
- `reminder_forwarded` with the draft's Graph ID, the matched case IDs and the number of unclear numbers
- `email_routed` from the email steps of #24
- `case_changed` on each marked case, with the old and new `reminder_received_at`

The events hold IDs only, never the forward address, the sender, the subject or any content (NF-03). Forward records cannot be deleted, not even by the owner.

## Still to come

- The sweep calls `forward_reminder` once sorting is wired into it (#91).
- Filing the email out of the Inbox is #26.
- Unsent drafts left by a crash after step 1 stay in Drafts. They are harmless; a later clean up could remove them.
- Migration 0007 follows 0004 for now. 0005 (#91) and 0006 (#26) are built at the same time; whichever is merged last moves its `down_revision` so the chain is one line again.

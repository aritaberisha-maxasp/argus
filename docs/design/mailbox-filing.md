# Filing emails and removing Other

Issue #26, ADR 0003. Requirements IN-06, NF-01.

## What it does

The Inbox is ARGUS's work queue. Once an email has been routed, ARGUS moves it out of the Inbox into its own folder tree, so the Inbox only holds what is still to do.

| Route | Folder |
|---|---|
| invoice processing | ARGUS/Incoming invoices |
| outgoing booking | ARGUS/Outgoing invoices |
| forward to Benjamin | ARGUS/Payment reminders |
| deletion folder (Other) | ARGUS/Removed |
| manual review | ARGUS/Manual review |

ARGUS creates the folders the first time it needs them. The names live in one place (`FOLDER_FOR` in `argus.intake.filing`).

Filing runs at the end of every mailbox sweep, every 5 minutes. It works from the database records at step `routed`, not from the Inbox.

## Other is never deleted straight away

Emails sorted as Other go to ARGUS/Removed. A daily job deletes them 30 days after ARGUS moved them there, and not one day earlier, so a misclassified invoice can still be found (note on IN-06). A deleted email goes to Deleted Items, where Exchange keeps it recoverable for at least 14 more days.

Three layers keep an invoice from being deleted:

1. **The database.** Only an email routed to the deletion folder can be filed in ARGUS/Removed. Only an email in ARGUS/Removed can be recorded as deleted, and only when it has been there 30 days by the database clock (migration 0006). Where and when an email was filed can never change.
2. **The Graph client.** Before it deletes, it checks that the email really is in ARGUS/Removed. If a person moved an email out of ARGUS/Removed to keep it, ARGUS refuses to delete it and logs a failure each day, so someone notices.
3. **The job.** It only picks emails that the database says have been in ARGUS/Removed for 30 days.

## Audit log

The database writes every event itself, in the same transaction as the change. The application cannot write any `email_*` event (migration 0004).

- `email_routed` and `email_filed`: the steps of ADR 0003 (migration 0004).
- `email_moved`: where the email went, with `from_folder`, `to_folder` and `route`. Actor `system:mailbox-filing`.
- `email_deleted`: the folder, when it was filed there and how many days it stayed. Actor `system:mailbox-removal`.

No sender, subject or content goes into the events or the logs. Logs name the internal email record ID only.

## Safe after a crash

Graph is called first, outside any transaction, and the result is recorded afterwards in a short one (PR #79 review). If ARGUS stops in between:

- **After a move:** the next run finds the record still at `routed` and moves it again. Moving an email into the folder it is already in changes nothing, and immutable IDs keep the email's ID the same.
- **After a delete:** the next daily run finds the record still due. Graph says the email is gone, and ARGUS records the deletion. It does the same if a person deleted the email by hand, so the job does not try again every day.

## New Graph calls

The allowlist of the Graph client grows by five calls, all on the one mailbox: list and create folders (at the top and under a folder), read which folder an email is in, move an email, and delete an email. Folder names are plain words only, because they go into a `$filter`. Forwarding (#27) and everything else are still refused.

## Running it

`argus.intake.filing.run_daily` runs the removal job once a day until stopped; `remove_due` runs it once. The Azure setup (#65) decides where the two jobs run.

## Still to come

- **Routing.** Filing needs the step `routed` with its route. Sorting is wired to the mailbox records in #91 (step `classified`). The stories that handle each route then call `mark_routed`. For Other there is nothing else to do, so routing it is just `mark_routed(..., Route.DELETION_FOLDER)` once its decision is recorded.
- **Migration order.** Migration 0005 is kept for #91, so this one is 0006 and follows 0004 for now. Whichever of the two lands second has to follow the other.

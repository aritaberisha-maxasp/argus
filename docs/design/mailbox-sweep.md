# The mailbox sweep

Issue #24, ADR 0003. Requirements IN-01, NF-01, NF-06.

## What it does

Every 5 minutes ARGUS looks at the Inbox of the accounts mailbox. For each email there it:

1. **Seen.** Makes sure the email has one record in the database, keyed by its immutable Graph message ID. The database refuses a second record for the same ID.
2. **Stored.** Reads the email as its raw `.eml` file and puts it, with every attachment, into the document store of #20, byte for byte, named by its SHA-256 fingerprint. Then, in one short transaction, it records the files and moves the email to `stored`.

Classifying, routing and filing come in later stories. The database already knows all five steps (`seen`, `stored`, `classified`, `routed`, `filed`) and refuses to skip one, so nothing can happen to an email before it is stored.

Each step writes an audit event (`email_seen`, `email_stored`) in the same transaction, with the actor `system:mailbox-sweep`. The events hold IDs and fingerprints only, never the sender, subject or content (NF-03). The document store adds its own `document_stored` event per file.

## Why it cannot lose or double an email

- **One sweep at a time.** A sweep holds a PostgreSQL advisory lock while it runs. A sweep that starts while another one runs does nothing and logs that it skipped.
- **One record per email.** Even if two workers ever raced on the same email, the unique ID and the step check let only one of them record it as stored. The store itself never overwrites a file.
- **Safe after a crash.** Every step is recorded before the next one starts. If ARGUS stops halfway, the email is still in the Inbox, the next sweep finds its record and carries on from the last finished step. Storing again gives back the copy that is already there.
- **One bad email does not stop the others.** If one email fails (Graph or the store is unreachable), the sweep logs its ID and moves on. The next sweep tries it again.
- **No outside call inside a transaction.** Graph and the document store are called first, then the result is recorded in a short transaction, because every transaction that writes an audit event holds the audit lock until it commits (review of PR #79).

## Access to the mailbox

The Graph client (`argus.intake.graph`) needs only the application permissions **Mail.ReadWrite** and **Mail.Send**, limited by Exchange to the accounts mailbox (ADR 0003). It checks every token it gets: if the token carries any other Graph permission, ARGUS refuses to make the call, so a too wide grant is noticed at once. A token without any mail roles is fine, because with Exchange RBAC for Applications the rights are granted in Exchange, not in the token.

Every request asks for immutable IDs (`Prefer: IdType="ImmutableId"`) and passes an allowlist of calls on the one mailbox: list the Inbox, read one email as `.eml`, and, only when a reminder address is set, the three calls that forward a payment reminder ([reminder-forward.md](reminder-forward.md)). Anything else, another mailbox or another host, is refused before it leaves ARGUS. Throttling answers (429, 503) are retried after the time Graph asks for.

## Running it

`argus.intake.sweep.run_every` runs the sweep every 5 minutes until stopped; `sweep` runs it once. Sign in through the Azure managed identity comes with the Azure setup (#65); until then everything runs against the fake Graph in [standins/graph](../../standins/graph/README.md).

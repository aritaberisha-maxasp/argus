# Spike: reading and managing the accounts mailbox

Issue: #3
Requirements: IN-01, IN-02, IN-05, IN-06, NF-06 (also touches NF-01, IN-09)
Decision record: [ADR 0003](../adr/0003-reading-the-accounts-mailbox.md)

This note collects what we found out. The ADR holds only the decision.

## The short answer

ARGUS talks to the mailbox through **Microsoft Graph**, the interface Microsoft supports for Exchange Online (NF-06). It signs in as an app, not as a person, and Exchange limits that app to **accounts@maxasp.com and nothing else**. Microsoft tells ARGUS when a new email arrives, but ARGUS never relies on that alone: a sweep every few minutes checks the Inbox and picks up anything that was missed. Every email gets one record in our database keyed by its permanent Exchange ID, and each step on it is recorded before the next one starts, so a crash or a double alert can neither lose an email nor process it twice.

## 1. Permissions (least privilege)

### What ARGUS needs to do in the mailbox

| Task | Graph call | Permission |
| --- | --- | --- |
| See new emails, read body and headers | `GET /users/{mailbox}/mailFolders/inbox/messages` | Mail.ReadWrite (Mail.Read would do for reading alone) |
| Keep the original email unaltered (NF-01) | `GET /messages/{id}/$value` returns the raw MIME file (.eml) | Mail.ReadWrite |
| Read attachments of any size, including PDFs, images and e-invoice XML (IN-02) | `GET /messages/{id}/attachments/{id}/$value` | Mail.ReadWrite |
| Create its own folders | `POST /mailFolders/{id}/childFolders` | Mail.ReadWrite |
| Move emails out of the Inbox (IN-06) | `POST /messages/{id}/move` | Mail.ReadWrite |
| Purge the deletion folder after 30 days | `DELETE /messages/{id}` | Mail.ReadWrite |
| Forward a reminder with its original attachment (IN-05) | `POST /messages/{id}/createForward`, then `POST /messages/{draftId}/send` | Mail.ReadWrite to make the draft, **Mail.Send** to send it |
| Be told about new emails | `POST /subscriptions` on the Inbox messages | Covered by Mail.ReadWrite |

So ARGUS needs exactly two application permissions: **Mail.ReadWrite** and **Mail.Send**. Nothing for calendars, contacts, files, Teams or users.

Forwarding with `createForward` keeps the original attachments on the forwarded email automatically, which is exactly what IN-05 asks for. Making a draft first and sending it second also helps us avoid double sends (see section 2).

### Limiting the app to one mailbox

Granted the normal way in Microsoft Entra, Mail.ReadWrite and Mail.Send would cover **every mailbox in the company**, including the managing director's. That is far too much. Exchange Online offers two ways to narrow it down:

1. **RBAC for Applications (recommended).** An Exchange admin gives the ARGUS app the Exchange roles `Application Mail.ReadWrite` and `Application Mail.Send`, limited by a management scope that contains only accounts@maxasp.com. The Entra admin consent for these two permissions must then **not** be given, because Entra grants add to Exchange grants and would open every mailbox again. Microsoft names this the successor to option 2. It is still marked as preview on the Microsoft page (checked on 15 Oct 2026), so we plan a fallback.
2. **Application access policy (fallback).** Admin consent for Mail.ReadWrite and Mail.Send in Entra, then an Exchange policy that restricts the app to a mail enabled security group containing only accounts@maxasp.com. Older, widely used, still works.

Either way, the admin can check the result with `Test-ServicePrincipalAuthorization` (option 1) or `Test-ApplicationAccessPolicy` (option 2) against accounts@ (allowed) and any other mailbox (denied). We ask for that test output as proof before go live. Changes can take up to two hours to apply.

### What Max ASP has to do (once)

Someone with Global Admin or Exchange Admin rights at Max ASP:

1. Registers two apps in Microsoft Entra: **ARGUS** (production) and **ARGUS test** (sandbox only). No client secrets: production signs in with the Azure managed identity linked to the app registration as a federated credential, as ADR 0001 proposes. A certificate kept in Key Vault is the fallback.
2. Runs the Exchange PowerShell steps that give each app Mail.ReadWrite and Mail.Send on its one mailbox only (production app on accounts@, test app on the sandbox mailbox). We will hand over a ready script with placeholders and the test commands.
3. Sends Arita the output of the authorization test.

Nobody at Max ASP has to share a password with the team, and agents never see the real app credentials.

### Things that do not need any extra permission

- Benjamin is inside the same company, so forwarding to benjamin.taenzer@maxasp.com is internal mail. Outgoing forwarding rules and external forwarding policies do not apply.
- Reading the mailbox through Graph does not need a licence for the app. A shared mailbox needs no licence either while it stays under 50 GB.

## 2. Push versus polling, and never missing or doubling an email

### What Microsoft offers

- **Change notifications (push).** ARGUS subscribes to "new message in the Inbox of accounts@". Graph calls an HTTPS address of ARGUS within seconds. A subscription for mail without the email content in it lasts at most 10,080 minutes (just under 7 days) and must be renewed before it runs out. Graph can also send lifecycle events: `missed` (some notifications were dropped, for example under throttling), `subscriptionRemoved` (start a new subscription and resync) and `reauthorizationRequired` (renew). Microsoft's own guidance is to resync with a sweep after `missed` or `subscriptionRemoved`, which tells us push alone is not reliable.
- **Polling.** ARGUS asks the Inbox regularly. Either a plain list of what is in the Inbox, or a **delta query** that returns only what changed since the last call.

### Comparison

| | Push only | Polling only | Push as a doorbell plus a regular sweep |
| --- | --- | --- | --- |
| How fast a new email is seen | Seconds | The polling interval | Seconds |
| Misses an email when notifications drop | Yes, unless we handle every lifecycle event perfectly | No | No, the sweep catches it |
| Needs a public HTTPS address | Yes | No | Yes, but it can come later |
| Moving parts | Subscription renewal, endpoint, lifecycle events | One scheduled job | Both, but the sweep alone is correct |

### What we propose

**The sweep is the source of truth. The notification is only a doorbell.**

- A **sweep job** runs every 5 minutes (the "mailbox sweep" job in ADR 0001). It lists every message still in the Inbox and processes any it has not finished yet.
- The **notification endpoint** does nothing with the email itself. It checks the `clientState` secret on the call, answers `202 Accepted` at once (Graph expects an answer within a few seconds) and asks for a sweep to run now. Lifecycle events also just trigger a sweep, plus a renewal or a new subscription where needed.
- A daily job renews the subscription when less than two days are left, and recreates it if it is gone.
- Because the notification carries no email content, we do not need the encryption certificate that rich notifications require, and no invoice data ever travels through the webhook.

This means the first story can ship the sweep alone, without any public address, and IN-01 is met with a delay of up to 5 minutes. The doorbell is added when the Azure web app exists and cuts the delay to seconds. Nothing about correctness changes when it is added.

### Why the Inbox works as a queue

ARGUS moves every email out of the Inbox once it is done with it, into its own folders (see section 3). So "what is in the Inbox" is always "what still needs work", and the sweep needs no delta token or other bookmark that could get lost. If Max ASP prefers processed emails to stay in the Inbox, we switch the sweep to a delta query on the Inbox and keep the delta link in the database. That works as well, but adds one more piece of state to look after. This is question 3 for Arita.

### Never twice, never lost

1. **Permanent IDs.** Normal Graph message IDs change when an email is moved to another folder. Every call sends the header `Prefer: IdType="ImmutableId"`, which gives an ID that stays the same for the life of the email. That ID is the key of the email record in our database, with a unique constraint.
2. **One sweep at a time.** The sweep takes a PostgreSQL advisory lock, so a doorbell sweep and a scheduled sweep can never work on the mailbox at the same time.
3. **Small recorded steps.** Each email moves through steps, and each step is written to the database and the audit log (NF-01) before the next starts:
   1. `seen`: record created with the immutable ID, internet message ID, sender, subject, received time.
   2. `stored`: raw .eml and every attachment saved byte for byte to the locked document store, with SHA-256 fingerprints (ADR 0001).
   3. `classified`: the result of classification (issue #5), or manual review when unsure (IN-09).
   4. `routed`: a processing case created, or the reminder forwarded.
   5. `filed`: the email moved out of the Inbox into the matching ARGUS folder.

   If ARGUS crashes, the next sweep finds the email still in the Inbox, looks up its record and continues from the last finished step. Each step checks before acting, for example the store step skips files whose fingerprint is already saved.
4. **Forwarding exactly once.** Sending mail is the one step that cannot be undone. ARGUS first creates the forward as a draft and saves the draft's ID in the record, then sends it, then records `forwarded`. After a crash: if the record has a draft ID and the draft still exists in Drafts, it was not sent and ARGUS sends it; if the draft is gone, it was sent and ARGUS records that. The worst case is one draft left behind, never two forwards to Benjamin.
5. **The same invoice sent twice is a different problem.** A vendor sending the same invoice in two emails produces two records, as it should. Spotting duplicate invoices belongs to invoice processing, not to the mailbox. We do keep the `internetMessageId` so a truly identical email delivered twice can be flagged.
6. **A person moves an email back.** If someone drags an email from an ARGUS folder back into the Inbox, the sweep finds a record that is already `filed`. We treat that as "a human disagrees" and send it to the manual review queue (IN-09) instead of skipping it or processing it blindly again.

### Limits to keep in mind

Graph allows about 10,000 requests per 10 minutes and 4 parallel requests per app per mailbox. A few hundred emails a month is far below that. The client still honours `Retry-After` on HTTP 429 and 503 answers.

## 3. Folders and the 30 day deletion folder (IN-06)

ARGUS creates and owns a folder tree under the Inbox (names are a proposal):

```
Inbox
└── ARGUS
    ├── Incoming invoices
    ├── Outgoing invoices
    ├── Payment reminders
    ├── Manual review
    └── Removed (purged after 30 days)
```

Emails classified as Other go to **ARGUS/Removed**, not straight to Deleted Items. Options for the 30 day purge:

| Option | Pro | Con |
| --- | --- | --- |
| A. ARGUS purges it itself: a daily job deletes emails it moved to Removed more than 30 days ago | Fully in our code, tested, every purge is in the audit log, works the same in the sandbox | ARGUS needs delete rights, which Mail.ReadWrite already includes |
| B. Exchange retention tag on the Deleted Items folder (30 days, admin set) and ARGUS moves Other there | No code | Affects anything else anyone deletes in that mailbox, nothing in our audit log, harder to test |
| C. Exchange retention tag on a custom folder | No code | Admin retention policies cannot target custom folders, only personal tags set by a mailbox user in Outlook. Fragile |

**We propose option A.** The purge job only deletes emails whose record says ARGUS moved them to Removed, counted from the time ARGUS moved them, not the received date. Anything a person puts in that folder by hand is left alone. A Graph `DELETE` puts the email into Exchange's Recoverable Items, where an admin can still restore it for 14 days by default (Max ASP can raise this to 30). So a misclassified invoice gets at least 30 days in a visible folder plus a hidden safety net, which covers the GoBD worry in the source note on IN-06.

Removed emails are not copied to the locked document store (ADR 0001), but their record (sender, subject, dates, classification, who or what removed and purged it) stays in the audit log.

## 4. Testing with a sandbox mailbox

Three layers, from cheap to real:

1. **Fake Graph in Docker for unit tests and CI.** A small stand in that serves the handful of Graph calls we use from synthetic fixtures (issue #9): list Inbox, get MIME, get attachments, move, createForward, send, delete, subscriptions. It can also simulate the hard cases on demand: a dropped notification, a `missed` lifecycle event, a 429 answer, a crash between draft and send, an email moved back by hand. All agents and CI use this. No Microsoft account needed.
2. **Recorded contract checks.** The fake's answers are checked against real Graph answers captured once from the sandbox (with all content synthetic), so the fake cannot drift from reality unnoticed.
3. **A real sandbox mailbox for end to end tests.** Run by hand or nightly, never in normal CI.

Options for the sandbox mailbox:

| Option | Pro | Con |
| --- | --- | --- |
| **A shared mailbox `argus-sandbox@maxasp.com` in Max ASP's tenant**, reached only by the separate "ARGUS test" app scoped to that one mailbox | Same Exchange setup as production, no licence cost, proves the scoping works | Lives in the real tenant, so it needs the same admin steps |
| A separate small Microsoft 365 tenant just for testing (one Business Basic licence, a few euros a month) | Nothing in it can ever touch Max ASP's data | Second tenant to look after, different admin, may differ from production settings |
| Microsoft 365 Developer Program sandbox | Free | Microsoft now only grants it to some Visual Studio subscribers and partners, so we cannot count on it |

**We propose the shared sandbox mailbox in Max ASP's tenant.** It costs nothing, matches production exactly, and the scoping test proves the test app cannot read accounts@ or any other mailbox. Test emails are synthetic, sent from a test sender, and Benjamin's address is replaced by a test recipient through configuration so no real person gets a forwarded test reminder.

## 5. Open questions for Arita (to ask Max ASP)

1. **Mailbox type.** Is accounts@maxasp.com a shared mailbox, or a normal user mailbox with its own licence? Both work; it changes the admin script slightly.
2. **Admin.** Who at Max ASP can do the one time setup (app registrations and the Exchange PowerShell scoping)? We provide the script.
3. **Folders.** May ARGUS move processed emails out of the Inbox into its own subfolders, so the Inbox only holds what is still waiting? Yes keeps the design simplest.
4. **Sandbox.** May we have a shared mailbox `argus-sandbox@maxasp.com` for testing, reachable only by a separate test app?
5. **People in the mailbox today.** Does anyone work in accounts@ by hand today, and are there Inbox rules or other tools on it? They could move emails before ARGUS sees them.
6. **Purge.** Is it fine that ARGUS deletes emails in its Removed folder after 30 days, with Exchange keeping them recoverable for another 14 days (or 30 if Max ASP raises it)?

## Sources (checked 15 Oct 2026)

- [Limit Microsoft Graph access to specific Exchange mailboxes](https://learn.microsoft.com/graph/auth-limit-mailbox-access)
- [Subscription resource type, maximum lifetimes](https://learn.microsoft.com/graph/api/resources/subscription)
- [Reduce missing subscriptions and change notifications (lifecycle events)](https://learn.microsoft.com/en-us/graph/webhooks-lifecycle)
- [message: createForward](https://learn.microsoft.com/en-us/graph/api/message-createforward)
- [Microsoft 365 Developer Program](https://learn.microsoft.com/office/developer-program/microsoft-365-developer-program)

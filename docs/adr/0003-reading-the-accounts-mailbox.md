# 0003: Reading and managing the accounts mailbox

Status: proposed
Requirements: IN-01, IN-02, IN-05, IN-06, NF-06 (also touches NF-01, IN-09)

The full findings are in the [mailbox spike note](../spikes/mailbox-access.md).

## Context

ARGUS must watch accounts@maxasp.com all the time (IN-01), read every attachment (IN-02), forward payment reminders to Benjamin with the original attachment (IN-05), and remove emails classified as Other into a folder that is purged after 30 days (IN-06). It may only use interfaces Microsoft supports (NF-06). It must never lose an email or process one twice, and it should not be able to read any other mailbox at Max ASP.

ADR 0001 (proposed) puts ARGUS on Azure in Max ASP's own tenant, with a 5 minute mailbox sweep job and Graph change notifications.

## Options

**How ARGUS reaches the mailbox**
1. Microsoft Graph with an app identity. The supported, current interface. Con: application mail permissions cover every mailbox unless Exchange narrows them.
2. IMAP or EWS. Con: Microsoft announced it is switching off EWS for Exchange Online from October 2026, and IMAP with app sign in is awkward and offers no notifications. Fails NF-06 in spirit.

**How access is limited to one mailbox**
1. Exchange RBAC for Applications, scoped to accounts@ only, no Entra consent for mail. Microsoft's successor model. Con: still marked preview.
2. Entra consent plus an Exchange application access policy limited to accounts@. Con: older model Microsoft is replacing.

**How ARGUS learns about new emails**
1. Push only (change notifications). Fast. Con: Microsoft itself says notifications can be missed and asks apps to resync.
2. Polling only. Simple and safe. Con: up to one interval of delay, and more calls.
3. A regular sweep as the source of truth, with notifications only as a doorbell that starts a sweep early. Fast and safe. Con: two mechanisms to run.

**How Other emails are purged after 30 days**
1. ARGUS moves them to its own folder and its own daily job deletes them after 30 days, logged in the audit log. Con: our code to maintain.
2. An Exchange retention tag. Con: works only on Deleted Items for admin policies, touches anything else deleted in the mailbox, nothing in our audit log.

## Decision

- **Interface:** Microsoft Graph v1.0 with application permissions **Mail.ReadWrite** and **Mail.Send** only. Every call asks for immutable message IDs.
- **Scope:** both permissions limited by Exchange to accounts@maxasp.com. We use RBAC for Applications and fall back to an application access policy if Max ASP's admin prefers the general availability feature. Max ASP proves the scope with Exchange's authorization test before go live.
- **Sign in:** no client secrets. The app registration trusts the Azure managed identity as a federated credential; a certificate in Key Vault is the fallback (ADR 0001).
- **New emails:** a sweep every 5 minutes lists the Inbox and is the only thing that decides what to process. Graph change notifications (without email content, renewed before their 7 day limit, with lifecycle events) only trigger an early sweep. The first story may ship the sweep alone.
- **No loss, no doubles:** one database record per email keyed by the immutable ID with a unique constraint; one sweep at a time through a PostgreSQL advisory lock; each step (`seen`, `stored`, `classified`, `routed`, `filed`) recorded before the next starts and safe to repeat. Forwarding goes through a saved draft so a crash can never send two forwards.
- **Folders:** ARGUS files every processed email out of the Inbox into its own folder tree, so the Inbox is the work queue. Other goes to `ARGUS/Removed`, and a daily ARGUS job deletes it 30 days after ARGUS moved it there. Exchange still keeps deleted items recoverable for at least 14 days.
- **Testing:** a fake Graph in Docker for CI, checked against recorded real answers, and a separate shared sandbox mailbox reached only by a separate test app for end to end tests.

## Consequences

**Easier**
- ARGUS can technically read only one mailbox, which is easy to explain to Max ASP and to an auditor.
- Correctness does not depend on Microsoft delivering every notification. A missed notification only costs up to 5 minutes.
- Every step on every email is in the audit log, including removals and purges.
- Agents and CI build and test everything against the fake, with no Microsoft account.

**Harder**
- Max ASP's admin has to run an Exchange PowerShell script once per app. We write it, they run it.
- If RBAC for Applications changes while in preview, we may need to switch to the application access policy.
- People working in the mailbox will see emails move into ARGUS folders within minutes.

**To watch**
- If Max ASP does not want emails moved out of the Inbox, the sweep switches to a Graph delta query and stores the delta link. The rest stays.
- Inbox rules or people moving emails before ARGUS sees them would bypass ARGUS. Question 5 in the spike note.
- The webhook needs the public HTTPS address ADR 0001 already accepts; it only accepts calls carrying our `clientState` secret.

## Questions for Arita

See section 5 of the spike note. The one that shapes the design most: may ARGUS move processed emails out of the Inbox into its own subfolders?

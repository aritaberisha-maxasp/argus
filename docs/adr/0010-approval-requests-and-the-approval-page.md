# 0010: How approval requests and the approval page work

Status: proposed
Requirements: AP-02, AP-06, AP-08, AP-10, PR-04, PR-05, VM-04, NF-01, NF-03, NF-04

Issue #191. Builds on ADR 0005 (invoice case and audit log), ADR 0008 (sign in and roles) and the approver table (#34). The approval page itself is #38, questions from the approver are #192.

## Context

Once an incoming invoice or credit note has passed every check, someone has to decide on it. AP-02 says it goes to the approver of its vendor, AP-06 says the deputy decides while the approver is away, and AP-10 says nothing is approved automatically.

What constrains us:

- Approvers work in Outlook. They should not have to look for work in ARGUS.
- An email can be forwarded anywhere, so it must not carry anything that is useful to a stranger: no IBAN, no attachment, no way to approve.
- The person who may decide must be fixed when the request goes out. If the approver table changes later, an invoice must not silently move to someone else, and the audit log must say who was asked and why (NF-01).
- Arita decided on 23 Nov 2026 that an approver may change the proposed G/L account on the approval page (#38), and that above EUR 5,000 the same person may give both approvals (#39). Neither may be ruled out here.

## Options

1. **Approve by replying to the email.** Pro: one click less. Con: anyone who can send mail as the approver, or forge a reply, approves. No sign in, no four eyes check. Rejected.
2. **Email with a link, decide on the page after Microsoft sign in.** Pro: the decision happens where ARGUS knows who the person is (ADR 0008). Con: one sign in per session.
3. **No email, approvers look at a list in ARGUS.** Pro: nothing to send. Con: invoices wait until someone happens to look.

## Decision

Option 2.

- **The step.** At the end of every live sweep, after the vendor match (#35) and the account proposal (#37), ARGUS moves each incoming invoice or credit note that has passed every check from Received to Awaiting approval. Passed every check means: a vendor account, an approver row for it, a duplicate key, a current account proposal and no open exception. The database refuses the move without a proposal, a duplicate key or an approval request written in the same transaction, so no other code path can skip a check.
- **The request record.** The move writes one row in `approval_request`, which can never be changed or removed, except that its sent time is set once. The database itself picks the person from the approver table at that moment: the approver, or the deputy while the approver is marked away and the deputy is not. It stores their object ID, name and address as they were then, whether they are asked as approver or as deputy, and for whom. The audit log gets `approval_requested`, and later `approval_request_sent`. A request is refused at commit unless its case went for approval in the same transaction, so no request can wait to be emailed for a case that never went.
- **Approver away, no deputy who is in.** A deputy who is away too counts as no deputy. No email. The case goes to the exception queue as `approver_absent`. ARGUS resolves it by itself on a later sweep once the approver or the deputy is back, or the vendor has a deputy who is in, and the case carries on. A person can also reject the invoice.
- **The email.** Plain text, from the accounts mailbox, to the one person on the request. It holds the vendor name, invoice number, gross amount and currency, and a link `<ARGUS address>/approvals/<case ID>`. Nothing else: no IBAN, no attachment, no other invoice content. Right before it sends, the Graph client reads the request again from the database by its ID and checks the email against that, not against what the caller hands it. It refuses a request that does not exist, was sent already or whose case no longer waits for approval, an address outside Max ASP's own domain (`ARGUS_OWN_DOMAIN`), and any other recipient or other text, before anything leaves ARGUS. An approver outside Max ASP, such as a tax advisor, would be a decision for Arita. Only the client built for approval requests may send it (#168).
- **Sending.** Graph is called outside any database transaction. A request is marked sent only after Graph accepted it. If sending fails, the next sweep tries again; a request marked sent is never sent again. If ARGUS stops between Graph accepting and the mark, the person can get the same email twice. That is harmless: the link and the decision are the same.
- **Shadow mode.** In shadow mode nothing moves and nothing is sent. ARGUS reads the mode before the pass and again before each email.
- **Who may decide (#38).** The person on the newest request of the case, after Microsoft sign in, and nobody else, also not finance or admin. A reply to the email never counts as a decision; ARGUS does not read replies to these emails.

## Consequences

- #38 builds the page behind the link and checks the signed in person against the newest request. Until #38 is merged the link shows "not found".
- #38 may let the approver change the G/L account while the case is in Awaiting approval (Arita, 23 Nov). The account proposal rules (migration 0019) allow a proposal only in Received or Exception today, so #38 must widen that rule on purpose, with the reason and the person logged.
- #39 needs a second request for the second approval, from Awaiting approval and possibly to Benjamin, who is no vendor's approver or deputy. The insert rule allows a request today only for a case in Received and only to the vendor's approver or a deputy who is in, and it must come with the move to Awaiting approval. #39 must widen that rule on purpose, so that Benjamin may give both approvals above EUR 5,000 as Arita decided. It must write the second request only after the first approval is recorded, and mark it "same person" in the request and in the trail when the two people match (compliance review of PR 200).
- #40 (escalation) adds a new request to someone else while the case waits for approval; the newest request then decides who may approve. It must widen the same insert rule on purpose (case state, who may be asked, and the check at commit). Reminders and escalation count from `sent_at`, not `requested_at`: a request that never left ARGUS must not tell anyone they have not answered.
- #117 (sweep again after a restore) must read `approval_requested` and `approval_request_sent` so it never sends again.
- A person who enters the G/L account by hand (#37) now wins over the booking history: the database refuses a later proposal from the history for the same case and vendor account (compliance review of PR 190).

# Sorting the accounts emails

Covers: IN-04, IN-07, IN-09, AR-01, AR-02, NF-03. Decision: [ADR 0006](../adr/0006-understanding-emails-and-invoices.md). Code: `src/argus/sorting/`.

## What happens to each email

1. **Our checks** (`checks.py`) look at the sender, the subject, the text and every attachment. They name a category only when every sign points to the same one. A pro forma, an unreadable scan, an invoice plus a reminder, an employee forwarding an invoice, an invoice that does not name Max ASP, a Max ASP invoice that did not come from the invoicing agent, or a partner asking about an invoice: each of these leaves the checks without a category.
2. **The language model** (`language_model.py`) proposes one of the four categories with a confidence, lists the invoices it sees and quotes the words it relied on. It runs on Azure OpenAI in Max ASP's tenant, at temperature 0, with a fixed JSON answer. Email content is passed as data, never as instructions.
3. **The routing rule** (`router.py`) routes the email only when the model's confidence is above the threshold for its category and the checks name the same category. Otherwise the email goes to manual review with every reason. A document issued by Max ASP never goes to invoice processing, whatever the model says (AR-02).

| Category | Route |
|---|---|
| incoming invoice or credit note | invoice processing |
| outgoing invoice or credit note | outgoing booking |
| payment reminder | forward to Benjamin (#27) |
| other | the deletion folder, ARGUS/Removed, deleted after 30 days ([#26](mailbox-filing.md)) |
| anything in doubt | manual review |

If Azure OpenAI cannot be reached, the email is not sorted at all and the next sweep tries again. If it answers in the wrong format or its content filter stops the request, the email goes to manual review.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ARGUS_SORTING_THRESHOLD_INCOMING_INVOICE` | 0.85 | confidence needed to route an incoming invoice |
| `ARGUS_SORTING_THRESHOLD_OUTGOING_INVOICE` | 0.85 | the same for an outgoing invoice |
| `ARGUS_SORTING_THRESHOLD_PAYMENT_REMINDER` | 0.85 | the same for a reminder |
| `ARGUS_SORTING_THRESHOLD_OTHER` | 0.95 | the same for Other; ARGUS refuses the settings if this is not the highest |
| `AZURE_OPENAI_API_KEY` | none | from Key Vault, never in the repo |

The defaults are a careful start. The real values come from the two week shadow run (#72).

## Logs and the audit log

Prompts and model answers are never written to the application logs. The logs say only which route an email got. The audit log gets the decision, the reasons and at most 10 evidence snippets of at most 200 characters each (`SortingDecision.audit_details`). Only snippets that really appear in the email are kept, every IBAN in them is masked down to its country code and last four characters, and the reasons name attachments by position ("attachment 2"), never by file name, because file names can hold personal names.

## What is still to come

- **Hooking it up to the mailbox records of #24.** Once #24 is merged, the sweep calls `sort_email` outside any transaction, then moves the email to the `classified` step. The database writes every `email_*` audit event itself (migration 0004), so the follow up adds migration 0005 to put the decision details into the `email_classified` event.
- **Reading the documents** (#28 e-invoices, #29 PDFs and scans) fills the attachment text. Until then the tests read the fixture PDFs directly and use a recorded reading for the two scans.
- **Sender authentication** (#70) and the named safety gates (#71).
- **Recording real answers.** The recorded answers in `tests/fixtures/model-answers/` are written by hand. Once the Azure setup (#65) exists, they are recorded from the real deployment and the fixture run is repeated before every model change.

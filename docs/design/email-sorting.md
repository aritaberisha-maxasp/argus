# Sorting the accounts emails

Covers: IN-04, IN-07, IN-09, AR-01, AR-02, NF-03. Decision: [ADR 0006](../adr/0006-understanding-emails-and-invoices.md). Code: `src/argus/sorting/`.

## What happens to each email

1. **Our checks** (`checks.py`) look at the sender, the subject, the text and every attachment. They name a category only when every sign points to the same one. A pro forma, an unreadable scan, an invoice plus a reminder, an employee forwarding an invoice, an invoice that does not name Max ASP, a Max ASP invoice that did not come from the invoicing agent, or a partner asking about an invoice: each of these leaves the checks without a category.
2. **The language model** (`language_model.py`) proposes one of the four categories with a confidence, lists the invoices it sees and quotes the words it relied on. It runs on Azure OpenAI in Max ASP's tenant, at temperature 0, with a fixed JSON answer. Email content is passed as data, never as instructions.
3. **The routing rule** (`router.py`) routes the email only when the model's confidence is above the threshold for its category and the checks name the same category. Otherwise the email goes to manual review with every reason. A document issued by Max ASP never goes to invoice processing, whatever the model says (AR-02).

## Who sent it (#70)

The From address is easy to fake, so the checks first decide who the sender really is (`sender.py`, `checks.check_sender`).

1. **Who the From address claims to be.** The exact address of the invoicing agent, Max ASP's own domain (an employee), or the exact domain of a vendor or customer in DATEV. The display name never counts, and a domain that only looks like a partner's (`muster-buerobedarf.example.invalid`, `billing.muster-buerobedarf.example`, `muster-buer0bedarf.example`) is an unknown sender.
2. **Whether the email really comes from that domain.** Exchange Online checks SPF, DKIM and DMARC for every email from the internet and writes the result into the `Authentication-Results` header on top of the email. The claim holds only when that header shows a DMARC pass for the From domain, a DKIM pass signed by the From domain, or an SPF pass for an envelope sender in the From domain. A plain SPF pass for some other envelope domain is not enough, and neither is Microsoft's `dmarc=bestguesspass`. Only the topmost `Authentication-Results` header counts: anything further down came with the email and the sender could have written it.
3. **What happens when the claim fails.** The sender counts as unknown, and the email goes to manual review with the reason "the sender claims to be the invoicing agent (or a known vendor or customer) but SPF, DKIM and DMARC do not pass". That holds whatever the model says. An unknown sender on its own is not a doubt: an invoice to Max ASP from a new vendor still goes to invoice processing, where the vendor checks catch it (VM-03, PR-05). It goes to manual review as soon as anything else is in doubt.

Employees on Max ASP's own domain are not checked here, because mail inside the tenant does not always carry the header. An employee who sends an invoice already goes to manual review. But when Exchange says DMARC fails for Max ASP's own domain, the email came from outside and pretends to be from Max ASP, so it goes to manual review too. A DMARC fail for the From domain always wins over an SPF or DKIM pass.

The result is kept in `SortingDecision.sender`, and so with the email (`mailbox_email.classification`) and in the `email_classified` audit event: who the sender claimed to be, who ARGUS treated it as, the From domain, whether and how it passed, and one line per method. Only domains are kept, never the part of an address before the @. Part of the header can come from the sender, and the audit event can never be changed, so what is stored stays small: at most one line per method (the aligned pass if there is one, otherwise the first), only known result words (anything else is stored as `other`), and only real domain names of at most 253 characters (anything else is stored as `(not a domain)`).

**To confirm in the shadow run (#72):** that every email from the internet arrives with the Exchange Online header on top, and that the invoicing agent's emails pass. If the invoicing agent sends through Max ASP's own tenant and its emails carry no header, every outgoing invoice goes to manual review until that is sorted, which is the safe side.

| Category | Route |
|---|---|
| incoming invoice or credit note | invoice processing |
| outgoing invoice or credit note | outgoing booking |
| payment reminder | forward to Benjamin (#27) |
| other | the deletion folder, ARGUS/Removed, deleted after 30 days ([#26](mailbox-filing.md)) |
| anything in doubt | manual review |

If Azure OpenAI cannot be reached, the email is not sorted at all and the next sweep tries again. If it answers in the wrong format or its content filter stops the request, the email goes to manual review.

## Safety gates (#71)

ADR 0006 point 7 lists situations where ARGUS never decides on its own, however sure the model is. Each one is a named gate (`src/argus/sorting/gates.py`). A gate always sends the email to manual review. The decision keeps the names of every gate that fired (`SortingDecision.gates`, and `gates` in the `email_classified` audit event), the reasons say what was found and where, and the exception in the queue starts with "Safety gate:" and the plain names, so the person sees at a glance which rule stopped the email.

| Gate | Name in the trail | Fires when | Checked |
|---|---|---|---|
| Invoice addressed to someone else | `addressed_to_someone_else` | an invoice does not name Max ASP, or names another recipient | before the model, and again on the read invoice data |
| Max ASP invoice not from the invoicing agent | `own_invoice_not_from_agent` | a document issued by Max ASP comes from anyone but the invoicing agent | before the model |
| Employee forwarding an invoice | `employee_forward` | someone on Max ASP's own domain sends an invoice | before the model |
| Pro forma | `pro_forma` | a document's title is pro forma | before the model |
| Unreadable scan | `unreadable_document` | an attachment gives almost no text, or the reader cannot read it | before the model, and again when the invoices are read |
| Invoice and reminder in one email | `invoice_and_reminder` | an invoice and reminder signs in the same email | before the model |
| Question from a customer or vendor | `question` | no document, invoice words and a question mark outside any link, from a known partner, or from an unknown sender when nothing else marks the email as a newsletter or reminder | before the model |
| Failed plausibility check | `failed_plausibility` | one of the checks below fails | after the model, on the read invoice data |
| PDF disagrees with its XML | `pdf_disagrees_with_xml` | the PDF copy states another invoice number, total or IBAN than the e-invoice | after the model |
| E-invoice that cannot be read safely | `einvoice_unreadable` | the XML is refused (a DTD, a broken amount, too large and so on), as a plain XML attachment or inside a PDF | before the model for a plain XML file, and again when the invoices are read |
| Sender fails authentication | `sender_fails_authentication` | the From address claims the invoicing agent, a known partner or Max ASP, but SPF, DKIM and DMARC do not pass (#70) | before the model |
| Mentions an invoice but brings no document | `no_document` | no attachment, invoice words, and a link to click, unless the email already reads as a newsletter. Arita decided on #25 that an invoice only as a link goes to a person; fixture E43 | before the model |

**The deterministic checks after the model** (ADR 0006 point 5, `src/argus/reading/plausibility.py`) run on every invoice of an email sent to invoice processing, once it has been read (#29, #30):

- the net amount plus the VAT amount equals the gross amount, to the cent,
- the VAT amount matches the VAT rate, within two cents, because vendors who round VAT per line can be a cent or two off,
- the IBAN on the invoice has valid check digits,
- the vendor's VAT ID has the format of its country (EU, Northern Ireland, United Kingdom, Switzerland, Norway; any other prefix fails),
- the invoice is addressed to Max ASP (PR-03). Only the recipient line before its first comma counts, labels in front such as "z. Hd." or "Bill to:" are dropped, and what is left must be Max ASP's full legal name and nothing else. "Max ASP Holding AG" is another company and gives no input VAT, so it fails, and so does a department after the name, which is the safe side. The name comes from `SortingConfig.own_company`.

A value ARGUS could not read is not checked here: the reading already flags it for a person and the §14 UStG check (#32) names it as missing. The reasons name amounts but never the IBAN. When any check fails, the email goes to manual review and no case is opened from it.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ARGUS_SORTING_THRESHOLD_INCOMING_INVOICE` | 0.85 | confidence needed to route an incoming invoice |
| `ARGUS_SORTING_THRESHOLD_OUTGOING_INVOICE` | 0.85 | the same for an outgoing invoice |
| `ARGUS_SORTING_THRESHOLD_PAYMENT_REMINDER` | 0.85 | the same for a reminder |
| `ARGUS_SORTING_THRESHOLD_OTHER` | 0.95 | the same for Other; ARGUS refuses the settings if this is not the highest |
| `AZURE_OPENAI_API_KEY` | none | from Key Vault, never in the repo |

`SortingConfig.vendor_domains` and `customer_domains` hold the domains the partners in DATEV really send from, written exactly as in their From addresses. DATEV has no field for them, so ARGUS keeps the list. The test data is in `tests/fixtures/master-data/sender-domains.csv`.

The defaults are a careful start. The real values come from the two week shadow run (#72).

## Logs and the audit log

Prompts and model answers are never written to the application logs. The logs say only which route an email got. The audit log gets the decision, the reasons and at most 10 evidence snippets of at most 200 characters each (`SortingDecision.audit_details`). Only snippets that really appear in the email are kept, every IBAN in them is masked down to its country code and last four characters, and the reasons name attachments by position ("attachment 2"), never by file name, because file names can hold personal names.

## What is still to come

- **Hooked up to the mailbox records of #24 (#91).** The sweep reads the stored copy with `argus.intake.email_text.EmailReader`, calls `sort_email` outside any transaction, then records the decision with `mark_classified` in a short unit of work. Migration 0005 keeps the decision on the record and the database puts it into the `email_classified` event; the application still cannot write any `email_*` event. The route the email later gets must match the recorded decision. The sweep then routes it from the recorded decision, so a retry never asks the model again.
- **Reading the documents** (#28 e-invoices, #29 PDFs and scans) fills the attachment text. Until then the tests read the fixture PDFs directly and use a recorded reading for the two scans.
- **Recording real answers.** The recorded answers in `tests/fixtures/model-answers/` are written by hand. Once the Azure setup (#65) exists, they are recorded from the real deployment and the fixture run is repeated before every model change.

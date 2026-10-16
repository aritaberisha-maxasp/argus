# 0006: How ARGUS understands emails and reads invoices

Status: proposed
Requirements: IN-02, IN-03, IN-04, IN-08, IN-09, PR-01, PR-03, NF-02, NF-03 (also touches NF-01, AC-1)

The full findings, the prototype and its measured results are in the [document understanding spike note](../spikes/document-understanding.md).

Numbered 0006 because 0002 belongs to the DATEV spike, 0004 is left for the Wise spike and 0005 to the invoice case model (PR #17).

## Context

Every email in the accounts inbox must land in one of four categories (IN-04): incoming invoice or credit note, outgoing invoice or credit note, payment reminder, or Other. When ARGUS is unsure it must hand the email to a person (IN-09). One email can hold several invoices (IN-08). Invoices arrive as PDF, as scans (IN-03), as XRechnung or ZUGFeRD (NF-02) and sometimes in the email body. From each invoice ARGUS needs the PR-01 fields and the §14 UStG checks (PR-03). Everything must be processed in the EU (NF-03).

The costly mistakes are not equal. An invoice sent to the deletion folder is lost money and a GoBD problem. An email sent to manual review costs a person a minute.

We built a prototype and ran it on QA's 50 synthetic emails and on 16 new emails it had never seen.

## Options

**How to read the documents**
1. **Own rules and regular expressions on PDF text, Tesseract for scans.** Free and runs anywhere. Con: the prototype got 50 of 50 on the emails the rules were written for, but only 8 of 16 on unseen layouts. Every new vendor layout would need new rules.
2. **Azure AI Document Intelligence** (Microsoft's OCR and invoice reader) in an EU region. Reads scans and any layout, gives each field with a confidence, works page by page. Con: usage based cost, and its invoice model does not decide "is this a reminder or an advert".
3. **A language model alone**, given the raw PDF. Con: weaker on scans than a dedicated OCR, and harder to prove where each value came from.

**How to classify the email**
1. **Rules only.** Explainable. Con: brittle, as the hold out run shows.
2. **A language model in an EU deployment** that reads the email, the extracted text and the fields, and returns a category, one entry per invoice, the words it relied on and a confidence, in a fixed JSON format. Con: a model can be confidently wrong, so it needs checks around it.
3. **A custom trained classifier.** Con: needs hundreds of labelled real emails we do not have yet.

**How to decide when ARGUS is unsure**
1. **Trust the model's confidence.** Con: models are often overconfident.
2. **Model confidence plus cross checks plus hard safety gates.** Route automatically only when all three agree. Con: more emails go to review at first.

## Decision

A layered pipeline where structured data always wins and every doubt goes to a person:

1. **E-invoices first (NF-02).** If an email has XRechnung or ZUGFeRD XML (as a file or embedded in the PDF), ARGUS takes every field from the XML with its own parser. The XML is the legal original for the values ARGUS books. The whole email, the XML and the PDF are all stored unchanged as received (ADR 0001, NF-01); the XML never replaces the stored original. An XML file and a PDF with the same issuer and invoice number are one case. If the PDF and the XML disagree on invoice number, total or IBAN, the email goes to manual review with both values shown, because the person approving sees the PDF, and a mismatch is a known fraud pattern. In the build stories the XML is also checked with the official KoSIT validator, which runs as a small container next to ARGUS. XML with a DTD is refused, values are read whole so a comment cannot cut them short, and any XML that cannot be read (a broken amount, for example) sends the email to manual review with the reason, never stops the run (EX-01).
2. **Text and fields from Azure AI Document Intelligence** for every PDF and image without XML: the layout model for text, the invoice model for PR-01 fields with a confidence per field. Tesseract stays for local development and CI only.
3. **A language model classifies** each email (IN-04) and lists every invoice it contains (IN-08), using Azure OpenAI in Max ASP's own Azure tenant, temperature 0 and a fixed JSON output. Only a Data Zone (EU) deployment or a regional deployment in an EU region is allowed. Global deployment types are not, because they can process prompts in any region; the infrastructure code enforces this (for example an Azure Policy on allowed locations and deployment types), so nobody switches to Global later to get a newer model. The model also fills fields the invoice model missed, and says on which page and in which words it found them. Email content comes from strangers and can carry instructions aimed at the model, so the model is never the only source for the IBAN, the payee or an amount: those must come from the XML, from Document Intelligence or from text our checks can find on the page, and PR-05 (the IBAN must match DATEV) still decides.
4. **Sender authentication.** The From address is easy to fake. ARGUS reads the `Authentication-Results` header that Exchange Online adds and treats a sender as the invoicing agent or a known vendor or customer only when SPF, DKIM or DMARC passes for that exact domain. A look alike domain is never a match. Otherwise the sender counts as unknown, and an email from an unknown sender that carries an invoice goes to manual review if anything else is in doubt; one that claims to come from Max ASP's invoicing agent always does.
5. **Deterministic checks after the model.** Net plus VAT equals gross, VAT matches the rate, IBAN check digits, VAT ID format, Max ASP is the recipient (PR-03), the number of invoices the model found matches the documents and pages. These are the prototype's rules, kept as a second opinion.
6. **Automatic routing only when everything agrees.** The model's confidence is above the threshold for that category, the model and the checks name the same category, and no safety gate fires. Moving an email to the deletion folder needs a higher threshold than any other route. Otherwise the email goes to the manual review queue with the reason shown.
7. **Safety gates, each sending the email to review:** an invoice addressed to someone else, a Max ASP invoice not sent by the invoicing agent, an employee forwarding an invoice, a pro forma, an unreadable scan, an invoice and a reminder in one email, a question from a customer or vendor, a failed plausibility check, a PDF copy that disagrees with its XML, an e-invoice that cannot be read safely, a sender who claims to be the invoicing agent or a known partner but fails authentication, and any email that mentions an invoice but brings no document (for example an invoice only as a download link).
8. **Logs.** Full prompts and model answers are not written to the application logs or Log Analytics. The audit log keeps only the evidence snippets, under the same role based access as the documents (NF-03).
9. **Shadow run before go live.** For two weeks ARGUS classifies real mail and a person routes it as today. We set the thresholds from that data so that no invoice is ever proposed for deletion, then switch on automatic routing.

Everything runs in Max ASP's Azure tenant in the EU, under the Microsoft data processing terms Arita already approved for ADR 0001 (NF-03).

## Consequences

**Easier**
- New vendor layouts and scans need no new code.
- E-invoices, which are becoming the norm in Germany, are read exactly, with no OCR or model involved.
- Every routing decision has a stored reason: the model's evidence, the checks and the gates. That goes into the audit log (NF-01).
- The prototype's checks and gates carry over into the product almost unchanged.

**Harder**
- Two more Azure services to set up, with usage based cost. At a few hundred emails a month we estimate EUR 5 to 20 a month in total; the spike note shows the sums.
- Model answers can change when Microsoft updates a model. We pin the model version and rerun the fixture set and the hold out set before every model change.
- More emails go to manual review in the first weeks. That is deliberate.

**To watch**
- AC-1 counts a manual review as wrong unless the email was meant to be uncertain, so too many gates hurt AC-1 while keeping money safe. If we must choose, we keep the gate.
- Azure OpenAI may keep prompts and answers for up to 30 days for abuse monitoring, and authorised Microsoft staff may look at content that gets flagged. Before shadow mode starts, because shadow mode already sends real mail, we confirm that this storage stays in the EU and whether Max ASP can get the exemption.
- Max ASP may need to add the two services to its GDPR record of processing activities (Article 30). That is Max ASP's call; question 1 asks Arita to check it.
- The prototype found one silent miss: two invoices on two pages of one PDF became one case. The build story must check page by page. Cases are keyed on issuer plus invoice number, so two vendors that both use number 1001 stay two cases.

## Questions for Arita

Posted on issue #5 with the label `needs-arita`:

1. May ARGUS send email and invoice content to Azure OpenAI and Azure AI Document Intelligence in Max ASP's own Azure tenant, processed only in the EU? Microsoft may keep prompts and answers for up to 30 days to detect abuse, and authorised Microsoft staff may look at flagged content, unless Max ASP is granted the exemption. Max ASP may also need to add these services to its GDPR record of processing.
2. May we run ARGUS for two weeks in shadow mode on real mail (ARGUS proposes, a person still sorts the inbox) before it routes anything on its own?

# 0005: How ARGUS understands emails and reads invoices

Status: proposed
Requirements: IN-02, IN-03, IN-04, IN-08, IN-09, PR-01, PR-03, NF-02, NF-03 (also touches NF-01, AC-1)

The full findings, the prototype and its measured results are in the [document understanding spike note](../spikes/document-understanding.md).

Numbered 0005 because 0002 belongs to the open DATEV spike and 0004 is left for the open Wise spike.

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

1. **E-invoices first (NF-02).** If an email has XRechnung or ZUGFeRD XML (as a file or embedded in the PDF), ARGUS takes every field from the XML with its own parser. The XML is the legal original; the PDF is only a copy for people. An XML file and a PDF with the same invoice number are one case. In the build stories the XML is also checked with the official KoSIT validator, which runs as a small container next to ARGUS.
2. **Text and fields from Azure AI Document Intelligence** for every PDF and image without XML: the layout model for text, the invoice model for PR-01 fields with a confidence per field. Tesseract stays for local development and CI only.
3. **A language model classifies** each email (IN-04) and lists every invoice it contains (IN-08), using Azure OpenAI in Max ASP's own Azure tenant with an EU data zone or EU regional deployment, temperature 0 and a fixed JSON output. It also fills fields the invoice model missed, and says on which page and in which words it found them.
4. **Deterministic checks after the model.** Net plus VAT equals gross, VAT matches the rate, IBAN check digits, VAT ID format, Max ASP is the recipient (PR-03), the number of invoices the model found matches the documents and pages. These are the prototype's rules, kept as a second opinion.
5. **Automatic routing only when everything agrees.** The model's confidence is above the threshold for that category, the model and the checks name the same category, and no safety gate fires. Moving an email to the deletion folder needs a higher threshold than any other route. Otherwise the email goes to the manual review queue with the reason shown.
6. **Safety gates, each sending the email to review:** an invoice addressed to someone else, a Max ASP invoice not sent by the invoicing agent, an employee forwarding an invoice, a pro forma, an unreadable scan, an invoice and a reminder in one email, a question from a customer or vendor, a failed plausibility check, and any email that mentions an invoice but brings no document (for example an invoice only as a download link).
7. **Shadow run before go live.** For two weeks ARGUS classifies real mail and a person routes it as today. We set the thresholds from that data so that no invoice is ever proposed for deletion, then switch on automatic routing.

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
- Azure OpenAI may keep prompts for up to 30 days for abuse monitoring. Before go live we check where that storage sits and whether Max ASP can apply for the exemption.
- The prototype found one silent miss: two invoices on two pages of one PDF became one case. The build story must check page by page.

## Questions for Arita

Posted on issue #5 with the label `needs-arita`:

1. May ARGUS send email and invoice content to Azure OpenAI and Azure AI Document Intelligence in Max ASP's own Azure tenant, processed only in the EU?
2. May we run ARGUS for two weeks in shadow mode on real mail (ARGUS proposes, a person still sorts the inbox) before it routes anything on its own?

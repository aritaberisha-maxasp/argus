# Reading the invoice data

Issue #29, ADR 0006. Requirements IN-02, IN-03, PR-01, NF-03, EX-02. Code: `src/argus/reading/`.

## What it does

For every PDF, image or scan attached to an email, and for an invoice written in the email body, ARGUS reads the PR-01 fields: issuer, VAT ID, invoice number, invoice date, service period, net amount, VAT rate and amount, gross amount, currency, due date, early payment discount and IBAN. It also reads the recipient, which the §14 UStG check (#32) needs. E-invoice XML is read by its own parser (#28), not here.

Every field keeps:

| | |
|---|---|
| value | in the formats of `tests/fixtures/documents/expected.json`: `"297.50"`, `"2026-10-02"`, `"19"`, the IBAN without spaces |
| source | Document Intelligence, the page text (our own rules), or the language model |
| confidence | Document Intelligence's own confidence from 0 to 1, when it gives one |
| page and words | where on the document the value stands |
| flag | why a person must look at it, if they must |

`InvoiceReading.as_record()` gives all of this as JSON. Storing it with the case comes with #30, which turns a reading into one case per invoice.

## How a document is read

1. **The reader.** In production, Azure AI Document Intelligence with the prebuilt invoice model, in Max ASP's tenant in an EU region. One call gives the text of every page and the invoice fields with a confidence each, so the separate layout call ADR 0006 mentions is not needed. ARGUS deletes the result from the service as soon as it has it, instead of leaving it there for 24 hours. In development and CI the local reader takes over: the PDF text layer through pypdf, and Tesseract for images and PDFs without text. A recorded stand in serves Document Intelligence answers in the real format (`tests/fixtures/document-intelligence/`).
2. **How sure the reader is.** A field below 0.80 is kept but flagged for a person. A field below 0.50 counts as not read.
3. **The page text.** Our own rules from the spike read every field the page states plainly. They fill what the reader missed, and if the reader and the page disagree on the invoice number, an amount or the IBAN, the field is flagged.
4. **The language model** may propose the fields that are still missing. It must name the page and quote the words, including the label next to the value. ARGUS keeps a value only when those words stand on that page and our own label rules read that very field from them: "Gesamtbetrag 1.190,00 EUR" can give the gross amount, but "Nettobetrag 100,00 EUR" cannot, even though the number is in it. The same holds for the VAT, the currency, the IBAN (which must also have the right check digits) and both dates. So the model never chooses which number or IBAN on the page is which field. Because the model still chose which words to quote, an amount or IBAN it located is kept with the flag `from_model`, and a person confirms it before it is used: a label inside a sentence ("unser Umsatz Total 9.999,00 EUR") would otherwise read as the gross amount. The issuer and recipient names, the service period and the discount are never taken from the model, because a quote cannot show that a name is the issuer's. PR-05 then checks the IBAN against DATEV in any case.
5. **Anything still missing** stays empty with the flag `not_found`. ARGUS never guesses: no first line taken as the issuer, no currency assumed. A missing discount is not flagged, because most invoices have none, and neither is a missing due date on a credit note.

ARGUS also reports, for a person to see:

- **An IBAN with wrong check digits** on the page, flagged `failed_check`.

- **Several invoices in one file.** If the pages state more than one invoice number, or Document Intelligence finds more than one invoice, the reading says so. The spike once merged two invoices of one PDF into one case without a warning (H04); #30 splits them.
- **Several VAT rates or IBANs.** The field stays empty and is flagged `several_values` with the values listed. For IBANs this counts every IBAN the page states, after an IBAN label or anywhere with valid check digits, whichever reader read the field, so an email body with two IBANs is never paid to the first one silently.

## When a document cannot be read

A document is unreadable when it has fewer than 40 readable characters, when the words are read with a confidence under 0.60, or when the reader refuses it (a broken PDF, a format it does not take). `queue_unreadable` then opens one exception with the reason unreadable document on the email, naming every unreadable attachment in the detail ("The attachment 2 could not be read: ..."). The audit log gets only the positions and the readers, never the file name or content (NF-03). Calling it twice for the same email opens one exception.

If Document Intelligence or the language model cannot be reached, the reading raises `ReaderUnavailable` or `ModelUnavailable`, and the email is tried again in the next sweep.

## The §14 UStG check

After reading, `check_invoice` (in `argus/reading/formal_check.py`, #32, PR-03) checks each incoming invoice for the items an invoice needs so Max ASP can claim the input VAT. An e-invoice is checked from its XML with `check_einvoice`, never from its PDF copy.

| List | When | Items |
|---|---|---|
| §14 Abs. 4 UStG | gross amount over EUR 250, any foreign currency, or gross amount not read | Max ASP as the recipient, vendor name, invoice number, invoice date, VAT ID or tax number, service date or period, net amount, VAT rate, VAT amount |
| §33 UStDV | gross amount up to and including EUR 250.00 | vendor name, invoice date, gross amount, VAT rate |

An invoice that charges no VAT must say why (reverse charge, an exemption, §19 UStG). A VAT rate of 0 without that note fails. A field read with a flag counts as there, because the flag already sends it to a person. A field ARGUS could not read counts as missing, so the message says "missing or not readable".

Only documents of kind invoice are checked: not credit notes, cancellations, pro formas, or invoices issued by Max ASP. Addresses and the quantity and kind of goods are not read yet (PR-01 has no such fields), so a person checks them when approving.

A failed check goes to the exception queue through `argus.einvoice.queue_failed_checks(..., formal=checks)` with the reason `invoice_check_failed`. It shares that one exception per email with the e-invoice checks. The detail starts with "Failed §14 UStG check" and names every missing item. The audit log gets only the item names (`missing_items`), never the values (NF-03).

## The type of a file

ARGUS decides whether a file is a PDF, JPEG, PNG or TIFF from its first bytes, not from the content type in the email, which the sender chose. Anything else is not read here.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `AZURE_DOCUMENT_INTELLIGENCE_API_KEY` | none | from Key Vault, never in the repo |
| `ReadingSettings.flag_below` | 0.80 | a field below this confidence is flagged |
| `ReadingSettings.ignore_below` | 0.50 | a field below this confidence counts as not read |
| `ReadingSettings.min_text_confidence` | 0.60 | a scan read less surely is unreadable |

The defaults are a careful start. The real values come from the shadow run (#72).

## Logs

Document content, prompts and model answers never go to the application logs. The logs say only how many pages were read or that a document was unreadable.

## Running it locally

Tesseract is a program: `apt-get install tesseract-ocr tesseract-ocr-deu`. Without it the OCR tests are skipped locally; in CI they fail.

## What is still to come

- **Hooking it into the sweep** after #91, so the text of each attachment also feeds the sorting instead of the readings in `tests/fixtures/readings`.
- **Real recordings.** The Document Intelligence answers in `tests/fixtures/document-intelligence/` are written by hand from `expected.json` (`tests/fixtures/tools/record_document_intelligence.py`). Once the Azure setup (#65) exists, they are recorded from the real service, and the hold out set of the spike is run once against it to see how far AC-1 moves on unseen layouts. Our rules were not tuned to the hold out set, so it stays an honest test.

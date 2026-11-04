# One case per invoice

Issue #30. Requirements IN-08, IN-09, NF-01, NF-03. Code: `src/argus/intake/invoices.py`, `src/argus/reading/split.py`, migration `0009_invoice_split.py`.

## What it does

Suppliers sometimes send several invoices in one email, as separate attachments or as several invoices in one PDF. When the sorting sends an email to invoice processing, the sweep reads every invoice in it and opens one case for each. All of them point back to the same email, each is linked to its own document, and each keeps its own reading.

It happens in the step that records the sorting decision, in the same unit of work. So sorting the same email twice never opens a case twice, and an email whose invoices do not add up goes to manual review instead of being routed.

## Reading the invoices of an email

1. **Each attachment** is read by the document reader of #29 (Document Intelligence in production). A file is split page by page before its fields are read:
   - a page that states an invoice number the pages before it did not state starts a new invoice;
   - so does a page with the same number but a VAT ID the pages before it did not state, because two vendors may both use number 1001;
   - a page without a number belongs to the invoice before it (a second page, the terms), and pages before the first number belong to the first invoice.
2. **The reader's own view.** Document Intelligence says how many invoices it sees and on which pages. Each part of the file gets the reader's fields of the one invoice the reader found on exactly those pages; our own rules read the rest.
3. **The email text** is read too, for an invoice written in the email itself (E16).
4. **E-invoices.** An XRechnung or ZUGFeRD and its PDF copy are one invoice (#28). The case takes the XML's values; the PDF copy is stored next to it for the person who checks.

## When the email goes to a person instead

ARGUS does not guess. The email goes to manual review with the reason (IN-09), and an exception opens for it, when:

- the language model listed another number of invoices than the documents and pages show (the model's count is kept in the decision as `model_invoices`, so routing never asks the model again);
- one page states two invoice numbers, so it cannot be split by page;
- the reader sees another number of invoices than the pages show, or puts one invoice across two;
- an invoice has no number or issuer ARGUS can read, or appears twice in the email;
- a document or an e-invoice cannot be read;
- the PDF copy of an e-invoice states another number, total or IBAN than its XML (#28).

The reasons name documents by position and page, never by file name, and never quote a value (NF-03).

## The case key

Each case is keyed on its issuer plus its invoice number: `vat:<VAT ID>|<number>`, or `name:<issuer name>|<number>` when the invoice states no VAT ID. The name is lower case, without its legal form, with umlauts written out. The number loses its spaces and is upper case.

The database refuses a second case with the same key for the same email, and the key never changes once set. Duplicates across emails are #31's: it can build `duplicate_key` from the case key, the gross amount and the currency.

## What a case gets

| | |
|---|---|
| kind | incoming invoice, or incoming credit note for a credit note or cancellation |
| `source_email_id` | the email |
| `case_key` | as above |
| `document_number` | the invoice number |
| `gross_minor`, `currency` | only when no field flag asks a person to check them first |
| `case_document` | the file the invoice stands on; two invoices in one PDF link the same file |
| `invoice_reading` | the reading of the invoice, with its pages, and the PDF copy of an e-invoice |

`invoice_reading` holds full IBANs and quotes from the invoice, so it is a table of its own and never goes into the audit log (compliance on PR 94). It is insert only. The database writes the audit event `invoice_read` with the IDs, the pages, the reader, the kind and the names of the flagged fields, never a value. Who may see a reading in the app comes with sign in and roles (#23).

## Not in this story

- Outgoing invoices get their cases with #52.
- The reader is called twice per invoice email, once for the sorting and once for the split. Reusing the first reading is a cost saving for later; the recorded stand ins make it free in CI.

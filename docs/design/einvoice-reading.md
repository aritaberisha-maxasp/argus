# Reading e-invoices

Issue #28, ADR 0006 point 1. Requirements IN-02, NF-02, PR-01, EX-01. Code: `src/argus/einvoice/`.

## What it does

ARGUS reads XRechnung (UBL and CII) and ZUGFeRD or Factur-X (CII) e-invoices with its own parser and takes every PR-01 field from the XML. The XML is the legal original for the values ARGUS books, so no OCR, no Document Intelligence and no language model is involved. The result is an `InvoiceReading` in the same shape as a PDF reading (#29, [invoice reading](invoice-reading.md)), with the source `einvoice_xml` on every field and the reader `einvoice_xml`.

| Where the XML is | How ARGUS finds it |
|---|---|
| An XML file attached to the email (XRechnung) | Any attachment whose bytes start like XML. The file name and content type are chosen by the sender and not trusted. |
| Inside a PDF (ZUGFeRD, Factur-X) | The embedded file `factur-x.xml`, `zugferd-invoice.xml` or `xrechnung.xml` of a PDF attachment. |

`read_einvoices(attachments)` returns every e-invoice of one email, plus the ones it refused. XML that is not an invoice or credit note in UBL or CII (an order, for example) is left alone.

The email, the XML file and the PDF stay stored exactly as they arrived (#20, `store_email`). Reading never changes or replaces a stored file; `EInvoice.xml` holds the bytes as received, for the validator.

## The fields

The values use the formats of `tests/fixtures/documents/expected.json`, like the PDF readings.

| Field | UBL | CII |
|---|---|---|
| issuer, recipient | `RegistrationName`, else `PartyName` | `SellerTradeParty/Name`, `BuyerTradeParty/Name` |
| vendor VAT ID | seller `PartyTaxScheme` with scheme VAT | seller `SpecifiedTaxRegistration` with scheme VA |
| number, date | `ID`, `IssueDate` | `ExchangedDocument/ID`, `IssueDateTime` |
| service period | `InvoicePeriod`, else the delivery date | `BillingSpecifiedPeriod`, else the delivery date |
| net, VAT, gross | `TaxExclusiveAmount`, `TaxTotal/TaxAmount`, `TaxInclusiveAmount` | `TaxBasisTotalAmount`, `TaxTotalAmount`, `GrandTotalAmount` |
| VAT rate | `TaxSubtotal/TaxCategory/Percent` | header `ApplicableTradeTax/RateApplicablePercent` |
| due date | `DueDate`, else `PaymentMeans/PaymentDueDate` | `SpecifiedTradePaymentTerms/DueDateDateTime` |
| IBAN | `PayeeFinancialAccount/ID` | `PayeePartyCreditorFinancialAccount/IBANID` |
| discount | `#SKONTO#TAGE=10#PROZENT=2.00#` in the payment terms, counted from the invoice date | the same |

The rules ARGUS adds:

- **The VAT in the invoice currency.** An invoice in USD may state the VAT a second time in EUR (BT-111). Only the amount in the invoice currency counts.
- **Several values.** Several VAT rates, IBANs, VAT IDs or discounts leave the field empty with the flag `several_values`, as in a PDF reading.
- **A wrong IBAN** (bad check digits) is kept and flagged `failed_check`. PR-05 checks it against DATEV in any case.
- **Direct debit** (payment means 49 or 59): the money is collected, so there is no payee IBAN, and `EInvoice.direct_debit` is true.
- **Missing fields** that an invoice must have are flagged `not_found`. A credit note needs no due date and no invoice needs a discount.
- **Credit notes** (type codes 381, 261, 396, 532, and every UBL CreditNote) have the kind `credit_note`, and `refers_to` names the invoice they correct.

## What is refused

Anything ARGUS cannot read safely refuses the whole e-invoice. The email then goes to manual review with the reason unreadable document, through `argus.reading.queue_unreadable`, in one exception with any other unreadable file of the same email. Nothing stops the run (EX-01).

- **A DTD** (`<!DOCTYPE`), whatever it holds. XRechnung and ZUGFeRD never need one, and entities could make the value ARGUS reads differ from what a viewer shows, blow up memory, or reach files and the network. The parser is the standard library's expat with every DTD handler set to refuse.
- **Not well formed XML**, or a file larger than 5 MB.
- **An amount** that is not a number, is infinite, or has more than two decimals (rounding would book a value the XML does not state).
- **A date** that is not a date, or a CII date in another format than `CCYYMMDD`.
- **A VAT rate** outside 0 to 100.
- **A document type** that is not an invoice or credit note, or no invoice number, issuer, total or currency.
- **ZUGFeRD 1**, out of use since 2020.

Comments and processing instructions inside a value are dropped and the text around them is joined, so `147<!-- x -->.56` is read as `147.56`, never as `147`.

## One invoice from an XML and its PDF

`pair(einvoices, pdf_readings)` gives each e-invoice its PDF copy:

1. A ZUGFeRD XML belongs to the PDF it is embedded in.
2. Otherwise a PDF reading is the copy when it states the same invoice number (ignoring case and spaces) and no other issuer. The VAT ID decides when both state one, else the names. Two vendors that both use number 1001 stay two invoices.
3. If exactly one e-invoice and exactly one PDF invoice from the same issuer are left, the PDF is the copy even though the numbers differ. That difference then reaches a person instead of becoming a second case.

Each `EInvoiceWithCopy` lists its documents by position. #30 creates one case from it that links all of them, and the PDF readings that belong to no e-invoice stay invoices of their own.

### When the PDF disagrees

ARGUS books from the XML, but the person approving sees the PDF, and a PDF copy that says something else than its XML is a known fraud pattern. So when the copy states another invoice number, total or IBAN, the email goes to manual review with the reason `invoice_check_failed`, showing both values:

> The PDF copy (attachment 2) and the e-invoice (attachment 1) disagree. total: 147.56 in the XML, 174.56 on the PDF; IBAN: DE\*\*\*\*\*\*\*\*\*\*\*\*\*\*\*\*0002 in the XML, DE\*\*\*\*\*\*\*\*\*\*\*\*\*\*\*\*3000 on the PDF. ...

IBANs are masked like everywhere in the queue. When two IBANs end in the same four digits, the detail says they differ in the hidden digits. A value the PDF reader could not read is no disagreement, because nothing on the PDF contradicts the XML.

A PDF that states several IBANs has its IBAN field left empty by the reading (`several_values`). The XML IBAN is then compared against every IBAN on the PDF, and any IBAN that is not the XML's is a disagreement, even when the XML's is listed too. A line like "please pay only to ..." on the PDF would otherwise send the approver to another account. The detail lists every IBAN, masked, and marks the one in the XML.

## The official check

Every e-invoice is checked with the KoSIT validator, the reference check for XRechnung. It runs as its own container (`docker/kosit-validator`) with the official XRechnung 3.0.2 configuration, which also holds the EN 16931 rules for ZUGFeRD and Factur-X. Both downloads are pinned by checksum in the Dockerfile.

`KositValidator(url).validate(xml)` posts the XML and reads the report. When the validator does not accept the XML, the email goes to manual review with the reason `invoice_check_failed`, naming up to ten rules it broke (for example `BR-DE-15`). XML for which the validator has no rules at all (a ZUGFeRD MINIMUM or BASIC WL file, which is no valid invoice in Germany) is not accepted either. When the validator cannot be reached or gives no usable answer, `ValidatorUnavailable` is raised and the email is tried again in the next sweep, so an outage never lets an unchecked e-invoice through.

### One exception per email

The exception queue allows one open exception per email and reason (`exception_open_once`, #19). So `queue_failed_checks(connection, email_id, invoices, validations)` puts every failed check of every e-invoice on the email into one exception, the way `queue_unreadable` does for unreadable documents. It takes the paired e-invoices and the validator's answer for each of them by document, and refuses to run when one has no answer, so an unchecked e-invoice never looks like one that passed. The detail has one paragraph per problem. The audit facts list `checks`, `documents`, `copies`, `fields`, `scenarios` and `rules`, never values. When the email already has an open `invoice_check_failed` exception, that one is returned.

Running it locally:

```
docker build -f docker/kosit-validator/Dockerfile -t argus-kosit-validator .
docker run -d --name kosit -p 8090:8080 argus-kosit-validator
ARGUS_KOSIT_URL=http://localhost:8090/ pytest tests/contract/kosit
```

The validator takes up to a minute to load its rules. Without `ARGUS_KOSIT_URL` the contract tests are skipped locally; in CI they must run.

## The audit log

Exceptions record only positions, field names, the scenario and rule codes in their facts, never a value from the invoice (NF-03). The values stay in the exception detail for the person who acts on it.

## What is still to come

- **Hooking it into the sweep** after #91, together with the PDF reader of #29: read the e-invoices first, then the PDFs, pair them, validate each e-invoice, and queue what needs a person. Call the validator outside any `unit_of_work` and queue in a short one afterwards.
- **One case per invoice** with #30, from `EInvoiceWithCopy.documents`.
- **Azure.** The validator container runs next to ARGUS in the Azure setup (#65); it needs no secrets and no internet access once built.

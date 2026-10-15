# Synthetic test fixtures

Test data for ARGUS. Everything here is invented. See the [test strategy](../../docs/testing/test-strategy.md) for how it is used.

## What is here

| Folder | Content |
|---|---|
| `emails/` | 50 test emails as `.eml` files for AC-1, and `manifest.csv` with the expected outcome of each |
| `documents/` | Every invoice and letter on its own, and `expected.json` with the data ARGUS should extract from each invoice (PR-01) |
| `master-data/` | `vendors.csv` (Kreditoren), `customers.csv` (Debitoren), `approvers.csv` (the ARGUS approver table, AP-01, AP-06, PY-13) and `roles.csv` (who approves above EUR 5,000, who gets reminders and exceptions) |
| `tools/` | `generate.py` builds everything above, `check.py` checks it |

## The 50 emails

| Expected category | Route | Count |
|---|---|---|
| `incoming_invoice` (invoices and credit notes from vendors) | `invoice_processing` | 16 |
| `outgoing_invoice` (invoices and credit notes from Max ASP's invoicing agent) | `outgoing_booking` | 8 |
| `payment_reminder` (reminders, dunning letters, collection) | `forward_reminder` | 7 |
| `other` (marketing, newsletters, spam, general mail) | `deletion_folder` | 13 |
| `uncertain` (deliberately ambiguous, IN-09) | `manual_review` | 6 |

`manifest.csv` columns: `email_id`, `file`, `expected_category`, `expected_route`, `invoice_count` (cases ARGUS should create, IN-08), `documents` (IDs in `expected.json`), `flags`, `requirements`, `note`.

Formats covered: PDF invoices, a scanned paper invoice (E07) and a scanned dunning letter (E28) as JPEG, an unreadable scan (E46), XRechnung (E08 incoming, E21 outgoing with a PDF copy), ZUGFeRD (E09) and an invoice written in the email body (E16).

Scenarios worth knowing:

| Email | Scenario | Requirement |
|---|---|---|
| E05 | Gross EUR 8,925.00, needs the second approval | AP-09, AC-3 |
| E06 | IBAN on the invoice differs from the vendor's stored IBAN | PR-05, AC-5 |
| E12 | Vendor not in DATEV | VM-03, AC-5 |
| E13 | Vendor without an approver | VM-02 |
| E14 | Three invoices in one email | IN-08 |
| E15 | Same invoice as E01 sent again | IN-10 |
| E03 | Rent paid by direct debit | PY-13 |
| E04, E11 | GBP invoice and credit note from a UK vendor | BK-04 |
| E17 to E19 | Outgoing: domestic 19 %, EU reverse charge, USA not taxable | AC-6 |
| E22 | Outgoing invoice to a customer not in DATEV | AR-03 |
| E23 | Outgoing invoice MA-2026-0107; MA-2026-0106 never arrives | AR-07 |
| E32 to E44 | "Other" emails that use invoice words, come from known vendors or carry PDFs with prices | IN-04 |

## How the data is kept synthetic

No real names, companies, addresses, IBANs, VAT IDs or email addresses. `check.py` enforces the identifier rules below on every email, every PDF and XML attachment and every master data file.

- **Companies** use invented names with Muster, Beispiel or Example. **People** use German placeholder names (Erika Mustermann, Anna Muster).
- **Max ASP GmbH** appears by name because ARGUS must recognise its own invoices. Its address, VAT ID and IBAN are placeholders.
- **Email addresses** only use the reserved domains `.example` and `.invalid` (RFC 2606). The accounts mailbox is `accounts@maxasp.example`; tests map it to the real mailbox through configuration.
- **IBANs** have a valid check sum but a bank code that no bank has: German IBANs use a bank code starting with `0000`, the UK IBAN uses the bank code `TEST`.
- **VAT IDs** start with `DE0`, `GB000` or `NL000000000B`, which are never issued.
- Every document carries the footer "Synthetic test document for ARGUS. Not a real invoice."

## Changing the fixtures

The generated files are committed, so tests never need to run the generator. To change a fixture, edit `tools/fixture_data.py` or `tools/email_data.py`, then:

```
python3 -m pip install -r tests/fixtures/tools/requirements.txt
python3 tests/fixtures/tools/generate.py
python3 tests/fixtures/tools/check.py
```

The generator is deterministic: the same input produces the same files, so a diff shows exactly what changed. The JPEG scans may differ in bytes between Pillow versions.

## Known limits

- The XRechnung and ZUGFeRD files follow the standards but have not been checked with an official validator. The ZUGFeRD PDF embeds `factur-x.xml` with the right metadata but is not a certified PDF/A-3.
- There is no Wise CSV export yet (AC-7). It follows once the Wise spike (#4) confirms the format.

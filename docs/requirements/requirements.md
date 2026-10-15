# ARGUS business requirements catalog

Source: Business Requirements v1.0 for Max ASP GmbH, prepared by Benjamin Tänzer on 15 Oct 2026. The original Word file is in [source/](source/). This file is the working copy every agent uses. Requirement IDs are stable: reference them in issues, PRs, commits and tests (for example `Covers: IN-04, IN-09`).

If this catalog and the source document ever disagree, the source wins and the PO raises it with Arita.

## 1. Purpose and scope

ARGUS automates Max ASP's bookkeeping in three streams: incoming invoices (accounts payable), outgoing invoices (accounts receivable) and Wise bank transactions. Every stream ends as bookings in DATEV Rechnungswesen, which Max ASP maintains in house. Humans stay in control at two points only: approval of incoming invoices and final payment release in Wise.

**In scope:** incoming supplier invoices and credit notes at accounts@maxasp.com (digital or scanned paper), outgoing invoices and credit notes sent by Max ASP's invoicing agent to that mailbox, payment reminders and dunning letters (routing only), approval workflow, DATEV booking, payment batching and Wise hand over, monthly booking of Wise bank transactions.

**Out of scope:** creating or maintaining vendor and customer master data (manual in DATEV), creating outgoing invoices, expense management (Factorial) and payroll, Oberbank and Volksbank, final payment release in Wise.

## 2. Process overview

1. **Intake:** every email is classified. Payment reminders go to Benjamin, other non invoice emails are removed.
2. **Approval:** invoices are checked and sent to the vendor's approver; above EUR 5,000 Benjamin approves as well.
3. **Booking:** approved invoices are booked in DATEV automatically.
4. **Payment run:** at the same time, approved invoices enter the payment pool and are handed to Wise on the 15th and 30th.
5. **Release:** Benjamin releases the payment batch manually in Wise.

Outgoing invoices and Wise bank transactions are booked without any approval step.

## 3. Intake and classification

Every email arriving at accounts@maxasp.com is classified into exactly one category and routed accordingly.

- **IN-01** The system monitors the mailbox accounts@maxasp.com continuously and processes every incoming email.
- **IN-02** The system accepts digital invoices (PDF, image) and structured e-invoices (XRechnung, ZUGFeRD / Factur-X) as attachment or email body.
- **IN-03** The system accepts scans of paper invoices sent to the mailbox and reads their content.
- **IN-04** Each email is classified as Incoming invoice or credit note, Outgoing invoice or credit note (issued by Max ASP GmbH), Payment reminder, or Other (marketing, newsletters, spam, general correspondence).
- **IN-05** Payment reminders and dunning letters are forwarded to benjamin.taenzer@maxasp.com with the original attachment.
- **IN-06** Emails classified as Other are removed from the inbox.
- **IN-07** Incoming invoices and credit notes go to invoice processing (section 4); outgoing invoices and credit notes go to section 7.
- **IN-08** An email containing several invoices is split into one processing case per invoice.
- **IN-09** When the classification is uncertain, the email goes to a manual review queue instead of being routed automatically.
- **IN-10** Duplicate invoices (same vendor, invoice number and amount) are detected and flagged, not processed twice.

> Note on IN-06 from the source: removed emails should first go to a deletion folder that is purged after 30 days. A misclassified invoice would otherwise be lost, which is also a GoBD retention risk.

## 4a. Invoice processing: data extraction

No incoming invoice is booked or paid before its designated approver has released it.

- **PR-01** The system captures at minimum: vendor name, vendor VAT ID, invoice number, invoice date, service period, net amount, VAT rate and amount, gross amount, currency, payment terms / due date, early-payment discount, vendor IBAN.
- **PR-02** The vendor is matched against the existing vendor account (Kreditor) in DATEV, primarily by VAT ID and IBAN, secondarily by name.
- **PR-03** The system checks formal invoice requirements under §14 UStG (e.g. Max ASP as recipient, sequential invoice number, VAT ID, VAT breakdown) and flags missing items.
- **PR-04** The system proposes a G/L account and tax key, based on the vendor's booking history.
- **PR-05** If the IBAN on the invoice differs from the IBAN stored for the vendor, the invoice is blocked and flagged as a potential fraud case.

## 4b. Invoice processing: approval

- **AP-01** Each vendor has exactly one designated approver, maintained manually in a separate ARGUS approver table.
- **AP-02** After successful extraction, the invoice is sent to its vendor's approver with the invoice image and the extracted key data.
- **AP-03** The approver can approve, reject (with mandatory comment) or return the invoice with a query.
- **AP-04** Approvers can decide without access to DATEV or Wise.
- **AP-05** If the approver has not responded after 3 working days, a reminder is sent; after 5 working days the case escalates to benjamin.taenzer@maxasp.com.
- **AP-06** A deputy approver can be defined for absences.
- **AP-07** Rejected invoices are not booked or paid; the case is closed with the reason documented.
- **AP-08** Every approval decision is logged with approver, timestamp and comment.
- **AP-09** Invoices above EUR 5,000 gross need a second approval by Benjamin after the vendor's approver has approved.
- **AP-10** Recurring invoices (e.g. rent, subscriptions) go through the normal approval; there is no auto-approval.

## 5. Booking in DATEV Rechnungswesen

Approval triggers the booking automatically; no one keys an incoming invoice into DATEV by hand.

- **BK-01** Immediately after approval, the invoice is booked in DATEV Rechnungswesen against the matched vendor account (Kreditor).
- **BK-02** The booking contains: vendor account, G/L account, tax key, invoice date, invoice number, net / VAT / gross amounts, due date.
- **BK-03** The invoice image (and the XML for e-invoices) is attached to the booking as the digital voucher (Belegbild).
- **BK-04** Foreign-currency invoices are booked in EUR, converted at the latest exchange rate available from Wise; original currency and amount are kept as reference.
- **BK-05** Credit notes are booked as such and offset against open items of the same vendor in the next payment run.
- **BK-06** If the booking fails (e.g. closed period, unknown account), the case goes to the exception queue and Benjamin is notified.
- **BK-07** The DATEV booking reference is stored on the case for reconciliation with the payment.

## 6. Payment runs and Wise hand over

Approved invoices are collected and handed to Wise as a batch on the 15th and 30th of each month; the actual payment is still released manually in Wise.

- **PY-01** At the same time as booking, every approved invoice is added to the open payment pool.
- **PY-02** Two payment runs per month: hand-over to Wise on the 15th (mid-month run) and the 30th (month-end run; in February the last day of the month).
- **PY-03** If the 15th or 30th is a weekend or public holiday (Baden-Württemberg), the hand-over moves to the previous working day.
- **PY-04** A run includes every approved invoice due on or before the date of the following run. Invoices due later stay in the pool.
- **PY-05** Invoices with an early-payment discount are included in the last run before the discount deadline, if that is earlier than PY-04.
- **PY-06** Overdue invoices and invoices with a payment reminder are always included in the next run.
- **PY-07** Payments to the same vendor are combined into one transfer, net of open credit notes, with all invoice numbers in the payment reference.
- **PY-08** Payments are created in Wise via API as a batch in the correct currency, but not funded or released. Release remains a manual step in Wise.
- **PY-09** Before hand-over, Benjamin receives a payment run summary (vendors, invoices, amounts per currency, total).
- **PY-10** Individual invoices can be held back from a run (payment block) or added to a run manually.
- **PY-11** After execution in Wise, the payment status is updated and the case is marked as paid.
- **PY-12** Executed Wise payments are cleared against the vendor's open items in DATEV through the monthly Wise bank import (section 8).
- **PY-13** Invoices paid by direct debit (SEPA Lastschrift) are approved and booked, but never added to a payment run. Direct debit is marked per vendor in the ARGUS approver table; the debit is cleared by the bank import.

## 7. Outgoing invoices

Outgoing invoices from Max ASP's invoicing agent are sent to accounts@maxasp.com and booked in DATEV automatically, without an approval step.

- **AR-01** The invoicing agent sends every issued invoice and credit note to accounts@maxasp.com (PDF, plus XML for e-invoices).
- **AR-02** The system recognises the document as outgoing (issuer Max ASP GmbH) and never routes it into the approval or payment process.
- **AR-03** The customer is matched against the existing customer account (Debitor) in DATEV; an unknown customer goes to the exception queue and Benjamin is notified to create the account manually. Processing resumes once the account exists.
- **AR-04** The invoice is booked immediately against customer account, revenue account and tax key, including reverse charge or non-taxable treatment for foreign customers.
- **AR-05** The booking carries invoice number, invoice date, service period, due date and net / VAT / gross amounts in EUR; foreign-currency invoices are converted at the latest exchange rate available from Wise.
- **AR-06** The invoice PDF (and XML) is attached to the booking as the digital voucher.
- **AR-07** Invoice numbers are checked for gaps and duplicates in the numbering sequence.
- **AR-08** Credit notes and cancellations are booked as such and linked to the original invoice.
- **AR-09** Open receivables are cleared when the customer payment arrives in the monthly Wise bank import (section 8).

## 8. Bank transactions

Wise carries most bank transactions; its monthly CSV export is turned into DATEV bookings automatically. Volksbank is already imported into DATEV and stays unchanged. Oberbank closes in Q1 2027 and is out of scope.

- **BA-01** Once a month, the Wise statement of the previous month is exported as CSV automatically, for all Wise balances and currencies. The import, including all resolved exceptions, is completed by the 5th of the following month.
- **BA-02** Every transaction is turned into a booking and lands in DATEV Rechnungswesen without manual steps.
- **BA-03** Outgoing payments are matched to vendor open items (via ARGUS payment run and payment reference) and cleared.
- **BA-04** Incoming payments are matched to customer open items by invoice number in the reference and amount, and cleared. Partial payments leave the remainder open.
- **BA-05** Transactions without an invoice (bank fees, conversions between own Wise balances, transfers between own accounts, taxes, payroll) are booked by rules to defined accounts; transfers between own accounts use a cash transit account.
- **BA-06** Transactions that cannot be matched or assigned by a rule are emailed to benjamin.taenzer@maxasp.com with date, counterparty, amount and reference. Benjamin replies with the account to book to; the system then books the transaction and can save the answer as a rule for the same counterparty.
- **BA-07** Each Wise transaction is imported only once, so a repeated import creates no duplicate bookings.
- **BA-08** Opening and closing balance per currency are reconciled with the Wise statement; a difference blocks the import and notifies Benjamin.

## 9a. Vendor and customer master data

- **VM-01** New vendors (Kreditoren) and customers (Debitoren) are created manually in DATEV once a contract or order has been signed. ARGUS never creates vendor or customer accounts itself.
- **VM-02** ARGUS uses the vendor and customer accounts from DATEV. The approver per vendor is kept in the ARGUS approver table; an invoice from a vendor without an approver goes to the exception queue.
- **VM-03** An invoice from an unknown vendor is not processed further; it goes to the exception queue and Benjamin is notified to create the vendor. Processing resumes automatically once the vendor exists.
- **VM-04** Changes to a vendor's bank details are only accepted from DATEV, never from an invoice.

## 9b. Exception handling

- **EX-01** One central exception queue holds every case that cannot be processed automatically, with the reason.
- **EX-02** Exception reasons include at least: uncertain classification, unreadable document, unknown vendor or customer, vendor without approver, duplicate, IBAN mismatch, failed §14 UStG check, booking error, unmatched bank transaction, bank balance difference.
- **EX-03** Benjamin receives a daily digest of open exceptions.

## 9c. Compliance and non functional

- **NF-01** GoBD compliance: original documents are stored unaltered and retained for the legal period; every step is logged and traceable.
- **NF-02** Receipt of structured e-invoices (XRechnung, ZUGFeRD) is supported, as required in Germany since 1 January 2025.
- **NF-03** GDPR: data is processed and stored in the EU; access is role-based.
- **NF-04** Segregation of duties: the system can prepare payments but never release them.
- **NF-05** The status of every invoice is visible at any time: received, awaiting approval, approved, booked, in payment run, paid, exception.
- **NF-06** Access to Microsoft 365, DATEV and Wise uses only interfaces supported by the vendors; credentials are stored securely.
- **NF-07** Target: at least 90% of invoices from known vendors are processed without manual intervention apart from approval.

## 10. Assumptions

- Scope is Max ASP GmbH, Max ASP's only legal entity.
- accounts@maxasp.com is a Microsoft 365 mailbox.
- Max ASP books in DATEV Rechnungswesen itself.
- Wise Business is the main bank account and the only account for supplier payments.

## 11. Acceptance criteria

- **AC-1** At least 95% of 50 test emails (invoices, reminders, marketing) are classified and routed correctly.
- **AC-2** An approved test invoice appears in DATEV with correct account, tax key and voucher image.
- **AC-3** An invoice above EUR 5,000 is only booked after the second approval by Benjamin.
- **AC-4** A test run on the 15th creates the correct batch in Wise, without releasing it.
- **AC-5** An invoice from an unknown vendor and one with a changed IBAN both land in the exception queue.
- **AC-6** An outgoing test invoice from the invoicing agent is booked to the correct customer account with the correct tax treatment.
- **AC-7** A monthly Wise CSV is imported, balances reconcile per currency, and paid incoming and outgoing invoices are cleared.
- **AC-8** Every test case can be traced from receipt to payment in the audit log.

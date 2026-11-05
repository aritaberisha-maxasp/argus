"""Send an email to manual review when its invoice cannot be trusted (#28, #32, EX-01, PR-03).

Three checks can fail, all leading to the exception reason ``invoice_check_failed`` on the email:

- the PDF copy disagrees with its XML on the invoice number, the total or the IBAN; the detail
  shows both values, so the person sees what differs;
- the KoSIT validator did not accept the XML; the detail names the rules it broke;
- an invoice misses an item §14 UStG asks for (``argus.reading.formal_check``); the detail names
  the missing items. This applies to PDFs, scans and e-invoices alike, so an email without any
  e-invoice passes its §14 results here too, with no invoices and no validations.

An email has at most one open exception per reason (``exception_open_once``, #19), so every
failed check of every e-invoice on the email goes into one exception, the way
``argus.reading.queue_unreadable`` names every unreadable document in one. An e-invoice ARGUS
cannot read at all is an unreadable document and goes through ``queue_unreadable``.

Like the other queue steps, this runs inside a ``unit_of_work`` after the reading, never around
the validator call (PR #79 review). ``detail`` stays on the exception for the person who acts on
it; ``facts`` go into the audit log and hold only positions, field names, item names and rule
codes (NF-03).
IBANs are masked in the detail like everywhere else in the queue (PR #86 review).
"""

from collections.abc import Mapping, Sequence

from sqlalchemy import Connection, text

from argus.cases.model import ExceptionReason
from argus.cases.store import open_exception
from argus.einvoice.pairing import Disagreement, EInvoiceWithCopy
from argus.einvoice.validator import Validation
from argus.reading.formal_check import FormalCheck
from argus.sorting.models import mask_ibans

PDF_MATCHES_XML = "pdf_matches_xml"
KOSIT_VALIDATION = "kosit_validation"
FORMAL_CHECK = "ustg14"

LABELS = {"invoice_number": "invoice number", "gross_amount": "total", "iban_on_document": "IBAN"}
# Enough for a person to see what is wrong; the full report is one click away in the validator.
MAX_RULES_SHOWN = 10


def queue_failed_checks(
    connection: Connection,
    email_id: int,
    invoices: Sequence[EInvoiceWithCopy],
    validations: Mapping[str, Validation],
    formal: Sequence[FormalCheck] = (),
) -> int | None:
    """One exception for every invoice of the email whose PDF copy disagrees with its XML, that
    the validator did not accept, or that failed the §14 UStG check. ``validations`` holds the
    validator's answer for each e-invoice, by its ``document``. ``formal`` holds the §14 UStG
    results of the email's invoices: check an e-invoice from its XML, not its PDF copy. Returns
    the exception ID, or None when every check passed. Safe to repeat: an email has at most one
    open exception for this."""
    missing = [i.einvoice.document for i in invoices if i.einvoice.document not in validations]
    if missing:
        # An e-invoice nobody checked must never look like one that passed.
        raise ValueError(f"no validation for {', '.join(missing)}")

    paragraphs: list[str] = []
    checks: set[str] = set()
    documents: list[str] = []
    copies: list[str] = []
    fields: set[str] = set()
    scenarios: set[str] = set()
    rules: set[str] = set()
    for invoice in invoices:
        document = invoice.einvoice.document
        validation = validations[document]
        failed = False
        if invoice.disagreements and invoice.pdf is not None:
            paragraphs.append(_disagreement_text(document, invoice.pdf.document, invoice))
            checks.add(PDF_MATCHES_XML)
            copies.append(invoice.pdf.document)
            fields.update(d.field for d in invoice.disagreements)
            failed = True
        if not validation.accepted:
            paragraphs.append(_rejection_text(document, validation))
            checks.add(KOSIT_VALIDATION)
            if validation.scenario is not None:
                scenarios.add(validation.scenario)
            rules.update(e.code for e in validation.errors if e.code)
            failed = True
        if failed:
            documents.append(document)
    missing_items: set[str] = set()
    formal_rules: set[str] = set()
    for check in formal:
        if check.passed:
            continue
        paragraphs.append(mask_ibans(check.paragraph()))
        checks.add(FORMAL_CHECK)
        missing_items.update(check.items)
        formal_rules.add(check.rule)
        if check.document not in documents:
            documents.append(check.document)
    if not paragraphs:
        return None

    existing = connection.execute(
        text(
            "select id from exception_item where email_id = :email_id"
            " and reason = :reason and resolved_at is null"
        ),
        {"email_id": email_id, "reason": ExceptionReason.INVOICE_CHECK_FAILED.value},
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    return open_exception(
        connection,
        ExceptionReason.INVOICE_CHECK_FAILED,
        " ".join(paragraphs),
        email_id=email_id,
        facts={
            "checks": sorted(checks),
            "documents": documents,
            "copies": copies,
            "fields": sorted(fields),
            "scenarios": sorted(scenarios),
            "rules": sorted(rules),
            "missing_items": sorted(missing_items),
            # Which list applied (ustg14 or ustdv33), so the trail shows why an item was asked.
            "formal_rules": sorted(formal_rules),
        },
    )


def _disagreement_text(document: str, copy: str, invoice: EInvoiceWithCopy) -> str:
    differences = "; ".join(_difference(d) for d in invoice.disagreements)
    return (
        f"The PDF copy ({copy}) and the e-invoice ({document}) disagree. {differences}. ARGUS "
        "books from the XML, but the person approving sees the PDF, so check with the vendor "
        "which one is right."
    )


def _difference(d: Disagreement) -> str:
    if d.field != "iban_on_document":
        return f"{LABELS[d.field]}: {d.xml_value} in the XML, {d.pdf_value} on the PDF"
    xml_iban = mask_ibans(d.xml_value)
    if len(d.pdf_values) == 1:
        line = f"IBAN: {xml_iban} in the XML, {mask_ibans(d.pdf_value)} on the PDF"
        if mask_ibans(d.pdf_value) == xml_iban:
            line += " (they differ in the hidden digits)"
        return line
    wanted = _compact(d.xml_value)
    shown = []
    for iban in d.pdf_values:
        masked = mask_ibans(iban)
        if _compact(iban) == wanted:
            masked += " (the one in the XML)"
        elif masked == xml_iban:
            masked += " (differs from the XML only in the hidden digits)"
        shown.append(masked)
    return f"IBAN: {xml_iban} in the XML, but the PDF states {len(shown)} IBANs: " + ", ".join(
        shown
    )


def _compact(iban: str) -> str:
    return "".join(iban.split()).upper()


def _rejection_text(document: str, validation: Validation) -> str:
    errors = validation.errors
    if validation.scenario is None:
        why = (
            "The official validator has no rules for this kind of e-invoice. It is neither an "
            "XRechnung nor an EN 16931 invoice, for example a ZUGFeRD MINIMUM or BASIC WL file, "
            "which is not a valid invoice in Germany."
        )
    elif errors:
        shown = "; ".join(
            f"{e.code}: {e.text}" if e.code else e.text for e in errors[:MAX_RULES_SHOWN]
        )
        if len(errors) > MAX_RULES_SHOWN:
            shown += f"; and {len(errors) - MAX_RULES_SHOWN} more"
        why = f"The official validator ({validation.scenario}) found these errors: {shown}."
    else:
        why = f"The official validator ({validation.scenario}) did not accept it."
    return mask_ibans(
        f"The e-invoice ({document}) failed the official check. {why} The vendor may need to "
        "send a corrected invoice."
    )

"""Send an email to manual review when its e-invoice cannot be trusted (#28, EX-01, ADR 0006).

Two reasons, both with the exception reason ``invoice_check_failed`` on the email:

- the PDF copy disagrees with its XML on the invoice number, the total or the IBAN; the detail
  shows both values, so the person sees what differs;
- the KoSIT validator did not accept the XML; the detail names the rules it broke.

An e-invoice ARGUS cannot read at all is an unreadable document and goes through
``argus.reading.queue_unreadable`` with the other unreadable files of the email.

Like the other queue steps, these run inside a ``unit_of_work`` after the reading, never around
the validator call (PR #79 review). ``detail`` stays on the exception for the person who acts on
it; ``facts`` go into the audit log and hold only positions, field names and rule codes (NF-03).
IBANs are masked in the detail like everywhere else in the queue (PR #86 review).
"""

from collections.abc import Sequence

from sqlalchemy import Connection, text

from argus.cases.model import ExceptionReason
from argus.cases.store import open_exception
from argus.einvoice.pairing import EInvoiceWithCopy
from argus.einvoice.parser import EInvoice
from argus.einvoice.validator import Validation
from argus.sorting.models import mask_ibans

PDF_MATCHES_XML = "pdf_matches_xml"
KOSIT_VALIDATION = "kosit_validation"

LABELS = {"invoice_number": "invoice number", "gross_amount": "total", "iban_on_document": "IBAN"}
# Enough for a person to see what is wrong; the full report is one click away in the validator.
MAX_RULES_SHOWN = 10


def queue_disagreements(
    connection: Connection, email_id: int, invoices: Sequence[EInvoiceWithCopy]
) -> list[int]:
    """One exception per e-invoice whose PDF copy disagrees with it. Returns the exception IDs.
    Safe to repeat: an open exception for the same e-invoice is not opened twice."""
    opened = []
    for invoice in invoices:
        if not invoice.disagreements or invoice.pdf is None:
            continue
        xml_document = invoice.einvoice.document
        existing = _open_exception(connection, email_id, PDF_MATCHES_XML, xml_document)
        if existing is not None:
            opened.append(existing)
            continue
        differences = []
        for d in invoice.disagreements:
            xml_value, pdf_value = _shown(d.field, d.xml_value), _shown(d.field, d.pdf_value)
            line = f"{LABELS[d.field]}: {xml_value} in the XML, {pdf_value} on the PDF"
            if xml_value == pdf_value:
                line += " (they differ in the hidden digits)"
            differences.append(line)
        detail = (
            f"The PDF copy ({invoice.pdf.document}) and the e-invoice ({xml_document}) disagree. "
            + "; ".join(differences)
            + ". ARGUS books from the XML, but the person approving sees the PDF, so check "
            "with the vendor which one is right."
        )
        opened.append(
            open_exception(
                connection,
                ExceptionReason.INVOICE_CHECK_FAILED,
                detail,
                email_id=email_id,
                facts={
                    "check": PDF_MATCHES_XML,
                    "document": xml_document,
                    "copy": invoice.pdf.document,
                    "fields": [d.field for d in invoice.disagreements],
                },
            )
        )
    return opened


def queue_rejected(
    connection: Connection, email_id: int, einvoice: EInvoice, validation: Validation
) -> int | None:
    """An exception when the validator did not accept the e-invoice. Returns its ID, or None
    when the validator accepted it. Safe to repeat."""
    if validation.accepted:
        return None
    existing = _open_exception(connection, email_id, KOSIT_VALIDATION, einvoice.document)
    if existing is not None:
        return existing
    errors = validation.errors
    if validation.scenario is None:
        why = (
            "The official validator has no rules for this kind of e-invoice. It is neither an "
            "XRechnung nor an EN 16931 invoice, for example a ZUGFeRD MINIMUM or BASIC WL file, "
            "which is not a valid invoice in Germany."
        )
    elif errors:
        shown = "; ".join(f"{e.code}: {e.text}" if e.code else e.text for e in errors)
        if len(errors) > MAX_RULES_SHOWN:
            shown = "; ".join(
                f"{e.code}: {e.text}" if e.code else e.text for e in errors[:MAX_RULES_SHOWN]
            )
            shown += f"; and {len(errors) - MAX_RULES_SHOWN} more"
        why = f"The official validator ({validation.scenario}) found these errors: {shown}."
    else:
        why = f"The official validator ({validation.scenario}) did not accept it."
    detail = mask_ibans(
        f"The e-invoice ({einvoice.document}) failed the official check. {why} The vendor "
        "may need to send a corrected invoice."
    )
    return open_exception(
        connection,
        ExceptionReason.INVOICE_CHECK_FAILED,
        detail,
        email_id=email_id,
        facts={
            "check": KOSIT_VALIDATION,
            "document": einvoice.document,
            "scenario": validation.scenario,
            "rules": sorted({e.code for e in errors if e.code}),
        },
    )


def _shown(field: str, value: str) -> str:
    return mask_ibans(value) if field == "iban_on_document" else value


def _open_exception(connection: Connection, email_id: int, check: str, document: str) -> int | None:
    found = connection.execute(
        text(
            "select id from exception_item where email_id = :email_id and reason = :reason"
            " and resolved_at is null and facts->>'check' = :check"
            " and facts->>'document' = :document"
        ),
        {
            "email_id": email_id,
            "reason": ExceptionReason.INVOICE_CHECK_FAILED.value,
            "check": check,
            "document": document,
        },
    ).scalar_one_or_none()
    return int(found) if found is not None else None

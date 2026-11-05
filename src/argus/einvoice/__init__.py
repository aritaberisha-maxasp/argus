"""Reading XRechnung and ZUGFeRD e-invoices (ADR 0006 point 1, #28, IN-02, NF-02, PR-01)."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from argus.einvoice.embedded import embedded_xml
from argus.einvoice.formal import check_einvoice
from argus.einvoice.pairing import Disagreement, EInvoiceWithCopy, compare, pair
from argus.einvoice.parser import (
    READER,
    EInvoice,
    EInvoiceRefused,
    looks_like_xml,
    read_xml,
    refused_reading,
)
from argus.einvoice.queue import queue_failed_checks
from argus.einvoice.safe_xml import XmlRefused
from argus.einvoice.validator import (
    KositValidator,
    Validation,
    ValidationMessage,
    Validator,
    ValidatorUnavailable,
)
from argus.reading.fields import InvoiceReading


@dataclass(frozen=True)
class EInvoices:
    """The e-invoices of one email. ``refused`` holds the XML ARGUS cannot read safely, as
    unreadable documents for ``argus.reading.queue_unreadable`` (EX-01, EX-02)."""

    einvoices: tuple[EInvoice, ...] = ()
    refused: tuple[InvoiceReading, ...] = field(default_factory=tuple)


def read_einvoices(attachments: Sequence[bytes]) -> EInvoices:
    """Find and read every e-invoice among the attachments of one email, numbered from 1 in the
    order of the email: XML files, and XML embedded in a PDF (ZUGFeRD, Factur-X). Other files
    are left to the PDF and image readers (#29). Never raises for a bad e-invoice: it is
    returned in ``refused`` instead, so one bad file cannot stop the run."""
    found: list[EInvoice] = []
    refused: list[InvoiceReading] = []
    for position, data in enumerate(attachments, start=1):
        document = f"attachment {position}"
        container = None
        if data.startswith(b"%PDF-"):
            xml = embedded_xml(data)
            if xml is None:
                continue
            container, document = document, f"{document}, embedded XML"
        elif looks_like_xml(data):
            xml = data
        else:
            continue
        try:
            einvoice = read_xml(document, xml, container)
        except EInvoiceRefused as error:
            refused.append(refused_reading(document, str(error)))
            continue
        if einvoice is not None:
            found.append(einvoice)
    return EInvoices(tuple(found), tuple(refused))


__all__ = [
    "READER",
    "Disagreement",
    "EInvoice",
    "EInvoiceRefused",
    "EInvoiceWithCopy",
    "EInvoices",
    "KositValidator",
    "Validation",
    "ValidationMessage",
    "Validator",
    "ValidatorUnavailable",
    "XmlRefused",
    "check_einvoice",
    "compare",
    "embedded_xml",
    "looks_like_xml",
    "pair",
    "queue_failed_checks",
    "read_einvoices",
    "read_xml",
    "refused_reading",
]

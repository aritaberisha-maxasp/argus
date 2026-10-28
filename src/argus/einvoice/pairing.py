"""Put each e-invoice together with its PDF copy, and find where the two disagree (#28, IN-02,
NF-02, ADR 0006 point 1).

An e-invoice often comes with a PDF a person can read: as a separate attachment (XRechnung plus
PDF) or as the PDF the XML is embedded in (ZUGFeRD, Factur-X). Both are one invoice, so they
become one case. The XML wins for every value ARGUS books. The person who approves sees the
PDF, though, so when the PDF says another invoice number, total or IBAN than the XML, the email
goes to a person: a PDF copy that differs from its XML is a known fraud pattern.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from argus.einvoice.parser import EInvoice
from argus.reading.fields import InvoiceReading
from argus.reading.text_rules import INVOICE_KINDS

# The values a PDF copy must state the same way as its XML (ADR 0006 point 1).
COMPARED = ("invoice_number", "gross_amount", "iban_on_document")


@dataclass(frozen=True)
class Disagreement:
    """One value the PDF copy states differently from the XML. Values in the formats of the
    readings: ``"147.56"``, the IBAN without spaces."""

    field: str
    xml_value: str
    pdf_value: str


@dataclass(frozen=True)
class EInvoiceWithCopy:
    """One invoice backed by an e-invoice, with the PDF copy that belongs to it, if any.

    #30 turns this into one case that links every document in ``documents``. The fields come
    from ``einvoice.reading``; the PDF reading only serves the comparison.
    """

    einvoice: EInvoice
    pdf: InvoiceReading | None
    disagreements: tuple[Disagreement, ...]

    @property
    def documents(self) -> tuple[str, ...]:
        """The attachments behind this invoice, by position. A ZUGFeRD PDF is one attachment."""
        names = [self.einvoice.container or self.einvoice.document]
        if self.pdf is not None and self.pdf.document not in names:
            names.append(self.pdf.document)
        return tuple(names)


def pair(
    einvoices: Sequence[EInvoice], pdf_readings: Sequence[InvoiceReading]
) -> tuple[list[EInvoiceWithCopy], list[InvoiceReading]]:
    """Give every e-invoice its PDF copy. Returns the e-invoices with their copies and the PDF
    readings that belong to no e-invoice, which stay invoices of their own.

    1. A ZUGFeRD XML belongs to the PDF it is embedded in.
    2. Otherwise a PDF is the copy when it states the same invoice number and no other issuer.
       Two vendors that both use number 1001 stay two invoices (ADR 0006).
    3. If exactly one e-invoice and exactly one PDF invoice from the same issuer are left, the
       PDF is taken as the copy even though the numbers differ, so the difference reaches a
       person instead of becoming a second case.
    """
    left = [r for r in pdf_readings if r.unreadable is None and r.kind in INVOICE_KINDS]
    other = [r for r in pdf_readings if r not in left]
    copies: dict[int, InvoiceReading] = {}

    for index, einvoice in enumerate(einvoices):
        if einvoice.container is None:
            continue
        found = next((r for r in left if r.document == einvoice.container), None)
        if found is not None:
            copies[index] = found
            left.remove(found)
        # A ZUGFeRD PDF that was unreadable as a PDF is still the container of its XML.
        other = [r for r in other if r.document != einvoice.container]

    for index, einvoice in enumerate(einvoices):
        if index in copies:
            continue
        found = next(
            (
                r
                for r in left
                if _same_number(einvoice.reading, r) and _issuer(einvoice.reading, r) is not False
            ),
            None,
        )
        if found is not None:
            copies[index] = found
            left.remove(found)

    alone = [i for i in range(len(einvoices)) if i not in copies]
    if len(alone) == 1 and len(left) == 1 and _issuer(einvoices[alone[0]].reading, left[0]):
        copies[alone[0]] = left.pop()

    paired = [
        EInvoiceWithCopy(e, copies.get(i), compare(e.reading, copies[i]) if i in copies else ())
        for i, e in enumerate(einvoices)
    ]
    return paired, left + other


def compare(xml: InvoiceReading, pdf: InvoiceReading) -> tuple[Disagreement, ...]:
    """The values the PDF states differently from the XML. A value the PDF reader could not
    read is not a disagreement: there is nothing on the PDF to contradict the XML."""
    found = []
    for name in COMPARED:
        xml_value, pdf_value = xml.value(name), pdf.value(name)
        if xml_value is None or pdf_value is None:
            continue
        if _normalise(name, xml_value) != _normalise(name, pdf_value):
            found.append(Disagreement(name, str(xml_value), str(pdf_value)))
    return tuple(found)


def _normalise(name: str, value: object) -> object:
    text = str(value)
    if name == "gross_amount":
        try:
            return Decimal(text)
        except InvalidOperation:
            return text
    return re.sub(r"\s", "", text).casefold()


def _same_number(xml: InvoiceReading, pdf: InvoiceReading) -> bool:
    number = pdf.value("invoice_number")
    return number is not None and _normalise("invoice_number", number) == _normalise(
        "invoice_number", xml.value("invoice_number")
    )


def _issuer(xml: InvoiceReading, pdf: InvoiceReading) -> bool | None:
    """Whether both name the same issuer: True, False, or None when the PDF names none ARGUS
    could read. The VAT ID decides when both state one. Otherwise the names must match, ignoring
    case, spaces and punctuation."""
    xml_vat, pdf_vat = xml.value("vendor_vat_id"), pdf.value("vendor_vat_id")
    if xml_vat and pdf_vat:
        return bool(_key(xml_vat) == _key(pdf_vat))
    xml_name, pdf_name = xml.value("issuer_name"), pdf.value("issuer_name")
    if not xml_name or not pdf_name:
        return None
    return bool(_key(xml_name) == _key(pdf_name))


def _key(value: str) -> str:
    return re.sub(r"[\W_]", "", value).casefold()

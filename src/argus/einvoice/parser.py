"""Read XRechnung (UBL and CII) and ZUGFeRD or Factur-X (CII) into the PR-01 fields (#28, IN-02,
NF-02, PR-01).

The XML is the legal original for the values ARGUS books (ADR 0006 point 1), so every field
comes from the XML alone: no OCR, no model, no guessing. A value the XML states in a way ARGUS
cannot read safely (an amount that is not a number, a date in an unknown format, a document
type that is not an invoice) refuses the whole e-invoice, and the email goes to a person.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree.ElementTree import Element

from argus.einvoice.safe_xml import XmlRefused, parse
from argus.reading.fields import (
    FIELDS,
    OPTIONAL_FIELDS,
    OPTIONAL_ON_CREDIT_NOTES,
    Flag,
    InvoiceReading,
    ReadField,
    Source,
)
from argus.reading.text_rules import iban_valid

READER = "einvoice_xml"

UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CII = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
# ZUGFeRD 1 used its own CII version. It is out of use since 2020 and ARGUS does not read it.
ZUGFERD_1 = "urn:ferd:CrossIndustryDocument:invoice:1p0"

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "rsm": CII,
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# UNTDID 1001 document type codes that EN 16931 and XRechnung allow.
INVOICE_CODES = frozenset({"326", "380", "383", "384", "386", "389", "751", "875", "876", "877"})
CREDIT_NOTE_CODES = frozenset({"261", "381", "396", "532"})
# UNTDID 4461: SEPA direct debit and direct debit. The money is collected, not paid by Max ASP.
DIRECT_DEBIT_CODES = frozenset({"49", "59"})


class EInvoiceRefused(ValueError):
    """The e-invoice cannot be read safely. The message is the reason a person sees."""


@dataclass(frozen=True)
class EInvoice:
    """One e-invoice, read from its XML.

    ``reading`` holds the PR-01 fields with the source ``einvoice_xml``, in the same shape as a
    PDF reading (#29), so the later steps treat both alike. ``xml`` is the XML exactly as it was
    received, for the validator; the stored original stays untouched in the document store.
    """

    document: str
    syntax: str  # "ubl" or "cii"
    specification: str | None  # CustomizationID or guideline ID, for example XRechnung 3.0
    type_code: str
    reading: InvoiceReading
    direct_debit: bool
    refers_to: str | None  # the invoice a credit note or a corrected invoice refers to
    xml: bytes
    container: str | None = None  # the PDF the XML is embedded in (ZUGFeRD, Factur-X)

    @property
    def invoice_number(self) -> str:
        number: str = self.reading.value("invoice_number")
        return number


def looks_like_xml(data: bytes) -> bool:
    """True when ``data`` starts like an XML document. Used to pick the attachments to read; the
    file name and content type of an attachment are chosen by the sender and not trusted."""
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def read_xml(document: str, data: bytes, container: str | None = None) -> EInvoice | None:
    """Read one XML document. Returns None when it is well formed XML but not an invoice or
    credit note in UBL or CII, so other XML files are left alone. Raises
    :class:`EInvoiceRefused` when the XML cannot be read safely."""
    try:
        root = parse(data)
    except XmlRefused as error:
        raise EInvoiceRefused(str(error)) from error
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace == ZUGFERD_1:
        raise EInvoiceRefused("ZUGFeRD 1 is no longer in use and ARGUS does not read it")
    if namespace in (UBL_INVOICE, UBL_CREDIT_NOTE):
        fields, extra = _ubl(root, credit_note=namespace == UBL_CREDIT_NOTE)
        syntax = "ubl"
    elif namespace == CII and root.tag.endswith("}CrossIndustryInvoice"):
        fields, extra = _cii(root)
        syntax = "cii"
    else:
        return None
    type_code = extra["type_code"]
    if type_code in INVOICE_CODES:
        kind = "invoice"
    elif type_code in CREDIT_NOTE_CODES:
        kind = "credit_note"
    else:
        raise EInvoiceRefused(f"the document type code {type_code!r} is not an invoice type")
    for required in ("invoice_number", "issuer_name", "gross_amount", "currency"):
        found = fields.get(required)
        if found is None or found.value is None:
            raise EInvoiceRefused(f"the e-invoice has no {required.replace('_', ' ')}")
    optional = OPTIONAL_FIELDS | (OPTIONAL_ON_CREDIT_NOTES if kind == "credit_note" else set())
    for name in FIELDS:
        if name not in fields and name not in optional:
            fields[name] = ReadField(name, flag=Flag.NOT_FOUND, note="not in the e-invoice")
    reading = InvoiceReading(
        document=document,
        kind=kind,
        reader=READER,
        pages=0,
        text="",
        fields={name: fields[name] for name in FIELDS if name in fields},
    )
    return EInvoice(
        document=document,
        syntax=syntax,
        specification=extra["specification"],
        type_code=type_code,
        reading=reading,
        direct_debit=extra["direct_debit"],
        refers_to=extra["refers_to"],
        xml=data,
        container=container,
    )


def refused_reading(document: str, reason: str) -> InvoiceReading:
    """A refused e-invoice as an unreadable document, so it joins the unreadable PDFs and scans
    of the same email in one exception with the reason unreadable document (EX-01)."""
    return InvoiceReading(
        document=document,
        kind="unknown",
        reader=READER,
        pages=0,
        text="",
        unreadable=f"the e-invoice cannot be read safely: {reason}",
    )


# Reading single values


def _all(node: Element, path: str) -> list[Element]:
    return node.findall(path, NS)


def _text(node: Element, path: str) -> str | None:
    found = node.find(path, NS)
    if found is None:
        return None
    value = "".join(found.itertext()).strip()
    return value or None


def _texts(node: Element, path: str) -> list[str]:
    values = ("".join(found.itertext()).strip() for found in _all(node, path))
    return [value for value in values if value]


def _money(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise EInvoiceRefused(f"the amount {value!r} is not a number") from error
    if not amount.is_finite():
        raise EInvoiceRefused(f"the amount {value!r} is not a number")
    cents = amount.quantize(Decimal("0.01"))
    if cents != amount:
        # EN 16931 allows two decimals. Rounding would book a value the XML does not state.
        raise EInvoiceRefused(f"the amount {value!r} has more than two decimals")
    return str(cents)


def _rate(value: str) -> str:
    try:
        rate = Decimal(value)
    except InvalidOperation as error:
        raise EInvoiceRefused(f"the VAT rate {value!r} is not a number") from error
    if not rate.is_finite() or not 0 <= rate <= 100:
        raise EInvoiceRefused(f"the VAT rate {value!r} is not a percentage")
    return format(rate.normalize(), "f") if rate else "0"


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise EInvoiceRefused(f"the date {value!r} is not a date") from error


def _cii_date(node: Element | None) -> str | None:
    if node is None:
        return None
    value = "".join(node.itertext()).strip()
    if not value:
        return None
    if node.get("format", "102") != "102" or len(value) != 8 or not value.isdigit():
        raise EInvoiceRefused(f"the date {value!r} is not in the format CCYYMMDD")
    return _iso_date(f"{value[0:4]}-{value[4:6]}-{value[6:8]}")


def _in_currency(nodes: Iterable[Element], currency: str | None) -> list[str]:
    """Amounts stated in the invoice currency. An invoice in another currency may state the VAT
    a second time in euros (BT-111); that one is not the invoice's VAT amount."""
    values = []
    for node in nodes:
        stated = node.get("currencyID")
        if stated is None or stated == currency:
            value = "".join(node.itertext()).strip()
            if value:
                values.append(value)
    return values


class _Fields:
    """Collects the fields of one e-invoice."""

    def __init__(self) -> None:
        self.fields: dict[str, ReadField] = {}

    def set(self, name: str, value: Any) -> None:
        if value is not None:
            self.fields[name] = ReadField(name, value, Source.EINVOICE_XML)

    def one_of(self, name: str, values: list[str], convert: Callable[[str], str]) -> None:
        """A field the XML may state several times, such as the VAT rate or the IBAN. Several
        different values leave it empty and flagged, like a PDF reading does (#29)."""
        distinct = sorted({convert(value) for value in values})
        if len(distinct) == 1:
            self.set(name, distinct[0])
        elif distinct:
            self.fields[name] = ReadField(
                name,
                flag=Flag.SEVERAL_VALUES,
                note=f"the e-invoice states {len(distinct)} values: {', '.join(distinct)}",
                values=tuple(distinct),
            )

    def check_iban(self) -> None:
        found = self.fields.get("iban_on_document")
        if found is not None and found.value is not None and not iban_valid(found.value):
            self.fields["iban_on_document"] = ReadField(
                "iban_on_document",
                found.value,
                Source.EINVOICE_XML,
                flag=Flag.FAILED_CHECK,
                note="the IBAN check digits are wrong",
            )

    def discount(self, terms: list[str], invoice_date: str | None) -> None:
        """The German XRechnung convention for an early payment discount (Skonto) in the payment
        terms: ``#SKONTO#TAGE=10#PROZENT=2.00#``, counted from the invoice date."""
        lines = [line.strip() for text in terms for line in text.splitlines()]
        skonto = [line for line in lines if line.upper().startswith("#SKONTO#")]
        if not skonto:
            return
        if len(skonto) > 1:
            self.fields["early_payment_discount"] = ReadField(
                "early_payment_discount",
                flag=Flag.SEVERAL_VALUES,
                note=f"the e-invoice states {len(skonto)} discounts",
            )
            return
        parts = dict(
            part.split("=", 1) for part in skonto[0].strip("#").split("#")[1:] if "=" in part
        )
        try:
            days = int(parts["TAGE"])
            percent = Decimal(parts["PROZENT"]).quantize(Decimal("0.01"))
            if invoice_date is None or days < 0 or not 0 < percent < 100:
                raise ValueError(skonto[0])
        except (KeyError, ValueError, InvalidOperation):
            self.fields["early_payment_discount"] = ReadField(
                "early_payment_discount",
                flag=Flag.FAILED_CHECK,
                note="the discount terms in the e-invoice cannot be read",
            )
            return
        until = date.fromisoformat(invoice_date) + timedelta(days=days)
        self.set("early_payment_discount", {"percent": str(percent), "until": until.isoformat()})


def _ubl(root: Element, *, credit_note: bool) -> tuple[dict[str, ReadField], dict[str, Any]]:
    out = _Fields()
    seller = "cac:AccountingSupplierParty/cac:Party"
    buyer = "cac:AccountingCustomerParty/cac:Party"
    currency = _text(root, "cbc:DocumentCurrencyCode")
    type_code = _text(root, "cbc:CreditNoteTypeCode" if credit_note else "cbc:InvoiceTypeCode")

    out.set("invoice_number", _text(root, "cbc:ID"))
    out.set(
        "issuer_name",
        _text(root, f"{seller}/cac:PartyLegalEntity/cbc:RegistrationName")
        or _text(root, f"{seller}/cac:PartyName/cbc:Name"),
    )
    out.set(
        "recipient_name",
        _text(root, f"{buyer}/cac:PartyLegalEntity/cbc:RegistrationName")
        or _text(root, f"{buyer}/cac:PartyName/cbc:Name"),
    )
    vat_ids = [
        _text(scheme, "cbc:CompanyID")
        for scheme in _all(root, f"{seller}/cac:PartyTaxScheme")
        if _text(scheme, "cac:TaxScheme/cbc:ID") == "VAT"
    ]
    out.one_of("vendor_vat_id", [v for v in vat_ids if v], _vat_id)
    issued = _text(root, "cbc:IssueDate")
    out.set("invoice_date", _iso_date(issued) if issued else None)
    start = _text(root, "cac:InvoicePeriod/cbc:StartDate")
    end = _text(root, "cac:InvoicePeriod/cbc:EndDate")
    delivered = _text(root, "cac:Delivery/cbc:ActualDeliveryDate")
    if start or end:
        out.set("service_period", [_iso_date(start or end or ""), _iso_date(end or start or "")])
    elif delivered:
        out.set("service_period", [_iso_date(delivered), _iso_date(delivered)])

    totals = "cac:LegalMonetaryTotal"
    for name, path in (
        ("net_amount", f"{totals}/cbc:TaxExclusiveAmount"),
        ("gross_amount", f"{totals}/cbc:TaxInclusiveAmount"),
    ):
        value = _text(root, path)
        out.set(name, _money(value) if value else None)
    vat = _in_currency(_all(root, "cac:TaxTotal/cbc:TaxAmount"), currency)
    out.one_of("vat_amount", vat, _money)
    rates = _texts(root, "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:Percent")
    out.one_of("vat_rate", rates, _rate)
    _zero_rate_without_percent(out)
    out.set("currency", currency)

    due = _text(root, "cbc:DueDate") or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate")
    out.set("due_date", _iso_date(due) if due else None)
    means = _all(root, "cac:PaymentMeans")
    codes = {_text(m, "cbc:PaymentMeansCode") for m in means}
    ibans = [
        iban
        for m in means
        if _text(m, "cbc:PaymentMeansCode") not in DIRECT_DEBIT_CODES
        for iban in _texts(m, "cac:PayeeFinancialAccount/cbc:ID")
    ]
    out.one_of("iban_on_document", ibans, _iban)
    out.check_iban()
    out.discount(_texts(root, "cac:PaymentTerms/cbc:Note"), _issued(out))
    return out.fields, {
        "type_code": type_code or ("381" if credit_note else "380"),
        "specification": _text(root, "cbc:CustomizationID"),
        "direct_debit": bool(codes & DIRECT_DEBIT_CODES),
        "refers_to": _text(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID"),
    }


def _cii(root: Element) -> tuple[dict[str, ReadField], dict[str, Any]]:
    out = _Fields()
    doc = "rsm:ExchangedDocument"
    tx = "rsm:SupplyChainTradeTransaction"
    agreement = f"{tx}/ram:ApplicableHeaderTradeAgreement"
    delivery = f"{tx}/ram:ApplicableHeaderTradeDelivery"
    settlement = f"{tx}/ram:ApplicableHeaderTradeSettlement"
    totals = f"{settlement}/ram:SpecifiedTradeSettlementHeaderMonetarySummation"
    currency = _text(root, f"{settlement}/ram:InvoiceCurrencyCode")

    out.set("invoice_number", _text(root, f"{doc}/ram:ID"))
    out.set("issuer_name", _text(root, f"{agreement}/ram:SellerTradeParty/ram:Name"))
    out.set("recipient_name", _text(root, f"{agreement}/ram:BuyerTradeParty/ram:Name"))
    out.one_of(
        "vendor_vat_id",
        _texts(
            root,
            f"{agreement}/ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']",
        ),
        _vat_id,
    )
    out.set("invoice_date", _cii_date(root.find(f"{doc}/ram:IssueDateTime/udt:DateTimeString", NS)))
    period = f"{settlement}/ram:BillingSpecifiedPeriod"
    start = _cii_date(root.find(f"{period}/ram:StartDateTime/udt:DateTimeString", NS))
    end = _cii_date(root.find(f"{period}/ram:EndDateTime/udt:DateTimeString", NS))
    delivered = _cii_date(
        root.find(
            f"{delivery}/ram:ActualDeliverySupplyChainEvent/ram:OccurrenceDateTime/udt:DateTimeString",
            NS,
        )
    )
    if start or end:
        out.set("service_period", [start or end, end or start])
    elif delivered:
        out.set("service_period", [delivered, delivered])

    for name, path in (
        ("net_amount", f"{totals}/ram:TaxBasisTotalAmount"),
        ("gross_amount", f"{totals}/ram:GrandTotalAmount"),
    ):
        value = _text(root, path)
        out.set(name, _money(value) if value else None)
    out.one_of(
        "vat_amount", _in_currency(_all(root, f"{totals}/ram:TaxTotalAmount"), currency), _money
    )
    out.one_of(
        "vat_rate",
        _texts(root, f"{settlement}/ram:ApplicableTradeTax/ram:RateApplicablePercent"),
        _rate,
    )
    _zero_rate_without_percent(out)
    out.set("currency", currency)

    terms = _all(root, f"{settlement}/ram:SpecifiedTradePaymentTerms")
    dues = [
        d for t in terms if (d := _cii_date(t.find("ram:DueDateDateTime/udt:DateTimeString", NS)))
    ]
    out.one_of("due_date", dues, str)
    means = _all(root, f"{settlement}/ram:SpecifiedTradeSettlementPaymentMeans")
    codes = {_text(m, "ram:TypeCode") for m in means}
    ibans = [
        iban
        for m in means
        if _text(m, "ram:TypeCode") not in DIRECT_DEBIT_CODES
        for iban in _texts(m, "ram:PayeePartyCreditorFinancialAccount/ram:IBANID")
    ]
    out.one_of("iban_on_document", ibans, _iban)
    out.check_iban()
    out.discount([d for t in terms if (d := _text(t, "ram:Description"))], _issued(out))
    return out.fields, {
        "type_code": _text(root, f"{doc}/ram:TypeCode") or "",
        "specification": _text(
            root,
            "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID",
        ),
        "direct_debit": bool(codes & DIRECT_DEBIT_CODES),
        "refers_to": _text(
            root, f"{settlement}/ram:InvoiceReferencedDocument/ram:IssuerAssignedID"
        ),
    }


def _vat_id(value: str) -> str:
    return "".join(value.split()).upper()


def _iban(value: str) -> str:
    return "".join(value.split()).upper()


def _issued(out: _Fields) -> str | None:
    found = out.fields.get("invoice_date")
    return found.value if found else None


def _zero_rate_without_percent(out: _Fields) -> None:
    """An exempt or reverse charge invoice may state no rate at all. With no VAT charged the
    rate is 0, the value the PDF readings use too."""
    vat = out.fields.get("vat_amount")
    if "vat_rate" not in out.fields and vat is not None and vat.value == "0.00":
        out.set("vat_rate", "0")

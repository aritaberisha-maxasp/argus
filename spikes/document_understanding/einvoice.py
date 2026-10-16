"""Read structured e-invoices: XRechnung (UBL or CII) and ZUGFeRD / Factur-X (CII).

Covers: IN-02, NF-02, PR-01

Structured data wins over anything printed on a PDF. When an email carries an
e-invoice, ARGUS takes the fields from the XML and only uses the PDF as the
human readable copy.
"""

from __future__ import annotations

from decimal import Decimal

from lxml import etree

UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CII = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "rsm": CII,
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# UNTDID 1001 document type codes used in EN 16931.
TYPE_CODES = {
    "380": "invoice",
    "384": "invoice",  # corrected invoice
    "389": "invoice",  # self billed invoice
    "875": "invoice",  # partial construction invoice
    "381": "credit_note",
    "261": "credit_note",  # self billed credit note
}

# Names that PDF/A-3 hybrid invoices use for the embedded XML.
EMBEDDED_XML_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml", "zugferd.xml")


def detect_syntax(xml_bytes: bytes) -> str | None:
    """Return "ubl", "cii" or None if the bytes are not an e-invoice."""
    try:
        root = etree.fromstring(xml_bytes, parser=_safe_parser())
    except etree.XMLSyntaxError:
        return None
    ns = etree.QName(root).namespace
    if ns in (UBL_INVOICE, UBL_CREDIT_NOTE):
        return "ubl"
    if ns == CII:
        return "cii"
    return None


def parse(xml_bytes: bytes) -> dict:
    """Parse an e-invoice into the PR-01 field set. Amounts are strings with two decimals."""
    root = etree.fromstring(xml_bytes, parser=_safe_parser())
    syntax = detect_syntax(xml_bytes)
    if syntax == "ubl":
        return _parse_ubl(root)
    if syntax == "cii":
        return _parse_cii(root)
    raise ValueError("not a UBL or CII e-invoice")


def _safe_parser() -> etree.XMLParser:
    # No external entities, no network: attachments come from strangers.
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _text(node, path: str) -> str | None:
    found = node.xpath(path, namespaces=NS)
    if not found:
        return None
    value = found[0] if isinstance(found[0], str) else found[0].text
    return value.strip() if value else None


def _money(value: str | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def _rate(value: str | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).normalize()) if Decimal(value) != 0 else "0"


def _cii_date(value: str | None) -> str | None:
    if not value or len(value) != 8:
        return value
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _parse_ubl(root) -> dict:
    is_credit = etree.QName(root).namespace == UBL_CREDIT_NOTE
    type_code = _text(root, "cbc:InvoiceTypeCode") or _text(root, "cbc:CreditNoteTypeCode")
    seller = "cac:AccountingSupplierParty/cac:Party"
    buyer = "cac:AccountingCustomerParty/cac:Party"
    period_start = _text(root, "cac:InvoicePeriod/cbc:StartDate")
    period_end = _text(root, "cac:InvoicePeriod/cbc:EndDate")
    return {
        "syntax": "ubl",
        "kind": "credit_note" if is_credit else TYPE_CODES.get(type_code or "380", "invoice"),
        "type_code": type_code,
        "issuer_name": _text(root, f"{seller}/cac:PartyLegalEntity/cbc:RegistrationName")
        or _text(root, f"{seller}/cac:PartyName/cbc:Name"),
        "recipient_name": _text(root, f"{buyer}/cac:PartyLegalEntity/cbc:RegistrationName")
        or _text(root, f"{buyer}/cac:PartyName/cbc:Name"),
        "vendor_vat_id": _text(root, f"{seller}/cac:PartyTaxScheme/cbc:CompanyID"),
        "invoice_number": _text(root, "cbc:ID"),
        "invoice_date": _text(root, "cbc:IssueDate"),
        "service_period": [period_start, period_end] if period_start else None,
        "net_amount": _money(_text(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount")),
        "vat_rate": _rate(_text(root, "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:Percent")),
        "vat_amount": _money(_text(root, "cac:TaxTotal/cbc:TaxAmount")),
        "gross_amount": _money(_text(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount")),
        "currency": _text(root, "cbc:DocumentCurrencyCode"),
        "due_date": _text(root, "cbc:DueDate") or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate"),
        "payment_terms": _text(root, "cac:PaymentTerms/cbc:Note"),
        "early_payment_discount": None,
        "iban_on_document": _text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID"),
        "direct_debit": _text(root, "cac:PaymentMeans/cbc:PaymentMeansCode") == "59",
        "refers_to": _text(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID"),
    }


def _parse_cii(root) -> dict:
    doc = "rsm:ExchangedDocument"
    tx = "rsm:SupplyChainTradeTransaction"
    agreement = f"{tx}/ram:ApplicableHeaderTradeAgreement"
    settlement = f"{tx}/ram:ApplicableHeaderTradeSettlement"
    totals = f"{settlement}/ram:SpecifiedTradeSettlementHeaderMonetarySummation"
    type_code = _text(root, f"{doc}/ram:TypeCode")
    period_start = _cii_date(_text(root, f"{settlement}/ram:BillingSpecifiedPeriod/ram:StartDateTime/udt:DateTimeString"))
    period_end = _cii_date(_text(root, f"{settlement}/ram:BillingSpecifiedPeriod/ram:EndDateTime/udt:DateTimeString"))
    terms = _text(root, f"{settlement}/ram:SpecifiedTradePaymentTerms/ram:Description")
    return {
        "syntax": "cii",
        "kind": TYPE_CODES.get(type_code or "380", "invoice"),
        "type_code": type_code,
        "issuer_name": _text(root, f"{agreement}/ram:SellerTradeParty/ram:Name"),
        "recipient_name": _text(root, f"{agreement}/ram:BuyerTradeParty/ram:Name"),
        "vendor_vat_id": _text(root, f"{agreement}/ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']"),
        "invoice_number": _text(root, f"{doc}/ram:ID"),
        "invoice_date": _cii_date(_text(root, f"{doc}/ram:IssueDateTime/udt:DateTimeString")),
        "service_period": [period_start, period_end] if period_start else None,
        "net_amount": _money(_text(root, f"{totals}/ram:TaxBasisTotalAmount")),
        "vat_rate": _rate(_text(root, f"{settlement}/ram:ApplicableTradeTax/ram:RateApplicablePercent")),
        "vat_amount": _money(_text(root, f"{totals}/ram:TaxTotalAmount")),
        "gross_amount": _money(_text(root, f"{totals}/ram:GrandTotalAmount")),
        "currency": _text(root, f"{settlement}/ram:InvoiceCurrencyCode"),
        "due_date": _cii_date(_text(root, f"{settlement}/ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString")),
        "payment_terms": terms,
        "early_payment_discount": _cii_skonto(terms),
        "iban_on_document": _text(root, f"{settlement}/ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID"),
        "direct_debit": _text(root, f"{settlement}/ram:SpecifiedTradeSettlementPaymentMeans/ram:TypeCode") == "59",
        "refers_to": _text(root, f"{settlement}/ram:InvoiceReferencedDocument/ram:IssuerAssignedID"),
    }


def _cii_skonto(terms: str | None) -> dict | None:
    """Read the German XRechnung Skonto convention: #SKONTO#TAGE=10#PROZENT=2.00#"""
    if not terms or "#SKONTO#" not in terms:
        return None
    parts = dict(p.split("=", 1) for p in terms.strip().strip("#").split("#") if "=" in p)
    if "PROZENT" not in parts:
        return None
    return {"percent": str(Decimal(parts["PROZENT"]).quantize(Decimal("0.01"))), "days": int(parts.get("TAGE", 0))}

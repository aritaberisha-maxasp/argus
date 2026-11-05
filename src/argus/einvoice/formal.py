"""The §14 UStG check for an e-invoice (#32, PR-03).

The XML is the legal original, so the check uses the e-invoice's reading. Two items are not in
the PR-01 fields and an e-invoice has no page text to search, so they come from the XML here:

- the vendor's German tax number (BT-32, scheme ``FC``), which may stand instead of a VAT ID;
- why no VAT is charged: a VAT category of reverse charge (``AE``), exempt (``E``),
  intra-community supply (``K``), export (``G``) or not subject to VAT (``O``), or an exemption
  reason (BT-120, BT-121). EN 16931 lets a seller state these only together with the reason, and
  the KoSIT validator checks that.
"""

from xml.etree.ElementTree import Element

from argus.einvoice.parser import NS, EInvoice
from argus.einvoice.safe_xml import XmlRefused, parse
from argus.reading.formal_check import OWN_COMPANY, FormalCheck, check_invoice

NO_VAT_CATEGORIES = frozenset({"AE", "E", "K", "G", "O"})


def check_einvoice(einvoice: EInvoice, own_company: str = OWN_COMPANY) -> FormalCheck | None:
    """Check one e-invoice. Returns None for a credit note, like :func:`check_invoice`."""
    tax_number, no_vat_reason = _from_xml(einvoice)
    return check_invoice(
        einvoice.reading, own_company, tax_number=tax_number, no_vat_reason=no_vat_reason
    )


def _from_xml(einvoice: EInvoice) -> tuple[str | None, str | None]:
    try:
        root = parse(einvoice.xml)
    except XmlRefused:
        # The reader parsed the same bytes already; this cannot happen, but never pass a check
        # on a document ARGUS could not read.
        return None, None
    if einvoice.syntax == "ubl":
        seller = "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme"
        tax_numbers = [
            _text(scheme, "cbc:CompanyID")
            for scheme in root.findall(seller, NS)
            if _text(scheme, "cac:TaxScheme/cbc:ID") not in (None, "VAT")
        ]
        categories = root.findall("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
        codes = [_text(c, "cbc:ID") for c in categories]
        reasons = [
            _text(c, "cbc:TaxExemptionReason") or _text(c, "cbc:TaxExemptionReasonCode")
            for c in categories
        ]
    else:
        tx = "rsm:SupplyChainTradeTransaction"
        seller = f"{tx}/ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty"
        tax_numbers = [
            _text(found, ".")
            for found in root.findall(f"{seller}/ram:SpecifiedTaxRegistration/ram:ID", NS)
            if found.get("schemeID") == "FC"
        ]
        taxes = root.findall(f"{tx}/ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", NS)
        codes = [_text(t, "ram:CategoryCode") for t in taxes]
        reasons = [
            _text(t, "ram:ExemptionReason") or _text(t, "ram:ExemptionReasonCode") for t in taxes
        ]
    tax_number = next((n for n in tax_numbers if n), None)
    reason = next((r for r in reasons if r), None)
    if reason is None:
        reason = next((f"VAT category {c}" for c in codes if c in NO_VAT_CATEGORIES), None)
    return tax_number, reason


def _text(node: Element, path: str) -> str | None:
    found = node if path == "." else node.find(path, NS)
    if found is None:
        return None
    value = "".join(found.itertext()).strip()
    return value or None

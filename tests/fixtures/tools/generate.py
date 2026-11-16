"""Generate the ARGUS synthetic test fixtures.

Usage, from the repository root:
    python3 -m pip install -r tests/fixtures/tools/requirements.txt
    python3 tests/fixtures/tools/generate.py

Writes into tests/fixtures/:
    emails/        50 .eml files and manifest.csv with the expected outcome
    documents/     every invoice and letter on its own, plus expected.json
    master-data/   vendors, customers, approver table and roles

The output is committed, so tests never need to run this script. Run it again
only after changing fixture_data.py or email_data.py, then run check.py.
"""

import csv
import io
import json
import random
import sys
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, TextStringObject
from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_data import EMAILS, EXTRA_SENDERS, ROUTES  # noqa: E402
from fixture_data import (  # noqa: E402
    ACCOUNTS_MAILBOX, CUSTOMERS, INVOICES, LETTERS, MAX_ASP, OBJECT_IDS, PEOPLE, ROLES,
    UNKNOWN_CUSTOMER, UNKNOWN_PARTIES, VENDORS, money,
)

rl_config.invariant = 1  # same input, same PDF bytes

ROOT = Path(__file__).resolve().parent.parent
EMAIL_DIR = ROOT / "emails"
DOC_DIR = ROOT / "documents"
MASTER_DIR = ROOT / "master-data"

VAT_RATES = {"standard": Decimal("19"), "reduced": Decimal("7"), "reverse_charge": Decimal("0"), "not_taxable": Decimal("0")}
TAX_TREATMENT = {"standard": "standard", "reduced": "standard_reduced_rate",
                 "reverse_charge": "reverse_charge", "not_taxable": "not_taxable_third_country"}

LABELS = {
    "de": {
        "invoice": "Rechnung", "credit_note": "Gutschrift", "cancellation": "Stornorechnung",
        "proforma": "Proforma-Rechnung", "number": "Nummer", "date": "Datum", "period": "Leistungszeitraum",
        "pos": "Pos.", "description": "Beschreibung", "qty": "Menge", "unit": "Einzelpreis", "amount": "Betrag",
        "net": "Nettobetrag", "vat": "USt", "gross": "Gesamtbetrag", "vat_id": "USt-IdNr.", "bank": "Bankverbindung",
        "refers": "Bezug: Rechnung", "due": "Zahlbar bis {due} ohne Abzug.",
        "discount": "Bei Zahlung bis {until} gewähren wir {percent} % Skonto.",
        "direct_debit": "Der Betrag wird am {due} per SEPA-Lastschrift von Ihrem Konto eingezogen.",
        "offset": "Der Betrag wird mit offenen Rechnungen verrechnet.",
        "reverse_charge": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
        "not_taxable": "Nicht im Inland steuerbare Leistung.",
    },
    "en": {
        "invoice": "Invoice", "credit_note": "Credit note", "cancellation": "Cancellation invoice",
        "proforma": "Pro forma invoice", "number": "Number", "date": "Date", "period": "Service period",
        "pos": "No.", "description": "Description", "qty": "Qty", "unit": "Unit price", "amount": "Amount",
        "net": "Net amount", "vat": "VAT", "gross": "Total", "vat_id": "VAT ID", "bank": "Bank details",
        "refers": "Refers to invoice", "due": "Payment due by {due}.",
        "discount": "{percent} % early payment discount if paid by {until}.",
        "direct_debit": "The amount will be collected by SEPA direct debit on {due}.",
        "offset": "This amount will be offset against open invoices.",
        "reverse_charge": "Reverse charge: VAT to be accounted for by the recipient.",
        "not_taxable": "Not subject to German VAT, place of supply outside Germany.",
    },
}


# ---------------------------------------------------------------- helpers

def party(key):
    if key == "max_asp":
        return MAX_ASP
    if key == "unknown_customer":
        return UNKNOWN_CUSTOMER
    for table in (VENDORS, UNKNOWN_PARTIES, CUSTOMERS):
        if key in table:
            return table[key]
    raise KeyError(key)


def sender(key):
    if key in EXTRA_SENDERS:
        return EXTRA_SENDERS[key]
    if key in PEOPLE:
        return PEOPLE[key]
    p = party(key)
    return p["name"], p["email"]


def fmt_date(iso, lang):
    d = date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y") if lang == "de" else d.strftime("%d %b %Y")


def fmt_money(value, currency, lang):
    text = f"{value:,.2f}"
    if lang == "de":
        text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text} {currency}"


def fmt_qty(q):
    return str(int(q)) if q == q.to_integral() else format(q.normalize(), "f")


def totals(doc):
    lines = [dict(line, amount=money(line["quantity"] * line["unit_price"])) for line in doc["lines"]]
    net = sum((line["amount"] for line in lines), Decimal("0.00"))
    rate = VAT_RATES[doc["tax"]]
    vat = money(net * rate / 100)
    return lines, net, rate, vat, net + vat


def doc_parties(doc):
    issuer = party(doc["issuer"])
    if doc["direction"] == "outgoing":
        recipient = party(doc["recipient"])
    else:
        recipient = party(doc["recipient"]) if doc.get("recipient") else MAX_ASP
    return issuer, recipient


def doc_iban(doc):
    issuer = party(doc["issuer"])
    return doc.get("iban_override") or issuer.get("iban")


def address_lines(p):
    return [p["name"], p["street"], f"{p['postcode']} {p['city']}", p["country"]]


def invoice_text(doc):
    """The visible content of an invoice, as a list of (style, text) rows."""
    t = LABELS[doc["lang"]]
    lang, cur = doc["lang"], doc["currency"]
    issuer, recipient = doc_parties(doc)
    lines, net, rate, vat, gross = totals(doc)
    rows = [("head", issuer["name"]),
            ("small", f"{issuer['street']}, {issuer['postcode']} {issuer['city']}, {issuer['country']}"),
            ("small", f"{t['vat_id']} {issuer['vat_id']}" if issuer.get("vat_id") else ""),
            ("gap", "")]
    rows += [("text", line) for line in address_lines(recipient)]
    rows += [("gap", ""), ("title", t[doc["kind"]]),
             ("text", f"{t['number']}: {doc['number']}"),
             ("text", f"{t['date']}: {fmt_date(doc['date'], lang)}"),
             ("text", f"{t['period']}: {fmt_date(doc['period'][0], lang)} - {fmt_date(doc['period'][1], lang)}")]
    if doc.get("refers_to"):
        rows.append(("text", f"{t['refers']} {doc['refers_to']}"))
    rows += [("gap", ""), ("table_head", (t["pos"], t["description"], t["qty"], t["unit"], t["amount"]))]
    for i, line in enumerate(lines, 1):
        rows.append(("table_row", (str(i), line["description"], fmt_qty(line["quantity"]),
                                   fmt_money(line["unit_price"], cur, lang), fmt_money(line["amount"], cur, lang))))
    rows += [("gap", ""),
             ("total", (t["net"], fmt_money(net, cur, lang))),
             ("total", (f"{t['vat']} {rate:.0f} %", fmt_money(vat, cur, lang))),
             ("total_bold", (t["gross"], fmt_money(gross, cur, lang))),
             ("gap", "")]
    if doc["tax"] in ("reverse_charge", "not_taxable"):
        rows.append(("text", t[doc["tax"]]))
    if doc["kind"] in ("credit_note", "cancellation"):
        rows.append(("text", t["offset"]))
    elif doc.get("direct_debit"):
        rows.append(("text", t["direct_debit"].format(due=fmt_date(doc["due"], lang))))
    else:
        rows.append(("text", t["due"].format(due=fmt_date(doc["due"], lang))))
        if doc.get("discount"):
            rows.append(("text", t["discount"].format(percent=doc["discount"]["percent"].replace(".", ",") if lang == "de"
                                                      else doc["discount"]["percent"],
                                                      until=fmt_date(doc["discount"]["until"], lang))))
    iban = doc_iban(doc)
    if iban:
        rows.append(("text", f"{t['bank']}: IBAN {' '.join(iban[i:i + 4] for i in range(0, len(iban), 4))}"))
    return rows


def letter_text(letter):
    rows = []
    if letter["issuer"]:
        issuer = party(letter["issuer"])
        rows += [("head", issuer["name"]),
                 ("small", f"{issuer['street']}, {issuer['postcode']} {issuer['city']}"), ("gap", "")]
        rows += [("text", line) for line in address_lines(MAX_ASP)]
        rows += [("gap", ""), ("text", fmt_date(letter["date"], "de")), ("gap", "")]
    if letter["title"]:
        rows += [("title", letter["title"]), ("gap", "")]
    for paragraph in letter["paragraphs"]:
        rows += [("text", line) for line in paragraph.split("\n")]
        rows.append(("gap", ""))
    return rows


# ---------------------------------------------------------------- renderers

def wrap(text, width):
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width and current:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return lines + ([current] if current else [])


def render_pdf(rows, title):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    c.setTitle(title)
    c.setAuthor("ARGUS synthetic test fixture")
    x, y = 20 * mm, A4[1] - 20 * mm
    cols = [20 * mm, 32 * mm, 120 * mm, 140 * mm, 170 * mm]
    for style, value in rows:
        if style == "gap":
            y -= 4 * mm
            continue
        if style == "head":
            c.setFont("Helvetica-Bold", 14)
        elif style == "title":
            c.setFont("Helvetica-Bold", 16)
        elif style == "small":
            c.setFont("Helvetica", 8)
        elif style in ("table_head", "total_bold"):
            c.setFont("Helvetica-Bold", 9)
        else:
            c.setFont("Helvetica", 10 if style == "text" else 9)
        if style in ("table_head", "table_row"):
            for col, cell in zip(cols, value):
                c.drawString(col, y, cell)
            y -= 5 * mm
        elif style.startswith("total"):
            c.drawString(120 * mm, y, value[0])
            c.drawRightString(190 * mm, y, value[1])
            y -= 5 * mm
        else:
            for line in wrap(value, 95) or [""]:
                c.drawString(x, y, line)
                y -= 5 * mm if style != "title" else 8 * mm
    c.setFont("Helvetica", 7)
    c.drawString(x, 12 * mm, "Synthetic test document for ARGUS. Not a real invoice.")
    c.showPage()
    c.save()
    return buffer.getvalue()


def font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    for folder in ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "C:/Windows/Fonts"):
        for name in names:
            path = Path(folder) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def render_scan(rows, seed, faded=False):
    """Draw the rows on an A4 page and make it look like a phone scan."""
    rng = random.Random(seed)
    width, height = 992, 1403  # A4 at 120 dpi
    page = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(page)
    ink = 215 if faded else 25
    x, y = 90, 90
    for style, value in rows:
        if style == "gap":
            y += 16
            continue
        size = {"head": 30, "title": 34, "small": 16}.get(style, 20)
        f = font(size, bold=style in ("head", "title", "table_head", "total_bold"))
        if style in ("table_head", "table_row"):
            for col, cell in zip((90, 150, 560, 680, 830), value):
                draw.text((col, y), cell, fill=ink, font=f)
        elif style.startswith("total"):
            draw.text((560, y), value[0], fill=ink, font=f)
            draw.text((830, y), value[1], fill=ink, font=f)
        else:
            for line in wrap(value, 70) or [""]:
                draw.text((x, y), line, fill=ink, font=f)
                y += size + 8
            continue
        y += size + 10
    draw.text((x, height - 60), "Synthetic test document for ARGUS. Not a real invoice.", fill=ink, font=font(12))
    page = page.rotate(rng.uniform(-2.5, 2.5), resample=Image.BICUBIC, fillcolor=235)
    if faded:
        page = page.filter(ImageFilter.GaussianBlur(4))
    noise = page.load()
    for _ in range(width * height // (8 if faded else 40)):
        px, py = rng.randrange(width), rng.randrange(height)
        noise[px, py] = max(0, min(255, noise[px, py] + rng.randint(-90, 90)))
    page = page.filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    page.save(out, "JPEG", quality=55 if faded else 70, optimize=True)
    return out.getvalue()


def ubl_tax_category(doc, rate):
    code = {"standard": "S", "reduced": "S", "reverse_charge": "AE", "not_taxable": "O"}[doc["tax"]]
    extra = ""
    if code == "AE":
        extra = "<cbc:TaxExemptionReason>Reverse charge</cbc:TaxExemptionReason>"
    elif code == "O":
        extra = "<cbc:TaxExemptionReason>Not subject to VAT</cbc:TaxExemptionReason>"
    percent = "" if code == "O" else f"<cbc:Percent>{rate:.2f}</cbc:Percent>"
    return code, percent, extra


def ubl_party(p, contact):
    vat = (f"<cac:PartyTaxScheme><cbc:CompanyID>{p['vat_id']}</cbc:CompanyID>"
           "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>") if p.get("vat_id") else ""
    contact_xml = (f"<cac:Contact><cbc:Name>Buchhaltung</cbc:Name><cbc:Telephone>+49 000 000000</cbc:Telephone>"
                   f"<cbc:ElectronicMail>{p['email']}</cbc:ElectronicMail></cac:Contact>") if contact else ""
    return (f"<cac:Party><cbc:EndpointID schemeID=\"EM\">{p['email']}</cbc:EndpointID>"
            f"<cac:PostalAddress><cbc:StreetName>{escape(p['street'])}</cbc:StreetName>"
            f"<cbc:CityName>{escape(p['city'])}</cbc:CityName><cbc:PostalZone>{p['postcode']}</cbc:PostalZone>"
            f"<cac:Country><cbc:IdentificationCode>{p['country']}</cbc:IdentificationCode></cac:Country></cac:PostalAddress>"
            f"{vat}<cac:PartyLegalEntity><cbc:RegistrationName>{escape(p['name'])}</cbc:RegistrationName></cac:PartyLegalEntity>"
            f"{contact_xml}</cac:Party>")


def xrechnung(doc):
    """XRechnung 3.0 in UBL syntax."""
    issuer, recipient = doc_parties(doc)
    lines, net, rate, vat, gross = totals(doc)
    cur = doc["currency"]
    code, percent, exemption = ubl_tax_category(doc, rate)
    tax_category = f"<cbc:ID>{code}</cbc:ID>{percent}<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>"
    invoice_lines = "".join(
        f"<cac:InvoiceLine><cbc:ID>{i}</cbc:ID>"
        f"<cbc:InvoicedQuantity unitCode=\"C62\">{fmt_qty(line['quantity'])}</cbc:InvoicedQuantity>"
        f"<cbc:LineExtensionAmount currencyID=\"{cur}\">{line['amount']}</cbc:LineExtensionAmount>"
        f"<cac:Item><cbc:Name>{escape(line['description'])}</cbc:Name>"
        f"<cac:ClassifiedTaxCategory>{tax_category}</cac:ClassifiedTaxCategory></cac:Item>"
        f"<cac:Price><cbc:PriceAmount currencyID=\"{cur}\">{line['unit_price']}</cbc:PriceAmount></cac:Price>"
        "</cac:InvoiceLine>"
        for i, line in enumerate(lines, 1))
    terms = LABELS["de"]["due"].format(due=fmt_date(doc["due"], "de"))
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<ubl:Invoice xmlns:ubl=\"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2\" "
        "xmlns:cac=\"urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2\" "
        "xmlns:cbc=\"urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2\">"
        "<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>"
        "<cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>"
        f"<cbc:ID>{doc['number']}</cbc:ID><cbc:IssueDate>{doc['date']}</cbc:IssueDate><cbc:DueDate>{doc['due']}</cbc:DueDate>"
        f"<cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>"
        "<cbc:Note>Synthetic test document for ARGUS. Not a real invoice.</cbc:Note>"
        f"<cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode><cbc:BuyerReference>n/a</cbc:BuyerReference>"
        f"<cac:InvoicePeriod><cbc:StartDate>{doc['period'][0]}</cbc:StartDate><cbc:EndDate>{doc['period'][1]}</cbc:EndDate></cac:InvoicePeriod>"
        f"<cac:AccountingSupplierParty>{ubl_party(issuer, True)}</cac:AccountingSupplierParty>"
        f"<cac:AccountingCustomerParty>{ubl_party(recipient, False)}</cac:AccountingCustomerParty>"
        f"<cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>"
        f"<cac:PayeeFinancialAccount><cbc:ID>{doc_iban(doc)}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
        f"<cac:PaymentTerms><cbc:Note>{escape(terms)}</cbc:Note></cac:PaymentTerms>"
        f"<cac:TaxTotal><cbc:TaxAmount currencyID=\"{cur}\">{vat}</cbc:TaxAmount><cac:TaxSubtotal>"
        f"<cbc:TaxableAmount currencyID=\"{cur}\">{net}</cbc:TaxableAmount><cbc:TaxAmount currencyID=\"{cur}\">{vat}</cbc:TaxAmount>"
        f"<cac:TaxCategory><cbc:ID>{code}</cbc:ID>{percent}{exemption}<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>"
        "</cac:TaxSubtotal></cac:TaxTotal>"
        f"<cac:LegalMonetaryTotal><cbc:LineExtensionAmount currencyID=\"{cur}\">{net}</cbc:LineExtensionAmount>"
        f"<cbc:TaxExclusiveAmount currencyID=\"{cur}\">{net}</cbc:TaxExclusiveAmount>"
        f"<cbc:TaxInclusiveAmount currencyID=\"{cur}\">{gross}</cbc:TaxInclusiveAmount>"
        f"<cbc:PayableAmount currencyID=\"{cur}\">{gross}</cbc:PayableAmount></cac:LegalMonetaryTotal>"
        f"{invoice_lines}</ubl:Invoice>\n")
    return xml.encode("utf-8")


def cii_party(tag, p):
    vat = f"<ram:SpecifiedTaxRegistration><ram:ID schemeID=\"VA\">{p['vat_id']}</ram:ID></ram:SpecifiedTaxRegistration>" \
        if p.get("vat_id") else ""
    return (f"<ram:{tag}><ram:Name>{escape(p['name'])}</ram:Name><ram:PostalTradeAddress>"
            f"<ram:PostcodeCode>{p['postcode']}</ram:PostcodeCode><ram:LineOne>{escape(p['street'])}</ram:LineOne>"
            f"<ram:CityName>{escape(p['city'])}</ram:CityName><ram:CountryID>{p['country']}</ram:CountryID>"
            f"</ram:PostalTradeAddress>{vat}</ram:{tag}>")


def factur_x(doc):
    """ZUGFeRD 2 / Factur-X XML, EN 16931 profile, CII syntax."""
    issuer, recipient = doc_parties(doc)
    lines, net, rate, vat, gross = totals(doc)
    cur = doc["currency"]
    d = lambda iso: f"<udt:DateTimeString format=\"102\">{iso.replace('-', '')}</udt:DateTimeString>"  # noqa: E731
    tax = (f"<ram:TypeCode>VAT</ram:TypeCode><ram:CategoryCode>S</ram:CategoryCode>"
           f"<ram:RateApplicablePercent>{rate:.2f}</ram:RateApplicablePercent>")
    items = "".join(
        "<ram:IncludedSupplyChainTradeLineItem>"
        f"<ram:AssociatedDocumentLineDocument><ram:LineID>{i}</ram:LineID></ram:AssociatedDocumentLineDocument>"
        f"<ram:SpecifiedTradeProduct><ram:Name>{escape(line['description'])}</ram:Name></ram:SpecifiedTradeProduct>"
        f"<ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>{line['unit_price']}"
        "</ram:ChargeAmount></ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>"
        f"<ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode=\"C62\">{fmt_qty(line['quantity'])}"
        "</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>"
        f"<ram:SpecifiedLineTradeSettlement><ram:ApplicableTradeTax>{tax}</ram:ApplicableTradeTax>"
        f"<ram:SpecifiedTradeSettlementLineMonetarySummation><ram:LineTotalAmount>{line['amount']}</ram:LineTotalAmount>"
        "</ram:SpecifiedTradeSettlementLineMonetarySummation></ram:SpecifiedLineTradeSettlement>"
        "</ram:IncludedSupplyChainTradeLineItem>"
        for i, line in enumerate(lines, 1))
    discount = doc.get("discount")
    days = (date.fromisoformat(discount["until"]) - date.fromisoformat(doc["date"])).days if discount else 0
    skonto = f"#SKONTO#TAGE={days}#PROZENT={discount['percent']}#\n" if discount else ""
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rsm:CrossIndustryInvoice xmlns:rsm=\"urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100\" "
        "xmlns:ram=\"urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100\" "
        "xmlns:udt=\"urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100\">"
        "<rsm:ExchangedDocumentContext><ram:GuidelineSpecifiedDocumentContextParameter>"
        "<ram:ID>urn:cen.eu:en16931:2017</ram:ID></ram:GuidelineSpecifiedDocumentContextParameter></rsm:ExchangedDocumentContext>"
        f"<rsm:ExchangedDocument><ram:ID>{doc['number']}</ram:ID><ram:TypeCode>380</ram:TypeCode>"
        f"<ram:IssueDateTime>{d(doc['date'])}</ram:IssueDateTime>"
        "<ram:IncludedNote><ram:Content>Synthetic test document for ARGUS. Not a real invoice.</ram:Content></ram:IncludedNote>"
        "</rsm:ExchangedDocument><rsm:SupplyChainTradeTransaction>"
        f"{items}<ram:ApplicableHeaderTradeAgreement>{cii_party('SellerTradeParty', issuer)}"
        f"{cii_party('BuyerTradeParty', recipient)}</ram:ApplicableHeaderTradeAgreement>"
        f"<ram:ApplicableHeaderTradeDelivery><ram:ActualDeliverySupplyChainEvent><ram:OccurrenceDateTime>"
        f"{d(doc['period'][0])}</ram:OccurrenceDateTime></ram:ActualDeliverySupplyChainEvent></ram:ApplicableHeaderTradeDelivery>"
        f"<ram:ApplicableHeaderTradeSettlement><ram:InvoiceCurrencyCode>{cur}</ram:InvoiceCurrencyCode>"
        "<ram:SpecifiedTradeSettlementPaymentMeans><ram:TypeCode>58</ram:TypeCode>"
        f"<ram:PayeePartyCreditorFinancialAccount><ram:IBANID>{doc_iban(doc)}</ram:IBANID>"
        "</ram:PayeePartyCreditorFinancialAccount></ram:SpecifiedTradeSettlementPaymentMeans>"
        f"<ram:ApplicableTradeTax><ram:CalculatedAmount>{vat}</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode>"
        f"<ram:BasisAmount>{net}</ram:BasisAmount><ram:CategoryCode>S</ram:CategoryCode>"
        f"<ram:RateApplicablePercent>{rate:.2f}</ram:RateApplicablePercent></ram:ApplicableTradeTax>"
        f"<ram:BillingSpecifiedPeriod><ram:StartDateTime>{d(doc['period'][0])}</ram:StartDateTime>"
        f"<ram:EndDateTime>{d(doc['period'][1])}</ram:EndDateTime></ram:BillingSpecifiedPeriod>"
        f"<ram:SpecifiedTradePaymentTerms><ram:Description>{skonto}</ram:Description>"
        f"<ram:DueDateDateTime>{d(doc['due'])}</ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>"
        f"<ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:LineTotalAmount>{net}</ram:LineTotalAmount>"
        f"<ram:TaxBasisTotalAmount>{net}</ram:TaxBasisTotalAmount>"
        f"<ram:TaxTotalAmount currencyID=\"{cur}\">{vat}</ram:TaxTotalAmount>"
        f"<ram:GrandTotalAmount>{gross}</ram:GrandTotalAmount><ram:DuePayableAmount>{gross}</ram:DuePayableAmount>"
        "</ram:SpecifiedTradeSettlementHeaderMonetarySummation></ram:ApplicableHeaderTradeSettlement>"
        "</rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>\n")
    return xml.encode("utf-8")


XMP = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"><pdfaid:part>3</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance></rdf:Description>
<rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
<fx:DocumentType>INVOICE</fx:DocumentType><fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
<fx:Version>1.0</fx:Version><fx:ConformanceLevel>EN 16931</fx:ConformanceLevel></rdf:Description>
</rdf:RDF></x:xmpmeta>
<?xpacket end="w"?>"""


def zugferd_pdf(pdf_bytes, xml_bytes):
    """Embed factur-x.xml into the PDF the way ZUGFeRD 2 / Factur-X expects."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes)))
    attachment = writer.add_attachment("factur-x.xml", xml_bytes)
    attachment.associated_file_relationship = NameObject("/Data")
    attachment.subtype = NameObject("/text/xml")
    attachment.description = TextStringObject("Factur-X invoice")
    writer._root_object[NameObject("/AF")] = ArrayObject([attachment.pdf_object.indirect_reference])
    metadata = DecodedStreamObject()
    metadata.set_data(XMP.encode("utf-8"))
    metadata.update({NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")})
    writer._root_object[NameObject("/Metadata")] = writer._add_object(metadata)
    writer._ID = None
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def body_invoice(doc):
    """Plain text version of an invoice for emails that carry it in the body."""
    rows = invoice_text(doc)
    out = []
    for style, value in rows:
        if style == "gap":
            out.append("")
        elif style in ("table_head", "table_row"):
            out.append("  ".join(value))
        elif style.startswith("total"):
            out.append(f"{value[0]}: {value[1]}")
        elif value:
            out.append(value)
    return "\n".join(out)


# ---------------------------------------------------------------- documents

def ascii_name(text):
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), (" ", "_"), (".", "")):
        text = text.replace(a, b)
    return text


def file_stem(doc):
    t = LABELS[doc["lang"]][doc["kind"]].replace(" ", "_").replace("-", "_")
    return f"{doc['id']}_{t}_{doc['number']}"


def build_documents():
    """Render every invoice and letter. Returns {ref: [(filename, bytes, mime)]} and expected data."""
    files, expected = {}, []
    for doc in INVOICES:
        rows = invoice_text(doc)
        stem = file_stem(doc)
        pdf = render_pdf(rows, f"{LABELS[doc['lang']][doc['kind']]} {doc['number']}")
        rendered = []
        if doc["format"] == "pdf":
            rendered = [(f"{stem}.pdf", pdf, "application/pdf")]
        elif doc["format"] == "scan":
            rendered = [(f"{doc['id']}_Scan_{doc['date']}.jpg", render_scan(rows, doc["id"]), "image/jpeg")]
        elif doc["format"] == "xrechnung":
            rendered = [(f"{stem}_xrechnung.xml", xrechnung(doc), "application/xml")]
            files[f"{doc['id']}+pdf"] = rendered + [(f"{stem}.pdf", pdf, "application/pdf")]
        elif doc["format"] == "zugferd":
            rendered = [(f"{stem}_zugferd.pdf", zugferd_pdf(pdf, factur_x(doc)), "application/pdf")]
        elif doc["format"] == "body":
            rendered = []
        files[doc["id"]] = rendered

        issuer, recipient = doc_parties(doc)
        lines, net, rate, vat, gross = totals(doc)
        counterparty_key = doc["issuer"] if doc["direction"] == "incoming" else doc["recipient"]
        counterparty = party(counterparty_key)
        expected.append({
            "document_id": doc["id"],
            "files": [name for name, _, _ in files.get(f"{doc['id']}+pdf", rendered)],
            "format": doc["format"],
            "direction": doc["direction"],
            "kind": doc["kind"],
            "issuer_name": issuer["name"],
            "recipient_name": recipient["name"],
            "counterparty_account": counterparty.get("account"),
            "vendor_vat_id": issuer.get("vat_id") or None,
            "invoice_number": doc["number"],
            "invoice_date": doc["date"],
            "service_period": list(doc["period"]),
            "net_amount": str(net),
            "vat_rate": str(rate),
            "vat_amount": str(vat),
            "gross_amount": str(gross),
            "currency": doc["currency"],
            "tax_treatment": TAX_TREATMENT[doc["tax"]],
            "due_date": doc["due"],
            "early_payment_discount": doc.get("discount"),
            "direct_debit": bool(doc.get("direct_debit")),
            "iban_on_document": doc_iban(doc),
            "refers_to": doc.get("refers_to"),
            "lines": [{"description": line["description"], "quantity": fmt_qty(line["quantity"]),
                       "unit_price": str(line["unit_price"]), "amount": str(line["amount"])} for line in lines],
        })

    for ref, letter in LETTERS.items():
        rows = letter_text(letter)
        title = letter["title"] or "Scan"
        if letter["format"] == "pdf":
            name = f"{ref}_{ascii_name(title)}.pdf"
            files[ref] = [(name, render_pdf(rows, title), "application/pdf")]
        else:
            faded = letter["format"] == "faded_scan"
            name = f"{ref}_scan_0001.jpg" if faded else f"{ref}_{ascii_name(title)}.jpg"
            files[ref] = [(name, render_scan(rows, ref, faded=faded), "image/jpeg")]
    return files, expected


# ---------------------------------------------------------------- emails

HTML_TEMPLATE = ("<!DOCTYPE html><html><body style=\"font-family: sans-serif\">{paragraphs}"
                 "<p style=\"font-size: 11px; color: #777\">Synthetic test email for ARGUS.</p></body></html>")


# Senders whose emails do not really come from their domain: SPF, DKIM and DMARC fail (#70).
NOT_AUTHENTICATED = {"phishing", "spam_invoice"}


def authentication_results(key, address):
    """The header Exchange Online writes on top of every email from the internet (ADR 0006).
    192.0.2.0/24 is a documentation network (RFC 5737)."""
    domain = address.rpartition("@")[2]
    if key in NOT_AUTHENTICATED:
        return (f"spf=fail (sender IP is 192.0.2.66) smtp.mailfrom={domain}; "
                f"dkim=none (message not signed) header.d=none;dmarc=fail action=quarantine "
                f"header.from={domain};compauth=fail reason=000")
    return (f"spf=pass (sender IP is 192.0.2.10) smtp.mailfrom={domain}; "
            f"dkim=pass (signature was verified) header.d={domain};dmarc=pass action=none "
            f"header.from={domain};compauth=pass reason=100")


def build_email(spec, files, invoices_by_id):
    name, address = sender(spec["from"])
    msg = EmailMessage()
    msg["Authentication-Results"] = authentication_results(spec["from"], address)
    msg["From"] = f"{name} <{address}>" if name else address
    msg["To"] = ACCOUNTS_MAILBOX
    msg["Subject"] = spec["subject"]
    msg["Date"] = format_datetime(datetime.fromisoformat(spec["date"]))
    msg["Message-ID"] = f"<{spec['id']}.{spec['slug']}@fixtures.argus.example>"
    body = spec.get("body", "")
    if spec.get("body_invoice"):
        body = ("Guten Tag,\n\nhier Ihre Rechnung. Bitte überweisen Sie den Betrag fristgerecht.\n\n"
                + body_invoice(invoices_by_id[spec["body_invoice"]]) + "\n\nMuster Telekom Service GmbH")
    msg.set_content(body)
    if spec.get("html"):
        paragraphs = "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in body.split("\n\n"))
        msg.add_alternative(HTML_TEMPLATE.format(paragraphs=paragraphs), subtype="html")
    for ref in spec["attachments"]:
        for filename, data, mime in files[ref]:
            # Attachments carry readable names without the internal document ID.
            public_name = filename.split("_", 1)[1]
            maintype, subtype = mime.split("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=public_name)
    for i, part in enumerate(p for p in msg.walk() if p.is_multipart()):
        part.set_boundary(f"argus-fixture-{spec['id']}-{i}")
    return msg.as_bytes()


# ---------------------------------------------------------------- master data

def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def build_master_data():
    MASTER_DIR.mkdir(exist_ok=True)
    write_csv(MASTER_DIR / "vendors.csv",
              ["vendor_account", "name", "street", "postcode", "city", "country", "vat_id", "iban", "currency", "payment_terms"],
              [[v["account"], v["name"], v["street"], v["postcode"], v["city"], v["country"], v["vat_id"], v["iban"],
                v["currency"], v["terms"]] for v in VENDORS.values()])
    write_csv(MASTER_DIR / "customers.csv",
              ["customer_account", "name", "street", "postcode", "city", "country", "vat_id", "tax_treatment"],
              [[c["account"], c["name"], c["street"], c["postcode"], c["city"], c["country"], c["vat_id"],
                c["tax_treatment"]] for c in CUSTOMERS.values()])
    # The domains the partners in DATEV really send from (#70). Not a DATEV field: ARGUS keeps it.
    write_csv(MASTER_DIR / "sender-domains.csv", ["domain", "kind", "account", "name"],
              [[p["email"].rpartition("@")[2], kind, p["account"], p["name"]]
               for kind, parties in (("vendor", VENDORS), ("customer", CUSTOMERS))
               for p in parties.values()])
    rows = []
    for v in VENDORS.values():
        if not v["approver"]:
            continue  # VM-02: this vendor has no approver on purpose
        approver, deputy = PEOPLE[v["approver"]], PEOPLE[v["deputy"]]
        rows.append([v["account"], v["name"], approver[0], approver[1], deputy[0], deputy[1],
                     "yes" if v["direct_debit"] else "no",
                     OBJECT_IDS[v["approver"]], OBJECT_IDS[v["deputy"]]])
    write_csv(MASTER_DIR / "approvers.csv",
              ["vendor_account", "vendor_name", "approver_name", "approver_email", "deputy_name", "deputy_email",
               "direct_debit", "approver_object_id", "deputy_object_id"], rows)
    write_csv(MASTER_DIR / "roles.csv", ["role", "description", "name", "email"],
              [[role, description, *PEOPLE[person]] for role, description, person in ROLES])


# ---------------------------------------------------------------- main

def main():
    for folder in (EMAIL_DIR, DOC_DIR):
        folder.mkdir(exist_ok=True)
        for old in folder.iterdir():
            if old.is_file():
                old.unlink()

    files, expected = build_documents()
    for rendered in files.values():
        for filename, data, _ in rendered:
            (DOC_DIR / filename).write_bytes(data)
    (DOC_DIR / "expected.json").write_text(json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    invoices_by_id = {doc["id"]: doc for doc in INVOICES}
    manifest = []
    for spec in EMAILS:
        filename = f"{spec['id']}_{spec['slug']}.eml"
        (EMAIL_DIR / filename).write_bytes(build_email(spec, files, invoices_by_id))
        documents = [d for ref in spec["attachments"] for d in [ref.split("+")[0]] if d.startswith("D")]
        if spec.get("body_invoice"):
            documents.append(spec["body_invoice"])
        manifest.append([spec["id"], filename, spec["category"], ROUTES[spec["category"]], spec["invoice_count"],
                         " ".join(documents), " ".join(spec["flags"]), " ".join(spec["requirements"]), spec["note"]])
    write_csv(EMAIL_DIR / "manifest.csv",
              ["email_id", "file", "expected_category", "expected_route", "invoice_count", "documents", "flags",
               "requirements", "note"], manifest)

    build_master_data()
    print(f"Wrote {len(manifest)} emails, {sum(len(v) for v in files.values())} document files, master data.")


if __name__ == "__main__":
    main()

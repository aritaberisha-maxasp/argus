"""Generate a small hold out set of 16 synthetic emails the prototype has never seen.

The prototype rules were frozen (commit "Add document understanding prototype for
the spike") before this file was written, and they were not changed after the run.
The emails differ on purpose from QA's fixtures: other layouts, other labels, two
VAT rates, two invoices in one PDF, a scanned PDF without text layer, a
Kleinbetragsrechnung, an invoice sent only as a link, and so on.

Same rules for synthetic data as tests/fixtures: .example and .invalid domains,
VAT IDs starting DE0, IBANs with bank code 0000.

Usage, from the repository root:
    python3 spikes/document_understanding/holdout/generate.py
"""

from __future__ import annotations

import csv
import io
import random
from email.message import EmailMessage
from pathlib import Path

import pypdfium2
from PIL import Image, ImageFilter
from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

rl_config.invariant = 1
pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))

OUT = Path(__file__).resolve().parent / "emails"
FOOTER = "Synthetic test document for ARGUS. Not a real invoice."
MAX = ["Max ASP GmbH", "Testallee 1", "70000 Musterstadt"]


def iban(country: str, bban: str) -> str:
    digits = "".join(str(int(c, 36)) for c in bban + country + "00")
    return f"{country}{98 - int(digits) % 97:02d}{bban}"


IBAN_A = iban("DE", "000000110000800011")
IBAN_B = iban("DE", "000000120000800012")
IBAN_C = iban("DE", "000000130000800013")


def spaced(value: str) -> str:
    return " ".join(value[i:i + 4] for i in range(0, len(value), 4))


def pdf(pages: list[list[tuple[float, float, str]]], size: int = 10) -> bytes:
    """Each item is (x in mm, y in mm from the top, text)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for items in pages:
        for x, y, text in items + [(20, 285, FOOTER)]:
            c.setFont("DejaVu", size if y < 280 else 7)
            c.drawString(x * mm, A4[1] - y * mm, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned(pdf_bytes: bytes, as_pdf: bool) -> bytes:
    """Rasterise a PDF page, tilt it and add noise like an office scanner."""
    page = pypdfium2.PdfDocument(pdf_bytes)[0]
    image = page.render(scale=200 / 72).to_pil().convert("L")
    image = image.rotate(1.2, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.6))
    rng = random.Random(7)
    pixels = image.load()
    for _ in range(image.width * image.height // 400):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = rng.choice((0, 160, 255))
    buf = io.BytesIO()
    image.save(buf, format="PDF" if as_pdf else "JPEG", quality=80, resolution=200)
    return buf.getvalue()


# ---------------------------------------------------------------- documents

def invoice_modern(number: str, date: str, lines: list[tuple[str, str]], net: str, vat: str, gross: str) -> bytes:
    """Recipient block first, labels on the left, issuer and VAT ID only in the footer."""
    items = [
        (20, 20, "An:"), (20, 26, MAX[0]), (20, 31, MAX[1]), (20, 36, MAX[2]),
        (130, 20, "Beispiel Gartenbau GmbH"),
        (20, 60, "RECHNUNG"),
        (20, 70, f"Rechnungs-Nr.: {number}"), (20, 76, f"Rechnungsdatum: {date}"),
        (20, 82, "Lieferdatum: entspricht Rechnungsdatum"),
    ]
    y = 100
    for text, amount in lines:
        items += [(20, y, text), (160, y, amount)]
        y += 6
    items += [
        (120, y + 6, f"Zwischensumme {net}"), (120, y + 12, f"zzgl. 19 % MwSt. {vat}"), (120, y + 18, f"Rechnungsbetrag {gross}"),
        (20, y + 34, "Bitte überweisen Sie den Betrag innerhalb von 14 Tagen."),
        (20, 265, f"Beispiel Gartenbau GmbH · Grünweg 3 · 70012 Beispielstadt · USt-ID DE012345612 · IBAN {spaced(IBAN_A)}"),
    ]
    return pdf([items], size=9)


def invoice_english(title: str, number: str, net: str, vat_label: str, vat: str, total: str, issuer: str = "Example Design Studio Ltd",
                    recipient: list[str] = MAX, note: str = "") -> bytes:
    items = [
        (20, 20, issuer), (20, 26, "5 Example Road, London"),
        (120, 20, title.upper()),
        (120, 30, f"Invoice # {number}"), (120, 36, "Issue date: 2026-10-14"), (120, 42, "Due: 2026-11-13"),
        (20, 50, "Bill to:"), (20, 56, recipient[0]), (20, 62, recipient[1]), (20, 68, recipient[2]),
        (20, 90, "Brand workshop, 2 days"), (160, 90, net),
        (120, 110, f"Subtotal: {net}"), (120, 116, f"{vat_label}: {vat}"), (120, 122, f"Amount due: {total}"),
        (20, 140, note),
        (20, 265, f"VAT reg. no. GB000000013 · IBAN {spaced(iban('GB', 'TEST00000080001300'))}"),
    ]
    return pdf([items])


def invoice_classic(issuer: str, address: str, vat_id: str, number: str, date: str, body: list[str], account: str) -> bytes:
    """The fixture style layout, used where only the content should differ."""
    items = [(20, 20, issuer), (20, 26, address), (20, 32, f"USt-IdNr. {vat_id}"),
             (20, 45, MAX[0]), (20, 51, MAX[1]), (20, 57, MAX[2]), (20, 63, "DE"),
             (20, 80, "Rechnung"), (20, 88, f"Nummer: {number}"), (20, 94, f"Datum: {date}")]
    y = 110
    for line in body:
        items.append((20, y, line))
        y += 6
    items.append((20, y + 6, f"Bankverbindung: IBAN {spaced(account)}"))
    return pdf([items])


def two_invoices_one_pdf() -> bytes:
    pages = []
    for number, amount, vat, gross in (("BM-7781", "400,00 EUR", "76,00 EUR", "476,00 EUR"),
                                       ("BM-7782", "150,00 EUR", "28,50 EUR", "178,50 EUR")):
        pages.append([
            (20, 20, "Beispiel Mietgeräte GmbH"), (20, 26, "Hallenstraße 2, 70014 Beispielstadt, DE"), (20, 32, "USt-IdNr. DE012345614"),
            (20, 45, MAX[0]), (20, 51, MAX[1]), (20, 57, MAX[2]), (20, 63, "DE"),
            (20, 80, "Rechnung"), (20, 88, f"Nummer: {number}"), (20, 94, "Datum: 14.10.2026"),
            (20, 110, f"Nettobetrag {amount}"), (20, 116, f"USt 19 % {vat}"), (20, 122, f"Gesamtbetrag {gross}"),
            (20, 128, "Zahlbar bis 28.10.2026 ohne Abzug."),
            (20, 140, f"Bankverbindung: IBAN {spaced(IBAN_C)}"),
        ])
    return pdf(pages)


def letter(sender: list[str], title: str, lines: list[str]) -> bytes:
    items = [(20, 20, sender[0]), (20, 26, sender[1]), (20, 45, MAX[0]), (20, 51, MAX[1]), (20, 57, MAX[2]), (20, 80, title)]
    y = 92
    for line in lines:
        items.append((20, y, line))
        y += 6
    return pdf([items])


# ---------------------------------------------------------------- emails

def build() -> list[dict]:
    gartenbau = invoice_modern("2026-4471", "14.10.2026", [("Rasenpflege Oktober", "180,00 €"), ("Heckenschnitt", "95,00 €")],
                               "275,00 €", "52,25 €", "327,25 €")
    two_rates = invoice_classic("Beispiel Kantine GmbH", "Speiseweg 1, 70015 Beispielstadt, DE", "DE012345615", "K-2026-118", "15.10.2026",
                                ["Mittagessen Team (7 %) 400,00 EUR", "Getränke (19 %) 100,00 EUR",
                                 "Nettobetrag 500,00 EUR", "USt 7 % 28,00 EUR", "USt 19 % 19,00 EUR", "Gesamtbetrag 547,00 EUR",
                                 "Zahlbar bis 29.10.2026 ohne Abzug."], IBAN_B)
    small = invoice_classic("Beispiel Schlüsseldienst", "Torweg 8, 70016 Beispielstadt", "DE012345616", "S-0915", "16.10.2026",
                            ["Kleinbetragsrechnung", "Zylinderschloss getauscht 119,00 EUR", "Gesamtbetrag 119,00 EUR inkl. 19 % MwSt."], IBAN_B)
    scan_source = invoice_classic("Beispiel Hausmeister GmbH", "Kellerweg 4, 70017 Beispielstadt, DE", "DE012345617", "HM-2026-301", "13.10.2026",
                                  ["Nettobetrag 240,00 EUR", "USt 19 % 45,60 EUR", "Gesamtbetrag 285,60 EUR", "Zahlbar bis 27.10.2026 ohne Abzug."], IBAN_A)
    mahnung = letter(["Beispiel Gartenbau GmbH", "Grünweg 3, 70012 Beispielstadt"], "Mahnung",
                     ["Unsere Rechnung 2026-4399 vom 01.09.2026 über 327,25 EUR ist noch nicht bezahlt.",
                      "Bitte zahlen Sie bis zum 25.10.2026."])
    second_reminder = letter(["Example Design Studio Ltd", "5 Example Road, London"], "Second payment reminder",
                             ["Invoice ED-2026-077 of 2026-08-30 for GBP 1,200.00 is still unpaid.", "Please pay within 7 days."])
    order = letter(["Beispiel Gartenbau GmbH", "Grünweg 3, 70012 Beispielstadt"], "Bestellbestätigung",
                   ["Wir bestätigen Ihre Bestellung von 12 Pflanzkübeln zu je 45,00 EUR.", "Die Rechnung folgt nach Lieferung."])
    credit_memo = invoice_english("Credit memo", "ED-CM-0012", "GBP 300.00", "VAT 0%", "GBP 0.00", "GBP 300.00",
                                  note="Credit for the cancelled second workshop day. Reverse charge.")
    outgoing_en = invoice_english("Invoice", "MA-2026-0110", "EUR 1,500.00", "VAT 0%", "EUR 0.00", "EUR 1,500.00",
                                  issuer="Max ASP GmbH", recipient=["Example Retail BV", "Voorbeeldstraat 1", "Voorbeeldstad, NL"],
                                  note="Reverse charge: VAT to be accounted for by the recipient.")

    return [
        dict(id="H01", sender=("Beispiel Gartenbau GmbH", "buchhaltung@beispiel-gartenbau.example"), subject="Ihre Rechnung 2026-4471",
             body="Guten Tag,\n\nanbei unsere Rechnung für die Gartenpflege im Oktober.\n\nViele Grüße\nBeispiel Gartenbau",
             attachments=[("Rechnung_2026-4471.pdf", gartenbau, "application/pdf")], route="invoice_processing", cases=1,
             note="Different layout: recipient first, issuer and VAT ID only in the footer, euro sign amounts"),
        dict(id="H02", sender=("Example Design Studio Ltd", "accounts@example-design.example"), subject="Invoice ED-2026-091",
             body="Hi,\n\nplease find our invoice attached.\n\nThanks\nExample Design Studio",
             attachments=[("ED-2026-091.pdf", invoice_english("Tax invoice", "ED-2026-091", "GBP 1,800.00", "VAT 0% (reverse charge)", "GBP 0.00", "GBP 1,800.00"), "application/pdf")],
             route="invoice_processing", cases=1, note="English layout: Invoice #, Subtotal, Amount due, VAT in the footer"),
        dict(id="H03", sender=("Beispiel Kantine GmbH", "rechnung@beispiel-kantine.example"), subject="Rechnung K-2026-118",
             body="Sehr geehrte Damen und Herren,\n\nanbei die Rechnung für das Teamessen.\n\nBeispiel Kantine",
             attachments=[("K-2026-118.pdf", two_rates, "application/pdf")], route="invoice_processing", cases=1,
             note="Two VAT rates on one invoice"),
        dict(id="H04", sender=("Beispiel Mietgeräte GmbH", "faktura@beispiel-mietgeraete.example"), subject="Rechnungen Oktober",
             body="Hallo,\n\nanbei beide Rechnungen in einer Datei.\n\nGruß",
             attachments=[("Rechnungen_Oktober.pdf", two_invoices_one_pdf(), "application/pdf")], route="invoice_processing", cases=2,
             note="Two invoices on two pages of one PDF (IN-08)"),
        dict(id="H05", sender=("Beispiel Hausmeister GmbH", "office@beispiel-hausmeister.example"), subject="Rechnung HM-2026-301",
             body="Anbei die Rechnung.",
             attachments=[("scan.pdf", scanned(scan_source, as_pdf=True), "application/pdf")], route="invoice_processing", cases=1,
             note="Scanned PDF without a text layer (IN-03)"),
        dict(id="H06", sender=("Example Design Studio Ltd", "accounts@example-design.example"), subject="Second reminder ED-2026-077",
             body="Hi,\n\nsee the attached reminder.\n\nThanks",
             attachments=[("reminder.pdf", second_reminder, "application/pdf")], route="forward_reminder", cases=0,
             note="English reminder titled 'Second payment reminder'"),
        dict(id="H07", sender=("Beispiel Software-Club", "news@beispiel-club.invalid"), subject="Sparen Sie bei jeder Rechnung!",
             body="Hallo,\n\nmit unserer App schreiben Sie jede Rechnung in 30 Sekunden. Jetzt 3 Monate gratis testen.\n\nZum Abmelden hier klicken.",
             attachments=[], headers={"List-Unsubscribe": "<mailto:off@beispiel-club.invalid>"}, route="deletion_folder", cases=0,
             note="Newsletter from an unknown sender that uses invoice words"),
        dict(id="H08", sender=("Beispiel Mobilfunk", "service@beispiel-mobilfunk.example"), subject="Ihre Rechnung Oktober ist online",
             body="Guten Tag,\n\nIhre Rechnung für Oktober über 49,90 EUR steht im Kundenportal bereit:\nhttps://portal.beispiel-mobilfunk.example/rechnungen\n\nIhr Beispiel Mobilfunk",
             attachments=[], route="manual_review", cases=0,
             note="Invoice only as a link. A person has to download it, ARGUS must not delete it"),
        dict(id="H09", sender=("Max ASP Rechnungsagent", "rechnungen@maxasp.example"), subject="Outgoing invoice MA-2026-0110",
             body="Automatisch erzeugt vom Rechnungsagenten.", attachments=[("Invoice_MA-2026-0110.pdf", outgoing_en, "application/pdf")],
             route="outgoing_booking", cases=1, note="Outgoing invoice in an English layout"),
        dict(id="H10", sender=("Example Design Studio Ltd", "accounts@example-design.example"), subject="Credit ED-CM-0012",
             body="Hi,\n\nas discussed, here is the credit for the cancelled day.\n\nThanks",
             attachments=[("ED-CM-0012.pdf", credit_memo, "application/pdf")], route="invoice_processing", cases=1,
             note="Credit note titled 'Credit memo'"),
        dict(id="H11", sender=("Beispiel Gartenbau GmbH", "buchhaltung@beispiel-gartenbau.example"), subject="Ihre Bestellung",
             body="Guten Tag,\n\nanbei die Bestellbestätigung.\n\nViele Grüße",
             attachments=[("Bestellbestaetigung.pdf", order, "application/pdf")], route="deletion_folder", cases=0,
             note="Order confirmation with a different title word"),
        dict(id="H12", sender=("Rechnungsstelle", "billing@rechnung-online.invalid"), subject="Ihre Rechnung Nr. 4471 ist fällig",
             body="Sehr geehrter Kunde,\n\nIhre Rechnung ist fällig. Klicken Sie hier, um sie anzusehen und zu bezahlen: http://rechnung-online.invalid/r4471",
             attachments=[], route="deletion_folder", cases=0, note="Phishing that pretends to be an invoice"),
        dict(id="H13", sender=("Beispiel Gartenbau GmbH", "buchhaltung@beispiel-gartenbau.example"), subject="",
             body="", attachments=[("IMG_2041.jpg", scanned(mahnung, as_pdf=False), "image/jpeg")], route="forward_reminder", cases=0,
             note="Photo of a dunning letter, no subject, no body"),
        dict(id="H14", sender=("Beispiel Schlüsseldienst", "info@beispiel-schluesseldienst.example"), subject="Rechnung S-0915",
             body="Anbei die Rechnung für den Einsatz heute.", attachments=[("S-0915.pdf", small, "application/pdf")],
             route="invoice_processing", cases=1, note="Kleinbetragsrechnung: gross only, no net amount"),
        dict(id="H15", sender=("Petra Probe", "petra.probe@beispiel-handel.example"), subject="Out of Office: Re: Invoice MA-2026-0102",
             body="I am out of the office until 26 October with limited access to email.", attachments=[],
             route="deletion_folder", cases=0, note="English out of office reply"),
        dict(id="H16", sender=("Beispiel Gartenbau GmbH", "buchhaltung@beispiel-gartenbau.example"), subject="Mahnung und neue Rechnung",
             body="Guten Tag,\n\nanbei unsere Mahnung und die neue Rechnung für Oktober.\n\nViele Grüße",
             attachments=[("Mahnung.pdf", mahnung, "application/pdf"), ("Rechnung_2026-4471.pdf", gartenbau, "application/pdf")],
             route="manual_review", cases=1, note="Reminder and invoice in one email"),
    ]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email_id", "file", "expected_route", "invoice_count", "note"])
        for item in build():
            msg = EmailMessage()
            msg["From"] = f"{item['sender'][0]} <{item['sender'][1]}>"
            msg["To"] = "accounts@maxasp.example"
            msg["Subject"] = item["subject"]
            msg["Date"] = "Fri, 16 Oct 2026 09:00:00 +0200"
            msg["Message-ID"] = f"<{item['id'].lower()}@holdout.argus.example>"
            for key, value in item.get("headers", {}).items():
                msg[key] = value
            msg.set_content(item["body"] or "\n")
            for name, data, ctype in item["attachments"]:
                maintype, subtype = ctype.split("/")
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
            file = f"{item['id']}.eml"
            (OUT / file).write_bytes(bytes(msg))
            writer.writerow([item["id"], file, item["route"], item["cases"], item["note"]])


if __name__ == "__main__":
    main()

"""Turn an email into documents ARGUS can reason about.

Covers: IN-02, IN-03, IN-08, PR-01, NF-02

For every email this module:
1. reads the body and every attachment,
2. takes e-invoice XML first (separate XML file or embedded in a ZUGFeRD PDF),
3. reads the text layer of PDFs and runs OCR on images and on PDFs without text,
4. decides what kind of document each one is (invoice, reminder, quote and so on),
5. extracts the PR-01 fields from invoices and credit notes.

The text extraction here uses local tools (pdfplumber, Tesseract) so the
prototype runs anywhere. The ADR explains what replaces them in production.
"""

from __future__ import annotations

import email
import io
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pdfplumber
import pypdf

import einvoice

# Below this many readable characters a scan counts as unreadable (IN-03, IN-09).
MIN_READABLE_CHARS = 40
# Below this mean Tesseract word confidence (0 to 100) a scan counts as unreadable.
MIN_OCR_CONFIDENCE = 60.0

INVOICE_KINDS = ("invoice", "credit_note", "cancellation")


@dataclass
class Document:
    source: str  # attachment file name, or "body"
    media: str  # pdf, image, xml, body
    text: str = ""
    kind: str = "unknown"
    einvoice: dict | None = None
    ocr_confidence: float | None = None
    fields: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def is_invoice(self) -> bool:
        return self.kind in INVOICE_KINDS

    @property
    def readable(self) -> bool:
        if self.einvoice:
            return True
        if len(re.sub(r"\s", "", self.text)) < MIN_READABLE_CHARS:
            return False
        return self.ocr_confidence is None or self.ocr_confidence >= MIN_OCR_CONFIDENCE


@dataclass
class ParsedEmail:
    path: Path
    sender_name: str
    sender_address: str
    subject: str
    body: str
    headers: dict
    documents: list[Document]
    attachment_count: int


# ---------------------------------------------------------------- email reading


def read_email(path: Path) -> ParsedEmail:
    msg: EmailMessage = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    sender = email.utils.parseaddr(str(msg.get("From", "")))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    if body_part is not None and body_part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)

    documents: list[Document] = []
    attachments = list(msg.iter_attachments())
    for part in attachments:
        documents.extend(read_attachment(part.get_filename() or "attachment", part.get_content_type(), part.get_payload(decode=True)))

    body_doc = Document(source="body", media="body", text=body)
    body_doc.kind = document_kind(body)
    if body_doc.is_invoice:
        documents.append(body_doc)

    headers = {k.lower(): str(v) for k, v in msg.items()}
    return ParsedEmail(
        path=path,
        sender_name=sender[0],
        sender_address=sender[1].lower(),
        subject=str(msg.get("Subject", "")),
        body=body,
        headers=headers,
        documents=finish_documents(documents),
        attachment_count=len(attachments),
    )


def read_attachment(name: str, content_type: str, data: bytes) -> list[Document]:
    lower = name.lower()
    if lower.endswith(".xml") or content_type in ("application/xml", "text/xml"):
        doc = Document(source=name, media="xml")
        if einvoice.detect_syntax(data):
            doc.einvoice = einvoice.parse(data)
        else:
            doc.text = data.decode("utf-8", "replace")
        return [doc]
    if lower.endswith(".pdf") or content_type == "application/pdf":
        return [read_pdf(name, data)]
    if content_type.startswith("image/") or lower.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff")):
        text, confidence = ocr_image(data)
        return [Document(source=name, media="image", text=text, ocr_confidence=confidence)]
    return []


def read_pdf(name: str, data: bytes) -> Document:
    doc = Document(source=name, media="pdf")
    reader = pypdf.PdfReader(io.BytesIO(data))
    for attached_name, contents in (reader.attachments or {}).items():
        if attached_name.lower() in einvoice.EMBEDDED_XML_NAMES and contents:
            xml = contents[0]
            if einvoice.detect_syntax(xml):
                doc.einvoice = einvoice.parse(xml)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        doc.text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if len(doc.text.strip()) < MIN_READABLE_CHARS:
            # A scanned PDF has no text layer: render each page and OCR it.
            texts, confidences = [], []
            for page in pdf.pages:
                buf = io.BytesIO()
                page.to_image(resolution=300).original.save(buf, format="PNG")
                text, confidence = ocr_image(buf.getvalue())
                texts.append(text)
                confidences.append(confidence)
            doc.text = "\n".join(texts)
            doc.ocr_confidence = min(confidences) if confidences else 0.0
    return doc


def ocr_image(data: bytes) -> tuple[str, float]:
    """OCR with Tesseract (German and English). Returns text and mean word confidence."""
    tsv = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "deu+eng", "tsv"],
        input=data,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    lines: dict[tuple, list[str]] = {}
    confidences = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        lines.setdefault((cols[2], cols[3], cols[4]), []).append(cols[11])
        confidences.append(float(cols[10]))
    text = "\n".join(" ".join(words) for words in lines.values())
    return text, (sum(confidences) / len(confidences)) if confidences else 0.0


# ---------------------------------------------------------------- document kind

# Title words, checked against short lines near the top of the document. Order matters:
# the more specific title wins ("Stornorechnung" before "Rechnung").
TITLE_PATTERNS = [
    ("proforma", r"pro[\s-]?forma(\s+(invoice|rechnung))?"),
    ("cancellation", r"stornorechnung|storno|cancellation invoice"),
    ("credit_note", r"gutschrift|rechnungskorrektur|credit note"),
    ("reminder", r"(\d\.\s*)?mahnung|letzte mahnung|zahlungserinnerung|zahlungsaufforderung|payment reminder|reminder|final notice"),
    ("quote", r"angebot(\s+nr\.?)?|quotation|quote"),
    ("advertising", r"[\w-]*angebote\s+\d{4}|prospekt|katalog|catalogue"),
    ("order_confirmation", r"auftragsbestätigung|auftragsbestaetigung|order confirmation"),
    ("delivery_note", r"lieferschein|delivery note"),
    ("invoice", r"rechnung|invoice|tax invoice"),
]
_TITLE_RES = [(kind, re.compile(rf"^(?:{pattern})\b", re.IGNORECASE)) for kind, pattern in TITLE_PATTERNS]


def document_kind(text: str) -> str:
    """Find the document title: a short line that starts with a known document word."""
    for line in text.splitlines()[:40]:
        line = line.strip()
        if not line or len(line.split()) > 4:
            continue
        for kind, regex in _TITLE_RES:
            if regex.match(line):
                return kind
    return "unknown"


def finish_documents(documents: list[Document]) -> list[Document]:
    """Decide kinds, extract fields and merge a PDF with the e-invoice of the same invoice."""
    for doc in documents:
        if doc.einvoice:
            doc.kind = doc.einvoice["kind"]
            doc.fields = normalise_einvoice(doc.einvoice)
        else:
            if doc.kind == "unknown":
                doc.kind = document_kind(doc.text)
            if doc.is_invoice or doc.kind == "proforma":
                doc.fields = extract_fields(doc.text)
        if doc.is_invoice:
            doc.problems = check_fields(doc.fields)
    return merge_same_invoice(documents)


def merge_same_invoice(documents: list[Document]) -> list[Document]:
    """An XRechnung plus its PDF copy is one invoice, not two (IN-08, E21)."""
    by_number: dict[str, Document] = {}
    result: list[Document] = []
    for doc in sorted(documents, key=lambda d: d.einvoice is None):
        number = doc.fields.get("invoice_number") if doc.is_invoice else None
        if number and number in by_number:
            continue  # the structured copy is already kept
        if number:
            by_number[number] = doc
        result.append(doc)
    return result


# ---------------------------------------------------------------- field extraction

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTHS.update({"mär": 3, "mai": 5, "okt": 10, "dez": 12})

DATE = r"(\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+[A-Za-zä]{3,9}\.?\s+\d{4}|\d{4}-\d{2}-\d{2})"
AMOUNT = r"(-?[\d.,]+)\s*(EUR|GBP|USD|CHF|€|\$|£)"


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value):
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    if m := re.fullmatch(r"(\d{1,2})\s+([A-Za-zä]{3,9})\.?\s+(\d{4})", value):
        month = MONTHS.get(m[2][:3].lower())
        return date(int(m[3]), month, int(m[1])).isoformat() if month else None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def parse_amount(value: str) -> str | None:
    """Read 1.234,56 (German) and 1,234.56 (English) into "1234.56"."""
    value = value.strip()
    if re.search(r",\d{2}$", value):
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", "")
    try:
        return str(Decimal(value).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def _find(pattern: str, text: str, flags=re.IGNORECASE | re.MULTILINE):
    return re.search(pattern, text, flags)


CURRENCY_SIGNS = {"€": "EUR", "$": "USD", "£": "GBP"}


def extract_fields(text: str) -> dict:
    """Pull PR-01 fields out of invoice text (German and English layouts)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields: dict = {}

    fields["issuer_name"] = _issuer(lines)
    fields["recipient_name"] = _recipient(lines)

    if m := _find(r"(?:USt-?IdNr\.?|USt-ID|VAT ID|VAT No\.?)[:\s]+([A-Z]{2}[ A-Z0-9]{5,14})\b", text):
        fields["vendor_vat_id"] = clean_vat_id(m[1])
    if m := _find(r"^(?:Rechnungsnummer|Rechnungs-Nr\.?|Nummer|Number|Invoice No\.?|Nr\.)[:\s]+(\S+)", text):
        fields["invoice_number"] = m[1]
    if m := _find(rf"^(?:Rechnungsdatum|Datum|Date|Invoice date)[:\s]+{DATE}", text):
        fields["invoice_date"] = parse_date(m[1])
    if m := _find(rf"^(?:Leistungszeitraum|Service period|Leistungsdatum)[:\s]+{DATE}\s*(?:-|bis|to)\s*{DATE}", text):
        fields["service_period"] = [parse_date(m[1]), parse_date(m[2])]
    if m := _find(rf"^(?:Nettobetrag|Summe netto|Net amount|Subtotal)[:\s]+{AMOUNT}", text):
        fields["net_amount"] = parse_amount(m[1])
        fields["currency"] = CURRENCY_SIGNS.get(m[2], m[2])
    if m := _find(rf"^(?:USt|MwSt|VAT)\.?\s*(\d{{1,2}}(?:[.,]\d+)?)\s*%[:\s]+{AMOUNT}", text):
        fields["vat_rate"] = m[1].replace(",", ".").rstrip("0").rstrip(".") if "." in m[1] or "," in m[1] else m[1]
        fields["vat_amount"] = parse_amount(m[2])
    if m := _find(rf"^(?:Gesamtbetrag|Rechnungsbetrag|Summe brutto|Total|Amount due)[:\s]+{AMOUNT}", text):
        fields["gross_amount"] = parse_amount(m[1])
        fields.setdefault("currency", CURRENCY_SIGNS.get(m[2], m[2]))
    if m := _find(rf"(?:Zahlbar bis|zahlbar bis zum|Payment due by|Due date)[:\s]+{DATE}", text):
        fields["due_date"] = parse_date(m[1])
    elif m := _find(rf"am {DATE} per SEPA-Lastschrift", text):
        fields["due_date"] = parse_date(m[1])
    if m := _find(rf"Zahlung bis (?:zum )?{DATE} gewähren wir ([\d,]+)\s*% Skonto", text):
        fields["early_payment_discount"] = {"percent": parse_amount(m[2]), "until": parse_date(m[1])}
    elif m := _find(rf"([\d.]+)\s*% (?:early payment )?discount if paid by {DATE}", text):
        fields["early_payment_discount"] = {"percent": parse_amount(m[1]), "until": parse_date(m[2])}
    else:
        fields["early_payment_discount"] = None
    if m := _find(r"IBAN[:\s]+([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)", text):
        fields["iban_on_document"] = re.sub(r"\s", "", m[1])
    fields["direct_debit"] = bool(_find(r"Lastschrift|direct debit", text))
    if m := _find(r"^(?:Bezug: Rechnung|Refers to invoice|Zu Rechnung)[:\s]+(\S+)", text):
        fields["refers_to"] = m[1]
    else:
        fields["refers_to"] = None
    return fields


def _vat_line(lines: list[str]) -> int | None:
    for i, line in enumerate(lines[:20]):
        if re.match(r"(USt-?IdNr|VAT ID|VAT No)", line, re.IGNORECASE):
            return i
    return None


def _issuer(lines: list[str]) -> str | None:
    """The issuer's name sits above its address line, which sits above its VAT ID."""
    i = _vat_line(lines)
    if i is not None and i >= 2:
        return lines[i - 2]
    return lines[0] if lines else None


def _recipient(lines: list[str]) -> str | None:
    """The recipient block follows the issuer's VAT ID line in the address header."""
    i = _vat_line(lines)
    if i is not None and i + 1 < len(lines):
        return lines[i + 1]
    return lines[2] if len(lines) > 2 else None


def clean_vat_id(raw: str) -> str:
    """Remove spaces and fix the usual OCR slip of letter O for digit 0 after the country code."""
    raw = re.sub(r"\s", "", raw.upper())
    return raw[:2] + raw[2:].replace("O", "0") if raw[:2] in ("DE", "AT") else raw


def normalise_einvoice(data: dict) -> dict:
    fields = {k: v for k, v in data.items() if k not in ("syntax", "kind", "type_code", "payment_terms")}
    discount = data.get("early_payment_discount")
    if discount and "days" in discount and data.get("invoice_date"):
        until = date.fromisoformat(data["invoice_date"]) + timedelta(days=discount["days"])
        fields["early_payment_discount"] = {"percent": discount["percent"], "until": until.isoformat()}
    if not fields.get("due_date") and data.get("payment_terms"):
        if m := _find(rf"(?:Zahlbar bis|Payment due by)\s+{DATE}", data["payment_terms"]):
            fields["due_date"] = parse_date(m[1])
    return fields


# ---------------------------------------------------------------- checks

REQUIRED_FIELDS = ("issuer_name", "invoice_number", "invoice_date", "net_amount", "vat_amount", "gross_amount", "currency")


def iban_valid(iban: str | None) -> bool:
    if not iban or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def check_fields(fields: dict) -> list[str]:
    """Plausibility checks. Any problem means a person looks at the case (IN-09, PR-03)."""
    problems = [f"missing {name}" for name in REQUIRED_FIELDS if not fields.get(name)]
    try:
        net, vat, gross = (Decimal(fields[k]) for k in ("net_amount", "vat_amount", "gross_amount"))
        if net + vat != gross:
            problems.append("net plus VAT does not equal gross")
        if fields.get("vat_rate") is not None:
            expected_vat = (net * Decimal(fields["vat_rate"]) / 100).quantize(Decimal("0.01"))
            if abs(expected_vat - vat) > Decimal("0.02"):
                problems.append("VAT amount does not match the VAT rate")
    except (KeyError, TypeError, InvalidOperation):
        pass
    if fields.get("iban_on_document") and not iban_valid(fields["iban_on_document"]):
        problems.append("IBAN check digits are wrong")
    return problems

"""Our own rules on the text of a page: what kind of document it is and which PR-01 fields it
states (ADR 0006 points 2 and 5).

They come from the prototype of the spike (#5). They are not the main reader, because they only
know the layouts they were written for, but they do three jobs: they read the email body, they
fill a field Document Intelligence missed when the page states it plainly, and they check every
value the language model proposes against the words on the page.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from argus.reading.pages import Page

# ------------------------------------------------------------------ document kind

# Title words, checked against short lines near the top of the document. Order matters: the more
# specific title wins ("Stornorechnung" before "Rechnung").
TITLE_PATTERNS = (
    ("proforma", r"pro[\s-]?forma(\s+(invoice|rechnung))?"),
    ("cancellation", r"stornorechnung|storno|cancellation invoice"),
    ("credit_note", r"gutschrift|rechnungskorrektur|credit note"),
    (
        "reminder",
        r"(\d\.\s*)?mahnung|letzte mahnung|zahlungserinnerung|zahlungsaufforderung"
        r"|payment reminder|reminder|final notice",
    ),
    ("quote", r"angebot(\s+nr\.?)?|quotation|quote"),
    ("advertising", r"[\w-]*angebote\s+\d{4}|prospekt|katalog|catalogue"),
    ("order_confirmation", r"auftragsbestätigung|auftragsbestaetigung|order confirmation"),
    ("delivery_note", r"lieferschein|delivery note"),
    ("invoice", r"rechnung|invoice|tax invoice"),
)
_TITLES = tuple((kind, re.compile(rf"^(?:{p})\b", re.IGNORECASE)) for kind, p in TITLE_PATTERNS)
INVOICE_KINDS = frozenset({"invoice", "credit_note", "cancellation"})
CREDIT_KINDS = frozenset({"credit_note", "cancellation"})


def document_kind(text: str) -> str:
    """The document title: a short line near the top that starts with a known document word."""
    for line in text.splitlines()[:40]:
        line = line.strip()
        if not line or len(line.split()) > 4:
            continue
        for kind, regex in _TITLES:
            if regex.match(line):
                return kind
    return "unknown"


# ------------------------------------------------------------------ values

MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
MONTHS.update({"mär": 3, "mai": 5, "okt": 10, "dez": 12})

DATE = r"(\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+[A-Za-zä]{3,9}\.?\s+\d{4}|\d{4}-\d{2}-\d{2})"
AMOUNT = r"(-?\d[\d.,]*)\s*(EUR|GBP|USD|CHF|€|\$|£)"
CURRENCY_SIGNS = {"€": "EUR", "$": "USD", "£": "GBP"}
CURRENCIES = frozenset({"EUR", "GBP", "USD", "CHF"})
IBAN = r"([A-Z]{2}\d{2}(?:[ \t]?[A-Z0-9]{4}){2,7}(?:[ \t]?[A-Z0-9]{1,3})?)"


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value):
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        if m := re.fullmatch(r"(\d{1,2})\s+([A-Za-zä]{3,9})\.?\s+(\d{4})", value):
            month = MONTHS.get(m[2][:3].lower())
            return date(int(m[3]), month, int(m[1])).isoformat() if month else None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
    except ValueError:  # 31.02.2026
        return None
    return None


def parse_amount(value: str) -> str | None:
    """Read ``1.234,56`` (German) and ``1,234.56`` (English) into ``"1234.56"``."""
    value = value.strip()
    if re.search(r",\d{1,2}$", value):
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", "")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return str(amount.quantize(Decimal("0.01")))


def parse_rate(value: str) -> str:
    """``"19"``, ``"7"``, ``"5.5"``: the VAT rate without trailing zeros."""
    value = value.replace(",", ".")
    return value.rstrip("0").rstrip(".") if "." in value else value


def clean_vat_id(raw: str) -> str:
    """Remove spaces and fix the usual OCR slip of letter O for digit 0 after the country code."""
    raw = re.sub(r"\s", "", raw.upper())
    return raw[:2] + raw[2:].replace("O", "0") if raw[:2] in ("DE", "AT") else raw


def compact_iban(raw: str) -> str:
    return re.sub(r"\s", "", raw).upper()


def iban_valid(iban: str | None) -> bool:
    if not iban or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------ fields on a page


@dataclass(frozen=True)
class Found:
    value: Any
    page: int
    words: str  # the text the value was read from, whitespace squashed


_FLAGS = re.IGNORECASE | re.MULTILINE

# One pattern per field: the value groups come after the label. ``[:\s]+`` also crosses a line
# break, because some PDFs put the label and its value on separate lines.
LABELS = {
    "vendor_vat_id": r"(?:USt-?IdNr\.?|USt-ID|VAT ID|VAT No\.?)[: \t]+([A-Z]{2}[ A-Z0-9]{5,14})\b",
    "invoice_number": (
        r"^(?:Rechnungsnummer|Rechnungs-Nr\.?|Nummer|Number|Invoice No\.?|Nr\.)[:\s]+(\S+)"
    ),
    "invoice_date": rf"^(?:Rechnungsdatum|Datum|Date|Invoice date)[:\s]+{DATE}",
    "service_period": (
        rf"^(?:Leistungszeitraum|Service period|Leistungsdatum)[:\s]+{DATE}\s*(?:-|bis|to)\s*{DATE}"
    ),
    "net_amount": rf"^(?:Nettobetrag|Summe netto|Net amount|Subtotal)[:\s]+{AMOUNT}",
    "vat": rf"^(?:USt|MwSt|VAT)\.?\s*(\d{{1,2}}(?:[.,]\d+)?)\s*%[:\s]+{AMOUNT}",
    "gross_amount": (
        rf"^(?:Gesamtbetrag|Rechnungsbetrag|Summe brutto|Total|Amount due)[:\s]+{AMOUNT}"
    ),
    "due_date": rf"(?:Zahlbar bis|zahlbar bis zum|Payment due by|Due date)[:\s]+{DATE}",
    "debit_date": rf"am {DATE} per SEPA-Lastschrift",
    "discount_de": rf"Zahlung bis (?:zum )?{DATE} gewähren wir ([\d,]+)\s*% Skonto",
    "discount_en": rf"([\d.]+)\s*% (?:early payment )?discount if paid by {DATE}",
    "iban": rf"IBAN[:\s]+{IBAN}",
}
_LABELS = {name: re.compile(pattern, _FLAGS) for name, pattern in LABELS.items()}
VAT_LINE = re.compile(r"(USt-?IdNr|USt-ID|VAT ID|VAT No)", re.IGNORECASE)


def _first(name: str, pages: tuple[Page, ...]) -> tuple[re.Match[str], int] | None:
    for page in pages:
        if m := _LABELS[name].search(page.text):
            return m, page.number
    return None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def find_fields(pages: tuple[Page, ...]) -> dict[str, Found]:
    """Every PR-01 field our rules can read, with its page and words. A field the rules cannot
    read is left out; nothing is guessed."""
    found: dict[str, Found] = {}

    def put(name: str, value: Any, m: re.Match[str], page: int) -> None:
        if value is not None:
            found[name] = Found(value, page, squash(m[0]))

    for page in pages:
        lines = _lines(page.text)
        vat_line = next((i for i, line in enumerate(lines[:20]) if VAT_LINE.match(line)), None)
        # The issuer's name sits above its address line, which sits above its VAT ID. The
        # recipient block follows that VAT ID.
        if vat_line is not None and vat_line >= 2:
            found["issuer_name"] = Found(lines[vat_line - 2], page.number, lines[vat_line - 2])
            if vat_line + 1 < len(lines):
                found["recipient_name"] = Found(
                    lines[vat_line + 1], page.number, lines[vat_line + 1]
                )
            break

    if hit := _first("vendor_vat_id", pages):
        put("vendor_vat_id", clean_vat_id(hit[0][1]), *hit)
    if hit := _first("invoice_number", pages):
        put("invoice_number", hit[0][1], *hit)
    if hit := _first("invoice_date", pages):
        put("invoice_date", parse_date(hit[0][1]), *hit)
    if hit := _first("service_period", pages):
        start, end = parse_date(hit[0][1]), parse_date(hit[0][2])
        put("service_period", [start, end] if start and end else None, *hit)
    if hit := _first("net_amount", pages):
        put("net_amount", parse_amount(hit[0][1]), *hit)
        put("currency", CURRENCY_SIGNS.get(hit[0][2], hit[0][2]), *hit)
    if hit := _first("vat", pages):
        put("vat_rate", parse_rate(hit[0][1]), *hit)
        put("vat_amount", parse_amount(hit[0][2]), *hit)
    if hit := _first("gross_amount", pages):
        put("gross_amount", parse_amount(hit[0][1]), *hit)
        if "currency" not in found:
            put("currency", CURRENCY_SIGNS.get(hit[0][2], hit[0][2]), *hit)
    if hit := _first("due_date", pages) or _first("debit_date", pages):
        put("due_date", parse_date(hit[0][1]), *hit)
    if hit := _first("discount_de", pages):
        m = hit[0]
        discount = {"percent": parse_amount(m[2]), "until": parse_date(m[1])}
        put("early_payment_discount", discount if all(discount.values()) else None, *hit)
    elif hit := _first("discount_en", pages):
        m = hit[0]
        discount = {"percent": parse_amount(m[1]), "until": parse_date(m[2])}
        put("early_payment_discount", discount if all(discount.values()) else None, *hit)
    if hit := _first("iban", pages):
        put("iban_on_document", compact_iban(hit[0][1]), *hit)
    return found


def invoice_numbers(pages: tuple[Page, ...]) -> list[str]:
    """Every invoice number stated in the document, in page order. More than one distinct number
    means several invoices in one file (IN-08), which the spike once merged silently (H04)."""
    numbers: list[str] = []
    for page in pages:
        for m in _LABELS["invoice_number"].finditer(page.text):
            if m[1] not in numbers:
                numbers.append(m[1])
    return numbers


def vat_rates(pages: tuple[Page, ...]) -> list[str]:
    """Every VAT rate with an amount line. More than one means the invoice has several rates."""
    rates: list[str] = []
    for page in pages:
        for m in _LABELS["vat"].finditer(page.text):
            if parse_rate(m[1]) not in rates:
                rates.append(parse_rate(m[1]))
    return rates


# ------------------------------------------------------------------ checking a proposed value


def confirm(name: str, value: str, words: str) -> Any:
    """Read ``name`` out of ``words`` with our own parsers and return the value only when it is
    the one proposed. Returns None when the words do not say that value (ADR 0006 point 3)."""
    text = squash(words)
    candidates: list[Any]
    if name in ("invoice_date", "due_date"):
        candidates = [parse_date(m[0]) for m in re.finditer(DATE, text)]
        wanted: Any = parse_date(value)
    elif name in ("net_amount", "vat_amount", "gross_amount"):
        candidates = [parse_amount(m[0]) for m in re.finditer(r"-?\d[\d.,]*\d|\d", text)]
        wanted = parse_amount(value)
    elif name == "vat_rate":
        candidates = [parse_rate(m[1]) for m in re.finditer(r"(\d{1,2}(?:[.,]\d+)?)\s*%", text)]
        wanted = parse_rate(value.strip().rstrip("%").strip())
    elif name == "iban_on_document":
        candidates = [compact_iban(m[1]) for m in re.finditer(IBAN, text.upper())]
        wanted = compact_iban(value)
        if not iban_valid(wanted):
            return None
    elif name == "vendor_vat_id":
        candidates = [clean_vat_id(m[0]) for m in re.finditer(r"[A-Z]{2}[ A-Z0-9]{5,14}", text)]
        wanted = clean_vat_id(value)
    elif name == "currency":
        signs = [CURRENCY_SIGNS[c] for c in text if c in CURRENCY_SIGNS]
        candidates = [*re.findall(r"\b[A-Z]{3}\b", text), *signs]
        wanted = value.strip().upper() if value.strip().upper() in CURRENCIES else None
    elif name in ("invoice_number", "issuer_name", "recipient_name"):
        wanted = squash(value)
        candidates = [wanted] if wanted and wanted in text else []
    else:
        return None  # structured fields (service period, discount) are not taken from the model
    return wanted if wanted is not None and wanted in candidates else None


def on_page(words: str, page: Page) -> bool:
    """The quoted words really stand on that page."""
    quoted = squash(words)
    return bool(quoted) and quoted in squash(page.text)

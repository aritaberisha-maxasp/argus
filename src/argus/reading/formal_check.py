"""Check an incoming invoice for the formal items §14 UStG asks for (#32, PR-03).

An invoice that misses a required item cannot be used for input VAT, so ARGUS names every item
it cannot find and a person asks the vendor for a corrected invoice.

Which list applies:

- **§14 Abs. 4 UStG**, the full list: Max ASP as the recipient, the vendor's name, the invoice
  number, the invoice date, the vendor's VAT ID or tax number, the date or period of the service,
  the net amount, the VAT rate and the VAT amount.
- **§33 UStDV**, the small invoice list, when the gross amount is at most EUR 250.00: the
  vendor's name, the invoice date, the gross amount and the VAT rate. The recipient, the number,
  the VAT ID and the separate amounts may be missing. Only an amount in euros counts as small;
  for another currency ARGUS would need an exchange rate, so the full list applies.

Where no VAT is charged (reverse charge, a tax exemption, a small business under §19 UStG), the
invoice must say why instead of naming a rate and an amount (§14 Abs. 4 Nr. 8 UStG, §14a Abs. 5
UStG). A VAT rate of 0 without such a note fails the check.

What is out of scope here and why:

- The addresses of both parties and the quantity and kind of goods or services: ARGUS does not
  read them yet (PR-01 has no such fields), so a person checks them when approving.
- Whether the invoice number is sequential: one invoice cannot show that, and the vendor's
  numbering is the vendor's affair.
- Credit notes, cancellations and pro forma invoices: only invoices are checked. Invoices issued
  by Max ASP itself are not checked here either.

An invoice is a document of kind ``invoice``, or a document whose title ARGUS did not recognise
(kind ``unknown``, for example "Rechnung Nr. 2026-0012 vom 01.10.2026") that states an invoice
number or a gross amount. An unknown attachment without either, such as terms and conditions or a
delivery note, is left alone, so it does not fill the queue (PR #104 review).

The reason for charging no VAT is searched in the whole text, so a stray "steuerfrei" anywhere on
the page lets a 0 % invoice pass. E-invoices take it from the XML instead. The 0 % rate of
§12 Abs. 3 UStG (solar systems) is a real rate that needs no note; it fails here as "reason no VAT
is charged" and a person confirms it. Both are rare for Max ASP; tune them if the shadow run (#72)
shows otherwise.

A field ARGUS read with a flag (low confidence, readers disagree, found by the model) counts as
present: the flag already sends it to a person. A field that could not be read counts as missing,
because ARGUS cannot tell an item the vendor left out from one it could not read. The message
says "missing or not readable" for that reason.

The check reads only; :func:`argus.einvoice.queue_failed_checks` puts the failed checks of an
email in the exception queue together with the e-invoice checks, because an email has at most
one open exception with the reason ``invoice_check_failed``.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from argus.reading.fields import Flag, InvoiceReading
from argus.sorting.checks import normalise

OWN_COMPANY = "Max ASP GmbH"
SMALL_INVOICE_LIMIT = Decimal("250.00")  # §33 UStDV: the gross amount does not exceed EUR 250

FULL = "ustg14"  # §14 Abs. 4 UStG
SMALL = "ustdv33"  # §33 UStDV
RULE_NAMES = {FULL: "§14 UStG", SMALL: "§33 UStDV (small invoice up to EUR 250)"}

# What a person reads for each item, in the order the law lists them.
LABELS = {
    "recipient": "Max ASP as the recipient",
    "issuer_name": "the vendor's name",
    "invoice_number": "the invoice number",
    "invoice_date": "the invoice date",
    "vat_id_or_tax_number": "the vendor's VAT ID or tax number",
    "service_date": "the date or period of the service",
    "net_amount": "the net amount",
    "gross_amount": "the gross amount",
    "vat_rate": "the VAT rate",
    "vat_amount": "the VAT amount",
    "no_vat_reason": "the reason no VAT is charged (reverse charge or tax exemption note)",
}

# The German tax number in its usual forms: 12/345/67890, 123/456/78901 or 13 digits.
TAX_NUMBER = re.compile(
    r"(?:Steuernummer|Steuer-Nr\.?|St\.?-?Nr\.?|Tax number)[:\s]+"
    r"(\d{2,3}\s?/\s?\d{3,4}\s?/\s?\d{4,5}|\d{10,13})\b",
    re.IGNORECASE,
)
# The notes that explain why an invoice charges no VAT.
NO_VAT_REASON = re.compile(
    r"reverse[- ]charge|Steuerschuldnerschaft des Leistungsempfängers|§\s*13b\s*UStG"
    r"|steuerfrei|tax[- ]exempt|exempt from VAT|VAT exempt|§\s*4\s*Nr\.?\s*\d+\s*\w?\s*UStG"
    r"|innergemeinschaftliche Lieferung|intra-community supply"
    r"|nicht steuerbar|not subject to German VAT|outside the scope of VAT"
    r"|§\s*19\s*(?:Abs\.\s*1\s*)?UStG|Kleinunternehmer",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MissingItem:
    item: str  # a key of LABELS
    note: str | None = None  # more for the person, for example who the invoice is addressed to

    @property
    def text(self) -> str:
        return f"{LABELS[self.item]} ({self.note})" if self.note else LABELS[self.item]


@dataclass(frozen=True)
class FormalCheck:
    """The result for one document. ``rule`` is :data:`FULL` or :data:`SMALL`."""

    document: str
    rule: str
    missing: tuple[MissingItem, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.missing

    @property
    def items(self) -> list[str]:
        return [m.item for m in self.missing]

    def paragraph(self) -> str:
        """What the person in the exception queue reads. Empty when the check passed."""
        if self.passed:
            return ""
        items = "; ".join(m.text for m in self.missing)
        return (
            f"Failed {RULE_NAMES[FULL]} check: the {self.document} is checked against "
            f"{RULE_NAMES[self.rule]} and these items are missing or not readable: {items}. "
            "Without them Max ASP cannot claim the input VAT, so ask the vendor for a corrected "
            "invoice, or confirm the items on the document if ARGUS could not read them."
        )


def check_invoice(
    reading: InvoiceReading,
    own_company: str = OWN_COMPANY,
    *,
    tax_number: str | None = None,
    no_vat_reason: str | None = None,
) -> FormalCheck | None:
    """Check one reading. Returns None when the document is not an incoming invoice: a credit
    note, a cancellation, a pro forma, a letter, an untitled document without an invoice number or
    gross amount, an unreadable document, or an invoice issued by Max ASP itself.

    ``tax_number`` and ``no_vat_reason`` are found in the text of the document. An e-invoice has
    no page text, so :func:`argus.einvoice.check_einvoice` passes what its XML states."""
    if reading.unreadable is not None or not _is_invoice(reading):
        return None
    own = normalise(own_company)
    issuer = reading.value("issuer_name")
    if isinstance(issuer, str) and normalise(issuer) == own:
        return None
    if tax_number is None and (m := TAX_NUMBER.search(reading.text)):
        tax_number = m[1]
    if no_vat_reason is None and (m := NO_VAT_REASON.search(reading.text)):
        no_vat_reason = m[0]

    rule = SMALL if _is_small(reading) else FULL
    missing: list[MissingItem] = []

    def need(item: str, *names: str) -> None:
        if not any(_present(reading, name) for name in names):
            missing.append(MissingItem(item))

    if rule == FULL:
        recipient = reading.value("recipient_name")
        if not _present(reading, "recipient_name"):
            missing.append(MissingItem("recipient"))
        elif isinstance(recipient, str) and normalise(recipient) != own:
            missing.append(
                MissingItem(
                    "recipient", f"the invoice is addressed to {recipient}, not {own_company}"
                )
            )
    need("issuer_name", "issuer_name")
    if rule == FULL:
        need("invoice_number", "invoice_number")
    need("invoice_date", "invoice_date")
    if rule == FULL:
        if not _present(reading, "vendor_vat_id") and tax_number is None:
            missing.append(MissingItem("vat_id_or_tax_number"))
        need("service_date", "service_period")
        need("net_amount", "net_amount")
    else:
        need("gross_amount", "gross_amount")
    missing.extend(_vat_items(reading, rule, no_vat_reason))
    return FormalCheck(reading.document, rule, tuple(missing))


def check_invoices(
    readings: Iterable[InvoiceReading], own_company: str = OWN_COMPANY
) -> list[FormalCheck]:
    """Check every reading of one email. Leaves out the documents that are not checked."""
    return [c for r in readings if (c := check_invoice(r, own_company)) is not None]


def _is_invoice(reading: InvoiceReading) -> bool:
    if reading.kind == "invoice":
        return True
    # A title ARGUS does not know must not let an invoice skip the check (PR #104 review).
    return reading.kind == "unknown" and (
        _present(reading, "invoice_number") or _present(reading, "gross_amount")
    )


def _present(reading: InvoiceReading, name: str) -> bool:
    found = reading.fields.get(name)
    if found is None:
        return False
    # Several VAT rates or IBANs: the document does state them, a person picks the right one.
    return found.value is not None or found.flag is Flag.SEVERAL_VALUES


def _is_small(reading: InvoiceReading) -> bool:
    gross = reading.value("gross_amount")
    if reading.value("currency") != "EUR" or gross is None:
        return False
    try:
        return abs(Decimal(str(gross))) <= SMALL_INVOICE_LIMIT
    except InvalidOperation:
        return False


def _vat_items(reading: InvoiceReading, rule: str, no_vat_reason: str | None) -> list[MissingItem]:
    rate = reading.value("vat_rate")
    zero = rate is not None and _is_zero(rate)
    if not _present(reading, "vat_rate"):
        # A small business or an exempt service names no rate; the note replaces it.
        return [] if no_vat_reason else [MissingItem("vat_rate")]
    if zero:
        # A rate of 0 is only allowed with the reason (§14 Abs. 4 Nr. 8, §14a Abs. 5 UStG).
        return [] if no_vat_reason else [MissingItem("no_vat_reason")]
    if rule == FULL and not _present(reading, "vat_amount"):
        return [MissingItem("vat_amount")]
    return []


def _is_zero(rate: object) -> bool:
    try:
        return Decimal(str(rate)) == 0
    except InvalidOperation:
        return False

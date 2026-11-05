"""Deterministic checks on the invoice data, run after the language model (ADR 0006 point 5, #71).

They are a second opinion that does not depend on the model or on any one reader:

- the net amount plus the VAT amount equals the gross amount,
- the VAT amount matches the VAT rate,
- the IBAN on the invoice has valid check digits,
- the vendor's VAT ID has the format of its country,
- the invoice is addressed to Max ASP (PR-03).

A failed check is a safety gate: the email goes to manual review with the reason, and no case is
opened from it. A value ARGUS could not read is not checked here. The reading already flags it for
a person (#29) and the §14 UStG check names it as missing (#32).

The VAT amount may differ from the rate by up to two cents, because many vendors round the VAT
per line and the sum of rounded lines can be a cent or two off. The net plus VAT sum must match to
the cent: that is plain addition on the totals.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from argus.reading.fields import Flag, InvoiceReading
from argus.reading.text_rules import clean_vat_id, iban_valid
from argus.sorting.checks import normalise
from argus.sorting.config import OWN_COMPANY
from argus.sorting.gates import Gate, GateHit

CENT = Decimal("0.01")
VAT_TOLERANCE = Decimal("0.02")

# The VAT ID formats of the EU (as the VIES service checks them), Northern Ireland, the United
# Kingdom, Switzerland and Norway. A VAT ID with any other country prefix fails the check, so a
# person looks at it.
VAT_ID_FORMATS = {
    "AT": r"U\d{8}",
    "BE": r"[01]\d{9}",
    "BG": r"\d{9,10}",
    "CY": r"\d{8}[A-Z]",
    "CZ": r"\d{8,10}",
    "DE": r"\d{9}",
    "DK": r"\d{8}",
    "EE": r"\d{9}",
    "EL": r"\d{9}",
    "ES": r"[A-Z0-9]\d{7}[A-Z0-9]",
    "FI": r"\d{8}",
    "FR": r"[A-HJ-NP-Z0-9]{2}\d{9}",
    "HR": r"\d{11}",
    "HU": r"\d{8}",
    "IE": r"\d{7}[A-W][A-I]?|\d[A-Z+*]\d{5}[A-W]",
    "IT": r"\d{11}",
    "LT": r"\d{9}|\d{12}",
    "LU": r"\d{8}",
    "LV": r"\d{11}",
    "MT": r"\d{8}",
    "NL": r"\d{9}B\d{2}",
    "PL": r"\d{10}",
    "PT": r"\d{9}",
    "RO": r"\d{2,10}",
    "SE": r"\d{12}",
    "SI": r"\d{8}",
    "SK": r"\d{10}",
    "XI": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
    "GB": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
    "CH": r"E\d{9}(MWST|TVA|IVA)?",
    "NO": r"\d{9}(MVA)?",
}
_VAT_ID_RES = {
    country: re.compile(rf"(?:{pattern})") for country, pattern in VAT_ID_FORMATS.items()
}


def vat_id_valid(vat_id: str) -> bool:
    """Whether the VAT ID has the format of its country. Spaces, dots and dashes are ignored."""
    cleaned = re.sub(r"[.\-]", "", clean_vat_id(vat_id))
    pattern = _VAT_ID_RES.get(cleaned[:2])
    return pattern is not None and pattern.fullmatch(cleaned[2:]) is not None


def _amount(reading: InvoiceReading, name: str) -> Decimal | None:
    """A value to check: read, and not one of several values the reader could not choose from."""
    found = reading.fields.get(name)
    if found is None or found.value is None or found.flag is Flag.SEVERAL_VALUES:
        return None
    try:
        value = Decimal(str(found.value))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _text(reading: InvoiceReading, name: str) -> str | None:
    found = reading.fields.get(name)
    if found is None or found.value is None or found.flag is Flag.SEVERAL_VALUES:
        return None
    return str(found.value)


# Words that may stand in front of the recipient's name: "z. Hd. Max ASP GmbH", "Bill to: ...".
# They are matched on the normalised line, where punctuation has become spaces.
RECIPIENT_LABELS = re.compile(
    r"^(?:(?:z\s*hd|zu h(?:ä|ae)nden|an|to|attn|attention|bill to|invoice to|rechnung an"
    r"|rechnungsempf(?:ä|ae)nger|kunde|customer|empf(?:ä|ae)nger|recipient)\s+)+"
)


def addressed_to_us(recipient: str, own_company: str) -> bool:
    """Whether the recipient line names Max ASP. Only the part before the first comma counts
    (the address often follows it), leading labels are dropped, and a department after the name
    ("Max ASP GmbH Buchhaltung") is fine. Another company that merely mentions Max ASP later in
    the line ("Muster AG c/o Max ASP GmbH") is not."""
    own = normalise(own_company)
    name = RECIPIENT_LABELS.sub("", normalise(recipient.split(",", 1)[0]))
    return name == own or name.startswith(own + " ")


def check_plausible(
    reading: InvoiceReading, where: str, own_company: str = OWN_COMPANY
) -> tuple[GateHit, ...]:
    """Every check the invoice in ``reading`` fails. ``where`` names it by position and page
    ("the attachment 2, page 1"), never by file name (NF-03). Amounts are named, IBANs are not:
    the reasons go into the audit log (PR #79 rule)."""
    hits: list[GateHit] = []

    def fail(gate: Gate, why: str) -> None:
        hits.append(GateHit(gate, f"the invoice in {where}: {why}"))

    net = _amount(reading, "net_amount")
    vat = _amount(reading, "vat_amount")
    gross = _amount(reading, "gross_amount")
    rate = _amount(reading, "vat_rate")
    if net is not None and vat is not None and gross is not None and net + vat != gross:
        fail(
            Gate.FAILED_PLAUSIBILITY,
            f"net {net:.2f} plus VAT {vat:.2f} is {net + vat:.2f}, not the gross {gross:.2f}",
        )
    if net is not None and vat is not None and rate is not None:
        expected = (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(expected - vat) > VAT_TOLERANCE:
            fail(
                Gate.FAILED_PLAUSIBILITY,
                f"VAT {vat:.2f} does not match {rate.normalize():f} % of the net {net:.2f}, "
                f"which is {expected:.2f}",
            )
    iban = _text(reading, "iban_on_document")
    if iban is not None and not iban_valid(re.sub(r"\s", "", iban).upper()):
        fail(Gate.FAILED_PLAUSIBILITY, "the IBAN on the invoice has wrong check digits")
    vat_id = _text(reading, "vendor_vat_id")
    if vat_id is not None and not vat_id_valid(vat_id):
        fail(
            Gate.FAILED_PLAUSIBILITY, "the vendor's VAT ID does not have the format of its country"
        )
    recipient = _text(reading, "recipient_name")
    if recipient is not None and not addressed_to_us(recipient, own_company):
        fail(Gate.ADDRESSED_TO_SOMEONE_ELSE, f"it is addressed to someone else, not {own_company}")
    return tuple(hits)

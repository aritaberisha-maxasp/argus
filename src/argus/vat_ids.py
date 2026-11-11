"""How ARGUS finds and cleans VAT IDs in document text.

The sorting checks (#25) and the invoice reading (#29) both look for the issuer's VAT ID and
both skip Max ASP's own (#106, review of PR 105). They share these rules so the two can never
read the same page differently again.
"""

import re

# A VAT ID label and the VAT ID after it. The value group is the VAT ID as printed.
VAT_ID_LABEL = (
    r"(?:USt-?IdNr\.?|USt-ID(?:-Nr\.?)?|VAT ID|VAT No\.?)[: \t]+([A-Z]{2}[ A-Z0-9]{5,14})\b"
)
# A line that starts with a VAT ID label.
VAT_LINE = re.compile(r"(USt-?IdNr|USt-ID|VAT ID|VAT No)", re.IGNORECASE)
_VAT_ID_LABEL = re.compile(VAT_ID_LABEL, re.IGNORECASE | re.MULTILINE)


def clean_vat_id(raw: str) -> str:
    """Remove spaces and fix the usual OCR slip of letter O for digit 0 after the country code."""
    raw = re.sub(r"\s", "", raw.upper())
    return raw[:2] + raw[2:].replace("O", "0") if raw[:2] in ("DE", "AT") else raw


def stated_vat_ids(text: str) -> list[str]:
    """Every VAT ID the text states after a VAT ID label, cleaned, in order."""
    return [clean_vat_id(m[1]) for m in _VAT_ID_LABEL.finditer(text)]


def states_only(line: str, vat_ids: frozenset[str]) -> bool:
    """The line states VAT IDs, and all of them are among ``vat_ids`` (Max ASP's own)."""
    stated = stated_vat_ids(line)
    return bool(stated) and all(v in vat_ids for v in stated)

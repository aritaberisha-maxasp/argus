"""The facts behind an exception, in plain words for the queue page (#188, EX-01, EX-02).

Each exception keeps its facts as structured data (``exception_item.facts``), written for the
audit log. The box "Why ARGUS stopped" shows them to Benjamin, so this module turns each fact
into a label and a value he can read: yes and no instead of true and false, "none" instead of
an empty list, lists joined by commas, and codes such as ``no_history`` as a sentence.

Facts that only matter to the system (a fingerprint, a draft ID, a file hash) are not shown.
A fact with no label here still shows, its key turned into words, so a new fact is never lost.

This only formats what is already stored. It adds no data, and the raw facts stay in the API
for the page's own use (the G/L account form reads ``candidates``).
"""

from collections.abc import Mapping
from typing import Any

# Facts the system uses to find or match an exception. They mean nothing to a person.
HIDDEN: frozenset[str] = frozenset(
    {
        "fingerprint",  # the nightly audit jobs use it to open one exception per problem
        "draft_message_id",  # the Microsoft ID of a saved forward
        "sha256",  # the hash of a refused statement file
        "bank_statement",  # a marker that the exception is about a statement; the text says so
    }
)

LABELS: dict[str, str] = {
    # IBAN hold (#36)
    "account": "Vendor account",
    "possible_fraud": "Possible fraud",
    "ibans_on_file": "IBANs on file",
    "ibans_stated": "IBANs on the invoice",
    "ibans_differing": "IBANs that differ",
    "read_with_flag": "IBAN read with doubts",
    "other_accounts": "Other vendor accounts",
    # missing G/L account proposal (#37)
    "why": "Why",
    "postings_read": "Postings read",
    "chart": "Chart of accounts",
    "candidates": "Candidates",
    # unknown vendor (#35)
    "vat_id_read": "VAT ID read",
    "iban_read": "IBAN read",
    "name_read": "Name read",
    "accounts": "Vendor accounts found",
    # reading and checking documents
    "documents": "Documents",
    "readers": "Read by",
    "checks": "Checks",
    "copies": "Copies",
    "fields": "Fields",
    "scenarios": "Scenarios",
    "rules": "Rules",
    "missing_items": "Missing items",
    "formal_rules": "Rule list",
    # duplicates, bank statements, reminders, audit copies
    "duplicate_of": "Duplicate of case",
    "currency": "Currency",
    "period": "Statement month",
    "differences": "Differences",
    "reason": "Why",
    "files": "Files",
    "problem": "Problem",
    "night": "Night",
    "error": "Error type",
    "findings": "Findings",
}

# Codes stored as values, in words. Codes not listed here are turned into words as they are.
CODES: dict[str, str] = {
    # why no G/L account was proposed (#37)
    "no_history": "no earlier invoice from this vendor in DATEV",
    "several_accounts": "the latest invoices from this vendor went to different accounts",
    # why no vendor was found (#35)
    "not_found": "no vendor in DATEV matches",
    "several_vendors": "more than one vendor in DATEV matches",
    "own_vat_id": "the only VAT ID on the invoice is Max ASP's own",
    "customer_vat_id": "the VAT ID belongs to a customer",
    "vat_id_differs": "the vendor in DATEV has a different VAT ID",
    # bank statements
    "statement_sender_not_confirmed": "the statement did not come from a confirmed sender",
    "no_csv_in_folder": "no CSV file in the statement folder",
    "running_balance": "running balance",
    "opening_balance": "opening balance",
    "later_month_imported": "a later month is imported already",
    # e-invoice and formal checks (#28, #32, #33)
    "pdf_matches_xml": "PDF matches the XML",
    "kosit_validation": "e-invoice rules (KoSIT)",
    "ustg14": "§14 UStG",
    "ustdv33": "§33 UStDV",
    # payment reminder forwards
    "draft_gone": "the saved forward is gone",
    "draft_changed": "the saved forward was changed",
}

# Keys whose string values are codes, so they are put into words.
CODE_KEYS: frozenset[str] = frozenset(
    {
        "why",
        "reason",
        "problem",
        "checks",
        "fields",
        "findings",
        "scenarios",
        "rules",
        "missing_items",
        "formal_rules",
    }
)


def fact_lines(facts: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Label and value for each fact a person should see. Known facts come in the order of
    :data:`LABELS`, so they read the same way every time (PostgreSQL keeps jsonb keys in its own
    order, not the order they were written). Facts without a label follow, in their order."""
    order = {key: n for n, key in enumerate(LABELS)}
    shown = sorted(
        (key for key in facts if key not in HIDDEN), key=lambda k: order.get(k, len(order))
    )
    return [(label(key), value_text(key, facts[key])) for key in shown]


def label(key: str) -> str:
    known = LABELS.get(key)
    if known:
        return known
    text = " ".join(_word(w) for w in key.split("_") if w)
    return text[:1].upper() + text[1:]


def _word(word: str) -> str:
    # Abbreviations a reader knows in capitals.
    return {
        "iban": "IBAN",
        "ibans": "IBANs",
        "vat": "VAT",
        "datev": "DATEV",
        "id": "ID",
        "gl": "G/L",
    }.get(word.lower(), word)


def value_text(key: str, value: Any) -> str:
    if key == "candidates" and isinstance(value, list):
        return ", ".join(_candidate(c) for c in value) or "none"
    return _text(value, key in CODE_KEYS)


def _text(value: Any, codes: bool) -> str:
    if value is None:
        return "not known"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _code(value) if codes else value
    if isinstance(value, Mapping):
        if not value:
            return "none"
        return ", ".join(
            f"{_code(str(k)) if codes else k}: {_text(v, codes)}" for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v, codes) for v in value) or "none"
    return str(value)


def _code(value: str) -> str:
    return CODES.get(value, value.replace("_", " "))


def _candidate(candidate: Any) -> str:
    if not isinstance(candidate, Mapping):
        return _text(candidate, False)
    account = candidate.get("gl_account") or "?"
    key = candidate.get("tax_key")
    return f"{account} with tax key {key}" if key else f"{account} with no tax key"

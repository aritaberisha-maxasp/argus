"""The facts behind an exception in plain words (#188, EX-01, EX-02)."""

from argus.queue_page.facts import fact_lines, label


def test_iban_hold_facts_read_as_words() -> None:
    facts = {
        "account": "70004",
        "possible_fraud": True,
        "ibans_on_file": 1,
        "ibans_stated": 2,
        "ibans_differing": 1,
        "read_with_flag": False,
        "other_accounts": [],
    }
    assert fact_lines(facts) == [
        ("Vendor account", "70004"),
        ("Possible fraud", "yes"),
        ("IBANs on file", "1"),
        ("IBANs on the invoice", "2"),
        ("IBANs that differ", "1"),
        ("IBAN read with doubts", "no"),
        ("Other vendor accounts", "none"),
    ]


def test_a_list_is_joined_by_commas_without_brackets() -> None:
    assert fact_lines({"other_accounts": ["70004", "70011"]}) == [
        ("Other vendor accounts", "70004, 70011")
    ]


def test_missing_account_proposal_without_history() -> None:
    facts = {
        "account": "70004",
        "why": "no_history",
        "postings_read": 0,
        "chart": "SKR03",
        "candidates": [],
    }
    assert fact_lines(facts) == [
        ("Vendor account", "70004"),
        ("Why", "no earlier invoice from this vendor in DATEV"),
        ("Postings read", "0"),
        ("Chart of accounts", "SKR03"),
        ("Candidates", "none"),
    ]


def test_missing_account_proposal_with_several_accounts() -> None:
    facts = {
        "why": "several_accounts",
        "candidates": [
            {"gl_account": "4930", "tax_key": "9"},
            {"gl_account": "4210", "tax_key": ""},
        ],
    }
    assert fact_lines(facts) == [
        ("Why", "the latest invoices from this vendor went to different accounts"),
        ("Candidates", "4930 with tax key 9, 4210 with no tax key"),
    ]


def test_unknown_vendor_reason_in_words() -> None:
    lines = dict(fact_lines({"why": "several_vendors", "vat_id_read": True, "accounts": []}))
    assert lines == {
        "Why": "more than one vendor in DATEV matches",
        "VAT ID read": "yes",
        "Vendor accounts found": "none",
    }


def test_system_facts_are_hidden() -> None:
    facts = {
        "night": "2026-11-20",
        "error": "OSError",
        "fingerprint": "ab12",
        "draft_message_id": "AAMk",
        "sha256": "f00d",
        "bank_statement": True,
    }
    assert fact_lines(facts) == [("Night", "2026-11-20"), ("Error type", "OSError")]


def test_codes_in_lists_and_counts_read_as_words() -> None:
    assert fact_lines({"checks": ["pdf_matches_xml", "ustg14"]}) == [
        ("Checks", "PDF matches the XML, §14 UStG")
    ]
    assert fact_lines({"findings": {"hash_mismatch": 2}}) == [("Findings", "hash mismatch: 2")]
    assert fact_lines({"problem": "draft_gone"}) == [("Problem", "the saved forward is gone")]


def test_an_unknown_key_still_shows_in_words() -> None:
    assert fact_lines({"new_iban_count": 3, "missing": ["delivery_date"]}) == [
        ("New IBAN count", "3"),
        ("Missing", "delivery_date"),
    ]
    assert label("vat_id_seen") == "VAT ID seen"


def test_no_value_and_an_empty_mapping() -> None:
    assert fact_lines({"reason": None, "findings": {}}) == [
        ("Why", "not known"),
        ("Findings", "none"),
    ]


def test_free_text_values_are_not_changed() -> None:
    # Only codes are put into words. A document name or a currency stays as stored.
    assert fact_lines({"documents": ["attachment_1"], "currency": "EUR"}) == [
        ("Documents", "attachment_1"),
        ("Currency", "EUR"),
    ]


def test_known_facts_keep_one_order_whatever_order_they_come_in() -> None:
    # PostgreSQL returns jsonb keys shortest first, not in the order they were written.
    from_db = {"account": "70006", "other_accounts": [], "possible_fraud": True, "zz_new": 1}
    assert [name for name, _ in fact_lines(from_db)] == [
        "Vendor account",
        "Possible fraud",
        "Other vendor accounts",
        "Zz new",
    ]

"""Checks that keep the spike's numbers honest. Run from the repository root:

    python3 -m pytest spikes/document_understanding -q

Needs Tesseract with German and English (apt install tesseract-ocr tesseract-ocr-deu).
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytest  # noqa: E402

import einvoice  # noqa: E402
from documents import check_fields, document_kind, iban_valid, parse_amount  # noqa: E402
from evaluate import FIXTURES, run  # noqa: E402

pytestmark = pytest.mark.req("IN-04", "IN-08", "IN-09", "PR-01", "NF-02")


def test_fixture_emails_meet_ac1_and_lose_no_invoice():
    result = run()
    assert result["routes_correct"] >= 48  # AC-1: 95 % of 50
    assert result["invoices_sent_to_deletion"] == []
    assert result["cases_correct"] == 50  # IN-08


def test_holdout_emails_never_delete_an_invoice():
    result = run(HERE / "holdout" / "emails")
    assert result["invoices_sent_to_deletion"] == []


def test_xrechnung_and_zugferd_give_the_same_fields_as_the_expected_data():
    xml = (FIXTURES / "documents" / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
    assert einvoice.detect_syntax(xml) == "ubl"
    data = einvoice.parse(xml)
    assert (data["invoice_number"], data["gross_amount"], data["iban_on_document"]) == ("BCH-26-10-0057", "147.56", "DE52000000030000700002")


def test_xml_with_an_external_entity_is_not_resolved():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    assert einvoice.detect_syntax(evil) is None


@pytest.mark.parametrize("title,kind", [
    ("Rechnung", "invoice"), ("Stornorechnung", "cancellation"), ("Gutschrift", "credit_note"),
    ("2. Mahnung", "reminder"), ("Pro forma invoice", "proforma"), ("Lieferschein LS-1", "delivery_note"),
    ("Rechnung BB-1 vom 01.09.2026 über 10,00 EUR, fällig", "unknown"),  # a sentence in a letter is not a title
])
def test_document_title(title, kind):
    assert document_kind(title) == kind


def test_amounts_and_checks():
    assert parse_amount("1.234,56") == "1234.56"
    assert parse_amount("1,234.56") == "1234.56"
    assert iban_valid("DE44000000020000700001")
    assert not iban_valid("DE45000000020000700001")
    problems = check_fields({"issuer_name": "A", "invoice_number": "1", "invoice_date": "2026-10-01", "currency": "EUR",
                             "net_amount": "100.00", "vat_rate": "19", "vat_amount": "19.00", "gross_amount": "120.00"})
    assert problems == ["net plus VAT does not equal gross"]

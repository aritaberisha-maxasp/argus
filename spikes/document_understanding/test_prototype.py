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
from documents import check_fields, compare_copies, document_kind, iban_valid, parse_amount  # noqa: E402
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


UBL_HEAD = (b'<ubl:Invoice xmlns:ubl="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
            b'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">')


def test_einvoice_with_a_dtd_is_rejected():
    xml = b'<!DOCTYPE i [<!ENTITY x "INJ">]>' + UBL_HEAD + b"<cbc:ID>A&x;B</cbc:ID></ubl:Invoice>"
    with pytest.raises(einvoice.InvalidEInvoice, match="DTD"):
        einvoice.parse(xml)


def test_comment_inside_a_value_does_not_cut_it_short():
    assert einvoice.parse(UBL_HEAD + b"<cbc:ID>R-1<!--x-->23</cbc:ID></ubl:Invoice>")["invoice_number"] == "R-123"


@pytest.mark.parametrize("amount", [b"12,50", b"abc", b"NaN"])
def test_broken_amount_is_rejected_not_crashing(amount):
    xml = UBL_HEAD.replace(b">", b' xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">', 1)
    xml += b"<cbc:ID>R-1</cbc:ID><cac:LegalMonetaryTotal><cbc:TaxInclusiveAmount>" + amount + b"</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal></ubl:Invoice>"
    with pytest.raises(einvoice.InvalidEInvoice, match="not a number"):
        einvoice.parse(xml)


def test_broken_einvoice_goes_to_manual_review():
    from classify import Config, classify
    from documents import ParsedEmail, read_attachment

    docs = read_attachment("x.xml", "application/xml", b'<!DOCTYPE i []>' + UBL_HEAD + b"<cbc:ID>R-1</cbc:ID></ubl:Invoice>")
    mail = ParsedEmail(Path("x.eml"), "Muster Bürobedarf GmbH", "rechnungen@muster-buerobedarf.example", "Rechnung R-1", "", {}, docs, 1)
    decision = classify(mail, Config.from_master_data(FIXTURES / "master-data"))
    assert decision.route == "manual_review"
    assert any("DTD" in gate for gate in decision.gates)


def test_same_number_from_two_vendors_stays_two_cases():
    from documents import Document, merge_same_invoice

    a = Document("a.pdf", "pdf", kind="invoice", fields={"invoice_number": "1001", "vendor_vat_id": "DE012345601"})
    b = Document("b.pdf", "pdf", kind="invoice", fields={"invoice_number": "1001", "vendor_vat_id": "DE012345602"})
    assert [d.source for d in merge_same_invoice([a, b])] == ["a.pdf", "b.pdf"]


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


def test_pdf_copy_that_differs_from_its_xml_is_flagged():
    xml = {"invoice_number": "R-1", "gross_amount": "100.00", "iban_on_document": "DE44000000020000700001"}
    pdf = {"invoice_number": "R-1", "gross_amount": "100.00", "iban_on_document": "DE33000000660000706666"}
    assert compare_copies(xml, pdf) == ["PDF copy and XML differ on iban_on_document: PDF DE33000000660000706666, XML DE44000000020000700001"]
    assert compare_copies(xml, dict(xml)) == []

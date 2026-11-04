"""ARGUS checks every incoming invoice for the formal items of §14 UStG and names what is missing.

Covers: PR-03
"""

import dataclasses
from pathlib import Path

import pytest

from argus.einvoice import EInvoice, check_einvoice, read_einvoices, read_xml
from argus.reading import (
    Analysis,
    Flag,
    InvoiceReading,
    Page,
    ReadField,
    ReadingSettings,
    Source,
    check_invoice,
    check_invoices,
    read_body,
)
from argus.reading.formal_check import FULL, SMALL
from argus.reading.invoice import build_reading
from tests.unit.test_reading import EXPECTED, READ_HERE, body_of, read_with_recording

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
D08 = (FIXTURES / "documents" / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
X01 = (FIXTURES / "einvoice" / "X01_xrechnung_cii_BCH-26-11-0061.xml").read_bytes()

INCOMING_INVOICES = [
    d
    for d in READ_HERE
    if EXPECTED[d]["direction"] == "incoming" and EXPECTED[d]["kind"] == "invoice"
]
NOT_CHECKED = [d for d in READ_HERE if d not in INCOMING_INVOICES]
ADDRESSED_TO_SOMEONE_ELSE = "D34"  # E49: the invoice is addressed to Max Muster AG


def without(reading: InvoiceReading, *names: str) -> InvoiceReading:
    """The reading with these fields not found, as ARGUS reads a document that lacks them."""
    fields = dict(reading.fields)
    for name in names:
        fields[name] = ReadField(name, flag=Flag.NOT_FOUND, note="not found on the document")
    return dataclasses.replace(reading, fields=fields)


def with_values(reading: InvoiceReading, **values: str) -> InvoiceReading:
    fields = dict(reading.fields)
    for name, value in values.items():
        fields[name] = ReadField(name, value, Source.PAGE_TEXT, page=1, words=value)
    return dataclasses.replace(reading, fields=fields)


def full_invoice() -> InvoiceReading:
    """D01: EUR 354.03 with 19 % VAT, every item there."""
    return read_with_recording("D01")


def small_invoice() -> InvoiceReading:
    """D32: EUR 58.31, a small invoice under §33 UStDV."""
    return read_with_recording("D32")


def items(reading: InvoiceReading) -> list[str]:
    check = check_invoice(reading)
    assert check is not None
    return check.items


@pytest.mark.req("PR-03")
@pytest.mark.parametrize("doc_id", [d for d in INCOMING_INVOICES if d != ADDRESSED_TO_SOMEONE_ELSE])
def test_every_complete_incoming_invoice_passes(doc_id: str) -> None:
    check = check_invoice(read_with_recording(doc_id))

    assert check is not None and check.passed, check
    assert check.paragraph() == ""


@pytest.mark.req("PR-03")
def test_the_invoice_in_the_email_body_passes() -> None:
    reading = read_body(body_of("E16_rechnung-im-mailtext.eml"))
    assert reading is not None

    check = check_invoice(reading)

    assert check is not None and check.passed and check.rule == SMALL


@pytest.mark.req("PR-03")
def test_an_invoice_to_another_company_fails_and_names_who_it_is_addressed_to() -> None:
    check = check_invoice(read_with_recording(ADDRESSED_TO_SOMEONE_ELSE))

    assert check is not None and check.rule == FULL
    assert check.items == ["recipient"]
    text = check.paragraph()
    assert text.startswith("Failed §14 UStG check: the attachment 1 is checked against §14 UStG")
    assert "Max ASP as the recipient (the invoice is addressed to Max Muster AG" in text


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    ("fields", "item"),
    [
        (("recipient_name",), "recipient"),
        (("issuer_name",), "issuer_name"),
        (("invoice_number",), "invoice_number"),
        (("invoice_date",), "invoice_date"),
        (("vendor_vat_id",), "vat_id_or_tax_number"),
        (("service_period",), "service_date"),
        (("net_amount",), "net_amount"),
        (("vat_rate",), "vat_rate"),
        (("vat_amount",), "vat_amount"),
    ],
)
def test_each_missing_item_of_the_full_list_is_named(fields: tuple[str, ...], item: str) -> None:
    assert items(without(full_invoice(), *fields)) == [item]


@pytest.mark.req("PR-03")
def test_every_missing_item_is_named_at_once_in_the_order_of_the_law() -> None:
    reading = without(full_invoice(), "vat_amount", "service_period", "invoice_number")

    check = check_invoice(reading)

    assert check is not None
    assert check.items == ["invoice_number", "service_date", "vat_amount"]
    assert (
        "the invoice number; the date or period of the service; the VAT amount" in check.paragraph()
    )


@pytest.mark.req("PR-03")
def test_a_tax_number_stands_in_for_the_vat_id() -> None:
    reading = without(full_invoice(), "vendor_vat_id")
    reading = dataclasses.replace(reading, text=reading.text + "\nSteuernummer: 12/345/67890\n")

    assert items(reading) == []


@pytest.mark.req("PR-03")
def test_a_small_invoice_needs_only_the_reduced_list_of_section_33_ustdv() -> None:
    reading = without(
        small_invoice(),
        "recipient_name",
        "invoice_number",
        "vendor_vat_id",
        "service_period",
        "net_amount",
        "vat_amount",
    )

    check = check_invoice(reading)

    assert check is not None and check.rule == SMALL and check.passed


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    ("field", "item"),
    [
        ("issuer_name", "issuer_name"),
        ("invoice_date", "invoice_date"),
        ("vat_rate", "vat_rate"),
    ],
)
def test_a_small_invoice_still_needs_the_items_of_section_33_ustdv(field: str, item: str) -> None:
    check = check_invoice(without(small_invoice(), field))

    assert check is not None and check.rule == SMALL
    assert check.items == [item]
    assert "§33 UStDV (small invoice up to EUR 250)" in check.paragraph()


@pytest.mark.req("PR-03")
def test_without_the_gross_amount_the_full_list_applies() -> None:
    check = check_invoice(without(small_invoice(), "gross_amount"))

    assert check is not None and check.rule == FULL and check.passed


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    ("gross", "currency", "rule"),
    [
        ("250.00", "EUR", SMALL),  # exactly EUR 250 is still small
        ("250.01", "EUR", FULL),
        ("0.01", "EUR", SMALL),
        ("78.00", "GBP", FULL),  # a foreign currency needs an exchange rate, so the full list
        ("78.00", "USD", FULL),
    ],
)
def test_the_small_invoice_limit_is_eur_250_including(gross: str, currency: str, rule: str) -> None:
    reading = with_values(full_invoice(), gross_amount=gross, currency=currency)

    check = check_invoice(reading)

    assert check is not None and check.rule == rule


@pytest.mark.req("PR-03")
def test_no_vat_without_a_reason_fails() -> None:
    reading = with_values(full_invoice(), vat_rate="0", vat_amount="0.00")

    assert items(reading) == ["no_vat_reason"]


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    "note",
    [
        "Reverse charge: VAT to be accounted for by the recipient.",
        "Steuerschuldnerschaft des Leistungsempfängers",
        "Steuerfreie innergemeinschaftliche Lieferung",
        "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.",
        "Steuerfrei nach § 4 Nr. 14 a UStG",
    ],
)
def test_no_vat_with_the_reason_passes(note: str) -> None:
    reading = with_values(full_invoice(), vat_rate="0", vat_amount="0.00")
    reading = dataclasses.replace(reading, text=reading.text + "\n" + note + "\n")

    assert items(reading) == []


@pytest.mark.req("PR-03")
def test_a_small_business_invoice_without_a_rate_passes_with_its_note() -> None:
    reading = without(full_invoice(), "vat_rate", "vat_amount")
    reading = dataclasses.replace(
        reading, text=reading.text + "\nKleinunternehmer nach § 19 UStG\n"
    )

    assert items(reading) == []


@pytest.mark.req("PR-03")
def test_a_value_read_with_a_flag_still_counts_as_there() -> None:
    reading = full_invoice()
    fields = dict(reading.fields)
    fields["invoice_number"] = dataclasses.replace(
        fields["invoice_number"], flag=Flag.LOW_CONFIDENCE, confidence=0.6
    )
    fields["vat_rate"] = ReadField("vat_rate", flag=Flag.SEVERAL_VALUES, note="19 and 7")

    assert items(dataclasses.replace(reading, fields=fields)) == []


@pytest.mark.req("PR-03")
@pytest.mark.parametrize("doc_id", NOT_CHECKED)
def test_credit_notes_pro_formas_and_our_own_invoices_are_not_checked(doc_id: str) -> None:
    assert check_invoice(read_with_recording(doc_id)) is None


@pytest.mark.req("PR-03")
def test_an_unreadable_document_is_not_checked() -> None:
    reading = InvoiceReading("attachment 1", "unknown", "test", 1, "", unreadable="blank page")

    assert check_invoice(reading) is None
    assert check_invoices([reading, full_invoice()]) == [check_invoice(full_invoice())]


def einvoice(data: bytes) -> EInvoice:
    found = read_xml("attachment 1", data)
    assert found is not None
    return found


@pytest.mark.req("PR-03")
def test_the_einvoices_pass() -> None:
    zugferd = (FIXTURES / "documents" / "D09_Rechnung_BB-2026-1243_zugferd.pdf").read_bytes()
    (embedded,) = read_einvoices([zugferd]).einvoices

    for found in (einvoice(D08), einvoice(X01), embedded):
        check = check_einvoice(found)
        assert check is not None and check.passed, check


@pytest.mark.req("PR-03")
def test_an_einvoice_with_a_tax_number_instead_of_a_vat_id_passes() -> None:
    xml = X01.replace(b'schemeID="VA"', b'schemeID="FC"')
    found = einvoice(xml)
    assert found.reading.value("vendor_vat_id") is None

    check = check_einvoice(found)

    assert check is not None and check.passed


@pytest.mark.req("PR-03")
def test_an_einvoice_without_vat_id_and_tax_number_fails() -> None:
    found = einvoice(X01.replace(b'schemeID="VA"', b'schemeID="XX"'))

    check = check_einvoice(found)

    assert check is not None and check.items == ["vat_id_or_tax_number"]


@pytest.mark.req("PR-03")
def test_an_einvoice_with_reverse_charge_passes_without_vat() -> None:
    reading = with_values(einvoice(X01).reading, vat_rate="0", vat_amount="0.00")
    reverse = einvoice(X01.replace(b"<ram:CategoryCode>S<", b"<ram:CategoryCode>AE<"))
    plain = einvoice(X01)

    with_reason = check_einvoice(dataclasses.replace(reverse, reading=reading))
    without_reason = check_einvoice(dataclasses.replace(plain, reading=reading))

    assert with_reason is not None and with_reason.passed
    assert without_reason is not None and without_reason.items == ["no_vat_reason"]


UNTITLED_INVOICE = """Beispiel Werkstatt GmbH
Musterweg 2, 70000 Musterstadt
Max ASP GmbH
Testallee 1
70000 Musterstadt

Rechnung Nr. 2026-0012 vom 01.10.2026
Rechnungsnummer: 2026-0012
Rechnungsdatum: 01.10.2026
Wartung Drucker, 2 Stunden
Nettobetrag: 400,00 EUR
USt 19 %: 76,00 EUR
Gesamtbetrag: 476,00 EUR
Zahlbar bis 31.10.2026
"""
TERMS = """Allgemeine Geschäftsbedingungen der Beispiel Werkstatt GmbH
1. Geltungsbereich. Diese Bedingungen gelten für alle Aufträge.
2. Zahlung. Rechnungen sind innerhalb von 30 Tagen zu zahlen.
"""


def read_text(text: str) -> InvoiceReading:
    analysis = Analysis(pages=(Page(1, text),))
    return build_reading("attachment 1", "test", analysis, None, ReadingSettings())


@pytest.mark.req("PR-03", "EX-01")
def test_an_invoice_with_a_title_argus_does_not_know_is_still_checked() -> None:
    reading = read_text(UNTITLED_INVOICE)
    assert reading.kind == "unknown"

    check = check_invoice(reading)

    # No VAT ID and no service date: it must not pass silently (PR #104 review).
    assert check is not None and check.rule == FULL
    assert "vat_id_or_tax_number" in check.items and "service_date" in check.items


@pytest.mark.req("PR-03")
def test_a_known_invoice_read_without_its_title_fails_like_any_other() -> None:
    reading = dataclasses.replace(without(full_invoice(), "service_period"), kind="unknown")

    assert items(reading) == ["service_date"]


@pytest.mark.req("PR-03")
def test_an_untitled_attachment_without_invoice_number_or_amount_is_not_checked() -> None:
    reading = read_text(TERMS)
    assert reading.kind == "unknown"

    assert check_invoice(reading) is None

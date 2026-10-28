"""ARGUS reads XRechnung and ZUGFeRD e-invoices straight from their XML.

Covers: IN-02, NF-02, PR-01, EX-01
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from argus.documents.email import attachments_of, store_email
from argus.documents.store import MemoryDocumentStore
from argus.einvoice import (
    READER,
    EInvoice,
    EInvoiceRefused,
    KositValidator,
    ValidatorUnavailable,
    compare,
    embedded_xml,
    pair,
    read_einvoices,
    read_xml,
)
from argus.einvoice.validator import read_report
from argus.reading import (
    PR01_FIELDS,
    Flag,
    InvoiceReading,
    LocalReader,
    ReadField,
    Source,
    read_attachment,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENTS = FIXTURES / "documents"
EINVOICES = FIXTURES / "einvoice"
EMAILS = FIXTURES / "emails"
EXPECTED = {d["document_id"]: d for d in json.loads((DOCUMENTS / "expected.json").read_text())}

D08 = (DOCUMENTS / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
D24 = (DOCUMENTS / "D24_Rechnung_MA-2026-0104_xrechnung.xml").read_bytes()
D09_PDF = (DOCUMENTS / "D09_Rechnung_BB-2026-1243_zugferd.pdf").read_bytes()
X01 = (EINVOICES / "X01_xrechnung_cii_BCH-26-11-0061.xml").read_bytes()
X02 = (EINVOICES / "X02_xrechnung_ubl_credit_note_GS-BB-2026-0071.xml").read_bytes()
FIELDS = (*PR01_FIELDS, "recipient_name")


def d09_xml() -> bytes:
    xml = embedded_xml(D09_PDF)
    assert xml is not None
    return xml


def read(data: bytes, document: str = "attachment 1") -> EInvoice:
    einvoice = read_xml(document, data)
    assert einvoice is not None
    return einvoice


def email_attachments(name: str) -> list[bytes]:
    raw = next(EMAILS.glob(f"{name}_*.eml")).read_bytes()
    return [a.data for a in attachments_of(raw)]


def pdf_reading(document: str, **values: Any) -> InvoiceReading:
    fields = {
        name: ReadField(name, value, Source.PAGE_TEXT, page=1, words=str(value))
        for name, value in values.items()
    }
    return InvoiceReading(document, "invoice", "local", 1, "text", fields)


# Reading the fields


@pytest.mark.req("IN-02", "NF-02", "PR-01")
@pytest.mark.parametrize(
    ("document_id", "data"),
    [("D08", D08), ("D24", D24), ("D09", d09_xml())],
    ids=["xrechnung-ubl", "xrechnung-ubl-outgoing", "zugferd-cii"],
)
def test_every_pr01_field_matches_the_fixture(document_id: str, data: bytes) -> None:
    einvoice = read(data)

    for name in FIELDS:
        assert einvoice.reading.value(name) == EXPECTED[document_id][name], name
    assert einvoice.reading.kind == EXPECTED[document_id]["kind"]
    assert einvoice.direct_debit is EXPECTED[document_id]["direct_debit"]


@pytest.mark.req("IN-02", "NF-02", "PR-01")
def test_xrechnung_cii_is_read_with_its_discount() -> None:
    einvoice = read(X01)

    assert einvoice.syntax == "cii"
    assert einvoice.specification is not None and "xrechnung_3.0" in einvoice.specification
    values = {name: einvoice.reading.value(name) for name in FIELDS}
    assert values == {
        "issuer_name": "Beispiel Cloud Hosting GmbH",
        "vendor_vat_id": "DE012345602",
        "invoice_number": "BCH-26-11-0061",
        "invoice_date": "2026-11-02",
        "service_period": ["2026-11-01", "2026-11-30"],
        "net_amount": "250.00",
        "vat_rate": "19",
        "vat_amount": "47.50",
        "gross_amount": "297.50",
        "currency": "EUR",
        "due_date": "2026-11-16",
        "early_payment_discount": {"percent": "3.00", "until": "2026-11-09"},
        "iban_on_document": "DE52000000030000700002",
        "recipient_name": "Max ASP GmbH",
    }


@pytest.mark.req("IN-02", "PR-01")
def test_a_ubl_credit_note_is_read_as_a_credit_note() -> None:
    einvoice = read(X02)

    assert einvoice.reading.kind == "credit_note"
    assert einvoice.type_code == "381"
    assert einvoice.refers_to == "BB-2026-1243"
    assert einvoice.reading.value("gross_amount") == "343.91"
    # A credit note asks for no payment, so no due date is needed (#29 rule).
    assert "due_date" not in einvoice.reading.fields


@pytest.mark.req("NF-02", "PR-01")
@pytest.mark.parametrize("data", [D08, X01, d09_xml()], ids=["ubl", "cii", "zugferd"])
def test_every_value_comes_from_the_xml_alone(data: bytes) -> None:
    reading = read(data).reading

    assert reading.reader == READER
    for found in reading.fields.values():
        if found.value is not None:
            # No OCR, no Document Intelligence, no model: the XML is the legal original.
            assert found.source is Source.EINVOICE_XML
            assert found.confidence is None


@pytest.mark.req("NF-02")
def test_the_xml_is_kept_exactly_as_received() -> None:
    assert read(D08).xml is D08


@pytest.mark.req("NF-02")
def test_a_comment_inside_a_value_cannot_cut_it_short() -> None:
    commented = D08.replace(
        b"<cbc:ID>DE52000000030000700002</cbc:ID>",
        b"<cbc:ID>DE5200000003<!-- note -->0000700002</cbc:ID>",
    ).replace(
        b'<cbc:TaxInclusiveAmount currencyID="EUR">147.56</cbc:TaxInclusiveAmount>',
        b'<cbc:TaxInclusiveAmount currencyID="EUR">1<!-- x -->47.56</cbc:TaxInclusiveAmount>',
    )

    reading = read(commented).reading

    assert reading.value("iban_on_document") == "DE52000000030000700002"
    assert reading.value("gross_amount") == "147.56"


@pytest.mark.req("PR-01")
def test_several_vat_rates_leave_the_rate_empty_and_flagged() -> None:
    two_rates = X01.replace(
        b"<ram:BillingSpecifiedPeriod>",
        b"<ram:ApplicableTradeTax><ram:CalculatedAmount>0.70</ram:CalculatedAmount>"
        b"<ram:TypeCode>VAT</ram:TypeCode><ram:BasisAmount>10.00</ram:BasisAmount>"
        b"<ram:CategoryCode>S</ram:CategoryCode>"
        b"<ram:RateApplicablePercent>7.00</ram:RateApplicablePercent></ram:ApplicableTradeTax>"
        b"<ram:BillingSpecifiedPeriod>",
    )

    rate = read(two_rates).reading.fields["vat_rate"]

    assert rate.value is None and rate.flag is Flag.SEVERAL_VALUES
    assert rate.note is not None and "19" in rate.note and "7" in rate.note


@pytest.mark.req("PR-01")
def test_vat_stated_again_in_euros_is_not_the_invoice_vat() -> None:
    # BT-111: an invoice in another currency may repeat the VAT in the accounting currency.
    usd = D08.replace(b'currencyID="EUR"', b'currencyID="USD"').replace(
        b"<cbc:DocumentCurrencyCode>EUR", b"<cbc:DocumentCurrencyCode>USD"
    )
    with_eur = usd.replace(
        b"<cac:TaxTotal>",
        b'<cac:TaxTotal><cbc:TaxAmount currencyID="EUR">21.70</cbc:TaxAmount></cac:TaxTotal>'
        b"<cac:TaxTotal>",
        1,
    )

    reading = read(with_eur).reading

    assert reading.value("currency") == "USD"
    assert reading.value("vat_amount") == "23.56"


@pytest.mark.req("PR-01")
def test_an_iban_with_wrong_check_digits_is_flagged() -> None:
    wrong = D08.replace(b"DE52000000030000700002", b"DE53000000030000700002")

    iban = read(wrong).reading.fields["iban_on_document"]

    assert iban.value == "DE53000000030000700002"
    assert iban.flag is Flag.FAILED_CHECK


@pytest.mark.req("PR-01")
def test_a_direct_debit_invoice_has_no_payee_iban() -> None:
    debit = X01.replace(b"<ram:TypeCode>58</ram:TypeCode>", b"<ram:TypeCode>59</ram:TypeCode>")

    einvoice = read(debit)

    assert einvoice.direct_debit is True
    assert einvoice.reading.value("iban_on_document") is None


@pytest.mark.req("PR-01")
def test_unreadable_discount_terms_are_flagged_not_guessed() -> None:
    odd = X01.replace(b"#SKONTO#TAGE=7#PROZENT=3.00#", b"#SKONTO#TAGE=sieben#PROZENT=3.00#")

    discount = read(odd).reading.fields["early_payment_discount"]

    assert discount.value is None and discount.flag is Flag.FAILED_CHECK


# Refusing what cannot be read safely


DTD_CASES = {
    "internal entity": b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY n "BCH-1">]>',
    "external entity": b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY n SYSTEM "file:///etc/passwd">]>',
    "billion laughs": (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    ),
    "external dtd": b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "http://attacker.example/x.dtd">',
    "empty doctype": b'<?xml version="1.0"?><!DOCTYPE x>',
}


@pytest.mark.req("NF-02", "EX-01")
@pytest.mark.parametrize("prolog", DTD_CASES.values(), ids=DTD_CASES.keys())
def test_xml_with_a_dtd_is_refused(prolog: bytes) -> None:
    with_dtd = prolog + D08.split(b"?>", 1)[1]

    with pytest.raises(EInvoiceRefused, match="DTD"):
        read_xml("attachment 1", with_dtd)


BROKEN = {
    "amount not a number": (
        b">147.56</cbc:TaxInclusiveAmount>",
        b">147,56</cbc:TaxInclusiveAmount>",
    ),
    "amount with three decimals": (
        b">147.56</cbc:TaxInclusiveAmount>",
        b">147.565</cbc:TaxInclusiveAmount>",
    ),
    "amount infinite": (b">147.56</cbc:TaxInclusiveAmount>", b">Infinity</cbc:TaxInclusiveAmount>"),
    "date not a date": (b"<cbc:IssueDate>2026-10-08", b"<cbc:IssueDate>2026-13-08"),
    "rate not a percentage": (
        b"<cbc:Percent>19.00</cbc:Percent>",
        b"<cbc:Percent>190</cbc:Percent>",
    ),
    "not an invoice type": (b">380</cbc:InvoiceTypeCode>", b">999</cbc:InvoiceTypeCode>"),
    "no invoice number": (b"<cbc:ID>BCH-26-10-0057</cbc:ID>", b""),
    "not well formed": (b"</ubl:Invoice>", b""),
}


@pytest.mark.req("NF-02", "EX-01")
@pytest.mark.parametrize(("old", "new"), BROKEN.values(), ids=BROKEN.keys())
def test_an_einvoice_that_cannot_be_read_safely_is_refused_with_a_reason(
    old: bytes, new: bytes
) -> None:
    assert old in D08
    with pytest.raises(EInvoiceRefused) as refused:
        read_xml("attachment 1", D08.replace(old, new))

    assert str(refused.value)


@pytest.mark.req("NF-02")
def test_a_cii_date_in_another_format_is_refused() -> None:
    other = X01.replace(b'format="102">20261102', b'format="610">202611')

    with pytest.raises(EInvoiceRefused, match="CCYYMMDD"):
        read_xml("attachment 1", other)


@pytest.mark.req("NF-02")
def test_zugferd_1_is_refused() -> None:
    old = b'<rsm:CrossIndustryDocument xmlns:rsm="urn:ferd:CrossIndustryDocument:invoice:1p0"/>'

    with pytest.raises(EInvoiceRefused, match="ZUGFeRD 1"):
        read_xml("attachment 1", old)


@pytest.mark.req("NF-02")
def test_an_oversized_xml_is_refused() -> None:
    huge = D08.replace(b"<cbc:Note>", b"<cbc:Note>" + b"x" * (6 * 1024 * 1024), 1)

    with pytest.raises(EInvoiceRefused, match="larger"):
        read_xml("attachment 1", huge)


@pytest.mark.req("IN-02")
def test_other_xml_is_not_an_einvoice() -> None:
    assert read_xml("attachment 1", b"<order><id>1</id></order>") is None


# Reading the attachments of an email


@pytest.mark.req("IN-02", "NF-02")
def test_the_xrechnung_email_gives_one_einvoice() -> None:
    found = read_einvoices(email_attachments("E08"))

    assert [e.document for e in found.einvoices] == ["attachment 1"]
    assert found.einvoices[0].invoice_number == "BCH-26-10-0057"
    assert found.refused == ()


@pytest.mark.req("IN-02", "NF-02")
def test_the_zugferd_email_gives_the_embedded_einvoice() -> None:
    found = read_einvoices(email_attachments("E09"))

    (einvoice,) = found.einvoices
    assert einvoice.document == "attachment 1, embedded XML"
    assert einvoice.container == "attachment 1"
    assert einvoice.invoice_number == "BB-2026-1243"


@pytest.mark.req("IN-02")
def test_a_plain_pdf_has_no_einvoice() -> None:
    assert read_einvoices(email_attachments("E01")) == read_einvoices([])


@pytest.mark.req("EX-01", "NF-02")
def test_a_refused_einvoice_does_not_stop_the_other_attachments() -> None:
    with_dtd = b'<?xml version="1.0"?><!DOCTYPE x>' + D08.split(b"?>", 1)[1]

    found = read_einvoices([with_dtd, b"not a document", D24, b"%PDF-1.4 broken"])

    assert [e.document for e in found.einvoices] == ["attachment 3"]
    (refused,) = found.refused
    assert refused.document == "attachment 1"
    assert refused.reader == READER
    assert refused.unreadable is not None and "DTD" in refused.unreadable


@pytest.mark.req("NF-02", "NF-01")
def test_the_stored_email_and_attachments_stay_as_received() -> None:
    raw = next(EMAILS.glob("E21_*.eml")).read_bytes()
    store = MemoryDocumentStore()
    stored = store_email(store, raw)
    before = dict(store.files)

    attachments = [store.get(a.document.sha256) for a in stored.attachments]
    found = read_einvoices(attachments)

    assert found.einvoices[0].xml == attachments[0]
    # Reading changes nothing in the store: the XML never replaces the stored original.
    assert store.files == before
    assert store.get(stored.raw.sha256) == raw


# One invoice from an XML and its PDF


@pytest.mark.req("IN-02", "NF-02")
def test_an_xrechnung_and_its_pdf_copy_become_one_invoice() -> None:
    attachments = email_attachments("E21")
    found = read_einvoices(attachments)
    pdfs = [
        reading
        for position, data in enumerate(attachments, start=1)
        if (reading := read_attachment(position, data, LocalReader())) is not None
    ]

    paired, left = pair(found.einvoices, pdfs)

    (invoice,) = paired
    assert invoice.documents == ("attachment 1", "attachment 2")
    assert invoice.pdf is not None and invoice.pdf.document == "attachment 2"
    assert invoice.disagreements == ()
    assert left == []


@pytest.mark.req("IN-02", "NF-02")
def test_a_zugferd_pdf_is_one_invoice_with_its_own_xml() -> None:
    found = read_einvoices([D09_PDF])
    pdf = pdf_reading("attachment 1", invoice_number="BB-2026-1243", gross_amount="687.82")

    paired, left = pair(found.einvoices, [pdf])

    (invoice,) = paired
    assert invoice.documents == ("attachment 1",)
    assert invoice.pdf is pdf and left == []


@pytest.mark.req("IN-02", "NF-02")
@pytest.mark.parametrize(
    ("field", "pdf_value"),
    [
        ("gross_amount", "174.56"),
        ("iban_on_document", "DE89370400440532013000"),
        ("invoice_number", "BCH-26-10-0075"),
    ],
)
def test_a_pdf_that_disagrees_with_its_xml_is_found(field: str, pdf_value: str) -> None:
    einvoice = read(D08)
    values = {
        "invoice_number": "BCH-26-10-0057",
        "issuer_name": "Beispiel Cloud Hosting GmbH",
        "gross_amount": "147.56",
        "iban_on_document": "DE52000000030000700002",
        field: pdf_value,
    }

    paired, left = pair([einvoice], [pdf_reading("attachment 2", **values)])

    (invoice,) = paired
    assert left == []
    (disagreement,) = invoice.disagreements
    assert disagreement.field == field
    assert disagreement.xml_value == EXPECTED["D08"][field]
    assert disagreement.pdf_value == pdf_value


@pytest.mark.req("NF-02")
def test_spaces_and_trailing_zeros_are_no_disagreement() -> None:
    xml = read(D08).reading
    pdf = pdf_reading(
        "attachment 2",
        invoice_number="bch-26-10-0057",
        gross_amount="147.560",
        iban_on_document="DE52 0000 0003 0000 7000 02",
    )

    assert compare(xml, pdf) == ()


@pytest.mark.req("NF-02")
def test_a_value_the_pdf_reader_missed_is_no_disagreement() -> None:
    xml = read(D08).reading
    pdf = pdf_reading("attachment 2", invoice_number="BCH-26-10-0057")

    assert compare(xml, pdf) == ()


@pytest.mark.req("IN-02")
def test_two_vendors_with_the_same_number_stay_two_invoices() -> None:
    einvoice = read(D08)
    other_vendor = pdf_reading(
        "attachment 2",
        invoice_number="BCH-26-10-0057",
        issuer_name="Andere Firma AG",
        vendor_vat_id="DE999999999",
        gross_amount="50.00",
    )

    paired, left = pair([einvoice], [other_vendor])

    assert paired[0].pdf is None and paired[0].disagreements == ()
    assert left == [other_vendor]


@pytest.mark.req("IN-02")
def test_a_second_pdf_invoice_of_the_vendor_stays_its_own_invoice() -> None:
    einvoice = read(D08)
    copy = pdf_reading("attachment 2", invoice_number="BCH-26-10-0057", vendor_vat_id="DE012345602")
    another = pdf_reading(
        "attachment 3", invoice_number="BCH-26-10-0099", vendor_vat_id="DE012345602"
    )

    paired, left = pair([einvoice], [copy, another])

    assert paired[0].pdf is copy and paired[0].disagreements == ()
    assert left == [another]


@pytest.mark.req("IN-02")
def test_unreadable_pdfs_and_letters_are_never_taken_as_the_copy() -> None:
    einvoice = read(D08)
    unreadable = InvoiceReading("attachment 2", "unknown", "local", 1, "", unreadable="blank")
    letter = InvoiceReading("attachment 3", "reminder", "local", 1, "Mahnung")

    paired, left = pair([einvoice], [unreadable, letter])

    assert paired[0].pdf is None
    assert left == [unreadable, letter]


# The validator client


REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1"
    xmlns:s="http://www.xoev.de/de/validator/framework/1/scenarios" valid="{valid}">
  {scenario}
  <rep:validationStepResult id="val-sch.1" valid="{valid}">
    {messages}
  </rep:validationStepResult>
  <rep:assessment><rep:{verdict}><rep:explanation>html</rep:explanation></rep:{verdict}>
  </rep:assessment>
</rep:report>"""
MATCHED = (
    "<rep:scenarioMatched><s:scenario><s:name>EN16931 XRechnung (UBL Invoice)</s:name>"
    "</s:scenario></rep:scenarioMatched>"
)


def report(accepted: bool, messages: str = "", scenario: str = MATCHED) -> bytes:
    return REPORT.format(
        valid=str(accepted).lower(),
        scenario=scenario,
        messages=messages,
        verdict="accept" if accepted else "reject",
    ).encode()


def validator_answering(status: int, body: bytes) -> KositValidator:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.content == D08
        return httpx.Response(status, content=body)

    return KositValidator("http://kosit.test/", httpx.Client(transport=httpx.MockTransport(answer)))


@pytest.mark.req("NF-02")
def test_an_accepted_report_is_read() -> None:
    result = validator_answering(200, report(True)).validate(D08)

    assert result.accepted and result.errors == ()
    assert result.scenario == "EN16931 XRechnung (UBL Invoice)"


@pytest.mark.req("NF-02")
def test_a_rejected_report_names_the_rules() -> None:
    messages = (
        '<rep:message id="1" level="error" code="BR-DE-15">Das Element "Buyer reference" '
        "(BT-10) muss übermittelt werden.</rep:message>"
        '<rep:message id="2" level="warning" code="BR-DE-21">Warnung</rep:message>'
    )
    result = validator_answering(406, report(False, messages)).validate(D08)

    assert not result.accepted
    assert [m.code for m in result.errors] == ["BR-DE-15"]
    assert len(result.messages) == 2


@pytest.mark.req("NF-02")
def test_a_report_without_a_matching_scenario_has_no_scenario() -> None:
    result = validator_answering(406, report(False, scenario="")).validate(D08)

    assert not result.accepted and result.scenario is None


@pytest.mark.req("NF-02")
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b"oops"),
        (200, b"<html>proxy page</html>"),
        (200, b'<!DOCTYPE x><rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1"/>'),
        (200, b'<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1"/>'),
    ],
    ids=["server error", "not a report", "report with dtd", "report without assessment"],
)
def test_a_validator_without_a_usable_answer_is_unavailable(status: int, body: bytes) -> None:
    with pytest.raises(ValidatorUnavailable):
        validator_answering(status, body).validate(D08)


@pytest.mark.req("NF-02")
def test_a_validator_that_cannot_be_reached_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    validator = KositValidator(
        "http://kosit.test/", httpx.Client(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(ValidatorUnavailable):
        validator.validate(D08)


@pytest.mark.req("NF-02")
def test_report_reading_never_trusts_an_accept_without_assessment() -> None:
    body = (
        report(True)
        .replace(b"<rep:accept>", b"<rep:other>")
        .replace(b"</rep:accept>", b"</rep:other>")
    )

    with pytest.raises(ValidatorUnavailable):
        read_report(body)

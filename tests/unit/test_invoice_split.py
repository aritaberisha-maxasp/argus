"""One case per invoice: splitting files page by page and keying each invoice (#30, IN-08).

The database side (cases, links, readings) is in ``tests/integration/test_invoice_split.py``.
"""

import io
from dataclasses import replace
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any

import pypdf
import pytest

from argus.cases.model import CaseKind
from argus.intake.invoices import (
    EmailInvoices,
    Invoicing,
    case_key,
    doubts_about,
    find_invoices,
    to_manual_review,
)
from argus.reading import parse_analyze_result
from argus.reading.fields import InvoiceReading, ReadField, Source
from argus.reading.pages import Analysis, InvoiceSpan, Page
from argus.reading.split import split_file
from argus.sorting.models import Route, SortingDecision
from tests.acceptance.intake import FixtureDocumentReader
from tests.unit.sorting_helpers import EMAILS, FIXTURES, OWN_VAT_IDS, manifest

DOCUMENTS = FIXTURES / "documents"


def invoice_page(number: int, invoice_number: str, issuer: str, vat_id: str) -> Page:
    """A synthetic invoice page in the layout of the fixture invoices."""
    return Page(
        number,
        f"{issuer}\nMusterweg 1, 70000 Musterstadt, DE\nUSt-IdNr. {vat_id}\nMax ASP GmbH\n"
        f"Testallee 1\nRechnung\nNummer: {invoice_number}\nDatum: 12.10.2026\n"
        "Nettobetrag 100,00 EUR\nUSt 19 % 19,00 EUR\nGesamtbetrag 119,00 EUR\n",
    )


def merged_pdf(*names: str) -> bytes:
    """The fixture PDFs as one PDF, one invoice per page, as a supplier might send them."""
    writer = pypdf.PdfWriter()
    for name in names:
        writer.append(str(next(DOCUMENTS.glob(f"{name}_*.pdf"))))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def email_with_pdf(*pdfs: bytes, template: str = "E14") -> bytes:
    """The fixture email ``template`` with its attachments replaced by these PDFs."""
    path = next(EMAILS.glob(f"{template}_*.eml"))
    message = message_from_bytes(path.read_bytes(), policy=policy.default)
    assert isinstance(message, EmailMessage)
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    message.clear_content()
    message.set_content(text)
    for pdf in pdfs:
        message.add_attachment(
            pdf, maintype="application", subtype="pdf", filename="Rechnungen.pdf"
        )
    del message["Message-ID"]
    message["Message-ID"] = "<split.two-invoices-one-pdf@fixtures.argus.example>"
    return message.as_bytes()


class PagesReader:
    """A reader that returns the same pages for any document."""

    name = "pages"

    def __init__(self, analysis: Analysis) -> None:
        self.analysis = analysis

    def read(self, data: bytes, content_type: str) -> Analysis:
        return self.analysis


# AC: a PDF with two invoices on separate pages becomes two cases, checked page by page.


@pytest.mark.req("IN-08")
def test_each_page_that_starts_a_new_invoice_number_starts_a_new_invoice() -> None:
    analysis = Analysis(
        pages=(
            invoice_page(1, "R-1", "Beispiel Reinigung GmbH", "DE012345607"),
            invoice_page(2, "R-2", "Beispiel Reinigung GmbH", "DE012345607"),
        )
    )
    split = split_file(analysis, "attachment 1")
    assert [part.pages for part in split.parts] == [(1,), (2,)]
    assert split.doubts == ()


@pytest.mark.req("IN-08")
def test_a_second_page_of_the_same_invoice_stays_with_it() -> None:
    first = invoice_page(1, "R-1", "Beispiel Reinigung GmbH", "DE012345607")
    analysis = Analysis(
        pages=(
            Page(1, "Deckblatt\nIhre Unterlagen"),  # before the first number
            replace(first, number=2),
            Page(3, "Seite 2\nAllgemeine Geschäftsbedingungen"),  # no number
            Page(4, "Rechnung Seite 3\nNummer: R-1\nÜbertrag"),  # the same number again
            invoice_page(5, "R-2", "Beispiel Reinigung GmbH", "DE012345607"),
        )
    )
    split = split_file(analysis, "attachment 1")
    assert [part.pages for part in split.parts] == [(1, 2, 3, 4), (5,)]


@pytest.mark.req("IN-08", "IN-09")
def test_two_invoice_numbers_on_one_page_cannot_be_split_and_need_a_person() -> None:
    page = invoice_page(1, "R-1", "Beispiel Reinigung GmbH", "DE012345607")
    page = replace(page, text=page.text + "Nummer: R-2\n")
    split = split_file(Analysis(pages=(page,)), "attachment 3")
    assert len(split.parts) == 1
    assert split.doubts == (
        "page 1 of attachment 3 states 2 invoice numbers and cannot be split by page",
    )


@pytest.mark.req("IN-08", "IN-09")
@pytest.mark.parametrize(
    ("invoice_count", "pages", "doubt"),
    [
        (2, 1, "the reader sees 2 invoices in attachment 1, its pages show one"),
        (3, 2, "the reader sees 3 invoices in attachment 1, its pages show 2"),
        (1, 2, "the reader sees 1 invoices in attachment 1, its pages show 2"),
    ],
)
def test_a_reader_that_counts_other_invoices_than_the_pages_sends_the_email_to_a_person(
    invoice_count: int, pages: int, doubt: str
) -> None:
    analysis = Analysis(
        pages=tuple(
            invoice_page(n, f"R-{n}", "Beispiel Reinigung GmbH", "DE012345607")
            for n in range(1, pages + 1)
        ),
        invoice_count=invoice_count,
    )
    assert split_file(analysis, "attachment 1").doubts == (doubt,)


@pytest.mark.req("IN-08")
def test_each_part_gets_the_reader_fields_of_the_invoice_on_its_pages() -> None:
    def number(value: str) -> dict[str, ReadField]:
        return {
            "invoice_number": ReadField(
                "invoice_number", value, Source.DOCUMENT_INTELLIGENCE, 0.99, 1
            )
        }

    analysis = Analysis(
        pages=(
            invoice_page(1, "R-1", "Beispiel Reinigung GmbH", "DE012345607"),
            invoice_page(2, "R-2", "Beispiel Reinigung GmbH", "DE012345607"),
        ),
        fields=number("R-1"),
        invoice_count=2,
        invoices=(
            InvoiceSpan(frozenset({2}), number("R-2")),
            InvoiceSpan(frozenset({1}), number("R-1")),
        ),
    )
    first, second = split_file(analysis, "attachment 1").parts
    assert first.analysis.fields["invoice_number"].value == "R-1"
    assert second.analysis.fields["invoice_number"].value == "R-2"


@pytest.mark.req("IN-08", "IN-09")
def test_a_reader_invoice_across_two_page_invoices_needs_a_person() -> None:
    analysis = Analysis(
        pages=(
            invoice_page(1, "R-1", "Beispiel Reinigung GmbH", "DE012345607"),
            invoice_page(2, "R-2", "Beispiel Reinigung GmbH", "DE012345607"),
        ),
        invoice_count=2,
        invoices=(InvoiceSpan(frozenset({1, 2})), InvoiceSpan(frozenset())),
    )
    assert split_file(analysis, "attachment 1").doubts == (
        "the reader puts one invoice of attachment 1 across two invoices of its pages",
    )


@pytest.mark.req("IN-08")
def test_document_intelligence_gives_the_pages_of_each_invoice_it_sees() -> None:
    def document(number: str, page: int) -> dict[str, Any]:
        return {
            "docType": "invoice",
            "boundingRegions": [{"pageNumber": page, "polygon": []}],
            "fields": {"InvoiceId": {"valueString": number, "content": number, "confidence": 0.9}},
        }

    analysis = parse_analyze_result(
        {
            "pages": [{"pageNumber": 1, "lines": []}, {"pageNumber": 2, "lines": []}],
            "documents": [document("R-1", 1), document("R-2", 2)],
        }
    )
    assert analysis.invoice_count == 2
    assert [span.pages for span in analysis.invoices] == [frozenset({1}), frozenset({2})]
    assert analysis.invoices[1].fields["invoice_number"].value == "R-2"
    assert analysis.fields["invoice_number"].value == "R-1"


@pytest.mark.req("IN-08")
def test_two_invoices_in_one_pdf_are_found_as_two_invoices_of_the_same_attachment() -> None:
    raw = email_with_pdf(merged_pdf("D14", "D15"))
    found = find_invoices(raw, Invoicing(FixtureDocumentReader(), OWN_VAT_IDS))
    assert found.doubts == ()
    assert [(i.source.position, i.source.pages) for i in found.invoices] == [(1, (1,)), (1, (2,))]
    assert [i.source.reading.value("invoice_number") for i in found.invoices] == [
        "R-2026-0932",
        "R-2026-0933",
    ]
    assert [i.source.reading.value("gross_amount") for i in found.invoices] == [
        "309.40",
        "172.55",
    ]


# AC: the multi invoice fixtures create one case per invoice.


@pytest.mark.req("IN-08")
@pytest.mark.parametrize(
    "entry",
    [e for e in manifest() if e["expected_route"] == "invoice_processing"],
    ids=lambda e: e["email_id"],
)
def test_every_incoming_invoice_fixture_holds_the_invoices_of_the_manifest(
    entry: dict[str, str],
) -> None:
    found = find_invoices(
        (EMAILS / entry["file"]).read_bytes(), Invoicing(FixtureDocumentReader(), OWN_VAT_IDS)
    )
    assert found.doubts == ()
    assert len(found.invoices) == int(entry["invoice_count"])


@pytest.mark.req("IN-08")
def test_the_three_invoices_of_e14_are_three_attachments_with_three_keys() -> None:
    raw = next(EMAILS.glob("E14_*.eml")).read_bytes()
    found = find_invoices(raw, Invoicing(FixtureDocumentReader(), OWN_VAT_IDS))
    assert [i.source.position for i in found.invoices] == [1, 2, 3]
    assert len({i.key for i in found.invoices}) == 3
    assert {i.kind for i in found.invoices} == {CaseKind.INCOMING_INVOICE}


@pytest.mark.req("IN-08")
def test_an_e_invoice_and_its_pdf_copy_are_one_invoice() -> None:
    raw = next(EMAILS.glob("E09_*.eml")).read_bytes()  # ZUGFeRD: the XML is inside the PDF
    [invoice] = find_invoices(raw, Invoicing(FixtureDocumentReader(), OWN_VAT_IDS)).invoices
    assert invoice.source.reading.document == "attachment 1, embedded XML"
    assert invoice.source.reading.reader == "einvoice_xml"
    assert [(c.reading.document, c.pages) for c in invoice.copies] == [("attachment 1", (1,))]


@pytest.mark.req("IN-08")
def test_a_credit_note_opens_a_credit_note_case() -> None:
    raw = next(EMAILS.glob("E10_*.eml")).read_bytes()
    [invoice] = find_invoices(raw, Invoicing(FixtureDocumentReader(), OWN_VAT_IDS)).invoices
    assert invoice.kind is CaseKind.INCOMING_CREDIT_NOTE


# AC: cases are keyed on issuer plus invoice number.


def reading(**values: str) -> InvoiceReading:
    return InvoiceReading(
        document="attachment 1",
        kind="invoice",
        reader="test",
        pages=1,
        text="",
        fields={
            name: ReadField(name, value, Source.PAGE_TEXT, page=1) for name, value in values.items()
        },
    )


@pytest.mark.req("IN-08")
def test_the_case_key_is_the_vat_id_or_the_issuer_name_plus_the_number() -> None:
    assert case_key(reading(vendor_vat_id="DE 0123 45607", invoice_number="r 1001")) == (
        "vat:DE012345607|R1001"
    )
    assert case_key(reading(issuer_name="Muster Bürobedarf GmbH", invoice_number="1001")) == (
        "name:muster buerobedarf|1001"
    )
    assert case_key(reading(issuer_name="Muster GmbH")) is None
    assert case_key(reading(invoice_number="1001")) is None


@pytest.mark.req("IN-08")
def test_two_vendors_that_both_use_number_1001_stay_two_invoices() -> None:
    analysis = Analysis(
        pages=(
            invoice_page(1, "1001", "Muster Bürobedarf GmbH", "DE012345601"),
            invoice_page(2, "1001", "Beispiel Reinigung GmbH", "DE012345607"),
        )
    )
    found = find_invoices(
        email_with_pdf(merged_pdf("D14")), Invoicing(PagesReader(analysis), OWN_VAT_IDS)
    )
    assert [i.key for i in found.invoices] == ["vat:DE012345601|1001", "vat:DE012345607|1001"]
    assert found.doubts == ()


@pytest.mark.req("IN-08", "IN-09")
def test_the_same_invoice_twice_in_one_email_needs_a_person() -> None:
    analysis = Analysis(pages=(invoice_page(1, "1001", "Muster Bürobedarf GmbH", "DE012345601"),))
    pdf = merged_pdf("D14")
    found = find_invoices(email_with_pdf(pdf, pdf), Invoicing(PagesReader(analysis), OWN_VAT_IDS))
    assert len(found.invoices) == 1
    assert found.doubts == ("the invoice in the attachment 2, page 1 is in this email twice",)


@pytest.mark.req("IN-08")
def test_a_page_that_restates_its_number_and_issuer_stays_with_its_invoice() -> None:
    page = invoice_page(1, "1001", "Muster Bürobedarf GmbH", "DE012345601")
    analysis = Analysis(pages=(page, replace(page, number=2)))
    found = find_invoices(
        email_with_pdf(merged_pdf("D14")), Invoicing(PagesReader(analysis), OWN_VAT_IDS)
    )
    assert [i.source.pages for i in found.invoices] == [(1, 2)]
    assert found.doubts == ()


@pytest.mark.req("IN-08", "IN-09")
def test_an_invoice_without_a_readable_number_or_issuer_needs_a_person() -> None:
    analysis = Analysis(
        pages=(Page(1, "Rechnung\nVielen Dank für Ihren Auftrag.\nGesamtbetrag 119,00 EUR\n"),)
    )
    found = find_invoices(
        email_with_pdf(merged_pdf("D14")), Invoicing(PagesReader(analysis), OWN_VAT_IDS)
    )
    assert found.invoices == ()
    assert found.doubts == (
        "the invoice in the attachment 1, page 1 has no invoice number or issuer ARGUS can read",
    )


# AC: if the model lists another number of invoices than the documents and pages show, the
# email goes to manual review.


def found_with(count: int) -> EmailInvoices:
    raw = next(EMAILS.glob("E14_*.eml")).read_bytes()
    found = find_invoices(raw, Invoicing(FixtureDocumentReader(), OWN_VAT_IDS))
    return replace(found, invoices=found.invoices[:count])


@pytest.mark.req("IN-08", "IN-09")
@pytest.mark.parametrize("model_invoices", [0, 1, 2, 4])
def test_a_model_that_lists_another_number_of_invoices_sends_the_email_to_a_person(
    model_invoices: int,
) -> None:
    assert doubts_about(found_with(3), model_invoices) == (
        f"the language model lists {model_invoices} invoices, the documents and pages show 3",
    )


@pytest.mark.req("IN-08")
def test_the_same_number_of_invoices_raises_no_doubt() -> None:
    assert doubts_about(found_with(3), 3) == ()


@pytest.mark.req("IN-09")
def test_no_invoices_found_is_never_taken_as_agreement() -> None:
    assert doubts_about(found_with(0), 0) == (
        "the language model lists 0 invoices, the documents and pages show 0",
    )
    assert doubts_about(found_with(1), None) == (
        "the language model did not say how many invoices the email holds",
    )


@pytest.mark.req("IN-09", "NF-03")
def test_a_doubt_turns_the_decision_into_manual_review_with_its_reasons() -> None:
    decision = SortingDecision(
        route=Route.INVOICE_PROCESSING,
        category=None,
        reasons=(),
        model_category=None,
        model_confidence=0.97,
        check_category=None,
        evidence=(),
        model_invoices=2,
    )
    review = to_manual_review(decision, ("the language model lists 2 invoices, ...",))
    assert review.route is Route.MANUAL_REVIEW
    assert review.reasons == ("the language model lists 2 invoices, ...",)
    assert review.audit_details()["model_invoices"] == 2
    assert SortingDecision.from_audit_details(review.audit_details()) == review


# Review of PR 105: Max ASP's own VAT ID is never the issuer's.


def buyer_vat_first(number: int, invoice_number: str, issuer: str, vat_id: str) -> Page:
    """An invoice that prints the customer's VAT ID in the address block, above the supplier's
    own in the footer."""
    return Page(
        number,
        f"{issuer}\nMusterweg 1, 70000 Musterstadt, DE\nMax ASP GmbH\nTestallee 1\n"
        "Ihre USt-IdNr.: DE099000001\nRechnung\n"
        f"Nummer: {invoice_number}\nDatum: 12.10.2026\nGesamtbetrag 119,00 EUR\n"
        f"{issuer}, Geschäftsführung Erika Mustermann, USt-IdNr. {vat_id}\n",
    )


@pytest.mark.req("IN-08")
def test_the_customer_vat_id_printed_first_never_becomes_the_issuer_key() -> None:
    analysis = Analysis(
        pages=(
            buyer_vat_first(1, "1001", "Alpha Technik GmbH", "DE012345611"),
            buyer_vat_first(2, "1001", "Beta Handel GmbH", "DE012345622"),
        )
    )
    found = find_invoices(
        email_with_pdf(merged_pdf("D14")), Invoicing(PagesReader(analysis), OWN_VAT_IDS)
    )
    assert [i.key for i in found.invoices] == ["vat:DE012345611|1001", "vat:DE012345622|1001"]
    assert found.doubts == ()


@pytest.mark.req("IN-08")
def test_a_second_page_with_the_customer_vat_id_stays_with_its_invoice() -> None:
    analysis = Analysis(
        pages=(
            buyer_vat_first(1, "1001", "Alpha Technik GmbH", "DE012345611"),
            Page(2, "Nummer: 1001\nIhre USt-IdNr.: DE099000001\nSeite 2 Übertrag"),
        )
    )
    split = split_file(analysis, "attachment 1", OWN_VAT_IDS)
    assert [part.pages for part in split.parts] == [(1, 2)]


@pytest.mark.req("IN-08")
def test_a_reader_that_names_max_asp_as_the_issuer_is_overruled_by_the_page() -> None:
    own = ReadField("vendor_vat_id", "DE099000001", Source.DOCUMENT_INTELLIGENCE, 0.95, 1)
    analysis = Analysis(
        pages=(buyer_vat_first(1, "1001", "Alpha Technik GmbH", "DE012345611"),),
        fields={"vendor_vat_id": own},
    )
    [invoice] = find_invoices(
        email_with_pdf(merged_pdf("D14")), Invoicing(PagesReader(analysis), OWN_VAT_IDS)
    ).invoices
    assert invoice.source.reading.value("vendor_vat_id") == "DE012345611"


# Review of PR 105: a document that looks like an invoice is never dropped without a trace.


class ByContent:
    """Reads each file from its own analysis, by its bytes."""

    name = "by-content"

    def __init__(self, analyses: dict[bytes, Analysis]) -> None:
        self.analyses = analyses

    def read(self, data: bytes, content_type: str) -> Analysis:
        return self.analyses[data]


@pytest.mark.req("IN-08", "IN-09", "EX-01")
@pytest.mark.parametrize(
    "text",
    [
        "Muster Technik GmbH\nRechnung Nr. 1002 vom 01.10.2026 an Max ASP GmbH\n"
        "Gesamtbetrag 238,00 EUR\n",
        "Muster Technik GmbH\nIhre Unterlagen zum Auftrag vom Oktober 2026\nNr.: 1002\n",
    ],
)
def test_a_document_without_an_invoice_title_but_a_number_or_total_needs_a_person(
    text: str,
) -> None:
    invoice, odd = merged_pdf("D14"), merged_pdf("D15")
    reader = ByContent(
        {
            invoice: Analysis(pages=(invoice_page(1, "1001", "Alpha GmbH", "DE012345611"),)),
            odd: Analysis(pages=(Page(1, text),)),
        }
    )
    found = find_invoices(email_with_pdf(invoice, odd), Invoicing(reader, OWN_VAT_IDS))
    # The model may well list one invoice too, so the count alone would not catch it.
    assert len(found.invoices) == 1
    assert doubts_about(found, 1) == (
        "the attachment 2, page 1 states an invoice number or a total but no document title"
        " ARGUS knows",
    )


@pytest.mark.req("IN-08")
def test_a_document_without_number_or_total_such_as_terms_raises_no_doubt() -> None:
    invoice, terms = merged_pdf("D14"), merged_pdf("D15")
    reader = ByContent(
        {
            invoice: Analysis(pages=(invoice_page(1, "1001", "Alpha GmbH", "DE012345611"),)),
            terms: Analysis(
                pages=(Page(1, "Allgemeine Geschäftsbedingungen\n1. Geltungsbereich\n" * 3),)
            ),
        }
    )
    found = find_invoices(email_with_pdf(invoice, terms), Invoicing(reader, OWN_VAT_IDS))
    assert doubts_about(found, 1) == ()

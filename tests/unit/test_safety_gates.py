"""The safety gates: doubtful emails go to a person, each with its own reason (#71).

ADR 0006 point 7 lists the situations where ARGUS never decides on its own, however sure the
language model is. Every gate has at least one test here with a fixture: one of the fixture
emails E43 to E50, a fixture email with one header or attachment changed, or a synthetic invoice
page read by the real reading code. The deterministic checks that run after the model (ADR 0006
point 5) are tested on their own further down.

Covers: IN-09, PR-03, AC-1
"""

import json
import re
from dataclasses import replace
from email import message_from_bytes, policy
from email.message import EmailMessage
from functools import cache

import pytest

from argus.intake.invoices import EmailInvoices, Invoicing, find_invoices, to_manual_review
from argus.intake.sweep import manual_review_detail
from argus.reading.fields import Flag, InvoiceReading, ReadField, Source
from argus.reading.pages import Analysis, Page
from argus.reading.plausibility import check_plausible, vat_id_valid
from argus.sorting import EmailToSort, RecordedModel, Route, SortingDecision, sort_email
from argus.sorting.checks import check_email
from argus.sorting.gates import LABELS, Gate, GateHit, gate_labels
from tests.unit.sorting_helpers import (
    EMAILS,
    FIXTURES,
    MODEL_ANSWERS,
    OWN_VAT_IDS,
    fixture_config,
    read_fixture_email,
)
from tests.unit.test_invoice_split import PagesReader, email_with_pdf, merged_pdf

CONFIG = fixture_config()
DOCUMENTS = FIXTURES / "documents"

# What Exchange Online writes when SPF, DKIM and DMARC all fail for the From domain.
FAILS = (
    "spf=fail (sender IP is 192.0.2.66) smtp.mailfrom={d}; dkim=none (message not signed) "
    "header.d=none;dmarc=fail action=quarantine header.from={d};compauth=fail reason=000"
)


@cache
def fixture(email_id: str) -> EmailToSort:
    return read_fixture_email(next(EMAILS.glob(f"{email_id}_*.eml")))


def sure_model() -> RecordedModel:
    """The recorded answers, every one of them sure of itself: a gate must hold anyway."""
    recorded = json.loads(MODEL_ANSWERS.read_text(encoding="utf-8"))
    return RecordedModel({key: {**value, "confidence": 0.99} for key, value in recorded.items()})


def failing_sender(email: EmailToSort) -> EmailToSort:
    """The same email, but Exchange says the From domain did not send it."""
    domain = email.sender_address.rpartition("@")[2]
    return replace(
        email, headers={**email.headers, "authentication-results": FAILS.format(d=domain)}
    )


# AC: each of the gates before the model sends the email to manual review with its own reason.

GATES_BEFORE_THE_MODEL = [
    ("E43", Gate.NO_DOCUMENT,
     "the email speaks of an invoice and asks to click a link, but brings no document"),
    ("E44", Gate.QUESTION, "a business partner asks about an invoice"),
    ("E45", Gate.PRO_FORMA, "attachment 1 is a pro forma invoice, not a VAT invoice"),
    ("E46", Gate.UNREADABLE_DOCUMENT, "attachment 1 cannot be read"),
    ("E47", Gate.INVOICE_AND_REMINDER,
     "an invoice and a payment reminder in one email: reminder words in the subject or text"),
    ("E48", Gate.EMPLOYEE_FORWARD, "an employee forwarded an invoice"),
    ("E49", Gate.ADDRESSED_TO_SOMEONE_ELSE,
     "attachment 1 is an invoice that does not name Max ASP GmbH"),
    ("E50", Gate.OWN_INVOICE_NOT_FROM_AGENT,
     "attachment 1 is issued by Max ASP GmbH but was not sent by the invoicing agent"),
]  # fmt: skip


@pytest.mark.req("IN-09", "AC-1")
@pytest.mark.parametrize(("email_id", "gate", "reason"), GATES_BEFORE_THE_MODEL)
def test_each_doubtful_fixture_fires_its_own_gate(email_id: str, gate: Gate, reason: str) -> None:
    checks = check_email(fixture(email_id), CONFIG)
    assert checks.category is None
    assert checks.gates == (GateHit(gate, reason),)
    assert checks.reasons == (reason,)


@pytest.mark.req("IN-09", "AC-1")
@pytest.mark.parametrize(("email_id", "gate", "reason"), GATES_BEFORE_THE_MODEL)
def test_a_gate_sends_the_email_to_review_however_sure_the_model_is(
    email_id: str, gate: Gate, reason: str
) -> None:
    decision = sort_email(fixture(email_id), sure_model(), CONFIG)
    assert decision.route is Route.MANUAL_REVIEW
    assert decision.category is None
    assert decision.gates == (gate.value,)
    assert f"our checks name no category: {reason}" in decision.reasons


@pytest.mark.req("IN-09")
def test_a_sender_who_fails_authentication_fires_its_gate() -> None:
    # E01 is a plain invoice from a known vendor; here Exchange says the domain did not send it.
    email = failing_sender(fixture("E01"))
    decision = sort_email(email, sure_model(), CONFIG)
    assert decision.route is Route.MANUAL_REVIEW
    assert decision.gates == (Gate.SENDER_FAILS_AUTHENTICATION.value,)
    assert any("SPF, DKIM and DMARC do not pass" in reason for reason in decision.reasons)
    # The same email as it arrived goes to invoice processing: the gate made the difference.
    assert sort_email(fixture("E01"), sure_model(), CONFIG).route is Route.INVOICE_PROCESSING


@pytest.mark.req("IN-09")
def test_several_gates_are_all_named_once_each() -> None:
    # A pro forma (E45) from a sender who fails authentication.
    decision = sort_email(failing_sender(fixture("E45")), sure_model(), CONFIG)
    assert decision.gates == (Gate.SENDER_FAILS_AUTHENTICATION.value, Gate.PRO_FORMA.value)


@pytest.mark.req("IN-09")
def test_an_email_without_doubt_fires_no_gate() -> None:
    decision = sort_email(fixture("E01"), sure_model(), CONFIG)
    assert decision.gates == ()
    assert "gates" not in decision.audit_details()


# The gate for an invoice that is only a link (Arita on #25): the details.


def link_only(sender: str, body: str, subject: str = "Ihre Rechnung") -> EmailToSort:
    return replace(
        fixture("E43"), sender_address=sender, subject=subject, body=body, attachments=()
    )


@pytest.mark.req("IN-09")
def test_a_known_vendor_who_sends_an_invoice_only_as_a_link_also_goes_to_review() -> None:
    email = replace(
        fixture("E01"),
        body="Ihre Rechnung steht bereit. Klicken Sie hier: https://portal.example/r/1",
        attachments=(),
    )
    assert check_email(email, CONFIG).gates == (
        GateHit(
            Gate.NO_DOCUMENT,
            "the email speaks of an invoice and asks to click a link, but brings no document",
        ),
    )


@pytest.mark.req("IN-04", "IN-09")
def test_a_link_without_invoice_words_from_an_unknown_sender_stays_other() -> None:
    # E34 is phishing that asks to log in, but speaks of no invoice.
    checks = check_email(fixture("E34"), CONFIG)
    assert checks.gates == ()
    assert checks.category is not None and checks.category.value == "other"


@pytest.mark.req("IN-04", "IN-09")
def test_a_newsletter_with_invoice_words_and_a_link_stays_other() -> None:
    for email_id in ("E32", "E33", "E42"):  # newsletters and webinars about e-invoicing
        checks = check_email(fixture(email_id), CONFIG)
        assert checks.gates == (), email_id
        assert checks.category is not None and checks.category.value == "other", email_id


@pytest.mark.req("IN-09")
def test_a_question_mark_inside_the_link_is_no_question() -> None:
    email = link_only(
        "billing@portal.example",
        "Your invoice is ready. Click here to view and pay: https://portal.example/pay?id=88812",
    )
    assert [hit.gate for hit in check_email(email, CONFIG).gates] == [Gate.NO_DOCUMENT]


@pytest.mark.req("IN-09")
def test_a_question_about_an_invoice_from_someone_not_known_yet_goes_to_review() -> None:
    email = link_only(
        "einkauf@neukunde.example",
        "Guten Tag,\n\nwarum ist auf Ihrer Rechnung MA-2026-0199 die Lieferung doppelt berechnet?",
        subject="Frage zur Rechnung",
    )
    assert check_email(email, CONFIG).gates == (
        GateHit(Gate.QUESTION, "a sender not known yet asks about an invoice"),
    )


# AC: the deterministic checks run after the model. A synthetic invoice page, read by the real
# reading code, stands in for the attachment of a fixture email.

GOOD_PAGE = """Beispiel Reinigung GmbH
Musterweg 1, 70000 Musterstadt, DE
USt-IdNr. DE012345607
Max ASP GmbH
Testallee 1
Rechnung
Nummer: R-1
Datum: 12.10.2026
Leistungszeitraum: 01.10.2026 - 31.10.2026
Nettobetrag 100,00 EUR
USt 19 % 19,00 EUR
Gesamtbetrag 119,00 EUR
Bankverbindung: IBAN DE52 0000 0003 0000 7000 02
"""


def invoices_on(page: str) -> EmailInvoices:
    """Every invoice of a fixture email whose one attachment reads as ``page``."""
    reader = PagesReader(Analysis(pages=(Page(1, page),)))
    return find_invoices(email_with_pdf(merged_pdf("D14")), Invoicing(reader, OWN_VAT_IDS))


@pytest.mark.req("PR-03")
def test_a_plausible_invoice_fires_no_gate() -> None:
    found = invoices_on(GOOD_PAGE)
    assert found.gates == () and found.doubts == ()
    assert len(found.invoices) == 1


@pytest.mark.req("IN-09", "PR-03")
@pytest.mark.parametrize(
    ("change", "gate", "reason"),
    [
        (("Gesamtbetrag 119,00", "Gesamtbetrag 129,00"), Gate.FAILED_PLAUSIBILITY,
         "net 100.00 plus VAT 19.00 is 119.00, not the gross 129.00"),
        (("USt 19 % 19,00 EUR\nGesamtbetrag 119,00", "USt 19 % 7,00 EUR\nGesamtbetrag 107,00"),
         Gate.FAILED_PLAUSIBILITY,
         "VAT 7.00 does not match 19 % of the net 100.00, which is 19.00"),
        (("IBAN DE52 0000 0003 0000 7000 02", "IBAN DE53 0000 0003 0000 7000 02"),
         Gate.FAILED_PLAUSIBILITY, "the IBAN on the invoice has wrong check digits"),
        (("USt-IdNr. DE012345607", "USt-IdNr. DE0123456"), Gate.FAILED_PLAUSIBILITY,
         "the vendor's VAT ID does not have the format of its country"),
    ],
)  # fmt: skip
def test_each_failed_check_after_the_model_fires_a_gate(
    change: tuple[str, str], gate: Gate, reason: str
) -> None:
    page = GOOD_PAGE.replace(*change)
    assert page != GOOD_PAGE
    found = invoices_on(page)
    assert found.gates == (GateHit(gate, f"the invoice in the attachment 1, page 1: {reason}"),)
    assert found.doubts == (found.gates[0].reason,)


@pytest.mark.req("IN-09", "NF-03")
def test_the_reason_for_a_bad_iban_does_not_show_the_iban() -> None:
    found = invoices_on(GOOD_PAGE.replace("DE52 0000", "DE53 0000"))
    assert not re.search(r"DE53|0000 0003|7000", found.doubts[0])


@pytest.mark.req("IN-09", "PR-03")
def test_a_failed_check_turns_the_decision_into_manual_review_with_the_gate() -> None:
    found = invoices_on(GOOD_PAGE.replace("Gesamtbetrag 119,00", "Gesamtbetrag 129,00"))
    routed = sort_email(fixture("E14"), sure_model(), CONFIG)
    assert routed.route is Route.INVOICE_PROCESSING
    decision = to_manual_review(routed, found.doubts, found.gates)
    assert decision.route is Route.MANUAL_REVIEW
    assert decision.gates == (Gate.FAILED_PLAUSIBILITY.value,)
    assert found.doubts[0] in decision.reasons


# The e-invoice gates: a PDF copy that disagrees with its XML, and XML that cannot be read safely.


def email_with(*attachments: tuple[bytes, str, str]) -> bytes:
    """The fixture email E08 with these attachments instead of its own."""
    message = message_from_bytes(next(EMAILS.glob("E08_*.eml")).read_bytes(), policy=policy.default)
    assert isinstance(message, EmailMessage)
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    message.clear_content()
    message.set_content(text)
    for data, subtype, filename in attachments:
        maintype = "text" if subtype == "xml" else "application"
        if maintype == "text":
            message.add_attachment(data.decode("utf-8"), subtype=subtype, filename=filename)
        else:
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


D08_PDF = (DOCUMENTS / "D08_Rechnung_BCH-26-10-0057.pdf").read_bytes()
D08_XML = (DOCUMENTS / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
# The text of the D08 PDF copy, as a reader gets it.
D08_PAGE = """Beispiel Cloud Hosting GmbH
Serverstraße 3, 70002 Beispielstadt, DE
USt-IdNr. DE012345602
Max ASP GmbH
Testallee 1
Rechnung
Nummer: BCH-26-10-0057
Datum: 08.10.2026
Leistungszeitraum: 01.10.2026 - 31.10.2026
Nettobetrag 124,00 EUR
USt 19 % 23,56 EUR
Gesamtbetrag 147,56 EUR
Bankverbindung: IBAN DE52 0000 0003 0000 7000 02
"""


def d08_with_pdf_text(page: str) -> EmailInvoices:
    raw = email_with((D08_PDF, "pdf", "Rechnung.pdf"), (D08_XML, "xml", "xrechnung.xml"))
    reader = PagesReader(Analysis(pages=(Page(1, page),)))
    return find_invoices(raw, Invoicing(reader, OWN_VAT_IDS))


@pytest.mark.req("IN-09", "NF-02")
def test_a_pdf_copy_that_agrees_with_its_xml_fires_no_gate() -> None:
    found = d08_with_pdf_text(D08_PAGE)
    assert found.gates == ()
    assert len(found.invoices) == 1


@pytest.mark.req("IN-09", "NF-02")
def test_a_pdf_copy_that_disagrees_with_its_xml_fires_its_gate() -> None:
    found = d08_with_pdf_text(D08_PAGE.replace("Gesamtbetrag 147,56", "Gesamtbetrag 174,56"))
    assert [hit.gate for hit in found.gates] == [Gate.PDF_DISAGREES_WITH_XML]
    assert found.gates[0].reason == (
        "the PDF copy of the e-invoice in the attachment 2 states another gross amount"
    )


@pytest.mark.req("IN-09", "NF-02", "EX-01")
def test_an_e_invoice_that_cannot_be_read_safely_fires_its_gate() -> None:
    # The D08 XML with a DTD in front: refused, because entities could change its values.
    with_dtd = D08_XML.replace(b"?>", b'?>\n<!DOCTYPE x [<!ENTITY a "1">]>', 1)
    assert with_dtd != D08_XML
    raw = email_with((with_dtd, "xml", "xrechnung.xml"))
    found = find_invoices(raw, Invoicing(PagesReader(Analysis(pages=())), OWN_VAT_IDS))
    assert [hit.gate for hit in found.gates] == [Gate.EINVOICE_UNREADABLE]
    assert found.gates[0].reason == "the e-invoice in the attachment 1 cannot be read"


@pytest.mark.req("IN-03", "IN-09")
def test_a_document_the_reader_cannot_read_fires_its_gate_after_the_model_too() -> None:
    found = invoices_on("")  # the reader gets no text at all out of the attachment
    assert [hit.gate for hit in found.gates] == [Gate.UNREADABLE_DOCUMENT]


# The deterministic checks one by one.


def reading(**values: str) -> InvoiceReading:
    fields = {name: ReadField(name, value, Source.PAGE_TEXT) for name, value in values.items()}
    return InvoiceReading("attachment 1", "invoice", "test", 1, "", fields)


GOOD = {
    "net_amount": "297.50",
    "vat_rate": "19",
    "vat_amount": "56.53",
    "gross_amount": "354.03",
    "iban_on_document": "DE52000000030000700002",
    "vendor_vat_id": "DE012345601",
    "recipient_name": "Max ASP GmbH",
}


@pytest.mark.req("PR-03")
def test_every_incoming_fixture_invoice_passes_the_checks() -> None:
    expected = json.loads((DOCUMENTS / "expected.json").read_text(encoding="utf-8"))
    for document in expected:
        if document["direction"] != "incoming" or document["kind"] == "proforma":
            continue
        values = {
            name: document[name]
            for name in GOOD
            if isinstance(document.get(name), str) and document[name]
        }
        if document["document_id"] == "D34":  # E49: addressed to someone else on purpose
            assert check_plausible(reading(**values), "x") != (), document["document_id"]
            continue
        assert check_plausible(reading(**values), "x") == (), document["document_id"]


@pytest.mark.req("PR-03")
def test_vat_rounded_per_line_may_differ_by_two_cents() -> None:
    assert check_plausible(reading(**{**GOOD, "vat_amount": "56.55", "gross_amount": "354.05"}),
                           "x") == ()  # fmt: skip
    [hit] = check_plausible(
        reading(**{**GOOD, "vat_amount": "56.56", "gross_amount": "354.06"}), "x"
    )
    assert hit.gate is Gate.FAILED_PLAUSIBILITY


@pytest.mark.req("PR-03")
def test_net_plus_vat_must_match_the_gross_to_the_cent() -> None:
    [hit] = check_plausible(reading(**{**GOOD, "gross_amount": "354.04"}), "x")
    assert (
        hit.reason == "the invoice in x: net 297.50 plus VAT 56.53 is 354.03, not the gross 354.04"
    )


@pytest.mark.req("PR-03")
def test_a_credit_note_with_negative_amounts_passes() -> None:
    values = {**GOOD, "net_amount": "-100.00", "vat_amount": "-19.00", "gross_amount": "-119.00"}
    assert check_plausible(reading(**values), "x") == ()


@pytest.mark.req("PR-03")
def test_no_vat_at_a_zero_rate_passes() -> None:
    values = {**GOOD, "vat_rate": "0", "vat_amount": "0.00", "gross_amount": "297.50"}
    assert check_plausible(reading(**values), "x") == ()


@pytest.mark.req("PR-03")
def test_an_invoice_to_someone_else_fires_the_recipient_gate() -> None:
    [hit] = check_plausible(reading(**{**GOOD, "recipient_name": "Max Muster AG"}), "x")
    assert hit == GateHit(
        Gate.ADDRESSED_TO_SOMEONE_ELSE,
        "the invoice in x: it is addressed to someone else, not Max ASP GmbH",
    )
    # Upper or lower case and a full stop after the legal form do not matter.
    assert check_plausible(reading(**{**GOOD, "recipient_name": "MAX ASP GmbH."}), "x") == ()


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    "recipient",
    ["z. Hd. Max ASP GmbH", "Max ASP GmbH, Testallee 1, Musterstadt", "Bill to: Max ASP GmbH",
     "Rechnungsempfänger: Max ASP GmbH, Buchhaltung", "MAX ASP GMBH"],
)  # fmt: skip
def test_a_recipient_line_with_a_label_or_address_is_still_max_asp(recipient: str) -> None:
    assert check_plausible(reading(**{**GOOD, "recipient_name": recipient}), "x") == ()


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    "recipient",
    ["Max Muster AG", "Muster AG c/o Max ASP GmbH", "Max ASPEN GmbH",
     "Beispiel Handel GmbH, Max ASP", "Max ASP Holding AG", "Max ASP GmbH & Co. KG", "Max ASP",
     "Max ASP GmbH Buchhaltung"],
)  # fmt: skip
def test_any_other_name_on_the_recipient_line_fires_the_gate(recipient: str) -> None:
    # Another legal entity gives Max ASP no input VAT (§14 UStG), so only the full legal name
    # passes. A department after it goes to a person too; that is the safe side.
    [hit] = check_plausible(reading(**{**GOOD, "recipient_name": recipient}), "x")
    assert hit.gate is Gate.ADDRESSED_TO_SOMEONE_ELSE


@pytest.mark.req("PR-03")
def test_the_recipient_check_uses_the_company_name_it_is_given() -> None:
    values = reading(**{**GOOD, "recipient_name": "Beispiel Handel GmbH"})
    assert check_plausible(values, "x", own_company="Beispiel Handel GmbH") == ()
    assert check_plausible(values, "x") != ()


@pytest.mark.req("PR-03")
def test_values_that_were_not_read_are_left_to_the_person_who_checks() -> None:
    assert check_plausible(reading(gross_amount="119.00"), "x") == ()
    several = ReadField("gross_amount", "1.00", Source.PAGE_TEXT, flag=Flag.SEVERAL_VALUES)
    values = reading(**{k: v for k, v in GOOD.items() if k != "gross_amount"})
    values = replace(values, fields={**values.fields, "gross_amount": several})
    assert check_plausible(values, "x") == ()


@pytest.mark.req("PR-03")
@pytest.mark.parametrize(
    ("vat_id", "valid"),
    [("DE012345601", True), ("DE 012 345 601", True), ("ATU12345678", True),
     ("NL000000000B01", True), ("GB000000001", True), ("FRXX123456789", True),
     ("CHE-123.456.789 MWST", True), ("IE1234567T", True),
     ("DE01234560", False), ("ATU1234567", False), ("NL000000000", False), ("XX123456789", False),
     ("DE01234560A", False), ("", False)],
)  # fmt: skip
def test_vat_id_formats(vat_id: str, valid: bool) -> None:
    assert vat_id_valid(vat_id) is valid


# What the person and the audit log see.


@pytest.mark.req("IN-09", "NF-01")
def test_the_gates_are_kept_in_the_audit_details_and_read_back() -> None:
    decision = sort_email(fixture("E45"), sure_model(), CONFIG)
    details = decision.audit_details()
    assert details["gates"] == ["pro_forma"]
    assert SortingDecision.from_audit_details(details).gates == ("pro_forma",)


@pytest.mark.req("IN-09")
def test_the_person_sees_the_gate_first_in_the_queue() -> None:
    decision = sort_email(fixture("E43"), sure_model(), CONFIG)
    detail = manual_review_detail(decision)
    assert detail.startswith(
        "Safety gate: mentions an invoice but brings no document. ARGUS could not sort"
    )
    for reason in decision.reasons:
        assert reason in detail


@pytest.mark.req("IN-09")
def test_every_gate_has_a_label_and_unknown_names_are_shown_as_they_are() -> None:
    assert set(LABELS) == set(Gate)
    assert gate_labels(("pro_forma", "pro_forma", "something_new")) == (
        "pro forma invoice; something_new"
    )

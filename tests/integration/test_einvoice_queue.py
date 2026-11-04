"""An e-invoice that cannot be trusted sends its email to manual review with the reason.

Covers: IN-02, NF-02, EX-01, NF-01, NF-03
"""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import list_open_exceptions, unit_of_work
from argus.einvoice import (
    EInvoice,
    EInvoiceWithCopy,
    Validation,
    ValidationMessage,
    pair,
    queue_failed_checks,
    read_einvoices,
    read_xml,
)
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from argus.reading import Flag, InvoiceReading, ReadField, Source, queue_unreadable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
D08 = (FIXTURES / "documents" / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
X01 = (FIXTURES / "einvoice" / "X01_xrechnung_cii_BCH-26-11-0061.xml").read_bytes()
INTAKE = Actor.system("intake")
OTHER_IBAN = "DE89370400440532013000"
SCENARIO = "EN16931 XRechnung (UBL Invoice)"
ACCEPTED = Validation(accepted=True, scenario=SCENARIO)
REJECTED = Validation(
    accepted=False,
    scenario=SCENARIO,
    messages=(
        ValidationMessage("error", "BR-DE-15", "Buyer reference (BT-10) fehlt."),
        ValidationMessage("warning", "BR-DE-21", "Nur eine Warnung."),
    ),
)


def new_email(app: Engine, graph_id: str) -> int:
    with unit_of_work(app, INTAKE) as db:
        record, _ = see_email(db, InboxMessage(graph_id, None, None))
    return record.id


def einvoice(document: str = "attachment 1", data: bytes = D08) -> EInvoice:
    found = read_xml(document, data)
    assert found is not None
    return found


def pdf_copy(document: str = "attachment 2", **changes: str) -> InvoiceReading:
    values = {
        "invoice_number": "BCH-26-10-0057",
        "issuer_name": "Beispiel Cloud Hosting GmbH",
        "gross_amount": "147.56",
        "iban_on_document": "DE52000000030000700002",
        **changes,
    }
    fields = {
        name: ReadField(name, value, Source.PAGE_TEXT, page=1, words=value)
        for name, value in values.items()
    }
    return InvoiceReading(document, "invoice", "local", 1, "text", fields)


def queue(
    app: Engine, email_id: int, paired: list[EInvoiceWithCopy], validation: Validation = ACCEPTED
) -> int | None:
    validations = {i.einvoice.document: validation for i in paired}
    with unit_of_work(app, INTAKE) as db:
        return queue_failed_checks(db, email_id, paired, validations)


def open_exceptions(app: Engine, email_id: int) -> list[Any]:
    with unit_of_work(app, INTAKE) as db:
        return [e for e in list_open_exceptions(db) if e.email_id == email_id]


def audit_details(app: Engine, exception_id: int) -> dict[str, Any]:
    with unit_of_work(app, INTAKE) as db:
        details: dict[str, Any] = db.execute(
            text("select details from audit_event where exception_id = :id"), {"id": exception_id}
        ).scalar_one()
    return details


@pytest.mark.req("IN-02", "NF-02", "EX-01")
def test_a_pdf_that_disagrees_with_its_xml_goes_to_manual_review_with_both_values(
    app: Engine,
) -> None:
    email_id = new_email(app, "AAk-einvoice-mismatch")
    paired, _ = pair([einvoice()], [pdf_copy(gross_amount="174.56", iban_on_document=OTHER_IBAN)])

    exception_id = queue(app, email_id, paired)

    (entry,) = open_exceptions(app, email_id)
    assert entry.id == exception_id and entry.case_id is None
    assert entry.reason is ExceptionReason.INVOICE_CHECK_FAILED
    assert "total: 147.56 in the XML, 174.56 on the PDF" in entry.detail
    # Both IBANs are shown, masked like everywhere in the queue (PR #86 review).
    assert "IBAN: DE****************0002 in the XML, DE****************3000 on the PDF" in (
        entry.detail
    )
    assert OTHER_IBAN not in entry.detail and "DE52000000030000700002" not in entry.detail


@pytest.mark.req("EX-01", "NF-01", "NF-03")
def test_the_audit_log_gets_the_fields_never_the_values(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-mismatch-audit")
    paired, _ = pair([einvoice()], [pdf_copy(gross_amount="174.56", iban_on_document=OTHER_IBAN)])

    exception_id = queue(app, email_id, paired)
    assert exception_id is not None

    facts = audit_details(app, exception_id)["facts"]
    assert facts == {
        "checks": ["pdf_matches_xml"],
        "documents": ["attachment 1"],
        "copies": ["attachment 2"],
        "fields": ["gross_amount", "iban_on_document"],
        "scenarios": [],
        "rules": [],
        "missing_items": [],
        "formal_rules": [],
    }
    assert "174.56" not in json.dumps(facts) and "3000" not in json.dumps(facts)


@pytest.mark.req("EX-01")
def test_ibans_that_differ_only_in_hidden_digits_are_called_out(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-hidden-digits")
    paired, _ = pair([einvoice()], [pdf_copy(iban_on_document="DE07000000040000700002")])

    queue(app, email_id, paired)

    (entry,) = open_exceptions(app, email_id)
    assert "they differ in the hidden digits" in entry.detail


@pytest.mark.req("EX-01", "NF-02")
def test_a_pdf_with_several_ibans_shows_each_of_them_masked(app: Engine) -> None:
    # PR #95 review and QA: the PDF states the XML's IBAN and another one.
    email_id = new_email(app, "AAk-einvoice-several-ibans")
    fields = dict(pdf_copy().fields)
    fields["iban_on_document"] = ReadField("iban_on_document", flag=Flag.SEVERAL_VALUES)
    page = f"IBAN DE52 0000 0003 0000 7000 02\nBitte nur noch auf {OTHER_IBAN} zahlen"
    copy = InvoiceReading("attachment 2", "invoice", "local", 1, page, fields)
    paired, _ = pair([einvoice()], [copy])

    queue(app, email_id, paired)

    (entry,) = open_exceptions(app, email_id)
    assert (
        "IBAN: DE****************0002 in the XML, but the PDF states 2 IBANs: "
        "DE****************0002 (the one in the XML), DE****************3000" in entry.detail
    )
    assert OTHER_IBAN not in entry.detail


@pytest.mark.req("EX-01")
def test_queueing_twice_opens_one_exception_and_agreement_opens_none(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-twice")
    mismatch, _ = pair([einvoice()], [pdf_copy(invoice_number="BCH-26-10-0075")])
    agreeing, _ = pair([einvoice()], [pdf_copy()])

    first = queue(app, email_id, mismatch)
    second = queue(app, email_id, mismatch)

    assert first is not None and first == second
    assert queue(app, new_email(app, "AAk-einvoice-agreeing"), agreeing) is None


@pytest.mark.req("NF-02", "EX-01")
def test_an_einvoice_the_validator_rejects_goes_to_manual_review(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-rejected")
    paired, _ = pair([einvoice()], [])

    exception_id = queue(app, email_id, paired, REJECTED)
    again = queue(app, email_id, paired, REJECTED)

    (entry,) = open_exceptions(app, email_id)
    assert exception_id is not None and again == exception_id == entry.id
    assert entry.reason is ExceptionReason.INVOICE_CHECK_FAILED
    assert "BR-DE-15" in entry.detail and "BR-DE-21" not in entry.detail
    assert audit_details(app, exception_id)["facts"] == {
        "checks": ["kosit_validation"],
        "documents": ["attachment 1"],
        "copies": [],
        "fields": [],
        "scenarios": [SCENARIO],
        "rules": ["BR-DE-15"],
        "missing_items": [],
        "formal_rules": [],
    }


@pytest.mark.req("NF-02", "EX-01")
def test_a_mismatch_and_a_rejection_on_one_email_make_one_exception(app: Engine) -> None:
    # PR #95 review: exception_open_once allows one open invoice_check_failed per email.
    email_id = new_email(app, "AAk-einvoice-mismatch-and-rejected")
    paired, _ = pair([einvoice()], [pdf_copy(gross_amount="174.56")])

    exception_id = queue(app, email_id, paired, REJECTED)

    (entry,) = open_exceptions(app, email_id)
    assert entry.id == exception_id
    assert "total: 147.56 in the XML, 174.56 on the PDF" in entry.detail
    assert "failed the official check" in entry.detail and "BR-DE-15" in entry.detail
    assert exception_id is not None
    facts = audit_details(app, exception_id)["facts"]
    assert facts["checks"] == ["kosit_validation", "pdf_matches_xml"]
    assert facts["documents"] == ["attachment 1"] and facts["rules"] == ["BR-DE-15"]


@pytest.mark.req("NF-02", "EX-01")
def test_two_mismatched_einvoices_on_one_email_make_one_exception(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-two-mismatches")
    paired, left = pair(
        [einvoice(), einvoice("attachment 3", X01)],
        [
            pdf_copy(gross_amount="174.56"),
            pdf_copy("attachment 4", invoice_number="BCH-26-11-0061", gross_amount="279.50"),
        ],
    )
    assert left == [] and all(i.disagreements for i in paired)

    exception_id = queue(app, email_id, paired)

    (entry,) = open_exceptions(app, email_id)
    assert entry.id == exception_id
    assert "(attachment 2) and the e-invoice (attachment 1)" in entry.detail
    assert "(attachment 4) and the e-invoice (attachment 3)" in entry.detail
    assert "total: 297.50 in the XML, 279.50 on the PDF" in entry.detail
    assert exception_id is not None
    facts = audit_details(app, exception_id)["facts"]
    assert facts["documents"] == ["attachment 1", "attachment 3"]
    assert facts["copies"] == ["attachment 2", "attachment 4"]


@pytest.mark.req("NF-02")
def test_an_accepted_einvoice_goes_nowhere_near_the_queue(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-accepted")
    paired, _ = pair([einvoice()], [pdf_copy()])

    assert queue(app, email_id, paired, ACCEPTED) is None
    assert open_exceptions(app, email_id) == []


@pytest.mark.req("NF-02")
def test_an_einvoice_without_a_validation_is_never_taken_as_passed(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-unchecked")
    paired, _ = pair([einvoice()], [pdf_copy()])

    with unit_of_work(app, INTAKE) as db, pytest.raises(ValueError, match="attachment 1"):
        queue_failed_checks(db, email_id, paired, {})


@pytest.mark.req("NF-02", "EX-01")
def test_an_xml_with_a_dtd_joins_the_unreadable_documents(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-dtd")
    with_dtd = b'<?xml version="1.0"?><!DOCTYPE x>' + D08.split(b"?>", 1)[1]
    found = read_einvoices([with_dtd])

    with unit_of_work(app, INTAKE) as db:
        exception_id = queue_unreadable(db, email_id, found.refused)
        assert exception_id is not None
        entry = next(e for e in list_open_exceptions(db) if e.id == exception_id)

    assert entry.reason is ExceptionReason.UNREADABLE_DOCUMENT
    assert entry.detail.startswith("The attachment 1 could not be read")
    assert "DTD" in entry.detail
    assert audit_details(app, exception_id)["facts"] == {
        "documents": ["attachment 1"],
        "readers": ["einvoice_xml"],
    }

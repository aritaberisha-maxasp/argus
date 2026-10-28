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
    Validation,
    ValidationMessage,
    pair,
    queue_disagreements,
    queue_rejected,
    read_einvoices,
    read_xml,
)
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from argus.reading import InvoiceReading, ReadField, Source, queue_unreadable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
D08 = (FIXTURES / "documents" / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
INTAKE = Actor.system("intake")
OTHER_IBAN = "DE89370400440532013000"


def new_email(app: Engine, graph_id: str) -> int:
    with unit_of_work(app, INTAKE) as db:
        record, _ = see_email(db, InboxMessage(graph_id, None, None))
    return record.id


def einvoice() -> EInvoice:
    found = read_xml("attachment 1", D08)
    assert found is not None
    return found


def pdf_copy(**changes: str) -> InvoiceReading:
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
    return InvoiceReading("attachment 2", "invoice", "local", 1, "text", fields)


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

    with unit_of_work(app, INTAKE) as db:
        (exception_id,) = queue_disagreements(db, email_id, paired)
        entry = next(e for e in list_open_exceptions(db) if e.id == exception_id)

    assert entry.email_id == email_id and entry.case_id is None
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

    with unit_of_work(app, INTAKE) as db:
        (exception_id,) = queue_disagreements(db, email_id, paired)

    facts = audit_details(app, exception_id)["facts"]
    assert facts == {
        "check": "pdf_matches_xml",
        "document": "attachment 1",
        "copy": "attachment 2",
        "fields": ["gross_amount", "iban_on_document"],
    }
    assert "174.56" not in json.dumps(facts) and "3000" not in json.dumps(facts)


@pytest.mark.req("EX-01")
def test_ibans_that_differ_only_in_hidden_digits_are_called_out(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-hidden-digits")
    same_ending = "DE07000000040000700002"
    paired, _ = pair([einvoice()], [pdf_copy(iban_on_document=same_ending)])

    with unit_of_work(app, INTAKE) as db:
        (exception_id,) = queue_disagreements(db, email_id, paired)
        entry = next(e for e in list_open_exceptions(db) if e.id == exception_id)

    assert "they differ in the hidden digits" in entry.detail


@pytest.mark.req("EX-01")
def test_queueing_twice_opens_one_exception_and_agreement_opens_none(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-twice")
    mismatch, _ = pair([einvoice()], [pdf_copy(invoice_number="BCH-26-10-0075")])
    agreeing, _ = pair([einvoice()], [pdf_copy()])

    with unit_of_work(app, INTAKE) as db:
        first = queue_disagreements(db, email_id, mismatch)
    with unit_of_work(app, INTAKE) as db:
        second = queue_disagreements(db, email_id, mismatch)
        assert queue_disagreements(db, email_id, agreeing) == []

    assert first == second and len(first) == 1


@pytest.mark.req("NF-02", "EX-01")
def test_an_einvoice_the_validator_rejects_goes_to_manual_review(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-rejected")
    validation = Validation(
        accepted=False,
        scenario="EN16931 XRechnung (UBL Invoice)",
        messages=(
            ValidationMessage("error", "BR-DE-15", "Buyer reference (BT-10) fehlt."),
            ValidationMessage("warning", "BR-DE-21", "Nur eine Warnung."),
        ),
    )

    with unit_of_work(app, INTAKE) as db:
        exception_id = queue_rejected(db, email_id, einvoice(), validation)
        again = queue_rejected(db, email_id, einvoice(), validation)
        entry = next(e for e in list_open_exceptions(db) if e.id == exception_id)

    assert exception_id is not None and again == exception_id
    assert entry.reason is ExceptionReason.INVOICE_CHECK_FAILED
    assert "BR-DE-15" in entry.detail and "BR-DE-21" not in entry.detail
    assert audit_details(app, exception_id)["facts"] == {
        "check": "kosit_validation",
        "document": "attachment 1",
        "scenario": "EN16931 XRechnung (UBL Invoice)",
        "rules": ["BR-DE-15"],
    }


@pytest.mark.req("NF-02")
def test_an_accepted_einvoice_goes_nowhere_near_the_queue(app: Engine) -> None:
    email_id = new_email(app, "AAk-einvoice-accepted")
    accepted = Validation(accepted=True, scenario="EN16931 XRechnung (UBL Invoice)")

    with unit_of_work(app, INTAKE) as db:
        assert queue_rejected(db, email_id, einvoice(), accepted) is None
        assert [e for e in list_open_exceptions(db) if e.email_id == email_id] == []


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

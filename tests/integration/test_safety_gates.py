"""The safety gates through the whole sweep, with a real PostgreSQL (#71).

The gates themselves are tested in ``tests/unit/test_safety_gates.py``. Here: the gate names are
kept with the email and in the ``email_classified`` audit event, the person in the exception
queue reads which gate stopped the email, and a gate that fires after the model (a failed
plausibility check) opens no case.

Covers: IN-09, PR-03, NF-01
"""

from dataclasses import replace

import pytest
from sqlalchemy import Engine

from argus.intake.invoices import Invoicing
from argus.intake.sweep import DocumentArchive, sweep
from argus.reading.pages import Analysis, Page
from tests.integration.test_email_sorting import (
    CountingModel,
    events,
    open_exceptions,
    record_of,
    run,
)
from tests.integration.test_invoice_split import (
    TWO_IN_ONE_PDF,
    cases_of,
    model,
    record_for,
    setup,
)
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
    fake_graph_with,
)
from tests.unit.sorting_helpers import OWN_VAT_IDS
from tests.unit.test_invoice_split import PagesReader, email_with_pdf, merged_pdf
from tests.unit.test_safety_gates import GOOD_PAGE, d08_with_dtd

LINK_ONLY = "E43"


@pytest.mark.req("IN-09", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_invoice_only_as_a_link_goes_to_review_with_its_gate_in_the_trail(app: Engine) -> None:
    fake = fake_graph_with(LINK_ONLY)
    documents, sorting, graph = run(fake, CountingModel())

    sweep(app, graph, DocumentArchive(documents), sorting)

    record = record_of(app, graph, LINK_ONLY)
    assert record.folder == "ARGUS/Manual review"
    [classified] = events(app, record.id, "email_classified")
    assert classified["details"]["route"] == "manual_review"
    assert classified["details"]["gates"] == ["no_document"]
    [exception] = open_exceptions(app, record.id)
    assert exception["reason"] == "uncertain_classification"
    assert exception["detail"].startswith(
        "Safety gate: mentions an invoice but brings no document."
    )


@pytest.mark.req("IN-09", "PR-03", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_invoice_that_fails_a_plausibility_check_opens_no_case(app: Engine) -> None:
    # E14's text and model answer, with one attachment whose totals do not add up.
    raw = email_with_pdf(merged_pdf("D14"))
    page = GOOD_PAGE.replace("Gesamtbetrag 119,00", "Gesamtbetrag 129,00")
    documents, sorting, graph = setup([raw], model({TWO_IN_ONE_PDF: 1}))
    reader = PagesReader(Analysis(pages=(Page(1, page),)))
    sorting = replace(sorting, invoicing=Invoicing(reader, OWN_VAT_IDS))

    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, raw)
    assert cases_of(app, email_id) == []
    [classified] = events(app, email_id, "email_classified")
    assert classified["details"]["route"] == "manual_review"
    assert "failed_plausibility" in classified["details"]["gates"]
    reason = (
        "the invoice in the attachment 1, page 1: net 100.00 plus VAT 19.00 is 119.00, "
        "not the gross 129.00"
    )
    assert reason in classified["details"]["reasons"]
    [exception] = open_exceptions(app, email_id)
    assert "invoice data fails a plausibility check" in exception["detail"]


@pytest.mark.req("IN-09", "NF-02", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_refused_plain_xml_e_invoice_goes_to_review_with_its_own_gate(app: Engine) -> None:
    # QA on PR 108: a plain XRechnung with a DTD, through the whole sweep with invoice reading on.
    raw = d08_with_dtd()
    documents, sorting, graph = setup([raw], model())

    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, raw)
    assert cases_of(app, email_id) == []
    [classified] = events(app, email_id, "email_classified")
    assert classified["details"]["route"] == "manual_review"
    assert classified["details"]["gates"] == ["einvoice_unreadable"]
    [exception] = open_exceptions(app, email_id)
    assert exception["detail"].startswith("Safety gate: e-invoice cannot be read safely.")

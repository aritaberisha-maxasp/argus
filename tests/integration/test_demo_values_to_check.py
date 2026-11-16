"""The demo email E51 ends in the exception queue with the correction form, and a correction
lets it carry on (#112 in the demo, EX-01, IN-10).

E51 is a demo only email (``tests/fixtures/emails/demo``): an invoice from Beispiel Reinigung
GmbH written in the email text, whose amounts carry no currency. The sweep runs on it as in
Azure, with a language model that proposes the missing amounts and EUR as a model might. ARGUS
must not take them, because the words the model quotes do not state a currency.
"""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import Actor, CaseStatus, ExceptionReason
from argus.cases.store import unit_of_work
from argus.demo.data import DEMO_ONLY
from argus.intake.corrections import CorrectedBy, Correction, correct_flagged_values
from argus.intake.invoices import Invoicing
from argus.intake.sweep import DocumentArchive, sweep
from argus.reading import Page
from argus.reading.language_model import ModelField
from argus.sorting import RecordedModel
from tests.integration.test_invoice_split import record_for, setup
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
)
from tests.unit.sorting_helpers import EMAILS, OWN_VAT_IDS

pytestmark = pytest.mark.usefixtures("clean", "live")

E51 = EMAILS / DEMO_ONLY / "E51_rechnung-ohne-waehrung.eml"
E51_ID = "<E51.rechnung-ohne-waehrung@fixtures.argus.example>"
# What the sorting model says about E51, in the format of tests/fixtures/model-answers.
ANSWER = {
    "category": "incoming_invoice",
    "confidence": 0.95,
    "invoices": [
        {"issuer": "Beispiel Reinigung GmbH", "number": "R-2026-0961", "document": "body"}
    ],
    "evidence": [
        "Rechnung R-2026-0961",
        "erhalten Sie die Rechnung diesmal direkt in dieser E-Mail",
    ],
}
BENJAMIN = CorrectedBy(
    Actor.user("51515151-5151-4515-8515-515151515151"),  # made up
    "Benjamin Test",
    "benjamin@max-asp-test.example",
)


class GuessingModel:
    """Proposes the amounts and a currency the way a language model might, quoting the lines
    of the email. None of them states a currency, so ARGUS confirms none (ADR 0006 point 3)."""

    def find_fields(self, pages: Sequence[Page], names: Sequence[str]) -> tuple[ModelField, ...]:
        proposals = {
            "net_amount": ("785,00", "Nettobetrag: 785,00"),
            "vat_amount": ("149,15", "USt 19 %: 149,15"),
            "vat_rate": ("19", "USt 19 %: 149,15"),
            "gross_amount": ("934,15", "Gesamtbetrag: 934,15"),
            "currency": ("EUR", "Gesamtbetrag: 934,15"),
        }
        return tuple(
            ModelField(name=name, value=value, page=1, words=words)
            for name, (value, words) in proposals.items()
            if name in names
        )


def case_of(app: Engine, email_id: int) -> dict[str, Any]:
    with app.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "select id, status, document_number, gross_minor, currency, duplicate_key"
                    " from invoice_case where source_email_id = :id"
                ),
                {"id": email_id},
            )
            .mappings()
            .one()
        )


def open_exceptions_of(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select id, reason, detail, facts from exception_item"
                " where case_id = :id and resolved_at is null order by id"
            ),
            {"id": case_id},
        ).mappings()
        return [dict(row) for row in rows]


@pytest.mark.req("EX-01", "IN-10")
def test_e51_lands_in_the_queue_for_values_to_check_and_a_correction_lets_it_carry_on(
    app: Engine,
) -> None:
    raw = E51.read_bytes()
    documents, sorting, graph = setup([raw], RecordedModel({E51_ID: ANSWER}))
    assert sorting.invoicing is not None
    sorting = replace(
        sorting, invoicing=Invoicing(sorting.invoicing.reader, OWN_VAT_IDS, GuessingModel())
    )
    sweep(app, graph, DocumentArchive(documents), sorting)

    case = case_of(app, record_for(app, raw))
    assert case["document_number"] == "R-2026-0961"
    assert (case["gross_minor"], case["currency"], case["duplicate_key"]) == (None, None, None)
    assert case["status"] == CaseStatus.EXCEPTION
    [exception] = open_exceptions_of(app, case["id"])
    assert exception["reason"] == ExceptionReason.VALUES_TO_CHECK
    assert exception["facts"] == {"fields": ["gross_amount", "currency"]}
    assert "the gross amount and the currency" in exception["detail"]

    # Benjamin looks at the email and enters what it says, with EUR as Max ASP agreed with
    # the vendor. The case gets its key and leaves the queue.
    with unit_of_work(app, BENJAMIN.actor) as db:
        corrected = correct_flagged_values(
            db,
            exception["id"],
            Correction("R-2026-0961", 93415, "EUR", "Amount from the email, currency agreed"),
            BENJAMIN,
        )
    assert corrected.case_id == case["id"] and corrected.duplicate_of is None
    after = case_of(app, record_for(app, raw))
    assert (after["gross_minor"], after["currency"]) == (93415, "EUR")
    assert after["duplicate_key"] is not None
    assert after["status"] != CaseStatus.EXCEPTION
    assert open_exceptions_of(app, case["id"]) == []

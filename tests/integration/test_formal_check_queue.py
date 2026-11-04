"""An invoice that fails the §14 UStG check goes to the exception queue and names what is missing.

Covers: PR-03, EX-01, NF-01, NF-03
"""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import OpenException, list_open_exceptions, unit_of_work
from argus.einvoice import Validation, pair, queue_failed_checks, read_xml
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from argus.reading import (
    FormalCheck,
    RecordedDocumentIntelligence,
    check_invoices,
    read_attachment,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENTS = FIXTURES / "documents"
INTAKE = Actor.system("intake")
# E49: invoice R-2026-0950, addressed to Max Muster AG instead of Max ASP GmbH.
D34 = DOCUMENTS / "D34_Rechnung_R-2026-0950.pdf"
D01 = DOCUMENTS / "D01_Rechnung_BB-2026-1187.pdf"
D08 = (DOCUMENTS / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()


def new_email(app: Engine, graph_id: str) -> int:
    with unit_of_work(app, INTAKE) as db:
        record, _ = see_email(db, InboxMessage(graph_id, None, None))
    return record.id


def checks(*paths: Path) -> list[FormalCheck]:
    """Read outside any transaction, as the sweep does (PR #79 review)."""
    reader = RecordedDocumentIntelligence.from_directory(FIXTURES / "document-intelligence")
    readings = []
    for position, path in enumerate(paths, start=1):
        reading = read_attachment(position, path.read_bytes(), reader)
        assert reading is not None
        readings.append(reading)
    return check_invoices(readings)


def queue(app: Engine, email_id: int, formal: list[FormalCheck]) -> int | None:
    with unit_of_work(app, INTAKE) as db:
        return queue_failed_checks(db, email_id, [], {}, formal)


def open_for(app: Engine, email_id: int) -> list[OpenException]:
    with unit_of_work(app, INTAKE) as db:
        return [e for e in list_open_exceptions(db) if e.email_id == email_id]


def audit_facts(app: Engine, exception_id: int) -> dict[str, Any]:
    with unit_of_work(app, INTAKE) as db:
        details: dict[str, Any] = db.execute(
            text("select details from audit_event where exception_id = :id"), {"id": exception_id}
        ).scalar_one()
    facts: dict[str, Any] = details["facts"]
    return facts


@pytest.mark.req("PR-03", "EX-01")
def test_the_invoice_to_another_company_lands_in_the_queue_naming_the_missing_item(
    app: Engine,
) -> None:
    email_id = new_email(app, "AAk-ustg14-e49")

    exception_id = queue(app, email_id, checks(D34))

    (entry,) = open_for(app, email_id)
    assert entry.id == exception_id and entry.case_id is None
    assert entry.reason is ExceptionReason.INVOICE_CHECK_FAILED
    assert entry.detail.startswith("Failed §14 UStG check: the attachment 1")
    assert "Max ASP as the recipient (the invoice is addressed to Max Muster AG" in entry.detail


@pytest.mark.req("PR-03", "EX-01", "NF-01", "NF-03")
def test_the_audit_log_gets_the_item_names_never_the_values(app: Engine) -> None:
    email_id = new_email(app, "AAk-ustg14-audit")

    exception_id = queue(app, email_id, checks(D01, D34))
    assert exception_id is not None

    facts = audit_facts(app, exception_id)
    assert facts == {
        "checks": ["ustg14"],
        "documents": ["attachment 2"],
        "copies": [],
        "fields": [],
        "scenarios": [],
        "rules": [],
        "missing_items": ["recipient"],
    }
    assert "Muster" not in json.dumps(facts)


@pytest.mark.req("PR-03")
def test_complete_invoices_open_nothing_and_a_repeat_opens_no_second_entry(app: Engine) -> None:
    email_id = new_email(app, "AAk-ustg14-repeat")

    assert queue(app, email_id, checks(D01)) is None
    first = queue(app, email_id, checks(D34))
    second = queue(app, email_id, checks(D34))

    assert first is not None and first == second
    assert len(open_for(app, email_id)) == 1


@pytest.mark.req("PR-03", "EX-01")
def test_a_failed_einvoice_check_and_a_failed_section_14_check_share_one_entry(
    app: Engine,
) -> None:
    email_id = new_email(app, "AAk-ustg14-with-einvoice")
    found = read_xml("attachment 2", D08)
    assert found is not None
    paired, _ = pair([found], [])
    rejected = Validation(accepted=False, scenario="EN16931 XRechnung (UBL Invoice)")

    with unit_of_work(app, INTAKE) as db:
        exception_id = queue_failed_checks(
            db, email_id, paired, {"attachment 2": rejected}, checks(D34)
        )

    (entry,) = open_for(app, email_id)
    assert entry.id == exception_id
    assert "The e-invoice (attachment 2) failed the official check" in entry.detail
    assert "Failed §14 UStG check: the attachment 1" in entry.detail
    facts = audit_facts(app, entry.id)
    assert facts["checks"] == ["kosit_validation", "ustg14"]
    assert facts["documents"] == ["attachment 2", "attachment 1"]

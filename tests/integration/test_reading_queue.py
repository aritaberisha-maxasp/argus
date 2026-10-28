"""An unreadable document goes to the exception queue with the reason unreadable document.

Covers: IN-03, EX-01, EX-02, NF-01, NF-03
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import list_open_exceptions, unit_of_work
from argus.intake.graph import InboxMessage
from argus.intake.records import see_email
from argus.reading import RecordedDocumentIntelligence, queue_unreadable, read_attachment

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
INTAKE = Actor.system("intake")


def new_email(app: Engine, graph_id: str) -> int:
    with unit_of_work(app, INTAKE) as db:
        record, _ = see_email(db, InboxMessage(graph_id, None, None))
    return record.id


def recorded() -> RecordedDocumentIntelligence:
    return RecordedDocumentIntelligence.from_directory(FIXTURES / "document-intelligence")


@pytest.mark.req("IN-03", "EX-01", "EX-02")
def test_the_unreadable_scan_lands_in_the_queue_with_its_reason(app: Engine) -> None:
    email_id = new_email(app, "AAk-reading-e46")
    # Read outside the transaction, record in a short one (PR #79 review).
    reading = read_attachment(
        1, (FIXTURES / "documents" / "L46_scan_0001.jpg").read_bytes(), recorded()
    )
    assert reading is not None and reading.unreadable

    with unit_of_work(app, INTAKE) as db:
        exception_id = queue_unreadable(db, email_id, reading)

    with unit_of_work(app, INTAKE) as db:
        queue = list_open_exceptions(db, reason=ExceptionReason.UNREADABLE_DOCUMENT)
    entry = next(e for e in queue if e.id == exception_id)
    assert entry.email_id == email_id and entry.case_id is None
    assert entry.detail.startswith("The attachment 1 could not be read")


@pytest.mark.req("EX-01", "NF-01", "NF-03")
def test_the_audit_log_gets_the_position_never_the_content(app: Engine) -> None:
    email_id = new_email(app, "AAk-reading-audit")
    reading = read_attachment(
        1, (FIXTURES / "documents" / "L46_scan_0001.jpg").read_bytes(), recorded()
    )
    assert reading is not None

    with unit_of_work(app, INTAKE) as db:
        exception_id = queue_unreadable(db, email_id, reading)
        details: dict[str, Any] = db.execute(
            text("select details from audit_event where exception_id = :id"), {"id": exception_id}
        ).scalar_one()

    assert details["facts"] == {"document": "attachment 1", "reader": "document_intelligence"}


@pytest.mark.req("EX-01")
def test_queueing_the_same_document_twice_opens_one_exception(app: Engine) -> None:
    email_id = new_email(app, "AAk-reading-twice")
    reading = read_attachment(
        1, (FIXTURES / "documents" / "L46_scan_0001.jpg").read_bytes(), recorded()
    )
    assert reading is not None

    with unit_of_work(app, INTAKE) as db:
        first = queue_unreadable(db, email_id, reading)
    with unit_of_work(app, INTAKE) as db:
        second = queue_unreadable(db, email_id, reading)

    assert first == second


@pytest.mark.req("EX-01")
def test_a_readable_document_goes_nowhere_near_the_queue(app: Engine) -> None:
    email_id = new_email(app, "AAk-reading-ok")
    pdf = (FIXTURES / "documents" / "D01_Rechnung_BB-2026-1187.pdf").read_bytes()
    reading = read_attachment(1, pdf, recorded())
    assert reading is not None and reading.unreadable is None

    with unit_of_work(app, INTAKE) as db:
        assert queue_unreadable(db, email_id, reading) is None
        open_for_email = [e for e in list_open_exceptions(db) if e.email_id == email_id]
    assert open_for_email == []

"""Every email the sweep stores is sorted, and the database logs the decision (#91).

Runs the sweep against the fake Graph, the recorded model answers and a real PostgreSQL. The
groups below follow the acceptance criteria of #91. Covers IN-04, IN-07, IN-09, NF-01 and
NF-05. The check that all 50 fixture emails end in the route of the manifest is AC-1 in
``tests/acceptance``.
"""

import logging
import re
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.cases.store import CaseRuleBroken, NotAllowed, record_event, unit_of_work
from argus.documents.store import MemoryDocumentStore
from argus.intake.email_text import EmailReader
from argus.intake.graph import GraphClient
from argus.intake.records import EmailRecord, EmailStep, get_email, mark_classified, mark_routed
from argus.intake.sweep import SWEEP_ACTOR, DocumentArchive, Sorting, sweep
from argus.sorting import (
    EmailToSort,
    LanguageModel,
    ModelAnswer,
    ModelUnavailable,
    RecordedModel,
    Route,
)
from standins.graph.app import create_app
from tests.acceptance.intake import FixtureDocumentReader
from tests.integration.test_reminder_forward import (
    FORWARD_TO,
    clean,  # noqa: F401  wipes forwards and email records around each test
    fake_graph_with,
    fixture_path,
    graph_for,
)
from tests.unit.intake.graph_helpers import TOKEN
from tests.unit.sorting_helpers import MODEL_ANSWERS, decision_for, fixture_config

INVOICE, REMINDER, OTHER, REVIEW = "E01", "E25", "E32", "E45"


class CountingModel:
    """The recorded answers, counting the calls. ``down`` makes the next calls fail as if Azure
    could not be reached."""

    def __init__(self, down: int = 0) -> None:
        self._recorded = RecordedModel.from_file(MODEL_ANSWERS)
        self.calls = 0
        self.down = down

    def propose(self, email: EmailToSort) -> ModelAnswer:
        self.calls += 1
        if self.down:
            self.down -= 1
            raise ModelUnavailable("Azure OpenAI could not be reached")
        return self._recorded.propose(email)


def sorting_for(
    documents: MemoryDocumentStore, graph: GraphClient | None, model: LanguageModel
) -> Sorting:
    return Sorting(
        documents=documents,
        model=model,
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
        reminders=graph,
    )


def run(
    fake: FastAPI, model: LanguageModel, *, forward: bool = True
) -> tuple[MemoryDocumentStore, Sorting, GraphClient]:
    """A document store, the sorting and the Graph client for one test. Without ``forward``
    the sweep has no reminder forwarding."""
    graph = graph_for(fake, FORWARD_TO if forward else None)
    documents = MemoryDocumentStore()
    return documents, sorting_for(documents, graph if forward else None, model), graph


def record_of(app: Engine, graph: GraphClient, email_id: str) -> EmailRecord:
    """The record of a fixture email, found by the Graph ID the fake gave it."""
    raw = fixture_path(email_id).read_bytes()
    with app.connect() as connection:
        graph_ids: list[str] = list(
            connection.execute(text("select graph_message_id from mailbox_email")).scalars()
        )
        for graph_id in graph_ids:
            if graph.get_raw(graph_id) == raw:
                found = get_email(connection, graph_id)
                assert found is not None
                return found
    raise AssertionError(f"no record for {email_id}")


def events(app: Engine, record_id: int, event_type: str) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select details, actor from audit_event"
                " where email_id = :id and event_type = :type order by chain_seq"
            ),
            {"id": record_id, "type": event_type},
        ).mappings()
        return [dict(row) for row in rows]


# AC: after an email is stored, ARGUS sorts it and the database logs the decision.


@pytest.mark.req("IN-04", "IN-07", "NF-01", "NF-05")
@pytest.mark.usefixtures("clean")
def test_each_stored_email_is_sorted_routed_and_filed_in_one_sweep(app: Engine) -> None:
    fake = fake_graph_with(INVOICE, REMINDER, OTHER, REVIEW)
    documents, sorting, graph = run(fake, CountingModel())

    result = sweep(app, graph, DocumentArchive(documents), sorting)

    assert (result.stored, result.classified, result.routed, result.filed) == (4, 4, 4, 4)
    assert result.failed == 0
    expected = {
        INVOICE: Route.INVOICE_PROCESSING,
        REMINDER: Route.FORWARD_REMINDER,
        OTHER: Route.DELETION_FOLDER,
        REVIEW: Route.MANUAL_REVIEW,
    }
    for email_id, route in expected.items():
        record = record_of(app, graph, email_id)
        assert (record.step, record.route) == (EmailStep.FILED, route)
        [classified] = events(app, record.id, "email_classified")
        assert classified["actor"] == SWEEP_ACTOR.name
        assert classified["details"]["route"] == route.value
        assert classified["details"]["raw_sha256"] == record.raw_sha256
        # A pro forma (E45) stops at a safety gate, and the gate is named (#71).
        gates = {"gates"} if route is Route.MANUAL_REVIEW else set()
        assert (
            set(classified["details"])
            == {
                "route",
                "category",
                "model_category",
                "model_confidence",
                "model_invoices",
                "check_category",
                "reasons",
                "evidence",
                "sender",
                "raw_sha256",
                "raw_location",
            }
            | gates
        )
        assert classified["details"]["sender"]["authenticated"] is True


@pytest.mark.req("IN-09", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_email_in_doubt_is_recorded_for_manual_review_with_its_reasons(app: Engine) -> None:
    fake = fake_graph_with(REVIEW)
    documents, sorting, graph = run(fake, CountingModel())

    sweep(app, graph, DocumentArchive(documents), sorting)

    record = record_of(app, graph, REVIEW)
    [classified] = events(app, record.id, "email_classified")
    assert classified["details"]["route"] == "manual_review"
    assert classified["details"]["category"] is None
    assert classified["details"]["reasons"]
    assert record.folder == "ARGUS/Manual review"


def open_exceptions(app: Engine, record_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select reason, detail, facts from exception_item"
                " where email_id = :id and resolved_at is null order by id"
            ),
            {"id": record_id},
        ).mappings()
        return [dict(row) for row in rows]


@pytest.mark.req("IN-09", "EX-01")
@pytest.mark.usefixtures("clean")
def test_an_email_in_doubt_opens_one_exception_also_after_a_second_sweep(app: Engine) -> None:
    fake = fake_graph_with(REVIEW, OTHER)
    documents, sorting, graph = run(fake, CountingModel())

    sweep(app, graph, DocumentArchive(documents), sorting)
    sweep(app, graph, DocumentArchive(documents), sorting)

    review = record_of(app, graph, REVIEW)
    [exception] = open_exceptions(app, review.id)
    assert exception["reason"] == "uncertain_classification"
    [classified] = events(app, review.id, "email_classified")
    for reason in classified["details"]["reasons"]:
        assert reason in exception["detail"]
    assert open_exceptions(app, record_of(app, graph, OTHER).id) == []


# AC: the model is called outside any unit of work; if Azure cannot be reached the email stays
# where it is and the next sweep tries again. Sorting twice writes no second event.


@pytest.mark.req("IN-04", "NF-01")
@pytest.mark.usefixtures("clean")
def test_when_azure_cannot_be_reached_the_email_stays_stored_until_the_next_sweep(
    app: Engine,
) -> None:
    fake = fake_graph_with(OTHER)
    model = CountingModel(down=1)
    documents, sorting, graph = run(fake, model)

    first = sweep(app, graph, DocumentArchive(documents), sorting)
    record = record_of(app, graph, OTHER)
    assert (first.stored, first.classified, first.failed) == (1, 0, 1)
    assert record.step is EmailStep.STORED
    assert events(app, record.id, "email_classified") == []

    second = sweep(app, graph, DocumentArchive(documents), sorting)
    assert (second.classified, second.routed, second.filed, second.failed) == (1, 1, 1, 0)
    assert len(events(app, record.id, "email_classified")) == 1


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_sorting_the_same_email_twice_records_nothing_the_second_time(app: Engine) -> None:
    fake = fake_graph_with(REMINDER)
    model = CountingModel()
    documents, sorting, graph = run(fake, model, forward=False)

    sweep(app, graph, DocumentArchive(documents), sorting)
    record = record_of(app, graph, REMINDER)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert not mark_classified(db, record.id, decision_for(Route.DELETION_FOLDER))
    sweep(app, graph, DocumentArchive(documents), sorting)

    assert model.calls == 1
    assert len(events(app, record.id, "email_classified")) == 1


@pytest.mark.req("IN-05", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_reminder_waits_classified_until_forwarding_is_set_then_is_forwarded(
    app: Engine,
) -> None:
    fake = fake_graph_with(REMINDER)
    model = CountingModel()
    documents = MemoryDocumentStore()
    graph = graph_for(fake)

    without = sweep(app, graph, DocumentArchive(documents), sorting_for(documents, None, model))
    record = record_of(app, graph, REMINDER)
    assert (without.classified, without.routed) == (1, 0)
    assert record.step is EmailStep.CLASSIFIED

    later = sweep(app, graph, DocumentArchive(documents), sorting_for(documents, graph, model))
    record = record_of(app, graph, REMINDER)
    assert (later.routed, later.filed) == (1, 1)
    assert record.folder == "ARGUS/Payment reminders"
    assert model.calls == 1  # routed from the recorded decision, the model was not asked again
    assert len(fake.state.mailbox.sent) == 1


# AC: the app still cannot write email_* events, and the log cannot disagree with the email.


@pytest.mark.req("NF-01", "NF-05")
@pytest.mark.usefixtures("clean")
def test_the_application_cannot_write_or_change_the_recorded_decision(app: Engine) -> None:
    fake = fake_graph_with(REMINDER)
    documents, sorting, graph = run(fake, CountingModel(), forward=False)
    sweep(app, graph, DocumentArchive(documents), sorting)
    record = record_of(app, graph, REMINDER)

    refused = pytest.raises(CaseRuleBroken, match="written by the database")
    with refused, unit_of_work(app, SWEEP_ACTOR) as db:
        record_event(
            db, "email_classified", email_id=record.id, details={"route": "deletion_folder"}
        )
    with pytest.raises(CaseRuleBroken, match="set once"), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text(
                'update mailbox_email set classification = \'{"route": "deletion_folder"}\''
                " where id = :id"
            ),
            {"id": record.id},
        )
    # The route must be the route of the decision.
    with pytest.raises(CaseRuleBroken), unit_of_work(app, SWEEP_ACTOR) as db:
        mark_routed(db, record.id, Route.DELETION_FOLDER)
    with pytest.raises(NotAllowed), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text("update mailbox_email set seen_at = now() where id = :id"), {"id": record.id}
        )


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_an_email_cannot_be_classified_without_a_decision(app: Engine) -> None:
    fake = fake_graph_with(OTHER)
    documents = MemoryDocumentStore()
    graph = graph_for(fake)
    sweep(app, graph, DocumentArchive(documents))  # no sorting: stops at stored
    record = record_of(app, graph, OTHER)

    with pytest.raises(CaseRuleBroken), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text("update mailbox_email set step = 'classified' where id = :id"),
            {"id": record.id},
        )


# AC: no email text, prompt or model answer in the logs.


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("clean")
def test_the_logs_hold_no_sender_subject_or_message_id(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_graph_with(INVOICE, REVIEW)
    documents, sorting, graph = run(fake, CountingModel())
    caplog.set_level(logging.DEBUG)

    sweep(app, graph, DocumentArchive(documents), sorting)

    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("argus"))
    for email_id in (INVOICE, REVIEW):
        read = sorting.read(fixture_path(email_id).read_bytes())
        for secret in (read.message_id, read.sender_address, read.subject):
            assert secret not in logged


# #70: the sender check is kept with the email and in the audit log.

SPOOFED = (
    b"Authentication-Results: spf=pass (sender IP is 192.0.2.66) smtp.mailfrom=attacker.invalid;"
    b"\n dkim=none (message not signed) header.d=none;dmarc=fail action=quarantine\n"
    b" header.from=maxasp.example;compauth=fail reason=000\n"
)


@pytest.mark.req("AR-02", "IN-09", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_outgoing_invoice_from_a_fake_invoicing_agent_goes_to_review(app: Engine) -> None:
    # E17 is a real outgoing invoice from the invoicing agent; here Exchange says it is not.
    raw = fixture_path("E17").read_bytes()
    raw = re.sub(rb"^Authentication-Results:.*?\n(?=\S)", SPOOFED, raw, count=1, flags=re.S)
    assert b"dmarc=fail" in raw
    fake = create_app(token=TOKEN, emails=[raw])
    documents, sorting, graph = run(fake, CountingModel())

    sweep(app, graph, DocumentArchive(documents), sorting)

    with app.connect() as connection:
        row = connection.execute(text("select id, route, classification from mailbox_email")).one()
    record_id: int = row.id
    route: str = row.route
    stored: dict[str, Any] = row.classification
    assert route == "manual_review"
    expected = {
        "claimed": "invoicing_agent",
        "treated_as": "unknown",
        "from_domain": "maxasp.example",
        "authenticated": False,
        "passed_by": None,
        "dmarc_failed": True,
        "results": [
            "spf=pass attacker.invalid",
            "dkim=none (not a domain)",
            "dmarc=fail maxasp.example",
        ],
    }
    assert stored["sender"] == expected
    [classified] = events(app, record_id, "email_classified")
    assert classified["details"]["sender"] == expected
    assert "the sender claims to be the invoicing agent" in " ".join(
        classified["details"]["reasons"]
    )

"""One case per invoice, opened by the sweep with a real PostgreSQL (#30, IN-08).

Runs the sweep against the fake Graph, the recorded model answers and the fixture document
reader, as the sorting tests do. The page by page split and the keys are tested in
``tests/unit/test_invoice_split.py``; here the cases, their links and the database rules.
"""

import json
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.cases.model import CaseKind
from argus.cases.store import (
    CaseRuleBroken,
    DuplicateRefused,
    NotAllowed,
    create_case,
    record_event,
    unit_of_work,
)
from argus.documents.store import MemoryDocumentStore
from argus.intake.email_text import EmailReader
from argus.intake.graph import GraphClient
from argus.intake.invoices import Invoicing
from argus.intake.records import EmailRecord, EmailStep
from argus.intake.sweep import SWEEP_ACTOR, DocumentArchive, Sorting, classify, sweep
from argus.sorting import LanguageModel, RecordedModel
from standins.graph.app import create_app
from tests.acceptance.intake import FixtureDocumentReader
from tests.integration.test_email_sorting import events, open_exceptions
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
    fixture_path,
    graph_for,
)
from tests.unit.intake.graph_helpers import TOKEN
from tests.unit.sorting_helpers import MODEL_ANSWERS, OWN_VAT_IDS, fixture_config
from tests.unit.test_invoice_split import email_with_pdf, merged_pdf

# The live mode behaviour of ARGUS (#72): shadow mode is the default, so these tests switch it.
pytestmark = pytest.mark.usefixtures("live")

TWO_IN_ONE_PDF = "<split.two-invoices-one-pdf@fixtures.argus.example>"
E14_ID = "<E14.drei-rechnungen-eine-mail@fixtures.argus.example>"


def model(listed: dict[str, int] | None = None) -> RecordedModel:
    """The recorded answers. The email with two invoices in one PDF gets E14's answer with its
    first two invoices; ``listed`` cuts the invoices of an answer to that many."""
    answers: dict[str, Any] = json.loads(MODEL_ANSWERS.read_text(encoding="utf-8"))
    e14 = answers[E14_ID]
    answers[TWO_IN_ONE_PDF] = {**e14, "invoices": e14["invoices"][:2]}
    for message_id, count in (listed or {}).items():
        answers[message_id] = {**answers[message_id], "invoices": e14["invoices"][:count]}
    return RecordedModel(answers)


def setup(
    raws: list[bytes], language_model: LanguageModel
) -> tuple[MemoryDocumentStore, Sorting, GraphClient]:
    graph = graph_for(create_app(token=TOKEN, emails=raws), None)
    documents = MemoryDocumentStore()
    sorting = Sorting(
        documents=documents,
        model=language_model,
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
        invoicing=Invoicing(FixtureDocumentReader(), OWN_VAT_IDS),
    )
    return documents, sorting, graph


def e14() -> bytes:
    return fixture_path("E14").read_bytes()


def two_in_one_pdf() -> bytes:
    return email_with_pdf(merged_pdf("D14", "D15"))


def record_for(app: Engine, raw: bytes) -> int:
    with app.connect() as connection:
        found: int = connection.execute(
            text(
                "select e.id from mailbox_email e join stored_document d"
                " on d.email_id = e.id and d.kind = 'email'"
                " where d.sha256 = encode(sha256(:raw), 'hex')"
            ),
            {"raw": raw},
        ).scalar_one()
    return int(found)


def cases_of(app: Engine, email_id: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                """
                select c.id, c.kind, c.status, c.case_key, c.document_number, c.gross_minor,
                       c.currency, c.source_email_id,
                       array(select document_id from case_document where case_id = c.id
                             order by document_id) as documents,
                       array(select r.pages::text from invoice_reading r
                             where r.case_id = c.id and r.role = 'invoice') as pages
                from invoice_case c where c.source_email_id = :id order by c.id
                """
            ),
            {"id": email_id},
        ).mappings()
        return [dict(row) for row in rows]


def attachment_ids(app: Engine, email_id: int) -> list[int]:
    with app.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select id from stored_document where email_id = :id and kind = 'attachment'"
                    " order by id"
                ),
                {"id": email_id},
            ).scalars()
        )


# AC: the multi invoice fixtures create one case per invoice; each case links to the same email
# and to its own document.


@pytest.mark.req("IN-08", "NF-01")
@pytest.mark.usefixtures("clean")
def test_three_invoices_in_one_email_become_three_cases_each_with_its_own_document(
    app: Engine,
) -> None:
    documents, sorting, graph = setup([e14()], model())
    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, e14())
    cases = cases_of(app, email_id)
    assert [c["document_number"] for c in cases] == ["R-2026-0932", "R-2026-0933", "R-2026-0934"]
    assert {c["source_email_id"] for c in cases} == {email_id}
    assert [c["documents"] for c in cases] == [[d] for d in attachment_ids(app, email_id)]
    assert {c["kind"] for c in cases} == {"incoming_invoice"}
    assert {c["status"] for c in cases} == {"received"}
    assert [c["gross_minor"] for c in cases] == [30940, 17255, 10282]
    assert {c["currency"] for c in cases} == {"EUR"}
    assert [c["case_key"] for c in cases] == [
        "vat:DE012345607|R-2026-0932",
        "vat:DE012345607|R-2026-0933",
        "vat:DE012345607|R-2026-0934",
    ]


@pytest.mark.req("IN-08", "NF-01")
@pytest.mark.usefixtures("clean")
def test_two_invoices_on_two_pages_of_one_pdf_become_two_cases(app: Engine) -> None:
    raw = two_in_one_pdf()
    documents, sorting, graph = setup([raw], model())
    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, raw)
    [pdf] = attachment_ids(app, email_id)
    cases = cases_of(app, email_id)
    assert [c["document_number"] for c in cases] == ["R-2026-0932", "R-2026-0933"]
    assert [c["documents"] for c in cases] == [[pdf], [pdf]]
    assert [c["pages"] for c in cases] == [["{1}"], ["{2}"]]
    assert [c["gross_minor"] for c in cases] == [30940, 17255]


@pytest.mark.req("IN-08", "NF-01", "NF-03")
@pytest.mark.usefixtures("clean")
def test_each_reading_is_stored_with_its_case_and_logged_without_its_values(app: Engine) -> None:
    raw = two_in_one_pdf()
    documents, sorting, graph = setup([raw], model())
    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, raw)
    _, second = cases_of(app, email_id)
    with app.connect() as connection:
        reading: dict[str, Any] = connection.execute(
            text("select reading from invoice_reading where case_id = :id"), {"id": second["id"]}
        ).scalar_one()
        logged = connection.execute(
            text(
                "select details, actor from audit_event"
                " where event_type = 'invoice_read' and case_id = :id"
            ),
            {"id": second["id"]},
        ).one()
    assert reading["fields"]["invoice_number"]["value"] == "R-2026-0933"
    assert reading["fields"]["iban_on_document"]["value"] == "DE57000000070000700007"
    assert logged.actor == SWEEP_ACTOR.name
    assert logged.details["pages"] == [2]
    assert logged.details["document"] == "attachment 1"
    # The log names the document and the flagged fields, never a value, a name or an IBAN.
    assert set(logged.details) == {
        "reading_id", "document_id", "document", "pages", "role", "reader", "kind", "flagged",
    }  # fmt: skip
    assert "0000700007" not in json.dumps(logged.details)
    assert "R-2026-0933" not in json.dumps(logged.details)


@pytest.mark.req("IN-08", "AC-8")
@pytest.mark.usefixtures("clean")
def test_the_trail_of_each_case_starts_with_the_email_it_came_from(app: Engine) -> None:
    documents, sorting, graph = setup([e14()], model())
    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, e14())
    for case in cases_of(app, email_id):
        with app.connect() as connection:
            trail: list[str] = list(
                connection.execute(
                    text(
                        "select event_type from case_trace where trace_case_id = :id"
                        " order by chain_seq"
                    ),
                    {"id": case["id"]},
                ).scalars()
            )
        assert trail[:3] == ["email_seen", "email_stored", "document_stored"]
        assert "email_classified" in trail
        assert trail.index("case_created") > trail.index("document_stored")
        assert {"case_created", "document_linked", "invoice_read", "email_filed"} <= set(trail)


# AC: if the model lists another number of invoices than the checks find, the email goes to
# manual review.


@pytest.mark.req("IN-08", "IN-09", "EX-01")
@pytest.mark.usefixtures("clean")
def test_a_model_that_lists_two_of_three_invoices_sends_the_email_to_manual_review(
    app: Engine,
) -> None:
    documents, sorting, graph = setup([e14()], model({E14_ID: 2}))
    sweep(app, graph, DocumentArchive(documents), sorting)

    email_id = record_for(app, e14())
    assert cases_of(app, email_id) == []
    [classified] = events(app, email_id, "email_classified")
    assert classified["details"]["route"] == "manual_review"
    assert classified["details"]["model_invoices"] == 2
    assert classified["details"]["invoices_found"] == 3
    reason = "the language model lists 2 invoices, the documents and pages show 3"
    assert reason in classified["details"]["reasons"]
    [exception] = open_exceptions(app, email_id)
    assert exception["reason"] == "uncertain_classification"
    assert reason in exception["detail"]
    with app.connect() as connection:
        folder: str = connection.execute(
            text("select folder from mailbox_email where id = :id"), {"id": email_id}
        ).scalar_one()
    assert folder == "ARGUS/Manual review"


@pytest.mark.req("IN-08")
@pytest.mark.usefixtures("clean")
def test_classifying_the_same_email_twice_opens_its_cases_once(app: Engine) -> None:
    documents, sorting, graph = setup([e14()], model())
    sweep(app, graph, DocumentArchive(documents), sorting)
    email_id = record_for(app, e14())
    with app.connect() as connection:
        sha: str = connection.execute(
            text("select raw_sha256 from mailbox_email where id = :id"), {"id": email_id}
        ).scalar_one()

    # A second worker that still holds the email at step stored records nothing and opens no
    # second set of cases.
    stale = EmailRecord(email_id, "stale", EmailStep.STORED, sha)
    assert classify(app, sorting, stale) is None
    assert len(cases_of(app, email_id)) == 3


# The database rules behind it.


def an_email_with_documents(app: Engine) -> tuple[int, list[int]]:
    documents, sorting, graph = setup([e14()], model())
    sweep(app, graph, DocumentArchive(documents), sorting)
    email_id = record_for(app, e14())
    return email_id, attachment_ids(app, email_id)


@pytest.mark.req("IN-08")
@pytest.mark.usefixtures("clean")
def test_one_email_has_at_most_one_case_per_key(app: Engine) -> None:
    email_id, _ = an_email_with_documents(app)
    with pytest.raises(DuplicateRefused), unit_of_work(app, SWEEP_ACTOR) as db:
        create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email_id,
            case_key="vat:DE012345607|R-2026-0932",
        )
    # Another email may hold the same invoice; catching that is #31's job.
    with unit_of_work(app, SWEEP_ACTOR) as db:
        create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email_id + 100000,
            case_key="vat:DE012345607|R-2026-0932",
        )


@pytest.mark.req("IN-08", "NF-05")
@pytest.mark.usefixtures("clean")
def test_the_case_key_is_set_once(app: Engine, owner: Engine) -> None:
    email_id, _ = an_email_with_documents(app)
    case_id = cases_of(app, email_id)[0]["id"]
    with pytest.raises(NotAllowed), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text("update invoice_case set case_key = 'vat:X|1' where id = :id"), {"id": case_id}
        )
    with pytest.raises(CaseRuleBroken, match="set once"), unit_of_work(owner, SWEEP_ACTOR) as db:
        db.execute(
            text("update invoice_case set case_key = 'vat:X|1' where id = :id"), {"id": case_id}
        )


@pytest.mark.req("IN-08")
@pytest.mark.parametrize(
    ("key", "email"),
    [("1001", True), ("vat:DE012345607", True), ("vat:DE012345607|1001", False)],
)
@pytest.mark.usefixtures("clean")
def test_a_case_key_names_issuer_and_number_and_needs_the_email(
    app: Engine, key: str, email: bool
) -> None:
    with pytest.raises(CaseRuleBroken), unit_of_work(app, SWEEP_ACTOR) as db:
        create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=424242 if email else None,
            case_key=key,
        )


@pytest.mark.req("NF-01", "NF-05")
@pytest.mark.usefixtures("clean")
def test_a_stored_reading_cannot_be_changed_or_removed(app: Engine, owner: Engine) -> None:
    email_id, _ = an_email_with_documents(app)
    case_id = cases_of(app, email_id)[0]["id"]
    for engine in (app, owner):
        for statement in (
            "update invoice_reading set reading = '{}' where case_id = :id",
            "delete from invoice_reading where case_id = :id",
        ):
            with pytest.raises(CaseRuleBroken), unit_of_work(engine, SWEEP_ACTOR) as db:
                db.execute(text(statement), {"id": case_id})


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_a_reading_must_come_from_a_document_of_the_cases_email(app: Engine) -> None:
    email_id, documents = an_email_with_documents(app)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        other = create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=email_id + 100001)
    with (
        pytest.raises(CaseRuleBroken, match="did not come with"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        db.execute(
            text(
                "insert into invoice_reading (case_id, document_id, document, role, reading)"
                " values (:case, :doc, 'attachment 1', 'invoice', '{}')"
            ),
            {"case": other, "doc": documents[0]},
        )


@pytest.mark.req("NF-01", "NF-05")
@pytest.mark.usefixtures("clean")
def test_the_application_cannot_write_invoice_read_itself(app: Engine) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        record_event(db, "invoice_read", details={"reading_id": 1})

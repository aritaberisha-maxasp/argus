"""The mailbox sweep hands Wise statement emails to the statement import (#166).

Runs the sweep against the fake Graph and a real PostgreSQL. The groups follow the acceptance
criteria of #166: only from a confirmed statement sender or the statement folder; always with
the email's ID; anything else to the queue with a reason; a daily statement job; twice imports
once; shadow mode as in PR 162. Covers BA-01, BA-07 and BA-08.
"""

from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.bank.importer import BankSettings, ImportOutcome
from argus.bank.notices import NoticeKind
from argus.cases.model import Actor, CaseKind, Outcome
from argus.cases.store import resolve_exception, unit_of_work
from argus.documents.store import MemoryDocumentStore
from argus.intake.email_text import EmailReader
from argus.intake.graph import GraphCallRefused, GraphClient
from argus.intake.jobs import StatementServices, run_statements
from argus.intake.statements import SENDER_NOT_CONFIRMED, Statements
from argus.intake.sweep import DocumentArchive, Sorting, sweep
from argus.sorting import Category, EmailToSort, ModelAnswer
from standins.graph.app import MAILBOX, create_app
from tests.acceptance.intake import FixtureDocumentReader
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport
from tests.unit.sorting_helpers import fixture_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wise"
OCTOBER = (FIXTURES / "statement_EUR_2026-10.csv").read_bytes()
NOVEMBER = (FIXTURES / "statement_EUR_2026-11.csv").read_bytes()
BENJAMIN = "benjamin@maxasp.example"  # synthetic, like every address in the fixtures
FOLDER = "ARGUS/Kontoauszüge"
SETTINGS = BankSettings(statement_senders=frozenset({BENJAMIN}), statement_folder=FOLDER)
DECEMBER_2ND = date(2026, 12, 2)
PERSON = Actor.user("55555555-5555-4555-8555-555555555555")  # made up, for the tests
# What Exchange Online writes for a message whose From domain passed DKIM and DMARC.
PASSED = (
    "mx.microsoft.com; spf=pass smtp.mailfrom=maxasp.example;"
    " dkim=pass header.d=maxasp.example; dmarc=pass action=none header.from=maxasp.example"
)
# Mail from inside the tenant often carries no result at all.
NO_RESULT = "mx.microsoft.com; spf=none smtp.mailfrom=maxasp.example; dkim=none"
# A forged From line with Benjamin's address, sent from outside.
FORGED = (
    "mx.microsoft.com; spf=pass smtp.mailfrom=outside.example;"
    " dkim=pass header.d=outside.example; dmarc=fail action=quarantine header.from=maxasp.example"
)


def statement_email(
    sender: str = BENJAMIN,
    auth: str | None = PASSED,
    files: tuple[tuple[str, bytes], ...] = (("statement_EUR_2026-10.csv", OCTOBER),),
    message_id: str = "<statement-1@maxasp.example>",
) -> bytes:
    message = EmailMessage()
    if auth is not None:
        message["Authentication-Results"] = auth
    message["From"] = f"Benjamin <{sender}>"
    message["To"] = MAILBOX
    message["Subject"] = "Wise Kontoauszug"
    message["Message-ID"] = message_id
    message["Date"] = "Tue, 01 Dec 2026 08:00:00 +0100"
    message.set_content("Anbei der Auszug.")
    for name, data in files:
        if name.endswith(".csv"):
            message.add_attachment(data, maintype="text", subtype="csv", filename=name)
        else:
            message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


@pytest.fixture
def fake() -> FastAPI:
    return create_app(token=TOKEN, emails=[])


def graph_for(fake: FastAPI, *, forward_to: str | None = BENJAMIN) -> GraphClient:
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        sleep=lambda _: None,
        forward_to=forward_to,
        statement_folder=FOLDER,
    )


@pytest.fixture
def documents() -> MemoryDocumentStore:
    return MemoryDocumentStore()


@pytest.fixture
def statements_setup(documents: MemoryDocumentStore) -> Statements:
    return Statements(documents, SETTINGS, None, today=lambda: DECEMBER_2ND)


def run_sweep(
    app: Engine,
    fake: FastAPI,
    documents: MemoryDocumentStore,
    statements: Statements,
    sorting: Sorting | None = None,
) -> Any:
    return sweep(app, graph_for(fake), DocumentArchive(documents), sorting, statements)


def statement_folder(fake: FastAPI) -> str:
    graph = graph_for(fake)
    argus = graph.find_folder(None, "ARGUS") or graph.create_folder(None, "ARGUS")
    return graph.find_folder(argus, "Kontoauszüge") or graph.create_folder(argus, "Kontoauszüge")


def into_folder(fake: FastAPI, raw: bytes) -> str:
    """Put an email straight into ARGUS/Kontoauszüge."""
    message = fake.state.mailbox.add(raw)
    move_to_folder(fake, message.immutable_id)
    return str(message.immutable_id)


def move_to_folder(fake: FastAPI, message_id: str) -> None:
    """Move an email into ARGUS/Kontoauszüge as a person in Outlook would. It keeps its ID."""
    fake.state.mailbox.location[message_id] = statement_folder(fake)


def rows(app: Engine, sql: str, **params: Any) -> list[Any]:
    with app.connect() as connection:
        return list(connection.execute(text(sql), params).all())


def statements_of(app: Engine) -> list[Any]:
    return rows(app, "select * from bank_statement order by id")


def email_id(app: Engine) -> int:
    found = rows(app, "select id from mailbox_email order by id")
    assert len(found) == 1
    return int(found[0].id)


def open_exceptions(app: Engine, email: int) -> list[Any]:
    return rows(
        app,
        "select reason, detail, facts from exception_item where email_id = :id"
        " and resolved_at is null",
        id=email,
    )


pytestmark = pytest.mark.usefixtures("clean", "clean_bank")


# 1. From a confirmed sender, or from the statement folder.


@pytest.mark.req("BA-01", "BA-07")
def test_a_statement_from_benjamin_that_passes_the_check_is_imported(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email())

    result = run_sweep(app, fake, documents, statements_setup)

    assert (result.statement_files, result.failed) == (1, 0)
    [statement] = statements_of(app)
    assert (statement.outcome, statement.source) == ("imported", "email")
    cases = rows(
        app,
        "select c.id from invoice_case c join bank_transaction t on t.case_id = c.id"
        " where t.statement_id = :id and c.kind = :k",
        id=statement.id,
        k=CaseKind.BANK_TRANSACTION,
    )
    assert len(cases) == statement.transaction_count


@pytest.mark.req("BA-01")
def test_a_statement_in_the_statement_folder_is_imported_whoever_sent_it(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    into_folder(fake, statement_email(sender="someone@maxasp.example", auth=NO_RESULT))

    result = run_sweep(app, fake, documents, statements_setup)

    assert (result.statement_files, result.failed) == (1, 0)
    [statement] = statements_of(app)
    assert (statement.outcome, statement.source) == ("imported", "folder")


@pytest.mark.req("BA-01")
def test_the_folder_listing_follows_graphs_paging(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    into_folder(fake, statement_email(message_id="<oct@maxasp.example>"))
    for n in range(60):  # more than one page of 50
        into_folder(fake, statement_email(files=(), message_id=f"<note-{n}@maxasp.example>"))

    result = run_sweep(app, fake, documents, statements_setup)

    assert (result.statement_files, result.failed) == (1, 0)
    assert len(rows(app, "select id from mailbox_email")) == 61
    # The 60 emails without a CSV file each go to the queue once.
    no_csv = rows(
        app, "select id from exception_item where facts ->> 'reason' = 'no_csv_in_folder'"
    )
    assert len(no_csv) == 60


@pytest.mark.req("NF-06")
def test_only_the_statement_folder_can_be_listed(fake: FastAPI) -> None:
    graph = graph_for(fake)
    into_folder(fake, statement_email())
    list(graph.list_statement_folder())
    for other in ("drafts", "sentitems", "AAMkSomeOtherFolder"):
        with pytest.raises(GraphCallRefused):
            graph._http.get(f"users/{MAILBOX}/mailFolders/{other}/messages")
    without = GraphClient(
        StaticTokens(), MAILBOX, BASE_URL, transport=standin_transport(fake), sleep=lambda _: None
    )
    with pytest.raises(GraphCallRefused):
        list(without.list_statement_folder())


@pytest.mark.req("BA-01")
def test_no_statement_folder_in_the_mailbox_is_no_failure(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    result = run_sweep(app, fake, documents, statements_setup)
    assert (result.statement_files, result.failed) == (0, 0)


# 2. Always with the email's ID.


@pytest.mark.req("BA-01", "NF-01")
@pytest.mark.parametrize("way", ["inbox", "folder"])
def test_every_statement_links_back_to_its_email(
    app: Engine,
    fake: FastAPI,
    documents: MemoryDocumentStore,
    statements_setup: Statements,
    way: str,
) -> None:
    raw = statement_email()
    if way == "inbox":
        fake.state.mailbox.add(raw)
    else:
        into_folder(fake, raw)

    run_sweep(app, fake, documents, statements_setup)

    [statement] = statements_of(app)
    assert statement.source_email_id == email_id(app)


@pytest.mark.req("BA-01")
def test_an_unreadable_statement_from_benjamin_lands_in_the_queue_on_its_email(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email(files=(("auszug.csv", b"not;a;statement\n"),)))

    run_sweep(app, fake, documents, statements_setup)

    assert statements_of(app) == []
    [exception] = open_exceptions(app, email_id(app))
    assert exception.reason == "unreadable_document"


# 3. Anyone else, or a failed check: not imported, to the queue with a reason.


@pytest.mark.req("BA-01", "NF-01")
@pytest.mark.parametrize(
    ("sender", "auth"),
    [
        ("someone@outside.example", PASSED),  # not a statement sender
        (BENJAMIN, FORGED),  # Benjamin's address, forged from outside
        (BENJAMIN, NO_RESULT),  # no result for his domain, as internal mail often has
        (BENJAMIN, None),  # no Authentication-Results at all
    ],
)
def test_a_statement_from_anyone_else_or_failing_the_check_goes_to_the_queue(
    app: Engine,
    fake: FastAPI,
    documents: MemoryDocumentStore,
    statements_setup: Statements,
    sender: str,
    auth: str | None,
) -> None:
    fake.state.mailbox.add(statement_email(sender=sender, auth=auth))

    # Swept twice while still unsorted: refused and flagged once, not on every sweep.
    run_sweep(app, fake, documents, statements_setup)
    run_sweep(app, fake, documents, statements_setup)

    assert statements_of(app) == []
    email = email_id(app)
    [exception] = open_exceptions(app, email)
    assert exception.reason == "uncertain_classification"
    assert exception.facts["reason"] == SENDER_NOT_CONFIRMED
    assert "Absenderprüfung nicht bestanden" in exception.detail
    assert FOLDER in exception.detail
    refused = rows(
        app,
        "select details from audit_event where email_id = :id"
        " and event_type = 'bank_import_refused'",
        id=email,
    )
    assert [r.details["reason"] for r in refused] == ["unknown_source"]


@pytest.mark.req("BA-01")
def test_a_csv_from_anyone_else_without_wise_columns_is_left_alone(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    other = b"Artikel,Menge,Preis\nPapier,2,9.90\n"
    fake.state.mailbox.add(
        statement_email(sender="shop@outside.example", files=(("bestellung.csv", other),))
    )

    result = run_sweep(app, fake, documents, statements_setup)

    assert result.statement_files == 0
    assert open_exceptions(app, email_id(app)) == []


@pytest.mark.req("BA-01")
def test_moving_a_refused_statement_into_the_folder_imports_it_and_a_person_closes_it(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email(auth=NO_RESULT))
    run_sweep(app, fake, documents, statements_setup)
    email = email_id(app)
    assert len(open_exceptions(app, email)) == 1

    # A person checks it is genuine and moves it into the statement folder.
    [message_id] = list(fake.state.mailbox.inbox)
    move_to_folder(fake, message_id)
    run_sweep(app, fake, documents, statements_setup)

    [statement] = statements_of(app)
    assert (statement.outcome, statement.source) == ("imported", "folder")
    # The move overrides the sender check and Graph does not say who moved the email, so the
    # exception stays open until a signed in person closes it (compliance review of PR 176).
    [still_open] = open_exceptions(app, email)
    assert still_open.facts["reason"] == SENDER_NOT_CONFIRMED
    assert "mit Begründung schließen" in still_open.detail
    [mark] = rows(
        app,
        "select details from audit_event where email_id = :id"
        " and event_type = 'bank_import_from_folder'",
        id=email,
    )
    assert mark.details["sender_confirmed"] is False


@pytest.mark.req("BA-01", "NF-01")
def test_an_email_in_the_folder_without_a_csv_file_goes_to_the_queue(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    into_folder(fake, statement_email(files=(("auszug.pdf", b"%PDF-1.4 not a csv"),)))

    first = run_sweep(app, fake, documents, statements_setup)
    again = run_sweep(app, fake, documents, statements_setup)

    assert (first.statement_files, first.failed, again.failed) == (0, 0, 0)
    [exception] = open_exceptions(app, email_id(app))
    assert exception.reason == "unreadable_document"
    assert exception.facts["reason"] == "no_csv_in_folder"
    assert "keine CSV Datei" in exception.detail


@pytest.mark.req("BA-01")
def test_a_resolved_refusal_is_not_opened_again(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email(sender="someone@outside.example"))
    run_sweep(app, fake, documents, statements_setup)
    email = email_id(app)
    [exception_id] = [
        r.id for r in rows(app, "select id from exception_item where email_id = :id", id=email)
    ]
    with unit_of_work(app, PERSON) as db:
        resolve_exception(db, exception_id, Outcome.REJECTED, resolution="Kein Auszug")
    # The email stays unsorted in this test, so the next sweep looks at it again.
    run_sweep(app, fake, documents, statements_setup)

    assert open_exceptions(app, email) == []


class ManualReviewModel:
    """A model that is never sure, so the sorting sends every email to manual review."""

    def propose(self, email: EmailToSort) -> ModelAnswer:
        return ModelAnswer(category=Category.OTHER, confidence=0.1, invoices=(), evidence=())


@pytest.mark.req("BA-01", "IN-09")
@pytest.mark.usefixtures("live")
def test_in_live_mode_the_sorting_adds_no_second_exception(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    sorting = Sorting(
        documents=documents,
        model=ManualReviewModel(),
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
    )
    fake.state.mailbox.add(statement_email(sender="someone@outside.example"))

    result = run_sweep(app, fake, documents, statements_setup, sorting)

    assert (result.classified, result.failed) == (1, 0)
    [record] = rows(app, "select step, route from mailbox_email")
    assert record.route == "manual_review"
    [exception] = open_exceptions(app, email_id(app))
    assert exception.facts["reason"] == SENDER_NOT_CONFIRMED


class CountingSureModel:
    """A model that is sure every email is "other", counting how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, email: EmailToSort) -> ModelAnswer:
        self.calls += 1
        return ModelAnswer(category=Category.OTHER, confidence=0.99, invoices=(), evidence=())


def sure_sorting(documents: MemoryDocumentStore, model: CountingSureModel) -> Sorting:
    return Sorting(
        documents=documents,
        model=model,
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
    )


@pytest.mark.req("BA-01", "IN-09")
@pytest.mark.usefixtures("live")
def test_in_live_mode_an_imported_statement_is_not_sorted_and_moves_to_the_folder(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    model = CountingSureModel()
    message = fake.state.mailbox.add(statement_email())

    first = run_sweep(app, fake, documents, statements_setup, sure_sorting(documents, model))
    again = run_sweep(app, fake, documents, statements_setup, sure_sorting(documents, model))

    assert (first.failed, again.failed, first.classified, model.calls) == (0, 0, 0, 0)
    [statement] = statements_of(app)
    assert (statement.outcome, statement.source) == ("imported", "email")
    assert open_exceptions(app, email_id(app)) == []
    [record] = rows(app, "select step, route from mailbox_email")
    assert (record.step, record.route) == ("stored", None)
    assert message.immutable_id in fake.state.mailbox.in_folder(FOLDER)


@pytest.mark.req("BA-01", "IN-09")
def test_in_shadow_mode_an_imported_statement_gets_no_proposal_and_stays_in_the_inbox(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    model = CountingSureModel()
    message = fake.state.mailbox.add(statement_email())

    result = run_sweep(app, fake, documents, statements_setup, sure_sorting(documents, model))

    assert (result.failed, result.proposed, model.calls) == (0, 0, 0)
    assert [s.outcome for s in statements_of(app)] == ["imported"]
    assert message.immutable_id in fake.state.mailbox.inbox
    assert fake.state.mailbox.moves == []


@pytest.mark.req("BA-01")
@pytest.mark.usefixtures("live")
def test_in_live_mode_the_sweep_creates_the_statement_folder(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    result = run_sweep(app, fake, documents, statements_setup)

    assert result.failed == 0
    names = {f.name for f in fake.state.mailbox.folders.values()}
    assert {"ARGUS", "Kontoauszüge"} <= names


# 4. The daily statement job.


@pytest.mark.req("BA-01", "BA-08")
@pytest.mark.usefixtures("live")
def test_the_statement_job_reminds_benjamin_and_is_clean(app: Engine, fake: FastAPI) -> None:
    services = StatementServices(app, graph_for(fake), SETTINGS)

    assert run_statements(services, today=DECEMBER_2ND)

    [notice] = fake.state.mailbox.notices
    assert notice["toRecipients"][0]["emailAddress"]["address"] == BENJAMIN
    kinds = rows(app, "select kind, status from bank_notice")
    assert [(k.kind, k.status) for k in kinds] == [(NoticeKind.STATEMENT_MISSING.value, "sent")]
    # Run again the same day: nothing more is sent.
    assert run_statements(services, today=DECEMBER_2ND)
    assert len(fake.state.mailbox.notices) == 1


@pytest.mark.req("BA-01", "BA-08")
@pytest.mark.usefixtures("live")
def test_a_notice_that_could_not_be_sent_fails_the_job(app: Engine, fake: FastAPI) -> None:
    fake.state.mailbox.fail_next.extend([(503, 0)] * 10)
    services = StatementServices(app, graph_for(fake), SETTINGS)

    assert not run_statements(services, today=DECEMBER_2ND)

    assert [r.status for r in rows(app, "select status from bank_notice")] == ["pending"]


# 5. Twice imports once.


@pytest.mark.req("BA-07")
def test_sweeping_the_same_statement_email_again_imports_it_once(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email())
    run_sweep(app, fake, documents, statements_setup)
    cases_before = rows(app, "select id from invoice_case")

    # The handover for an Inbox email runs once the email is sorted; until then each sweep
    # looks again. Nothing new comes in either way.
    for _ in range(2):
        run_sweep(app, fake, documents, statements_setup)

    assert len(statements_of(app)) == 1
    assert rows(app, "select id from invoice_case") == cases_before


@pytest.mark.req("BA-07")
def test_the_same_statement_in_the_inbox_and_the_folder_imports_once(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore, statements_setup: Statements
) -> None:
    fake.state.mailbox.add(statement_email(message_id="<a@maxasp.example>"))
    into_folder(fake, statement_email(message_id="<b@maxasp.example>"))

    first = run_sweep(app, fake, documents, statements_setup)
    again = run_sweep(app, fake, documents, statements_setup)

    assert len(statements_of(app)) == 1
    assert first.failed == again.failed == 0
    # The folder email is read once, not on every sweep.
    marks = rows(
        app,
        "select e.details from audit_event e join mailbox_email m on m.id = e.email_id"
        " where e.event_type = 'bank_import_from_folder'",
    )
    assert len(marks) == 1
    assert marks[0].details["files"][0]["outcome"] == ImportOutcome.ALREADY_SEEN.value


# 6. Shadow mode, as in PR 162.


@pytest.mark.req("BA-01", "BA-08")
def test_in_shadow_mode_the_statement_is_recorded_and_benjamin_gets_nothing(
    app: Engine, fake: FastAPI, documents: MemoryDocumentStore
) -> None:
    graph = graph_for(fake)
    statements = Statements(documents, SETTINGS, graph, today=lambda: DECEMBER_2ND)
    blocked = OCTOBER  # imported first, then November one cent off is blocked
    fake.state.mailbox.add(statement_email(files=(("okt.csv", blocked),)))
    run_sweep(app, fake, documents, statements)
    off = NOVEMBER.replace(b'"-500.00","EUR"', b'"-500.01","EUR"')
    fake.state.mailbox.add(
        statement_email(files=(("nov.csv", off),), message_id="<nov@maxasp.example>")
    )
    run_sweep(app, fake, documents, statements)
    assert run_statements(StatementServices(app, graph, SETTINGS), today=date(2027, 1, 5))

    assert [s.outcome for s in statements_of(app)] == ["imported", "blocked"]
    assert fake.state.mailbox.notices == []
    statuses = {r.status for r in rows(app, "select status from bank_notice")}
    assert statuses == {"not_sent_shadow"}

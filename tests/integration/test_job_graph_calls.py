"""Each job does its whole work with a Graph client that has only its own calls (#168, NF-03).

The other integration tests build their Graph clients with every call, so they cannot show
that a job's list is complete. These run each job against the fake Graph and a real
PostgreSQL with the client the job builds in production, from the lists in
:mod:`argus.intake.jobs`. A call missing from a list makes the job fail here.
"""

import logging
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.bank.importer import import_statement
from argus.bank.notices import NoticeKind, remind_missing_statements
from argus.documents.store import MemoryDocumentStore
from argus.intake.digest import send_daily_digest
from argus.intake.email_text import EmailReader
from argus.intake.filing import REMOVED_FOLDER, Folders, remove_due
from argus.intake.graph import GraphCall, GraphCallRefused, GraphClient
from argus.intake.invoices import Invoicing
from argus.intake.jobs import (
    DIGEST_CALLS,
    OK_MARK,
    REMOVAL_CALLS,
    STATEMENT_REMINDER_CALLS,
    SWEEP_JOB_CALLS,
    Services,
    main,
)
from argus.intake.records import EmailStep
from argus.intake.statements import Statements
from argus.intake.sweep import DocumentArchive, Sorting, sweep
from argus.sorting import RecordedModel
from standins.graph.app import DELETED_ITEMS_ID, MAILBOX, create_app
from tests.acceptance.intake import FixtureDocumentReader
from tests.integration.test_bank_import import FROM_BENJAMIN, NOVEMBER, OCTOBER
from tests.integration.test_bank_import import SETTINGS as BANK_SETTINGS
from tests.integration.test_email_filing import filed_other, fixed, move_filing_back
from tests.integration.test_exception_digest import (
    APP_URL,
    DIGEST_TO,
    empty_queue,  # noqa: F401  resolves every open exception around each test
    next_day,
    open_audit_exceptions,
)
from tests.integration.test_reminder_forward import (
    FORWARD_TO,
    fake_graph_with,
)
from tests.integration.test_statement_handover import (
    DECEMBER_2ND,
    FOLDER,
    into_folder,
    statement_email,
)
from tests.integration.test_statement_handover import SETTINGS as HANDOVER_SETTINGS
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport
from tests.unit.sorting_helpers import MODEL_ANSWERS, OWN_VAT_IDS, fixture_config

INVOICE, REMINDER, OTHER = "E01", "E25", "E32"


def job_client(
    fake: FastAPI,
    calls: frozenset[GraphCall],
    *,
    forward_to: str | None = None,
    digest_to: str | None = None,
    statement_folder: str | None = None,
) -> GraphClient:
    """A client like the one the job builds: its own calls and its own addresses."""
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        sleep=lambda _: None,
        forward_to=forward_to,
        digest_to=digest_to,
        statement_folder=statement_folder,
        calls=calls,
    )


def steps(app: Engine) -> list[str]:
    with app.connect() as connection:
        return sorted(connection.execute(text("select step from mailbox_email")).scalars())


@pytest.mark.req("NF-03", "IN-01", "IN-05")
@pytest.mark.usefixtures("clean", "live")
def test_the_sweep_job_sorts_files_and_forwards_with_its_own_calls(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_graph_with(INVOICE, REMINDER, OTHER)

    def build(environ: object) -> Services:
        graph = job_client(fake, SWEEP_JOB_CALLS, forward_to=FORWARD_TO)
        documents = MemoryDocumentStore()
        sorting = Sorting(
            documents=documents,
            model=RecordedModel.from_file(MODEL_ANSWERS),
            config=fixture_config(),
            read=EmailReader(FixtureDocumentReader()),
            reminders=graph,
            invoicing=Invoicing(FixtureDocumentReader(), OWN_VAT_IDS),
        )
        return Services(app, graph, DocumentArchive(documents), sorting)

    with caplog.at_level(logging.INFO):
        assert main(["sweep"], environ={}, build=build) == 0

    assert f"{OK_MARK} sweep" in caplog.text
    assert "Refused Graph call" not in caplog.text
    assert steps(app) == [EmailStep.FILED.value] * 3
    assert len(fake.state.mailbox.sent) == 1  # the payment reminder went to Benjamin


@pytest.mark.req("NF-03", "IN-01")
@pytest.mark.usefixtures("clean", "shadow")
def test_the_sweep_job_runs_shadow_mode_with_its_own_calls(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_graph_with(INVOICE, OTHER)
    graph = job_client(fake, SWEEP_JOB_CALLS, forward_to=FORWARD_TO)
    documents = MemoryDocumentStore()
    archive = DocumentArchive(documents)
    sorting = Sorting(
        documents=documents,
        model=RecordedModel.from_file(MODEL_ANSWERS),
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
        reminders=graph,
        invoicing=Invoicing(FixtureDocumentReader(), OWN_VAT_IDS),
    )
    first = sweep(app, graph, archive, sorting)
    # A person deletes both emails in Outlook; the next sweep looks up where they went.
    box = fake.state.mailbox
    for message_id in list(box.location):
        box.location[message_id] = DELETED_ITEMS_ID
    second = sweep(app, graph, archive, sorting)

    assert (first.failed, second.failed) == (0, 0)
    assert second.sorted_by_person == 2
    assert box.moves == []  # shadow mode: ARGUS moved nothing
    assert "Refused Graph call" not in caplog.text


@pytest.mark.req("NF-03", "BA-01")
@pytest.mark.usefixtures("clean", "clean_bank", "live")
def test_the_sweep_job_imports_a_statement_from_the_folder_with_its_own_calls(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = create_app(token=TOKEN, emails=[])
    into_folder(fake, statement_email(sender="someone@maxasp.example"))
    # From Benjamin into the Inbox: handed over, then moved into the statement folder.
    benjamin = fake.state.mailbox.add(
        statement_email(
            files=(("statement_EUR_2026-11.csv", NOVEMBER),), message_id="<statement-2@x.example>"
        )
    )
    documents = MemoryDocumentStore()
    graph = job_client(fake, SWEEP_JOB_CALLS, forward_to=FORWARD_TO, statement_folder=FOLDER)
    statements = Statements(documents, HANDOVER_SETTINGS, graph, today=lambda: DECEMBER_2ND)

    result = sweep(app, graph, DocumentArchive(documents), None, statements)

    assert result.failed == 0
    with app.connect() as connection:
        imported: int = connection.execute(text("select count(*) from bank_statement")).scalar_one()
    assert imported == 2
    assert benjamin.immutable_id in fake.state.mailbox.in_folder(FOLDER)
    assert "Refused Graph call" not in caplog.text


@pytest.mark.req("NF-03", "IN-06")
@pytest.mark.usefixtures("clean", "live")
def test_the_removal_job_deletes_with_its_own_calls_and_cannot_list_or_move(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)  # filed by a sweep, which has its own client
    filed_at = move_filing_back(owner, other.id, days=30)
    removal = job_client(fake_graph, REMOVAL_CALLS)

    result = remove_due(app, removal, Folders(removal), fixed(filed_at + timedelta(days=30)))

    assert (result.due, result.deleted, result.failed) == (1, 1, 0)
    box = fake_graph.state.mailbox
    assert set(box.deleted) == {other.graph_message_id}
    assert box.in_folder(REMOVED_FOLDER) == set()
    with pytest.raises(GraphCallRefused):
        next(removal.list_inbox())
    with pytest.raises(GraphCallRefused):
        removal.move(other.graph_message_id, other.graph_message_id)


@pytest.mark.req("NF-03", "EX-03")
def test_the_digest_job_sends_with_its_own_calls(app: Engine) -> None:
    fake = create_app(token=TOKEN)
    open_audit_exceptions(app)
    digest = job_client(fake, DIGEST_CALLS, digest_to=DIGEST_TO)

    assert send_daily_digest(app, digest, APP_URL, today=next_day()).ok

    assert len(fake.state.mailbox.sent) == 1


@pytest.mark.req("NF-03", "BA-01")
@pytest.mark.usefixtures("clean_bank", "live")
def test_the_statement_job_reminds_with_its_own_calls(app: Engine) -> None:
    fake = create_app(token=TOKEN)
    import_statement(
        app,
        MemoryDocumentStore(),
        BANK_SETTINGS,
        FROM_BENJAMIN,
        OCTOBER,
        today=date(2026, 11, 2),
    )
    notices = job_client(fake, STATEMENT_REMINDER_CALLS, forward_to=FORWARD_TO)

    sent = remind_missing_statements(app, BANK_SETTINGS.balances, notices, today=date(2026, 11, 3))

    assert sent is NoticeKind.STATEMENT_MISSING
    assert len(fake.state.mailbox.notices) == 1

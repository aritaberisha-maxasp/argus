"""One run of the scheduled sweep job turns a new email into a sorted case (#125, IN-01, NF-01).

The job is started the way Azure starts it, through ``main``, against the fake Graph, the
recorded model answers and a real PostgreSQL. Only the building of the clients is swapped,
because CI has no Azure OpenAI and no Document Intelligence.
"""

import logging
import threading
from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.documents.store import MemoryDocumentStore
from argus.intake.email_text import EmailReader
from argus.intake.invoices import Invoicing
from argus.intake.jobs import FAILED_MARK, OK_MARK, Services, main
from argus.intake.records import EmailStep
from argus.intake.sweep import DocumentArchive, Sorting, sweep_lock
from argus.sorting import RecordedModel
from tests.acceptance.intake import FixtureDocumentReader
from tests.integration.test_reminder_forward import (
    FORWARD_TO,
    clean,  # noqa: F401  wipes forwards and email records around each test
    fake_graph_with,
    graph_for,
)
from tests.unit.sorting_helpers import MODEL_ANSWERS, OWN_VAT_IDS, fixture_config

INVOICE, REMINDER, OTHER = "E01", "E25", "E32"


def build_for(app: Engine, fake: FastAPI) -> "BuildServices":
    return BuildServices(app, fake)


class BuildServices:
    """Builds the services of one run the way :func:`services_from_env` does, with the CI
    stand ins in place of the Azure clients."""

    def __init__(self, app: Engine, fake: FastAPI) -> None:
        self.app = app
        self.fake = fake
        self.runs = 0

    def __call__(self, environ: Mapping[str, str]) -> Services:
        self.runs += 1
        graph = graph_for(self.fake, FORWARD_TO)
        documents = MemoryDocumentStore()
        sorting = Sorting(
            documents=documents,
            model=RecordedModel.from_file(MODEL_ANSWERS),
            config=fixture_config(),
            read=EmailReader(FixtureDocumentReader()),
            reminders=graph,
            invoicing=Invoicing(FixtureDocumentReader(), OWN_VAT_IDS),
        )
        return Services(self.app, graph, DocumentArchive(documents), sorting)


def steps(app: Engine) -> list[str]:
    with app.connect() as connection:
        return sorted(connection.execute(text("select step from mailbox_email")).scalars())


def cases(app: Engine) -> int:
    with app.connect() as connection:
        found: int = connection.execute(
            text(
                "select count(*) from invoice_case c join mailbox_email e"
                " on e.id = c.source_email_id"
            )
        ).scalar_one()
    return found


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_one_sweep_run_turns_new_emails_into_sorted_cases(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build = build_for(app, fake_graph_with(INVOICE, REMINDER, OTHER))
    with caplog.at_level(logging.INFO):
        assert main(["sweep"], environ={}, build=build) == 0
    assert f"{OK_MARK} sweep" in caplog.text
    assert steps(app) == [EmailStep.FILED.value] * 3
    assert cases(app) == 1  # the incoming invoice
    # The next run finds nothing new and changes nothing.
    assert main(["sweep"], environ={}, build=build) == 0
    assert steps(app) == [EmailStep.FILED.value] * 3
    assert cases(app) == 1


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_run_while_another_sweep_works_does_nothing_and_is_not_a_failure(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build = build_for(app, fake_graph_with(INVOICE))
    with sweep_lock(app) as got, caplog.at_level(logging.INFO):
        assert got
        assert main(["sweep"], environ={}, build=build) == 0
    assert "another sweep is running" in caplog.text
    assert f"{OK_MARK} sweep" in caplog.text
    assert steps(app) == []


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_two_runs_started_together_process_each_email_once(app: Engine) -> None:
    build = build_for(app, fake_graph_with(INVOICE, OTHER))
    codes: list[int] = []
    runs = [
        threading.Thread(target=lambda: codes.append(main(["sweep"], environ={}, build=build)))
        for _ in range(2)
    ]
    for run in runs:
        run.start()
    for run in runs:
        run.join()
    assert codes == [0, 0]
    assert steps(app) == [EmailStep.FILED.value] * 2
    assert cases(app) == 1


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_failed_run_is_reported_and_the_next_run_still_works(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_graph_with(INVOICE)
    working = build_for(app, fake)

    def mailbox_down(environ: Mapping[str, str]) -> Services:
        raise ConnectionError("the mailbox could not be reached")

    with caplog.at_level(logging.INFO):
        assert main(["sweep"], environ={}, build=mailbox_down) == 1
        assert f"{FAILED_MARK} sweep" in caplog.text
        assert main(["sweep"], environ={}, build=working) == 0
    assert steps(app) == [EmailStep.FILED.value]


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_the_removal_job_runs_once_and_exits_clean(
    app: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build = build_for(app, fake_graph_with(OTHER))
    assert main(["sweep"], environ={}, build=build) == 0
    with caplog.at_level(logging.INFO):
        # Nothing has been in ARGUS/Removed for 30 days yet, so nothing is deleted.
        assert main(["removal"], environ={}, build=build) == 0
    assert f"{OK_MARK} removal" in caplog.text
    assert steps(app) == [EmailStep.FILED.value]

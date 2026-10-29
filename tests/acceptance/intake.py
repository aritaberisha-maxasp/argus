"""The 50 fixture emails taken through intake, for AC-1 and AC-8.

What runs for real: the fake Graph serves the 50 ``.eml`` fixtures, the mailbox sweep of #24
picks them up and stores them in Azurite, the sorting of #25 sorts each one as read back from
the document store, and a second sweep files each email into its ARGUS folder (#26).

What is stood in: the sweep does not sort yet (#91). Until then this module reads each stored
email with the test reader of the sorting tests, records the decision with the rights the
application has, and routes the email, as the filing tests do. The AC-1 check that the sweep
does this on its own waits on #91.
"""

import email
from dataclasses import dataclass
from email import policy

from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.documents.store import DocumentStore
from argus.intake.filing import FOLDER_FOR
from argus.intake.graph import GraphClient
from argus.intake.records import mark_routed
from argus.intake.sweep import DocumentArchive, SweepResult, sweep
from argus.sorting import RecordedModel, Route, SortingDecision, sort_email
from standins.graph.app import create_app
from tests.unit.intake.graph_helpers import TOKEN, client_for
from tests.unit.sorting_helpers import (
    EMAILS,
    MODEL_ANSWERS,
    fixture_config,
    manifest,
    read_raw_email,
)

# Stands in for the sorting step of the sweep until #91 records it.
SORTING_STANDIN = Actor.system("acceptance-sorting-standin")


@dataclass(frozen=True)
class IntakeEmail:
    """One fixture email after intake."""

    fixture_id: str  # E01 to E50, the email_id column of manifest.csv
    record_id: int  # mailbox_email.id
    expected_category: str
    expected_route: Route
    decision: SortingDecision
    folder: str | None  # the ARGUS folder the fake Graph holds it in, None if still in the Inbox

    @property
    def landed_as_expected(self) -> bool:
        return (
            self.decision.route == self.expected_route
            and self.folder == FOLDER_FOR[self.expected_route]
        )


@dataclass(frozen=True)
class IntakeRun:
    first_sweep: SweepResult
    second_sweep: SweepResult
    emails: tuple[IntakeEmail, ...]
    steps_after_first_sweep: dict[int, str]  # mailbox_email.id to step, before any stand in


def _message_id(raw: bytes) -> str:
    return str(email.message_from_bytes(raw, policy=policy.default).get("Message-ID", "")).strip()


def _wipe_emails(owner: Engine) -> None:
    """Start from no email records. Records cannot be deleted, so the owner turns the guard off
    for this cleanup only, as the ``clean`` fixture of the integration tests does."""
    with owner.begin() as connection:
        connection.execute(
            text("alter table mailbox_email disable trigger mailbox_email_no_delete")
        )
        connection.execute(text("delete from mailbox_email"))
        connection.execute(text("alter table mailbox_email enable trigger mailbox_email_no_delete"))


def _folder_of(fake: FastAPI) -> dict[str, str]:
    mailbox = fake.state.mailbox
    return {
        message_id: path for path in FOLDER_FOR.values() for message_id in mailbox.in_folder(path)
    }


def run_intake(owner: Engine, app: Engine, documents: DocumentStore) -> IntakeRun:
    _wipe_emails(owner)
    fake = create_app(token=TOKEN)
    graph: GraphClient = client_for(fake)
    archive = DocumentArchive(documents)

    first = sweep(app, graph, archive)
    with app.connect() as connection:
        rows = connection.execute(
            text("select id, graph_message_id, step, raw_sha256 from mailbox_email order by id")
        ).all()
    steps = {row.id: row.step for row in rows}

    expected = {_message_id((EMAILS / entry["file"]).read_bytes()): entry for entry in manifest()}
    model, config = RecordedModel.from_file(MODEL_ANSWERS), fixture_config()
    decided: list[tuple[int, str, dict[str, str], SortingDecision]] = []
    for row in rows:
        raw = documents.get(row.raw_sha256)
        entry = expected[_message_id(raw)]
        decision = sort_email(read_raw_email(raw), model, config)
        # Stand in for #91: classify, then route, as the application.
        with unit_of_work(app, SORTING_STANDIN) as db:
            db.execute(
                text("update mailbox_email set step = 'classified' where id = :id"),
                {"id": row.id},
            )
            assert mark_routed(db, row.id, decision.route)
        decided.append((row.id, row.graph_message_id, entry, decision))

    second = sweep(app, graph, archive)
    folders = _folder_of(fake)
    emails = tuple(
        IntakeEmail(
            fixture_id=entry["email_id"],
            record_id=record_id,
            expected_category=entry["expected_category"],
            expected_route=Route(entry["expected_route"]),
            decision=decision,
            folder=folders.get(graph_id),
        )
        for record_id, graph_id, entry, decision in decided
    )
    return IntakeRun(first, second, emails, steps)

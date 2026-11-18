"""The 50 fixture emails taken through intake, for AC-1 and AC-8.

What runs for real: the fake Graph serves the 50 ``.eml`` fixtures, the mailbox sweep of #24
picks them up and stores them in Azurite, sorts each one as read back from the document store
(#91, with the sorting of #25 and the email reader of #91), opens one case per invoice of
every incoming invoice email (#30), routes it, forwards the payment
reminders (#27), files each email into its ARGUS folder (#26) and matches every case to its
vendor account in the fake DATEV, with the fixture approver table loaded (#35). A second sweep
finds nothing left to do.

What is stood in: the language model answers from its recordings, and the document reader
reads PDFs by their text layer and the two scans from their recorded readings, as in the
sorting tests.
"""

import email
import hashlib
import io
from dataclasses import dataclass
from email import policy

import pypdf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.approvers.table import ChangedBy, load_approvers_csv
from argus.cases.model import Actor
from argus.datev import DatevConnectGateway
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev
from argus.documents.email import attachments_of
from argus.documents.store import DocumentStore
from argus.intake.email_text import EmailReader
from argus.intake.filing import FOLDER_FOR
from argus.intake.graph import GraphClient
from argus.intake.invoices import Invoicing
from argus.intake.mode import IntakeMode, SwitchedBy, switch_intake_mode
from argus.intake.records import classification_of
from argus.intake.sweep import DocumentArchive, Sorting, SweepResult, sweep
from argus.reading.pages import Analysis, DocumentNotReadable, Page
from argus.sorting import RecordedModel, Route, SortingDecision
from standins.graph.app import MAILBOX, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport
from tests.unit.sorting_helpers import (
    EMAILS,
    MODEL_ANSWERS,
    OWN_VAT_IDS,
    READINGS,
    fixture_config,
    manifest,
)

APPROVERS = EMAILS.parent / "master-data" / "approvers.csv"

# The test stand in that recorded classified and routed before #91. Nothing records under this
# name any more; AC-8 checks that no step in the trail carries it.
SORTING_STANDIN = Actor.system("acceptance-sorting-standin")
REMINDER_ADDRESS = "reminders@maxasp.example"


class FixtureDocumentReader:
    """Stands in for Document Intelligence: the text layer of each PDF, and the recorded
    reading of the two readable scans. Any other scan cannot be read (E46)."""

    name = "fixture"

    def __init__(self) -> None:
        self._readings: dict[str, str] = {}
        for entry in manifest():
            raw = (EMAILS / entry["file"]).read_bytes()
            for attachment in attachments_of(raw):
                reading = READINGS / f"{attachment.filename}.txt"
                if reading.exists():
                    digest = hashlib.sha256(attachment.data).hexdigest()
                    self._readings[digest] = reading.read_text(encoding="utf-8")

    def read(self, data: bytes, content_type: str) -> Analysis:
        recorded = self._readings.get(hashlib.sha256(data).hexdigest())
        if recorded is not None:
            return Analysis(pages=(Page(1, recorded),))
        if content_type == "application/pdf":
            pdf = pypdf.PdfReader(io.BytesIO(data))
            pages = [Page(n, p.extract_text() or "") for n, p in enumerate(pdf.pages, start=1)]
            return Analysis(pages=tuple(pages))
        raise DocumentNotReadable("no recorded reading for this scan")


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


# A made up Entra object ID for the admin who switches to live mode (#72).
ADMIN = SwitchedBy(
    Actor.user("44444444-4444-4444-8444-444444444444"),
    name="Test Admin",
    reason="acceptance run in live mode",
)


def _wipe_emails(owner: Engine) -> None:
    """Start from no email records. Records cannot be deleted, so the owner turns the guards off
    for this cleanup only, as the ``clean`` fixture of the integration tests does."""
    with owner.begin() as connection:
        # Stored documents point at the records and are append only. The replica role lets
        # this test cleanup leave them behind; record IDs are never reused (#90).
        connection.execute(text("set local session_replication_role = replica"))
        for table, trigger in (
            ("reminder_forward", "reminder_forward_no_delete"),
            ("mailbox_email", "mailbox_email_no_delete"),
        ):
            connection.execute(text(f"alter table {table} disable trigger {trigger}"))
            connection.execute(text(f"delete from {table}"))  # noqa: S608  fixed names
            connection.execute(text(f"alter table {table} enable trigger {trigger}"))


def _folder_of(fake: FastAPI) -> dict[str, str]:
    mailbox = fake.state.mailbox
    return {
        message_id: path for path in FOLDER_FOR.values() for message_id in mailbox.in_folder(path)
    }


def run_intake(owner: Engine, app: Engine, documents: DocumentStore) -> IntakeRun:
    """Both sweeps run in live mode, as after the shadow run (#72). Back to shadow after."""
    _wipe_emails(owner)
    switch_intake_mode(owner, ADMIN, IntakeMode.LIVE)
    try:
        return _run_intake(app, documents)
    finally:
        switch_intake_mode(owner, ADMIN, IntakeMode.SHADOW)


def _run_intake(app: Engine, documents: DocumentStore) -> IntakeRun:
    load_approvers_csv(app, APPROVERS, ChangedBy(ADMIN.actor, ADMIN.name))
    with TestClient(create_fake_datev()) as datev:
        return _sweep_twice(app, documents, DatevConnectGateway(datev, DEFAULT_CLIENT_ID))


def _sweep_twice(app: Engine, documents: DocumentStore, datev: DatevConnectGateway) -> IntakeRun:
    fake = create_app(token=TOKEN)
    graph = GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        sleep=lambda _: None,
        forward_to=REMINDER_ADDRESS,
    )
    archive = DocumentArchive(documents)
    sorting = Sorting(
        documents=documents,
        model=RecordedModel.from_file(MODEL_ANSWERS),
        config=fixture_config(),
        read=EmailReader(FixtureDocumentReader()),
        reminders=graph,
        invoicing=Invoicing(FixtureDocumentReader(), OWN_VAT_IDS),
        vendors=datev,
    )

    first = sweep(app, graph, archive, sorting)
    with app.connect() as connection:
        rows = connection.execute(
            text("select id, graph_message_id, step, raw_sha256 from mailbox_email order by id")
        ).all()
        decisions = {row.id: classification_of(connection, row.id) for row in rows}
    steps = {row.id: row.step for row in rows}

    expected = {_message_id((EMAILS / entry["file"]).read_bytes()): entry for entry in manifest()}
    second = sweep(app, graph, archive, sorting)
    folders = _folder_of(fake)
    emails = []
    for row in rows:
        entry = expected[_message_id(documents.get(row.raw_sha256))]
        details = decisions[row.id]
        assert details is not None, f"{entry['email_id']} was not sorted by the sweep"
        emails.append(
            IntakeEmail(
                fixture_id=entry["email_id"],
                record_id=row.id,
                expected_category=entry["expected_category"],
                expected_route=Route(entry["expected_route"]),
                decision=SortingDecision.from_audit_details(details),
                folder=folders.get(row.graph_message_id),
            )
        )
    return IntakeRun(first, second, tuple(emails), steps)

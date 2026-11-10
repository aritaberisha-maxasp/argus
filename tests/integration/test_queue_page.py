"""The exception queue page against a real PostgreSQL (#126, EX-01, EX-02, NF-03).

The app reads with the rights of argus_app. The files come from an in memory document store
that checks fingerprints like the real one. Every name, address and amount below is made up.
"""

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, ExceptionReason, Outcome
from argus.cases.store import create_case, open_exception, resolve_exception, unit_of_work
from argus.documents.email import store_email
from argus.documents.records import link_document_to_case, record_email_documents
from argus.documents.store import MemoryDocumentStore
from argus.intake.graph import InboxMessage
from argus.intake.records import mark_classified, mark_stored, see_email
from argus.intake.sweep import SWEEP_ACTOR
from argus.sorting.models import Route
from argus.sorting.sender import Authentication, SenderCheck
from standins.entra.app import FakeEntra
from tests.unit.sorting_helpers import decision_for

pytestmark = pytest.mark.req("EX-01", "EX-02", "NF-03")

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up

# Personal data that must never reach a log line (NF-03).
SENDER = "Erika Mustermann <erika.mustermann@lieferant-test.example>"
SUBJECT = "Rechnung RE-QUEUE-4711 fuer Oktober"
FILENAME = "Rechnung Erika Mustermann.pdf"
PDF = b"%PDF-1.4\n% made up invoice for the queue page test\n%%EOF\n"


@dataclass(frozen=True)
class Stored:
    email_id: int
    raw_document_id: int
    attachment_document_id: int
    raw: bytes


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def store() -> MemoryDocumentStore:
    return MemoryDocumentStore()


@pytest.fixture
def client(app: Engine, entra: FakeEntra, store: MemoryDocumentStore) -> TestClient:
    return TestClient(
        create_app(lambda: app, sign_in=entra.verifier, documents_factory=lambda: store)
    )


def as_role(entra: FakeEntra, role: Role | None, who: str = BENJAMIN) -> dict[str, str]:
    token = entra.token(who, roles=[role] if role else [])
    return {"Authorization": f"Bearer {token}"}


def received_email(app: Engine, store: MemoryDocumentStore) -> Stored:
    """An email with one PDF, stored, recorded and sorted like the sweep does it."""
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "buchhaltung@max-asp-test.example"
    message["Subject"] = SUBJECT
    message["Date"] = "Tue, 10 Nov 2026 09:00:00 +0100"
    message["Message-ID"] = f"<{uuid.uuid4().hex}@lieferant-test.example>"
    message.set_content("Guten Tag,\nanbei die Rechnung.\n<script>alert(1)</script>\n")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename=FILENAME)
    raw = message.as_bytes()
    stored = store_email(store, raw)
    decision = replace(
        decision_for(Route.MANUAL_REVIEW),
        sender=SenderCheck(
            claimed="unknown",
            treated_as="unknown",
            authentication=Authentication(
                from_domain="lieferant-test.example", passed_by="dmarc", results=()
            ),
        ),
    )
    with unit_of_work(app, SWEEP_ACTOR) as db:
        record, _ = see_email(
            db,
            InboxMessage(
                id=f"AAMk-{uuid.uuid4().hex}",
                internet_message_id=message["Message-ID"],
                received_at=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
            ),
        )
        assert mark_stored(db, record.id, stored.raw.sha256, stored.raw.location)
        ids = record_email_documents(db, record.id, stored)
        assert mark_classified(db, record.id, decision)
    return Stored(record.id, ids[0], ids[1], raw)


def email_exception(app: Engine, email_id: int) -> int:
    with unit_of_work(app, SWEEP_ACTOR) as db:
        return open_exception(
            db,
            ExceptionReason.UNCERTAIN_CLASSIFICATION,
            "ARGUS could not sort this email on its own: the model and our checks disagree",
            email_id=email_id,
        )


def case_exception(app: Engine, email: Stored) -> tuple[int, int]:
    with unit_of_work(app, Actor.system("intake")) as db:
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email.email_id,
            partner_account="70001",
            document_number=f"RE-QUEUE-{uuid.uuid4().hex[:6]}",
            gross_cents=892500,
            currency="EUR",
        )
        link_document_to_case(db, case_id, email.attachment_document_id)
        exception_id = open_exception(
            db,
            ExceptionReason.INVOICE_CHECK_FAILED,
            "The invoice has no delivery date (§14 UStG).",
            case_id=case_id,
            facts={"missing": ["delivery_date"]},
        )
    return case_id, exception_id


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


def assert_no_personal_data(caplog: pytest.LogCaptureFixture) -> None:
    formatter = logging.Formatter()
    logged = "\n".join(
        record.getMessage()
        + (formatter.formatException(record.exc_info) if record.exc_info else "")
        for record in caplog.records
    )
    for personal in ("Mustermann", "erika", SUBJECT, FILENAME, "RE-QUEUE"):
        assert personal not in logged


# --- AC: the list shows every open exception with case, reason, age and sender ---


def test_benjamin_sees_every_open_exception_with_case_reason_age_and_sender(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    captured: pytest.LogCaptureFixture,
) -> None:
    email = received_email(app, store)
    on_email = email_exception(app, email.email_id)
    case_id, on_case = case_exception(app, email)

    response = client.get("/exceptions", headers=as_role(entra, Role.FINANCE))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    rows = {r["exception_id"]: r for r in response.json()["exceptions"]}
    assert rows[on_email]["reason"] == "uncertain_classification"
    assert rows[on_email]["reason_text"] == "ARGUS could not sort the email with certainty"
    assert rows[on_email]["case_id"] is None
    assert rows[on_email]["email_id"] == email.email_id
    assert rows[on_email]["sender"] == "lieferant-test.example"
    assert rows[on_email]["age_working_days"] == 0
    assert rows[on_case]["case_id"] == case_id
    assert rows[on_case]["case_kind"] == "incoming_invoice"
    assert rows[on_case]["email_id"] == email.email_id  # the email the case came from
    assert rows[on_case]["sender"] == "lieferant-test.example"
    # Oldest first.
    order = [r["exception_id"] for r in response.json()["exceptions"]]
    assert order.index(on_email) < order.index(on_case)
    assert_no_personal_data(captured)


def test_the_age_counts_working_days_by_the_database_clock(
    app: Engine, owner: Engine, client: TestClient, entra: FakeEntra, store: MemoryDocumentStore
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)
    with owner.begin() as db:
        # Only the owner can move the opening time; ten days back is at least six working days.
        db.execute(
            text("alter table exception_item disable trigger user"),
        )
        db.execute(
            text(
                "update exception_item set opened_at = opened_at - interval '10 days' "
                "where id = :id"
            ),
            {"id": exception_id},
        )
        db.execute(text("alter table exception_item enable trigger user"))

    rows = client.get("/exceptions", headers=as_role(entra, Role.ADMIN, ARITA)).json()
    age = next(r for r in rows["exceptions"] if r["exception_id"] == exception_id)[
        "age_working_days"
    ]

    assert 6 <= age <= 8


def test_a_resolved_exception_leaves_the_queue(
    app: Engine, client: TestClient, entra: FakeEntra, store: MemoryDocumentStore
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)
    with unit_of_work(app, Actor.user(BENJAMIN)) as db:
        resolve_exception(db, exception_id, Outcome.ANSWERED, resolution="routed by hand")

    listed = client.get("/exceptions", headers=as_role(entra, Role.FINANCE)).json()
    opened = client.get(f"/exceptions/{exception_id}", headers=as_role(entra, Role.FINANCE))

    assert exception_id not in [r["exception_id"] for r in listed["exceptions"]]
    assert opened.status_code == 404


# --- AC: each row opens the case, the original email, its files and why ARGUS stopped ---


def test_one_exception_shows_why_argus_stopped_the_case_and_the_original_email(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    captured: pytest.LogCaptureFixture,
) -> None:
    email = received_email(app, store)
    case_id, exception_id = case_exception(app, email)

    response = client.get(f"/exceptions/{exception_id}", headers=as_role(entra, Role.FINANCE))

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "invoice_check_failed"
    assert body["detail"] == "The invoice has no delivery date (§14 UStG)."
    assert body["facts"] == {"missing": ["delivery_date"]}
    assert body["case"]["id"] == case_id
    assert body["case"]["status"] == "exception"
    assert body["case"]["resume_status"] == "received"
    assert body["case"]["gross_cents"] == 892500
    assert body["email"]["sender"] == SENDER
    assert body["email"]["subject"] == SUBJECT
    assert "anbei die Rechnung" in body["email"]["body"]
    assert body["email_problem"] is None
    files = body["files"]
    assert [(f["kind"], f["filename"], f["linked_to_case"]) for f in files] == [
        ("email", None, False),
        ("attachment", FILENAME, True),
    ]
    assert files[1]["content_type"] == "application/pdf"
    assert files[1]["size_bytes"] == len(PDF)
    assert len(files[1]["fingerprint"]) == 64
    assert_no_personal_data(captured)


def test_each_file_downloads_exactly_as_stored(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    captured: pytest.LogCaptureFixture,
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)
    headers = as_role(entra, Role.FINANCE)

    raw = client.get(f"/exceptions/{exception_id}/files/{email.raw_document_id}", headers=headers)
    pdf = client.get(
        f"/exceptions/{exception_id}/files/{email.attachment_document_id}", headers=headers
    )

    assert raw.status_code == pdf.status_code == 200
    assert raw.content == email.raw
    assert pdf.content == PDF
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith('attachment; filename="argus-')
    assert "sandbox" in pdf.headers["content-security-policy"]
    assert_no_personal_data(captured)


def test_a_file_of_another_email_is_not_reachable_through_an_exception(
    app: Engine, client: TestClient, entra: FakeEntra, store: MemoryDocumentStore
) -> None:
    mine = received_email(app, store)
    other = received_email(app, store)
    exception_id = email_exception(app, mine.email_id)

    response = client.get(
        f"/exceptions/{exception_id}/files/{other.attachment_document_id}",
        headers=as_role(entra, Role.FINANCE),
    )

    assert response.status_code == 404


def test_when_the_document_store_is_down_the_rest_still_shows(
    app: Engine,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    captured: pytest.LogCaptureFixture,
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)

    class Down(MemoryDocumentStore):
        def get(self, sha256: str) -> bytes:
            raise ConnectionError(f"blob store unreachable for {FILENAME}")

    client = TestClient(create_app(lambda: app, sign_in=entra.verifier, documents_factory=Down))
    headers = as_role(entra, Role.FINANCE)
    shown = client.get(f"/exceptions/{exception_id}", headers=headers)
    download = client.get(
        f"/exceptions/{exception_id}/files/{email.attachment_document_id}", headers=headers
    )

    assert shown.status_code == 200
    assert shown.json()["email"] is None
    assert shown.json()["email_problem"] == "The stored email could not be loaded right now."
    assert len(shown.json()["files"]) == 2
    assert download.status_code == 503
    # The store's error names the file; the log keeps only the type of error.
    assert_no_personal_data(captured)
    assert "ConnectionError" in captured.text


# --- AC: a person without the right role gets no access ---


@pytest.mark.parametrize("role", [Role.APPROVER, None])
def test_an_approver_or_a_person_without_a_role_gets_no_queue(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    role: Role | None,
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)
    headers = as_role(entra, role, ALICE)

    responses: list[httpx.Response] = [
        client.get("/exceptions", headers=headers),
        client.get(f"/exceptions/{exception_id}", headers=headers),
        client.get(f"/exceptions/{exception_id}/files/{email.raw_document_id}", headers=headers),
    ]

    for response in responses:
        assert response.status_code == 403
        assert SENDER not in response.text and "lieferant" not in response.text


def test_without_sign_in_there_is_no_queue(
    app: Engine, client: TestClient, store: MemoryDocumentStore
) -> None:
    email = received_email(app, store)
    exception_id = email_exception(app, email.email_id)

    assert client.get("/exceptions").status_code == 401
    assert client.get(f"/exceptions/{exception_id}").status_code == 401

"""The trail of one case on the invoices page and as a file, against a real PostgreSQL (#63,
NF-01, AP-08, AC-8).

The app reads with the rights of argus_app, as in production. Every name, number and amount
below is made up.
"""

import csv
import io
import logging
import secrets
import string
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.approvers.table import ChangedBy, save_approver
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason
from argus.cases.store import (
    correct_case,
    create_case,
    move_case,
    open_exception,
    unit_of_work,
)
from argus.documents.store import MemoryDocumentStore
from argus.invoices_page.trail import FILE_COLUMNS
from standins.entra.app import FakeEntra
from tests.integration.approval_helpers import HELPER_APPROVER, decide, prepare_for_approval
from tests.integration.test_queue_page import received_email

# Exceptions open only once ARGUS runs live (#72), like in the other page tests.
pytestmark = [pytest.mark.req("NF-01", "AP-08", "AC-8"), pytest.mark.usefixtures("live")]

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up
INTAKE = Actor.system("intake")
COMMENT = "Rang Erika Mustermann, she confirmed it"  # made up, must stay out of the log
CORRECTOR = "Max Mustermann"  # made up


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


@pytest.fixture
def tag() -> str:
    return f"TR{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


def as_role(entra: FakeEntra, role: Role | None, who: str = BENJAMIN) -> dict[str, str]:
    token = entra.token(who, roles=[role] if role else [])
    return {"Authorization": f"Bearer {token}"}


def booked_case_from_email(app: Engine, store: MemoryDocumentStore, number: str) -> int:
    """A case from a stored and sorted email, approved by a person and booked."""
    email = received_email(app, store)
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email.email_id,
            document_number=number,
            gross_cents=11900,
            currency="EUR",
            duplicate_key=f"key-{number}",
        )
    current = CaseStatus.RECEIVED
    for step in (CaseStatus.AWAITING_APPROVAL, CaseStatus.APPROVED, CaseStatus.BOOKED):
        with unit_of_work(app, INTAKE) as db:
            if step is CaseStatus.AWAITING_APPROVAL:
                prepare_for_approval(db, case_id)
            if step is CaseStatus.APPROVED:
                # Only the person asked approves, on the approval page (#38).
                assert decide(db, case_id)
                current = step
                continue
            move_case(
                db,
                case_id,
                expected=current,
                to=step,
                datev_booking_ref=f"DATEV-{number}" if step is CaseStatus.BOOKED else None,
            )
        current = step
    return case_id


def stop_and_resolve(
    app: Engine, client: TestClient, entra: FakeEntra, case_id: int, comment: str = COMMENT
) -> int:
    """Stop the case with a booking error and resolve it from the page with a comment."""
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.BOOKING_ERROR, "Detail for the person.", case_id=case_id
        )
    answer = client.post(
        f"/exceptions/{exception_id}/resolution",
        json={"outcome": "fixed_in_datev", "comment": comment},
        headers=as_role(entra, Role.FINANCE),
    )
    assert answer.status_code == 200, answer.text
    return exception_id


def trail(client: TestClient, entra: FakeEntra, case_id: int, role: Role = Role.FINANCE) -> Any:
    response = client.get(
        f"/cases/{case_id}/trail",
        headers=as_role(entra, role, ARITA if role is Role.ADMIN else BENJAMIN),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def exported(client: TestClient, entra: FakeEntra, case_id: int) -> tuple[Any, list[list[str]]]:
    response = client.get(f"/cases/{case_id}/trail.csv", headers=as_role(entra, Role.FINANCE))
    assert response.status_code == 200, response.text
    body = response.content.decode("utf-8")
    assert body.startswith("﻿")
    rows = list(csv.reader(io.StringIO(body[1:]), delimiter=";"))
    return response, rows


def table(rows: list[list[str]]) -> list[dict[str, str]]:
    """The step rows of an exported file, by column name."""
    start = rows.index(list(FILE_COLUMNS))
    return [dict(zip(FILE_COLUMNS, r, strict=True)) for r in rows[start + 1 :]]


# --- AC: every step with time and actor, from the email to the last status ---


def test_the_trail_shows_every_step_from_the_email_to_the_last_status(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id)

    body = trail(client, entra, case_id)
    steps = body["steps"]
    kinds = [s["event_type"] for s in steps]

    # It starts with the email, before there was a case.
    assert kinds[:2] == ["email_seen", "email_stored"]
    assert kinds.index("email_classified") < kinds.index("case_created")
    # Every status the case went through, in order, ending where it stands now.
    moves = [(s["from_status"], s["to_status"]) for s in steps if s["to_status"]]
    assert moves == [
        (None, "received"),
        ("received", "awaiting_approval"),
        ("awaiting_approval", "approved"),
        ("approved", "booked"),
        ("booked", "exception"),
        ("exception", "booked"),
    ]
    assert body["case"]["status"] == "booked"
    assert moves[-1][1] == body["case"]["status"]
    # Each step has a time, an actor and a sentence; times and chain numbers go forward.
    for s in steps:
        assert s["occurred_at"] and s["actor"] and s["actor_kind"] in ("system", "user")
        assert s["what"] and s["hash"]
    times = [datetime.fromisoformat(s["occurred_at"]) for s in steps]
    assert times == sorted(times)
    assert [s["chain_seq"] for s in steps] == sorted(s["chain_seq"] for s in steps)
    assert not body["truncated"]


def test_each_step_says_who_did_it(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id)

    steps = trail(client, entra, case_id)["steps"]
    approved = next(s for s in steps if s["to_status"] == "approved")
    # The person the approval request went to approved it (#38), with their name then.
    assert (approved["actor_kind"], approved["actor"]) == ("user", HELPER_APPROVER)
    assert approved["actor_name"] == "Helper Approver"
    booked = next(s for s in steps if s["to_status"] == "booked")
    assert (booked["actor_kind"], booked["actor"]) == ("system", "intake")
    resolved = next(s for s in steps if s["event_type"] == "exception_resolved")
    assert (resolved["actor_kind"], resolved["actor"]) == ("user", BENJAMIN)
    assert resolved["what"] == (
        "The exception was resolved as fixed in DATEV: " + resolved["what"].split(": ", 1)[1]
    )
    assert next(s for s in steps if s["to_status"] == "approved")["what"] == (
        "The status changed from Awaiting approval to Approved"
    )


def test_the_comment_of_a_resolved_exception_shows_with_its_step(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    """The audit log keeps no free text, so the comment comes from the exception (AP-08)."""
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id)

    steps = trail(client, entra, case_id)["steps"]
    commented = [(s["event_type"], s["comment"]) for s in steps if s["comment"]]
    assert commented == [("exception_resolved", COMMENT)]


def test_a_correction_shows_who_made_it_by_name_and_why(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    email = received_email(app, store)
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=email.email_id)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.VALUES_TO_CHECK, "Number unclear.", case_id=case_id
        )
    with unit_of_work(app, Actor.user(ARITA)) as db:
        correct_case(
            db,
            case_id,
            document_number=tag,
            gross_cents=5000,
            currency="EUR",
            reason="Read from the PDF",
            name=CORRECTOR,
            exception_id=exception_id,
        )

    steps = trail(client, entra, case_id, Role.ADMIN)["steps"]
    corrected = next(s for s in steps if s["event_type"] == "case_corrected")
    assert (corrected["actor"], corrected["actor_name"]) == (ARITA, CORRECTOR)
    assert corrected["comment"] == "Read from the PDF"
    assert f"document number: none → {tag}" in corrected["facts"]
    # Every other step by this person carries the name too.
    assert {s["actor_name"] for s in steps if s["actor"] == ARITA} == {CORRECTOR}
    assert {s["actor_name"] for s in steps if s["actor_kind"] == "system"} == {None}


def test_a_correction_keeps_the_name_given_then_not_the_name_in_the_table_today(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    """ADR 0008: the name as it was when the person acted, from the step itself."""
    person = "55555555-5555-4555-8555-555555555555"  # made up
    save_approver(
        app, ChangedBy(Actor.user(ARITA), "Arita Test"), person, "Name Today", "p@x.example"
    )
    email = received_email(app, store)
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=email.email_id)
        exception_id = open_exception(
            db, ExceptionReason.VALUES_TO_CHECK, "Number unclear.", case_id=case_id
        )
    with unit_of_work(app, Actor.user(person)) as db:
        correct_case(
            db,
            case_id,
            document_number=tag,
            gross_cents=5000,
            currency="EUR",
            reason="Read from the PDF",
            name="Name Back Then",
            exception_id=exception_id,
        )

    steps = trail(client, entra, case_id)["steps"]
    corrected = next(s for s in steps if s["event_type"] == "case_corrected")
    assert corrected["actor_name"] == "Name Back Then"


def test_the_trail_holds_only_this_case_and_its_own_email(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    mine = booked_case_from_email(app, store, tag)
    other = booked_case_from_email(app, store, tag + "X")

    with app.connect() as db:
        email_of: dict[int, int] = dict(
            db.execute(
                text("select id, source_email_id from invoice_case where id = any(:ids)"),
                {"ids": [mine, other]},
            ).all()
        )
    seqs = {s["chain_seq"] for s in trail(client, entra, mine)["steps"]}
    with app.connect() as db:
        foreign = db.execute(
            text(
                "select count(*) from audit_event where chain_seq = any(:seqs)"
                " and (case_id = :other or email_id = :other_email)"
            ),
            {"seqs": list(seqs), "other": other, "other_email": email_of[other]},
        ).scalar()
    assert foreign == 0


def bank_case_from_emailed_statement(
    app: Engine, store: MemoryDocumentStore, tag: str, *, by_email: bool = True
) -> tuple[int, int, int, int | None]:
    """Two bank transaction cases imported from one statement that came by email. Returns
    (case, other case on the same statement, statement, email). A currency of its own, so the
    one statement per month rule of migration 0014 never meets another test."""
    email_id = received_email(app, store).email_id if by_email else None
    currency = "Q" + "".join(secrets.choice(string.ascii_uppercase) for _ in range(2))
    with unit_of_work(app, Actor.system("bank")) as db:
        sha = uuid.uuid4().hex * 2
        statement_id: int = db.execute(
            text(
                """
                insert into bank_statement (sha256, location, size_bytes, source,
                                            source_email_id, currency, period, opening_minor,
                                            closing_minor, transaction_count, outcome)
                values (:sha, 'sha256/' || :sha, 1, :source, :email, :currency,
                        date '2026-10-01', 0, -4250, 2, 'imported')
                returning id
                """
            ),
            {
                "sha": sha,
                "source": "email" if by_email else "folder",
                "email": email_id,
                "currency": currency,
            },
        ).scalar_one()
        ids = []
        for n in (1, 2):
            case_id = create_case(db, CaseKind.BANK_TRANSACTION, partner_account=f"W{n}-{tag}")
            db.execute(
                text(
                    """
                    insert into bank_transaction (case_id, wise_balance_currency,
                                                  wise_transaction_id, booked_on, amount_minor,
                                                  statement_id)
                    values (:id, :currency, :tx, date '2026-10-12', -2125, :statement)
                    """
                ),
                {
                    "id": case_id,
                    "currency": currency,
                    "tx": f"TX{n}-{tag}",
                    "statement": statement_id,
                },
            )
            ids.append(case_id)
    return ids[0], ids[1], statement_id, email_id


def test_a_bank_transaction_trail_starts_with_the_statement_and_its_email(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    """A bank case has no email of its own: its receipt is the statement (compliance review)."""
    case_id, other, statement_id, _ = bank_case_from_emailed_statement(app, store, tag)

    steps = trail(client, entra, case_id)["steps"]
    kinds = [s["event_type"] for s in steps]

    assert kinds[:2] == ["email_seen", "email_stored"]
    assert kinds.index("bank_statement_recorded") < kinds.index("case_created")
    imported = next(s for s in steps if s["event_type"] == "bank_transaction_imported")
    assert f"bank statement: {statement_id}" in imported["facts"]
    # Nothing of the other transaction on the same statement.
    with app.connect() as db:
        foreign = db.execute(
            text("select count(*) from audit_event where chain_seq = any(:seqs) and case_id = :o"),
            {"seqs": [s["chain_seq"] for s in steps], "o": other},
        ).scalar()
    assert foreign == 0
    times = [datetime.fromisoformat(s["occurred_at"]) for s in steps]
    assert times == sorted(times)


def test_a_bank_transaction_from_a_statement_in_the_folder_starts_at_the_statement(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id, _, statement_id, _ = bank_case_from_emailed_statement(app, store, tag, by_email=False)
    steps = trail(client, entra, case_id)["steps"]
    assert [s["event_type"] for s in steps][:3] == [
        "bank_statement_recorded",
        "case_created",
        "bank_transaction_imported",
    ]
    assert f"bank statement: {statement_id}" in steps[0]["facts"]


def test_an_invoice_trail_holds_no_bank_statement_events(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    bank_case_from_emailed_statement(app, store, tag)
    case_id = booked_case_from_email(app, store, tag)
    kinds = {s["event_type"] for s in trail(client, entra, case_id)["steps"]}
    assert "bank_statement_recorded" not in kinds
    assert "bank_transaction_imported" not in kinds


# --- AC: the trail can be exported as a file ---


def test_the_trail_downloads_as_a_file_with_the_same_steps(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id)

    shown = trail(client, entra, case_id)["steps"]
    response, rows = exported(client, entra, case_id)

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="argus-case-{case_id}-trail-20')
    assert rows[0] == [f"ARGUS audit trail of case {case_id}"]
    assert rows[1][0].startswith("Exported ") and BENJAMIN in rows[1][0]
    assert rows[2] == ["Hash chain of the whole audit log: intact"]

    steps = table(rows)
    assert [int(s["Chain number"]) for s in steps] == [s["chain_seq"] for s in shown]
    assert [s["Event"] for s in steps] == [s["event_type"] for s in shown]
    assert [s["Hash"] for s in steps] == [s["hash"] for s in shown]
    assert [s["Step"] for s in steps] == [str(n) for n in range(1, len(shown) + 1)]
    # Berlin time with its offset, and the same moment as on the page.
    for row, step in zip(steps, shown, strict=True):
        moment = datetime.fromisoformat(row["Time (Berlin)"])
        assert row["Time (Berlin)"][-6:] in ("+01:00", "+02:00")
        assert moment == datetime.fromisoformat(step["occurred_at"]).replace(microsecond=0)
    assert [s["Comment"] for s in steps if s["Comment"]] == [COMMENT]


def test_a_comment_that_looks_like_a_formula_cannot_run_in_a_spreadsheet(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id, comment='=HYPERLINK("http://x.example","y")')

    _, rows = exported(client, entra, case_id)
    comments = [s["Comment"] for s in table(rows) if s["Comment"]]
    assert comments == ['\'=HYPERLINK("http://x.example","y")']


# --- AC: the trail cannot be edited from the page ---


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_route_changes_the_trail(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
    method: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    before = trail(client, entra, case_id)["steps"]
    for path in (f"/cases/{case_id}/trail", f"/cases/{case_id}/trail.csv"):
        response = client.request(method, path, headers=as_role(entra, Role.ADMIN, ARITA))
        assert response.status_code == 405, (method, path)
    assert trail(client, entra, case_id)["steps"] == before


def test_the_trail_is_read_in_a_read_only_transaction(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a mistake in the reading code could not change anything: the database refuses a
    write in the transaction the trail routes read in."""
    from argus.invoices_page.trail import case_trail as real

    case_id = booked_case_from_email(app, store, tag)
    seen: list[str] = []

    def spy(connection: Any, wanted: int) -> Any:
        seen.append(
            connection.execute(text("show transaction_read_only")).scalar()
            + " "
            + connection.execute(text("show transaction_isolation")).scalar()
        )
        return real(connection, wanted)

    monkeypatch.setattr("argus.invoices_page.web.case_trail", spy)
    trail(client, entra, case_id)
    exported(client, entra, case_id)
    assert seen == ["on repeatable read", "on repeatable read"]


# --- who may read it ---


def test_admin_and_finance_read_the_trail(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    assert trail(client, entra, case_id, Role.ADMIN)["case"]["case_id"] == case_id
    assert trail(client, entra, case_id, Role.FINANCE)["case"]["case_id"] == case_id


@pytest.mark.parametrize("suffix", ["trail", "trail.csv"])
def test_an_approver_or_someone_without_a_role_or_token_gets_no_trail(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    tag: str,
    suffix: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    path = f"/cases/{case_id}/{suffix}"
    assert client.get(path, headers=as_role(entra, Role.APPROVER, ALICE)).status_code == 403
    assert client.get(path, headers=as_role(entra, None)).status_code == 403
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("suffix", ["trail", "trail.csv"])
def test_an_unknown_or_impossible_case_number_is_refused(
    client: TestClient, entra: FakeEntra, suffix: str
) -> None:
    headers = as_role(entra, Role.FINANCE)
    assert client.get(f"/cases/{2**62}/{suffix}", headers=headers).status_code == 404
    assert client.get(f"/cases/0/{suffix}", headers=headers).status_code == 422
    assert client.get(f"/cases/{2**63}/{suffix}", headers=headers).status_code == 422
    assert client.get(f"/cases/abc/{suffix}", headers=headers).status_code == 422


def test_the_log_names_ids_and_counts_but_never_a_name_or_comment(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    store: MemoryDocumentStore,
    captured: pytest.LogCaptureFixture,
    tag: str,
) -> None:
    case_id = booked_case_from_email(app, store, tag)
    stop_and_resolve(app, client, entra, case_id)
    captured.clear()

    steps = trail(client, entra, case_id)["steps"]
    exported(client, entra, case_id)

    logged = "\n".join(r.getMessage() for r in captured.records)
    assert f"user {BENJAMIN} read the trail of case {case_id} ({len(steps)} steps)" in logged
    assert (
        f"user {BENJAMIN} exported the trail of case {case_id} ({len(steps)} steps, chain intact)"
        in logged
    )
    for personal in ("Mustermann", "Erika", "confirmed it", tag):
        assert personal not in logged

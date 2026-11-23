"""Resolving an exception from the invoice status page, against a real PostgreSQL (#62, EX-01).

The app writes with the rights of argus_app, so the database rules of migrations 0002, 0010 and
0013 apply as in production. Every name, number and amount below is made up.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason
from argus.cases.store import create_case, get_case, move_case, open_exception, unit_of_work
from argus.documents.store import MemoryDocumentStore
from argus.intake.duplicates import flag_if_duplicate
from standins.entra.app import FakeEntra
from tests.integration.approval_helpers import prepare_for_approval

# Exceptions open only once ARGUS runs live (#72), like in the other page tests.
pytestmark = [pytest.mark.req("EX-01", "NF-05", "NF-03"), pytest.mark.usefixtures("live")]

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up
INTAKE = Actor.system("intake")
APPROVER = Actor.user(ALICE)
COMMENT = "Rang Erika Mustermann, she confirmed it"  # made up, must stay out of log and audit


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    store = MemoryDocumentStore()
    return TestClient(
        create_app(lambda: app, sign_in=entra.verifier, documents_factory=lambda: store)
    )


@pytest.fixture
def tag() -> str:
    return f"RS{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


def as_role(entra: FakeEntra, role: Role | None, who: str = BENJAMIN) -> dict[str, str]:
    token = entra.token(who, roles=[role] if role else [])
    return {"Authorization": f"Bearer {token}"}


def case_in_exception(
    app: Engine,
    reason: ExceptionReason,
    number: str,
    *,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
    before: CaseStatus | None = None,
) -> tuple[int, int]:
    """A case that stopped with ``reason``, after reaching ``before`` if given."""
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            kind,
            document_number=number,
            gross_cents=11900,
            currency="EUR",
            duplicate_key=f"key-{number}",
        )
    path = [CaseStatus.AWAITING_APPROVAL, CaseStatus.APPROVED, CaseStatus.BOOKED]
    current = CaseStatus.RECEIVED
    for step in path[: path.index(before) + 1] if before else []:
        # Only a person approves (migration 0002); the rest is done by jobs.
        actor = APPROVER if step is CaseStatus.APPROVED else INTAKE
        with unit_of_work(app, actor) as db:
            if step is CaseStatus.AWAITING_APPROVAL:
                prepare_for_approval(db, case_id)
            move_case(
                db,
                case_id,
                expected=current,
                to=step,
                datev_booking_ref=f"DATEV-{number}" if step is CaseStatus.BOOKED else None,
            )
        current = step
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(db, reason, "Detail for the person.", case_id=case_id)
    return case_id, exception_id


def choices(client: TestClient, entra: FakeEntra, exception_id: int) -> dict[str, Any]:
    response = client.get(
        f"/exceptions/{exception_id}/choices", headers=as_role(entra, Role.FINANCE)
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def outcomes(body: dict[str, Any]) -> list[str]:
    return [c["outcome"] for c in body["choices"]]


def resolve(
    client: TestClient,
    entra: FakeEntra,
    exception_id: int,
    outcome: str,
    comment: str = COMMENT,
    role: Role = Role.FINANCE,
) -> Any:
    return client.post(
        f"/exceptions/{exception_id}/resolution",
        json={"outcome": outcome, "comment": comment},
        headers=as_role(entra, role, ARITA if role is Role.ADMIN else BENJAMIN),
    )


def resolved_row(owner: Engine, exception_id: int) -> dict[str, Any]:
    with owner.connect() as db:
        row = (
            db.execute(
                text("select outcome, resolution, resolved_by from exception_item where id = :id"),
                {"id": exception_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def audit_details(owner: Engine, case_id: int) -> str:
    with owner.connect() as db:
        rows = db.execute(
            text("select event_type, details::text from audit_event where case_id = :id"),
            {"id": case_id},
        ).all()
    return "\n".join(f"{t} {d}" for t, d in rows)


# --- AC: an exception can be resolved from the page ---


def test_checked_carry_on_sends_the_case_back_and_records_who_and_why(
    app: Engine, owner: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    case_id, exception_id = case_in_exception(app, ExceptionReason.INVOICE_CHECK_FAILED, tag)

    offered = choices(client, entra, exception_id)
    assert offered["case_id"] == case_id
    assert offered["reason"] == "invoice_check_failed"
    assert offered["detail"] == "Detail for the person."
    assert offered["resume_status"] == "received"
    assert outcomes(offered) == ["accepted_with_comment", "rejected"]

    response = resolve(client, entra, exception_id, "accepted_with_comment")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "case_id": case_id,
        "outcome": "accepted_with_comment",
        "case_status": "received",
        "case_status_text": "Received",
        "others_open": 0,
    }
    row = resolved_row(owner, exception_id)
    assert row["outcome"] == "accepted_with_comment"
    assert row["resolution"] == COMMENT
    assert row["resolved_by"] == f"user:{BENJAMIN}"
    # The audit log has the outcome but never the comment (NF-03).
    trail = audit_details(owner, case_id)
    assert "exception_resolved" in trail and "accepted_with_comment" in trail
    assert "Mustermann" not in trail


def test_rejecting_closes_the_case_as_rejected(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    case_id, exception_id = case_in_exception(
        app, ExceptionReason.UNKNOWN_VENDOR, tag, before=CaseStatus.AWAITING_APPROVAL
    )

    response = resolve(client, entra, exception_id, "rejected", role=Role.ADMIN)

    assert response.status_code == 200, response.text
    assert response.json()["case_status"] == "closed"
    with app.connect() as db:
        case = get_case(db, case_id)
    assert (case.status, case.closed_reason) == (CaseStatus.CLOSED, "rejected")


def test_a_duplicate_can_be_confirmed_or_waved_through_with_a_reason(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    def flagged_copy(number: str) -> tuple[int, int]:
        """Two cases for the same invoice, opened as the sweep does: the second is flagged."""
        ids: list[int] = []
        for _ in range(2):
            with unit_of_work(app, INTAKE) as db:
                case_id = create_case(
                    db,
                    CaseKind.INCOMING_INVOICE,
                    source_email_id=800_000 + int(uuid.uuid4().int % 100_000),
                    document_number=number,
                    gross_cents=5000,
                    currency="EUR",
                    case_key=f"vat:DE123456789|{number}",
                )
                flag_if_duplicate(db, case_id)
                ids.append(case_id)
        with app.connect() as db:
            exception_id: int = db.execute(
                text("select id from exception_item where case_id = :c and resolved_at is null"),
                {"c": ids[1]},
            ).scalar_one()
        return ids[1], exception_id

    confirmed_case, confirmed = flagged_copy(f"A-{tag}")
    waved_case, waved = flagged_copy(f"B-{tag}")

    assert outcomes(choices(client, entra, confirmed)) == [
        "confirmed_duplicate",
        "not_a_duplicate",
        "rejected",
    ]
    assert resolve(client, entra, confirmed, "confirmed_duplicate").json()["case_status"] == (
        "closed"
    )
    assert resolve(client, entra, waved, "not_a_duplicate").json()["case_status"] == "received"
    with app.connect() as db:
        closed = get_case(db, confirmed_case)
        own_key: str = db.execute(
            text("select duplicate_key from invoice_case where id = :id"), {"id": waved_case}
        ).scalar_one()
    assert closed.closed_reason == "duplicate"
    # It carries on with its own key, so it is not flagged against the same invoice again.
    assert own_key.endswith(f"|case:{waved_case}")


def test_a_customer_question_ends_as_answered_and_an_outgoing_invoice_is_never_rejected(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(
        app, ExceptionReason.CUSTOMER_QUERY, tag, kind=CaseKind.OUTGOING_INVOICE
    )

    assert outcomes(choices(client, entra, exception_id)) == ["answered"]
    refused = resolve(client, entra, exception_id, "rejected")
    assert refused.status_code == 422
    assert "cannot end that way" in refused.json()["problem"]

    assert resolve(client, entra, exception_id, "answered").json()["case_status"] == "received"


def test_a_booked_case_cannot_be_closed_from_the_page(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(
        app, ExceptionReason.INVOICE_CHECK_FAILED, tag, before=CaseStatus.BOOKED
    )

    assert outcomes(choices(client, entra, exception_id)) == ["accepted_with_comment"]
    assert resolve(client, entra, exception_id, "rejected").status_code == 422


def test_an_iban_mismatch_can_only_be_rejected_here(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(
        app, ExceptionReason.IBAN_MISMATCH, tag, before=CaseStatus.APPROVED
    )

    offered = choices(client, entra, exception_id)
    assert outcomes(offered) == ["rejected"]
    assert offered["note"].startswith("Possible fraud.")
    assert "next sweep" in offered["note"]
    # Fixed in DATEV is ARGUS's own check against DATEV (#36), never a click.
    assert resolve(client, entra, exception_id, "fixed_in_datev").status_code == 422
    assert resolve(client, entra, exception_id, "accepted_with_comment").status_code == 422


def test_values_to_check_point_to_the_correction_form(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(app, ExceptionReason.VALUES_TO_CHECK, tag)

    offered = choices(client, entra, exception_id)
    assert outcomes(offered) == ["rejected"]
    assert "exception queue" in offered["note"]
    assert resolve(client, entra, exception_id, "data_corrected").status_code == 422


def test_with_two_open_exceptions_the_case_carries_on_only_after_both(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    case_id, first = case_in_exception(app, ExceptionReason.UNKNOWN_VENDOR, tag)
    with unit_of_work(app, INTAKE) as db:
        second = open_exception(
            db, ExceptionReason.VENDOR_WITHOUT_APPROVER, "No approver.", case_id=case_id
        )

    offered = choices(client, entra, first)
    # Closing waits until the other one is resolved, so the case never closes half way.
    assert outcomes(offered) == ["accepted_with_comment"]
    assert offered["others_open"] == 1
    assert "another open exception" in offered["note"]

    after_first = resolve(client, entra, first, "accepted_with_comment").json()
    assert (after_first["case_status"], after_first["others_open"]) == ("exception", 1)
    assert outcomes(choices(client, entra, second)) == ["accepted_with_comment", "rejected"]
    assert resolve(client, entra, second, "rejected").json()["case_status"] == "closed"


# --- Refusals ---


def test_a_resolved_exception_cannot_be_resolved_again(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(app, ExceptionReason.UNREADABLE_DOCUMENT, tag)
    assert resolve(client, entra, exception_id, "accepted_with_comment").status_code == 200

    again = resolve(client, entra, exception_id, "rejected")

    assert again.status_code == 409
    assert "a moment ago" in again.json()["problem"]
    assert (
        client.get(
            f"/exceptions/{exception_id}/choices", headers=as_role(entra, Role.FINANCE)
        ).status_code
        == 404
    )


def test_an_exception_without_a_case_is_not_resolved_here(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    with unit_of_work(app, Actor.system("audit")) as db:
        exception_id = open_exception(db, ExceptionReason.AUDIT_COPY_FAILED, "Copy failed.")

    assert (
        client.get(
            f"/exceptions/{exception_id}/choices", headers=as_role(entra, Role.FINANCE)
        ).status_code
        == 404
    )
    assert resolve(client, entra, exception_id, "retried").status_code == 404


@pytest.mark.parametrize("comment", ["", "  ", "ok", "x" * 501])
def test_the_comment_is_needed_and_short(
    app: Engine, owner: Engine, client: TestClient, entra: FakeEntra, tag: str, comment: str
) -> None:
    _, exception_id = case_in_exception(app, ExceptionReason.UNKNOWN_VENDOR, tag)

    response = resolve(client, entra, exception_id, "accepted_with_comment", comment)

    assert response.status_code == 422
    assert "3 to 500 characters" in response.json()["problem"]
    assert resolved_row(owner, exception_id)["outcome"] is None


def test_an_approver_or_someone_without_a_role_cannot_resolve(
    app: Engine, owner: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, exception_id = case_in_exception(app, ExceptionReason.UNKNOWN_VENDOR, tag)
    path = f"/exceptions/{exception_id}/resolution"
    body = {"outcome": "rejected", "comment": COMMENT}

    assert (
        client.post(path, json=body, headers=as_role(entra, Role.APPROVER, ALICE)).status_code
        == 403
    )
    assert client.post(path, json=body, headers=as_role(entra, None, ALICE)).status_code == 403
    assert client.post(path, json=body).status_code == 401
    assert (
        client.get(
            f"/exceptions/{exception_id}/choices", headers=as_role(entra, Role.APPROVER, ALICE)
        ).status_code
        == 403
    )
    assert resolved_row(owner, exception_id)["outcome"] is None


def test_the_log_names_ids_and_the_outcome_but_never_the_comment(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    captured: pytest.LogCaptureFixture,
    tag: str,
) -> None:
    case_id, exception_id = case_in_exception(app, ExceptionReason.UNKNOWN_VENDOR, tag)

    choices(client, entra, exception_id)
    resolve(client, entra, exception_id, "accepted_with_comment")

    logged = "\n".join(r.getMessage() for r in captured.records)
    assert (
        f"user {BENJAMIN} resolved exception {exception_id} of case {case_id}"
        " as accepted_with_comment" in logged
    )
    for personal in ("Mustermann", "Erika", "confirmed it"):
        assert personal not in logged


def test_an_iban_mismatch_that_cannot_be_rejected_here_says_why(
    app: Engine, client: TestClient, entra: FakeEntra, tag: str
) -> None:
    _, booked = case_in_exception(
        app, ExceptionReason.IBAN_MISMATCH, f"A-{tag}", before=CaseStatus.BOOKED
    )
    case_id, waiting = case_in_exception(app, ExceptionReason.IBAN_MISMATCH, f"B-{tag}")
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.UNKNOWN_VENDOR, "Not known.", case_id=case_id)

    for exception_id, why in ((booked, "still waiting before booking"), (waiting, "another")):
        offered = choices(client, entra, exception_id)
        assert offered["choices"] == []
        assert "can only reject" not in offered["note"]
        assert why in offered["note"]

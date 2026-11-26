"""The approval page and its database rules against a real PostgreSQL (#38).

Covers AP-02, AP-03, AP-04, AP-07, AP-08, AP-10, PR-04, NF-01, NF-03 and NF-04. The app reads
and writes with the rights of argus_app. Cases from other tests stay in the database, so each
test makes its own vendor account and finds its cases by ID. Every name, number, address and
amount below is made up.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from argus.app import create_app
from argus.approvers.table import ChangedBy, save_approver, set_absent, set_vendor_approver
from argus.auth import Role
from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
    Outcome,
)
from argus.cases.store import (
    CaseRuleBroken,
    change_case,
    create_case,
    get_case,
    move_case,
    open_exception,
    resolve_exception,
    set_actor,
    unit_of_work,
)
from argus.documents.records import link_document_to_case
from argus.documents.store import MemoryDocumentStore
from argus.intake.accounts import current_proposal
from standins.entra.app import FakeEntra
from tests.integration.approval_helpers import decide as helper_decide
from tests.integration.approval_helpers import send_for_approval
from tests.integration.test_queue_page import PDF, received_email

pytestmark = pytest.mark.req("AP-02", "AP-03", "AP-08", "NF-03")

ALICE = "a0380000-0000-4000-8000-000000000001"  # the vendor's approver, made up
DORA = "a0380000-0000-4000-8000-000000000002"  # her deputy, made up
OTTO = "a0380000-0000-4000-8000-000000000003"  # another vendor's approver, made up
BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
INTAKE = Actor.system("intake")
ADMIN = ChangedBy(Actor.user(ARITA, "Arita Test"), "Arita Test")
ALICE_ADDRESS = "alice.approver@maxasp-test.example"

_numbers = count(1)


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
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


@pytest.fixture
def vendor(app: Engine) -> str:
    """A vendor account of its own, approved by Alice with Dora as deputy."""
    account = str(70000 + uuid.uuid4().int % 9000)
    for oid, name in ((ALICE, "Alice Approver"), (DORA, "Dora Deputy"), (OTTO, "Otto Other")):
        save_approver(app, ADMIN, oid, name, f"{name.split()[0].lower()}@maxasp-test.example")
        set_absent(app, ADMIN, oid, False)
    set_vendor_approver(app, ADMIN, account, ALICE, DORA, False)
    return account


def as_person(
    entra: FakeEntra,
    who: str,
    role: Role = Role.APPROVER,
    name: str = "Alice Approver",
    address: str | None = ALICE_ADDRESS,
) -> dict[str, str]:
    token = entra.token(who, roles=[role], name=name, sign_in_address=address)
    return {"Authorization": f"Bearer {token}"}


def alice(entra: FakeEntra) -> dict[str, str]:
    return as_person(entra, ALICE)


def read_invoice(db: Connection, case_id: int, document_id: int | None, **fields: str) -> None:
    reading = {
        "fields": {
            name: {"value": value, "source": "page_text", "confidence": 0.95, "flag": None}
            for name, value in fields.items()
        }
    }
    db.execute(
        text(
            """
            insert into invoice_reading (case_id, document_id, document, role, reading)
            values (:case_id, :document_id, :document, 'invoice', cast(:reading as jsonb))
            """
        ),
        {
            "case_id": case_id,
            "document_id": document_id,
            "document": "body" if document_id is None else "attachment 1",
            "reading": json.dumps(reading),
        },
    )


def waiting_case(
    app: Engine,
    vendor: str,
    store: MemoryDocumentStore | None = None,
    *,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
    gross: int = 11900,
) -> int:
    """An incoming invoice of the vendor, read, with a proposal, sent for approval."""
    number = f"RE-38-{next(_numbers)}-{uuid.uuid4().hex[:6]}"
    email = received_email(app, store) if store is not None else None
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            kind,
            source_email_id=email.email_id if email else None,
            document_number=number,
            gross_cents=gross,
            currency="EUR",
            duplicate_key=f"dup-{number}",
        )
        change_case(db, case_id, partner_account=vendor)
        document = email.attachment_document_id if email else None
        if document is not None:
            link_document_to_case(db, case_id, document)
        read_invoice(
            db,
            case_id,
            document,
            issuer_name="Musterlieferant Test GmbH",
            invoice_date="2026-11-02",
            due_date="2026-12-02",
            net_amount="100.00",
            vat_amount="19.00",
        )
        assert send_for_approval(db, case_id)
    return case_id


def decide(
    client: TestClient,
    entra: FakeEntra,
    case_id: int,
    choice: str,
    comment: str | None = None,
    headers: dict[str, str] | None = None,
    request_id: int | None = None,
    proposal_id: int | None = None,
) -> Any:
    """Decide as the page does: with the request and the account proposal it shows now, unless
    the test names others."""
    if request_id is None:
        request_id = request_of(client, entra, case_id)
    if proposal_id is None:
        proposal_id = proposal_of(client, entra, case_id, headers)
    return client.post(
        f"/approval-requests/{case_id}/decision",
        json={
            "request_id": request_id,
            "proposal_id": proposal_id,
            "choice": choice,
            "comment": comment,
        },
        headers=headers or alice(entra),
    )


def proposal_of(
    client: TestClient, entra: FakeEntra, case_id: int, headers: dict[str, str] | None = None
) -> int | None:
    shown = client.get(f"/approval-requests/{case_id}", headers=headers or alice(entra)).json()
    proposal = shown.get("proposal")
    return int(proposal["proposal_id"]) if proposal else None


def request_of(client: TestClient, entra: FakeEntra, case_id: int) -> int:
    body = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    return int(body["request"]["request_id"])


def events(app: Engine, case_id: int, event_type: str) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text(
                "select actor, details from audit_event where case_id = :id"
                " and event_type = :t order by chain_seq"
            ),
            {"id": case_id, "t": event_type},
        ).mappings()
        return [{"actor": r["actor"], **r["details"]} for r in rows]


# --------------------------------------------------------------------------- the page


def test_the_page_is_a_fixed_file_for_every_case_and_opens_without_data(
    client: TestClient,
) -> None:
    for path in ("/approvals", "/approvals/123", "/approvals/app.js", "/approvals/app.css"):
        response = client.get(path)
        assert response.status_code == 200, path
    page = client.get("/approvals/123")
    assert "Content-Security-Policy" in page.headers
    assert "blob:" in page.headers["Content-Security-Policy"]
    assert client.get("/approvals/123").text == client.get("/approvals/456").text


@pytest.mark.req("AP-02", "AP-06")
def test_an_approver_sees_only_what_waits_for_them(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    mine = waiting_case(app, vendor)

    listed = client.get("/approval-requests", headers=alice(entra)).json()
    row = next(r for r in listed["approvals"] if r["case_id"] == mine)
    assert row["may_decide"] is True
    assert "recipient_id" not in row
    assert listed["sees_all"] is False

    # Otto approves another vendor's invoices: he never sees Alice's.
    otto = as_person(entra, OTTO, name="Otto Other", address="otto@maxasp-test.example")
    assert mine not in [
        r["case_id"] for r in client.get("/approval-requests", headers=otto).json()["approvals"]
    ]
    assert client.get(f"/approval-requests/{mine}", headers=otto).status_code == 403
    # Nor can he probe for case numbers: one that does not exist answers the same.
    assert client.get("/approval-requests/999999999", headers=otto).status_code == 403


def test_finance_and_admin_see_every_waiting_invoice_but_read_only(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    for who, role in ((BENJAMIN, Role.FINANCE), (ARITA, Role.ADMIN)):
        headers = as_person(entra, who, role, name="Someone Else", address=None)
        listed = client.get("/approval-requests", headers=headers).json()
        row = next(r for r in listed["approvals"] if r["case_id"] == case_id)
        assert row["may_decide"] is False
        assert listed["sees_all"] is True
        one = client.get(f"/approval-requests/{case_id}", headers=headers).json()
        assert one["may_decide"] is False
        assert one["waiting"] is True
    assert client.get("/approval-requests/999999999", headers=headers).status_code == 404


@pytest.mark.req("AP-02", "PR-04")
@pytest.mark.usefixtures("live")  # the email is sorted, which needs live mode (#72)
def test_the_page_shows_the_invoice_data_and_where_the_proposal_came_from(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str, store: MemoryDocumentStore
) -> None:
    case_id = waiting_case(app, vendor, store)

    body = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()

    assert body["may_decide"] is True
    assert body["status"] == "awaiting_approval"
    assert body["vendor_name"] == "Musterlieferant Test GmbH"
    assert body["vendor_account"] == vendor
    assert (body["invoice_date"], body["due_date"]) == ("2026-11-02", "2026-12-02")
    assert (body["net_cents"], body["vat_cents"], body["gross_cents"]) == (10000, 1900, 11900)
    assert body["currency"] == "EUR"
    assert body["request"]["recipient_name"] == "Alice Approver"
    assert body["request"]["recipient_role"] == "approver"
    assert "recipient_id" not in body["request"]
    proposal = body["proposal"]
    assert (proposal["gl_account"], proposal["tax_key"]) == ("4930", "9")
    assert proposal["origin"] == "from the booking of RE-HELPER on 15.01.2026"
    assert proposal["changed_at_approval"] is False
    assert body["decision"] is None
    assert body["file"]["content_type"] == "application/pdf"
    assert body["file"]["viewable"] is True


@pytest.mark.usefixtures("live")
def test_the_invoice_file_is_served_only_to_people_who_may_see_the_case(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str, store: MemoryDocumentStore
) -> None:
    case_id = waiting_case(app, vendor, store)
    body = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    document_id = body["file"]["document_id"]
    path = f"/approval-requests/{case_id}/files/{document_id}"

    response = client.get(path, headers=alice(entra))
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-security-policy"].startswith("sandbox")

    otto = as_person(entra, OTTO, name="Otto Other", address=None)
    assert client.get(path, headers=otto).status_code == 403
    # Only the invoice file of this case, not the raw email or another file.
    with app.connect() as db:
        raw: int = db.execute(
            text(
                "select id from stored_document where kind = 'email' and id < :id"
                " order by id desc limit 1"
            ),
            {"id": document_id},
        ).scalar_one()
    assert (
        client.get(f"/approval-requests/{case_id}/files/{raw}", headers=alice(entra)).status_code
        == 404
    )


@pytest.mark.req("PR-04")
def test_a_blank_tax_key_comes_as_blank_so_the_page_says_no_tax_key(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    changed = client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_of(client, entra, case_id),
            "gl_account": "4930",
            "tax_key": "",
            "reason": "Reverse charge, no input VAT here",
        },
        headers=alice(entra),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["proposal"]["tax_key"] == ""
    script = client.get("/approvals/app.js").text
    assert '"No tax key"' in script
    assert "No input VAT will be booked" in script


# ----------------------------------------------------------------------- the decision


@pytest.mark.req("AP-03", "AP-08", "NF-01")
def test_approve_moves_the_case_to_approved_and_logs_who_with_their_name(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)

    response = decide(client, entra, case_id, "approve")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["decision"]["decision"] == "approved"
    assert body["decision"]["decided_by_name"] == "Alice Approver"
    assert body["may_decide"] is False
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.APPROVED
        row = (
            db.execute(text("select * from approval_decision where case_id = :id"), {"id": case_id})
            .mappings()
            .one()
        )
    assert row["decided_by"] == f"user:{ALICE}"
    assert row["decided_by_address"] == ALICE_ADDRESS
    assert row["decided_as"] == "approver"
    decided = events(app, case_id, "approval_decided")
    assert len(decided) == 1
    assert decided[0]["actor"] == ALICE
    assert decided[0]["decision"] == "approved"
    assert decided[0]["name"] == "Alice Approver"
    assert decided[0]["sign_in_address"] == ALICE_ADDRESS


@pytest.mark.req("AP-03", "AP-07")
def test_reject_needs_a_real_comment_and_closes_the_case_as_rejected(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    for blank in (None, "", "  ", "\t\n", "​​​​", "ok"):
        response = decide(client, entra, case_id, "reject", blank)
        assert response.status_code == 422, repr(blank)
        assert "at least 3 characters" in response.json()["problem"]

    response = decide(client, entra, case_id, "reject", "  Not ordered by us  ")

    assert response.status_code == 200, response.text
    assert response.json()["decision"]["comment"] == "Not ordered by us"
    with app.connect() as db:
        case = get_case(db, case_id)
    assert (case.status, case.closed_reason) == (CaseStatus.CLOSED, ClosedReason.REJECTED)
    decided = events(app, case_id, "approval_decided")[0]
    assert decided["decision"] == "rejected"
    # The comment stays out of the audit log; its hash shows a later change (NF-03).
    assert "Not ordered" not in json.dumps(decided)
    assert len(decided["comment_sha256"]) == 64


@pytest.mark.req("AP-07")
def test_the_database_refuses_a_rejection_without_a_real_comment(app: Engine, vendor: str) -> None:
    case_id = waiting_case(app, vendor)
    for blank in (None, "   ", "　​﻿"):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, Actor.user(ALICE, "Alice")) as db:
            insert_decision(db, case_id, "rejected", blank)


def insert_decision(
    db: Connection, case_id: int, decision: str, comment: str | None, proposal_id: int | None = -1
) -> int:
    """Insert a decision on the newest request, naming the case's current proposal unless the
    test names another one (or None for none)."""
    request_id: int = db.execute(
        text("select max(id) from approval_request where case_id = :id"), {"id": case_id}
    ).scalar_one()
    if proposal_id == -1:
        current = current_proposal(db, case_id)
        proposal_id = current.id if current else None
    decision_id: int = db.execute(
        text(
            "insert into approval_decision (case_id, request_id, proposal_id, decision, comment)"
            " values (:c, :r, :p, :d, :m) returning id"
        ),
        {"c": case_id, "r": request_id, "p": proposal_id, "d": decision, "m": comment},
    ).scalar_one()
    return decision_id


@pytest.mark.req("AP-02", "AP-03")
def test_only_the_person_asked_can_decide_finance_and_admin_too_get_403(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    others = [
        as_person(entra, OTTO, name="Otto Other", address=None),
        as_person(entra, DORA, name="Dora Deputy", address=None),  # deputy, Alice is in
        as_person(entra, BENJAMIN, Role.FINANCE, name="Benjamin Test", address=None),
        as_person(entra, ARITA, Role.ADMIN, name="Arita Test", address=None),
    ]
    for headers in others:
        assert decide(client, entra, case_id, "approve", headers=headers).status_code == 403
        account = client.post(
            f"/approval-requests/{case_id}/account",
            json={"request_id": 1, "gl_account": "4930", "tax_key": "9", "reason": "test"},
            headers=headers,
        )
        assert account.status_code == 403
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL


@pytest.mark.req("AP-02", "AP-10")
def test_the_database_refuses_a_decision_by_anyone_but_the_person_asked(
    app: Engine, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    for actor in (Actor.user(OTTO, "Otto"), Actor.user(BENJAMIN, "Benjamin")):
        with (
            pytest.raises(CaseRuleBroken, match="only the person approval request"),
            unit_of_work(app, actor) as db,
        ):
            insert_decision(db, case_id, "approved", None)
    with pytest.raises(CaseRuleBroken, match="only a person"), unit_of_work(app, INTAKE) as db:
        insert_decision(db, case_id, "approved", None)


@pytest.mark.req("AP-10")
def test_nothing_reaches_approved_without_a_persons_decision(app: Engine, vendor: str) -> None:
    """AP-10: a recurring invoice (the rent, say) never reaches Approved on its own. A job is
    refused, and so is a person who did not decide on the page."""
    case_id = waiting_case(app, vendor)
    with (
        pytest.raises(CaseRuleBroken, match="only be approved by a person"),
        unit_of_work(app, INTAKE) as db,
    ):
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    with (
        pytest.raises(CaseRuleBroken, match="by the person asked"),
        unit_of_work(app, Actor.user(ALICE, "Alice")) as db,
    ):
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    # Nor may a job close it as rejected, or close it for another reason.
    with (
        pytest.raises(CaseRuleBroken, match="by the person asked"),
        unit_of_work(app, INTAKE) as db,
    ):
        move_case(
            db,
            case_id,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    with (
        pytest.raises(CaseRuleBroken, match="only be closed as rejected"),
        unit_of_work(app, Actor.user(ALICE, "Alice")) as db,
    ):
        move_case(
            db,
            case_id,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.WITHDRAWN,
        )


def test_a_decision_never_stands_without_its_move(app: Engine, vendor: str) -> None:
    case_id = waiting_case(app, vendor)
    with (
        pytest.raises(CaseRuleBroken, match="needs the case to move"),
        unit_of_work(app, Actor.user(ALICE, "Alice")) as db,
    ):
        insert_decision(db, case_id, "approved", None)
    with app.connect() as db:
        assert (
            db.execute(
                text("select count(*) from approval_decision where case_id = :id"), {"id": case_id}
            ).scalar_one()
            == 0
        )


def test_a_decision_needs_the_persons_name(app: Engine, vendor: str) -> None:
    case_id = waiting_case(app, vendor)
    with (
        pytest.raises(CaseRuleBroken, match="needs the name"),
        unit_of_work(app, Actor.user(ALICE)) as db,
    ):
        insert_decision(db, case_id, "approved", None)


@pytest.mark.req("PR-04", "AP-08")
def test_the_database_refuses_approval_without_the_proposal_the_person_saw(
    app: Engine, vendor: str
) -> None:
    """Arita on PR 190: a second check at the move to Approved, so no other path can skip it.
    Review of PR 205: the decision names the proposal it approves, and it must still be the
    current one, also when the account changes between the decision and the move. Since
    #39 the account cannot change at all once the request has a decision, which stops it even
    earlier; the check at the move stays behind it."""
    case_id = waiting_case(app, vendor)
    alice_actor = Actor.user(ALICE, "Alice Approver", ALICE_ADDRESS)
    with (
        pytest.raises(CaseRuleBroken, match="needs the G/L account and tax key proposal"),
        unit_of_work(app, alice_actor) as db,
    ):
        insert_decision(db, case_id, "approved", None, proposal_id=None)
    with unit_of_work(app, INTAKE) as db:
        shown = current_proposal(db, case_id)
    assert shown is not None
    with (
        pytest.raises(
            CaseRuleBroken,
            match="tax key proposal the person saw|decided on already; its account cannot change",
        ),
        unit_of_work(app, alice_actor) as db,
    ):
        insert_decision(db, case_id, "approved", None, proposal_id=shown.id)
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, proposed_by_name, reason)"
                " values (:c, :v, '4980', '', 'SKR03', 'person', 'Alice Approver', 'Tools')"
            ),
            {"c": case_id, "v": vendor},
        )
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL


@pytest.mark.req("AP-02", "AP-08")
def test_a_proposal_changed_since_the_page_showed_it_is_not_approved(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    """Compliance review of PR 205: the page is open, the account changes (a second tab, or a
    person in the exception queue), and the click on the old page must not approve the new
    account unseen. The database refuses it too."""
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    shown = proposal_of(client, entra, case_id)
    assert shown is not None
    changed = client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_id,
            "gl_account": "4980",
            "tax_key": "",
            "reason": "Changed in a second tab",
        },
        headers=alice(entra),
    )
    assert changed.status_code == 200, changed.text

    response = decide(client, entra, case_id, "approve", request_id=request_id, proposal_id=shown)

    assert response.status_code == 409
    assert "changed since you opened the invoice" in response.json()["problem"]
    now = response.json()["approval"]
    assert now["waiting"] is True
    assert now["proposal"]["gl_account"] == "4980"
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL
    with (
        pytest.raises(CaseRuleBroken, match="changed since the page showed it"),
        unit_of_work(app, Actor.user(ALICE, "Alice Approver")) as db,
    ):
        insert_decision(db, case_id, "approved", None, proposal_id=shown)
    # With the page showing the new account, the approval goes through and names it.
    assert decide(client, entra, case_id, "approve").status_code == 200
    with app.connect() as db:
        approved: int = db.execute(
            text("select proposal_id from approval_decision where case_id = :id"),
            {"id": case_id},
        ).scalar_one()
        current = current_proposal(db, case_id)
    assert current is not None and approved == current.id != shown
    assert events(app, case_id, "approval_decided")[0]["proposal_id"] == approved


@pytest.mark.req("AP-02", "AP-09")
def test_an_invoice_that_moved_to_another_vendor_is_not_decided_on_the_old_request(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    """Review of PR 205 (must fix): a job changes the vendor account while the case waits. The
    approver of the old vendor may neither change the account nor approve, on the page or in
    the database. Otto, the new vendor's approver, was never asked."""
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    other = str(int(vendor) + 1)
    set_vendor_approver(app, ADMIN, other, OTTO, None, False)
    with unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, partner_account=other)

    changed = client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_id,
            "gl_account": "4980",
            "tax_key": "9",
            "reason": "Belongs to the new vendor",
        },
        headers=alice(entra),
    )
    assert changed.status_code == 409
    assert "another vendor" in changed.json()["problem"]
    for choice, comment in (("approve", None), ("reject", "Not ours any more")):
        response = decide(client, entra, case_id, choice, comment, request_id=request_id)
        assert response.status_code == 409
        assert "another vendor" in response.json()["problem"]

    alice_actor = Actor.user(ALICE, "Alice Approver", ALICE_ADDRESS)
    with (
        pytest.raises(CaseRuleBroken, match="moved to another vendor account"),
        unit_of_work(app, alice_actor) as db,
    ):
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, proposed_by_name, reason)"
                " values (:c, :v, '4980', '9', 'SKR03', 'person', 'Alice Approver', 'New one')"
            ),
            {"c": case_id, "v": other},
        )
    with (
        pytest.raises(CaseRuleBroken, match="moved to another vendor account"),
        unit_of_work(app, alice_actor) as db,
    ):
        insert_decision(db, case_id, "approved", None, proposal_id=None)
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL
        assert (
            db.execute(
                text("select count(*) from approval_decision where case_id = :id"),
                {"id": case_id},
            ).scalar_one()
            == 0
        )


@pytest.mark.req("AP-07", "AP-10")
def test_a_job_cannot_close_a_waiting_invoice_through_the_exception_queue(
    app: Engine, vendor: str
) -> None:
    """Compliance review of PR 205: the detour Awaiting approval, Exception, Closed. A job may
    stop a waiting invoice, but only a person closes it from there."""
    case_id = waiting_case(app, vendor)
    with unit_of_work(app, INTAKE) as db:
        exception_id = open_exception(
            db, ExceptionReason.INVOICE_CHECK_FAILED, "Check it.", case_id=case_id
        )
    with (
        pytest.raises(CaseRuleBroken, match="waited for approval and only a person can close"),
        unit_of_work(app, INTAKE) as db,
    ):
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.EXCEPTION
    # A person in the exception queue still can, as before (E44, disputes).
    with unit_of_work(app, Actor.user(BENJAMIN, "Benjamin Test")) as db:
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            resolution="Disputed with the vendor",
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.CLOSED


@pytest.mark.req("NF-01")
def test_a_decision_is_kept_as_it_was_made(app: Engine, owner: Engine, vendor: str) -> None:
    case_id = waiting_case(app, vendor)
    with unit_of_work(app, INTAKE) as db:
        assert helper_decide(db, case_id)
    for sql in (
        "update approval_decision set comment = 'changed' where case_id = :id",
        "delete from approval_decision where case_id = :id",
    ):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, Actor.user(ALICE, "Alice")) as db:
            db.execute(text(sql), {"id": case_id})
        # Not even the owner can change it.
        with pytest.raises(DBAPIError, match="kept as it was made"), owner.begin() as db:
            set_actor(db, Actor.user(ARITA, "Arita"))
            db.execute(text(sql), {"id": case_id})


@pytest.mark.req("NF-04", "AP-07")
def test_a_rejected_invoice_is_never_booked_or_paid(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert (
        decide(client, entra, case_id, "reject", "Duplicate of a paper invoice").status_code == 200
    )

    for to in (CaseStatus.APPROVED, CaseStatus.BOOKED, CaseStatus.IN_PAYMENT_RUN, CaseStatus.PAID):
        with pytest.raises(CaseRuleBroken, match="closed"), unit_of_work(app, INTAKE) as db:
            move_case(
                db,
                case_id,
                expected=CaseStatus.CLOSED,
                to=to,
                datev_booking_ref="DATEV-38" if to is not CaseStatus.APPROVED else None,
            )


def test_deciding_twice_shows_what_was_decided_by_whom_and_when(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    assert decide(client, entra, case_id, "approve", request_id=request_id).status_code == 200

    # A second click, a second tab with reject, or an old link.
    for choice, comment in (("approve", None), ("reject", "Changed my mind")):
        again = decide(client, entra, case_id, choice, comment, request_id=request_id)
        assert again.status_code == 409
        stands = again.json()["approval"]
        assert stands["decision"]["decision"] == "approved"
        assert stands["decision"]["decided_by_name"] == "Alice Approver"
        assert stands["decision"]["decided_at"]
        assert stands["may_decide"] is False
    old_link = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    assert old_link["decision"]["decision"] == "approved"
    with app.connect() as db:
        assert (
            db.execute(
                text("select count(*) from approval_decision where case_id = :id"), {"id": case_id}
            ).scalar_one()
            == 1
        )


def test_a_stale_request_id_is_refused(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    response = decide(client, entra, case_id, "approve", request_id=request_id + 100000)
    assert response.status_code == 409
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL


@pytest.mark.req("AP-06")
def test_the_deputy_decides_while_the_approver_is_away(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    set_absent(app, ADMIN, ALICE, True)
    try:
        case_id = waiting_case(app, vendor)
    finally:
        set_absent(app, ADMIN, ALICE, False)
    dora = as_person(entra, DORA, name="Dora Deputy", address="dora@maxasp-test.example")
    # Alice is back, but the request went to Dora, so Dora decides.
    assert client.get(f"/approval-requests/{case_id}", headers=alice(entra)).status_code == 403
    body = client.get(f"/approval-requests/{case_id}", headers=dora).json()
    assert body["request"]["recipient_role"] == "deputy"
    assert body["request"]["on_behalf_of_name"] == "Alice Approver"

    response = decide(
        client, entra, case_id, "approve", headers=dora, request_id=body["request"]["request_id"]
    )

    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert (decision["decided_as"], decision["on_behalf_of_name"]) == ("deputy", "Alice Approver")
    assert events(app, case_id, "approval_decided")[0]["on_behalf_of_name"] == "Alice Approver"


@pytest.mark.req("AP-04")
def test_the_page_works_with_datev_and_wise_switched_off(
    app: Engine, entra: FakeEntra, vendor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ARGUS_DATEV_URL",
        "ARGUS_DATEV_CLIENT",
        "ARGUS_WISE_URL",
        "ARGUS_WISE_PROFILE_ID",
        "ARGUS_DEMO",
    ):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_app(lambda: app, sign_in=entra.verifier))
    case_id = waiting_case(app, vendor)

    assert decide(client, entra, case_id, "approve").status_code == 200


# --------------------------------------------------------------------- change account


@pytest.mark.req("PR-04", "AP-08")
def test_the_approver_changes_the_account_with_a_reason_and_approval_uses_it(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)

    response = client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_id,
            "gl_account": "4980",
            "tax_key": "9",
            "reason": "Tools, not office supplies",
        },
        headers=alice(entra),
    )

    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert (proposal["gl_account"], proposal["tax_key"]) == ("4980", "9")
    assert proposal["changed_at_approval"] is True
    assert proposal["origin"] == "changed by Alice Approver: Tools, not office supplies"
    proposed = events(app, case_id, "account_proposed")[-1]
    assert proposed["actor"] == ALICE
    assert proposed["at_approval"] is True
    assert proposed["name"] == "Alice Approver"
    assert proposed["reason"] == "Tools, not office supplies"
    with app.connect() as db:
        assert get_case(db, case_id).status is CaseStatus.AWAITING_APPROVAL

    assert decide(client, entra, case_id, "approve", request_id=request_id).status_code == 200
    with app.connect() as db:
        current = current_proposal(db, case_id)
    assert current is not None and current.gl_account == "4980"


def test_changing_the_account_follows_the_queue_form_rules(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    path = f"/approval-requests/{case_id}/account"
    for gl, key, reason in (
        ("4980", "9", ""),  # no reason
        ("4980", "9", " ​\t "),  # blank reason
        ("1200", "9", "Paid by bank"),  # a bank account, not an invoice account
        ("49", "9", "Too short"),
        ("4980", "12345", "Tax key too long"),
    ):
        response = client.post(
            path,
            json={"request_id": request_id, "gl_account": gl, "tax_key": key, "reason": reason},
            headers=alice(entra),
        )
        assert response.status_code == 422, (gl, key, reason)
    with app.connect() as db:
        current = current_proposal(db, case_id)
    assert current is not None and current.gl_account == "4930"


@pytest.mark.req("PR-04")
def test_the_database_lets_only_the_person_asked_change_the_account_while_it_waits(
    app: Engine, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)

    def person_proposal(db: Connection, reason: str = "A reason") -> None:
        db.execute(
            text(
                """
                insert into account_proposal (case_id, vendor_account, gl_account, tax_key, chart,
                                              source, reason, proposed_by_name)
                values (:id, :vendor, '4980', '9', 'SKR03', 'person', :reason, 'Someone')
                """
            ),
            {"id": case_id, "vendor": vendor, "reason": reason},
        )

    for actor in (Actor.user(OTTO, "Otto"), Actor.user(BENJAMIN, "Benjamin")):
        with (
            pytest.raises(CaseRuleBroken, match="only the person asked"),
            unit_of_work(app, actor) as db,
        ):
            person_proposal(db)
    with (
        pytest.raises(CaseRuleBroken, match="needs the name and a reason"),
        unit_of_work(app, Actor.user(ALICE, "Alice")) as db,
    ):
        person_proposal(db, reason="​ ​")
    with unit_of_work(app, Actor.user(ALICE, "Alice")) as db:
        person_proposal(db)
    # Once approved, nobody changes it any more.
    with unit_of_work(app, INTAKE) as db:
        assert helper_decide(db, case_id)
    with (
        pytest.raises(CaseRuleBroken, match="settled before approval"),
        unit_of_work(app, Actor.user(ALICE, "Alice")) as db,
    ):
        person_proposal(db)


# ------------------------------------------------------------- invoices page and trail


@pytest.mark.req("NF-01", "AP-08")
def test_the_trail_and_the_invoices_page_show_the_change_and_the_decision(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_id,
            "gl_account": "4980",
            "tax_key": "9",
            "reason": "Tools, not office supplies",
        },
        headers=alice(entra),
    )
    decide(client, entra, case_id, "reject", "Not ordered by us", request_id=request_id)
    benjamin = as_person(entra, BENJAMIN, Role.FINANCE, name="Benjamin Test", address=None)

    steps = client.get(f"/cases/{case_id}/trail", headers=benjamin).json()["steps"]
    changed = next(
        s for s in steps if s["event_type"] == "account_proposed" and s["actor"] == ALICE
    )
    assert (
        changed["what"] == "Changed by Alice Approver at approval: G/L account 4980 with tax key 9"
    )
    assert changed["comment"] == "Tools, not office supplies"
    decided = next(s for s in steps if s["event_type"] == "approval_decided")
    assert decided["what"] == "Alice Approver rejected the invoice"
    assert decided["comment"] == "Not ordered by us"
    assert decided["actor_name"] == "Alice Approver"
    closed = next(s for s in steps if s["to_status"] == "closed")
    assert closed["actor"] == ALICE

    found = client.post("/cases/search", json={"q": str(case_id)}, headers=benjamin).json()["cases"]
    row = next(c for c in found if c["case_id"] == case_id)
    assert (row["decision"], row["decided_by_name"]) == ("rejected", "Alice Approver")
    assert row["decided_at"]


def test_the_log_names_ids_but_never_a_name_comment_or_reason(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    vendor: str,
    captured: pytest.LogCaptureFixture,
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": request_id,
            "gl_account": "4980",
            "tax_key": "9",
            "reason": "Secret reason Zebra",
        },
        headers=alice(entra),
    )
    decide(client, entra, case_id, "reject", "Secret comment Giraffe", request_id=request_id)
    decide(client, entra, case_id, "reject", "Second comment Okapi", request_id=request_id)

    logged = captured.text
    assert str(case_id) in logged
    for secret in ("Zebra", "Giraffe", "Okapi", "Alice Approver", ALICE_ADDRESS, "Musterlieferant"):
        assert secret not in logged

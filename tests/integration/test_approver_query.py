"""Sending an invoice back with a question, and answering it, against a real PostgreSQL (#192).

Covers AP-03, AP-05, AP-08, EX-01, EX-02, NF-03 and NF-04. The app reads and writes with the
rights of argus_app. Each test makes its own vendor account and finds its cases by ID. Every
name, number, address and text below is made up.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from argus.app import create_app
from argus.approvers.table import save_approver, set_absent, set_vendor_approver
from argus.auth import Role
from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
)
from argus.cases.store import (
    CaseRuleBroken,
    change_case,
    get_case,
    move_case,
    open_exception,
    unit_of_work,
)
from argus.documents.store import MemoryDocumentStore
from argus.invoices_page.resolve import REJECT, choices_for
from standins.entra.app import FakeEntra
from tests.integration.test_approval_page import (
    ADMIN,
    ALICE,
    ALICE_ADDRESS,
    ARITA,
    BENJAMIN,
    DORA,
    INTAKE,
    OTTO,
    alice,
    as_person,
    decide,
    events,
    request_of,
    waiting_case,
)

pytestmark = pytest.mark.req("AP-03", "AP-05", "AP-08", "EX-01", "EX-02")

BENJAMIN_ADDRESS = "benjamin.finance@maxasp-test.example"
QUESTION = "Was this order placed by the Stuttgart team?"
ANSWER = "Yes, ordered by the Stuttgart team on 12 November."


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
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


@pytest.fixture
def vendor(app: Engine) -> str:
    """A vendor account of its own, approved by Alice with Dora as deputy."""
    account = str(82000 + uuid.uuid4().int % 8000)  # clear of the fixture accounts 700xx
    for oid, name in ((ALICE, "Alice Approver"), (DORA, "Dora Deputy"), (OTTO, "Otto Other")):
        save_approver(app, ADMIN, oid, name, f"{name.split()[0].lower()}@maxasp-test.example")
        set_absent(app, ADMIN, oid, False)
    set_vendor_approver(app, ADMIN, account, ALICE, DORA, False)
    return account


def benjamin(entra: FakeEntra) -> dict[str, str]:
    return as_person(entra, BENJAMIN, Role.FINANCE, name="Benjamin Test", address=BENJAMIN_ADDRESS)


def ask(
    client: TestClient,
    entra: FakeEntra,
    case_id: int,
    question: str = QUESTION,
    headers: dict[str, str] | None = None,
    request_id: int | None = None,
) -> Any:
    if request_id is None:
        request_id = request_of(client, entra, case_id)
    return client.post(
        f"/approval-requests/{case_id}/question",
        json={"request_id": request_id, "question": question},
        headers=headers or alice(entra),
    )


def open_query(app: Engine, case_id: int) -> int:
    with app.connect() as db:
        found: int = db.execute(
            text(
                "select id from exception_item where case_id = :id and reason = 'approver_query'"
                " and resolved_at is null"
            ),
            {"id": case_id},
        ).scalar_one()
    return found


def reply(
    client: TestClient,
    entra: FakeEntra,
    exception_id: int,
    choice: str = "answer",
    words: str = ANSWER,
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        f"/exceptions/{exception_id}/answer",
        json={"choice": choice, "text": words},
        headers=headers or benjamin(entra),
    )


def requests_of(app: Engine, case_id: int) -> list[Any]:
    with app.connect() as db:
        return list(
            db.execute(
                text(
                    "select id, recipient_id, recipient_name, recipient_role, on_behalf_of_name,"
                    " requested_at, requested_by, vendor_account from approval_request"
                    " where case_id = :id order by id"
                ),
                {"id": case_id},
            )
        )


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with app.connect() as db:
        return get_case(db, case_id).status


# ------------------------------------------------------------------------- the whole way


@pytest.mark.req("AP-03", "AP-05", "AP-08", "NF-03")
def test_ask_answer_and_approve(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    first = requests_of(app, case_id)[0]

    asked = ask(client, entra, case_id)
    assert asked.status_code == 200, asked.text
    page = asked.json()
    assert page["status"] == "exception"
    assert page["waiting_for_answer"] is True
    assert page["may_decide"] is False
    [query] = page["queries"]
    assert query["question"] == QUESTION
    assert query["asked_by_name"] == "Alice Approver"
    assert query["asked_as"] == "approver"
    assert query["outcome"] is None
    assert "asked_by" not in query  # object IDs are not for the page

    # Benjamin sees the question, who asked and when, in the exception queue.
    exception_id = open_query(app, case_id)
    listed = client.get("/exceptions", headers=benjamin(entra)).json()["exceptions"]
    assert any(
        r["exception_id"] == exception_id and r["reason"] == "approver_query" for r in listed
    )
    shown = client.get(f"/exceptions/{exception_id}", headers=benjamin(entra)).json()
    assert shown["approver_query"]["question"] == QUESTION
    assert shown["approver_query"]["asked_by_name"] == "Alice Approver"
    assert shown["approver_query"]["asked_at"]
    assert shown["approver_query"]["others_open"] == 0
    assert shown["fact_lines"] == []  # the request ID means nothing to Benjamin

    answered = reply(client, entra, exception_id)
    assert answered.status_code == 200, answered.text
    assert answered.json()["outcome"] == "answered"
    assert answered.json()["sent_to"] == "Alice Approver"
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL

    # A new request to the same person: a new clock for the reminders of #40 (AP-05).
    old, new = requests_of(app, case_id)
    assert old.id == first.id
    assert new.recipient_id == ALICE
    assert new.recipient_role == "approver"
    assert new.requested_at > old.requested_at
    assert new.requested_by == f"user:{BENJAMIN}"

    # The approval page shows the question and the answer, and Alice decides again.
    page = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    assert page["may_decide"] is True
    assert page["request"]["request_id"] == new.id
    [query] = page["queries"]
    assert query["outcome"] == "answered"
    assert query["answer"] == ANSWER
    assert query["answered_by_name"] == "Benjamin Test"
    approved = decide(client, entra, case_id, "approve")
    assert approved.status_code == 200, approved.text
    assert status_of(app, case_id) is CaseStatus.APPROVED

    # The audit log names who and when, with a hash of the texts but never the texts.
    [queried] = events(app, case_id, "approval_queried")
    assert queried["actor"] == ALICE
    assert queried["name"] == "Alice Approver"
    assert queried["sign_in_address"] == ALICE_ADDRESS
    assert queried["request_id"] == first.id
    assert len(queried["question_sha256"]) == 64
    [answer_event] = events(app, case_id, "approval_query_answered")
    assert answer_event["actor"] == BENJAMIN
    assert answer_event["name"] == "Benjamin Test"
    assert answer_event["sign_in_address"] == BENJAMIN_ADDRESS
    assert len(answer_event["answer_sha256"]) == 64
    with app.connect() as db:
        logged: list[str] = list(
            db.execute(
                text("select details::text from audit_event where case_id = :id"), {"id": case_id}
            ).scalars()
        )
        assert not any(QUESTION in d or ANSWER in d for d in logged)
        stored = db.execute(
            text(
                "select asked_by, asked_by_name, asked_by_address, asked_at from approval_query"
                " where case_id = :id"
            ),
            {"id": case_id},
        ).one()
        assert stored.asked_by == f"user:{ALICE}"
        assert stored.asked_by_address == ALICE_ADDRESS
        assert stored.asked_at is not None


@pytest.mark.req("AP-03", "AP-07")
def test_ask_then_benjamin_rejects_the_invoice(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)

    short = reply(client, entra, exception_id, "reject", "  no ")
    assert short.status_code == 422
    assert "at least 3 characters" in short.json()["problem"]

    rejected = reply(client, entra, exception_id, "reject", "Not ordered by Max ASP")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["outcome"] == "rejected"
    with app.connect() as db:
        case = get_case(db, case_id)
    assert case.status is CaseStatus.CLOSED
    assert case.closed_reason is ClosedReason.REJECTED
    assert len(requests_of(app, case_id)) == 1  # no new request
    page = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    assert page["queries"][0]["outcome"] == "rejected"
    assert page["may_decide"] is False


def test_the_invoices_page_offers_only_reject_for_an_approver_question() -> None:
    choices = choices_for(
        ExceptionReason.APPROVER_QUERY, CaseKind.INCOMING_INVOICE, CaseStatus.AWAITING_APPROVAL, 0
    )
    assert choices == (REJECT,)


# ---------------------------------------------------------------------------- who may ask


@pytest.mark.req("AP-03", "AP-02")
def test_only_the_person_asked_can_send_it_back(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    for headers in (
        as_person(entra, OTTO, name="Otto Other", address=None),
        as_person(entra, DORA, name="Dora Deputy", address=None),  # deputy, Alice is in
        benjamin(entra),
        as_person(entra, ARITA, Role.ADMIN, name="Arita Test", address=None),
    ):
        refused = ask(client, entra, case_id, headers=headers, request_id=request_id)
        assert refused.status_code == 403
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


def insert_query(db: Connection, case_id: int, question: str = QUESTION) -> int:
    """Open the exception and write the question on the newest request, as the page does."""
    request_id: int = db.execute(
        text("select max(id) from approval_request where case_id = :id"), {"id": case_id}
    ).scalar_one()
    exception_id: int = db.execute(
        text(
            "insert into exception_item (case_id, reason, detail)"
            " values (:id, 'approver_query', 'Question') returning id"
        ),
        {"id": case_id},
    ).scalar_one()
    db.execute(
        text(
            "insert into approval_query (case_id, request_id, exception_id, question)"
            " values (:c, :r, :e, :q)"
        ),
        {"c": case_id, "r": request_id, "e": exception_id, "q": question},
    )
    return exception_id


@pytest.mark.req("AP-03", "AP-02")
def test_the_database_refuses_a_question_from_anyone_but_the_person_asked(
    app: Engine, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    for actor in (Actor.user(OTTO, "Otto"), Actor.user(BENJAMIN, "Benjamin Test")):
        with (
            pytest.raises(CaseRuleBroken, match="only the person asked"),
            unit_of_work(app, actor) as db,
        ):
            insert_query(db, case_id)
    with pytest.raises(CaseRuleBroken, match="only the person"), unit_of_work(app, INTAKE) as db:
        insert_query(db, case_id)
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


def test_a_question_needs_three_real_characters_on_the_page_and_in_the_database(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    for blank in ("", "  ab \n", "​​​"):
        refused = ask(client, entra, case_id, question=blank)
        assert refused.status_code == 422, blank
        assert "at least 3 characters" in refused.json()["problem"]
    with (
        pytest.raises(CaseRuleBroken, match="at least 3 characters"),
        unit_of_work(app, Actor.user(ALICE, "Alice Approver")) as db,
    ):
        insert_query(db, case_id, question=" \t ab ")
        move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.EXCEPTION)
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


def test_a_question_never_stands_without_its_move_and_the_reason_never_without_a_question(
    app: Engine, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    alice_actor = Actor.user(ALICE, "Alice Approver")
    with (
        pytest.raises(
            CaseRuleBroken,
            match=r"needs the case to go to the exception queue|open exception but is in",
        ),
        unit_of_work(app, alice_actor) as db,
    ):
        insert_query(db, case_id)  # the case stays in Awaiting approval
    with (
        pytest.raises(CaseRuleBroken, match="needs the question"),
        unit_of_work(app, alice_actor) as db,
    ):
        open_exception(db, ExceptionReason.APPROVER_QUERY, "No question", case_id=case_id)
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


def test_a_question_needs_the_persons_name(app: Engine, vendor: str) -> None:
    case_id = waiting_case(app, vendor)
    with (
        pytest.raises(CaseRuleBroken, match="needs the name"),
        unit_of_work(app, Actor.user(ALICE)) as db,
    ):
        insert_query(db, case_id)


def test_a_question_is_kept_as_it_was_asked(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    for sql in (
        "update approval_query set question = 'Changed later' where case_id = :id",
        "delete from approval_query where case_id = :id",
    ):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
            db.execute(text(sql), {"id": case_id})


def test_asking_twice_or_on_an_old_request_shows_what_stands_now(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    request_id = request_of(client, entra, case_id)
    assert ask(client, entra, case_id, request_id=request_id).status_code == 200
    again = ask(client, entra, case_id, request_id=request_id)
    assert again.status_code == 409
    assert again.json()["approval"]["waiting_for_answer"] is True
    exception_id = open_query(app, case_id)
    assert reply(client, entra, exception_id).status_code == 200
    # The question was asked on the old request; the new one is the one to ask on.
    stale = ask(client, entra, case_id, request_id=request_id)
    assert stale.status_code == 409
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    # On the new request Alice may ask again.
    assert ask(client, entra, case_id).status_code == 200


# --------------------------------------------------------------------------- who answers


@pytest.mark.req("AP-03", "AP-10", "NF-04")
def test_a_job_cannot_answer_or_reject_an_approver_question(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    for outcome in ("answered", "rejected"):
        with (
            pytest.raises(CaseRuleBroken, match="only a person can answer or reject"),
            unit_of_work(app, INTAKE) as db,
        ):
            db.execute(
                text(
                    "update exception_item set outcome = :o, resolution = 'Done by a job'"
                    " where id = :id"
                ),
                {"o": outcome, "id": exception_id},
            )
    # A person cannot let it carry on any other way either.
    with (
        pytest.raises(CaseRuleBroken, match="only end as answered or rejected"),
        unit_of_work(app, Actor.user(BENJAMIN, "Benjamin Test")) as db,
    ):
        db.execute(
            text(
                "update exception_item set outcome = 'accepted_with_comment',"
                " resolution = 'Fine' where id = :id"
            ),
            {"id": exception_id},
        )
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


def test_an_answer_needs_three_real_characters(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    assert reply(client, entra, exception_id, words="  ok ").status_code == 422
    with (
        pytest.raises(CaseRuleBroken, match="at least 3 characters"),
        unit_of_work(app, Actor.user(BENJAMIN, "Benjamin Test")) as db,
    ):
        db.execute(
            text(
                "update exception_item set outcome = 'answered', resolution = ' ok ' where id = :id"
            ),
            {"id": exception_id},
        )
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


def test_the_database_wants_three_real_characters_to_reject_instead_of_answering(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    with (
        pytest.raises(CaseRuleBroken, match=r"rejecting approver question .* at least 3"),
        unit_of_work(app, Actor.user(BENJAMIN, "Benjamin Test")) as db,
    ):
        db.execute(
            text(
                "update exception_item set outcome = 'rejected', resolution = ' \u200bno '"
                " where id = :id"
            ),
            {"id": exception_id},
        )
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


def test_only_finance_and_admin_answer_in_the_queue(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    assert reply(client, entra, exception_id, headers=alice(entra)).status_code == 403
    admin = as_person(entra, ARITA, Role.ADMIN, name="Arita Test", address=None)
    answered = reply(client, entra, exception_id, headers=admin)
    assert answered.status_code == 200, answered.text
    assert reply(client, entra, exception_id).status_code == 404  # answered already


def test_the_case_goes_back_to_the_approver_only_with_a_new_request(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    with (
        pytest.raises(CaseRuleBroken, match="only with a new approval request"),
        unit_of_work(app, Actor.user(BENJAMIN, "Benjamin Test")) as db,
    ):
        db.execute(
            text("update exception_item set outcome = 'answered', resolution = :r where id = :id"),
            {"r": ANSWER, "id": exception_id},
        )
        move_case(db, case_id, expected=CaseStatus.EXCEPTION, to=CaseStatus.AWAITING_APPROVAL)
    # Nobody else writes that request: not a job, and not another person.
    for actor in (INTAKE, Actor.user(ARITA, "Arita Test")):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, actor) as db:
            db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


def test_another_open_exception_is_resolved_before_the_answer(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    exception_id = open_query(app, case_id)
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.INVOICE_CHECK_FAILED, "Check it.", case_id=case_id)
    shown = client.get(f"/exceptions/{exception_id}", headers=benjamin(entra)).json()
    assert shown["approver_query"]["others_open"] == 1
    refused = reply(client, entra, exception_id)
    assert refused.status_code == 409
    assert "another open exception" in refused.json()["problem"]
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


# --------------------------------------------------------------------- who gets it back


def test_the_answer_goes_to_the_deputy_when_the_person_who_asked_is_away(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    set_absent(app, ADMIN, ALICE, True)
    try:
        answered = reply(client, entra, open_query(app, case_id))
        assert answered.status_code == 200, answered.text
        assert answered.json()["sent_to"] == "Dora Deputy"
        new = requests_of(app, case_id)[-1]
        assert new.recipient_id == DORA
        assert new.recipient_role == "deputy"
        assert new.on_behalf_of_name == "Alice Approver"
    finally:
        set_absent(app, ADMIN, ALICE, False)


def test_a_deputy_who_asked_gets_it_back_even_when_the_approver_returned(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    set_absent(app, ADMIN, ALICE, True)
    try:
        case_id = waiting_case(app, vendor)
        dora = as_person(entra, DORA, name="Dora Deputy", address="dora@maxasp-test.example")
        request_id = requests_of(app, case_id)[-1].id
        assert ask(client, entra, case_id, headers=dora, request_id=request_id).status_code == 200
    finally:
        set_absent(app, ADMIN, ALICE, False)
    answered = reply(client, entra, open_query(app, case_id))
    assert answered.status_code == 200, answered.text
    new = requests_of(app, case_id)[-1]
    assert new.recipient_id == DORA
    assert new.recipient_role == "deputy"


def test_nobody_to_ask_refuses_the_answer_with_a_plain_sentence(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    set_absent(app, ADMIN, ALICE, True)
    set_absent(app, ADMIN, DORA, True)
    try:
        refused = reply(client, entra, open_query(app, case_id))
        assert refused.status_code == 409
        assert "away" in refused.json()["problem"]
        assert status_of(app, case_id) is CaseStatus.EXCEPTION
    finally:
        set_absent(app, ADMIN, ALICE, False)
        set_absent(app, ADMIN, DORA, False)


def test_after_a_vendor_change_the_new_vendors_approver_is_asked(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    other = str(90000 + uuid.uuid4().int % 9000)
    save_approver(app, ADMIN, OTTO, "Otto Other", "otto@maxasp-test.example")
    set_vendor_approver(app, ADMIN, other, OTTO, None, False)
    with unit_of_work(app, INTAKE) as db:
        change_case(db, case_id, partner_account=other)
    answered = reply(client, entra, open_query(app, case_id))
    assert answered.status_code == 200, answered.text
    new = requests_of(app, case_id)[-1]
    assert new.recipient_id == OTTO
    assert new.vendor_account == other

    # The new vendor has no account proposal yet. Otto enters one on the approval page and
    # approves (code review of PR 209): the case does not get stuck.
    otto = as_person(entra, OTTO, name="Otto Other", address="otto@maxasp-test.example")
    page = client.get(f"/approval-requests/{case_id}", headers=otto).json()
    assert page["may_decide"] is True
    assert page["proposal"] is None
    no_proposal = client.post(
        f"/approval-requests/{case_id}/decision",
        json={"request_id": new.id, "proposal_id": None, "choice": "approve", "comment": None},
        headers=otto,
    )
    assert no_proposal.status_code == 409
    entered = client.post(
        f"/approval-requests/{case_id}/account",
        json={
            "request_id": new.id,
            "gl_account": "4980",
            "tax_key": "9",
            "reason": "New vendor, booked like its other licences",
        },
        headers=otto,
    )
    assert entered.status_code == 200, entered.text
    proposal_id = entered.json()["proposal"]["proposal_id"]
    approved = client.post(
        f"/approval-requests/{case_id}/decision",
        json={
            "request_id": new.id,
            "proposal_id": proposal_id,
            "choice": "approve",
            "comment": None,
        },
        headers=otto,
    )
    assert approved.status_code == 200, approved.text
    assert status_of(app, case_id) is CaseStatus.APPROVED


# ------------------------------------------------------------------------------- the log


def test_the_log_names_ids_but_never_a_question_answer_or_name(
    app: Engine,
    client: TestClient,
    entra: FakeEntra,
    vendor: str,
    captured: pytest.LogCaptureFixture,
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id, question="Secret question Zebra").status_code == 200
    exception_id = open_query(app, case_id)
    assert reply(client, entra, exception_id, words="Secret answer Giraffe").status_code == 200
    logged = captured.text
    assert str(case_id) in logged
    for secret in ("Zebra", "Giraffe", "Alice Approver", "Benjamin Test", ALICE_ADDRESS):
        assert secret not in logged


# ------------------------------------------------------------- answering one's own question


@pytest.mark.req("AP-03", "AP-05", "AP-08")
def test_answering_ones_own_question_is_marked_and_keeps_the_reminder_clock(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    """Compliance review of PR 209: an approver who also has the finance role may answer their
    own question (Arita on #39, 23 Nov: allowed, but marked "same person"). It never restarts
    the 3 and 5 working day clock of #40, however often it is done."""
    case_id = waiting_case(app, vendor)
    first = requests_of(app, case_id)[0]
    alice_finance = as_person(entra, ALICE, Role.FINANCE)
    for _ in range(2):
        assert ask(client, entra, case_id, headers=alice_finance).status_code == 200
        answered = reply(client, entra, open_query(app, case_id), headers=alice_finance)
        assert answered.status_code == 200, answered.text
        assert answered.json()["sent_to"] == "Alice Approver"

    with app.connect() as db:
        rows = list(
            db.execute(
                text(
                    "select id, query_id, answered_by_asker, clock_from from approval_request"
                    " where case_id = :id order by id"
                ),
                {"id": case_id},
            )
        )
    assert [r.answered_by_asker for r in rows] == [False, True, True]
    assert rows[0].clock_from is None and rows[0].query_id is None
    assert all(r.query_id is not None for r in rows[1:])
    # Both later requests count from the first one (never sent here, so its request time).
    assert rows[1].clock_from == first.requested_at
    assert rows[2].clock_from == first.requested_at

    answers = events(app, case_id, "approval_query_answered")
    assert [a.get("same_person") for a in answers] == [True, True]
    page = client.get(f"/approval-requests/{case_id}", headers=alice(entra)).json()
    assert all(q["same_person"] for q in page["queries"])


def test_an_answer_by_someone_else_starts_a_new_reminder_clock(
    app: Engine, client: TestClient, entra: FakeEntra, vendor: str
) -> None:
    case_id = waiting_case(app, vendor)
    assert ask(client, entra, case_id).status_code == 200
    assert reply(client, entra, open_query(app, case_id)).status_code == 200
    with app.connect() as db:
        new = db.execute(
            text(
                "select answered_by_asker, clock_from, query_id from approval_request"
                " where case_id = :id order by id desc limit 1"
            ),
            {"id": case_id},
        ).one()
    assert new.answered_by_asker is False
    assert new.clock_from is None  # #40 counts from this request's own sent time
    assert new.query_id is not None
    [answer_event] = events(app, case_id, "approval_query_answered")
    assert "same_person" not in answer_event

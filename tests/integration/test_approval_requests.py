"""Send each checked invoice to its approver (#191, ADR 0010), against a real PostgreSQL with the
rights of argus_app and the fake Graph.

Covers AP-02 (the approver gets it), AP-06 (the deputy while the approver is away), PR-04 (no
approval without a proposal), PR-05 and VM-04 (no approval while an IBAN hold is open), NF-01
(every request is kept and in the audit log) and NF-03 (only the approval client sends it, and
only to the person on the request). Each test makes its own vendor accounts and people and limits
the pass to its own cases, because the database is shared. Names and addresses are made up.
"""

import itertools
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.approvers.table import ChangedBy, save_approver, set_absent, set_vendor_approver
from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason, ExceptionReason
from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    create_case,
    get_case,
    move_case,
    open_exception,
    record_event,
    unit_of_work,
)
from argus.intake.approvals import Approvals, request_approvals, stored_requests
from argus.intake.graph import (
    ApprovalMail,
    GraphApiError,
    GraphCall,
    GraphCallRefused,
    GraphClient,
)
from argus.invoices_page.trail import case_trail
from argus.invoices_page.views import case_row
from standins.graph.app import MAILBOX, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

pytestmark = pytest.mark.req("AP-02")

INTAKE = Actor.system("intake")
JOB = Actor.system("approval-request")
ADMIN = ChangedBy(Actor.user("44444444-4444-4444-8444-444444444444"), "Arita Test")
APP_URL = "https://argus.example"
DOMAIN = "maxasp-test.example"  # where person() puts every made up address

# Vendor accounts nobody else uses.
_numbers = itertools.count(850_000_000 + uuid.uuid4().int % 1_000_000 * 100)


@pytest.fixture
def fake() -> FastAPI:
    return create_app(token=TOKEN)


@pytest.fixture
def approvals(app: Engine, fake: FastAPI) -> Approvals:
    client = GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        calls=frozenset({GraphCall.SEND_APPROVAL_REQUEST}),
        approval_lookup=stored_requests(app, APP_URL),
        approval_domain=DOMAIN,
    )
    return Approvals(client, APP_URL)


def sent(fake: FastAPI) -> list[dict[str, Any]]:
    mails: list[dict[str, Any]] = fake.state.mailbox.notices
    return mails


def person(app: Engine, name: str) -> tuple[str, str]:
    object_id = str(uuid.uuid4())
    address = f"{name.split()[0].lower()}.{object_id[:8]}@maxasp-test.example"
    save_approver(app, ADMIN, object_id, name, address)
    return object_id, address


def vendor(app: Engine, *, deputy: bool = False) -> tuple[str, str, str | None]:
    """A vendor account with an approver, and a deputy if asked. Returns (account, approver,
    deputy)."""
    account = str(next(_numbers))
    approver, _ = person(app, "Alice Approver")
    deputy_id = person(app, "Dora Deputy")[0] if deputy else None
    set_vendor_approver(app, ADMIN, account, approver, deputy_id, False)
    return account, approver, deputy_id


def checked_case(
    app: Engine,
    account: str | None,
    *,
    proposal: bool = True,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
) -> int:
    """An incoming case as #35 and #37 leave it: matched, checked for duplicates, proposed."""
    number = f"AR-{uuid.uuid4().hex[:8]}"
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            kind,
            partner_account=account,
            document_number=number,
            gross_cents=123456,
            currency="EUR",
            duplicate_key=f"dup-{number}",
        )
    if proposal and account is not None:
        propose(app, case_id, account)
    return case_id


def propose(app: Engine, case_id: int, account: str) -> None:
    with unit_of_work(app, Actor.system("account-proposal")) as db:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, from_document_number, from_posting_date)"
                " values (:id, :account, '4930', '9', 'SKR03', 'booking_history', 'RE-1',"
                " date '2026-03-01')"
            ),
            {"id": case_id, "account": account},
        )


def requests_of(app: Engine, case_id: int) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text("select * from approval_request where case_id = :id order by id"),
            {"id": case_id},
        ).mappings()
        return [dict(r) for r in rows]


def events_of(app: Engine, case_id: int, *types: str) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text(
                "select event_type, actor_type, actor, details from audit_event"
                " where case_id = :id and event_type = any(:types) order by chain_seq"
            ),
            {"id": case_id, "types": list(types)},
        ).mappings()
        return [dict(r) for r in rows]


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with app.connect() as db:
        return get_case(db, case_id).status


def open_reasons(app: Engine, case_id: int) -> list[str]:
    with app.connect() as db:
        return list(
            db.execute(
                text(
                    "select reason::text from exception_item"
                    " where case_id = :id and resolved_at is null order by id"
                ),
                {"id": case_id},
            ).scalars()
        )


# The step and the email


@pytest.mark.usefixtures("live")
def test_a_fully_checked_case_is_sent_to_its_approver_once(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, _ = vendor(app)
    case_id = checked_case(app, account)

    first = request_approvals(app, approvals, cases=[case_id])
    second = request_approvals(app, approvals, cases=[case_id])

    assert (first.requested, first.sent, first.failed) == (1, 1, 0)
    assert (second.requested, second.sent, second.failed) == (0, 0, 0)
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    (request,) = requests_of(app, case_id)
    assert request["recipient_id"] == approver
    assert request["recipient_role"] == "approver"
    assert request["on_behalf_of"] is None
    assert request["sent_at"] is not None
    assert request["requested_by"] == "system:approval-request"
    (mail,) = sent(fake)
    assert mail["toRecipients"] == [{"emailAddress": {"address": request["recipient_address"]}}]
    text_body = mail["body"]["content"]
    assert f"Vendor: vendor account {account}" in text_body
    assert "Gross amount: 1,234.56 EUR" in text_body
    assert f"{APP_URL}/approvals/{case_id}" in text_body
    assert "attachments" not in mail and "ccRecipients" not in mail
    requested, mailed = events_of(app, case_id, "approval_requested", "approval_request_sent")
    assert requested["details"]["recipient"] == approver
    assert requested["details"]["as"] == "approver"
    assert (requested["actor_type"], requested["actor"]) == ("system", "approval-request")
    assert mailed["details"]["request_id"] == request["id"]


@pytest.mark.req("PR-04")
@pytest.mark.usefixtures("live")
def test_a_case_without_a_proposal_stays_in_received(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account, proposal=False)

    result = request_approvals(app, approvals, cases=[case_id])

    assert result.requested == 0
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    assert requests_of(app, case_id) == [] and sent(fake) == []


@pytest.mark.req("PR-05")
@pytest.mark.usefixtures("live")
def test_a_case_with_an_open_exception_stays_where_it_is(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.IBAN_MISMATCH, "Possible fraud.", case_id=case_id)

    request_approvals(app, approvals, cases=[case_id])

    assert status_of(app, case_id) is CaseStatus.EXCEPTION
    assert requests_of(app, case_id) == [] and sent(fake) == []


@pytest.mark.req("VM-02")
@pytest.mark.usefixtures("live")
def test_a_case_without_an_approver_stays_in_received(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account = str(next(_numbers))  # no row in the approver table
    case_id = checked_case(app, account)
    unmatched = checked_case(app, None)

    result = request_approvals(app, approvals, cases=[case_id, unmatched])

    assert (result.requested, result.failed) == (0, 0)
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    assert status_of(app, unmatched) is CaseStatus.RECEIVED
    assert sent(fake) == []


@pytest.mark.req("AP-06")
@pytest.mark.usefixtures("live")
def test_while_the_approver_is_away_the_deputy_gets_it(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, deputy = vendor(app, deputy=True)
    set_absent(app, ADMIN, approver, True)
    case_id = checked_case(app, account)

    request_approvals(app, approvals, cases=[case_id])

    (request,) = requests_of(app, case_id)
    assert (request["recipient_id"], request["recipient_role"]) == (deputy, "deputy")
    assert (request["on_behalf_of"], request["on_behalf_of_name"]) == (approver, "Alice Approver")
    (mail,) = sent(fake)
    assert mail["toRecipients"][0]["emailAddress"]["address"] == request["recipient_address"]
    assert "deputy of Alice Approver" in mail["body"]["content"]
    (requested,) = events_of(app, case_id, "approval_requested")
    assert requested["details"]["as"] == "deputy"
    assert requested["details"]["on_behalf_of"] == approver


@pytest.mark.req("AP-06")
@pytest.mark.usefixtures("live")
def test_a_deputy_who_is_away_too_counts_as_no_deputy(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, deputy = vendor(app, deputy=True)
    assert deputy is not None
    set_absent(app, ADMIN, approver, True)
    set_absent(app, ADMIN, deputy, True)
    case_id = checked_case(app, account)

    first = request_approvals(app, approvals, cases=[case_id])

    assert (first.absent, first.requested, first.sent) == (1, 0, 0)
    assert open_reasons(app, case_id) == ["approver_absent"]
    assert requests_of(app, case_id) == [] and sent(fake) == []
    # The database refuses a request for another case of the vendor while both are away.
    other = checked_case(app, account)
    with pytest.raises(CaseRuleBroken, match="no deputy who is in"), unit_of_work(app, JOB) as db:
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": other})

    set_absent(app, ADMIN, deputy, False)
    back = request_approvals(app, approvals, cases=[case_id])

    assert (back.resumed, back.requested, back.sent) == (1, 1, 1)
    (request,) = requests_of(app, case_id)
    assert (request["recipient_id"], request["recipient_role"]) == (deputy, "deputy")


@pytest.mark.req("AP-06")
@pytest.mark.usefixtures("live")
def test_an_absent_approver_without_a_deputy_gets_nothing_until_back(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, approver, _ = vendor(app)
    set_absent(app, ADMIN, approver, True)
    case_id = checked_case(app, account)

    first = request_approvals(app, approvals, cases=[case_id])

    assert (first.absent, first.requested, first.sent) == (1, 0, 0)
    assert open_reasons(app, case_id) == ["approver_absent"]
    assert requests_of(app, case_id) == [] and sent(fake) == []
    # Nothing changes on the next pass while the approver is still away.
    again = request_approvals(app, approvals, cases=[case_id])
    assert (again.absent, again.resumed) == (0, 0)
    assert open_reasons(app, case_id) == ["approver_absent"]

    set_absent(app, ADMIN, approver, False)
    back = request_approvals(app, approvals, cases=[case_id])

    assert (back.resumed, back.requested, back.sent) == (1, 1, 1)
    assert open_reasons(app, case_id) == []
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    assert requests_of(app, case_id)[0]["recipient_id"] == approver


@pytest.mark.req("AP-06")
def test_an_absent_approver_exception_ends_as_retried_only_once_someone_can_approve(
    app: Engine,
) -> None:
    account, approver, _ = vendor(app)
    set_absent(app, ADMIN, approver, True)
    case_id = checked_case(app, account)
    with unit_of_work(app, JOB) as db:
        exception_id = open_exception(db, ExceptionReason.APPROVER_ABSENT, "Away.", case_id=case_id)

    with (
        pytest.raises(CaseRuleBroken, match="once someone can approve"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(
            text("update exception_item set outcome = 'retried' where id = :id"),
            {"id": exception_id},
        )
    with (
        pytest.raises(CaseRuleBroken, match="retried or rejected"),
        unit_of_work(app, Actor.user("55555555-5555-4555-8555-555555555555")) as db,
    ):
        db.execute(
            text("update exception_item set outcome = 'accepted_with_comment' where id = :id"),
            {"id": exception_id},
        )
    with (
        pytest.raises(CaseRuleBroken, match="always belongs to a case"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(
            text(
                "insert into exception_item (email_id, reason, detail)"
                " values (1, 'approver_absent', 'x')"
            )
        )


@pytest.mark.req("IN-04", "IN-06")
@pytest.mark.usefixtures("shadow")
def test_shadow_mode_moves_and_sends_nothing(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)

    result = request_approvals(app, approvals, cases=[case_id])

    assert (result.requested, result.sent) == (0, 0)
    assert status_of(app, case_id) is CaseStatus.RECEIVED
    assert requests_of(app, case_id) == [] and sent(fake) == []


class Failing:
    """The approval client, failing the first ``times`` sends as Graph would when down."""

    def __init__(self, inner: Approvals, times: int) -> None:
        self.inner = inner.mailbox
        self.times = times
        self.mails: list[ApprovalMail] = []

    def send_approval_request(self, mail: ApprovalMail) -> None:
        self.mails.append(mail)
        if self.times > 0:
            self.times -= 1
            raise GraphApiError("POST", "sendMail", 503, "ServiceUnavailable")
        self.inner.send_approval_request(mail)


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("live")
def test_a_failed_send_is_tried_again_and_a_sent_request_never_again(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    failing = Failing(approvals, times=1)
    flaky = Approvals(failing, APP_URL)

    first = request_approvals(app, flaky, cases=[case_id])

    assert (first.requested, first.sent, first.failed) == (1, 0, 1)
    (request,) = requests_of(app, case_id)
    assert request["sent_at"] is None and first.not_sent == [request["id"]]
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    assert events_of(app, case_id, "approval_request_sent") == []

    second = request_approvals(app, flaky, cases=[case_id])
    third = request_approvals(app, flaky, cases=[case_id])

    assert (second.sent, third.sent) == (1, 0)
    assert len(failing.mails) == 2 and len(sent(fake)) == 1
    assert len(events_of(app, case_id, "approval_request_sent")) == 1
    with pytest.raises(CaseRuleBroken, match="never sent again"), unit_of_work(app, JOB) as db:
        db.execute(
            text("update approval_request set sent_at = now() where id = :id"),
            {"id": request["id"]},
        )


# The database rules


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("live")
def test_the_client_sends_only_what_the_database_holds_for_an_unsent_request(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    failing = Failing(approvals, times=1)
    request_approvals(app, Approvals(failing, APP_URL), cases=[case_id])
    (mail,) = failing.mails
    client = approvals.mailbox

    # A made up request, or a real one with another recipient or another link, never goes out.
    for wrong, words in (
        (replace(mail, request_id=mail.request_id + 10_000_000), "no unsent stored request"),
        (replace(mail, recipient_address="mallory@evil.example"), "not the person"),
        (replace(mail, recipient_name="Mallory"), "more or other text"),
        (replace(mail, link="https://evil.example/approvals/1"), "more or other text"),
    ):
        with pytest.raises(GraphCallRefused, match=words):
            client.send_approval_request(wrong)
    assert sent(fake) == []

    request_approvals(app, approvals, cases=[case_id])
    assert len(sent(fake)) == 1
    # Once it is marked sent, the same request cannot go out again.
    with pytest.raises(GraphCallRefused, match="no unsent stored request"):
        client.send_approval_request(mail)
    assert len(sent(fake)) == 1


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("live")
def test_an_approver_address_outside_max_asp_gets_no_email(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account = str(next(_numbers))
    object_id = str(uuid.uuid4())
    save_approver(app, ADMIN, object_id, "Otto Outside", f"otto.{object_id[:8]}@outside.example")
    set_vendor_approver(app, ADMIN, account, object_id, None, False)
    case_id = checked_case(app, account)

    result = request_approvals(app, approvals, cases=[case_id])

    # The request is kept, so the trail shows who the table named, but nothing goes out and
    # it stays unsent until someone fixes the address.
    assert (result.requested, result.sent, result.failed) == (1, 0, 1)
    assert requests_of(app, case_id)[0]["sent_at"] is None
    assert sent(fake) == []
    # Rejected, so the unsent request does not wait for other tests' sweeps.
    with unit_of_work(app, INTAKE) as db:
        move_case(
            db,
            case_id,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("live")
def test_the_client_sends_nothing_for_a_case_that_no_longer_waits_for_approval(
    app: Engine, approvals: Approvals, fake: FastAPI
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    failing = Failing(approvals, times=1)
    request_approvals(app, Approvals(failing, APP_URL), cases=[case_id])
    (mail,) = failing.mails
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.IBAN_MISMATCH, "Held.", case_id=case_id)

    with pytest.raises(GraphCallRefused, match="no unsent stored request"):
        approvals.mailbox.send_approval_request(mail)
    assert sent(fake) == []


@pytest.mark.req("PR-04")
def test_the_database_refuses_approval_without_a_proposal(app: Engine) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account, proposal=False)

    with (
        pytest.raises(CaseRuleBroken, match="cannot go for approval without an account proposal"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
    assert status_of(app, case_id) is CaseStatus.RECEIVED


@pytest.mark.req("PR-04")
def test_a_proposal_for_another_vendor_account_does_not_count(app: Engine) -> None:
    account, _, _ = vendor(app)
    other, _, _ = vendor(app)
    case_id = checked_case(app, other)
    with unit_of_work(app, INTAKE) as db:
        db.execute(
            text("update invoice_case set partner_account = :a where id = :id"),
            {"a": account, "id": case_id},
        )

    with (
        pytest.raises(CaseRuleBroken, match="without an account proposal"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)


def test_the_database_refuses_approval_without_a_request(app: Engine) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)

    with (
        pytest.raises(CaseRuleBroken, match="without an approval request"),
        unit_of_work(app, JOB) as db,
    ):
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
    # A request saved without the move is refused when the transaction commits, so no request
    # can wait to be emailed for a case that never went for approval.
    with (
        pytest.raises(CaseRuleBroken, match="go for approval in the same transaction"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
    assert requests_of(app, case_id) == []
    assert status_of(app, case_id) is CaseStatus.RECEIVED


@pytest.mark.req("PR-05", "VM-04")
@pytest.mark.parametrize(
    "reason", [ExceptionReason.IBAN_MISMATCH, ExceptionReason.NO_ACCOUNT_PROPOSAL]
)
def test_the_database_refuses_approval_while_an_exception_is_open(
    app: Engine, reason: ExceptionReason
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, reason, "Held.", case_id=case_id)

    with (
        pytest.raises(CaseRuleBroken, match="has an open exception"),
        unit_of_work(app, JOB) as db,
    ):
        db.execute(
            text("update invoice_case set status = 'awaiting_approval' where id = :id"),
            {"id": case_id},
        )
    assert status_of(app, case_id) is CaseStatus.EXCEPTION


@pytest.mark.req("NF-01")
def test_only_argus_writes_a_request_and_the_database_picks_the_person(app: Engine) -> None:
    account, approver, _ = vendor(app)
    case_id = checked_case(app, account)
    someone = Actor.user("55555555-5555-4555-8555-555555555555")

    with pytest.raises(CaseRuleBroken, match="only ARGUS"), unit_of_work(app, someone) as db:
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
    with pytest.raises(NotAllowed), unit_of_work(app, JOB) as db:
        db.execute(
            text(
                "insert into approval_request (case_id, recipient_id, recipient_name,"
                " recipient_address, recipient_role) values (:id, 'x', 'Mallory',"
                " 'mallory@evil.example', 'approver')"
            ),
            {"id": case_id},
        )
    with unit_of_work(app, JOB) as db:
        db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
    (request,) = requests_of(app, case_id)
    assert request["recipient_id"] == approver
    with pytest.raises(NotAllowed), unit_of_work(app, JOB) as db:
        db.execute(text("update approval_request set recipient_address = 'mallory@evil.example'"))
    with pytest.raises(NotAllowed), unit_of_work(app, JOB) as db:
        db.execute(text("delete from approval_request where case_id = :id"), {"id": case_id})
    with pytest.raises(CaseRuleBroken, match="only ARGUS marks"), unit_of_work(app, someone) as db:
        db.execute(
            text("update approval_request set sent_at = now() where case_id = :id"),
            {"id": case_id},
        )


def test_no_request_for_a_case_that_is_not_waiting_in_received(app: Engine) -> None:
    account, _, _ = vendor(app)
    outgoing = checked_case(app, account, proposal=False, kind=CaseKind.OUTGOING_INVOICE)
    away_account, away, _ = vendor(app)
    set_absent(app, ADMIN, away, True)
    away_case = checked_case(app, away_account)
    no_approver = checked_case(app, str(next(_numbers)))

    for case_id, words in (
        (outgoing, "not an incoming invoice"),
        (away_case, "away and it has no deputy"),
        (no_approver, "has no approver"),
    ):
        with pytest.raises(CaseRuleBroken, match=words), unit_of_work(app, JOB) as db:
            db.execute(text("insert into approval_request (case_id) values (:id)"), {"id": case_id})


@pytest.mark.req("NF-01")
def test_the_app_cannot_write_the_approval_events_itself(app: Engine) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    for event in ("approval_requested", "approval_request_sent"):
        with (
            pytest.raises(CaseRuleBroken, match="written by the database"),
            unit_of_work(app, JOB) as db,
        ):
            record_event(db, event, case_id=case_id, details={"name": "Mallory"})


@pytest.mark.req("PR-04")
def test_a_person_s_account_wins_over_the_booking_history(app: Engine) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account, proposal=False)
    with unit_of_work(app, Actor.user("33333333-3333-4333-8333-333333333333")) as db:
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, reason, proposed_by_name) values (:id, :account, '4980', '9',"
                " 'SKR03', 'person', 'Office supplies, checked with Benjamin', 'Benjamin Test')"
            ),
            {"id": case_id, "account": account},
        )

    with pytest.raises(CaseRuleBroken, match="does not replace it"):
        propose(app, case_id, account)


# Where a person sees it


@pytest.mark.usefixtures("live")
def test_the_invoices_page_and_the_trail_say_who_it_was_sent_to(
    app: Engine, approvals: Approvals
) -> None:
    account, _, _ = vendor(app)
    case_id = checked_case(app, account)
    failing = Failing(approvals, times=1)

    request_approvals(app, Approvals(failing, APP_URL), cases=[case_id])
    with app.connect() as db:
        waiting = case_row(db, case_id)
    request_approvals(app, approvals, cases=[case_id])
    with app.connect() as db:
        row = case_row(db, case_id)
        trail = case_trail(db, case_id)

    assert waiting is not None and row is not None and trail is not None
    assert (waiting.approval_to, waiting.approval_sent_at) == ("Alice Approver", None)
    assert row.approval_to == "Alice Approver" and row.approval_sent_at is not None
    whats = [step.what for step in trail.steps]
    assert "ARGUS sent the case for approval to Alice Approver" in whats
    assert "Sent to Alice Approver for approval" in whats

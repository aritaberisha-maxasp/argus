"""Benjamin's second approval above EUR 5,000 (#39, AP-09), against a real PostgreSQL with the
rights of argus_app, the fake Graph and a made up Wise rate.

Covers AP-09 (two approvals above EUR 5,000, by two people except Arita's exception), AP-08
(every step in the audit log, "same person" marked), AP-10 (only a person approves; ARGUS only
completes an approval at or below the line after the Wise rate) and NF-04 (nothing here books
or pays). The second approver is one setting for the whole database, so each test sets its own.
Each test makes its own vendor account and limits the pass to its own cases. Names, numbers,
addresses, amounts and rates are made up.
"""

import itertools
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app as create_argus
from argus.approvals_page.decide import Choice, decide
from argus.approvals_page.views import approval_view
from argus.approvers.table import (
    ChangedBy,
    remove_second_approver,
    save_approver,
    second_approver,
    set_absent,
    set_second_approver,
    set_vendor_approver,
)
from argus.auth import Role
from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    correct_case,
    create_case,
    get_case,
    move_case,
    unit_of_work,
)
from argus.intake.approvals import Approvals, request_approvals, stored_requests, wise_time
from argus.intake.graph import GraphCall, GraphClient
from argus.invoices_page.resolve import resolve_from_page
from argus.invoices_page.trail import case_trail
from standins.entra.app import FakeEntra
from standins.graph.app import MAILBOX, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

pytestmark = pytest.mark.req("AP-09")

INTAKE = Actor.system("intake")
JOB = Actor.system("approval-request")
ARITA = "44444444-4444-4444-8444-444444444444"
ADMIN = ChangedBy(Actor.user(ARITA, "Arita Test"), "Arita Test")
APP_URL = "https://argus.example"
DOMAIN = "maxasp-test.example"

_numbers = itertools.count(860_000_000 + uuid.uuid4().int % 1_000_000 * 100)


class Rates:
    """A made up Wise: EUR for one unit of each currency, at the time asked (as Wise answers
    with the rate of that minute), or a failure. ``time`` and ``pair`` make it answer wrong."""

    def __init__(
        self,
        per_unit: Mapping[str, str],
        *,
        fail: bool = False,
        time: str | None = None,
        pair: tuple[str, str] | None = None,
    ) -> None:
        self.per_unit = dict(per_unit)
        self.fail = fail
        self.time = time
        self.pair = pair
        self.asked: list[tuple[str, str, str | None]] = []

    def get_rate(self, source: str, target: str, at: str | None = None) -> Mapping[str, Any]:
        self.asked.append((source, target, at))
        if self.fail:
            raise RuntimeError("Wise is down")
        answer_source, answer_target = self.pair or (source, target)
        return {
            "rate": float(self.per_unit[source]),
            "source": answer_source,
            "target": answer_target,
            "time": self.time or f"{at}+0000",
        }


@pytest.fixture
def fake() -> FastAPI:
    return create_app(token=TOKEN)


def approvals_with(app: Engine, fake: FastAPI, rates: Rates | None = None) -> Approvals:
    client = GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake),
        calls=frozenset({GraphCall.SEND_APPROVAL_REQUEST}),
        approval_lookup=stored_requests(app, APP_URL),
        approval_domain=DOMAIN,
    )
    return Approvals(client, APP_URL, DOMAIN, rates)


def person(app: Engine, name: str) -> tuple[str, str]:
    object_id = str(uuid.uuid4())
    address = f"{name.split()[0].lower()}.{object_id[:8]}@{DOMAIN}"
    save_approver(app, ADMIN, object_id, name, address)
    return object_id, address


def actor_of(app: Engine, object_id: str) -> Actor:
    with app.connect() as db:
        row = db.execute(
            text("select name, email from approver where object_id = :id"), {"id": object_id}
        ).one()
    return Actor.user(object_id, row.name, row.email)


@pytest.fixture
def benjamin(app: Engine) -> str:
    """A made up Benjamin, set as the second approver for this test."""
    object_id, _ = person(app, "Benjamin Second")
    set_second_approver(app, ADMIN, object_id)
    return object_id


def vendor(app: Engine, approver: str | None = None) -> tuple[str, str]:
    account = str(next(_numbers))
    approver = approver or person(app, "Alice Approver")[0]
    set_vendor_approver(app, ADMIN, account, approver, None, False)
    return account, approver


def waiting_case(
    app: Engine,
    approvals: Approvals,
    account: str,
    cents: int,
    currency: str = "EUR",
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
) -> int:
    """A checked case, sent to its approver by the pass."""
    number = f"SA-{uuid.uuid4().hex[:8]}"
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db,
            kind,
            partner_account=account,
            document_number=number,
            gross_cents=cents,
            currency=currency,
            duplicate_key=f"dup-{number}",
        )
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
    request_approvals(app, approvals, cases=[case_id])
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
    return case_id


def newest(app: Engine, case_id: int) -> dict[str, Any]:
    with app.connect() as db:
        return dict(
            db.execute(
                text("select * from approval_request where case_id = :id order by id desc limit 1"),
                {"id": case_id},
            )
            .mappings()
            .one()
        )


def page_decides(
    app: Engine, case_id: int, choice: Choice = Choice.APPROVE, comment: str | None = None
) -> CaseStatus:
    """The person on the newest request decides on the approval page."""
    request = newest(app, case_id)
    with unit_of_work(app, actor_of(app, request["recipient_id"])) as db:
        proposal: int = db.execute(
            text(
                "select p.id from account_proposal p join invoice_case c on c.id = p.case_id"
                " where p.case_id = :id and p.vendor_account = c.partner_account"
                " order by p.id desc limit 1"
            ),
            {"id": case_id},
        ).scalar_one()
        done = decide(db, case_id, request["id"], choice, comment, proposal)
    return done.status


def status_of(app: Engine, case_id: int) -> CaseStatus:
    with app.connect() as db:
        return get_case(db, case_id).status


def amount_of(app: Engine, case_id: int) -> dict[str, Any] | None:
    with app.connect() as db:
        row = (
            db.execute(text("select * from approval_amount where case_id = :id"), {"id": case_id})
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def open_exception_of(app: Engine, case_id: int) -> tuple[int, str] | None:
    with app.connect() as db:
        row = db.execute(
            text(
                "select id, reason::text as reason from exception_item"
                " where case_id = :id and resolved_at is null"
            ),
            {"id": case_id},
        ).one_or_none()
    return (row.id, row.reason) if row else None


def events_of(app: Engine, case_id: int, *types: str) -> list[dict[str, Any]]:
    with app.connect() as db:
        rows = db.execute(
            text(
                "select event_type, actor, details from audit_event"
                " where case_id = :id and event_type = any(:types) order by chain_seq"
            ),
            {"id": case_id, "types": list(types)},
        ).mappings()
        return [dict(r) for r in rows]


def mails(fake: FastAPI) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = fake.state.mailbox.notices
    return found


# ------------------------------------------------------------------------------- the line


@pytest.mark.usefixtures("live", "benjamin")
@pytest.mark.parametrize(("cents", "settles"), [(500_000, True), (500_001, False)])
def test_eur_5000_00_needs_one_approval_and_5000_01_needs_two(
    app: Engine, fake: FastAPI, cents: int, settles: bool
) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, cents)

    after = page_decides(app, case_id)

    amount = amount_of(app, case_id)
    assert amount is not None
    assert (amount["source"], amount["rate"], amount["eur_minor"]) == ("eur", 1, cents)
    assert amount["above_line"] is (not settles)
    if settles:
        assert after is CaseStatus.APPROVED
    else:
        assert after is CaseStatus.AWAITING_APPROVAL
        assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


@pytest.mark.usefixtures("live", "benjamin")
def test_a_credit_note_counts_without_its_sign(app: Engine, fake: FastAPI) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, -500_001, kind=CaseKind.INCOMING_CREDIT_NOTE)

    assert page_decides(app, case_id) is CaseStatus.AWAITING_APPROVAL
    amount = amount_of(app, case_id)
    assert amount is not None and amount["above_line"] and amount["eur_minor"] == 500_001


# ------------------------------------------------------------------------ another currency


@pytest.mark.usefixtures("live", "benjamin")
@pytest.mark.parametrize(
    ("cents", "rate", "eur", "settles"),
    [
        (454_545, "1.1", 500_000, True),  # USD 4,545.45 at 1.1 is EUR 4,999.995, so 5,000.00
        (454_546, "1.1", 500_001, False),  # USD 4,545.46 at 1.1 is EUR 5,000.006, so 5,000.01
    ],
)
def test_another_currency_is_compared_at_the_wise_rate_of_the_first_approval(
    app: Engine, fake: FastAPI, cents: int, rate: str, eur: int, settles: bool
) -> None:
    rates = Rates({"USD": rate})
    approvals = approvals_with(app, fake, rates)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, cents, "USD")

    assert page_decides(app, case_id) is CaseStatus.AWAITING_APPROVAL
    assert amount_of(app, case_id) is None  # the page reads no Wise rate (AP-04)
    result = request_approvals(app, approvals, cases=[case_id])

    with app.connect() as db:
        decided_at: datetime = db.execute(
            text("select decided_at from approval_decision where case_id = :id"), {"id": case_id}
        ).scalar_one()
    assert rates.asked == [("USD", "EUR", wise_time(decided_at))]
    assert "." not in str(rates.asked[0][2])  # to the second, as Wise's examples write it
    amount = amount_of(app, case_id)
    assert amount is not None
    assert (amount["source"], str(amount["rate"]), amount["eur_minor"]) == ("wise", rate, eur)
    assert abs(amount["rate_time"] - decided_at) < timedelta(seconds=1)
    assert amount["written_by"] == "system:approval-request"
    (checked,) = events_of(app, case_id, "approval_amount_checked")
    assert checked["details"]["eur_minor"] == eur
    if settles:
        assert (result.rates_read, result.completed, result.second_requested) == (1, 1, 0)
        assert status_of(app, case_id) is CaseStatus.APPROVED
    else:
        assert (result.rates_read, result.completed, result.second_requested) == (1, 0, 1)
        assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL
        assert newest(app, case_id)["step"] == "second"


@pytest.mark.usefixtures("live", "benjamin")
@pytest.mark.parametrize("rates", [None, Rates({"USD": "1.1"}, fail=True)])
def test_without_a_wise_rate_the_case_waits_and_the_next_sweep_tries_again(
    app: Engine, fake: FastAPI, rates: Rates | None
) -> None:
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals_with(app, fake), account, 100, "USD")
    page_decides(app, case_id)

    waiting = request_approvals(app, approvals_with(app, fake, rates), cases=[case_id])
    assert waiting.rate_waiting == 1
    assert amount_of(app, case_id) is None
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL  # never "below the line"

    later = request_approvals(
        app, approvals_with(app, fake, Rates({"USD": "1.1"})), cases=[case_id]
    )
    assert later.completed == 1
    assert status_of(app, case_id) is CaseStatus.APPROVED


def test_only_argus_stores_a_wise_rate_and_eur_is_always_rate_1(
    app: Engine, fake: FastAPI, live: None, benjamin: str
) -> None:
    account, approver = vendor(app)
    usd = waiting_case(app, approvals_with(app, fake), account, 100, "USD")
    page_decides(app, usd)
    decision = newest_decision(app, usd)
    with (
        pytest.raises(CaseRuleBroken, match="only ARGUS stores the Wise rate"),
        unit_of_work(app, actor_of(app, approver)) as db,
    ):
        db.execute(
            text(
                "insert into approval_amount (decision_id, rate, rate_time) values (:d, 0.1, now())"
            ),
            {"d": decision},
        )
    # An EUR amount is the database's own: a rate handed in is replaced by 1.
    eur = waiting_case(app, approvals_with(app, fake), account, 900_000)
    page_decides(app, eur)
    amount = amount_of(app, eur)
    assert amount is not None and amount["rate"] == 1 and amount["above_line"]


def newest_decision(app: Engine, case_id: int) -> int:
    with app.connect() as db:
        found: int = db.execute(
            text("select max(id) from approval_decision where case_id = :id"), {"id": case_id}
        ).scalar_one()
    return found


# ------------------------------------------------------------------- the second request


@pytest.mark.usefixtures("live")
def test_above_the_line_benjamin_gets_the_second_request_and_his_approval_settles_it(
    app: Engine, fake: FastAPI, benjamin: str
) -> None:
    approvals = approvals_with(app, fake)
    account, alice = vendor(app)
    case_id = waiting_case(app, approvals, account, 650_000)
    page_decides(app, case_id, comment="Ordered for the Berlin project")

    result = request_approvals(app, approvals, cases=[case_id])

    assert (result.second_requested, result.sent) == (1, 1)
    second = newest(app, case_id)
    assert (second["step"], second["recipient_id"], second["same_person"]) == (
        "second",
        benjamin,
        False,
    )
    mail = mails(fake)[-1]
    assert mail["subject"].startswith("ARGUS: second approval for invoice")
    assert "Alice Approver gave the first approval" in mail["body"]["content"]
    with app.connect() as db:
        view = approval_view(db, case_id)
    assert view is not None and view.waiting
    assert view.first_approval is not None
    assert view.first_approval.decided_by_name == "Alice Approver"
    assert view.request.step == "second"

    assert page_decides(app, case_id) is CaseStatus.APPROVED
    first, last = events_of(app, case_id, "approval_decided")
    assert first["actor"] == alice and last["actor"] == benjamin
    assert last["details"]["step"] == "second" and "same_person" not in last["details"]


@pytest.mark.usefixtures("live", "benjamin")
def test_benjamin_can_reject_the_second_request_with_a_comment(app: Engine, fake: FastAPI) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, 650_000)
    page_decides(app, case_id)
    request_approvals(app, approvals, cases=[case_id])

    assert page_decides(app, case_id, Choice.REJECT, "Too expensive") is CaseStatus.CLOSED
    with app.connect() as db:
        assert get_case(db, case_id).closed_reason == "rejected"


@pytest.mark.req("AP-09", "AP-10")
@pytest.mark.usefixtures("live", "benjamin")
def test_the_database_refuses_approved_after_the_first_approval_alone(
    app: Engine, fake: FastAPI
) -> None:
    """AC-3: nothing can be booked after the first approval alone. argus_app tries every way."""
    approvals = approvals_with(app, fake)
    account, alice = vendor(app)
    eur = waiting_case(app, approvals, account, 500_001)
    usd = waiting_case(app, approvals, account, 100, "USD")
    for case_id, why in ((eur, "needs the second approval"), (usd, "waits for its amount")):
        request = newest(app, case_id)
        with (
            pytest.raises(CaseRuleBroken, match=why),
            unit_of_work(app, actor_of(app, alice)) as db,
        ):
            db.execute(
                text(
                    "insert into approval_decision (case_id, request_id, proposal_id, decision)"
                    " values (:c, :r, (select max(id) from account_proposal where case_id = :c),"
                    " 'approved')"
                ),
                {"c": case_id, "r": request["id"]},
            )
            move_case(db, case_id, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    page_decides(app, eur)
    for actor in (actor_of(app, alice), JOB, INTAKE):
        with pytest.raises(CaseRuleBroken), unit_of_work(app, actor) as db:
            move_case(db, eur, expected=CaseStatus.AWAITING_APPROVAL, to=CaseStatus.APPROVED)
    assert status_of(app, eur) is CaseStatus.AWAITING_APPROVAL


@pytest.mark.usefixtures("live", "benjamin")
def test_only_argus_writes_a_second_request_and_only_above_the_line(
    app: Engine, fake: FastAPI
) -> None:
    approvals = approvals_with(app, fake)
    account, alice = vendor(app)
    below = waiting_case(app, approvals, account, 100, "USD")
    page_decides(app, below)
    request_approvals(app, approvals_with(app, fake, Rates({"USD": "0.9"})), cases=[below])
    above = waiting_case(app, approvals, account, 600_000)
    with pytest.raises(CaseRuleBroken, match="no first approval"), unit_of_work(app, JOB) as db:
        db.execute(
            text("insert into approval_request (case_id, step) values (:c, 'second')"), {"c": above}
        )
    page_decides(app, above)
    with (
        pytest.raises(CaseRuleBroken, match="only ARGUS"),
        unit_of_work(app, actor_of(app, alice)) as db,
    ):
        db.execute(
            text("insert into approval_request (case_id, step) values (:c, 'second')"), {"c": above}
        )
    with pytest.raises(CaseRuleBroken, match="in approved"), unit_of_work(app, JOB) as db:
        db.execute(
            text("insert into approval_request (case_id, step) values (:c, 'second')"), {"c": below}
        )


@pytest.mark.usefixtures("live", "benjamin")
def test_the_first_approver_cannot_give_the_second_approval(app: Engine, fake: FastAPI) -> None:
    """Two different people (AP-09): the second request goes to Benjamin only, and nobody else
    decides on it, the first approver included."""
    approvals = approvals_with(app, fake)
    account, alice = vendor(app)
    case_id = waiting_case(app, approvals, account, 700_000)
    page_decides(app, case_id)
    request_approvals(app, approvals, cases=[case_id])
    second = newest(app, case_id)
    with (
        pytest.raises(CaseRuleBroken, match="only the person"),
        unit_of_work(app, actor_of(app, alice)) as db,
    ):
        db.execute(
            text(
                "insert into approval_decision (case_id, request_id, proposal_id, decision)"
                " values (:c, :r, (select max(id) from account_proposal where case_id = :c),"
                " 'approved')"
            ),
            {"c": case_id, "r": second["id"]},
        )


@pytest.mark.req("AP-08", "AP-09")
@pytest.mark.usefixtures("live", "benjamin")
def test_after_the_first_approval_nobody_changes_the_account_until_the_second_request(
    app: Engine, fake: FastAPI
) -> None:
    """The first approver's decision settles the account for that step; otherwise the account
    could change after the approval without anyone approving it again."""
    approvals = approvals_with(app, fake)
    account, alice = vendor(app)
    case_id = waiting_case(app, approvals, account, 700_000)
    page_decides(app, case_id)
    with (
        pytest.raises(CaseRuleBroken, match="decided on already"),
        unit_of_work(app, actor_of(app, alice)) as db,
    ):
        db.execute(
            text(
                "insert into account_proposal (case_id, vendor_account, gl_account, tax_key,"
                " chart, source, proposed_by_name, reason)"
                " values (:c, :v, '4980', '', 'SKR03', 'person', 'Alice Approver', 'Late change')"
            ),
            {"c": case_id, "v": account},
        )


# --------------------------------------------------------------------- nobody to ask


@pytest.mark.usefixtures("live")
def test_without_a_second_approver_the_case_goes_to_the_queue(
    app: Engine, fake: FastAPI, benjamin: str
) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, 600_000)
    page_decides(app, case_id)
    set_absent(app, ADMIN, benjamin, True)

    away = request_approvals(app, approvals, cases=[case_id])
    assert away.second_queued == 1
    found = open_exception_of(app, case_id)
    assert found is not None and found[1] == ExceptionReason.SECOND_APPROVER_MISSING
    assert status_of(app, case_id) is CaseStatus.EXCEPTION

    set_absent(app, ADMIN, benjamin, False)
    with unit_of_work(app, Actor.user(ARITA, "Arita Test")) as db:
        resolve_from_page(db, found[0], Outcome.ACCEPTED_WITH_COMMENT, "Benjamin is back")
    back = request_approvals(app, approvals, cases=[case_id])
    assert back.second_requested == 1
    assert newest(app, case_id)["recipient_id"] == benjamin


def test_no_second_approver_set_also_goes_to_the_queue(
    app: Engine, fake: FastAPI, live: None
) -> None:
    before = _second(app)
    if before:
        remove_second_approver(app, ADMIN)
    try:
        approvals = approvals_with(app, fake)
        account, _ = vendor(app)
        case_id = waiting_case(app, approvals, account, 600_000)
        page_decides(app, case_id)
        request_approvals(app, approvals, cases=[case_id])
        found = open_exception_of(app, case_id)
        assert found is not None and found[1] == ExceptionReason.SECOND_APPROVER_MISSING
    finally:
        if before:
            set_second_approver(app, ADMIN, before)


def _second(app: Engine) -> str | None:
    with app.connect() as db:
        return second_approver(db)


# --------------------------------------------------------- the same person (Arita, 23 Nov)


@pytest.mark.req("AP-09", "AP-08")
@pytest.mark.usefixtures("live")
def test_benjamin_as_vendor_approver_gives_both_approvals_marked_same_person(
    app: Engine, fake: FastAPI, benjamin: str
) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app, approver=benjamin)
    case_id = waiting_case(app, approvals, account, 800_000)
    page_decides(app, case_id)

    queued = request_approvals(app, approvals, cases=[case_id])

    assert queued.second_queued == 1
    found = open_exception_of(app, case_id)
    assert found is not None and found[1] == ExceptionReason.SECOND_APPROVER_SAME_PERSON
    with unit_of_work(app, Actor.user(ARITA, "Arita Test")) as db:
        resolve_from_page(db, found[0], Outcome.ACCEPTED_WITH_COMMENT, "As agreed on #39")
    sent = request_approvals(app, approvals, cases=[case_id])
    assert sent.second_requested == 1
    second = newest(app, case_id)
    assert (second["recipient_id"], second["same_person"]) == (benjamin, True)
    assert '"same person"' in mails(fake)[-1]["body"]["content"]

    assert page_decides(app, case_id) is CaseStatus.APPROVED

    with app.connect() as db:
        marked = db.execute(
            text("select same_person, step from approval_decision where request_id = :r"),
            {"r": second["id"]},
        ).one()
        trail = case_trail(db, case_id)
    assert (marked.same_person, marked.step) == (True, "second")
    requested = events_of(app, case_id, "approval_requested")[-1]
    decided = events_of(app, case_id, "approval_decided")[-1]
    assert requested["details"]["same_person"] is True
    assert decided["details"]["same_person"] is True
    assert trail is not None
    assert any('marked "same person"' in step.what for step in trail.steps)


@pytest.mark.req("AP-09")
@pytest.mark.usefixtures("live")
def test_without_the_resolved_exception_the_same_person_is_refused(
    app: Engine, fake: FastAPI, benjamin: str
) -> None:
    approvals = approvals_with(app, fake)
    account, _ = vendor(app, approver=benjamin)
    case_id = waiting_case(app, approvals, account, 800_000)
    page_decides(app, case_id)
    with pytest.raises(CaseRuleBroken, match="same person exception"), unit_of_work(app, JOB) as db:
        db.execute(
            text("insert into approval_request (case_id, step) values (:c, 'second')"),
            {"c": case_id},
        )


# ------------------------------------------------------------------- the setting


def test_only_an_admin_sets_the_second_approver_by_entra_id(app: Engine) -> None:
    entra = FakeEntra()
    client = TestClient(create_argus(lambda: app, sign_in=entra.verifier))
    before = _second(app)
    someone, _ = person(app, "Benjamin Second")
    admin = {"Authorization": f"Bearer {entra.token(ARITA, roles=[Role.ADMIN])}"}
    finance = {"Authorization": f"Bearer {entra.token(str(uuid.uuid4()), roles=[Role.FINANCE])}"}
    try:
        assert (
            client.put("/second-approver", json={"approver": someone}, headers=finance).status_code
            == 403
        )
        assert (
            client.put(
                "/second-approver", json={"approver": "benjamin@maxasp-test.example"}, headers=admin
            ).status_code
            == 422
        )
        stranger = str(uuid.uuid4())
        refused = client.put("/second-approver", json={"approver": stranger}, headers=admin)
        assert refused.status_code == 409
        done = client.put("/second-approver", json={"approver": someone.upper()}, headers=admin)
        assert done.status_code == 200 and done.json() == {"changed": True}
        assert client.get("/approvers", headers=finance).json()["second_approver"] == someone
        assert client.delete(f"/approvers/{someone}", headers=admin).status_code == 409
        with app.connect() as db:
            event: dict[str, Any] = db.execute(
                text(
                    "select details from audit_event where event_type = 'second_approver_set'"
                    " order by chain_seq desc limit 1"
                )
            ).scalar_one()
        assert event["approver"] == someone and event["name"]  # the admin, by name (NF-01)
    finally:
        if before:
            set_second_approver(app, ADMIN, before)


# ----------------------------------------------- review fixes on PR 210 (code and compliance)


@pytest.mark.usefixtures("live", "benjamin")
@pytest.mark.parametrize(
    "wrong",
    [
        Rates({"USD": "0.5"}, time="2020-01-01T00:00:00+0000"),  # an old or cached rate
        Rates({"USD": "0.5"}, pair=("GBP", "EUR")),  # a rate for other currencies
    ],
)
def test_a_wise_rate_from_another_time_or_pair_is_not_used(
    app: Engine, fake: FastAPI, wrong: Rates
) -> None:
    """Code review of PR 210: a stale rate of 0.5 made USD 9,000 pass as EUR 4,500, and the job
    approved it after one person. Now the case waits, as for any unreadable rate."""
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals_with(app, fake), account, 900_000, "USD")
    page_decides(app, case_id)

    result = request_approvals(app, approvals_with(app, fake, wrong), cases=[case_id])

    assert (result.rate_waiting, result.completed) == (1, 0)
    assert amount_of(app, case_id) is None
    assert status_of(app, case_id) is CaseStatus.AWAITING_APPROVAL


@pytest.mark.usefixtures("live", "benjamin")
def test_the_database_refuses_a_rate_time_away_from_the_first_approval(
    app: Engine, fake: FastAPI
) -> None:
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals_with(app, fake), account, 900_000, "USD")
    page_decides(app, case_id)
    decision = newest_decision(app, case_id)
    for shift in ("-2 hours", "10 minutes"):
        with (
            pytest.raises(CaseRuleBroken, match="not from the time of the first approval"),
            unit_of_work(app, JOB) as db,
        ):
            db.execute(
                text(
                    "insert into approval_amount (decision_id, rate, rate_time)"
                    " select id, 0.5, decided_at + cast(:shift as interval)"
                    " from approval_decision where id = :d"
                ),
                {"d": decision, "shift": shift},
            )
    assert amount_of(app, case_id) is None


@pytest.mark.req("AP-09", "AP-08")
@pytest.mark.usefixtures("live", "benjamin")
@pytest.mark.parametrize(("cents", "currency"), [(2_000_000, "USD"), (900_000, "EUR")])
def test_number_amount_and_currency_are_locked_once_the_first_approval_is_in(
    app: Engine, fake: FastAPI, cents: int, currency: str
) -> None:
    """Compliance review of PR 210: a correction from USD 20,000 to USD 100 after the approval
    let the job approve USD 100, and EUR 9,000 could reach Benjamin as EUR 90,000."""
    approvals = approvals_with(app, fake)
    account, _ = vendor(app)
    case_id = waiting_case(app, approvals, account, cents, currency)
    page_decides(app, case_id)
    with (
        pytest.raises(CaseRuleBroken, match="number, amount and currency cannot change"),
        unit_of_work(app, Actor.user(ARITA, "Arita Test")) as db,
    ):
        case = get_case(db, case_id)
        correct_case(
            db,
            case_id,
            document_number=str(case.document_number),
            gross_cents=cents // 200 if currency == "USD" else cents * 10,
            currency=currency,
            reason="Typo in the amount",
            name="Arita Test",
        )
    with app.connect() as db:
        assert get_case(db, case_id).gross_cents == cents


def test_nobody_sets_themselves_as_second_approver(app: Engine) -> None:
    """Compliance review of PR 210: Arita's same person exception is for Benjamin, not for the
    person who holds the admin role."""
    admin_id, _ = person(app, "Ada Admin")
    with pytest.raises(CaseRuleBroken, match="themselves"):
        set_second_approver(
            app, ChangedBy(Actor.user(admin_id, "Ada Admin"), "Ada Admin"), admin_id
        )


@pytest.mark.usefixtures("live")
def test_benjamin_as_deputy_gets_the_right_words_in_the_queue(
    app: Engine, fake: FastAPI, benjamin: str
) -> None:
    approvals = approvals_with(app, fake)
    account = str(next(_numbers))
    alice, _ = person(app, "Alice Approver")
    set_vendor_approver(app, ADMIN, account, alice, benjamin, False)
    set_absent(app, ADMIN, alice, True)
    case_id = waiting_case(app, approvals, account, 800_000)
    page_decides(app, case_id)

    request_approvals(app, approvals, cases=[case_id])

    with app.connect() as db:
        detail: str = db.execute(
            text("select detail from exception_item where case_id = :id and resolved_at is null"),
            {"id": case_id},
        ).scalar_one()
    assert "Benjamin Second gave the first approval as deputy of Alice Approver" in detail

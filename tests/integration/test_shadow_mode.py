"""Two weeks in shadow mode before ARGUS routes anything (#72).

Runs the sweep against the fake Graph, the recorded model answers and a real PostgreSQL. The
groups below follow the acceptance criteria of #72. Covers IN-04, IN-06, IN-09, NF-01 and
NF-03.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.cases.model import Actor
from argus.cases.store import CaseRuleBroken, NotAllowed, record_event, unit_of_work
from argus.intake.filing import Folders, database_clock, remove_due
from argus.intake.mode import (
    IntakeMode,
    SwitchedBy,
    intake_mode,
    is_live,
    switch_intake_mode,
)
from argus.intake.records import EmailStep, mark_classified
from argus.intake.shadow import (
    DELETED_ITEMS,
    GONE,
    NOT_LISTED,
    PersonFolders,
    mark_proposed,
    shadow_report,
)
from argus.intake.sweep import SWEEP_ACTOR, DocumentArchive, Sorting, sweep
from argus.sorting import Category, Route
from standins.entra.app import FakeEntra
from standins.graph.app import DELETED_ITEMS_ID, Folder
from tests.integration.conftest import TEST_ADMIN, TEST_SWITCH, set_mode
from tests.integration.test_email_sorting import (
    INVOICE,
    OTHER,
    REMINDER,
    REVIEW,
    CountingModel,
    events,
    open_exceptions,
    record_of,
    run,
)
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
    fake_graph_with,
    graph_for,
    mailbox_of,
)
from tests.unit.sorting_helpers import decision_for

pytestmark = pytest.mark.req("IN-04", "IN-06", "IN-09")

INCOMING = "Rechnungen/Eingang"
REMINDERS = "Mahnungen"
FOLDER_LIST = PersonFolders(
    {INCOMING: Category.INCOMING_INVOICE, REMINDERS: Category.PAYMENT_REMINDER}
)

FINANCE = "33333333-3333-4333-8333-333333333333"  # made up
APPROVER = "11111111-1111-4111-8111-111111111111"  # made up
ADDRESS = "admin@maxasp.example"  # made up


@pytest.fixture
def shadow_mode(owner: Engine) -> Iterator[None]:
    set_mode(owner, IntakeMode.SHADOW)
    yield
    set_mode(owner, IntakeMode.SHADOW)


def cases_of(app: Engine, record_id: int) -> int:
    with app.connect() as connection:
        found: int = connection.execute(
            text("select count(*) from invoice_case where source_email_id = :id"),
            {"id": record_id},
        ).scalar_one()
        return found


def mode_events(app: Engine, since: int) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select details, actor_type, actor from audit_event"
                " where event_type = 'intake_mode_changed' and chain_seq > :since"
                " order by chain_seq"
            ),
            {"since": since},
        ).mappings()
        return [dict(row) for row in rows]


def last_seq(app: Engine) -> int:
    with app.connect() as connection:
        found: int = connection.execute(
            text("select coalesce(max(chain_seq), 0) from audit_event")
        ).scalar_one()
        return found


def person_moves(fake: FastAPI, message_id: str, path: str) -> None:
    """A person moves the email into the folder with this path, making the folders as needed."""
    box = mailbox_of(fake)
    parent: str | None = None
    for name in path.split("/"):
        found = next(
            (f.id for f in box.folders.values() if f.parent_id == parent and f.name == name),
            None,
        )
        if found is None:
            found = f"AAMkAGPerson{len(box.folders):04d}"
            box.folders[found] = Folder(found, name, parent)
        parent = found
    assert parent is not None
    box.location[message_id] = parent


# AC: a setting switches ARGUS between shadow mode and live mode; shadow mode is the default.


@pytest.mark.req("NF-01")
def test_shadow_mode_is_the_default(owner: Engine) -> None:
    with owner.connect() as connection:
        default: str = connection.execute(
            text(
                "select column_default from information_schema.columns"
                " where table_name = 'intake_mode' and column_name = 'mode'"
            )
        ).scalar_one()
        rows: int = connection.execute(text("select count(*) from intake_mode")).scalar_one()
    assert "shadow" in default
    assert rows == 1


@pytest.mark.usefixtures("shadow_mode")
def test_the_mode_can_be_switched_both_ways(app: Engine) -> None:
    assert not is_live(app)

    assert switch_intake_mode(app, TEST_SWITCH, IntakeMode.LIVE) is True
    assert is_live(app)
    assert switch_intake_mode(app, TEST_SWITCH, IntakeMode.LIVE) is False  # already live
    assert switch_intake_mode(app, TEST_SWITCH, IntakeMode.SHADOW) is True
    with app.connect() as connection:
        assert intake_mode(connection) is IntakeMode.SHADOW


# AC: in shadow mode ARGUS stores its proposed route and reason for every email but does not
# move, forward or delete anything.


@pytest.mark.req("IN-05", "IN-07", "NF-01")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_in_shadow_mode_argus_proposes_a_route_for_every_email_and_touches_nothing(
    app: Engine,
) -> None:
    fake = fake_graph_with(INVOICE, REMINDER, OTHER, REVIEW)
    model = CountingModel()
    documents, sorting, graph = run(fake, model)
    box = mailbox_of(fake)
    folders_before = set(box.folders)

    first = sweep(app, graph, DocumentArchive(documents), sorting)
    second = sweep(app, graph, DocumentArchive(documents), sorting)

    assert (first.stored, first.proposed, first.failed) == (4, 4, 0)
    assert (first.classified, first.routed, first.filed) == (0, 0, 0)
    assert (second.proposed, second.classified, second.routed, second.filed) == (0, 0, 0, 0)
    assert model.calls == 4  # the second sweep does not ask the model again
    # Nothing moved, forwarded, deleted or created in the mailbox.
    assert len(box.inbox) == 4
    assert box.moves == []
    assert box.drafts == {} and box.sent == []
    assert box.deleted == {}
    assert set(box.folders) == folders_before
    expected = {
        INVOICE: Route.INVOICE_PROCESSING,
        REMINDER: Route.FORWARD_REMINDER,
        OTHER: Route.DELETION_FOLDER,
        REVIEW: Route.MANUAL_REVIEW,
    }
    for email_id, route in expected.items():
        record = record_of(app, graph, email_id)
        assert (record.step, record.route, record.folder) == (EmailStep.STORED, None, None)
        assert events(app, record.id, "email_classified") == []
        [proposed] = events(app, record.id, "email_proposed")
        assert proposed["actor"] == SWEEP_ACTOR.name
        assert proposed["details"]["route"] == route.value
        assert "reasons" in proposed["details"]
        assert proposed["details"]["model_confidence"] is not None
        # No case and no exception: a person sorts the Inbox while ARGUS only proposes.
        assert cases_of(app, record.id) == 0
        assert open_exceptions(app, record.id) == []
    review = record_of(app, graph, REVIEW)
    [proposed] = events(app, review.id, "email_proposed")
    assert proposed["details"]["reasons"]  # why it would have gone to a person (IN-09)


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_in_shadow_mode_the_daily_removal_deletes_nothing(app: Engine, owner: Engine) -> None:
    fake = fake_graph_with(OTHER)
    documents, sorting, graph = run(fake, CountingModel())
    set_mode(owner, IntakeMode.LIVE)
    sweep(app, graph, DocumentArchive(documents), sorting)
    record = record_of(app, graph, OTHER)
    assert record.folder == "ARGUS/Removed"
    set_mode(owner, IntakeMode.SHADOW)
    now = database_clock(app)()

    result = remove_due(app, graph, Folders(graph), clock=lambda: now + timedelta(days=31))

    assert result.shadow is True
    assert result.deleted == 0
    assert mailbox_of(fake).deleted == {}
    assert events(app, record.id, "email_deleted") == []


@pytest.mark.req("IN-05")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_switching_back_to_shadow_mode_stops_a_forward_and_filing(
    app: Engine, owner: Engine
) -> None:
    fake = fake_graph_with(REMINDER)
    documents, no_forwarding, graph = run(fake, CountingModel(), forward=False)
    set_mode(owner, IntakeMode.LIVE)
    sweep(app, graph, DocumentArchive(documents), no_forwarding)  # waits for forwarding
    set_mode(owner, IntakeMode.SHADOW)
    forwarding = graph_for(fake)
    with_forwarding = replace(no_forwarding, reminders=forwarding)

    result = sweep(app, forwarding, DocumentArchive(documents), with_forwarding)

    record = record_of(app, graph, REMINDER)
    assert record.step is EmailStep.CLASSIFIED  # classified in live mode, then nothing more
    assert (result.routed, result.filed) == (0, 0)
    box = mailbox_of(fake)
    assert box.drafts == {} and box.sent == [] and box.moves == []


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_the_database_classifies_nothing_in_shadow_mode(app: Engine) -> None:
    fake = fake_graph_with(INVOICE)
    documents, _, graph = run(fake, CountingModel())
    sweep(app, graph, DocumentArchive(documents))  # stores only
    record = record_of(app, graph, INVOICE)

    with (
        pytest.raises(CaseRuleBroken, match="shadow mode"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        mark_classified(db, record.id, decision_for(Route.INVOICE_PROCESSING))


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_the_database_takes_a_proposal_once_and_only_in_shadow_mode(
    app: Engine, owner: Engine
) -> None:
    fake = fake_graph_with(INVOICE)
    documents, _, graph = run(fake, CountingModel())
    sweep(app, graph, DocumentArchive(documents))  # stores only
    record = record_of(app, graph, INVOICE)
    decision = decision_for(Route.INVOICE_PROCESSING)

    set_mode(owner, IntakeMode.LIVE)
    with (
        pytest.raises(CaseRuleBroken, match="only in shadow mode"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        db.execute(
            text(
                'update mailbox_email set proposal = \'{"route": "manual_review"}\' where id = :id'
            ),
            {"id": record.id},
        )
    set_mode(owner, IntakeMode.SHADOW)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert mark_proposed(db, record.id, decision) is True
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert mark_proposed(db, record.id, decision) is False
    with pytest.raises(CaseRuleBroken, match="set once"), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text(
                'update mailbox_email set proposal = \'{"route": "manual_review"}\' where id = :id'
            ),
            {"id": record.id},
        )


# AC: only Arita can switch to live mode, and the switch is in the audit log.


@pytest.mark.req("NF-01", "NF-04")
@pytest.mark.usefixtures("shadow_mode")
def test_a_job_can_never_switch_argus_to_live_mode(app: Engine) -> None:
    with pytest.raises(PermissionError):
        switch_intake_mode(app, replace(TEST_SWITCH, actor=SWEEP_ACTOR), IntakeMode.LIVE)
    with (
        pytest.raises(CaseRuleBroken, match="only a person"),
        unit_of_work(app, Actor.system("mailbox-sweep")) as db,
    ):
        db.execute(text("update intake_mode set mode = 'live'"))
    assert not is_live(app)


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("shadow_mode")
def test_each_switch_is_in_the_audit_log_with_the_person(app: Engine) -> None:
    since = last_seq(app)

    switch_intake_mode(app, TEST_SWITCH, IntakeMode.LIVE)
    switch_intake_mode(app, TEST_SWITCH, IntakeMode.LIVE)  # no change, nothing logged
    switch_intake_mode(app, TEST_SWITCH, IntakeMode.SHADOW)

    who = {"name": TEST_SWITCH.name, "reason": TEST_SWITCH.reason}
    admin = {"actor_type": "user", "actor": TEST_ADMIN.name}
    assert mode_events(app, since) == [
        {"details": {"from": "shadow", "to": "live", **who}, **admin},
        {"details": {"from": "live", "to": "shadow", **who}, **admin},
    ]


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("shadow_mode")
def test_a_switch_needs_the_persons_name_and_a_reason(app: Engine) -> None:
    for name, reason in (("", "report checked"), ("Test Admin", ""), ("Test Admin", "  ")):
        with pytest.raises(CaseRuleBroken, match="name and a reason"):
            switch_intake_mode(app, SwitchedBy(TEST_ADMIN, name, reason), IntakeMode.LIVE)
    # Setting only the mode, without who and why in the same transaction, is refused too, so
    # a switch never reuses an earlier one's name or reason.
    switch_intake_mode(app, TEST_SWITCH, IntakeMode.LIVE)
    with (
        pytest.raises(CaseRuleBroken, match="name and a reason"),
        unit_of_work(app, TEST_ADMIN) as db,
    ):
        db.execute(text("update intake_mode set mode = 'shadow'"))
    assert is_live(app)
    with app.connect() as connection:
        row = connection.execute(text("select changed_by_name, reason from intake_mode")).one()
    assert tuple(row) == (TEST_SWITCH.name, TEST_SWITCH.reason)
    with pytest.raises(NotAllowed), unit_of_work(app, TEST_ADMIN) as db:
        db.execute(text("update intake_mode set reason = 'changed afterwards'"))


@pytest.mark.req("NF-01")
def test_the_app_cannot_write_the_shadow_events_or_change_the_mode_row(app: Engine) -> None:
    for event_type in ("intake_mode_changed", "email_proposed", "email_sorted_by_person"):
        with (
            pytest.raises(CaseRuleBroken, match="written by the database"),
            unit_of_work(app, TEST_ADMIN) as db,
        ):
            record_event(db, event_type, details={"to": "live"})
    for statement in (
        "insert into intake_mode (id, mode) values (false, 'live')",
        "delete from intake_mode",
        "update intake_mode set changed_at = now()",
    ):
        with pytest.raises(NotAllowed), unit_of_work(app, TEST_ADMIN) as db:
            db.execute(text(statement))


def token_for(entra: FakeEntra, who: str, role: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer {entra.token(who, roles=[role])}"}


@pytest.mark.req("NF-01", "NF-03")
@pytest.mark.usefixtures("shadow_mode")
def test_only_the_admin_can_switch_in_the_app(app: Engine) -> None:
    entra = FakeEntra()
    client = TestClient(create_app(lambda: app, sign_in=entra.verifier))
    since = last_seq(app)

    def put(who: str, role: Role, mode: str, reason: str = "report checked") -> httpx.Response:
        token = entra.token(who, roles=[role], name="Arita Test", sign_in_address=ADDRESS)
        response: httpx.Response = client.put(
            "/intake/mode",
            json={"mode": mode, "reason": reason},
            headers={"Authorization": f"Bearer {token}"},
        )
        return response

    assert put(FINANCE, Role.FINANCE, "live").status_code == 403
    assert put(APPROVER, Role.APPROVER, "live").status_code == 403
    assert client.put("/intake/mode", json={"mode": "live"}).status_code == 401
    assert not is_live(app)

    switched = put(TEST_ADMIN.name, Role.ADMIN, "live")
    assert switched.status_code == 200
    assert switched.json() == {"mode": "live", "changed": True}
    read = client.get("/intake/mode", headers=token_for(entra, FINANCE, Role.FINANCE))
    assert read.json() == {"mode": "live"}
    assert put(TEST_ADMIN.name, Role.ADMIN, "shadow").json() == {"mode": "shadow", "changed": True}
    assert put(TEST_ADMIN.name, Role.ADMIN, "half").status_code == 422
    assert put(TEST_ADMIN.name, Role.ADMIN, "live", reason="").status_code == 422
    assert put(TEST_ADMIN.name, Role.ADMIN, "live", reason="   ").status_code == 422
    assert not is_live(app)
    to_live, to_shadow = mode_events(app, since)
    assert to_live["actor"] == to_shadow["actor"] == TEST_ADMIN.name
    assert to_live["details"] == {
        "from": "shadow",
        "to": "live",
        "name": "Arita Test",
        "sign_in_address": ADDRESS,
        "reason": "report checked",
    }


# AC: a report compares ARGUS's proposals with what the person did, per category, and lists
# every invoice ARGUS would have sent to Other.


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean", "shadow_mode")
def test_the_report_compares_proposals_with_what_the_person_did(app: Engine) -> None:
    fake = fake_graph_with(INVOICE, REMINDER, OTHER, REVIEW)
    documents, sorting, graph = run(fake, CountingModel())
    sorting = _with_folders(sorting)
    sweep(app, graph, DocumentArchive(documents), sorting)
    ids = {e: record_of(app, graph, e) for e in (INVOICE, REMINDER, OTHER, REVIEW)}

    # The person sorts the Inbox. They treat the newsletter ARGUS would have sent to Other as
    # an incoming invoice, which is the mistake the report must show.
    person_moves(fake, ids[INVOICE].graph_message_id, INCOMING)
    person_moves(fake, ids[REMINDER].graph_message_id, REMINDERS)
    person_moves(fake, ids[OTHER].graph_message_id, INCOMING)
    mailbox_of(fake).location[ids[REVIEW].graph_message_id] = DELETED_ITEMS_ID
    result = sweep(app, graph, DocumentArchive(documents), sorting)
    again = sweep(app, graph, DocumentArchive(documents), sorting)

    assert (result.sorted_by_person, result.failed) == (4, 0)
    assert again.sorted_by_person == 0
    [sorted_event] = events(app, ids[REMINDER].id, "email_sorted_by_person")
    assert sorted_event["details"] == {"folder": REMINDERS, "category": "payment_reminder"}
    assert mailbox_of(fake).moves == []  # the moves were the person's, not ARGUS's
    with app.connect() as connection:
        report = shadow_report(connection)
    assert (report.mode, report.proposed, report.sorted_by_person, report.waiting) == (
        IntakeMode.SHADOW,
        4,
        4,
        0,
    )
    rows = {row.category: row for row in report.categories}
    assert set(rows) == {"incoming_invoice", "payment_reminder", "other"}
    incoming = rows["incoming_invoice"]
    assert (incoming.emails, incoming.same_route, incoming.other_route) == (2, 1, 1)
    assert incoming.proposed == {"deletion_folder": 1, "invoice_processing": 1}
    assert (rows["payment_reminder"].emails, rows["payment_reminder"].same_route) == (1, 1)
    assert (rows["other"].emails, rows["other"].manual_review) == (1, 1)
    [missed] = report.invoices_to_other
    assert missed.email_id == ids[OTHER].id
    assert (missed.person_folder, missed.person_category) == (INCOMING, "incoming_invoice")
    assert missed.model_category == "other"


@pytest.mark.usefixtures("clean", "shadow_mode")
def test_a_folder_not_on_the_list_and_a_deleted_email_are_recorded_as_such(
    app: Engine,
) -> None:
    fake = fake_graph_with(INVOICE, OTHER)
    documents, sorting, graph = run(fake, CountingModel())
    sorting = _with_folders(sorting)
    sweep(app, graph, DocumentArchive(documents), sorting)
    invoice, other = record_of(app, graph, INVOICE), record_of(app, graph, OTHER)

    person_moves(fake, invoice.graph_message_id, "Ablage")
    mailbox_of(fake).remove(other.graph_message_id)  # deleted for good
    sweep(app, graph, DocumentArchive(documents), sorting)

    [listed] = events(app, invoice.id, "email_sorted_by_person")
    assert listed["details"] == {"folder": NOT_LISTED, "category": None}
    [gone] = events(app, other.id, "email_sorted_by_person")
    assert gone["details"] == {"folder": GONE, "category": "other"}
    with app.connect() as connection:
        report = shadow_report(connection)
    assert [row.category for row in report.categories] == ["other", "not_listed"]
    assert report.invoices_to_other == []
    assert DELETED_ITEMS == "Deleted Items"


@pytest.mark.req("NF-03")
@pytest.mark.usefixtures("shadow_mode")
def test_only_finance_and_admin_see_the_report(app: Engine) -> None:
    entra = FakeEntra()
    client = TestClient(create_app(lambda: app, sign_in=entra.verifier))

    def get(who: str, role: Role) -> httpx.Response:
        response: httpx.Response = client.get(
            "/intake/shadow-report", headers=token_for(entra, who, role)
        )
        return response

    assert get(APPROVER, Role.APPROVER).status_code == 403
    report = get(FINANCE, Role.FINANCE)
    assert report.status_code == 200
    assert set(report.json()) == {
        "mode",
        "proposed",
        "sorted_by_person",
        "waiting",
        "categories",
        "invoices_to_other",
    }
    assert get(TEST_ADMIN.name, Role.ADMIN).status_code == 200


def _with_folders(sorting: Sorting) -> Sorting:
    return replace(sorting, person_folders=FOLDER_LIST)


# The folder list setting


def test_the_folder_list_is_read_from_the_environment() -> None:
    folders = PersonFolders.from_environment(
        {"ARGUS_SHADOW_FOLDERS": '{"/Rechnungen/Eingang/": "incoming_invoice"}'}
    )
    assert folders.folders == {"Rechnungen/Eingang": Category.INCOMING_INVOICE}
    assert PersonFolders.from_environment({}).folders == {}
    for bad in ("[]", "not json", '{"Ablage": "invoice"}', '{"": "other"}'):
        with pytest.raises(ValueError, match="ARGUS_SHADOW_FOLDERS"):
            PersonFolders.from_environment({"ARGUS_SHADOW_FOLDERS": bad})

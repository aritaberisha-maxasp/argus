"""Processed emails leave the Inbox, and Other is deleted 30 days later, not earlier (#26).

Runs against the fake Graph serving the 50 fixture emails and a real PostgreSQL. The groups
below follow the acceptance criteria of #26. Covers IN-06 and NF-01 (ADR 0003).

These tests leave the sorting out of the sweep and record a chosen decision for each email
themselves, with the rights the application has, so each route can be tested on its own.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.cases.store import CaseRuleBroken, NotAllowed, unit_of_work, verify_chain
from argus.intake.filing import (
    FILING_ACTOR,
    FOLDER_FOR,
    REMOVAL_ACTOR,
    REMOVED_FOLDER,
    Folders,
    file_routed,
    remove_due,
)
from argus.intake.graph import GraphCallRefused, GraphClient
from argus.intake.records import (
    DeletionOutcome,
    EmailRecord,
    get_email,
    mark_classified,
    mark_deleted,
    mark_routed,
)
from argus.intake.sweep import SWEEP_ACTOR, sweep
from argus.sorting.models import Route
from standins.graph.app import DELETED_ITEMS_ID, INBOX_ID
from tests.integration.test_mailbox_sweep import MemoryArchive
from tests.unit.sorting_helpers import decision_for

DAY = timedelta(days=1)


def stored_emails(app: Engine, graph: GraphClient) -> list[EmailRecord]:
    """Sweep the fixture emails so every one has a record at step stored."""
    sweep(app, graph, MemoryArchive())
    with app.connect() as connection:
        ids = connection.execute(text("select graph_message_id from mailbox_email order by id"))
        records = [get_email(connection, row[0]) for row in ids]
    return [r for r in records if r is not None]


def route(app: Engine, record: EmailRecord, to: Route) -> None:
    """Record a decision for ``to``, then route, as the sweep does since #91."""
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert mark_classified(db, record.id, decision_for(to))
        assert mark_routed(db, record.id, to)


def row(engine: Engine, email_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        found = connection.execute(
            text("select * from mailbox_email where id = :id"), {"id": email_id}
        ).mappings()
        return dict(found.one())


def events(engine: Engine, email_id: int) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "select event_type, actor_type || ':' || actor as actor, details"
                " from audit_event where email_id = :id order by chain_seq"
            ),
            {"id": email_id},
        ).mappings()
        return [dict(e) for e in found]


def move_filing_back(owner: Engine, email_id: int, days: int) -> datetime:
    """Pretend ARGUS filed the email ``days`` ago. Only the owner can, with the guard off for
    this test step; the application can never set the time."""
    with owner.begin() as connection:
        connection.execute(
            text("alter table mailbox_email disable trigger mailbox_email_filing_guard")
        )
        filed_at: datetime = connection.execute(
            text(
                "update mailbox_email set filed_at = filed_at - make_interval(days => :days)"
                " where id = :id returning filed_at"
            ),
            {"id": email_id, "days": days},
        ).scalar_one()
        connection.execute(
            text("alter table mailbox_email enable trigger mailbox_email_filing_guard")
        )
    return filed_at


def fixed(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def filed_other(app: Engine, graph: GraphClient) -> EmailRecord:
    other = stored_emails(app, graph)[0]
    route(app, other, Route.DELETION_FOLDER)
    file_routed(app, graph, Folders(graph))
    return other


# Every processed email ends up in the ARGUS folder for its category ------------------------


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_each_routed_email_is_moved_into_the_folder_for_its_route(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    records = stored_emails(app, graph)
    chosen = dict(zip(Route, records, strict=False))
    for to, record in chosen.items():
        route(app, record, to)

    result = sweep(app, graph, MemoryArchive())

    assert result.filed == len(Route) == 5
    assert result.failed == 0
    box = fake_graph.state.mailbox
    assert len(box.inbox) == 50 - 5
    for to, record in chosen.items():
        assert box.in_folder(FOLDER_FOR[to]) == {record.graph_message_id}
        filed = row(app, record.id)
        assert (filed["step"], filed["route"], filed["folder"]) == ("filed", to, FOLDER_FOR[to])
        assert filed["filed_at"] is not None
    assert box.deleted == {}


@pytest.mark.req("IN-06")
def test_the_folder_tree_is_argus_with_one_folder_per_route() -> None:
    assert FOLDER_FOR == {
        Route.INVOICE_PROCESSING: "ARGUS/Incoming invoices",
        Route.OUTGOING_BOOKING: "ARGUS/Outgoing invoices",
        Route.FORWARD_REMINDER: "ARGUS/Payment reminders",
        Route.DELETION_FOLDER: "ARGUS/Removed",
        Route.MANUAL_REVIEW: "ARGUS/Manual review",
    }


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_emails_that_are_not_routed_stay_in_the_inbox(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    records = stored_emails(app, graph)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        assert mark_classified(db, records[0].id, decision_for(Route.DELETION_FOLDER))

    result = sweep(app, graph, MemoryArchive())

    assert result.filed == 0
    assert len(fake_graph.state.mailbox.inbox) == 50
    assert fake_graph.state.mailbox.moves == []


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_filing_twice_moves_and_records_once(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    record = stored_emails(app, graph)[0]
    route(app, record, Route.INVOICE_PROCESSING)
    file_routed(app, graph, Folders(graph))
    before = events(app, record.id)

    again = file_routed(app, graph, Folders(graph))

    assert (again.filed, again.failed) == (0, 0)
    assert events(app, record.id) == before
    assert len(fake_graph.state.mailbox.moves) == 1


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_email_moved_just_before_a_crash_is_recorded_on_the_next_run(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    record = stored_emails(app, graph)[0]
    route(app, record, Route.OUTGOING_BOOKING)
    folders = Folders(graph)
    # The move reached Graph, then ARGUS stopped before recording it.
    graph.move(record.graph_message_id, folders.id_of("ARGUS/Outgoing invoices"))
    assert record.graph_message_id not in fake_graph.state.mailbox.inbox

    result = file_routed(app, graph, folders)

    assert result.filed == 1
    assert row(app, record.id)["folder"] == "ARGUS/Outgoing invoices"
    assert [e["event_type"] for e in events(app, record.id)].count("email_moved") == 1


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_the_argus_folders_are_created_once(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    records = stored_emails(app, graph)
    for record in records[:3]:
        route(app, record, Route.DELETION_FOLDER)
    file_routed(app, graph, Folders(graph))
    route(app, records[3], Route.DELETION_FOLDER)
    file_routed(app, graph, Folders(graph))

    names = sorted(f.name for f in fake_graph.state.mailbox.folders.values())
    assert names == ["ARGUS", "Deleted Items", "Inbox", "Removed"]
    assert len(fake_graph.state.mailbox.in_folder(REMOVED_FOLDER)) == 4


# Other goes to ARGUS/Removed, never deleted straight away ---------------------------------


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_other_goes_to_removed_and_stays_there(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)

    result = remove_due(app, graph, Folders(graph))  # today, by the database clock

    assert (result.due, result.deleted) == (0, 0)
    assert fake_graph.state.mailbox.in_folder(REMOVED_FOLDER) == {other.graph_message_id}
    assert fake_graph.state.mailbox.deleted == {}
    assert row(app, other.id)["deleted_at"] is None


@pytest.mark.req("IN-06")
@pytest.mark.parametrize(
    ("route_to", "folder"),
    [
        (Route.INVOICE_PROCESSING, REMOVED_FOLDER),
        (Route.MANUAL_REVIEW, REMOVED_FOLDER),
        (Route.DELETION_FOLDER, "ARGUS/Incoming invoices"),
    ],
)
@pytest.mark.usefixtures("clean")
def test_the_database_keeps_removed_for_other_only(
    app: Engine, graph: GraphClient, route_to: Route, folder: str
) -> None:
    record = stored_emails(app, graph)[0]
    route(app, record, route_to)
    with pytest.raises(CaseRuleBroken), unit_of_work(app, FILING_ACTOR) as db:
        db.execute(
            text("update mailbox_email set step = 'filed', folder = :folder where id = :id"),
            {"id": record.id, "folder": folder},
        )


# A daily job deletes emails 30 days after they went to ARGUS/Removed, not one day earlier ---


@pytest.mark.req("IN-06")
@pytest.mark.parametrize("early", [DAY, timedelta(seconds=1)], ids=["one-day", "one-second"])
@pytest.mark.usefixtures("clean")
def test_nothing_is_deleted_before_30_days_with_a_fixed_clock(
    app: Engine, graph: GraphClient, fake_graph: FastAPI, early: timedelta
) -> None:
    other = filed_other(app, graph)
    filed_at = row(app, other.id)["filed_at"]

    result = remove_due(app, graph, Folders(graph), fixed(filed_at + 30 * DAY - early))

    assert (result.due, result.deleted) == (0, 0)
    assert fake_graph.state.mailbox.deleted == {}
    assert row(app, other.id)["deleted_at"] is None


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_the_email_is_deleted_30_days_after_it_was_moved_with_a_fixed_clock(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)
    filed_at = move_filing_back(owner, other.id, days=30)

    result = remove_due(app, graph, Folders(graph), fixed(filed_at + 30 * DAY))

    assert (result.due, result.deleted, result.failed) == (1, 1, 0)
    box = fake_graph.state.mailbox
    assert set(box.deleted) == {other.graph_message_id}
    assert box.in_folder(REMOVED_FOLDER) == set()
    deleted_at = row(app, other.id)["deleted_at"]
    assert deleted_at is not None and deleted_at >= filed_at + 30 * DAY


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_the_daily_job_deletes_only_what_is_due(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    records = stored_emails(app, graph)
    for record in records[:3]:
        route(app, record, Route.DELETION_FOLDER)
    file_routed(app, graph, Folders(graph))
    move_filing_back(owner, records[0].id, days=31)
    move_filing_back(owner, records[1].id, days=29)

    result = remove_due(app, graph, Folders(graph))  # the database clock, as in production

    assert (result.due, result.deleted) == (1, 1)
    assert set(fake_graph.state.mailbox.deleted) == {records[0].graph_message_id}
    assert fake_graph.state.mailbox.in_folder(REMOVED_FOLDER) == {
        records[1].graph_message_id,
        records[2].graph_message_id,
    }


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_the_database_refuses_a_deletion_before_30_days(
    app: Engine, owner: Engine, graph: GraphClient
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=29)
    with (
        pytest.raises(CaseRuleBroken, match="less than 30 days"),
        unit_of_work(app, REMOVAL_ACTOR) as db,
    ):
        mark_deleted(db, other.id, DeletionOutcome.DELETED)


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_the_database_refuses_to_delete_an_email_outside_removed(
    app: Engine, owner: Engine, graph: GraphClient
) -> None:
    invoice = stored_emails(app, graph)[0]
    route(app, invoice, Route.INVOICE_PROCESSING)
    file_routed(app, graph, Folders(graph))
    move_filing_back(owner, invoice.id, days=40)
    with (
        pytest.raises(CaseRuleBroken, match="not in ARGUS/Removed"),
        unit_of_work(app, REMOVAL_ACTOR) as db,
    ):
        mark_deleted(db, invoice.id, DeletionOutcome.DELETED)


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_an_email_a_person_moved_out_of_removed_is_not_deleted(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    fake_graph.state.mailbox.location[other.graph_message_id] = INBOX_ID  # a person kept it

    result = remove_due(app, graph, Folders(graph))

    assert (result.due, result.deleted, result.failed) == (1, 0, 1)
    assert fake_graph.state.mailbox.deleted == {}
    assert row(app, other.id)["deleted_at"] is None


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_email_deleted_just_before_a_crash_is_recorded_on_the_next_run(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    folders = Folders(graph)
    removed = folders.existing(REMOVED_FOLDER)
    assert removed is not None
    assert graph.delete_from(other.graph_message_id, removed)  # then ARGUS stopped
    # Like Graph, the email still exists, in Deleted Items, not answered with 404.
    assert graph.folder_of(other.graph_message_id) == DELETED_ITEMS_ID

    result = remove_due(app, graph, folders)

    assert (result.deleted, result.failed) == (1, 0)
    assert set(fake_graph.state.mailbox.deleted) == {other.graph_message_id}, "not deleted twice"
    deleted = [e for e in events(app, other.id) if e["event_type"] == "email_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["details"]["outcome"] == "already_gone"
    assert row(app, other.id)["deletion_outcome"] == "already_gone"
    again = remove_due(app, graph, folders)
    assert (again.due, again.deleted) == (0, 0)


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_the_graph_client_refuses_to_delete_an_invoice(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    invoice = stored_emails(app, graph)[0]
    route(app, invoice, Route.INVOICE_PROCESSING)
    file_routed(app, graph, Folders(graph))
    removed = Folders(graph).id_of(REMOVED_FOLDER)

    with pytest.raises(GraphCallRefused):
        graph.delete_from(invoice.graph_message_id, removed)
    assert fake_graph.state.mailbox.deleted == {}


# Filing and removal are written to the audit log -------------------------------------------


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_filing_and_removal_are_in_the_audit_log(
    app: Engine, owner: Engine, graph: GraphClient
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    remove_due(app, graph, Folders(graph))

    logged = [e for e in events(app, other.id) if e["event_type"].startswith("email_")]
    assert [e["event_type"] for e in logged] == [
        "email_seen",
        "email_stored",
        "email_classified",
        "email_routed",
        "email_filed",
        "email_moved",
        "email_deleted",
    ]
    moved, deleted = logged[-2], logged[-1]
    assert moved["actor"] == str(FILING_ACTOR)
    assert moved["details"] == {"to_folder": "ARGUS/Removed", "route": "deletion_folder"}
    assert deleted["actor"] == str(REMOVAL_ACTOR)
    assert deleted["details"]["outcome"] == "deleted"
    assert deleted["details"]["folder"] == "ARGUS/Removed"
    assert deleted["details"]["days_in_folder"] == 30
    assert set(deleted["details"]) == {"outcome", "folder", "filed_at", "days_in_folder"}
    with app.connect() as connection:
        assert verify_chain(connection) is None


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.parametrize("event", ["email_moved", "email_deleted"])
@pytest.mark.usefixtures("clean")
def test_the_application_cannot_write_a_filing_event_itself(
    app: Engine, graph: GraphClient, event: str
) -> None:
    record = stored_emails(app, graph)[0]
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, FILING_ACTOR) as db,
    ):
        db.execute(
            text(
                "insert into audit_event (email_id, event_type, details) values (:id, :event, '{}')"
            ),
            {"id": record.id, "event": event},
        )


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ("folder = 'ARGUS/Incoming invoices'", CaseRuleBroken),
        ("route = 'invoice_processing'", CaseRuleBroken),
        ("step = 'routed'", CaseRuleBroken),
        ("filed_at = now() - interval '60 days'", NotAllowed),
    ],
)
@pytest.mark.usefixtures("clean")
def test_where_and_when_an_email_was_filed_cannot_be_changed(
    app: Engine, graph: GraphClient, change: str, refusal: type[Exception]
) -> None:
    other = filed_other(app, graph)
    with pytest.raises(refusal), unit_of_work(app, FILING_ACTOR) as db:
        db.execute(text(f"update mailbox_email set {change} where id = :id"), {"id": other.id})  # noqa: S608


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_deletion_is_recorded_once_and_its_time_set_by_the_database(
    app: Engine, owner: Engine, graph: GraphClient
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    with unit_of_work(app, REMOVAL_ACTOR) as db:
        db.execute(
            text(
                "update mailbox_email set deleted_at = '2000-01-01',"
                " deletion_outcome = 'deleted' where id = :id"
            ),
            {"id": other.id},
        )
    deleted_at = row(app, other.id)["deleted_at"]
    assert deleted_at.year >= 2026
    with (
        pytest.raises(CaseRuleBroken, match="already deleted"),
        unit_of_work(app, REMOVAL_ACTOR) as db,
    ):
        db.execute(
            text("update mailbox_email set deleted_at = now() where id = :id"), {"id": other.id}
        )


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("deleted_at = now()", "needs its outcome"),
        ("deletion_outcome = 'deleted'", "set with its deletion"),
        ("deleted_at = now(), deletion_outcome = 'removed'", "deletion_outcome"),
    ],
)
@pytest.mark.usefixtures("clean")
def test_a_deletion_always_says_whether_argus_deleted_the_email(
    app: Engine, owner: Engine, graph: GraphClient, change: str, message: str
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    with (
        pytest.raises(CaseRuleBroken, match=message),
        unit_of_work(app, REMOVAL_ACTOR) as db,
    ):
        db.execute(text(f"update mailbox_email set {change} where id = :id"), {"id": other.id})  # noqa: S608


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_the_deletion_outcome_cannot_be_changed_afterwards(
    app: Engine, owner: Engine, graph: GraphClient
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    remove_due(app, graph, Folders(graph))
    assert row(app, other.id)["deletion_outcome"] == "deleted"
    with (
        pytest.raises(CaseRuleBroken, match="already deleted"),
        unit_of_work(app, REMOVAL_ACTOR) as db,
    ):
        db.execute(
            text("update mailbox_email set deletion_outcome = 'already_gone' where id = :id"),
            {"id": other.id},
        )


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_moved_names_only_where_the_email_went_wherever_it_came_from(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    record = stored_emails(app, graph)[0]
    route(app, record, Route.INVOICE_PROCESSING)
    folders = Folders(graph)
    # A person dragged the email into another folder before ARGUS filed it.
    elsewhere = graph.create_folder(None, "Archive")
    graph.move(record.graph_message_id, elsewhere)

    assert file_routed(app, graph, folders).filed == 1

    assert fake_graph.state.mailbox.in_folder("ARGUS/Incoming invoices") == {
        record.graph_message_id
    }
    moved = [e for e in events(app, record.id) if e["event_type"] == "email_moved"]
    assert [e["details"] for e in moved] == [
        {"to_folder": "ARGUS/Incoming invoices", "route": "invoice_processing"}
    ]


@pytest.mark.req("IN-06")
@pytest.mark.usefixtures("clean")
def test_a_clock_running_ahead_deletes_nothing(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    # Review of PR 92: the database decides before the delete, not only when it is recorded.
    other = filed_other(app, graph)
    filed_at = row(app, other.id)["filed_at"]

    result = remove_due(app, graph, Folders(graph), fixed(filed_at + 31 * DAY))

    assert (result.due, result.deleted, result.skipped, result.failed) == (1, 0, 1, 0)
    assert fake_graph.state.mailbox.deleted == {}
    assert fake_graph.state.mailbox.in_folder(REMOVED_FOLDER) == {other.graph_message_id}
    assert row(app, other.id)["deleted_at"] is None


@pytest.mark.req("IN-06", "NF-01")
@pytest.mark.usefixtures("clean")
def test_an_email_a_person_deleted_by_hand_is_recorded_as_already_gone(
    app: Engine, owner: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    other = filed_other(app, graph)
    move_filing_back(owner, other.id, days=30)
    # A person deleted it in Outlook: like Graph, it now sits in Deleted Items.
    fake_graph.state.mailbox.location[other.graph_message_id] = DELETED_ITEMS_ID

    result = remove_due(app, graph, Folders(graph))

    assert (result.deleted, result.failed) == (1, 0)
    assert row(app, other.id)["deletion_outcome"] == "already_gone"
    assert set(fake_graph.state.mailbox.deleted) == {other.graph_message_id}, "left as it was"

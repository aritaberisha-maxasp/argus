"""The mailbox sweep picks up every email once and stores it first (#24, ADR 0003).

Runs the sweep against the fake Graph serving the 50 fixture emails and a real PostgreSQL.
The acceptance criteria of #24 map to the groups below. Covers IN-01, NF-01 and NF-06.

``MemoryArchive`` is the real archive of the sweep on the in-memory document store of #20, so
the files are recorded in stored_document exactly as in production; only the blobs live in
memory instead of Azurite.
"""

import hashlib
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from psycopg import errors as pg
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from argus.cases.store import (
    CaseRuleBroken,
    NotAllowed,
    record_event,
    unit_of_work,
    verify_chain,
)
from argus.documents.email import EML_CONTENT_TYPE, StoredEmail, attachments_of
from argus.documents.store import MemoryDocumentStore
from argus.intake.graph import GraphClient, InboxMessage
from argus.intake.records import EmailStep, get_email, see_email
from argus.intake.sweep import (
    SWEEP_ACTOR,
    DocumentArchive,
    process_email,
    run_every,
    sweep,
    sweep_lock,
)
from tests.unit.intake.graph_helpers import client_for

FIXTURE_COUNT = 50


@dataclass
class MemoryArchive:
    """The real archive on the in-memory document store of #20. Counts every call so tests can
    see how often each email was stored, and can fail on purpose to simulate a crash."""

    documents: MemoryDocumentStore = field(default_factory=MemoryDocumentStore)
    puts: Counter[str] = field(default_factory=Counter)
    recorded: list[tuple[int, str]] = field(default_factory=list)
    fail_after_store: set[str] = field(default_factory=set)
    fail_in_record: set[str] = field(default_factory=set)
    delay: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def blobs(self) -> dict[str, bytes]:
        """The raw emails in the store, by fingerprint."""
        return {
            location.removeprefix("sha256/"): data
            for location, (data, content_type) in self.documents.files.items()
            if content_type == EML_CONTENT_TYPE
        }

    def store(self, raw: bytes) -> StoredEmail:
        time.sleep(self.delay)
        with self.lock:
            stored = DocumentArchive(self.documents).store(raw)
            self.puts[stored.raw.sha256] += 1
        if stored.raw.sha256 in self.fail_after_store:
            self.fail_after_store.discard(stored.raw.sha256)
            raise RuntimeError("crash after storing, before recording")
        return stored

    def record(self, connection: Connection, email_id: int, stored: StoredEmail) -> None:
        if stored.raw.sha256 in self.fail_in_record:
            self.fail_in_record.discard(stored.raw.sha256)
            raise RuntimeError("crash while recording")
        DocumentArchive(self.documents).record(connection, email_id, stored)
        with self.lock:
            self.recorded.append((email_id, stored.raw.sha256))


def records(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("select id, graph_message_id, step, raw_sha256 from mailbox_email order by id")
        ).mappings()
        return [dict(r) for r in rows]


def email_events(engine: Engine) -> Counter[tuple[int, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select e.email_id, e.event_type from audit_event e"
                " join mailbox_email m on m.id = e.email_id"
                " where e.event_type like 'email%'"
            )
        )
        return Counter((r.email_id, r.event_type) for r in rows)


# The fake Graph serves the fixture emails --------------------------------------------------


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_one_sweep_picks_up_all_fixture_emails(app: Engine, graph: GraphClient) -> None:
    archive = MemoryArchive()
    result = sweep(app, graph, archive)

    assert result.ran
    assert (result.listed, result.new, result.stored, result.failed) == (50, 50, 50, 0)
    rows = records(app)
    assert len(rows) == FIXTURE_COUNT
    assert {r["step"] for r in rows} == {"stored"}
    assert sorted(archive.recorded) == sorted((r["id"], r["raw_sha256"]) for r in rows)


# Exactly one record per email, keyed by the immutable message ID ----------------------------


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_a_second_sweep_creates_nothing_new(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    archive = MemoryArchive()
    sweep(app, graph, archive)
    events_before = email_events(app)

    again = sweep(app, graph, archive)

    assert (again.listed, again.new, again.stored) == (50, 0, 0)
    assert len(records(app)) == FIXTURE_COUNT
    assert email_events(app) == events_before
    assert set(archive.puts.values()) == {1}
    assert set(fake_graph.state.mailbox.raw_reads.values()) == {1}


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_records_are_keyed_by_the_immutable_id(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    sweep(app, graph, MemoryArchive())
    assert {r["graph_message_id"] for r in records(app)} == set(fake_graph.state.mailbox.inbox)


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_the_database_refuses_a_second_record_for_the_same_id(app: Engine) -> None:
    with unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(text("insert into mailbox_email (graph_message_id) values ('AAk-same')"))
    with pytest.raises(CaseRuleBroken), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(text("insert into mailbox_email (graph_message_id) values ('AAk-same')"))


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_a_new_email_is_picked_up_by_the_next_sweep(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    archive = MemoryArchive()
    sweep(app, graph, archive)
    late = b"From: a@vendor.example\r\nMessage-ID: <late@fixtures.argus.example>\r\n\r\nHallo\r\n"
    fake_graph.state.mailbox.add(late)

    result = sweep(app, graph, archive)

    assert (result.listed, result.new, result.stored) == (51, 1, 1)
    assert hashlib.sha256(late).hexdigest() in archive.blobs


# Two sweeps at the same time process each email once ----------------------------------------


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_two_sweeps_at_the_same_time_process_each_email_once(
    app: Engine, fake_graph: FastAPI
) -> None:
    archive = MemoryArchive(delay=0.01)
    start = threading.Barrier(2)
    results = []

    def run() -> None:
        graph = client_for(fake_graph)
        start.wait()
        results.append(sweep(app, graph, archive))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(r.ran for r in results) == [False, True], "one sweep runs, the other skips"
    assert len(records(app)) == FIXTURE_COUNT
    assert set(archive.puts.values()) == {1}, "each email stored once"
    assert len(archive.recorded) == FIXTURE_COUNT
    events = email_events(app)
    assert Counter(kind for _, kind in events.elements()) == {
        "email_seen": FIXTURE_COUNT,
        "email_stored": FIXTURE_COUNT,
    }


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_without_the_lock_two_workers_still_record_an_email_once(
    app: Engine, fake_graph: FastAPI
) -> None:
    """Belt and braces: if two workers ever race on one email, the unique ID and the step
    check let only one of them record it as stored."""
    archive = MemoryArchive(delay=0.05)
    message = next(client_for(fake_graph).list_inbox())
    start = threading.Barrier(4)
    outcomes: list[tuple[bool, bool]] = []

    def run() -> None:
        graph = client_for(fake_graph)
        start.wait()
        outcomes.append(process_email(app, graph, archive, message))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sum(new for new, _ in outcomes) == 1, "one record created"
    assert sum(stored for _, stored in outcomes) == 1, "one worker recorded it stored"
    assert len(archive.blobs) == 1, "one copy, never overwritten"
    assert len(archive.recorded) == 1
    events = email_events(app)
    assert sorted(kind for _, kind in events.elements()) == ["email_seen", "email_stored"]


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_a_sweep_skips_while_another_holds_the_lock(app: Engine, graph: GraphClient) -> None:
    with sweep_lock(app) as got:
        assert got
        assert sweep(app, graph, MemoryArchive()).ran is False
    assert records(app) == []
    with sweep_lock(app) as got:
        assert got, "the lock is given back"


# Stored before anything else happens to it -----------------------------------------------


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_each_email_is_stored_byte_for_byte(app: Engine, graph: GraphClient) -> None:
    archive = MemoryArchive()
    sweep(app, graph, archive)
    for row in records(app):
        blob = archive.blobs[str(row["raw_sha256"])]
        assert hashlib.sha256(blob).hexdigest() == row["raw_sha256"]
        assert blob == graph.get_raw(str(row["graph_message_id"]))


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_the_email_and_every_attachment_are_recorded_in_the_document_store(
    app: Engine, graph: GraphClient
) -> None:
    archive = MemoryArchive()
    sweep(app, graph, archive)
    with app.connect() as connection:
        documents = connection.execute(
            text(
                "select d.email_id, d.kind, d.sha256 from stored_document d"
                " join mailbox_email m on m.id = d.email_id"
            )
        ).all()
        stored_events = connection.execute(
            text(
                "select count(*) from audit_event e join mailbox_email m on m.id = e.email_id"
                " where e.event_type = 'document_stored'"
            )
        ).scalar()
    for row in records(app):
        raw = archive.blobs[row["raw_sha256"]]
        mine = [d for d in documents if d.email_id == row["id"]]
        assert [d.sha256 for d in mine if d.kind == "email"] == [row["raw_sha256"]]
        assert len([d for d in mine if d.kind == "attachment"]) == len(attachments_of(raw))
    assert stored_events == len(documents)
    assert any(d.kind == "attachment" for d in documents)


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("later", ["classified", "routed", "filed"])
def test_no_later_step_can_happen_before_the_email_is_stored(app: Engine, later: str) -> None:
    with unit_of_work(app, SWEEP_ACTOR) as db:
        record, _ = see_email(db, InboxMessage("AAk-early", None, None))
    with (
        pytest.raises(CaseRuleBroken, match="cannot go from seen"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        db.execute(
            text("update mailbox_email set step = :s where id = :id"),
            {"s": later, "id": record.id},
        )


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_stored_needs_the_fingerprint(app: Engine) -> None:
    with unit_of_work(app, SWEEP_ACTOR) as db:
        record, _ = see_email(db, InboxMessage("AAk-nofp", None, None))
    with pytest.raises(CaseRuleBroken), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(
            text("update mailbox_email set step = 'stored' where id = :id"), {"id": record.id}
        )


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    ("assignment", "refusal"),
    [
        ("raw_sha256 = repeat('0', 64)", CaseRuleBroken),
        ("raw_location = 'sha256/other'", CaseRuleBroken),
        ("step = 'seen', raw_sha256 = null, raw_location = null", CaseRuleBroken),
        ("graph_message_id = 'AAk-other'", NotAllowed),
        ("received_at = now()", NotAllowed),
        ("seen_at = now()", NotAllowed),
        ("stored_at = now()", NotAllowed),
    ],
)
def test_the_stored_copy_and_the_reported_facts_cannot_be_changed(
    app: Engine, graph: GraphClient, assignment: str, refusal: type[Exception]
) -> None:
    sweep(app, graph, MemoryArchive())
    email_id = records(app)[0]["id"]
    sql = f"update mailbox_email set {assignment} where id = :id"  # noqa: S608  fixed test SQL
    with pytest.raises(refusal), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(text(sql), {"id": email_id})


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_the_app_cannot_delete_an_email_record(app: Engine, graph: GraphClient) -> None:
    sweep(app, graph, MemoryArchive())
    with pytest.raises(NotAllowed), unit_of_work(app, SWEEP_ACTOR) as db:
        db.execute(text("delete from mailbox_email"))


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("statement", ["delete from mailbox_email", "truncate mailbox_email"])
def test_not_even_the_owner_can_remove_an_email_record(
    owner: Engine, app: Engine, graph: GraphClient, statement: str
) -> None:
    sweep(app, graph, MemoryArchive())
    with pytest.raises(DBAPIError, match="cannot be deleted"), owner.begin() as connection:
        connection.execute(text(statement))
    assert len(records(app)) == FIXTURE_COUNT


# Each step is recorded and safe to repeat after a crash -------------------------------------


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    "event_type",
    ["email_seen", "email_stored", "email_classified", "email_routed", "email_filed"],
)
def test_the_application_cannot_write_an_email_event_itself(
    app: Engine, graph: GraphClient, event_type: str
) -> None:
    """Only the database writes the step events, so the log can never claim a step that did
    not happen."""
    sweep(app, graph, MemoryArchive())
    email_id = records(app)[0]["id"]
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        record_event(db, event_type, email_id=email_id, details={"raw_sha256": "0" * 64})
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, SWEEP_ACTOR) as db,
    ):
        db.execute(
            text("insert into audit_event (email_id, event_type) values (:id, :t)"),
            {"id": email_id, "t": event_type},
        )
    assert Counter(t for _, t in email_events(app).elements())[event_type] == (
        FIXTURE_COUNT if event_type in ("email_seen", "email_stored") else 0
    )


@pytest.mark.req("NF-01")
@pytest.mark.usefixtures("clean")
def test_other_application_events_about_an_email_are_still_allowed(
    app: Engine, graph: GraphClient
) -> None:
    """The guard is about the step events only, so later stories can log their own facts."""
    sweep(app, graph, MemoryArchive())
    with unit_of_work(app, SWEEP_ACTOR) as db:
        record_event(db, "classification_suggested", email_id=records(app)[0]["id"])


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_each_step_writes_one_audit_event_with_its_actor(app: Engine, graph: GraphClient) -> None:
    sweep(app, graph, MemoryArchive())
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select e.email_id, e.event_type, e.actor_type, e.actor, e.details, m.raw_sha256"
                " from audit_event e join mailbox_email m on m.id = e.email_id"
                " where e.event_type like 'email%' order by e.chain_seq"
            )
        ).all()
        assert verify_chain(connection) is None
    assert Counter(r.event_type for r in rows) == {
        "email_seen": FIXTURE_COUNT,
        "email_stored": FIXTURE_COUNT,
    }
    assert {(r.actor_type, r.actor) for r in rows} == {("system", "mailbox-sweep")}
    for r in rows:
        if r.event_type == "email_stored":
            assert r.details == {
                "raw_sha256": r.raw_sha256,
                "raw_location": f"sha256/{r.raw_sha256}",
            }
        else:
            assert set(r.details) == {"graph_message_id"}, "no sender, subject or content"


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_crash_before_storing_is_finished_by_the_next_sweep(
    app: Engine, graph: GraphClient, fake_graph: FastAPI
) -> None:
    """The document store is unreachable for one email: its record stays at seen and the
    next sweep stores it."""
    first = next(client_for(fake_graph).list_inbox())
    archive = MemoryArchive()
    original_store = archive.store
    calls = {"n": 0}

    def crash_once(raw: bytes) -> StoredEmail:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("document store unreachable")
        return original_store(raw)

    archive.store = crash_once  # type: ignore[method-assign]
    result = sweep(app, graph, archive)
    assert (result.new, result.stored, result.failed) == (50, 49, 1)
    with unit_of_work(app, SWEEP_ACTOR) as db:
        stuck = get_email(db, first.id)
    assert stuck is not None and stuck.step is EmailStep.SEEN

    again = sweep(app, graph, archive)

    assert (again.new, again.stored, again.failed) == (0, 1, 0)
    assert {r["step"] for r in records(app)} == {"stored"}
    events = email_events(app)
    assert events[(stuck.id, "email_seen")] == 1
    assert events[(stuck.id, "email_stored")] == 1


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_crash_after_storing_stores_again_without_a_second_copy(
    app: Engine, graph: GraphClient
) -> None:
    """The file reached the store but the step was not recorded. The next sweep stores the
    same bytes again, gets the same copy back and records the step once."""
    first_raw = graph.get_raw(next(graph.list_inbox()).id)
    sha = hashlib.sha256(first_raw).hexdigest()
    archive = MemoryArchive(fail_after_store={sha})

    result = sweep(app, graph, archive)
    assert result.failed == 1
    assert sha in archive.blobs

    sweep(app, graph, archive)

    assert archive.puts[sha] == 2
    assert len(archive.blobs) == FIXTURE_COUNT, "no second copy"
    row = next(r for r in records(app) if r["raw_sha256"] == sha)
    events = email_events(app)
    assert events[(row["id"], "email_stored")] == 1
    assert [e for e, s in archive.recorded if s == sha] == [row["id"]]


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.usefixtures("clean")
def test_a_crash_while_recording_rolls_back_the_step(app: Engine, graph: GraphClient) -> None:
    """Recording the documents and marking the email stored are one transaction: if one fails,
    neither is kept, and the next sweep does both."""
    first_raw = graph.get_raw(next(graph.list_inbox()).id)
    sha = hashlib.sha256(first_raw).hexdigest()
    archive = MemoryArchive(fail_in_record={sha})

    assert sweep(app, graph, archive).failed == 1
    with app.connect() as connection:
        step = connection.execute(
            text("select step from mailbox_email where id = (select min(id) from mailbox_email)")
        ).scalar()
    assert step == "seen"

    assert sweep(app, graph, archive).stored == 1
    assert {r["step"] for r in records(app)} == {"stored"}


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_run_every_keeps_sweeping_until_stopped(app: Engine, graph: GraphClient) -> None:
    stop = threading.Event()
    archive = MemoryArchive()
    worker = threading.Thread(
        target=run_every, args=(app, graph, archive, stop, timedelta(milliseconds=10))
    )
    worker.start()
    deadline = time.monotonic() + 30
    while len(records(app)) < FIXTURE_COUNT and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert len(records(app)) == FIXTURE_COUNT


@pytest.mark.req("IN-01")
@pytest.mark.usefixtures("clean")
def test_a_change_without_an_actor_is_refused(app: Engine) -> None:
    with pytest.raises(DBAPIError) as error, app.begin() as connection:
        connection.execute(text("insert into mailbox_email (graph_message_id) values ('AAk-x')"))
    assert isinstance(error.value.orig, pg.RaiseException)
    assert records(app) == []

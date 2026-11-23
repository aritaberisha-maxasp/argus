"""The nightly copy of the audit log and the check against it (issue #83).

Runs against the test PostgreSQL and Azurite, with no real Azure access. Each test gets its own
blob container, so it sees only its own copies. The audit log itself is shared by all tests and
append only, so a copy here holds every event written so far; the tests look at the numbers
relative to what was there before.

To show what the check finds, the owner breaks the log inside a transaction that is rolled
back at the end, so the shared log stays whole for the other tests.
"""

import json
import logging
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from argus.audit.copies import (
    EVENT_COLUMNS,
    AzureCopyStore,
    CopyHeader,
    CopyStore,
    decode,
    encode,
    event_record,
    name_for,
)
from argus.audit.nightly import (
    CheckReport,
    CopyRefused,
    CopyResult,
    check_copies,
    main,
    run_nightly,
    snapshot,
    write_copy,
)
from argus.cases.model import Actor, ExceptionReason, Outcome
from argus.cases.store import (
    CaseRuleBroken,
    list_open_exceptions,
    open_exception,
    record_event,
    resolve_exception,
    unit_of_work,
    verify_chain,
)
from argus.working_days import berlin_date

JOB = Actor.system("audit-test")
# Nights in the past, so events written by a test count as later than every copy's night,
# as they would after the nightly run. The test of copies written ahead uses later nights.
TODAY = berlin_date(datetime.now(UTC))
NIGHT_1 = TODAY - timedelta(days=3)
NIGHT_2 = TODAY - timedelta(days=2)
NIGHT_3 = TODAY - timedelta(days=1)
N1, N2, N3 = NIGHT_1.isoformat(), NIGHT_2.isoformat(), NIGHT_3.isoformat()
AUDIT_REASONS = (ExceptionReason.AUDIT_COPY_FAILED, ExceptionReason.AUDIT_CHECK_FAILED)


@pytest.fixture
def copies(blob_connection_string: str) -> AzureCopyStore:
    store = AzureCopyStore.from_connection_string(
        blob_connection_string, f"audit-test-{uuid.uuid4().hex[:12]}"
    )
    store.create_container_if_missing()
    return store


@pytest.fixture(autouse=True)
def no_open_audit_exceptions(app: Engine) -> Iterator[None]:
    """Each test starts and ends with no open audit copy exception, as a person would have
    resolved it."""

    def resolve_all() -> None:
        with unit_of_work(app, Actor.user("auditor-test", "Auditor Test")) as db:
            for item in list_open_exceptions(db):
                if item.reason in AUDIT_REASONS:
                    resolve_exception(db, item.id, Outcome.RETRIED)

    resolve_all()
    yield
    resolve_all()


def add_events(app: Engine, count: int) -> list[int]:
    with unit_of_work(app, JOB) as db:
        return [
            record_event(db, "audit_copy_test", details={"step": n}) for n in range(1, count + 1)
        ]


def newest_seq(engine: Engine) -> int:
    with engine.connect() as connection:
        value: int = connection.execute(text("select max(chain_seq) from audit_event")).scalar_one()
    return value


def copy(app: Engine, store: CopyStore, night: date) -> CopyResult:
    with snapshot(app) as connection:
        return write_copy(connection, store, night)


def check(app: Engine, store: CopyStore, night: date | None = None) -> CheckReport:
    with snapshot(app) as connection:
        return check_copies(connection, store, night)


@pytest.fixture
def broken(owner: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is rolled back at the end. Call
    :func:`unguard` on it, after the copy is written, to turn the append only guard off."""
    with owner.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def unguard(connection: Connection) -> None:
    # Locks the table until the rollback, so it comes after every write of the test.
    connection.execute(text("alter table audit_event disable trigger audit_no_change"))


# --- AC: every event since the last copy, with its chain number and hash, as one file ---


@pytest.mark.req("NF-01")
def test_the_copy_holds_every_event_with_its_chain_number_and_hash(
    app: Engine, copies: AzureCopyStore
) -> None:
    add_events(app, 3)
    last = newest_seq(app)

    result = copy(app, copies, NIGHT_1)

    assert result.written
    assert (result.name, result.first_seq, result.last_seq) == (name_for(NIGHT_1), 1, last)
    assert copies.names() == [f"audit-log/{N1}.jsonl"]
    saved = decode(result.name, copies.read(result.name))
    assert saved.header.prev_hash == "00"
    assert saved.header.event_count == last
    assert [e["chain_seq"] for e in saved.events] == list(range(1, last + 1))
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select chain_seq, encode(hash, 'hex'), encode(prev_hash, 'hex')"
                " from audit_event order by chain_seq"
            )
        ).all()
    assert [(e["chain_seq"], e["hash"], e["prev_hash"]) for e in saved.events] == [
        tuple(r) for r in rows
    ]
    assert saved.events[-1]["details"] == {"step": 3}
    assert saved.events[-1]["actor"] == "audit-test"


@pytest.mark.req("NF-01")
def test_the_next_night_starts_right_after_the_last_copy(
    app: Engine, copies: AzureCopyStore
) -> None:
    first = copy(app, copies, NIGHT_1)
    add_events(app, 2)

    second = copy(app, copies, NIGHT_2)

    assert second.written
    assert second.first_seq == first.last_seq + 1
    assert second.last_seq == newest_seq(app)
    saved = decode(second.name, copies.read(second.name))
    assert saved.header.prev_hash == decode(first.name, copies.read(first.name)).events[-1]["hash"]
    assert check(app, copies).ok


@pytest.mark.req("NF-01")
def test_a_night_without_new_events_still_leaves_a_copy(
    app: Engine, copies: AzureCopyStore
) -> None:
    first = copy(app, copies, NIGHT_1)

    quiet = copy(app, copies, NIGHT_2)

    assert quiet.written
    assert (quiet.first_seq, quiet.last_seq, quiet.event_count) == (
        first.last_seq + 1,
        first.last_seq,
        0,
    )
    assert decode(quiet.name, copies.read(quiet.name)).events == []
    assert check(app, copies).ok


@pytest.mark.req("NF-01")
def test_the_copy_holds_exactly_the_columns_of_the_audit_log(
    app: Engine, copies: AzureCopyStore
) -> None:
    """Nothing is added to a copy that is not already in the log, so no free text or IBAN can
    get in through the copy. A new column has to be added here on purpose."""
    with app.connect() as connection:
        columns: list[str] = list(
            connection.execute(
                text(
                    "select column_name from information_schema.columns"
                    " where table_name = 'audit_event' order by ordinal_position"
                )
            ).scalars()
        )
        assert tuple(columns) == EVENT_COLUMNS
    add_events(app, 1)
    result = copy(app, copies, NIGHT_1)
    for event in decode(result.name, copies.read(result.name)).events:
        assert tuple(sorted(event)) == tuple(sorted(EVENT_COLUMNS))


# --- AC: running twice for the same night creates no second copy and no gaps ---


@pytest.mark.req("NF-01")
def test_running_twice_for_the_same_night_writes_nothing_the_second_time(
    app: Engine, copies: AzureCopyStore
) -> None:
    first = copy(app, copies, NIGHT_1)
    before = copies.read(first.name)
    add_events(app, 2)

    again = copy(app, copies, NIGHT_1)

    assert not again.written
    assert (again.name, again.first_seq, again.last_seq) == (
        first.name,
        first.first_seq,
        first.last_seq,
    )
    assert copies.names() == [first.name]
    assert copies.read(first.name) == before
    # The events of the second run are not lost: the next night takes them.
    following = copy(app, copies, NIGHT_2)
    assert following.first_seq == first.last_seq + 1
    assert following.last_seq == newest_seq(app)
    assert check(app, copies).ok


@pytest.mark.req("NF-01")
def test_a_copy_that_is_there_already_is_never_replaced(
    app: Engine, copies: AzureCopyStore
) -> None:
    result = copy(app, copies, NIGHT_1)
    before = copies.read(result.name)

    assert not copies.write_new(result.name, b"something else")
    assert copies.read(result.name) == before


@pytest.mark.req("NF-01")
def test_an_earlier_night_cannot_follow_a_later_one(app: Engine, copies: AzureCopyStore) -> None:
    copy(app, copies, NIGHT_2)

    with pytest.raises(CopyRefused, match=f"{N2} is already copied"):
        copy(app, copies, NIGHT_1)
    assert copies.names() == [name_for(NIGHT_2)]


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("night", [TODAY + timedelta(days=1), date(2062, 1, 1)])
def test_a_night_after_today_is_refused_before_anything_is_written(
    app: Engine, copies: AzureCopyStore, night: date
) -> None:
    """Review of PR 102: a locked copy for a future night (a typo such as 2062 for 2026) could
    never be removed and would stop every later night."""
    with pytest.raises(CopyRefused, match=f"the night {night} is after today"):
        copy(app, copies, night)

    assert copies.names() == []
    assert copy(app, copies, TODAY).written


@pytest.mark.req("NF-01")
def test_a_newest_copy_that_ends_past_the_database_is_refused(
    app: Engine, copies: AzureCopyStore
) -> None:
    """Starting after a copy that claims more events than the database has would leave every
    real event up to that number uncopied for good (review of PR 102)."""
    beyond = newest_seq(app) + 100
    assert copies.write_new(
        name_for(NIGHT_1), encode(CopyHeader(NIGHT_1, beyond + 1, beyond, 0, "ab" * 32), [])
    )

    with pytest.raises(CopyRefused, match=f"ends at event {beyond}, but the database ends"):
        copy(app, copies, NIGHT_2)
    assert copies.names() == [name_for(NIGHT_1)]


@pytest.mark.req("NF-01")
def test_the_copy_only_reads_and_does_not_wait_for_the_audit_lock(
    app: Engine, copies: AzureCopyStore
) -> None:
    """A writer that holds the audit lock does not stop the copy, and its event, not yet
    committed, is left for the next night."""
    before = newest_seq(app)
    with unit_of_work(app, JOB) as writer:
        record_event(writer, "audit_copy_test", details={"step": "open"})
        with app.connect() as reader:
            reader = reader.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            with reader.begin():
                reader.execute(text("set local lock_timeout = '2s'"))
                reader.execute(text("set local statement_timeout = '5s'"))
                result = write_copy(reader, copies, NIGHT_1)
    assert result.last_seq == before
    assert copy(app, copies, NIGHT_2).last_seq == before + 1


# --- AC: the check reports missing or changed events and gaps ---


@pytest.mark.req("NF-01")
def test_cutting_off_the_newest_events_is_found(
    app: Engine, copies: AzureCopyStore, broken: Connection
) -> None:
    add_events(app, 3)
    result = copy(app, copies, NIGHT_1)
    last = result.last_seq

    unguard(broken)
    broken.execute(text("delete from audit_event where chain_seq > :seq"), {"seq": last - 2})

    # The chain alone still looks whole: that is the gap this story closes.
    assert verify_chain(broken) is None
    report = check_copies(broken, copies)
    assert [(f.kind, f.first_seq, f.last_seq) for f in report.findings] == [
        ("missing", last - 1, last)
    ]
    assert report.findings[0].message == (
        f"events {last - 1} to {last} from {result.name} are missing in the database"
    )


@pytest.mark.req("NF-01")
def test_an_event_changed_in_place_is_found(
    app: Engine, copies: AzureCopyStore, broken: Connection
) -> None:
    add_events(app, 2)
    result = copy(app, copies, NIGHT_1)
    target = result.last_seq - 1

    unguard(broken)
    broken.execute(
        text("update audit_event set details = '{\"step\": 99}' where chain_seq = :seq"),
        {"seq": target},
    )

    report = check_copies(broken, copies)
    assert [(f.kind, f.first_seq) for f in report.findings] == [
        ("chain_broken", target),
        ("changed", target),
    ]
    assert report.findings[1].message.endswith(f"differs from audit-log/{N1}.jsonl in: details")


@pytest.mark.req("NF-01")
def test_a_changed_event_is_found_even_when_the_whole_chain_was_rehashed(
    app: Engine, copies: AzureCopyStore, broken: Connection
) -> None:
    """Someone with owner rights changes an event and computes every later hash again, so the
    chain in the database checks out. The copy still shows it."""
    add_events(app, 2)
    result = copy(app, copies, NIGHT_1)
    target = result.last_seq - 1

    unguard(broken)
    broken.execute(
        text("update audit_event set details = '{\"step\": 99}' where chain_seq = :seq"),
        {"seq": target},
    )
    broken.execute(
        text(
            """
            do $$ declare e audit_event; prev bytea; begin
              select hash into prev from audit_event where chain_seq = %s - 1;
              for e in select * from audit_event where chain_seq >= %s order by chain_seq loop
                e.prev_hash := prev;
                e.hash := audit_hash(e);
                update audit_event set prev_hash = e.prev_hash, hash = e.hash
                  where chain_seq = e.chain_seq;
                prev := e.hash;
              end loop;
            end $$
            """.replace("%s", str(int(target)))
        )
    )

    assert verify_chain(broken) is None
    report = check_copies(broken, copies)
    assert {(f.kind, f.first_seq) for f in report.findings} == {
        ("changed", seq) for seq in range(target, result.last_seq + 1)
    }
    assert "details" in report.findings[0].message
    assert "hash" in report.findings[0].message


@pytest.mark.req("NF-01")
def test_a_gap_in_the_database_is_found(
    app: Engine, copies: AzureCopyStore, broken: Connection
) -> None:
    add_events(app, 3)
    result = copy(app, copies, NIGHT_1)
    middle = result.last_seq - 1

    unguard(broken)
    broken.execute(text("delete from audit_event where chain_seq = :seq"), {"seq": middle})

    report = check_copies(broken, copies)
    kinds = [(f.kind, f.first_seq, f.last_seq) for f in report.findings]
    assert ("gap", middle, middle) in kinds
    assert ("missing", middle, middle) in kinds
    assert ("chain_broken", middle + 1, middle + 1) in kinds


def events_from(app: Engine, first: int, last: int) -> list[dict[str, Any]]:
    """Events as the copy writes them, to build a copy by hand."""
    with snapshot(app) as connection:
        rows = connection.execute(
            text(
                "select * from audit_event where chain_seq between :first and :last"
                " order by chain_seq"
            ),
            {"first": first, "last": last},
        ).mappings()
        return [event_record(row) for row in rows]


@pytest.mark.req("NF-01")
def test_a_gap_between_two_copies_is_found(app: Engine, copies: AzureCopyStore) -> None:
    first = copy(app, copies, NIGHT_1)
    add_events(app, 3)
    # The next copy skips the first new event, as if that night's file had lost it.
    events = events_from(app, first.last_seq + 2, first.last_seq + 3)
    header = CopyHeader(NIGHT_2, first.last_seq + 2, first.last_seq + 3, 2, events[0]["prev_hash"])
    assert copies.write_new(name_for(NIGHT_2), encode(header, events))

    report = check(app, copies)

    assert [(f.kind, f.first_seq, f.last_seq, f.copy) for f in report.findings] == [
        ("gap", first.last_seq + 1, first.last_seq + 1, name_for(NIGHT_2)),
        ("broken_link", first.last_seq + 2, first.last_seq + 2, name_for(NIGHT_2)),
    ]
    assert report.findings[0].message == (
        f"{name_for(NIGHT_2)} starts at event {first.last_seq + 2}, "
        f"but the copies before it end at event {first.last_seq}"
    )


@pytest.mark.req("NF-01")
def test_a_copy_that_does_not_continue_the_chain_is_found(
    app: Engine, copies: AzureCopyStore
) -> None:
    first = copy(app, copies, NIGHT_1)
    add_events(app, 1)
    events = events_from(app, first.last_seq + 1, first.last_seq + 1)
    events[0]["prev_hash"] = "ab" * 32
    header = CopyHeader(NIGHT_2, first.last_seq + 1, first.last_seq + 1, 1, "ab" * 32)
    assert copies.write_new(name_for(NIGHT_2), encode(header, events))

    report = check(app, copies)

    assert [(f.kind, f.copy) for f in report.findings] == [
        ("broken_link", name_for(NIGHT_2)),
        ("changed", name_for(NIGHT_2)),
    ]
    assert report.findings[1].message.endswith("in: prev_hash")


@pytest.mark.req("NF-01")
def test_a_file_that_is_not_a_copy_is_reported(app: Engine, copies: AzureCopyStore) -> None:
    copy(app, copies, NIGHT_1)
    copies.write_new(name_for(NIGHT_2), b"not json\n")

    report = check(app, copies)

    assert [(f.kind, f.copy) for f in report.findings] == [("unreadable", name_for(NIGHT_2))]


def write_ahead(copies: AzureCopyStore, after: CopyResult, nights: list[date]) -> None:
    """Empty copies written early by someone with the storage key. Each one continues the
    chain correctly, so on their own they look like quiet nights."""
    tip = decode(after.name, copies.read(after.name)).events[-1]["hash"]
    for night in nights:
        header = CopyHeader(night, after.last_seq + 1, after.last_seq, 0, tip)
        assert copies.write_new(name_for(night), encode(header, []))


@pytest.mark.req("NF-01", "EX-01")
def test_copies_written_ahead_for_future_nights_are_found(
    app: Engine, copies: AzureCopyStore
) -> None:
    """Compliance review of PR 102: empty copies for the next 30 nights would make the job
    copy nothing and, without these checks, leave the check green."""
    first = copy(app, copies, NIGHT_1)
    ahead = [NIGHT_2 + timedelta(days=n) for n in range(32)]
    write_ahead(copies, first, ahead)
    add_events(app, 5)
    last = newest_seq(app)

    result = run_nightly(app, copies, NIGHT_3)

    assert result.copy is not None and not result.copy.written
    assert result.report is not None and not result.ok
    future = [f.copy for f in result.report.findings if f.kind == "future_copy"]
    assert future == [name_for(n) for n in ahead if n > NIGHT_3]
    not_copied = [
        (f.first_seq, f.last_seq) for f in result.report.findings if f.kind == "not_copied"
    ]
    assert not_copied == [(first.last_seq + 1, last)]
    [(reason, detail)] = open_audit_exceptions(app)
    assert reason is ExceptionReason.AUDIT_CHECK_FAILED
    assert f"{name_for(ahead[-1])} is for a night after {N3}" not in detail  # only 10 shown
    assert f"{name_for(TODAY)} is for a night after {N3}, the night being checked" in detail
    with app.connect() as connection:
        facts: dict[str, Any] = connection.execute(
            text("select facts from exception_item where id = :id"),
            {"id": result.exception_ids[0]},
        ).scalar_one()
    assert without_fingerprint(facts) == {
        "night": N3,
        "findings": {"future_copy": 30, "not_copied": 1},
    }


@pytest.mark.req("NF-01")
def test_events_left_behind_by_a_copy_written_early_are_found(
    app: Engine, copies: AzureCopyStore
) -> None:
    """Even on the night the early copy is for, so with no copy from the future, the events
    it left behind are reported, and cutting them off is still caught."""
    first = copy(app, copies, NIGHT_1)
    tomorrow = TODAY + timedelta(days=1)
    write_ahead(copies, first, [tomorrow])
    add_events(app, 2)
    last = newest_seq(app)

    report = check(app, copies, tomorrow)

    assert [(f.kind, f.first_seq, f.last_seq) for f in report.findings] == [
        ("not_copied", first.last_seq + 1, last)
    ]
    assert report.findings[0].message == (
        f"events {first.last_seq + 1} to {last} happened before the night {tomorrow}"
        " but no copy holds them"
    )


@pytest.mark.req("NF-01")
def test_events_after_the_newest_night_began_are_left_for_the_next_copy(
    app: Engine, copies: AzureCopyStore
) -> None:
    copy(app, copies, NIGHT_1)
    add_events(app, 2)

    assert check(app, copies, NIGHT_1).ok


# --- AC: a failure of the job or the check lands in the exception queue ---


class FailingStore:
    """Azurite underneath, but writing (or listing) fails, as when the storage is down."""

    def __init__(self, inner: AzureCopyStore, *, fail_names: bool = False) -> None:
        self.inner = inner
        self.fail_names = fail_names

    def names(self) -> list[str]:
        if self.fail_names:
            raise ConnectionError("storage not reachable")
        return self.inner.names()

    def read(self, name: str) -> bytes:
        return self.inner.read(name)

    def write_new(self, name: str, data: bytes) -> bool:
        raise ConnectionError("storage not reachable")


def without_fingerprint(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts apart from the fingerprint, after checking it is a plain hex digest."""
    rest = dict(facts)
    assert re.fullmatch(r"[0-9a-f]{32}", rest.pop("fingerprint"))
    return rest


def open_audit_exceptions(app: Engine) -> list[tuple[ExceptionReason, str]]:
    with app.connect() as connection:
        return [
            (item.reason, item.detail)
            for item in list_open_exceptions(connection)
            if item.reason in AUDIT_REASONS
        ]


@pytest.mark.req("NF-01", "EX-01")
def test_a_good_night_opens_no_exception(app: Engine, copies: AzureCopyStore) -> None:
    add_events(app, 1)

    result = run_nightly(app, copies, NIGHT_1)

    assert result.ok
    assert result.exception_ids == []
    assert result.copy is not None and result.copy.written
    assert open_audit_exceptions(app) == []


@pytest.mark.req("NF-01", "EX-01")
def test_a_failed_copy_lands_in_the_exception_queue_once(
    app: Engine, copies: AzureCopyStore
) -> None:
    store = FailingStore(copies)

    result = run_nightly(app, store, NIGHT_1)

    assert not result.ok
    assert result.copy is None
    assert result.report is not None and result.report.ok
    assert open_audit_exceptions(app) == [
        (
            ExceptionReason.AUDIT_COPY_FAILED,
            f"The copy of the audit log for the night {N1} could not be written. "
            "ConnectionError: storage not reachable",
        )
    ]
    # The next night fails too, before anyone looked: still one item, not two.
    again = run_nightly(app, store, NIGHT_2)
    assert again.exception_ids == result.exception_ids
    assert len(open_audit_exceptions(app)) == 1
    with app.connect() as connection:
        facts: dict[str, Any] = connection.execute(
            text("select facts from exception_item where id = :id"),
            {"id": result.exception_ids[0]},
        ).scalar_one()
    assert without_fingerprint(facts) == {"night": f"{N1}", "error": "ConnectionError"}


@pytest.mark.req("NF-01", "EX-01")
def test_a_check_that_cannot_run_lands_in_the_exception_queue(
    app: Engine, copies: AzureCopyStore
) -> None:
    result = run_nightly(app, FailingStore(copies, fail_names=True), NIGHT_1)

    assert not result.ok
    assert result.report is None
    reasons = [reason for reason, _ in open_audit_exceptions(app)]
    assert sorted(reasons) == sorted(AUDIT_REASONS)
    details = dict(open_audit_exceptions(app))
    assert details[ExceptionReason.AUDIT_CHECK_FAILED] == (
        f"The audit log check of the night {N1} could not run. "
        "ConnectionError: storage not reachable"
    )


@pytest.mark.req("NF-01", "EX-01")
def test_problems_found_by_the_check_land_in_the_exception_queue(
    app: Engine, copies: AzureCopyStore
) -> None:
    first = copy(app, copies, NIGHT_1)
    # A copy for the next night that claims a different end of the chain.
    forged = CopyHeader(NIGHT_2, first.last_seq + 1, first.last_seq, 0, "ab" * 32)
    assert copies.write_new(name_for(NIGHT_2), encode(forged, []))

    result = run_nightly(app, copies, NIGHT_3)

    assert not result.ok
    assert result.copy is not None and result.copy.written  # tonight's copy is still made
    [(reason, detail)] = open_audit_exceptions(app)
    assert reason is ExceptionReason.AUDIT_CHECK_FAILED
    assert detail == (
        f"The audit log check of the night {N3} found 2 problem(s):\n"
        f"- audit-log/{N2}.jsonl does not continue the chain where the copy before it"
        " ended\n"
        f"- audit-log/{N3}.jsonl does not continue the chain where the copy before it ended"
    )
    with app.connect() as connection:
        facts: dict[str, Any] = connection.execute(
            text("select facts from exception_item where id = :id"),
            {"id": result.exception_ids[0]},
        ).scalar_one()
    assert without_fingerprint(facts) == {"night": f"{N3}", "findings": {"broken_link": 2}}


@pytest.mark.req("NF-01", "EX-01")
def test_a_new_problem_while_an_older_one_is_open_gets_its_own_item(
    app: Engine, copies: AzureCopyStore
) -> None:
    """Review of PR 102: the same findings again keep their item, but a new finding while
    that item is open must not be lost."""
    first = copy(app, copies, NIGHT_1)
    forged = CopyHeader(NIGHT_2, first.last_seq + 1, first.last_seq, 0, "ab" * 32)
    assert copies.write_new(name_for(NIGHT_2), encode(forged, []))
    one = run_nightly(app, copies, NIGHT_3)
    again = run_nightly(app, copies, NIGHT_3)
    assert again.exception_ids == one.exception_ids
    assert len(open_audit_exceptions(app)) == 1

    copies.write_new(name_for(TODAY), b"not json\n")
    later = run_nightly(app, copies, TODAY)

    check_items = [
        detail
        for reason, detail in open_audit_exceptions(app)
        if reason is ExceptionReason.AUDIT_CHECK_FAILED
    ]
    assert len(check_items) == 2
    assert "cannot be read" not in check_items[0]
    assert f"- {name_for(TODAY)} cannot be read: JSONDecodeError" in check_items[1]
    assert set(later.exception_ids).isdisjoint(one.exception_ids)


@pytest.mark.req("NF-01", "EX-01")
def test_an_unreadable_last_copy_stops_the_copy_and_both_steps_report_it(
    app: Engine, copies: AzureCopyStore
) -> None:
    """The job cannot tell where the last copy ended, so it copies nothing rather than guess
    and leave a gap or an overlap."""
    copy(app, copies, NIGHT_1)
    copies.write_new(name_for(NIGHT_2), b"not json\n")

    result = run_nightly(app, copies, NIGHT_3)

    assert result.copy is None
    assert copies.names() == [name_for(NIGHT_1), name_for(NIGHT_2)]
    assert dict(open_audit_exceptions(app)) == {
        ExceptionReason.AUDIT_COPY_FAILED: (
            f"The copy of the audit log for the night {N3} could not be written. "
            f"CopyUnreadable: audit-log/{N2}.jsonl cannot be read: JSONDecodeError"
        ),
        ExceptionReason.AUDIT_CHECK_FAILED: (
            f"The audit log check of the night {N3} found 1 problem(s):\n"
            f"- audit-log/{N2}.jsonl cannot be read: JSONDecodeError"
        ),
    }


@pytest.mark.req("NF-01", "EX-01")
def test_the_audit_exception_is_logged_without_free_text(
    app: Engine, copies: AzureCopyStore
) -> None:
    result = run_nightly(app, FailingStore(copies), NIGHT_1)

    with app.connect() as connection:
        details: dict[str, Any] = connection.execute(
            text(
                "select details from audit_event where exception_id = :id"
                " and event_type = 'exception_opened'"
            ),
            {"id": result.exception_ids[0]},
        ).scalar_one()
    assert details["reason"] == "audit_copy_failed"
    assert without_fingerprint(details["facts"]) == {"night": N1, "error": "ConnectionError"}
    assert "storage not reachable" not in json.dumps(details)


@pytest.mark.req("EX-01")
def test_only_the_audit_reasons_may_have_no_case_email_or_statement(app: Engine) -> None:
    with pytest.raises(CaseRuleBroken), unit_of_work(app, JOB) as db:
        open_exception(db, ExceptionReason.BOOKING_ERROR, "no target")


@pytest.mark.req("NF-01")
def test_the_job_exits_with_1_when_something_failed(
    app: Engine,
    copies: AzureCopyStore,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ARGUS_DATABASE_URL", database_url)

    with caplog.at_level(logging.INFO):
        assert main(["--night", f"{N1}"], store=copies) == 0
        assert main(["--night", f"{N2}"], store=FailingStore(copies)) == 1
    # One outcome line per run, which the Azure alerts watch (#103).
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("argus-job")] == [
        "argus-job ok audit-copy",
        "argus-job failed audit-copy",
    ]

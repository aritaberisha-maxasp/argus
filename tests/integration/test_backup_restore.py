"""Backups of the case database and the restore check (#82, NF-01).

Azure Backup's long term backup is a logical backup made with ``pg_dump``, and its restore is
``pg_restore`` into a new database (docs/operations/backup-and-restore.md). These tests do the
same against the test PostgreSQL: dump the test database with events in its audit log,
restore it into a new database and run ``tools/restore_check.py`` on it. They also prove the
check notices a restore that lost or changed events, and that ``tools/deploy.py
database-owner`` hands a database owned by ``azure_pg_admin`` to the admin, because the long
term backup would skip it otherwise.

``pg_dump`` and ``pg_restore`` of PostgreSQL 16 must be on the PATH; CI has them.
"""

import importlib.util
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError

from argus.audit.copies import CopyHeader, decode, encode, event_record, name_for
from argus.cases.model import Actor
from argus.cases.store import record_event, unit_of_work
from tests.integration.conftest import head

REPO = Path(__file__).resolve().parents[2]
JOB = Actor.system("restore-test")


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restore_check = load("restore_check")
deploy = load("deploy")


def libpq_url(url: str, database: str | None = None) -> str:
    parsed = make_url(url).set(drivername="postgresql")
    if database is not None:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        pytest.fail(f"{name} is not on the PATH; the restore tests need the PostgreSQL 16 tools")
    return found


def add_events(app: Engine, count: int) -> None:
    with unit_of_work(app, JOB) as db:
        for n in range(1, count + 1):
            record_event(db, "restore_test", details={"step": n})


@pytest.fixture
def restored(owner: Engine, app: Engine, database_url: str, tmp_path: Path) -> Iterator[str]:
    """A restore of the test database into a new database, as the runbook does it: dump in
    the custom format, create an empty database, ``pg_restore --no-owner``."""
    add_events(app, 3)
    dump = tmp_path / "argus.dump"
    subprocess.run(  # noqa: S603  the PostgreSQL tools with the test database's own URL
        [tool("pg_dump"), "--format=custom", f"--file={dump}", libpq_url(database_url)],
        check=True,
    )
    name = f"argus_restore_{uuid.uuid4().hex[:8]}_test"
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'create database "{name}"'))
    try:
        subprocess.run(  # noqa: S603  the PostgreSQL tools with the test database's own URL
            [
                tool("pg_restore"),
                "--no-owner",
                "--exit-on-error",
                f"--dbname={libpq_url(database_url, name)}",
                str(dump),
            ],
            check=True,
        )
        yield make_url(database_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'drop database if exists "{name}" with (force)'))


def chain_end(engine: Engine) -> tuple[int, int, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "select count(*), max(chain_seq),"
                " (select encode(hash, 'hex') from audit_event order by chain_seq desc limit 1)"
                " from audit_event"
            )
        ).one()
    return int(row[0]), int(row[1]), str(row[2])


def nightly_copy(engine: Engine, first: int, last: int, path: Path) -> Path:
    """A nightly copy (#83) of events ``first`` to ``last``, as a file the runbook downloads."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select * from audit_event where chain_seq between :first and :last"
                " order by chain_seq"
            ),
            {"first": first, "last": last},
        ).mappings()
        events = [event_record(row) for row in rows]
        prev = connection.execute(
            text("select encode(hash, 'hex') from audit_event where chain_seq = :seq"),
            {"seq": first - 1},
        ).scalar_one_or_none()
    night = date(2026, 11, 1)
    header = CopyHeader(night, first, last, len(events), prev or "00")
    target = path / name_for(night).removeprefix("audit-log/")
    target.write_bytes(encode(header, events))
    return target


@pytest.mark.req("NF-01")
def test_a_restored_database_keeps_the_whole_audit_chain(
    owner: Engine, app: Engine, restored: str, tmp_path: Path
) -> None:
    engine = create_engine(restored)
    try:
        report = restore_check.check_restore(engine)
        count, last, newest = chain_end(owner)
        assert report.ok, report.problems
        assert report.broken_at is None
        assert (report.event_count, report.last_seq, report.newest_hash) == (count, last, newest)
        assert report.revision == head() == report.newest_revision
        assert "restore check: passed" in report.lines()
        # The restored database still refuses changes to the audit log, even from its owner:
        # the triggers came back with the dump, after the data, so they did not rewrite the
        # restored events either.
        with engine.begin() as connection, pytest.raises(DBAPIError, match=r"append only"):
            connection.execute(text("delete from audit_event"))
    finally:
        engine.dispose()


@pytest.mark.req("NF-01")
def test_events_newer_than_the_backup_show_against_the_nightly_copy(
    owner: Engine, app: Engine, restored: str, tmp_path: Path
) -> None:
    _, last_in_backup, _ = chain_end(create_engine(restored))
    add_events(app, 2)  # written after the backup was taken
    _, last_now, _ = chain_end(owner)
    copy = nightly_copy(owner, last_in_backup - 2, last_now, tmp_path)

    engine = create_engine(restored)
    try:
        audit_copy = decode(f"audit-log/{copy.name}", copy.read_bytes())
        report = restore_check.check_restore(engine, [audit_copy])
    finally:
        engine.dispose()
    assert report.ok, report.problems
    assert report.copies_checked == 1
    assert report.copy_events_matched == 3
    assert report.copy_events_after_backup == last_now - last_in_backup == 2


@pytest.mark.req("NF-01")
def test_a_restore_with_a_changed_event_fails_the_check(owner: Engine, restored: str) -> None:
    engine = create_engine(restored)
    try:
        _, last, _ = chain_end(engine)
        with engine.begin() as connection:
            connection.execute(text("alter table audit_event disable trigger audit_no_change"))
            connection.execute(
                text("update audit_event set details = '{}'::jsonb where chain_seq = :seq"),
                {"seq": last - 1},
            )
        report = restore_check.check_restore(engine)
    finally:
        engine.dispose()
    assert not report.ok
    assert report.broken_at == last - 1
    assert "restore check: FAILED" in report.lines()


@pytest.mark.req("NF-01")
def test_a_restore_without_its_newest_events_shows_them_as_newer_than_the_backup(
    restored: str, tmp_path: Path
) -> None:
    """A restore that lost the newest events looks like an older backup: no later hash points
    to them. The nightly copy is what tells how many events are not in the restore."""
    engine = create_engine(restored)
    try:
        _, last, _ = chain_end(engine)
        copy = nightly_copy(engine, last - 2, last, tmp_path)
        audit_copy = decode(f"audit-log/{copy.name}", copy.read_bytes())
        with engine.begin() as connection:
            connection.execute(text("alter table audit_event disable trigger audit_no_change"))
            connection.execute(
                text("delete from audit_event where chain_seq = :seq"), {"seq": last}
            )
        report = restore_check.check_restore(engine, [audit_copy])
    finally:
        engine.dispose()
    assert report.ok, report.problems
    assert report.last_seq == last - 1
    assert (report.copy_events_matched, report.copy_events_after_backup) == (2, 1)


@pytest.mark.req("NF-01")
def test_the_command_prints_the_record_and_fails_on_a_broken_chain(
    restored: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ARGUS_DATABASE_URL", restored)
    engine = create_engine(restored)
    try:
        _, last, _ = chain_end(engine)
        copy = nightly_copy(engine, last - 1, last, tmp_path)
        assert restore_check.main(["--audit-copy", str(copy)]) == 0
        out = capsys.readouterr().out
        assert f"last chain number {last}" in out
        assert "hash chain: whole" in out
        assert "nightly copies: 1 read, 2 events match" in out
        assert "restore check: passed" in out
        # Only numbers, times and hashes: no event content goes into the record (NF-03).
        assert "restore_test" not in out and "step" not in out

        with engine.begin() as connection:
            connection.execute(text("alter table audit_event disable trigger audit_no_change"))
            connection.execute(
                text("update audit_event set details = '{}'::jsonb where chain_seq = :seq"),
                {"seq": last},
            )
        assert restore_check.main(["--audit-copy", str(copy)]) == 1
        out = capsys.readouterr().out
        assert f"hash chain: broken at event {last}" in out
        assert "restore check: FAILED" in out
    finally:
        engine.dispose()


@pytest.mark.req("NF-01")
def test_the_command_refuses_a_file_that_is_not_a_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "2026-11-01.jsonl"
    bad.write_text("not a copy\n", encoding="utf-8")
    assert restore_check.main(["--audit-copy", str(bad)]) == 1
    assert "cannot read" in capsys.readouterr().out


@pytest.mark.req("NF-01")
def test_a_gap_in_the_restored_chain_fails_the_check(restored: str, tmp_path: Path) -> None:
    engine = create_engine(restored)
    try:
        _, last, _ = chain_end(engine)
        copy = nightly_copy(engine, last - 2, last, tmp_path)
        audit_copy = decode(f"audit-log/{copy.name}", copy.read_bytes())
        with engine.begin() as connection:
            connection.execute(text("alter table audit_event disable trigger audit_no_change"))
            connection.execute(
                text("delete from audit_event where chain_seq = :seq"), {"seq": last - 1}
            )
        report = restore_check.check_restore(engine, [audit_copy])
    finally:
        engine.dispose()
    assert not report.ok
    assert report.broken_at == last
    assert "1 events are missing from the chain" in report.problems
    assert f"{copy.name}" in " ".join(report.problems)


@pytest.fixture
def azure_owned_database(owner: Engine, database_url: str) -> Iterator[str]:
    """A database owned by azure_pg_admin, as Azure may create it from the templates."""
    name = f"argus_owner_{uuid.uuid4().hex[:8]}_test"
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        created_role = not connection.execute(
            text("select 1 from pg_roles where rolname = 'azure_pg_admin'")
        ).first()
        if created_role:
            connection.execute(text("create role azure_pg_admin nologin"))
        connection.execute(text(f'create database "{name}" owner azure_pg_admin'))
    try:
        yield make_url(database_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'drop database if exists "{name}" with (force)'))
            if created_role:
                connection.execute(text("drop role azure_pg_admin"))


@pytest.mark.req("NF-01")
def test_a_database_owned_by_azure_pg_admin_goes_to_the_admin(
    owner: Engine, azure_owned_database: str
) -> None:
    engine = create_engine(azure_owned_database)
    try:
        with engine.connect() as connection:
            admin: str = connection.execute(text("select current_user")).scalar_one()
        assert deploy.ensure_backup_owner(engine) == ("azure_pg_admin", admin)
        # Every deploy runs it again and then keeps the owner.
        assert deploy.ensure_backup_owner(engine) == (admin, admin)
    finally:
        engine.dispose()


@pytest.mark.req("NF-01")
def test_a_database_with_another_owner_is_left_alone(owner: Engine) -> None:
    with owner.connect() as connection:
        before: str = connection.execute(
            text(
                "select pg_get_userbyid(datdba) from pg_database where datname = current_database()"
            )
        ).scalar_one()
    assert deploy.ensure_backup_owner(owner) == (before, before)


@pytest.mark.req("NF-01")
def test_an_event_with_another_hash_than_its_nightly_copy_fails_the_check(
    restored: str, tmp_path: Path
) -> None:
    """A chain that was rewritten and hashed again looks whole on its own. The nightly copy,
    kept in the locked store, still has the old hash."""
    engine = create_engine(restored)
    try:
        _, last, _ = chain_end(engine)
        copy = nightly_copy(engine, last - 1, last, tmp_path)
        audit_copy = decode(f"audit-log/{copy.name}", copy.read_bytes())
        audit_copy.events[0]["hash"] = "ab" * 32
        report = restore_check.check_restore(engine, [audit_copy])
    finally:
        engine.dispose()
    assert not report.ok
    assert f"event {last - 1} has another hash in the restore" in " ".join(report.problems)

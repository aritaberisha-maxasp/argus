"""Check a restored copy of the ARGUS database (#82, NF-01).

Run it against the restored database, never against production:

    ARGUS_DATABASE_URL=postgresql+psycopg://<host>:5432/<restored database> \\
        python tools/restore_check.py [--audit-copy audit-log/<YYYY-MM-DD>.jsonl ...]

It reads, it never writes. It reports:

- the migration the restored database is on, next to the newest one in the code;
- how many audit events it holds and when the newest one happened;
- whether ``audit_verify()`` finds the hash chain whole, and whether the chain has gaps;
- with ``--audit-copy``: every event of those nightly copies (#83) that the restored
  database holds must have exactly the same hash. Events in a copy that are newer than the
  restore are counted as "after the backup", which shows how much the restore lost.

It prints counts, sequence numbers, times and hashes only, never the content of an event, so
the output may go into the restore record (NF-03). Exit code 0 when the chain is whole and
every copied event matches, 1 otherwise.

Uses ``ARGUS_DATABASE_URL``, ``PGUSER`` and ``PGPASSWORD`` like the migrations do, so in Azure
it signs in with an Entra token (docs/operations/backup-and-restore.md).
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text

from argus.audit.copies import AuditCopy, CopyUnreadable, decode
from argus.db import engine_from_env

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@dataclass
class RestoreReport:
    revision: str
    newest_revision: str
    event_count: int
    last_seq: int
    newest_event_at: str | None
    newest_hash: str | None
    broken_at: int | None
    copies_checked: int = 0
    copy_events_matched: int = 0
    copy_events_after_backup: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def lines(self) -> list[str]:
        revision_note = (
            "the newest" if self.revision == self.newest_revision else "older than the code"
        )
        lines = [
            f"migration: {self.revision} ({revision_note}, code is at {self.newest_revision})",
            f"audit events: {self.event_count}, last chain number {self.last_seq}",
            f"newest event: {self.newest_event_at or 'none'}",
            f"newest hash: {self.newest_hash or 'none'}",
            "hash chain: "
            + ("whole" if self.broken_at is None else f"broken at event {self.broken_at}"),
        ]
        if self.copies_checked:
            lines.append(
                f"nightly copies: {self.copies_checked} read, {self.copy_events_matched} events "
                f"match, {self.copy_events_after_backup} events are newer than the backup"
            )
        lines += [f"problem: {problem}" for problem in self.problems]
        lines.append("restore check: " + ("passed" if self.ok else "FAILED"))
        return lines


def newest_revision() -> str:
    head = ScriptDirectory.from_config(AlembicConfig(str(ALEMBIC_INI))).get_current_head()
    return head or "none"


def _hex(value: bytes | memoryview | None) -> str | None:
    return None if value is None else bytes(value).hex()


def _compare_copy(connection: Connection, copy: AuditCopy, report: RestoreReport) -> None:
    report.copies_checked += 1
    held = [seq for seq in (int(e["chain_seq"]) for e in copy.events) if seq <= report.last_seq]
    # One query per copy, not per event: a year of copies against a remote server stays quick.
    rows = connection.execute(
        text("select chain_seq, hash from audit_event where chain_seq = any(:seqs)"),
        {"seqs": held},
    ).all()
    stored: dict[int, str | None] = {row[0]: _hex(row[1]) for row in rows}
    for event in copy.events:
        seq = int(event["chain_seq"])
        if seq > report.last_seq:
            report.copy_events_after_backup += 1
        elif seq not in stored:
            report.problems.append(f"{copy.name}: event {seq} is missing in the restore")
        elif stored[seq] != event["hash"]:
            report.problems.append(f"{copy.name}: event {seq} has another hash in the restore")
        else:
            report.copy_events_matched += 1


def check_restore(
    engine: Engine,
    copies: Sequence[AuditCopy] = (),
    head: Callable[[], str] = newest_revision,
) -> RestoreReport:
    with engine.connect() as connection:
        revision = connection.execute(
            text("select version_num from alembic_version")
        ).scalar_one_or_none()
        row = connection.execute(
            text(
                "select count(*), coalesce(max(chain_seq), 0),"
                " to_char(max(occurred_at) at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
                " from audit_event"
            )
        ).one()
        count: int = row[0]
        last_seq: int = row[1]
        newest_at: str | None = row[2]
        newest_hash = connection.execute(
            text("select hash from audit_event order by chain_seq desc limit 1")
        ).scalar_one_or_none()
        broken_at = connection.execute(
            text("select broken_at from audit_verify()")
        ).scalar_one_or_none()
        report = RestoreReport(
            revision=str(revision or "none"),
            newest_revision=head(),
            event_count=int(count),
            last_seq=int(last_seq),
            newest_event_at=newest_at,
            newest_hash=_hex(newest_hash),
            broken_at=broken_at,
        )
        if broken_at is not None:
            report.problems.append(f"audit_verify() finds the chain broken at event {broken_at}")
        if report.event_count != report.last_seq:
            # The chain numbers run 1, 2, 3 without gaps, so a missing event shows here even
            # when it was the newest one and no later hash points to it.
            report.problems.append(
                f"{report.last_seq - report.event_count} events are missing from the chain"
            )
        for copy in copies:
            _compare_copy(connection, copy, report)
        connection.rollback()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--audit-copy",
        action="append",
        default=[],
        type=Path,
        help="a nightly audit copy (audit-log/<YYYY-MM-DD>.jsonl) to compare with",
    )
    args = parser.parse_args(argv)

    copies = []
    for path in args.audit_copy:
        name = f"audit-log/{path.name}"
        try:
            copies.append(decode(name, path.read_bytes()))
        except (OSError, CopyUnreadable) as error:
            print(f"cannot read {path}: {error}")
            return 1

    report = check_restore(engine_from_env(), copies)
    for line in report.lines():
        print(line)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

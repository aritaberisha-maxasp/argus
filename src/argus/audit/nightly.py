"""Copy the new audit events every night, then compare the database with all copies.

Covers NF-01 and EX-01 (issue #83). The job only reads the database, in one read only snapshot
per step, and takes no lock: the audit lock is only taken by writes. It writes to the database
only to put a failure into the exception queue.

Why both steps: :func:`argus.cases.store.verify_chain` finds an old event that was changed,
but a chain whose newest events were deleted still looks whole. The copies cannot be changed or
deleted, so comparing with them shows any event that was copied and is now gone or different.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, Engine, text

from argus.audit.copies import (
    EVENT_COLUMNS,
    GENESIS_HASH,
    AuditCopy,
    AzureCopyStore,
    CopyHeader,
    CopyStore,
    CopyUnreadable,
    decode,
    encode,
    event_record,
    name_for,
    night_of,
)
from argus.cases.model import Actor, ExceptionReason
from argus.cases.store import DuplicateRefused, open_exception, unit_of_work, verify_chain
from argus.connections import demo_from_env
from argus.db import engine_from_env
from argus.db.mode import check_database_for_mode
from argus.logsafe import log_outcome, use_safe_logging
from argus.secrets import EnvironmentSecretStore
from argus.working_days import BERLIN, berlin_date

log = logging.getLogger("argus.audit")

ACTOR = Actor.system("audit-copy")
# The name in the job's outcome line, ``argus-job ok audit-copy`` or ``argus-job failed
# audit-copy``, which the Azure alerts watch (#103, infra/modules/alerts.bicep).
JOB = "audit-copy"
_COLUMNS = ", ".join(EVENT_COLUMNS)


class CopyRefused(Exception):
    """The copy for this night cannot be written, for example because a later night is already
    copied. Writing it would put the copies out of order."""


@dataclass(frozen=True)
class CopyResult:
    name: str
    first_seq: int
    last_seq: int
    written: bool
    """False when the copy for this night was already there; nothing was written then."""

    @property
    def event_count(self) -> int:
        return self.last_seq - self.first_seq + 1


@dataclass(frozen=True)
class Finding:
    """One thing the check found. ``first_seq`` to ``last_seq`` is the range of chain numbers
    it is about, when it is about events."""

    kind: str
    """``missing``, ``changed``, ``gap``, ``broken_link``, ``chain_broken``, ``unreadable``,
    ``future_copy`` or ``not_copied``."""
    message: str
    first_seq: int | None = None
    last_seq: int | None = None
    copy: str | None = None


@dataclass(frozen=True)
class CheckReport:
    copies: int
    events_checked: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass(frozen=True)
class NightlyResult:
    copy: CopyResult | None
    report: CheckReport | None
    exception_ids: list[int]

    @property
    def ok(self) -> bool:
        return self.copy is not None and self.report is not None and self.report.ok


@contextmanager
def snapshot(engine: Engine) -> Iterator[Connection]:
    """One read only transaction that sees the database as it was when it started."""
    with engine.connect() as connection:
        connection = connection.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        with connection.begin():
            yield connection


def _events(connection: Connection, first_seq: int, last_seq: int | None) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            f"select {_COLUMNS} from audit_event"  # noqa: S608 (fixed column list)
            " where chain_seq >= :first and (cast(:last as bigint) is null or chain_seq <= :last)"
            " order by chain_seq"
        ),
        {"first": first_seq, "last": last_seq},
    ).mappings()
    return [event_record(row) for row in rows]


def _hash_of(connection: Connection, chain_seq: int) -> str | None:
    if chain_seq == 0:
        return GENESIS_HASH
    value = connection.execute(
        text("select hash from audit_event where chain_seq = :seq"), {"seq": chain_seq}
    ).scalar()
    return bytes(value).hex() if value is not None else None


def _copy_nights(store: CopyStore) -> dict[date, str]:
    nights: dict[date, str] = {}
    for name in store.names():
        night = night_of(name)
        if night is not None:
            nights[night] = name
    return nights


def _today() -> date:
    return berlin_date(datetime.now(BERLIN))


def write_copy(
    connection: Connection, store: CopyStore, night: date, *, today: date | None = None
) -> CopyResult:
    """Write every audit event since the last copy into the copy for ``night``.

    Run it inside :func:`snapshot`. Running it again for the same night writes nothing and
    gives back the copy that is there, so a retry can create neither a second copy nor a gap.
    The next night starts right after the last event of that copy.

    A night after ``today`` (Berlin) is refused before anything is written: the locked copy
    could never be removed, and every later night would be refused (review of PR 102).
    """
    today = today or _today()
    if night > today:
        raise CopyRefused(f"the night {night} is after today ({today}) and cannot be copied yet")
    nights = _copy_nights(store)
    if night in nights:
        header = decode(nights[night], store.read(nights[night])).header
        return CopyResult(nights[night], header.first_seq, header.last_seq, written=False)
    later = [n for n in nights if n > night]
    if later:
        raise CopyRefused(f"the night {max(later)} is already copied, so {night} cannot follow")

    after = 0
    if nights:
        newest = nights[max(nights)]
        after = decode(newest, store.read(newest)).header.last_seq
        in_database: int = connection.execute(
            text("select coalesce(max(chain_seq), 0) from audit_event")
        ).scalar_one()
        if after > in_database:
            # Starting there would leave every event up to it uncopied, for good.
            raise CopyRefused(
                f"{newest} ends at event {after}, but the database ends at event {in_database}"
            )
    events = _events(connection, after + 1, None)
    first_seq = after + 1
    last_seq = events[-1]["chain_seq"] if events else after
    # The previous hash comes from the database, as it is now. If the event is gone, the copy
    # still keeps tonight's events; the check reports the missing one.
    header = CopyHeader(
        night=night,
        first_seq=first_seq,
        last_seq=last_seq,
        event_count=len(events),
        prev_hash=_hash_of(connection, after) or "",
    )
    name = name_for(night)
    written = store.write_new(name, encode(header, events))
    if not written:
        # Another run wrote this night in the meantime. Its copy counts.
        header = decode(name, store.read(name)).header
        return CopyResult(name, header.first_seq, header.last_seq, written=False)
    log.info("Copied audit events %s to %s into %s", first_seq, last_seq, name)
    return CopyResult(name, first_seq, last_seq, written=True)


def _ranges(numbers: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for number in numbers:
        if ranges and ranges[-1][1] == number - 1:
            ranges[-1] = (ranges[-1][0], number)
        else:
            ranges.append((number, number))
    return ranges


def _span(first: int, last: int) -> str:
    return f"event {first}" if first == last else f"events {first} to {last}"


def _database_gaps(connection: Connection) -> list[Finding]:
    rows = connection.execute(
        text(
            "select prev + 1 as first_missing, chain_seq - 1 as last_missing from ("
            " select chain_seq, coalesce(lag(chain_seq) over (order by chain_seq), 0) as prev"
            " from audit_event) s where chain_seq <> prev + 1 order by chain_seq"
        )
    ).all()
    return [
        Finding(
            "gap",
            f"the database has no {_span(first, last)}",
            first_seq=first,
            last_seq=last,
        )
        for first, last in rows
        if isinstance(first, int) and isinstance(last, int)
    ]


def _check_one(
    connection: Connection, copy: AuditCopy, expected_first: int, tip_hash: str | None
) -> list[Finding]:
    findings: list[Finding] = []
    header = copy.header
    if header.first_seq != expected_first:
        findings.append(
            Finding(
                "gap",
                f"{copy.name} starts at event {header.first_seq}, "
                f"but the copies before it end at event {expected_first - 1}",
                first_seq=min(expected_first, header.first_seq),
                last_seq=max(expected_first, header.first_seq) - 1,
                copy=copy.name,
            )
        )
    seqs = [event.get("chain_seq") for event in copy.events]
    if seqs != list(range(header.first_seq, header.last_seq + 1)) or header.event_count != len(
        seqs
    ):
        findings.append(
            Finding(
                "gap",
                f"{copy.name} does not hold exactly events {header.first_seq} to "
                f"{header.last_seq} in order",
                first_seq=header.first_seq,
                last_seq=header.last_seq,
                copy=copy.name,
            )
        )
    # Each event names the hash of the one before it, across the files too.
    previous: object = header.prev_hash
    if tip_hash is not None and previous != tip_hash:
        findings.append(
            Finding(
                "broken_link",
                f"{copy.name} does not continue the chain where the copy before it ended",
                first_seq=header.first_seq,
                last_seq=header.first_seq,
                copy=copy.name,
            )
        )
    for event in copy.events:
        if event.get("prev_hash") != previous:
            seq = event.get("chain_seq")
            findings.append(
                Finding(
                    "broken_link",
                    f"in {copy.name}, event {seq} does not follow the event before it",
                    first_seq=seq if isinstance(seq, int) else None,
                    last_seq=seq if isinstance(seq, int) else None,
                    copy=copy.name,
                )
            )
        previous = event.get("hash")

    numbers = [seq for seq in seqs if isinstance(seq, int)]
    if not numbers:
        return findings
    in_database = {row["chain_seq"]: row for row in _events(connection, min(numbers), max(numbers))}
    missing: list[int] = []
    for event in copy.events:
        seq = event.get("chain_seq")
        row = in_database.get(seq) if isinstance(seq, int) else None
        if row is None:
            if isinstance(seq, int):
                missing.append(seq)
            continue
        changed = sorted(c for c in set(row) | set(event) if row.get(c) != event.get(c))
        if changed:
            findings.append(
                Finding(
                    "changed",
                    f"event {seq} in the database differs from {copy.name} in: "
                    + ", ".join(changed),
                    first_seq=seq,
                    last_seq=seq,
                    copy=copy.name,
                )
            )
    for first, last in _ranges(missing):
        findings.append(
            Finding(
                "missing",
                f"{_span(first, last)} from {copy.name} "
                + ("is" if first == last else "are")
                + " missing in the database",
                first_seq=first,
                last_seq=last,
                copy=copy.name,
            )
        )
    return findings


def _not_copied(connection: Connection, last_copied: int, newest_night: date) -> list[Finding]:
    """Events that happened before the newest copy's night began (Berlin time) but that no
    copy holds. The job copies everything up to the moment it runs, so such an event means a
    copy was written early and left it behind (compliance review of PR 102)."""
    cutoff = datetime.combine(newest_night, time(0), tzinfo=BERLIN)
    numbers: list[int] = list(
        connection.execute(
            text(
                "select chain_seq from audit_event"
                " where chain_seq > :last and occurred_at < :cutoff order by chain_seq"
            ),
            {"last": last_copied, "cutoff": cutoff},
        ).scalars()
    )
    return [
        Finding(
            "not_copied",
            f"{_span(first, last)} happened before the night {newest_night} "
            "but no copy holds " + ("it" if first == last else "them"),
            first_seq=first,
            last_seq=last,
        )
        for first, last in _ranges(numbers)
    ]


def check_copies(
    connection: Connection, store: CopyStore, night: date | None = None
) -> CheckReport:
    """Compare the database with every copy, oldest night first. Run it inside
    :func:`snapshot`. ``night`` is the night the job runs for; a copy for a later night
    is reported, because nobody can have written it honestly.

    Reports an event that was copied and is now missing from the database or different
    there, a gap in the chain numbers (between copies, inside a copy or in the database), a
    copy that does not continue the chain of the one before, a file that cannot be read, the
    first broken link :func:`verify_chain` finds, a copy from the future, and an event older
    than the newest copy's night that no copy holds. Events newer than that are only checked
    by the chain; the next copy takes them.
    """
    findings: list[Finding] = []
    broken = verify_chain(connection)
    if broken is not None:
        findings.append(
            Finding(
                "chain_broken",
                f"the hash chain in the database breaks at event {broken}",
                first_seq=broken,
                last_seq=broken,
            )
        )
    findings += _database_gaps(connection)

    nights = _copy_nights(store)
    if night is not None:
        findings += [
            Finding(
                "future_copy",
                f"{nights[later]} is for a night after {night}, the night being checked",
                copy=nights[later],
            )
            for later in sorted(nights)
            if later > night
        ]
    expected_first = 1
    tip_hash: str | None = GENESIS_HASH
    checked = 0
    for copy_night in sorted(nights):
        name = nights[copy_night]
        try:
            copy = decode(name, store.read(name))
        except CopyUnreadable as error:
            findings.append(Finding("unreadable", str(error), copy=name))
            tip_hash = None  # the link into the next copy cannot be checked
            continue
        findings += _check_one(connection, copy, expected_first, tip_hash)
        checked += len(copy.events)
        expected_first = copy.header.last_seq + 1
        tip_hash = copy.events[-1].get("hash") if copy.events else copy.header.prev_hash
    if nights:
        findings += _not_copied(connection, expected_first - 1, max(nights))
    return CheckReport(copies=len(nights), events_checked=checked, findings=findings)


def fingerprint_of(what: Any) -> str:
    """A short fingerprint of what went wrong, so the same problem found again keeps its open
    exception, and a different one gets its own (review of PR 102)."""
    data = json.dumps(what, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:32]


def _report_problem(
    engine: Engine, reason: ExceptionReason, detail: str, facts: dict[str, Any]
) -> int:
    """Put the failure into the exception queue. If the same problem (same reason and
    fingerprint in ``facts``) is still open, a person has not looked yet: keep that one
    instead of adding another. A different problem always gets its own item."""
    try:
        with unit_of_work(engine, ACTOR) as db:
            return open_exception(db, reason, detail, facts=facts)
    except DuplicateRefused:
        with engine.connect() as connection:
            open_id: int = connection.execute(
                text(
                    "select id from exception_item where reason = :reason and resolved_at is null"
                    " and case_id is null and email_id is null and statement_id is null"
                    " and facts->>'fingerprint' = :fingerprint"
                ),
                {"reason": reason.value, "fingerprint": facts["fingerprint"]},
            ).scalar_one()
        log.info("Exception %s about the audit copy is still open", open_id)
        return open_id


def _first_line(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    return f"{type(error).__name__}: {lines[0]}" if lines else type(error).__name__


def _check_detail(night: date, report: CheckReport) -> str:
    shown = report.findings[:10]
    lines = [f"The audit log check of the night {night} found {len(report.findings)} problem(s):"]
    lines += [f"- {finding.message}" for finding in shown]
    if len(report.findings) > len(shown):
        lines.append(f"- and {len(report.findings) - len(shown)} more")
    return "\n".join(lines)


def run_nightly(engine: Engine, store: CopyStore, night: date) -> NightlyResult:
    """Copy the new events for ``night``, then check every copy. Each failure opens an
    exception with a clear reason (EX-01). The check also runs when the copy failed."""
    exception_ids: list[int] = []
    copy: CopyResult | None = None
    report: CheckReport | None = None
    try:
        with snapshot(engine) as connection:
            copy = write_copy(connection, store, night)
    except Exception as error:
        log.exception("The nightly audit copy failed")
        exception_ids.append(
            _report_problem(
                engine,
                ExceptionReason.AUDIT_COPY_FAILED,
                f"The copy of the audit log for the night {night} could not be written. "
                f"{_first_line(error)}",
                {
                    "night": night.isoformat(),
                    "error": type(error).__name__,
                    "fingerprint": fingerprint_of(_first_line(error)),
                },
            )
        )
    try:
        with snapshot(engine) as connection:
            report = check_copies(connection, store, night)
    except Exception as error:
        log.exception("The audit copy check could not run")
        exception_ids.append(
            _report_problem(
                engine,
                ExceptionReason.AUDIT_CHECK_FAILED,
                f"The audit log check of the night {night} could not run. {_first_line(error)}",
                {
                    "night": night.isoformat(),
                    "error": type(error).__name__,
                    "fingerprint": fingerprint_of(_first_line(error)),
                },
            )
        )
    else:
        if not report.ok:
            kinds: dict[str, int] = {}
            for finding in report.findings:
                kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
            exception_ids.append(
                _report_problem(
                    engine,
                    ExceptionReason.AUDIT_CHECK_FAILED,
                    _check_detail(night, report),
                    {
                        "night": night.isoformat(),
                        "findings": kinds,
                        "fingerprint": fingerprint_of(
                            [(f.kind, f.first_seq, f.last_seq, f.copy) for f in report.findings]
                        ),
                    },
                )
            )
    return NightlyResult(copy, report, exception_ids)


def main(argv: list[str] | None = None, store: CopyStore | None = None) -> int:
    """Entry point of the nightly job: ``python -m argus.audit [--night YYYY-MM-DD]``.

    The database comes from ``ARGUS_DATABASE_URL`` and the storage from the document store
    secret, both set by the environment (Key Vault in Azure). Exits with 1 when anything
    failed. Normally the exception is in the queue by then, but when the database cannot be
    reached it cannot be, so every run also ends with one outcome line that the Azure alerts
    watch: ``argus-job ok audit-copy`` or ``argus-job failed audit-copy`` (#103). Errors are
    logged as their type and codes only, never their text (NF-03).
    """
    parser = argparse.ArgumentParser(
        prog="python -m argus.audit",
        description="Copy the new audit events into the locked storage, then check all copies.",
    )
    parser.add_argument("--night", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    use_safe_logging()
    # One line per storage call would fill the log; failures still show.
    logging.getLogger("azure").setLevel(logging.WARNING)
    night: date = args.night or berlin_date(datetime.now().astimezone())
    ok = False
    try:
        # Before anything is written: demo audit events must never reach the locked store,
        # and real ones never the demo store that every reset empties (ADR 0009, compliance
        # review of PR 157). A mismatch fails the run like any other error.
        check_database_for_mode(os.environ, demo=demo_from_env(os.environ))
        engine = engine_from_env()
        try:
            store = store or AzureCopyStore.from_secrets(EnvironmentSecretStore())
            result = run_nightly(engine, store, night)
        finally:
            engine.dispose()
        if result.copy is not None:
            log.info("Copy %s holds %s event(s)", result.copy.name, result.copy.event_count)
        if result.report is not None:
            log.info(
                "Checked %s copies with %s events: %s problem(s)",
                result.report.copies,
                result.report.events_checked,
                len(result.report.findings),
            )
        ok = result.ok
    except Exception:
        # For example the database cannot be reached, so not even the exception could be
        # written. The failed line is then the only sign (review of PR 102, on #103).
        log.exception("The nightly audit copy could not finish")
    log_outcome(log, JOB, ok)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

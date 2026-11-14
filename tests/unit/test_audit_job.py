"""The nightly audit copy as a scheduled job: outcome line and exit code (#103, NF-01, NF-03).

The copy and the check themselves are tested in tests/integration/test_audit_copy.py. Here the
job around them is tested: every run ends with the one line the Azure alerts look for, also
when the database cannot be reached and so no exception can be put in the queue, and errors
are logged without their text.
"""

import logging
from datetime import date
from typing import Any

import pytest

from argus.audit import nightly
from argus.audit.nightly import CheckReport, CopyResult, Finding, NightlyResult, main
from argus.logsafe import FAILED_MARK, OK_MARK, SafeFormatter

NIGHT = "2026-11-03"


class MemoryStore:
    """An in memory copy store; the job here never gets as far as writing."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def names(self) -> list[str]:
        return sorted(self.files)

    def read(self, name: str) -> bytes:
        return self.files[name]

    def write_new(self, name: str, data: bytes) -> bool:
        if name in self.files:
            return False
        self.files[name] = data
        return True


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


def outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("argus-job")]


def safe_text(caplog: pytest.LogCaptureFixture) -> str:
    """The log as the job writes it in Azure, through :class:`SafeFormatter`."""
    formatter = SafeFormatter("%(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


def fake_run(result: NightlyResult | BaseException) -> Any:
    def run(engine: Any, store: Any, night: date) -> NightlyResult:
        assert night == date.fromisoformat(NIGHT)
        if isinstance(result, BaseException):
            raise result
        return result

    return run


COPY = CopyResult("audit-log/2026-11-03.jsonl", 1, 5, written=True)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine()
    monkeypatch.setattr(nightly, "engine_from_env", lambda: fake)
    return fake


@pytest.mark.req("NF-01")
def test_a_clean_night_logs_ok_and_exits_0(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clean = NightlyResult(COPY, CheckReport(copies=1, events_checked=5), [])
    monkeypatch.setattr(nightly, "run_nightly", fake_run(clean))
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 0
    assert outcome_lines(caplog) == [f"{OK_MARK} audit-copy"]
    assert engine.disposed == 1


@pytest.mark.req("NF-01", "EX-01")
def test_a_night_with_findings_logs_failed_and_exits_1(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    report = CheckReport(copies=1, events_checked=5, findings=[Finding("missing", "gone")])
    monkeypatch.setattr(nightly, "run_nightly", fake_run(NightlyResult(COPY, report, [7])))
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} audit-copy"]


@pytest.mark.req("NF-01")
def test_a_failed_copy_logs_failed_and_exits_1(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    report = CheckReport(copies=0, events_checked=0)
    monkeypatch.setattr(nightly, "run_nightly", fake_run(NightlyResult(None, report, [8])))
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} audit-copy"]


@pytest.mark.req("NF-01", "NF-03")
def test_a_run_that_breaks_off_logs_failed_without_the_error_text(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The exception could not be written, so the outcome line is the only sign.
    down = ConnectionError("could not reach psql-argus as id-argus-app")
    monkeypatch.setattr(nightly, "run_nightly", fake_run(down))
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} audit-copy"]
    assert engine.disposed == 1
    text = safe_text(caplog)
    assert "error: ConnectionError" in text
    assert "psql-argus" not in text
    assert "Traceback" not in text


@pytest.mark.req("NF-01")
def test_a_missing_database_setting_logs_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("ARGUS_DATABASE_URL", raising=False)
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} audit-copy"]


@pytest.mark.req("NF-01", "NF-03")
def test_an_unreachable_database_logs_failed_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Nothing listens on port 1: neither the copy nor the exception about it can be written.
    monkeypatch.setenv(
        "ARGUS_DATABASE_URL", "postgresql+psycopg://argus_app:s3cret-pw@127.0.0.1:1/argus"
    )
    monkeypatch.delenv("ARGUS_DATABASE_AUTH", raising=False)
    with caplog.at_level(logging.INFO):
        assert main(["--night", NIGHT], store=MemoryStore()) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} audit-copy"]
    text = safe_text(caplog)
    assert "error: OperationalError" in text
    assert "s3cret-pw" not in text
    assert "Traceback" not in text

"""Run the acceptance tests AC-1 to AC-8 and print pass, fail or not run for each AC (#66).

Run from the repository root before every sprint review:
``python tools/acceptance.py``

The tests are in ``tests/acceptance`` and need the stand ins of the integration tests: set
``ARGUS_TEST_DATABASE_URL`` and ``ARGUS_TEST_BLOB_CONNECTION_STRING`` as the README shows.
Without them the scenarios that need them show as not run.

An AC passes only when every one of its checks ran and passed. A check whose feature is not
built yet (``@pytest.mark.waits_on``), or that was skipped for any other reason, is not run, and
so is its AC. Nothing that did not run ever counts as passed.

``--markdown FILE`` also appends the result as Markdown to FILE, for the sprint review or the CI
job summary. Anything after ``--`` goes to pytest. Exits 1 when a check fails.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "docs" / "requirements" / "requirements.md"
TESTS = REPO / "tests" / "acceptance"
AC_LINE = re.compile(r"^- \*\*(AC-\d+)\*\* (.+)$", re.MULTILINE)


class Result(StrEnum):
    PASSED = "pass"
    FAILED = "fail"
    NOT_RUN = "not run"


@dataclass
class Check:
    nodeid: str
    title: str
    acs: tuple[str, ...]
    result: Result | None = None
    reason: str = ""


@dataclass(frozen=True)
class Verdict:
    ac: str
    text: str
    result: Result
    summary: str
    checks: tuple[Check, ...]


def acceptance_criteria(catalog: str) -> dict[str, str]:
    """AC-1 to AC-8 with their text, in catalog order."""
    return dict(AC_LINE.findall(catalog))


# Words a test name has to write in lower case, as they are written in a sentence.
PROPER = {
    "argus": "ARGUS",
    "csv": "CSV",
    "datev": "DATEV",
    "eur": "EUR",
    "friday": "Friday",
    "iban": "IBAN",
    "november": "November",
    "pdf": "PDF",
    "sunday": "Sunday",
    "vat": "VAT",
    "wise": "Wise",
}
FIXTURE_ID = re.compile(r"^e\d{2}$")  # e01 is the fixture email E01
AC_NAME = re.compile(r"^ac(\d)$")  # ac2 is AC-2


def _word(word: str) -> str:
    if FIXTURE_ID.match(word):
        return word.upper()
    if found := AC_NAME.match(word):
        return f"AC-{found.group(1)}"
    return PROPER.get(word, word)


def title_of(item: pytest.Item) -> str:
    """The test name as a sentence: ``test_e12_waits[x]`` becomes ``E12 waits (x)``."""
    name = getattr(item, "originalname", item.name).removeprefix("test_")
    words = [_word(w) for w in name.split("_") if w]
    title = " ".join(words)
    title = title[:1].upper() + title[1:]
    callspec = getattr(item, "callspec", None)
    return f"{title} ({callspec.id})" if callspec is not None else title


def skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return "skipped"


class Collector:
    """A pytest plugin that records what happened to each acceptance check."""

    def __init__(self) -> None:
        self.checks: dict[str, Check] = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            acs = tuple(
                sorted(
                    {
                        str(req_id)
                        for marker in item.iter_markers(name="req")
                        for req_id in marker.args
                        if str(req_id).startswith("AC-")
                    }
                )
            )
            self.checks[item.nodeid] = Check(item.nodeid, title_of(item), acs)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        check = self.checks.get(report.nodeid)
        if check is None or check.result is Result.FAILED:
            return
        if hasattr(report, "wasxfail"):
            # An expected failure, or one that passed unexpectedly: neither proves the AC.
            check.result, check.reason = Result.NOT_RUN, "marked as an expected failure"
        elif report.failed:
            check.result, check.reason = Result.FAILED, f"failed in {report.when}"
        elif report.skipped:
            check.result, check.reason = Result.NOT_RUN, skip_reason(report)
        elif report.when == "call" and report.passed and check.result is None:
            check.result = Result.PASSED


def verdicts(criteria: dict[str, str], checks: Iterable[Check]) -> list[Verdict]:
    """One verdict per AC. A check that never reported counts as not run."""
    found = list(checks)
    for check in found:
        if check.result is None:
            check.result, check.reason = Result.NOT_RUN, "did not run"
    result = []
    for ac, text in criteria.items():
        mine = tuple(c for c in found if ac in c.acs)
        passed = sum(c.result is Result.PASSED for c in mine)
        failed = sum(c.result is Result.FAILED for c in mine)
        if not mine:
            verdict, summary = Result.NOT_RUN, "no scenario yet"
        elif failed:
            verdict, summary = Result.FAILED, f"{failed} of {len(mine)} checks fail"
        elif passed == len(mine):
            verdict, summary = Result.PASSED, f"all {len(mine)} checks pass"
        elif passed:
            verdict = Result.NOT_RUN
            summary = f"{passed} of {len(mine)} checks pass, {len(mine) - passed} not run"
        else:
            verdict = Result.NOT_RUN
            summary = "the only check is not run" if len(mine) == 1 else "no check ran yet"
        result.append(Verdict(ac, text, verdict, summary, mine))
    return result


def as_text(found: Sequence[Verdict], heading: str) -> str:
    lines = [heading, ""]
    for v in found:
        lines.append(f"{v.ac:<5} {v.result.value.upper():<8} {v.summary}")
        lines.append(f"      {v.text}")
        for c in v.checks:
            reason = f" ({c.reason})" if c.reason else ""
            lines.append(f"        {c.result.value if c.result else '':<8} {c.title}{reason}")
        lines.append("")
    lines.append(totals(found))
    return "\n".join(lines)


def as_markdown(found: Sequence[Verdict], heading: str) -> str:
    lines = [f"## {heading}", "", "| AC | Result | Checks |", "|---|---|---|"]
    lines += [f"| {v.ac} | {v.result.value} | {v.summary} |" for v in found]
    lines.append("")
    for v in found:
        lines.append(f"**{v.ac}** {v.text}")
        lines.append("")
        for c in v.checks:
            reason = f" ({c.reason})" if c.reason else ""
            lines.append(f"- {c.result.value if c.result else ''}: {c.title}{reason}")
        lines.append("")
    lines.append(totals(found))
    return "\n".join(lines) + "\n"


def totals(found: Sequence[Verdict]) -> str:
    count = {r: sum(v.result is r for v in found) for r in Result}
    return ", ".join(f"{count[r]} {r.value}" for r in Result)


def heading() -> str:
    now = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return f"ARGUS acceptance tests, {now} Berlin time, commit {commit or 'unknown'}"


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", type=Path, help="also append the result as Markdown here")
    parser.add_argument("pytest_args", nargs="*", help="passed on to pytest, after --")
    args = parser.parse_args(argv)

    collector = Collector()
    code = pytest.main([str(TESTS), "-q", "-rfE", *args.pytest_args], plugins=[collector])
    if code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        print(f"pytest could not run the acceptance tests (exit code {code})", file=sys.stderr)
        return int(code)

    orphans = [c.nodeid for c in collector.checks.values() if not c.acs]
    if orphans:
        print("Every acceptance test must name its AC in a req marker:", *orphans, sep="\n  ")
        return 2
    criteria = acceptance_criteria(CATALOG.read_text(encoding="utf-8"))
    found = verdicts(criteria, collector.checks.values())
    title = heading()
    print()
    print(as_text(found, title))
    if args.markdown:
        with args.markdown.open("a", encoding="utf-8") as out:
            out.write(as_markdown(found, title))
    return 1 if any(v.result is Result.FAILED for v in found) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

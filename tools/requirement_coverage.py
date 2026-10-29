"""List every requirement ID of the catalog with the tests that cover it (#66, test strategy).

Run from the repository root: ``python tools/requirement_coverage.py``

A test covers a requirement when its ``@pytest.mark.req(...)`` marker names the ID. An acceptance
scenario that waits on a feature not built yet (``@pytest.mark.waits_on``) is listed but does not
count as a test. The script only collects the tests, it runs none of them, so it needs no
database.

``--missing`` lists only the IDs no test names yet. ``--markdown FILE`` writes the list as a
Markdown table to FILE instead of printing it.
"""

import argparse
import contextlib
import io
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "docs" / "requirements" / "requirements.md"
TESTS = REPO / "tests"
# The same pattern tests/conftest.py checks every marker against.
REQ_ID = re.compile(r"\b[A-Z]{2}-\d+\b")
DEFINED = re.compile(r"^- \*\*([A-Z]{2}-\d+)\*\*", re.MULTILINE)


def catalog_ids(catalog: str) -> list[str]:
    """Every requirement ID in the catalog, in the order the catalog defines them, then any ID
    the text only mentions."""
    return list(dict.fromkeys([*DEFINED.findall(catalog), *REQ_ID.findall(catalog)]))


class Collector:
    """A pytest plugin that records the requirement IDs each collected test names."""

    def __init__(self) -> None:
        self.tests: list[Test] = []

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            ids = tuple(str(i) for m in item.iter_markers(name="req") for i in m.args)
            if not ids:
                continue
            waits = item.get_closest_marker("waits_on")
            waits_on = ", ".join(f"#{n}" for n in waits.args) if waits else ""
            # One line per test function, not per parameter set.
            self.tests.append(Test(item.nodeid.partition("[")[0], ids, waits_on))


@dataclass(frozen=True)
class Test:
    nodeid: str
    ids: tuple[str, ...]
    waits_on: str = ""  # issues a scenario waits on, empty for a test that runs

    @property
    def label(self) -> str:
        return (
            f"{self.nodeid} (not built yet, waits on {self.waits_on})"
            if self.waits_on
            else self.nodeid
        )


def coverage(ids: Sequence[str], tests: Iterable[Test]) -> dict[str, list[Test]]:
    """Each catalog ID with the tests that name it, in catalog order."""
    found: dict[str, list[Test]] = {req_id: [] for req_id in ids}
    for test in tests:
        for req_id in dict.fromkeys(test.ids):
            if req_id in found and test.nodeid not in {t.nodeid for t in found[req_id]}:
                found[req_id].append(test)
    return found


def running(tests: list[Test]) -> int:
    return sum(not t.waits_on for t in tests)


def as_text(found: dict[str, list[Test]], missing_only: bool) -> str:
    lines = []
    for req_id, tests in found.items():
        count = running(tests)
        if missing_only and count:
            continue
        lines.append(f"{req_id:<6} {count} test{'' if count == 1 else 's'}")
        lines += [f"       {t.label}" for t in tests]
    lines += ["", summary(found)]
    return "\n".join(lines)


def as_markdown(found: dict[str, list[Test]], missing_only: bool) -> str:
    lines = ["| Requirement | Tests |", "|---|---|"]
    for req_id, tests in found.items():
        if missing_only and running(tests):
            continue
        names = "<br>".join(f"`{t.label}`" for t in tests) or "none yet"
        lines.append(f"| {req_id} | {names} |")
    return "\n".join([*lines, "", summary(found)]) + "\n"


def summary(found: dict[str, list[Test]]) -> str:
    covered = sum(running(tests) > 0 for tests in found.values())
    waiting = sum(bool(tests) and not running(tests) for tests in found.values())
    return (
        f"{covered} of {len(found)} requirement IDs have at least one test. "
        f"{waiting} more only have a scenario that waits on a feature not built yet."
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--missing", action="store_true", help="only IDs without a test")
    parser.add_argument("--markdown", type=Path, help="write a Markdown table to this file")
    args = parser.parse_args(argv)

    collector = Collector()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = pytest.main([str(TESTS), "--collect-only", "-q"], plugins=[collector])
    if code != pytest.ExitCode.OK:
        print(output.getvalue(), file=sys.stderr)
        print(f"pytest could not collect the tests (exit code {code})", file=sys.stderr)
        return int(code)

    found = coverage(catalog_ids(CATALOG.read_text(encoding="utf-8")), collector.tests)
    if args.markdown:
        args.markdown.write_text(as_markdown(found, args.missing), encoding="utf-8")
    else:
        print(as_text(found, args.missing))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

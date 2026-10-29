"""The acceptance runner and the coverage list never show something as tested that did not run
(#66): a scenario that waits on a feature is not run, never passed."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal

import pytest

from tests.acceptance.conftest import waits_on_reason

REPO = Path(__file__).resolve().parents[2]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = load("acceptance")
requirement_coverage = load("requirement_coverage")


def report(
    when: Literal["setup", "call", "teardown"],
    outcome: Literal["passed", "failed", "skipped"],
    **extra: Any,
) -> pytest.TestReport:
    longrepr = (
        ("file.py", 1, "Skipped: not built yet, waits on #41") if outcome == "skipped" else None
    )
    result = pytest.TestReport("t::a", ("file.py", 1, "a"), {}, outcome, longrepr, when)
    for key, value in extra.items():
        setattr(result, key, value)
    return result


def outcome_of(*reports: pytest.TestReport) -> tuple[Any, str]:
    collector = acceptance.Collector()
    collector.checks["t::a"] = acceptance.Check("t::a", "A", ("AC-1",))
    for each in reports:
        collector.pytest_runtest_logreport(each)
    check = collector.checks["t::a"]
    return check.result, check.reason


def test_a_check_passes_only_when_its_call_passed() -> None:
    assert outcome_of(report("setup", "passed"), report("call", "passed"))[0] == "pass"


def test_a_check_skipped_in_setup_is_not_run_with_its_reason() -> None:
    assert outcome_of(report("setup", "skipped")) == ("not run", "not built yet, waits on #41")


def test_a_failure_in_teardown_after_a_pass_is_a_fail() -> None:
    reports = [report("setup", "passed"), report("call", "passed"), report("teardown", "failed")]
    assert outcome_of(*reports)[0] == "fail"


def test_an_expected_failure_that_passed_is_not_a_pass() -> None:
    assert outcome_of(report("call", "passed", wasxfail=""))[0] == "not run"


def check(acs: tuple[str, ...], result: str | None) -> Any:
    found = acceptance.Check("t::x", "X", acs)
    found.result = None if result is None else acceptance.Result(result)
    return found


CRITERIA = {"AC-1": "Sorting", "AC-2": "Booking"}


@pytest.mark.parametrize(
    ("results", "verdict"),
    [
        (["pass", "pass"], "pass"),
        (["pass", "not run"], "not run"),
        (["pass", "fail", "not run"], "fail"),
        (["not run"], "not run"),
        (["pass", None], "not run"),
    ],
)
def test_an_ac_passes_only_when_every_check_ran_and_passed(
    results: list[str | None], verdict: str
) -> None:
    found = acceptance.verdicts(CRITERIA, [check(("AC-1",), r) for r in results])
    assert found[0].result == verdict


def test_an_ac_without_any_scenario_is_not_run() -> None:
    found = acceptance.verdicts(CRITERIA, [check(("AC-1",), "pass")])
    assert (found[1].ac, found[1].result, found[1].summary) == (
        "AC-2",
        "not run",
        "no scenario yet",
    )


def test_the_eight_acceptance_criteria_are_read_from_the_catalog() -> None:
    catalog = (REPO / "docs" / "requirements" / "requirements.md").read_text(encoding="utf-8")
    assert list(acceptance.acceptance_criteria(catalog)) == [f"AC-{n}" for n in range(1, 9)]


@pytest.mark.parametrize(
    ("word", "written"), [("e05", "E05"), ("ac2", "AC-2"), ("iban", "IBAN"), ("vendor", "vendor")]
)
def test_test_names_are_written_as_sentences(word: str, written: str) -> None:
    assert acceptance._word(word) == written


@pytest.mark.parametrize("issues", [(), ("41",), (0,)])
def test_waits_on_needs_issue_numbers(issues: tuple[object, ...]) -> None:
    with pytest.raises(pytest.UsageError):
        waits_on_reason(issues)


def test_the_waits_on_reason_names_every_issue() -> None:
    assert waits_on_reason((41, 42)) == "not built yet, waits on #41, #42"


def test_coverage_lists_ids_in_catalog_order_and_skips_ids_outside_it() -> None:
    catalog = "AC-1 is checked by IN-02.\n- **IN-01** one\n- **IN-02** two\n- **AC-1** three\n"
    test = requirement_coverage.Test("t::a", ("IN-02", "XX-9"))
    found = requirement_coverage.coverage(requirement_coverage.catalog_ids(catalog), [test])
    assert list(found) == ["IN-01", "IN-02", "AC-1"]
    assert found["IN-02"] == [test]


def test_a_scenario_that_waits_on_a_feature_does_not_count_as_a_test() -> None:
    waiting = requirement_coverage.Test("t::w", ("VM-03",), waits_on="#35")
    found = requirement_coverage.coverage(["VM-03", "IN-01"], [waiting])
    text = requirement_coverage.as_text(found, missing_only=True)
    assert "VM-03  0 tests" in text
    assert "t::w (not built yet, waits on #35)" in text
    assert requirement_coverage.summary(found).startswith("0 of 2 requirement IDs")


class FakeItem:
    """Just what the collector reads from a pytest item."""

    def __init__(self, nodeid: str, *acs: str) -> None:
        self.nodeid, self.name, self._acs = nodeid, nodeid.rpartition("::")[2], acs

    def iter_markers(self, name: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(args=self._acs)] if name == "req" else []


def test_a_narrower_run_never_drops_a_check_collected_before() -> None:
    collector = acceptance.Collector()
    everything = [FakeItem("t::test_run_date", "AC-4"), FakeItem("t::test_batch", "AC-4")]
    collector.pytest_collection_modifyitems(everything)
    # The run after ``--`` only names the first check, and it passes.
    collector.pytest_collection_modifyitems(everything[:1])
    collector.pytest_runtest_logreport(
        pytest.TestReport("t::test_run_date", ("t", 1, "a"), {}, "passed", None, "call")
    )
    found = acceptance.verdicts({"AC-4": "Payment run"}, collector.checks.values())
    assert (found[0].result, found[0].summary) == ("not run", "1 of 2 checks pass, 1 not run")

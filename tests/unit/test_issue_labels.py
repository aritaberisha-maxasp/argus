"""The issue labels workflow takes the board labels off closed issues (#195).

The tests run the script against a small in memory GitHub that keeps issues, labels and
comments and records every change.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tidy_issue_labels", REPO / "tools" / "tidy_issue_labels.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the file loads
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.deleted: list[tuple[int, str]] = []
        self.posted: list[int] = []

    def add(
        self, number: int, labels: list[str], *, state: str = "closed", pr: bool = False
    ) -> None:
        issue: dict[str, Any] = {
            "number": number,
            "title": f"Issue {number}",
            "state": state,
            "labels": [{"name": name} for name in labels],
        }
        if pr:
            issue["pull_request"] = {"url": "x"}
        self.issues[number] = issue
        self.comments.setdefault(number, [])

    def labels(self, number: int) -> list[str]:
        return [label["name"] for label in self.issues[number]["labels"]]

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        params = params or {}
        parts = path.strip("/").split("/")
        if parts == ["issues"]:
            # GitHub's list issues call: pull requests come back too, 100 per page.
            hits = [
                issue
                for _, issue in sorted(self.issues.items())
                if issue["state"] == params["state"]
                and params["labels"] in [label["name"] for label in issue["labels"]]
            ]
            return self._page(hits, params)
        if len(parts) == 2:
            return self.issues[int(parts[1])]
        if parts[2] == "comments":
            bodies = [{"body": body} for body in self.comments[int(parts[1])]]
            return self._page(bodies, params)
        raise AssertionError(f"unexpected GET {path}")

    @staticmethod
    def _page(items: list[Any], params: dict[str, str]) -> list[Any]:
        size = int(params.get("per_page", "30"))
        start = (int(params.get("page", "1")) - 1) * size
        return items[start : start + size]

    def post(self, path: str, body: dict[str, Any]) -> Any:
        number = int(path.strip("/").split("/")[1])
        self.comments[number].append(body["body"])
        self.posted.append(number)
        return {}

    def delete(self, path: str) -> None:
        parts = path.strip("/").split("/")
        number = int(parts[1])
        name = tool.urllib.parse.unquote(parts[3])
        self.deleted.append((number, name))
        issue = self.issues[number]
        issue["labels"] = [label for label in issue["labels"] if label["name"] != name]


KEEP = ["story", "req:AP", "role:dev", "size:M", "blocked"]


def test_closed_issue_loses_board_labels_and_keeps_the_rest() -> None:
    github = FakeGitHub()
    github.add(7, [*KEEP, "status:qa-passed", "ready-for-arita"])

    result = tool.tidy_closed(github, 7)

    assert result.removed == ["status:qa-passed", "ready-for-arita"]
    assert github.labels(7) == KEEP
    assert github.posted == []


@pytest.mark.parametrize("label", tool.BOARD_LABELS)
def test_each_board_label_comes_off(label: str) -> None:
    github = FakeGitHub()
    github.add(3, ["story", label])

    tool.tidy_closed(github, 3)

    assert github.labels(3) == ["story"]


def test_board_label_names_match_the_workflow_doc() -> None:
    text = (REPO / "docs" / "process" / "workflow.md").read_text("utf-8")
    for label in tool.BOARD_LABELS:
        assert f"`{label}`" in text


def test_needs_arita_stays_and_gets_one_comment_only() -> None:
    github = FakeGitHub()
    github.add(35, ["story", "status:in-review", "needs-arita"])

    first = tool.tidy_closed(github, 35)
    second = tool.tidy_closed(github, 35)

    assert github.labels(35) == ["story", "needs-arita"]
    assert first.commented and not second.commented
    assert github.posted == [35]
    assert tool.MARKER in github.comments[35][0]
    assert "--" not in github.comments[35][0].replace(tool.MARKER, "")


def test_comment_marker_is_found_beyond_the_first_page_of_comments() -> None:
    github = FakeGitHub()
    github.add(40, ["needs-arita"])
    github.comments[40] = ["chatter"] * 150 + [tool.QUESTION_COMMENT]

    result = tool.tidy_closed(github, 40)

    assert not result.commented
    assert github.posted == []


def test_issue_without_board_labels_is_left_alone() -> None:
    github = FakeGitHub()
    github.add(9, KEEP)

    result = tool.tidy_closed(github, 9)

    assert result.removed == []
    assert github.deleted == [] and github.posted == []
    assert "nothing to do" in tool.closed_report(result)


def test_issue_reopened_before_the_run_is_left_alone() -> None:
    github = FakeGitHub()
    github.add(11, ["status:in-progress", "needs-arita"], state="open")

    result = tool.tidy_closed(github, 11)

    assert result.skipped
    assert github.deleted == [] and github.posted == []


def test_pull_request_is_left_alone() -> None:
    github = FakeGitHub()
    github.add(190, ["ready-for-arita"], pr=True)

    result = tool.tidy_closed(github, 190)

    assert result.skipped
    assert github.labels(190) == ["ready-for-arita"]


def test_sweep_tidies_every_closed_issue_found_now_and_lists_questions() -> None:
    github = FakeGitHub()
    # More than one page of closed issues with the same label.
    for number in range(1000, 1130):
        github.add(number, ["story", "status:in-progress"])
    github.add(34, ["status:qa-passed", "ready-for-arita"])
    github.add(36, ["chore", "needs-arita"])
    github.add(56, ["status:ready", "needs-arita"], state="open")
    github.add(185, ["ready-for-arita"], pr=True)

    outcome = tool.sweep(github)

    assert [r.number for r in outcome.tidied] == [34, *range(1000, 1130)]
    assert github.labels(34) == []
    assert all(github.labels(n) == ["story"] for n in range(1000, 1130))
    assert github.labels(56) == ["status:ready", "needs-arita"]
    assert github.labels(185) == ["ready-for-arita"]
    assert outcome.open_questions == [(36, "Issue 36")]
    assert github.posted == []
    report = tool.sweep_report(outcome, dry_run=False)
    assert "Took labels off 131 closed issues" in report
    assert "- #36 Issue 36" in report


def test_sweep_dry_run_changes_nothing() -> None:
    github = FakeGitHub()
    github.add(31, ["status:in-review"])

    outcome = tool.sweep(github, dry_run=True)

    assert [r.removed for r in outcome.tidied] == [["status:in-review"]]
    assert github.deleted == []
    assert "Would take labels off 1 closed issues" in tool.sweep_report(outcome, dry_run=True)


def test_sweep_with_nothing_to_do() -> None:
    report = tool.sweep_report(tool.sweep(FakeGitHub()), dry_run=False)

    assert "No closed issue has a leftover board label." in report
    assert "None." in report


def test_label_path_encodes_the_colon() -> None:
    assert tool.label_path(5, "status:qa-passed") == "/issues/5/labels/status%3Aqa-passed"


def test_missing_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")

    assert tool.main(["sweep", "--dry-run"]) == 2


def test_workflow_runs_on_closed_issues_only_with_issue_rights_only() -> None:
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "issue-labels.yml").read_text("utf-8")
    )
    # PyYAML reads the key "on" as True.
    triggers = workflow[True]

    assert set(triggers) == {"issues", "workflow_dispatch"}
    assert triggers["issues"]["types"] == ["closed"]
    assert workflow["permissions"] == {"issues": "write"}
    text = (REPO / ".github" / "workflows" / "issue-labels.yml").read_text("utf-8")
    assert "secrets." not in text
    for job in workflow["jobs"].values():
        assert "permissions" not in job

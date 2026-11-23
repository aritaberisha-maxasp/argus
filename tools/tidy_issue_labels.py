"""Take the board labels off closed issues (issue #195).

    python tools/tidy_issue_labels.py closed 123
    python tools/tidy_issue_labels.py sweep [--dry-run]

An issue carries exactly one status label while it is open, and none once it is closed
(docs/process/workflow.md, "Board columns"). GitHub closes an issue when Arita merges its PR,
and nobody from the team is around at that moment, so this script does the tidying instead.
The workflow ``.github/workflows/issue-labels.yml`` runs it.

``closed <n>`` runs when one issue closes. It removes ``status:ready``, ``status:in-progress``,
``status:in-review``, ``status:qa-passed`` and ``ready-for-arita``, and leaves every other
label alone. If ``needs-arita`` is still on the issue, the label stays and the script posts
one comment saying a question for Arita is still open on a closed issue. It posts that comment
only once per issue: it looks for its own marker in the comments first.

``sweep`` is the one time clean up. It looks up every closed issue that still has one of the
five labels, at the moment it runs, removes them, and lists the closed issues that still carry
``needs-arita`` so the PO can move their questions to the numbered question list.
``--dry-run`` only prints what it would do.

Pull requests are left alone in both modes. The script reads ``GITHUB_TOKEN`` and
``GITHUB_REPOSITORY`` from the environment and uses the GitHub REST API only. It needs
nothing but the standard library, so the workflow does not install anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

BOARD_LABELS = (
    "status:ready",
    "status:in-progress",
    "status:in-review",
    "status:qa-passed",
    "ready-for-arita",
)
QUESTION_LABEL = "needs-arita"
# Hidden in the rendered comment. Lets the script see that it already posted on an issue.
MARKER = "<!-- argus-issue-labels:needs-arita -->"
QUESTION_COMMENT = (
    f"{MARKER}\n"
    "This issue is closed, but it still has the label `needs-arita`, so a question for Arita "
    'may still be open here. Arita\'s "Questions for Arita" view only shows open issues, so it '
    "would not show up there.\n\n"
    "The PO moves this question to the numbered question list in the next planning proposal "
    "or daily digest. Whoever reads Arita's answer removes `needs-arita`.\n\n"
    "_Posted by the issue labels workflow (#195)._"
)
API = "https://api.github.com"


class GitHubError(Exception):
    """A call to GitHub failed in a way the script cannot carry on from."""


class GitHub(Protocol):
    def get(self, path: str, params: dict[str, str] | None = None) -> Any: ...

    def post(self, path: str, body: dict[str, Any]) -> Any: ...

    def delete(self, path: str) -> None: ...


class RestGitHub:
    """The few REST calls the script needs, with the built in token of the workflow run."""

    def __init__(self, token: str, repository: str, api: str = API) -> None:
        self.token = token
        self.base = f"{api}/repos/{repository}"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 (fixed https URL of the GitHub API)
            self.base + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "argus-issue-labels",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            if method == "DELETE" and error.code == 404:
                # The label is already gone, for example because someone took it off by hand.
                return None
            raise GitHubError(f"{method} {path} failed with HTTP {error.code}") from error
        return json.loads(text) if text else None

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._call("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, body)

    def delete(self, path: str) -> None:
        self._call("DELETE", path)


def label_path(number: int, label: str) -> str:
    return f"/issues/{number}/labels/{urllib.parse.quote(label, safe='')}"


def pages(github: GitHub, path: str, params: dict[str, str]) -> Iterable[dict[str, Any]]:
    page = 1
    while True:
        items = github.get(path, {**params, "per_page": "100", "page": str(page)})
        yield from items
        if len(items) < 100:
            return
        page += 1


def label_names(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


def already_asked(github: GitHub, number: int) -> bool:
    return any(
        MARKER in (comment.get("body") or "")
        for comment in pages(github, f"/issues/{number}/comments", {})
    )


@dataclass
class Result:
    number: int
    removed: list[str] = field(default_factory=list)
    open_question: bool = False
    commented: bool = False
    skipped: str = ""


def tidy_closed(github: GitHub, number: int, *, dry_run: bool = False) -> Result:
    """Tidy one issue that just closed."""
    result = Result(number)
    issue = github.get(f"/issues/{number}")
    if "pull_request" in issue:
        result.skipped = "is a pull request"
        return result
    if issue.get("state") != "closed":
        # Reopened before the run started. Its labels belong to the open issue again.
        result.skipped = "is open again"
        return result
    names = label_names(issue)
    result.removed = [label for label in BOARD_LABELS if label in names]
    result.open_question = QUESTION_LABEL in names
    if dry_run:
        return result
    for label in result.removed:
        github.delete(label_path(number, label))
    if result.open_question and not already_asked(github, number):
        github.post(f"/issues/{number}/comments", {"body": QUESTION_COMMENT})
        result.commented = True
    return result


def closed_with(github: GitHub, label: str) -> list[dict[str, Any]]:
    params = {"state": "closed", "labels": label}
    return [issue for issue in pages(github, "/issues", params) if "pull_request" not in issue]


@dataclass
class Sweep:
    tidied: list[Result] = field(default_factory=list)
    open_questions: list[tuple[int, str]] = field(default_factory=list)


def sweep(github: GitHub, *, dry_run: bool = False) -> Sweep:
    """Find every closed issue with a leftover board label right now and take the labels off."""
    found: dict[int, dict[str, Any]] = {}
    for label in BOARD_LABELS:
        for issue in closed_with(github, label):
            found[issue["number"]] = issue
    outcome = Sweep()
    for number in sorted(found):
        names = label_names(found[number])
        result = Result(number, removed=[label for label in BOARD_LABELS if label in names])
        if not dry_run:
            for label in result.removed:
                github.delete(label_path(number, label))
        outcome.tidied.append(result)
    for issue in sorted(closed_with(github, QUESTION_LABEL), key=lambda item: item["number"]):
        outcome.open_questions.append((issue["number"], issue.get("title", "")))
    return outcome


def sweep_report(outcome: Sweep, *, dry_run: bool) -> str:
    verb = "Would take" if dry_run else "Took"
    lines = ["## Closed issues with leftover board labels", ""]
    if outcome.tidied:
        lines.append(f"{verb} labels off {len(outcome.tidied)} closed issues:")
        lines.append("")
        lines += [
            f"- #{r.number}: {', '.join(f'`{n}`' for n in r.removed)}" for r in outcome.tidied
        ]
    else:
        lines.append("No closed issue has a leftover board label.")
    lines += ["", "## Closed issues that still have `needs-arita`", ""]
    if outcome.open_questions:
        lines.append(
            "The PO moves these questions to the numbered question list in the next planning "
            "proposal or daily digest:"
        )
        lines.append("")
        lines += [f"- #{number} {title}" for number, title in outcome.open_questions]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def closed_report(result: Result) -> str:
    if result.skipped:
        return f"#{result.number} {result.skipped}, nothing to do."
    parts = []
    if result.removed:
        parts.append(f"took off {', '.join(result.removed)}")
    if result.open_question:
        parts.append(
            "posted the open question comment"
            if result.commented
            else "needs-arita is still on, comment was already there"
        )
    if not parts:
        return f"#{result.number} has no board labels, nothing to do."
    return f"#{result.number}: " + "; ".join(parts) + "."


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    one = modes.add_parser("closed", help="tidy one issue that just closed")
    one.add_argument("number", type=int)
    every = modes.add_parser("sweep", help="tidy every closed issue with leftover labels")
    every.add_argument("--dry-run", action="store_true", help="only print what would change")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY must be set.", file=sys.stderr)
        return 2
    github = RestGitHub(token, repository)
    try:
        if args.mode == "closed":
            print(closed_report(tidy_closed(github, args.number)))
            return 0
        report = sweep_report(sweep(github, dry_run=args.dry_run), dry_run=args.dry_run)
    except GitHubError as error:
        print(error, file=sys.stderr)
        return 1
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

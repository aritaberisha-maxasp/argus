"""Try the whole merge list on top of main before Arita merges it (issue #165).

    python tools/check_merge_order.py 154 156 155 158
    python tools/check_merge_order.py --fast 154 156 155 158

Each PR in the list was tested against main as it was when its checks ran, not against main
after the PRs above it in the list. This script takes the PR numbers in the order Arita will
merge them, starts from the current main on GitHub and merges each PR's head commit on top of
the result of the step before, the way Arita will. After each step it prints one line:

- ``clean``,
- ``conflict`` with the files that clash. That PR is left out and the steps below it are
  tried without it, so one conflict does not hide the next one,
- ``migration clash`` from ``tools/check_migrations.py``, so two PRs whose migrations point
  at the same earlier one show up at the step where they meet, or
- ``tests red`` with the names of the failing tests. Tests that already fail on main here,
  for example because a service such as PostgreSQL is not running, are left out of every step
  and named once at the top.

``--fast`` skips the tests and only checks for conflicts and the migration chain.

By default each step is a squash merge, because that is how Arita merges. A squashed PR leaves
no trace of its commits in main, so a PR built on another PR's branch (a stacked PR) can
conflict with main once the PR below it is squashed in. That is exactly what the check should
catch. ``--merge-commit`` checks the list as if Arita used "Create a merge commit" instead.

The script only reads from GitHub. It fetches main and each PR's head into local refs under
``refs/merge-check/``, builds the merges in a throwaway local worktree and removes it at the
end. It pushes nothing and touches no branch on GitHub. A PR that targets another PR's branch
is handled the same as any other: the script uses its head commit, not its base.

The exit code is 0 only when every step is clean, 1 when any step needs a fix, and 2 when the
script could not run, for example because a PR number does not exist.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
MIGRATIONS = Path("src/argus/db/migrations")
REF_PREFIX = "refs/merge-check"
DEFAULT_TESTS = f"{shlex.quote(sys.executable)} -m pytest -q -rfE --no-header -p no:cacheprovider"
# The synthetic merge commits only live in this local repository, but git still wants a name.
IDENTITY = {
    "GIT_AUTHOR_NAME": "merge list check",
    "GIT_AUTHOR_EMAIL": "merge-check@localhost",
    "GIT_COMMITTER_NAME": "merge list check",
    "GIT_COMMITTER_EMAIL": "merge-check@localhost",
}


class CheckError(Exception):
    """The check could not run at all, as opposed to a step that is not clean."""


@dataclass
class Step:
    number: int
    pr: int
    conflicts: list[str] = field(default_factory=list)
    migration_problems: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    tests_broken: str = ""
    already_in: bool = False

    @property
    def clean(self) -> bool:
        return not (
            self.conflicts or self.migration_problems or self.failing_tests or self.tests_broken
        )

    def line(self) -> str:
        if self.conflicts:
            verdict = (
                f"conflict in {', '.join(self.conflicts)} "
                "(left out, the steps below are tried without it)"
            )
        else:
            parts = []
            if self.migration_problems:
                parts.append(f"migration clash: {'; '.join(self.migration_problems)}")
            if self.failing_tests:
                parts.append(f"tests red: {', '.join(self.failing_tests)}")
            if self.tests_broken:
                parts.append(f"tests red: {self.tests_broken}")
            if parts:
                verdict = "; ".join(parts)
            elif self.already_in:
                verdict = "clean (nothing new, its changes are already in main)"
            else:
                verdict = "clean"
        return f"Step {self.number}  #{self.pr}  {verdict}"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 (fixed git commands, no shell)
        ["git", *args],  # noqa: S607 (git from PATH, like every other tool here)
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    )
    if check and result.returncode != 0:
        raise CheckError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def fetch(repo: Path, remote: str, base: str, prs: list[int]) -> tuple[str, dict[int, str]]:
    """Fetch main and every PR head into local refs, and return their commits.

    GitHub keeps every PR's head at ``refs/pull/<n>/head``, also when the PR targets another
    PR's branch, so this needs no API call and no write access.
    """
    refspecs = [f"+refs/heads/{base}:{REF_PREFIX}/base"]
    refspecs += [f"+refs/pull/{pr}/head:{REF_PREFIX}/pr/{pr}" for pr in prs]
    # A shallow clone, as cloud sessions get, has no merge base, so every PR would look
    # unrelated to main. Fetch the whole history the first time.
    shallow = git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    deepen = ["--unshallow"] if shallow else []
    result = git(
        repo, "fetch", "--no-tags", "--quiet", *deepen, remote, *refspecs, check=False
    )  # fmt: skip
    if result.returncode != 0:
        missing = [pr for pr in prs if f"refs/pull/{pr}/head" in result.stderr]
        if missing:
            names = ", ".join(f"#{pr}" for pr in missing)
            raise CheckError(f"GitHub has no pull request {names}.")
        raise CheckError(f"Could not fetch from {remote}: {result.stderr.strip()}")
    base_commit = git(repo, "rev-parse", f"{REF_PREFIX}/base").stdout.strip()
    heads = {pr: git(repo, "rev-parse", f"{REF_PREFIX}/pr/{pr}").stdout.strip() for pr in prs}
    return base_commit, heads


def merge(repo: Path, current: str, pr: int, head: str, squash: bool) -> tuple[str, list[str]]:
    """Merge one PR head onto ``current`` without a checkout.

    Returns the new commit, or ``current`` and the conflicting files when it does not merge.
    """
    result = git(
        repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", current, head,
        check=False,
    )  # fmt: skip
    lines = [line for line in result.stdout.splitlines() if line]
    if result.returncode == 1:
        return current, sorted(set(lines[1:]))
    if result.returncode != 0:
        raise CheckError(f"git merge-tree failed for #{pr}: {result.stderr.strip()}")
    parents = ["-p", current] if squash else ["-p", current, "-p", head]
    message = f"{'Squash' if squash else 'Merge'} #{pr} (merge list check)"
    commit = git(repo, "commit-tree", lines[0], *parents, "-m", message).stdout.strip()
    return commit, []


def same_tree(repo: Path, one: str, other: str) -> bool:
    trees = git(repo, "rev-parse", f"{one}^{{tree}}", f"{other}^{{tree}}").stdout.split()
    return trees[0] == trees[1]


def migration_problems(worktree: Path) -> list[str]:
    """The clashes ``tools/check_migrations.py`` finds in this worktree, if any."""
    folder = worktree / MIGRATIONS
    if not (folder / "versions").is_dir():
        return []
    result = subprocess.run(  # noqa: S603 (our own check, no shell)
        [sys.executable, str(TOOLS / "check_migrations.py"), str(folder)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    prefix = "Migration clash: "
    found = [line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)]
    return found or [result.stdout.strip() or result.stderr.strip()]


def run_tests(worktree: Path, command: list[str]) -> tuple[set[str], str]:
    """Run the tests in the worktree. Returns the failing test names, or a reason when the run
    broke without naming any (for example a collection error)."""
    # The project is installed in editable mode from the main checkout. Put the worktree's own
    # code first, so the tests run against the merged code and not the checkout's.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(worktree / "src"), str(worktree)])}
    result = subprocess.run(  # noqa: S603 (the command comes from whoever runs the check)
        command, cwd=worktree, capture_output=True, text=True, env=env
    )
    if result.returncode in (0, 5):  # 5 means pytest found no tests
        return set(), ""
    failing = set()
    for line in result.stdout.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            failing.add(line.split(" ", 1)[1].split(" - ", 1)[0].strip())
    if failing:
        return failing, ""
    return set(), f"the test run stopped with exit code {result.returncode} and named no test"


def check(
    repo: Path,
    prs: list[int],
    *,
    remote: str = "origin",
    base: str = "main",
    fast: bool = False,
    squash: bool = True,
    tests: list[str] | None = None,
    out: list[str] | None = None,
) -> list[Step]:
    """Try the merge list and return one step per PR. Lines for people go to ``out``."""
    say = out.append if out is not None else print
    command = tests or shlex.split(DEFAULT_TESTS)
    base_commit, heads = fetch(repo, remote, base, prs)
    how = "squash merges" if squash else "merge commits"
    say(f"Starting from {base} at {base_commit[:7]}, with {how}.")

    worktree = Path(tempfile.mkdtemp(prefix="merge-check-"))
    git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), base_commit)
    try:
        known_red: set[str] = set()
        if not fast:
            known_red, broken = run_tests(worktree, command)
            if broken:
                raise CheckError(f"The tests do not run on {base} here: {broken}.")
            if known_red:
                say(
                    f"{len(known_red)} tests already fail on {base} here, so the steps leave them "
                    f"out: {', '.join(sorted(known_red))}"
                )
        known_clashes = set(migration_problems(worktree))

        steps = []
        current = base_commit
        for number, pr in enumerate(prs, start=1):
            step = Step(number, pr)
            merged, step.conflicts = merge(repo, current, pr, heads[pr], squash)
            if not step.conflicts:
                step.already_in = same_tree(repo, current, merged)
                current = merged
                git(worktree, "checkout", "--detach", "--force", "--quiet", current)
                git(worktree, "clean", "-fdxq")
                step.migration_problems = [
                    p for p in migration_problems(worktree) if p not in known_clashes
                ]
                if not fast:
                    failing, step.tests_broken = run_tests(worktree, command)
                    step.failing_tests = sorted(failing - known_red)
            say(step.line())
            steps.append(step)
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)

    flagged = [step for step in steps if not step.clean]
    if flagged:
        names = ", ".join(f"#{step.pr}" for step in flagged)
        say(f"{len(flagged)} of {len(steps)} steps need a fix before Arita merges: {names}.")
    else:
        say(f"All {len(steps)} steps are clean.")
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Try the whole merge list on top of main before Arita merges it."
    )
    parser.add_argument("prs", nargs="+", type=int, help="PR numbers in merge order")
    parser.add_argument("--fast", action="store_true", help="skip the tests")
    parser.add_argument(
        "--merge-commit",
        action="store_true",
        help='check as if Arita used "Create a merge commit" instead of squash merging',
    )
    parser.add_argument("--remote", default="origin", help="the GitHub remote (default origin)")
    parser.add_argument("--base", default="main", help="the branch to start from (default main)")
    parser.add_argument("--tests", default=DEFAULT_TESTS, help="the command that runs the tests")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the local repository")
    args = parser.parse_args(argv)

    if not (args.repo / ".git").exists():
        print(f"{args.repo} is not a git repository. Run the check from the ARGUS checkout.")
        return 2
    if len(set(args.prs)) != len(args.prs):
        print("Each PR may appear only once in the merge list.")
        return 2
    try:
        steps = check(
            args.repo,
            args.prs,
            remote=args.remote,
            base=args.base,
            fast=args.fast,
            squash=not args.merge_commit,
            tests=shlex.split(args.tests),
        )
    except CheckError as error:
        print(f"The merge list check could not run: {error}")
        return 2
    return 0 if all(step.clean for step in steps) else 1


if __name__ == "__main__":
    sys.exit(main())

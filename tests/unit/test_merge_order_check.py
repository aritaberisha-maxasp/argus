"""The merge list check tries every PR on top of main in order, on a throwaway repository (#165).

Each test builds a small "GitHub" repository with a main branch and PR heads under
``refs/pull/<n>/head``, the way GitHub keeps them, clones it and runs the check in the clone.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
TESTS = f"{sys.executable} -m pytest -q -rfE -p no:cacheprovider"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_merge_order", REPO / "tools" / "check_merge_order.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the file loads
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = load_check()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 (fixed git commands in a throwaway repository)
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    ).stdout.strip()


def migration(revision: str, down: str | None) -> str:
    return (
        f'"""Test migration {revision}."""\n\n'
        f"revision = {revision!r}\n"
        f"down_revision = {down!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\n"
        "def downgrade() -> None:\n    pass\n"
    )


class GitHub:
    """A throwaway stand in for the GitHub repository, plus a local clone to run the check in."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "github"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        self.root = root

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for name, text in files.items():
            path = self.origin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.origin, "add", "--all")
        git(self.origin, "commit", "--quiet", "-m", message)
        return git(self.origin, "rev-parse", "HEAD")

    def pull_request(self, number: int, start: str, files: dict[str, str]) -> str:
        """A PR branched from ``start`` (main or another PR's head) with one commit."""
        git(self.origin, "checkout", "--quiet", "--detach", start)
        head = self.commit(files, f"PR {number}")
        git(self.origin, "update-ref", f"refs/pull/{number}/head", head)
        git(self.origin, "checkout", "--quiet", "main")
        return head

    def clone(self) -> Path:
        local = self.root / "local"
        git(self.root, "clone", "--quiet", str(self.origin), str(local))
        return local


@pytest.fixture
def github(tmp_path: Path) -> GitHub:
    hub = GitHub(tmp_path)
    hub.commit(
        {
            "app.py": "colour = 'blue'\nsize = 1\n",
            "src/argus/db/migrations/versions/0001_first.py": migration("0001", None),
        },
        "main",
    )
    return hub


def run(local: Path, *args: str) -> tuple[int, list[str]]:
    out: list[str] = []
    prs = [int(arg) for arg in args if arg.isdigit()]
    options = [arg for arg in args if not arg.isdigit()]
    try:
        steps = check.check(
            local,
            prs,
            fast="--fast" in options,
            squash="--merge-commit" not in options,
            tests=TESTS.split(),
            out=out,
        )
    except check.CheckError as error:
        return 2, [str(error)]
    return (0 if all(step.clean for step in steps) else 1), out


@pytest.mark.req("NF-01")
def test_a_clean_list_passes_and_touches_nothing_on_github(github: GitHub) -> None:
    github.pull_request(1, "main", {"one.py": "x = 1\n"})
    github.pull_request(2, "main", {"two.py": "y = 2\n"})
    before = git(github.origin, "for-each-ref")
    local = github.clone()

    code, out = run(local, "1", "2", "--fast")

    assert code == 0
    assert out[1] == "Step 1  #1  clean"
    assert out[2] == "Step 2  #2  clean"
    assert out[-1] == "All 2 steps are clean."
    assert git(github.origin, "for-each-ref") == before
    assert git(local, "status", "--porcelain") == ""
    assert "merge-check" not in git(local, "worktree", "list")


@pytest.mark.req("NF-01")
def test_a_conflict_at_step_2_names_the_file_and_the_list_goes_on_without_it(
    github: GitHub,
) -> None:
    github.pull_request(1, "main", {"app.py": "colour = 'red'\nsize = 1\n"})
    github.pull_request(2, "main", {"app.py": "colour = 'green'\nsize = 1\n"})
    github.pull_request(3, "main", {"three.py": "z = 3\n"})
    local = github.clone()

    code, out = run(local, "1", "2", "3", "--fast")

    assert code == 1
    assert out[1] == "Step 1  #1  clean"
    assert out[2] == (
        "Step 2  #2  conflict in app.py (left out, the steps below are tried without it)"
    )
    assert out[3] == "Step 3  #3  clean"
    assert out[-1] == "1 of 3 steps need a fix before Arita merges: #2."
    assert check.main(["--fast", "--repo", str(local), "1", "2", "3"]) == 1
    assert check.main(["--fast", "--repo", str(local), "1", "3"]) == 0


@pytest.mark.req("NF-01")
def test_two_migrations_on_the_same_parent_clash_at_the_step_where_they_meet(
    github: GitHub,
) -> None:
    versions = "src/argus/db/migrations/versions"
    github.pull_request(1, "main", {f"{versions}/0002_one.py": migration("0002", "0001")})
    github.pull_request(2, "main", {f"{versions}/0003_two.py": migration("0003", "0001")})

    code, out = run(github.clone(), "1", "2", "--fast")

    assert code == 1
    assert out[1] == "Step 1  #1  clean"
    assert out[2].startswith("Step 2  #2  migration clash: ")
    assert "0002_one.py" in out[2]
    assert "0003_two.py" in out[2]


@pytest.mark.req("NF-01")
def test_a_stacked_pr_is_checked_from_its_own_head(github: GitHub) -> None:
    """PR 2 targets PR 1's branch. PR 1 gets one more commit after PR 2 was branched off it.

    Squashed, PR 1's commits are not in main, so PR 2 brings back the older line and clashes,
    which is what Arita saw with PRs 155 and 158. With merge commits it goes in cleanly.
    """
    first = github.pull_request(1, "main", {"feature.py": "limit = 10\n"})
    github.pull_request(2, first, {"page.py": "title = 'Invoices'\n"})
    git(github.origin, "checkout", "--quiet", "--detach", first)
    later = github.commit({"feature.py": "limit = 20\n"}, "PR 1 review fix")
    git(github.origin, "update-ref", "refs/pull/1/head", later)
    git(github.origin, "checkout", "--quiet", "main")
    local = github.clone()

    assert run(local, "2", "--fast")[1][1] == "Step 1  #2  clean"

    code, out = run(local, "1", "2", "--fast")
    assert code == 1
    assert out[2].startswith("Step 2  #2  conflict in feature.py")

    code, out = run(local, "1", "2", "--fast", "--merge-commit")
    assert code == 0, out
    assert out[0].endswith("with merge commits.")


@pytest.mark.req("NF-01")
def test_red_tests_are_named_and_tests_already_red_on_main_are_left_out(
    github: GitHub,
) -> None:
    github.commit(
        {
            "test_main.py": "def test_fine():\n    pass\n\n\n"
            "def test_already_red():\n    assert False\n",
        }
    )
    github.pull_request(1, "main", {"test_one.py": "def test_new_red():\n    assert False\n"})

    code, out = run(github.clone(), "1")

    assert code == 1
    assert out[1] == (
        "1 tests already fail on main here, so the steps leave them out: "
        "test_main.py::test_already_red"
    )
    assert out[2] == "Step 1  #1  tests red: test_one.py::test_new_red"


@pytest.mark.req("NF-01")
def test_a_pr_that_does_not_exist_stops_the_check(github: GitHub) -> None:
    github.pull_request(1, "main", {"one.py": "x = 1\n"})

    code, out = run(github.clone(), "1", "99", "--fast")

    assert code == 2
    assert out == ["GitHub has no pull request #99."]


@pytest.mark.req("NF-01")
def test_a_shallow_clone_is_deepened_so_prs_still_have_a_merge_base(github: GitHub) -> None:
    github.commit({"more.py": "a = 1\n"})
    github.pull_request(1, "main", {"one.py": "x = 1\n"})
    shallow = github.root / "shallow"
    git(github.root, "clone", "--quiet", "--depth=1", github.origin.as_uri(), str(shallow))

    code, out = run(shallow, "1", "--fast")

    assert code == 0, out
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "false"

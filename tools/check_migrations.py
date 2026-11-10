"""Fail when the database migrations no longer form one straight line (issue #123, NF-01).

    python tools/check_migrations.py [migrations folder]

Two open PRs that each add a migration pointing at the same earlier one give the database two
"latest" versions, and Alembic refuses to upgrade. This check reads the migration files only,
with no database, and fails when:

- the chain has more than one head, naming the files that split it, or
- two files in ``versions/`` start with the same number, such as two ``0011_`` files, or
- Alembic cannot read the chain at all, for example a ``down_revision`` that no file has.

The folder defaults to the one in ``alembic.ini`` (``src/argus/db/migrations``). Run it from the
repository root.
"""

import re
import sys
import warnings
from collections import defaultdict
from pathlib import Path

from alembic.script import Script, ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError

DEFAULT_FOLDER = Path("src/argus/db/migrations")
NUMBER = re.compile(r"^(\d+)_")
HOW_TO_FIX = (
    "Point your new migration's down_revision at the highest migration in main or in any open "
    "PR, give it the next free number, and write 'Merge after #<n>' in the PR description."
)
MISSING_PARENT = "is not present"
HOW_TO_FIX_MISSING_PARENT = (
    "A migration points at one this branch does not have. If that one is in an open PR, build "
    "your branch on that PR's branch and write 'Merge after #<n>' in the PR description, or wait "
    "until that PR merges and update your branch from main."
)


def _file(script: Script, folder: Path) -> str:
    return Path(script.path).relative_to(folder).as_posix()


def duplicate_numbers(folder: Path) -> list[str]:
    """One line for each number prefix that more than one migration file uses."""
    by_number: dict[str, list[str]] = defaultdict(list)
    for path in sorted((folder / "versions").glob("*.py")):
        match = NUMBER.match(path.name)
        if match:
            by_number[match.group(1)].append(f"versions/{path.name}")
    return [
        f"Number {number} is used by more than one migration: {', '.join(files)}"
        for number, files in sorted(by_number.items())
        if len(files) > 1
    ]


def chain_problems(folder: Path) -> list[str]:
    """One line for each place the chain splits, or for a chain Alembic cannot read."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            scripts = ScriptDirectory(str(folder))
            heads = scripts.get_heads()
            revisions = list(scripts.walk_revisions())
        except (CommandError, RevisionError, KeyError) as error:
            # A missing parent comes out as a bare KeyError, after a warning that names it.
            reason = "; ".join(str(warning.message) for warning in caught) or repr(error)
            return [f"Alembic cannot read the migration chain: {reason}"]

    problems = [f"Alembic warns: {warning.message}" for warning in caught]
    if len(heads) <= 1:
        return problems

    head_files = sorted(_file(scripts.get_revision(head), folder) for head in heads)
    problems.append(f"The chain has {len(heads)} heads: {', '.join(head_files)}")
    for script in sorted(revisions, key=lambda s: s.revision):
        children = sorted(_file(scripts.get_revision(child), folder) for child in script.nextrev)
        if len(children) > 1:
            who = (
                f"Both {children[0]} and {children[1]}"
                if len(children) == 2
                else f"{', '.join(children[:-1])} and {children[-1]} all"
            )
            problems.append(f"{who} point at {script.revision} ({_file(script, folder)})")
    return problems


def problems(folder: Path) -> list[str]:
    """Every reason the migrations in this folder are not one straight line."""
    # Alembic gives each script's path as an absolute one, so the folder must be absolute too
    # to name the files relative to it (QA on PR 128).
    folder = folder.resolve()
    return duplicate_numbers(folder) + chain_problems(folder)


def how_to_fix(found: list[str]) -> str:
    """The advice for these problems. A missing parent needs its own (reviewer on PR 128)."""
    if any(MISSING_PARENT in problem for problem in found):
        return HOW_TO_FIX_MISSING_PARENT
    return HOW_TO_FIX


def main(argv: list[str]) -> int:
    folder = Path(argv[1]) if len(argv) > 1 else DEFAULT_FOLDER
    if not (folder / "versions").is_dir():
        print(f"No migrations folder at {folder} (it needs a versions folder inside).")
        return 1
    found = problems(folder)
    for problem in found:
        print(f"Migration clash: {problem}")
    if found:
        print(how_to_fix(found))
        return 1
    head = ScriptDirectory(str(folder)).get_current_head()
    print(f"The migrations in {folder} form one chain, head {head}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

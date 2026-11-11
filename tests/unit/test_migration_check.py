"""The CI check fails when the migration chain splits or a number is used twice (#123, NF-01)."""

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "src" / "argus" / "db" / "migrations"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_migrations", REPO / "tools" / "check_migrations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def folder(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway migrations folder with the given files in ``versions/``."""
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    for name, text in files.items():
        (versions / name).write_text(text, encoding="utf-8")
    return tmp_path / "migrations"


def next_free(path: Path, step: int = 1) -> str:
    """A number no migration in ``path`` uses yet, so a test file never clashes by number
    with a real migration that merges later."""
    numbers = [int(file.name[:4]) for file in (path / "versions").glob("[0-9][0-9][0-9][0-9]_*.py")]
    return f"{max(numbers) + step:04d}"


def copy_of_main(tmp_path: Path) -> Path:
    """The real migrations of this branch, copied so a test can add a file to them."""
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


@pytest.mark.req("NF-01")
def test_the_migrations_in_the_repository_form_one_chain() -> None:
    assert load_check().problems(MIGRATIONS) == []


@pytest.mark.req("NF-01")
def test_a_straight_chain_passes(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0003_c.py": migration("0003", "0002"),
        },
    )
    assert load_check().problems(path) == []


@pytest.mark.req("NF-01")
def test_a_chain_that_is_not_in_number_order_still_passes(tmp_path: Path) -> None:
    # On main 0005 follows 0007 and 0008 follows 0005. One line is all that counts.
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0003"),
            "0003_c.py": migration("0003", "0001"),
        },
    )
    assert load_check().problems(path) == []


@pytest.mark.req("NF-01")
def test_a_second_head_fails_and_names_the_files(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0003_c.py": migration("0003", "0001"),
        },
    )
    assert load_check().problems(path) == [
        "The chain has 2 heads: versions/0002_b.py, versions/0003_c.py",
        "Both versions/0002_b.py and versions/0003_c.py point at 0001 (versions/0001_a.py)",
    ]


@pytest.mark.req("NF-01")
def test_a_new_migration_on_an_old_parent_in_main_fails(tmp_path: Path) -> None:
    # The Sprint 2 case: a PR adds a migration on top of 0008 while main already has 0009 there.
    path = copy_of_main(tmp_path)
    late = f"{next_free(path)}_late.py"
    (path / "versions" / late).write_text(migration(next_free(path), "0008"), encoding="utf-8")
    found = load_check().problems(path)
    assert found[0].startswith("The chain has 2 heads:")
    assert f"versions/{late}" in found[0]
    assert f"versions/{late}" in found[1] and "point at 0008" in found[1]


@pytest.mark.req("NF-01")
def test_a_number_used_twice_fails_even_when_the_chain_is_straight(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0002_c.py": migration("0002c", "0002"),
        },
    )
    assert load_check().problems(path) == [
        "Number 0002 is used by more than one migration: versions/0002_b.py, versions/0002_c.py"
    ]


@pytest.mark.req("NF-01")
def test_two_files_with_the_same_revision_id_fail(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0002_c.py": migration("0002", "0001"),
        },
    )
    found = load_check().problems(path)
    assert found[0].startswith("Number 0002 is used by more than one migration")
    assert any("0002" in line and "more than once" in line for line in found[1:])


@pytest.mark.req("NF-01")
def test_a_parent_that_no_file_has_fails(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0099"),
        },
    )
    found = load_check().problems(path)
    assert len(found) == 1
    assert found[0].startswith("Alembic cannot read the migration chain:")
    assert "0099" in found[0]


@pytest.mark.req("NF-01")
def test_the_command_says_what_to_do_when_it_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0003_c.py": migration("0003", "0001"),
        },
    )
    assert load_check().main(["check_migrations.py", str(path)]) == 1
    output = capsys.readouterr().out
    assert "Migration clash: The chain has 2 heads" in output
    assert "Merge after" in output


@pytest.mark.req("NF-01")
def test_the_command_passes_on_the_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO)
    assert load_check().main(["check_migrations.py"]) == 0
    assert "form one chain, head " in capsys.readouterr().out


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("argument", ["migrations", "./migrations"])
def test_the_command_names_the_files_with_a_relative_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
) -> None:
    folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0003_c.py": migration("0003", "0001"),
        },
    )
    monkeypatch.chdir(tmp_path)
    assert load_check().main(["check_migrations.py", argument]) == 1
    output = capsys.readouterr().out
    assert "Both versions/0002_b.py and versions/0003_c.py point at 0001" in output


@pytest.mark.req("NF-01")
def test_the_command_names_the_files_with_no_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The way CI runs it: from the repository root, with the default folder.
    copy = tmp_path / "src" / "argus" / "db" / "migrations"
    shutil.copytree(MIGRATIONS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    late = f"{next_free(copy)}_late.py"
    (copy / "versions" / late).write_text(migration(next_free(copy), "0008"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_check().main(["check_migrations.py"]) == 1
    output = capsys.readouterr().out
    assert "Migration clash: The chain has 2 heads:" in output
    assert f"versions/{late}" in output and "point at 0008" in output


@pytest.mark.req("NF-01")
def test_a_parent_from_another_branch_gets_its_own_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Pointing the next number at a parent that only an open PR has, while the branch is
    # based on main.
    path = copy_of_main(tmp_path)
    other, mine = next_free(path), next_free(path, 2)
    (path / "versions" / f"{mine}_next.py").write_text(migration(mine, other), encoding="utf-8")
    check = load_check()
    assert check.main(["check_migrations.py", str(path)]) == 1
    output = capsys.readouterr().out
    assert other in output and "is not present" in output
    assert check.HOW_TO_FIX_MISSING_PARENT in output
    assert check.HOW_TO_FIX not in output


@pytest.mark.req("NF-01")
def test_three_migrations_on_one_parent_are_named_in_one_line(tmp_path: Path) -> None:
    path = folder(
        tmp_path,
        {
            "0001_a.py": migration("0001", None),
            "0002_b.py": migration("0002", "0001"),
            "0003_c.py": migration("0003", "0001"),
            "0004_d.py": migration("0004", "0001"),
        },
    )
    assert load_check().problems(path)[1] == (
        "versions/0002_b.py, versions/0003_c.py and versions/0004_d.py all point at 0001 "
        "(versions/0001_a.py)"
    )


@pytest.mark.req("NF-01")
def test_a_wrong_folder_is_not_called_a_clash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load_check().main(["check_migrations.py", str(tmp_path / "nowhere")]) == 1
    output = capsys.readouterr().out
    assert output.startswith("No migrations folder at ")
    assert "clash" not in output

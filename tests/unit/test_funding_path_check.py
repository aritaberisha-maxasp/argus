"""The CI check fails when a Wise funding path appears in ARGUS code (ADR 0004, NF-04)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_no_funding_paths", REPO / "tools" / "check_no_funding_paths.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.req("NF-04")
@pytest.mark.parametrize(
    "line",
    [
        'url = f"/v3/profiles/{p}/batch-payments/{g}/payments"',
        'url = f"/v3/profiles/{p}/batch-groups/{g}/payment-initiations"',
        'url = f"/v3/profiles/{p}/transfers/{t}/payments"',
        'URL = "/V3/PROFILES/1/BATCH-PAYMENTS"',
    ],
)
def test_a_funding_path_in_the_code_fails_the_check(tmp_path: Path, line: str) -> None:
    write(tmp_path, "src/argus/wise/extra.py", f"{line}\n")
    write(tmp_path, "standins/wise/app.py", "ok = True\n")
    assert load_check().funding_paths(tmp_path) == [f"src/argus/wise/extra.py:1: {line}"]


@pytest.mark.req("NF-04")
def test_a_funding_endpoint_in_the_standin_fails_the_check(tmp_path: Path) -> None:
    write(tmp_path, "src/argus/app.py", "ok = True\n")
    write(tmp_path, "standins/wise/app.py", '@app.post("/v3/profiles/{p}/batch-payments")\n')
    assert len(load_check().funding_paths(tmp_path)) == 1


@pytest.mark.req("NF-04")
def test_the_repository_has_no_funding_path() -> None:
    assert load_check().funding_paths(REPO) == []

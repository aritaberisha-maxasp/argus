"""The rules check on the deploy and preview workflows (tools/check_pipeline.py, #81).

The real workflows must pass. Each broken copy below must fail, so the check can be trusted.
"""

import importlib.util
import shutil
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_pipeline", REPO / "tools" / "check_pipeline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_pipeline = load_check()


def copy_repo(tmp_path: Path) -> Path:
    for relative in (
        ".github/workflows/deploy.yml",
        ".github/workflows/infra-preview.yml",
        "tools/deploy.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, target)
    shutil.copytree(REPO / "tools" / "azure", tmp_path / "tools" / "azure")
    return tmp_path


def edit(root: Path, relative: str, change: Callable[[str], str]) -> None:
    path = root / relative
    before = path.read_text(encoding="utf-8")
    after = change(before)
    assert after != before, "the test did not change the file"
    path.write_text(after, encoding="utf-8")


DEPLOY = ".github/workflows/deploy.yml"
PREVIEW = ".github/workflows/infra-preview.yml"


@pytest.mark.req("NF-04", "NF-06")
def test_the_real_workflows_keep_the_rules() -> None:
    assert check_pipeline.check(REPO) == []
    assert check_pipeline.main([str(REPO)]) == 0


@pytest.mark.req("NF-06")
def test_a_stored_azure_secret_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        DEPLOY,
        lambda s: s.replace(
            "          client-id: ${{ vars.AZURE_CLIENT_ID }}\n",
            "          creds: ${{ secrets.AZURE_CREDENTIALS }}\n",
        ),
    )
    problems = check_pipeline.check(root)
    assert any("reads a GitHub secret" in p for p in problems)
    assert any("other inputs than the three IDs (extra: creds)" in p for p in problems)


def test_a_deploy_on_pull_requests_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace("  workflow_dispatch:\n", "  pull_request:\n"))
    assert "deploy.yml may only run after CI or by hand" in check_pipeline.check(root)


def test_a_deploy_without_the_main_guard_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace("github.ref == 'refs/heads/main' && ", ""))
    assert any("must require main" in p for p in check_pipeline.check(root))


def test_new_image_before_the_migrations_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        DEPLOY,
        lambda s: s.replace(
            "ARGUS_APP_IMAGE: ${{ steps.current.outputs.image }}",
            "ARGUS_APP_IMAGE: ${{ steps.image.outputs.image }}",
        ),
    )
    assert any("with the running image" in p for p in check_pipeline.check(root))


def test_moving_the_migrations_after_the_release_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace("id: migrate", "id: migrate-later"))
    assert any("in the order" in p for p in check_pipeline.check(root))


def test_carrying_on_after_a_failed_migration_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        DEPLOY,
        lambda s: s.replace(
            "        id: migrate\n", "        id: migrate\n        continue-on-error: true\n"
        ),
    )
    assert any("may not continue on error" in p for p in check_pipeline.check(root))


@pytest.mark.req("NF-01")
def test_touching_the_retention_lock_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        "tools/azure/apply_infra.sh",
        lambda s: s.replace(
            "    --parameters infra/main.bicepparam \\\n",
            "    --parameters infra/main.bicepparam retentionLocked=true \\\n",
        ),
    )
    assert any("the retention lock" in p for p in check_pipeline.check(root))


@pytest.mark.req("NF-04")
@pytest.mark.parametrize(
    "line",
    [
        "az keyvault secret show --vault-name kv --name wise-api-token",
        "curl https://api.wise.com/v3/profiles",
    ],
)
def test_reading_a_secret_or_calling_wise_fails(tmp_path: Path, line: str) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace("az acr login", f"{line}\n          az acr login"))
    problems = check_pipeline.check(root)
    assert any(p.startswith("deploy.yml mentions") for p in problems)


@pytest.mark.req("NF-04")
def test_a_wise_call_in_the_deploy_helper_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        "tools/deploy.py",
        lambda s: s.replace('APP_ROLE = "argus_app"', 'APP_ROLE = "argus_app"\nWISE = "wise"'),
    )
    assert any("tools/deploy.py mentions Wise" in p for p in check_pipeline.check(root))


@pytest.mark.parametrize(
    "line",
    [
        "az deployment group create --resource-group x --parameters infra/main.bicepparam",
        "docker push registry/argus:1",
        "alembic upgrade head",
        "az containerapp update --name x --image y",
    ],
)
def test_a_preview_that_changes_something_fails(tmp_path: Path, line: str) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        PREVIEW,
        lambda s: s.replace(
            '        run: az bicep install --version "$BICEP_VERSION"\n',
            "        run: |\n"
            '          az bicep install --version "$BICEP_VERSION"\n'
            f"          {line}\n",
        ),
    )
    assert any(p.startswith("infra-preview.yml must not make") for p in check_pipeline.check(root))


def test_a_token_permission_without_a_sign_in_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        PREVIEW,
        lambda s: s.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  id-token: write\n",
            1,
        ),
    )
    assert any("'contents: read'" in p for p in check_pipeline.check(root))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "line",
    [
        "az storage container immutability-policy delete --account-name st --container-name c",
        "az deployment group create --resource-group x --mode Complete --template-file t.json",
    ],
)
def test_getting_around_the_lock_fails(tmp_path: Path, line: str) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace("az acr login", f"{line}\n          az acr login"))
    assert "deploy.yml mentions the locked document store" in check_pipeline.check(root)


def test_deploying_without_the_tip_of_main_check_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(root, DEPLOY, lambda s: s.replace(' --main-tip "$tip"', ""))
    assert any("tip of main" in p for p in check_pipeline.check(root))


@pytest.mark.parametrize("step_id", ["release", "migrate", "smoke"])
def test_a_condition_on_a_deploy_step_fails(tmp_path: Path, step_id: str) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        DEPLOY,
        lambda s: s.replace(
            f"        id: {step_id}\n", f"        id: {step_id}\n        if: always()\n"
        ),
    )
    assert f"deploy.yml step {step_id} may not have its own if condition" in (
        check_pipeline.check(root)
    )


@pytest.mark.req("NF-03")
def test_a_bare_alembic_call_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root, DEPLOY, lambda s: s.replace("python tools/deploy.py migrate", "alembic upgrade head")
    )
    problems = check_pipeline.check(root)
    assert "deploy.yml migrate step must run tools/deploy.py migrate" in problems
    assert any("keeps row data out of the log" in p for p in problems)


def test_a_firewall_rule_left_open_quietly_fails(tmp_path: Path) -> None:
    root = copy_repo(tmp_path)
    edit(
        root,
        DEPLOY,
        lambda s: s.replace(
            '--rule-name "$RULE" --yes\n',
            '--rule-name "$RULE" --yes \\\n            || echo "::warning::left open"\n',
        ),
    )
    assert "deploy.yml must fail when the database firewall cannot be closed" in (
        check_pipeline.check(root)
    )

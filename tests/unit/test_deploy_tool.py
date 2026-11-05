"""The helpers of the deploy pipeline (tools/deploy.py, #81), tested without Azure."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]

CLIENT = "11111111-2222-3333-4444-555555555555"
TENANT = "22222222-3333-4444-5555-666666666666"
SUBSCRIPTION = "33333333-4444-5555-6666-777777777777"
ADMIN = "44444444-5555-6666-7777-888888888888"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_tool", REPO / "tools" / "deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_tool()


def full_config(client_name: str = "AZURE_CLIENT_ID") -> dict[str, str]:
    return {
        client_name: CLIENT,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "ARGUS_RESOURCE_GROUP": "rg-argus",
        "ARGUS_DATABASE_ADMIN_OBJECT_ID": ADMIN,
        "ARGUS_DATABASE_ADMIN_NAME": "id-argus-deploy",
    }


def test_nothing_set_means_azure_is_not_set_up_yet() -> None:
    result = deploy.check_config("deploy", {})

    assert not result.configured
    assert result.problems == []


@pytest.mark.parametrize(
    ("mode", "client_name"),
    [("deploy", "AZURE_CLIENT_ID"), ("preview", "AZURE_PREVIEW_CLIENT_ID")],
)
def test_everything_set_is_ready(mode: str, client_name: str) -> None:
    result = deploy.check_config(mode, full_config(client_name))

    assert result.configured
    assert result.problems == []


def test_the_preview_does_not_use_the_deploy_identity() -> None:
    result = deploy.check_config("preview", full_config("AZURE_CLIENT_ID"))

    assert not result.configured
    assert result.problems == ["GitHub variable AZURE_PREVIEW_CLIENT_ID is not set"]


def test_a_half_finished_setup_names_what_is_missing() -> None:
    config = full_config()
    del config["ARGUS_DATABASE_ADMIN_OBJECT_ID"]
    config["ARGUS_RESOURCE_GROUP"] = "  "

    result = deploy.check_config("deploy", config)

    assert not result.configured
    assert result.problems == [
        "GitHub variable ARGUS_RESOURCE_GROUP is not set",
        "GitHub variable ARGUS_DATABASE_ADMIN_OBJECT_ID is not set",
    ]


@pytest.mark.parametrize("value", ["00000000-0000-0000-0000-000000000000", "argus-deploy"])
def test_the_all_zero_fallback_admin_id_is_refused(value: str) -> None:
    config = full_config()
    config["ARGUS_DATABASE_ADMIN_OBJECT_ID"] = value

    result = deploy.check_config("deploy", config)

    assert not result.configured
    assert result.problems == [
        "GitHub variable ARGUS_DATABASE_ADMIN_OBJECT_ID is not a real Azure ID"
    ]


def test_check_config_writes_the_step_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for name in full_config():
        monkeypatch.delenv(name, raising=False)

    assert deploy.main(["check-config", "--mode", "deploy"]) == 0
    assert output.read_text() == "configured=false\n"
    assert "::notice::" in capsys.readouterr().out

    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT)
    assert deploy.main(["check-config", "--mode", "deploy"]) == 1
    assert output.read_text().splitlines()[-1] == "configured=false"


def test_check_config_says_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for name, value in full_config().items():
        monkeypatch.setenv(name, value)

    assert deploy.main(["check-config", "--mode", "deploy"]) == 0
    assert output.read_text() == "configured=true\n"


def fake_app(
    *, build: str = "abc", database: tuple[int, dict[str, str]] | None = None
) -> httpx.Client:
    answer = database or (
        200,
        {"status": "ok", "role": "argus_app", "revision": "0009", "head": "0009"},
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "build": build})
        if request.url.path == "/health/database":
            return httpx.Response(answer[0], content=json.dumps(answer[1]))
        return httpx.Response(404)

    return httpx.Client(
        base_url="https://argus.example.invalid", transport=httpx.MockTransport(handle)
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run_smoke(client: httpx.Client, build: str = "abc") -> tuple[bool, list[str]]:
    clock = Clock()
    said: list[str] = []
    passed = deploy.smoke(
        client, build, timeout=30, interval=10, clock=clock, sleep=clock.sleep, say=said.append
    )
    return passed, said


def test_smoke_passes_on_the_new_build_with_the_database_at_head() -> None:
    passed, said = run_smoke(fake_app())

    assert passed
    assert said == ["build abc is live, database at 0009, as argus_app"]


def test_smoke_fails_while_the_old_build_still_answers() -> None:
    passed, said = run_smoke(fake_app(build="old"))

    assert not passed
    assert said[-1] == "/health still shows build old, waiting for abc"
    assert len(said) == 4  # tried at 0, 10, 20 and 30 seconds


def test_smoke_fails_when_the_database_is_behind() -> None:
    behind = (503, {"status": "behind", "role": "argus_app", "revision": "0008", "head": "0009"})

    passed, said = run_smoke(fake_app(database=behind))

    assert not passed
    assert "503" in said[-1]


@pytest.mark.req("NF-04")
def test_smoke_fails_when_the_app_works_as_another_role() -> None:
    owner = (200, {"status": "ok", "role": "id-argus-deploy", "revision": "1", "head": "1"})

    passed, said = run_smoke(fake_app(database=owner))

    assert not passed
    assert said[-1] == "the app works as id-argus-deploy, not as argus_app"


def test_smoke_waits_while_the_app_is_not_reachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="https://x.invalid", transport=httpx.MockTransport(refuse))

    passed, said = run_smoke(client)

    assert not passed
    assert said[0] == "/health not reachable yet (ConnectError)"


@pytest.mark.parametrize("object_id", ["", "not-a-guid", "00000000-0000-0000-0000-000000000000"])
def test_the_app_role_needs_a_real_object_id(object_id: str) -> None:
    with pytest.raises(ValueError, match="object ID"):
        deploy.ensure_app_role(None, None, "id-argus-app", object_id)

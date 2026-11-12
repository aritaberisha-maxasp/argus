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


TIP = "a" * 40
OLD = "b" * 40


def test_an_old_commit_is_skipped_and_stays_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {**full_config(), "GITHUB_OUTPUT": str(tmp_path / "output")}

    assert deploy.run_check_config("deploy", OLD, TIP, environ) == 0
    assert (tmp_path / "output").read_text() == "configured=false\n"
    assert "no longer the tip of main" in capsys.readouterr().out


def test_the_tip_of_main_deploys(tmp_path: Path) -> None:
    environ = {**full_config(), "GITHUB_OUTPUT": str(tmp_path / "output")}

    assert deploy.run_check_config("deploy", TIP, TIP, environ) == 0
    assert (tmp_path / "output").read_text() == "configured=true\n"


def test_an_unknown_tip_of_main_fails(tmp_path: Path) -> None:
    environ = {**full_config(), "GITHUB_OUTPUT": str(tmp_path / "output")}

    assert deploy.run_check_config("deploy", TIP, "", environ) == 1
    assert (tmp_path / "output").read_text() == "configured=false\n"


class FakeDiag:
    schema_name = "public"
    table_name = "vendor"
    column_name = None
    constraint_name = "vendor_iban_check"


class FakeDriverError(Exception):
    sqlstate = "23514"
    diag = FakeDiag()


# Built at runtime so nothing here looks like real personal data to a scanner.
PERSON = "Erika " + "Mustermann"
IBAN = "DE" + "00" * 10


def row_data_error() -> Exception:
    from sqlalchemy.exc import IntegrityError

    original = FakeDriverError(
        'new row for relation "vendor" violates check constraint "vendor_iban_check"\n'
        f"DETAIL:  Failing row contains ({PERSON}, {IBAN})."
    )
    return IntegrityError("insert into vendor values (%(name)s)", {"name": PERSON}, original)


@pytest.mark.req("NF-03")
def test_a_failed_migration_logs_codes_and_names_but_no_row_data() -> None:
    said: list[str] = []

    def fail() -> None:
        raise row_data_error()

    assert deploy.migrate(fail, lambda: "0009", said.append) == 1
    log = "\n".join(said)
    assert PERSON not in log
    assert IBAN not in log
    assert "Failing row" not in log
    assert "SQLSTATE 23514" in log
    assert "table vendor" in log
    assert "constraint vendor_iban_check" in log
    assert "after revision 0009" in log


@pytest.mark.req("NF-03")
def test_any_other_migration_error_shows_only_its_type() -> None:
    said: list[str] = []

    def fail() -> None:
        raise ValueError(f"cannot parse {IBAN}")

    assert deploy.migrate(fail, lambda: "0008", said.append) == 1
    assert IBAN not in said[0]
    assert "ValueError" in said[0]


def test_a_good_migration_says_where_the_database_is() -> None:
    said: list[str] = []

    assert deploy.migrate(lambda: None, lambda: "0009", said.append) == 0
    assert said == ["The database is on revision 0009."]


def text_answer(path_with_text: str, status: int, text: str) -> httpx.Client:
    """A fake app where one endpoint answers with a page that is not a JSON object."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == path_with_text:
            return httpx.Response(status, text=text)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "build": "abc"})
        return httpx.Response(
            200, json={"status": "ok", "role": "argus_app", "revision": "1", "head": "1"}
        )

    return httpx.Client(base_url="https://x.invalid", transport=httpx.MockTransport(handle))


@pytest.mark.parametrize(
    ("path", "status", "text", "message"),
    [
        ("/health", 200, "<html>starting</html>", "/health answered 200, not JSON"),
        ("/health", 200, '["not", "an", "object"]', "/health answered 200, not JSON"),
        (
            "/health/database",
            503,
            "upstream connect error",
            "/health/database answered 503, not JSON",
        ),
    ],
)
def test_a_page_that_is_not_json_is_one_more_try(
    path: str, status: int, text: str, message: str
) -> None:
    passed, said = run_smoke(text_answer(path, status, text))

    assert not passed
    assert said == [message] * 4  # kept trying until the deadline, no crash


# Sign in with Microsoft (#133, NF-03).

SIGN_IN = {
    "ARGUS_ENTRA_TENANT_ID": TENANT,
    "ARGUS_ENTRA_API_CLIENT_ID": "55555555-6666-7777-8888-999999999999",
    "ARGUS_ENTRA_GROUP_APPROVER": "66666666-7777-8888-9999-aaaaaaaaaaaa",
    "ARGUS_ENTRA_GROUP_FINANCE": "77777777-8888-9999-aaaa-bbbbbbbbbbbb",
    "ARGUS_ENTRA_GROUP_ADMIN": "88888888-9999-aaaa-bbbb-cccccccccccc",
}


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("mode", ["deploy", "preview"])
def test_all_five_sign_in_ids_set_is_ready_without_a_warning(mode: str) -> None:
    client = "AZURE_CLIENT_ID" if mode == "deploy" else "AZURE_PREVIEW_CLIENT_ID"
    result = deploy.check_config(mode, {**full_config(client), **SIGN_IN})

    assert result.configured
    assert result.problems == []
    assert result.warnings == []


@pytest.mark.req("NF-03")
def test_no_sign_in_ids_still_deploys_but_warns() -> None:
    result = deploy.check_config("deploy", full_config())

    assert result.configured
    assert result.problems == []
    assert len(result.warnings) == 1
    assert "503" in result.warnings[0]
    assert all(name in result.warnings[0] for name in SIGN_IN)


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("missing", list(SIGN_IN))
def test_half_set_sign_in_ids_fail_and_name_what_is_missing(missing: str) -> None:
    config = {**full_config(), **SIGN_IN}
    config[missing] = " "

    result = deploy.check_config("deploy", config)

    assert not result.configured
    assert result.problems == [f"GitHub variable {missing} is not set"]


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("value", ["ARGUS finance", "00000000-0000-0000-0000-000000000000"])
def test_a_sign_in_id_that_is_not_a_real_id_fails(value: str) -> None:
    config = {**full_config(), **SIGN_IN, "ARGUS_ENTRA_GROUP_FINANCE": value}

    result = deploy.check_config("deploy", config)

    assert not result.configured
    assert result.problems == ["GitHub variable ARGUS_ENTRA_GROUP_FINANCE is not a real Entra ID"]


def test_the_sign_in_warning_shows_in_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    for name in SIGN_IN:
        monkeypatch.delenv(name, raising=False)
    for name, value in full_config().items():
        monkeypatch.setenv(name, value)

    assert deploy.main(["check-config", "--mode", "deploy"]) == 0
    assert "::warning::Sign in with Microsoft is not set up" in capsys.readouterr().out


def test_no_sign_in_warning_while_azure_is_not_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    for name in [*full_config(), *SIGN_IN]:
        monkeypatch.delenv(name, raising=False)

    assert deploy.main(["check-config", "--mode", "deploy"]) == 0
    assert "::warning::" not in capsys.readouterr().out


def test_the_workflows_pass_the_five_sign_in_variables() -> None:
    deploy_yml = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    preview_yml = (REPO / ".github" / "workflows" / "infra-preview.yml").read_text(encoding="utf-8")
    bicepparam = (REPO / "infra" / "main.bicepparam").read_text(encoding="utf-8")
    for name in SIGN_IN:
        line = f"{name}: ${{{{ vars.{name} }}}}"
        # Once for the check of the variables, once for main.bicepparam.
        assert deploy_yml.count(line) == 2, name
        assert preview_yml.count(line) == 2, name
        assert f"readEnvironmentVariable('{name}', '')" in bicepparam, name
    assert "readEnvironmentVariable('ARGUS_ENTRA_AUTHORITY'" not in bicepparam


@pytest.mark.req("NF-03")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("ARGUS_ENTRA_GROUP_APPROVER", "ARGUS_ENTRA_GROUP_FINANCE"),
        ("ARGUS_ENTRA_GROUP_APPROVER", "ARGUS_ENTRA_GROUP_ADMIN"),
        ("ARGUS_ENTRA_GROUP_FINANCE", "ARGUS_ENTRA_GROUP_ADMIN"),
    ],
)
def test_one_group_for_two_roles_fails(first: str, second: str) -> None:
    config = {**full_config(), **SIGN_IN}
    # Same group, written in capitals once: Entra IDs are not case sensitive.
    config[second] = config[first].upper()

    result = deploy.check_config("deploy", config)

    assert not result.configured
    assert result.problems == [
        f"GitHub variables {first} and {second} name the same group; "
        "each role needs its own Entra group"
    ]

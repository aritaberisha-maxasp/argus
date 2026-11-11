"""The demo commands: seed the fake mailbox, reset the demo data (#124, ADR 0009).

Covers: IN-01, NF-04
"""

import importlib
import re
from email import message_from_bytes
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.connections import connections_from_env
from argus.demo import (
    DEMO_EMAILS,
    DemoRefused,
    check_demo_database,
    check_demo_documents,
    control_root,
    documents_account,
    reset_standins,
    seed,
)
from argus.demo.__main__ import main
from argus.wise import WiseCallRefused
from standins.graph.app import create_app as create_fake_graph
from standins.graph.app import fake_token
from tests.unit.intake.graph_helpers import standin_transport
from tests.unit.test_connections import DEMO, LIVE

REPO = Path(__file__).resolve().parents[2]
EMAILS = REPO / "tests" / "fixtures" / "emails"
MANIFEST = EMAILS / "manifest.csv"
STANDINS_BICEP = REPO / "infra" / "modules" / "standins.bicep"
PROFILE_ID = 16_000_001


def manifest() -> dict[str, dict[str, str]]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    rows = [dict(zip(header, line.split(","), strict=False)) for line in lines[1:]]
    return {row["file"]: row for row in rows}


@pytest.mark.req("IN-01")
def test_the_demo_set_shows_every_case_the_issue_asks_for() -> None:
    rows = manifest()
    picked = [rows[email.file] for email in DEMO_EMAILS]
    flags = {flag for row in picked for flag in row["flags"].split()}
    categories = {row["expected_category"] for row in picked}
    # Incoming invoices as PDF, scan, XRechnung and ZUGFeRD; an outgoing invoice; a reminder.
    assert {"incoming_invoice", "outgoing_invoice", "payment_reminder", "other"} <= categories
    assert {"scanned", "e_invoice", "duplicate", "wrong_recipient", "spam"} <= flags
    assert any(row["file"].startswith("E08") for row in picked)  # XRechnung
    assert any(row["file"].startswith("E09") for row in picked)  # ZUGFeRD
    assert any("known_sender" in row["flags"] for row in picked)  # marketing mail


def test_the_duplicate_arrives_after_the_invoice_it_repeats() -> None:
    files = [email.file for email in DEMO_EMAILS]
    assert files.index("E15_doppelte-rechnung.eml") > files.index("E01_buerobedarf-rechnung.eml")


def test_every_demo_email_is_a_synthetic_fixture() -> None:
    for email in DEMO_EMAILS:
        raw = (EMAILS / email.file).read_bytes()
        parsed = message_from_bytes(raw)
        addresses = re.findall(r"[\w.+-]+@[\w.-]+", f"{parsed['From']} {parsed['To']}")
        assert addresses
        assert all(a.endswith((".example", ".invalid")) for a in addresses), email.file


def demo_graph() -> tuple[TestClient, str]:
    token = fake_token(["Mail.ReadWrite", "Mail.Send"])
    return TestClient(create_fake_graph(token=token, emails=[])), token


@pytest.mark.req("IN-01")
def test_seed_puts_the_demo_emails_into_the_empty_fake_mailbox() -> None:
    http, token = demo_graph()
    connections = connections_from_env({**DEMO, "ARGUS_GRAPH_STANDIN_TOKEN": token})
    seeded = seed(connections, EMAILS, http)
    assert [email.file for email in seeded] == [email.file for email in DEMO_EMAILS]
    assert http.get("/_standin/records").json()["inbox"] == len(DEMO_EMAILS)

    # ARGUS reads them back through the Graph client built from the settings, as the sweep
    # will (#125).
    graph = connections.graph_client(transport=standin_transport(http.app))  # type: ignore[arg-type]
    assert len(list(graph.list_inbox())) == len(DEMO_EMAILS)


def test_seeding_twice_adds_nothing_new() -> None:
    http, token = demo_graph()
    connections = connections_from_env({**DEMO, "ARGUS_GRAPH_STANDIN_TOKEN": token})
    seed(connections, EMAILS, http)
    seed(connections, EMAILS, http)
    assert http.get("/_standin/records").json()["inbox"] == len(DEMO_EMAILS)


@pytest.mark.req("NF-04")
def test_seed_refuses_when_demo_mode_is_off() -> None:
    http, _ = demo_graph()
    with pytest.raises(DemoRefused, match="demo mode is off"):
        seed(connections_from_env(LIVE), EMAILS, http)
    assert http.get("/_standin/records").json()["inbox"] == 0


def test_seed_refuses_a_missing_email(tmp_path: Path) -> None:
    http, _ = demo_graph()
    with pytest.raises(DemoRefused, match="demo emails not found"):
        seed(connections_from_env(DEMO), tmp_path, http)


def test_seed_refuses_an_email_that_is_not_synthetic(tmp_path: Path) -> None:
    for email in DEMO_EMAILS:
        (tmp_path / email.file).write_bytes((EMAILS / email.file).read_bytes())
    first = tmp_path / DEMO_EMAILS[0].file
    first.write_bytes(first.read_bytes().replace(b"accounts@maxasp.example", b"someone@gmail.com"))
    http, _ = demo_graph()
    with pytest.raises(DemoRefused, match="not addressed to the synthetic test mailbox"):
        seed(connections_from_env(DEMO), tmp_path, http)
    assert http.get("/_standin/records").json()["inbox"] == 0


def test_the_control_calls_sit_at_the_root_of_the_stand_in() -> None:
    assert control_root("http://ca-argus-standin-graph/v1.0") == "http://ca-argus-standin-graph"
    assert control_root("http://localhost:8081/v1.0") == "http://localhost:8081"


@pytest.mark.req("NF-04")
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://argus_owner@localhost:5432/argus",
        "postgresql+psycopg://id-argus-app@psql-argus.postgres.database.azure.com:5432/argus",
        "postgresql+psycopg://argus_owner@localhost:5432/argus_demo_copy",
    ],
)
def test_reset_refuses_any_database_but_a_demo_one(url: str) -> None:
    with pytest.raises(DemoRefused, match="must end in _demo"):
        check_demo_database(url)


def test_reset_accepts_a_demo_database() -> None:
    check_demo_database("postgresql+psycopg://admin@psql-argus.example:5432/argus_demo")


@pytest.mark.req("NF-04")
def test_reset_refuses_the_locked_document_store() -> None:
    real = "DefaultEndpointsProtocol=https;AccountName=stargusdocsabc123;AccountKey=x;"
    with pytest.raises(DemoRefused, match="not the demo one"):
        check_demo_documents(real)
    assert documents_account(real) == "stargusdocsabc123"


def test_reset_accepts_the_demo_store_and_the_local_emulator() -> None:
    check_demo_documents("DefaultEndpointsProtocol=https;AccountName=stargusdemoabc123;")
    check_demo_documents("UseDevelopmentStorage=true")


def test_reset_asks_each_stand_in_to_start_over() -> None:
    called: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        done = reset_standins(connections_from_env(DEMO), http)
    assert done == ["fake Graph", "fake DATEV", "Wise stand in"]
    assert called == [
        "http://ca-argus-standin-graph/_standin/reset",
        "http://ca-argus-standin-datev/_fake/reset",
        "http://ca-argus-standin-wise/_standin/reset",
    ]


def test_reset_of_the_stand_ins_refuses_when_demo_mode_is_off() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http,
        pytest.raises(DemoRefused),
    ):
        reset_standins(connections_from_env(LIVE), http)


@pytest.mark.req("NF-04")
def test_the_command_refuses_to_reset_when_demo_mode_is_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {**LIVE, "ARGUS_DATABASE_URL": "postgresql+psycopg://a@localhost/argus_demo"}
    assert main(["reset"], environ) == 2
    assert "demo mode is off" in capsys.readouterr().err


def test_the_command_checks_every_target_before_it_changes_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {
        **DEMO,
        "ARGUS_DATABASE_URL": "postgresql+psycopg://a@localhost/argus_demo",
        "ARGUS_DOCUMENTS_CONNECTION_STRING": "AccountName=stargusdocsabc;AccountKey=x",
    }
    # The database would be fine, but the store is the locked one, so nothing runs at all.
    assert main(["reset"], environ) == 2
    assert "not the demo one" in capsys.readouterr().err


def test_the_command_refuses_mixed_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["seed"], {**DEMO, "WISE_API_TOKEN": "x" * 20}) == 2
    assert "real secret WISE_API_TOKEN" in capsys.readouterr().err


def test_the_command_needs_something_to_reset(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reset"], DEMO) == 2
    assert "nothing to reset" in capsys.readouterr().err


def test_the_graph_stand_in_starts_empty_for_the_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPH_STANDIN_TOKEN", fake_token(["Mail.ReadWrite"]))
    monkeypatch.setenv("GRAPH_STANDIN_START_EMPTY", "true")
    module = importlib.import_module("standins.graph.app")
    http = TestClient(module.app)
    assert http.get("/_standin/records").json()["inbox"] == 0
    http.post("/_standin/messages", content=(EMAILS / DEMO_EMAILS[0].file).read_bytes())
    assert http.post("/_standin/reset").json() == {"inbox": 0}


def test_the_graph_stand_in_still_starts_with_the_fixtures_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPH_STANDIN_TOKEN", fake_token(["Mail.ReadWrite"]))
    monkeypatch.delenv("GRAPH_STANDIN_START_EMPTY", raising=False)
    module = importlib.import_module("standins.graph.app")
    assert TestClient(module.app).get("/_standin/records").json()["inbox"] == 50


@pytest.mark.req("NF-04")
def test_the_wise_stand_in_in_azure_cannot_fund_or_release_a_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Wise stand in runs in Azure exactly as below: the module and settings that
    infra/modules/standins.bicep starts. ARGUS's client refuses funding before it leaves, and
    the stand in has no funding or release endpoint at all, so even a raw call gets 404."""
    bicep = STANDINS_BICEP.read_text(encoding="utf-8")
    assert "'standins.wise.app:app'" in bicep
    assert "WISE_STANDIN_TOKEN" in bicep

    token = "standin-" + "z" * 24
    monkeypatch.setenv("WISE_STANDIN_TOKEN", token)
    standin = TestClient(importlib.import_module("standins.wise.app").create_app())

    def forward(request: httpx.Request) -> httpx.Response:
        answer = standin.request(
            request.method, request.url.path, content=request.read(), headers=request.headers
        )
        return httpx.Response(answer.status_code, content=answer.content)

    settings = {**DEMO, "ARGUS_WISE_STANDIN_TOKEN": token}
    with connections_from_env(settings).wise_client(httpx.MockTransport(forward)) as wise:
        group = wise.create_batch_group("Demo run", "EUR")
        group = wise.get_batch_group(group["id"])
        assert wise.complete_batch_group(group["id"], group["version"])["status"] == "COMPLETED"
        funding = f"/v3/profiles/{PROFILE_ID}/batch-" + f"payments/{group['id']}/" + "payments"
        with pytest.raises(WiseCallRefused):
            wise._send("POST", funding, {"type": "BALANCE"})

    for path in (
        funding,
        f"/v3/profiles/{PROFILE_ID}/transfers/1/" + "payments",
        "/v1/transfers/1/" + "payments",
        f"/v3/profiles/{PROFILE_ID}/batch-groups/{group['id']}/fund",
        f"/v3/profiles/{PROFILE_ID}/batch-groups/{group['id']}/release",
    ):
        answer = standin.post(
            path, json={"type": "BALANCE"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert answer.status_code in (404, 405), path


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_seed_waits_until_the_fake_mailbox_answers() -> None:
    standin, token = demo_graph()
    tries = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_standin/records" and tries["n"] < 3:
            tries["n"] += 1
            if tries["n"] == 1:
                raise httpx.ConnectError("starting")
            return httpx.Response(503, text="upstream connect error")
        reply = standin.request(request.method, request.url.path, content=request.read())
        return httpx.Response(reply.status_code, content=reply.content)

    clock = Clock()
    connections = connections_from_env({**DEMO, "ARGUS_GRAPH_STANDIN_TOKEN": token})
    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        seeded = seed(connections, EMAILS, http, clock=clock, sleep=clock.sleep)
    assert len(seeded) == len(DEMO_EMAILS)
    assert clock.now == 15.0


def test_seed_gives_up_when_the_fake_mailbox_never_answers() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nobody home")

    clock = Clock()
    with (
        httpx.Client(transport=httpx.MockTransport(down)) as http,
        pytest.raises(DemoRefused, match="did not answer within 60 seconds"),
    ):
        seed(connections_from_env(DEMO), EMAILS, http, timeout=60, clock=clock, sleep=clock.sleep)
    assert clock.now <= 60


def test_seed_does_not_wait_on_something_that_is_not_the_fake_mailbox() -> None:
    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    clock = Clock()
    with (
        httpx.Client(transport=httpx.MockTransport(other)) as http,
        pytest.raises(httpx.HTTPStatusError),
    ):
        seed(connections_from_env(DEMO), EMAILS, http, clock=clock, sleep=clock.sleep)
    assert clock.now == 0

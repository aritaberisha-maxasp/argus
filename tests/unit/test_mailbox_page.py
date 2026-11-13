"""The fake mailbox page for the demo (#135, IN-04, IN-06, NF-03).

Demo mode on: finance and admin see every folder of the fake mailbox. Demo mode off: the page
and its data do not exist. Nothing about an email goes into the log.
"""

import logging
from collections.abc import Iterator
from email import message_from_bytes
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus.app import PUBLIC_PATHS, create_app
from argus.auth import Role
from argus.connections import connections_from_env
from argus.intake.filing import Folders
from argus.intake.graph import GraphCallRefused, GraphClient, allowed_paths
from argus.mailbox_page.web import DATA_PATH, PAGE_PATHS, STATIC
from standins.entra.app import FakeEntra
from standins.graph.app import create_app as create_fake_graph
from standins.graph.app import fake_token
from tests.unit.intake.graph_helpers import client_for, standin_transport

pytestmark = pytest.mark.req("IN-04", "IN-06", "NF-03")

EMAILS = Path(__file__).resolve().parents[1] / "fixtures" / "emails"
TOKEN = fake_token(["Mail.ReadWrite", "Mail.Send"])
DEMO = {
    "ARGUS_DEMO": "true",
    "ARGUS_GRAPH_URL": "http://ca-argus-standin-graph/v1.0",
    "ARGUS_MAILBOX": "accounts@maxasp.example",
    "ARGUS_DATEV_URL": "http://ca-argus-standin-datev",
    "ARGUS_DATEV_CLIENT": "muster-gmbh",
    "ARGUS_WISE_URL": "http://ca-argus-standin-wise",
    "ARGUS_WISE_PROFILE_ID": "16000001",
    "ARGUS_GRAPH_STANDIN_TOKEN": TOKEN,
    "ARGUS_WISE_STANDIN_TOKEN": "standin-" + "y" * 24,
}
SETTINGS = (*DEMO, "WISE_API_TOKEN", "ARGUS_REMINDER_FORWARD_TO")

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up

MARKETING = "E32"
REMINDER = "E25"


def no_database() -> Engine:
    raise RuntimeError("these tests have no database")


def fixture(prefix: str) -> bytes:
    return next(EMAILS.glob(f"{prefix}*.eml")).read_bytes()


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def no_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in DEMO.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def fake_graph() -> TestClient:
    """The fake mailbox after a sweep: one invoice filed, one marketing mail in Removed and a
    reminder still in the Inbox."""
    http = TestClient(
        create_fake_graph(
            token=TOKEN, emails=[fixture("E01"), fixture(MARKETING), fixture(REMINDER)]
        )
    )
    graph = demo_graph_for(http)()
    folders = Folders(graph)
    ids = {m.internet_message_id: m.id for m in graph.list_inbox()}
    by_file = {}
    for prefix in ("E01", MARKETING):
        by_file[prefix] = ids[message_from_bytes(fixture(prefix))["Message-ID"]]
    graph.move(by_file["E01"], folders.id_of("ARGUS/Incoming invoices"))
    graph.move(by_file[MARKETING], folders.id_of("ARGUS/Removed"))
    return http


def demo_graph_for(http: TestClient):  # type: ignore[no-untyped-def]
    connections = connections_from_env(DEMO)
    return lambda: connections.graph_client(transport=standin_transport(http.app))  # type: ignore[arg-type]


@pytest.fixture
def client(demo: None, entra: FakeEntra, fake_graph: TestClient) -> Iterator[TestClient]:
    app = create_app(no_database, sign_in=entra.verifier, demo_graph=demo_graph_for(fake_graph))
    yield TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Demo mode only


def test_without_demo_mode_the_page_does_not_exist(no_demo: None, entra: FakeEntra) -> None:
    client = TestClient(create_app(no_database, sign_in=entra.verifier))
    admin = bearer(entra.token(ARITA, roles=[Role.ADMIN]))

    for path in (*PAGE_PATHS, DATA_PATH):
        assert client.get(path).status_code == 404, path
        assert client.get(path, headers=admin).status_code == 404, path


def test_with_demo_mode_live_settings_never_open_it(entra: FakeEntra) -> None:
    # The app reads the demo switch from the settings it starts with; live settings have none.
    with pytest.MonkeyPatch.context() as patch:
        for name in SETTINGS:
            patch.delenv(name, raising=False)
        patch.setenv("ARGUS_DEMO", "false")
        app = create_app(no_database, sign_in=entra.verifier)
    assert DATA_PATH not in {r.path for r in app.routes if isinstance(r, APIRoute)}


# Who may see it


@pytest.mark.parametrize("role", [Role.FINANCE, Role.ADMIN])
def test_finance_and_admin_see_the_mailbox(
    entra: FakeEntra, client: TestClient, role: Role
) -> None:
    response = client.get(DATA_PATH, headers=bearer(entra.token(BENJAMIN, roles=[role])))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_an_approver_does_not_see_the_mailbox(entra: FakeEntra, client: TestClient) -> None:
    response = client.get(DATA_PATH, headers=bearer(entra.token(ALICE, roles=[Role.APPROVER])))

    assert response.status_code == 403


def test_without_a_token_or_a_role_there_is_no_mailbox(
    entra: FakeEntra, client: TestClient
) -> None:
    assert client.get(DATA_PATH).status_code == 401
    assert client.get(DATA_PATH, headers=bearer(entra.token(ALICE, roles=[]))).status_code == 403


def test_in_demo_mode_every_other_route_still_needs_sign_in(client: TestClient) -> None:
    open_paths = PUBLIC_PATHS | PAGE_PATHS
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.path in open_paths:
            continue
        path = route.path
        for name in ("case_id", "exception_id", "document_id"):
            path = path.replace("{" + name + "}", "1")
        method = sorted(route.methods or {"GET"})[0]
        assert client.request(method, path).status_code == 401, route.path


# What it shows


def test_it_lists_every_folder_inbox_first_with_subject_sender_and_time(
    entra: FakeEntra, client: TestClient
) -> None:
    body = client.get(DATA_PATH, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE])))
    data = body.json()
    folders = {f["path"]: f for f in data["folders"]}

    assert data["mailbox"] == "accounts@maxasp.example"
    assert [f["path"] for f in data["folders"]] == [
        "Inbox",
        "ARGUS",
        "ARGUS/Incoming invoices",
        "ARGUS/Removed",
        "Deleted Items",
    ]
    assert len(folders["Inbox"]["emails"]) == 1
    assert folders["ARGUS"]["emails"] == []
    invoice = folders["ARGUS/Incoming invoices"]["emails"][0]
    assert invoice["subject"]
    assert invoice["sender_address"].endswith(".example")
    assert invoice["received_at"].startswith("20")
    assert len(folders["ARGUS/Removed"]["emails"]) == 1
    assert folders["Deleted Items"]["emails"] == []


def test_reading_the_mailbox_changes_nothing_in_it(
    entra: FakeEntra, client: TestClient, fake_graph: TestClient
) -> None:
    box = fake_graph.app.state.mailbox  # type: ignore[attr-defined]
    before = (dict(box.location), list(box.moves), dict(box.folders), dict(box.drafts))
    response = client.get(DATA_PATH, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE])))

    assert response.status_code == 200
    assert (box.location, box.moves, box.folders, box.drafts) == before


def test_nothing_about_the_emails_goes_into_the_log(
    entra: FakeEntra, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    data = client.get(DATA_PATH, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE]))).json()
    logged = "\n".join(record.getMessage() for record in caplog.records)

    emails = [e for f in data["folders"] for e in f["emails"]]
    assert emails
    for email in emails:
        assert email["subject"] not in logged
        assert email["sender_address"] not in logged
        if email["sender_name"]:
            assert email["sender_name"] not in logged
    assert "looked at the fake mailbox" in logged


def test_when_the_fake_mailbox_cannot_be_read_the_page_says_so(
    demo: None, entra: FakeEntra, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> GraphClient:
        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"code": "Secret subject in error"}})

        return connections_from_env(DEMO).graph_client(transport=httpx.MockTransport(fail))

    client = TestClient(create_app(no_database, sign_in=entra.verifier, demo_graph=broken))
    caplog.set_level(logging.DEBUG)
    response = client.get(DATA_PATH, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE])))

    assert response.status_code == 503
    assert "Secret subject" not in response.text
    assert "Secret subject" not in "\n".join(r.getMessage() for r in caplog.records)


# No new way into the real mailbox


def test_folder_listings_are_refused_against_the_real_graph() -> None:
    graph = client_for(create_fake_graph(token=TOKEN))
    with pytest.raises(GraphCallRefused):
        graph.folder_messages("AAMkAGInbox0000")

    path = "/v1.0/users/accounts@maxasp.example/mailFolders/AAMkAGInbox0000/messages"
    live = allowed_paths("/v1.0", "accounts@maxasp.example")
    demo = allowed_paths("/v1.0", "accounts@maxasp.example", standin=True)
    assert not any(m == "GET" and p.fullmatch(path) for m, p in live)
    assert any(m == "GET" and p.fullmatch(path) for m, p in demo)


# The page


def test_the_page_is_a_fixed_file_with_its_own_scripts_only(
    entra: FakeEntra, client: TestClient
) -> None:
    for path in PAGE_PATHS:
        anonymous = client.get(path)
        signed_in = client.get(path, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE])))
        assert anonymous.status_code == 200, path
        assert anonymous.content == signed_in.content, path
    page = client.get("/mailbox")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "<script>" not in page.text
    assert 'name="argus-sign-in-mode"' in page.text


def test_the_script_puts_server_data_on_the_page_as_text_only() -> None:
    script = (STATIC / "mailbox.js").read_text(encoding="utf-8")

    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script

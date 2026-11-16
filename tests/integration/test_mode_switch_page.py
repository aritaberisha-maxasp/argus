"""Shadow or live mode on the queue page (#134, IN-01, NF-01, NF-03).

The page shows the mode to finance and admin, and lets the admin switch it with a reason. It
uses the same ``PUT /intake/mode`` as before (#72), so the database still writes the switch
into the audit log. Every name and address below is made up.
"""

import logging
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.auth import Role
from argus.intake.mode import IntakeMode, is_live
from argus.intake.records import EmailStep
from argus.intake.sweep import DocumentArchive, sweep
from standins.entra.app import FakeEntra
from tests.integration.conftest import set_mode
from tests.integration.test_email_sorting import (
    INVOICE,
    REVIEW,
    CountingModel,
    open_exceptions,
    record_of,
    run,
)
from tests.integration.test_reminder_forward import (
    clean,  # noqa: F401  wipes forwards and email records around each test
    fake_graph_with,
)

pytestmark = pytest.mark.req("IN-01", "NF-01", "NF-03")

ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up
NAME = "Arita Beispiel"  # made up
ADDRESS = "arita.beispiel@maxasp.example"  # made up
REASON = "Demo fuer Benjamin am Dienstag"


@pytest.fixture
def shadow(owner: Engine) -> Iterator[None]:
    set_mode(owner, IntakeMode.SHADOW)
    yield
    set_mode(owner, IntakeMode.SHADOW)


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    return TestClient(create_app(lambda: app, sign_in=entra.verifier))


def as_role(entra: FakeEntra, who: str, role: Role | None) -> dict[str, str]:
    token = entra.token(who, roles=[role] if role else [], name=NAME, sign_in_address=ADDRESS)
    return {"Authorization": f"Bearer {token}"}


def switch(client: TestClient, headers: dict[str, str], mode: str) -> httpx.Response:
    response: httpx.Response = client.put(
        "/intake/mode", json={"mode": mode, "reason": REASON}, headers=headers
    )
    return response


def switches_logged(app: Engine) -> int:
    with app.connect() as connection:
        found: int = connection.execute(
            text("select count(*) from audit_event where event_type = 'intake_mode_changed'")
        ).scalar_one()
        return found


# AC: the page shows the current mode, shadow or live, to finance and admin.


@pytest.mark.usefixtures("shadow")
@pytest.mark.parametrize("role", [Role.FINANCE, Role.ADMIN])
def test_finance_and_admin_see_the_mode(
    app: Engine, owner: Engine, client: TestClient, entra: FakeEntra, role: Role
) -> None:
    headers = as_role(entra, ARITA if role is Role.ADMIN else BENJAMIN, role)

    shadow = client.get("/intake/mode", headers=headers)
    set_mode(owner, IntakeMode.LIVE)
    live = client.get("/intake/mode", headers=headers)

    assert shadow.status_code == live.status_code == 200
    assert shadow.json()["mode"] == "shadow"
    assert live.json()["mode"] == "live"
    assert shadow.json()["may_switch"] is (role is Role.ADMIN)


# AC: an admin can switch on the page and must give a reason; PUT /intake/mode does the work
# and writes the audit event.


@pytest.mark.usefixtures("shadow")
def test_the_admin_switches_with_a_reason_and_the_switch_is_logged(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    headers = as_role(entra, ARITA, Role.ADMIN)
    before = switches_logged(app)

    to_live = switch(client, headers, "live")

    assert to_live.status_code == 200
    assert to_live.json() == {"mode": "live", "changed": True}
    assert client.get("/intake/mode", headers=headers).json() == {
        "mode": "live",
        "may_switch": True,
    }
    assert switches_logged(app) == before + 1
    no_reason = client.put("/intake/mode", json={"mode": "shadow"}, headers=headers)
    assert no_reason.status_code == 422
    assert is_live(app)
    assert switch(client, headers, "shadow").json() == {"mode": "shadow", "changed": True}
    assert switches_logged(app) == before + 2


# AC: a finance person sees the mode but cannot switch it.


@pytest.mark.usefixtures("shadow")
def test_finance_sees_the_mode_but_cannot_switch_it(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    headers = as_role(entra, BENJAMIN, Role.FINANCE)
    before = switches_logged(app)

    seen = client.get("/intake/mode", headers=headers)
    refused = switch(client, headers, "live")

    assert seen.json() == {"mode": "shadow", "may_switch": False}
    assert refused.status_code == 403
    assert not is_live(app)
    assert switches_logged(app) == before


# AC: a person without a role sees nothing. An approver neither: the mode is not theirs to know.


@pytest.mark.usefixtures("shadow")
@pytest.mark.parametrize("role", [None, Role.APPROVER])
def test_a_person_without_a_role_or_an_approver_sees_no_mode(
    app: Engine, client: TestClient, entra: FakeEntra, role: Role | None
) -> None:
    headers = as_role(entra, ALICE, role)

    seen = client.get("/intake/mode", headers=headers)
    refused = switch(client, headers, "live")

    assert seen.status_code == 403
    assert "shadow" not in seen.text and "live" not in seen.text
    assert refused.status_code == 403
    assert not is_live(app)


@pytest.mark.usefixtures("shadow")
def test_without_sign_in_there_is_no_mode(client: TestClient) -> None:
    assert client.get("/intake/mode").status_code == 401
    assert client.put("/intake/mode", json={"mode": "live", "reason": REASON}).status_code == 401


# AC: no personal data in the logs (NF-03).


@pytest.mark.usefixtures("shadow")
def test_the_switch_logs_no_name_address_or_reason(
    client: TestClient, entra: FakeEntra, caplog: pytest.LogCaptureFixture
) -> None:
    headers = as_role(entra, ARITA, Role.ADMIN)

    with caplog.at_level(logging.DEBUG):
        client.get("/intake/mode", headers=headers)
        switch(client, headers, "live")
        switch(client, headers, "shadow")

    assert "switched to live mode" in caplog.text
    for personal in (NAME, ADDRESS, REASON, "Arita", "maxasp.example"):
        assert personal not in caplog.text


# The demo: after a reset ARGUS starts in shadow mode and the sweep may already have proposed a
# route for the seeded emails. Once the admin switches to live, the next sweep sorts the emails
# still in the Inbox for real, so the queue fills without seeding again.


@pytest.mark.req("IN-04", "IN-09", "EX-01")
@pytest.mark.usefixtures("clean", "shadow")
def test_emails_proposed_in_shadow_mode_are_sorted_once_switched_to_live(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    fake = fake_graph_with(INVOICE, REVIEW)
    documents, sorting, graph = run(fake, CountingModel())
    in_shadow = sweep(app, graph, DocumentArchive(documents), sorting)
    assert (in_shadow.proposed, in_shadow.classified) == (2, 0)

    assert switch(client, as_role(entra, ARITA, Role.ADMIN), "live").status_code == 200
    in_live = sweep(app, graph, DocumentArchive(documents), sorting)

    assert (in_live.classified, in_live.routed, in_live.failed) == (2, 2, 0)
    review = record_of(app, graph, REVIEW)
    assert review.step is not EmailStep.STORED
    assert len(open_exceptions(app, review.id)) == 1
    queue = client.get("/exceptions", headers=as_role(entra, BENJAMIN, Role.FINANCE))
    # Other tests may leave exceptions of their own; this email must be in the queue now.
    assert review.id in [row["email_id"] for row in queue.json()["exceptions"]]

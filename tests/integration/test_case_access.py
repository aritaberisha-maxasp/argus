"""Who may open which case, against a real PostgreSQL (#23, NF-03).

The app reads with the rights of the application's database user, argus_app. A small stand in
tells the app the approvers of each case, so these tests do not depend on the approver table.
Without one the app asks the approver table (#34); test_approver_table.py covers that.
"""

from collections.abc import Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine

from argus.app import create_app
from argus.auth import Role
from standins.entra.app import FakeEntra
from tests.integration.test_case_store import new_case

pytestmark = pytest.mark.req("NF-03", "NF-05")

ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up
BOB = "22222222-2222-4222-8222-222222222222"  # another approver, made up
BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up


class KnownApprovers:
    def __init__(self, approvers: Mapping[int, frozenset[str]]) -> None:
        self._approvers = approvers

    def approvers_of(self, connection: Connection, case_id: int) -> frozenset[str]:
        return self._approvers.get(case_id, frozenset())


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def cases(app: Engine) -> tuple[int, int]:
    return new_case(app), new_case(app)


def client_for(app: Engine, entra: FakeEntra, approvers: KnownApprovers | None) -> TestClient:
    return TestClient(create_app(lambda: app, sign_in=entra.verifier, case_approvers=approvers))


def get(client: TestClient, entra: FakeEntra, who: str, role: Role, case_id: int) -> httpx.Response:
    token = entra.token(who, roles=[role])
    response: httpx.Response = client.get(
        f"/cases/{case_id}", headers={"Authorization": f"Bearer {token}"}
    )
    return response


def test_an_approver_cannot_open_another_approvers_invoice(
    app: Engine, entra: FakeEntra, cases: tuple[int, int]
) -> None:
    alices, bobs = cases
    client = client_for(
        app, entra, KnownApprovers({alices: frozenset({ALICE}), bobs: frozenset({BOB})})
    )

    own = get(client, entra, ALICE, Role.APPROVER, alices)
    other = get(client, entra, ALICE, Role.APPROVER, bobs)
    missing = get(client, entra, ALICE, Role.APPROVER, 999_999_999)

    assert own.status_code == 200
    assert own.json()["id"] == alices
    assert own.json()["status"] == "received"
    # Bob's invoice answers exactly like one that does not exist.
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json() == {"detail": "no such case"}


def test_a_deputy_sees_the_case_while_standing_in(
    app: Engine, entra: FakeEntra, cases: tuple[int, int]
) -> None:
    _, bobs = cases
    client = client_for(app, entra, KnownApprovers({bobs: frozenset({BOB, ALICE})}))

    assert get(client, entra, ALICE, Role.APPROVER, bobs).status_code == 200


@pytest.mark.parametrize(("who", "role"), [(BENJAMIN, Role.FINANCE), (ARITA, Role.ADMIN)])
def test_finance_and_admin_see_every_case(
    app: Engine, entra: FakeEntra, cases: tuple[int, int], who: str, role: Role
) -> None:
    alices, bobs = cases
    client = client_for(
        app, entra, KnownApprovers({alices: frozenset({ALICE}), bobs: frozenset({BOB})})
    )

    for case_id in cases:
        response = get(client, entra, who, role, case_id)
        assert response.status_code == 200
        assert response.json()["id"] == case_id


def test_an_approver_of_no_vendor_sees_no_case(
    app: Engine, entra: FakeEntra, cases: tuple[int, int]
) -> None:
    client = client_for(app, entra, None)

    for case_id in cases:
        assert get(client, entra, ALICE, Role.APPROVER, case_id).status_code == 404
    assert get(client, entra, BENJAMIN, Role.FINANCE, cases[0]).status_code == 200


def test_no_sign_in_no_case(app: Engine, entra: FakeEntra, cases: tuple[int, int]) -> None:
    client = client_for(app, entra, None)

    response = client.get(f"/cases/{cases[0]}")

    assert response.status_code == 401
    assert "id" not in response.json()

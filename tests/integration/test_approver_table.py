"""The approver table against a real PostgreSQL (#34, migration 0015).

The app works with the rights of argus_app. Every test uses its own made up people and vendor
accounts, because the table cannot be emptied between tests without breaking its audit trail.
Only the CSV test uses the fixture accounts 70001 to 70007.
"""

import itertools
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import create_app
from argus.approvers import (
    ApproverTable,
    ChangedBy,
    approvers_for_vendor,
    direct_debit_of,
    load_approvers_csv,
    remove_approver,
    remove_vendor_approver,
    save_approver,
    set_absent,
    set_vendor_approver,
    vendor_approver,
)
from argus.auth import Role
from argus.cases.model import Actor, CaseKind
from argus.cases.store import (
    CaseRuleBroken,
    DuplicateRefused,
    create_case,
    record_event,
    unit_of_work,
)
from standins.entra.app import FakeEntra
from tests.integration.test_case_store import new_case

pytestmark = pytest.mark.req("AP-01", "AP-06", "PY-13", "VM-02", "NF-01")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "master-data"
ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up
BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ADMIN = ChangedBy(Actor.user(ARITA), "Arita Test", "arita@maxasp.example")

# Vendor accounts nobody else uses: 9 digits, starting at 810000000.
_accounts = itertools.count(810_000_000 + uuid.uuid4().int % 1_000_000 * 100)


def account() -> str:
    return str(next(_accounts))


def person(app: Engine, name: str = "Approver Test") -> str:
    object_id = str(uuid.uuid4())
    assert save_approver(app, ADMIN, object_id, name, f"{object_id[:8]}@maxasp.example")
    return object_id


def events(app: Engine, event_type: str, key: str, value: str) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select actor_type, actor, details from audit_event "
                "where event_type = :type and details->>:key = :value order by chain_seq"
            ),
            {"type": event_type, "key": key, "value": value},
        ).all()
    return [{"actor": f"{r.actor_type}:{r.actor}", **r.details} for r in rows]


# ---------------------------------------------------------------------------- the table


def test_approvers_load_from_the_fixture_csv(app: Engine) -> None:
    loaded = load_approvers_csv(app, FIXTURES / "approvers.csv", ADMIN)
    # Loading again changes nothing.
    load_approvers_csv(app, FIXTURES / "approvers.csv", ADMIN)

    anna, lea = "a0000000-0000-4000-8000-000000000002", "a0000000-0000-4000-8000-000000000004"
    with app.connect() as connection:
        row = vendor_approver(connection, "70001")
        assert loaded == 7
        assert row is not None
        assert (row.approver_id, row.deputy_id, row.direct_debit) == (anna, lea, False)
        assert approvers_for_vendor(connection, "70001") == {anna}
        # The rent is paid by direct debit and never goes into a payment run (PY-13).
        assert direct_debit_of(connection, "70003") is True
        assert direct_debit_of(connection, "70001") is False
        # 70008 is the vendor without an approver on purpose (VM-02).
        assert vendor_approver(connection, "70008") is None
        assert approvers_for_vendor(connection, "70008") == frozenset()
        assert direct_debit_of(connection, "70008") is None
    assert len(events(app, "vendor_approver_set", "vendor_account", "70001")) == 1


def test_an_admin_adds_changes_and_removes_a_vendors_approver(app: Engine) -> None:
    first, second, deputy = person(app), person(app), person(app)
    vendor = account()

    assert set_vendor_approver(app, ADMIN, vendor, first, None, False)
    assert set_vendor_approver(app, ADMIN, vendor, second, deputy, True)
    assert not set_vendor_approver(app, ADMIN, vendor, second, deputy, True)  # nothing new
    with app.connect() as connection:
        row = vendor_approver(connection, vendor)
        assert row is not None
        assert (row.approver_id, row.deputy_id, row.direct_debit) == (second, deputy, True)

    assert remove_vendor_approver(app, ADMIN, vendor)
    assert not remove_vendor_approver(app, ADMIN, vendor)
    with app.connect() as connection:
        assert approvers_for_vendor(connection, vendor) == frozenset()

    logged = events(app, "vendor_approver_set", "vendor_account", vendor)
    assert len(logged) == 2  # the change that changed nothing is not logged
    assert logged[1]["approver"] == {"old": first, "new": second}
    assert logged[1]["deputy"] == {"old": None, "new": deputy}
    assert logged[1]["direct_debit"] == {"old": False, "new": True}
    assert all(e["actor"] == f"user:{ARITA}" and e["name"] == "Arita Test" for e in logged)
    assert logged[0]["sign_in_address"] == "arita@maxasp.example"
    (removed,) = events(app, "vendor_approver_removed", "vendor_account", vendor)
    assert removed["approver"] == second
    assert removed["direct_debit"] is True
    assert removed["actor"] == f"user:{ARITA}"


def test_a_vendor_has_only_one_approver(app: Engine) -> None:
    vendor = account()
    set_vendor_approver(app, ADMIN, vendor, person(app), None, False)

    with pytest.raises(DuplicateRefused), unit_of_work(app, ADMIN.actor) as db:
        db.execute(text("select set_config('argus.changed_by_name', 'Arita Test', true)"))
        db.execute(
            text("insert into vendor_approver (vendor_account, approver_id) values (:v, :a)"),
            {"v": vendor, "a": person(app)},
        )


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ("deputy_is_approver", "the deputy cannot be the approver as well"),
        ("unknown_approver", "add the approver and the deputy to the approver table first"),
        ("unknown_deputy", "add the approver and the deputy to the approver table first"),
    ],
)
def test_the_database_refuses_a_wrong_vendor_row(app: Engine, change: str, refusal: str) -> None:
    known = person(app)
    approver, deputy = {
        "deputy_is_approver": (known, known),
        "unknown_approver": (str(uuid.uuid4()), None),
        "unknown_deputy": (known, str(uuid.uuid4())),
    }[change]

    with pytest.raises(CaseRuleBroken, match=refusal):
        set_vendor_approver(app, ADMIN, account(), approver, deputy, False)


@pytest.mark.parametrize("vendor", ["", "0700", "07000", "70001a", "1234567890"])
def test_a_vendor_account_is_a_datev_account_number(app: Engine, vendor: str) -> None:
    with pytest.raises(CaseRuleBroken):
        set_vendor_approver(app, ADMIN, vendor, person(app), None, False)


def test_a_person_still_in_use_cannot_be_removed(app: Engine) -> None:
    approver, deputy, unused = person(app), person(app), person(app)
    set_vendor_approver(app, ADMIN, account(), approver, deputy, False)

    for in_use in (approver, deputy):
        with pytest.raises(CaseRuleBroken, match="still approver or deputy"):
            remove_approver(app, ADMIN, in_use)
    assert remove_approver(app, ADMIN, unused)
    assert not remove_approver(app, ADMIN, unused)
    (removed,) = events(app, "approver_removed", "approver", unused)
    assert removed["actor"] == f"user:{ARITA}"


def test_changing_a_persons_name_or_address_is_logged_with_old_and_new(app: Engine) -> None:
    object_id = person(app, "Old Name")
    first_address = f"{object_id[:8]}@maxasp.example"

    assert save_approver(app, ADMIN, object_id, "  New Name ", "NEW@maxasp.example")
    assert not save_approver(app, ADMIN, object_id, "New Name", "new@maxasp.example")
    assert save_approver(app, ADMIN, object_id, "New Name", "third@maxasp.example")

    (added,) = events(app, "approver_added", "approver", object_id)
    first, second = events(app, "approver_changed", "approver", object_id)
    assert added["approver_name"] == "Old Name"
    assert added["email"] == first_address
    assert first["approver_name"] == {"old": "Old Name", "new": "New Name"}
    # Where approval requests go must stay traceable, even after a later change.
    assert first["email"] == {"old": first_address, "new": "new@maxasp.example"}
    assert second["email"] == {"old": "new@maxasp.example", "new": "third@maxasp.example"}
    assert first["actor"] == second["actor"] == f"user:{ARITA}"


# ---------------------------------------------------------------------------- absence (AP-06)


def test_while_the_approver_is_away_the_deputy_decides_too(app: Engine) -> None:
    approver, deputy = person(app), person(app)
    vendor = account()
    set_vendor_approver(app, ADMIN, vendor, approver, deputy, False)

    assert set_absent(app, ADMIN, approver, True)
    assert set_absent(app, ADMIN, approver, True) is False  # already away, nothing logged
    with app.connect() as connection:
        assert approvers_for_vendor(connection, vendor) == {approver, deputy}
        since: object = connection.execute(
            text("select absent_since from approver where object_id = :id"), {"id": approver}
        ).scalar_one()
        assert since is not None

    assert set_absent(app, ADMIN, approver, False)
    with app.connect() as connection:
        assert approvers_for_vendor(connection, vendor) == {approver}
    assert set_absent(app, ADMIN, str(uuid.uuid4()), True) is None

    logged = events(app, "approver_absence_changed", "approver", approver)
    assert [e["absent"] for e in logged] == [True, False]
    assert all(e["actor"] == f"user:{ARITA}" and e["name"] == "Arita Test" for e in logged)


def test_an_absent_approver_without_a_deputy_still_decides_alone(app: Engine) -> None:
    approver = person(app)
    vendor = account()
    set_vendor_approver(app, ADMIN, vendor, approver, None, False)
    set_absent(app, ADMIN, approver, True)

    with app.connect() as connection:
        assert approvers_for_vendor(connection, vendor) == {approver}


# ---------------------------------------------------------------------------- who may change it


def test_a_job_cannot_change_the_approver_table(app: Engine) -> None:
    job = ChangedBy(Actor.system("intake"), "Intake")
    with pytest.raises(PermissionError):
        save_approver(app, job, str(uuid.uuid4()), "Someone", "someone@maxasp.example")

    # Not even around the Python check: the database refuses it.
    with pytest.raises(CaseRuleBroken, match="only a person"), unit_of_work(app, job.actor) as db:
        db.execute(text("select set_config('argus.changed_by_name', 'Intake', true)"))
        db.execute(
            text("insert into approver (object_id, name, email) values (:id, 'X', 'x@y.example')"),
            {"id": str(uuid.uuid4())},
        )


def test_a_change_without_the_persons_name_is_refused(app: Engine) -> None:
    nameless = ChangedBy(Actor.user(ARITA), "   ")
    with pytest.raises(CaseRuleBroken, match="needs the person's name"):
        save_approver(app, nameless, str(uuid.uuid4()), "Someone", "someone@maxasp.example")


def test_the_app_cannot_write_the_approver_events_itself(app: Engine) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, ADMIN.actor) as db,
    ):
        record_event(db, "vendor_approver_set", details={"vendor_account": "70001"})


def test_the_app_cannot_set_the_times_the_database_sets(app: Engine) -> None:
    object_id = person(app)
    with pytest.raises(CaseRuleBroken), unit_of_work(app, ADMIN.actor) as db:
        db.execute(
            text("update approver set absent_since = now() where object_id = :id"),
            {"id": object_id},
        )


# ---------------------------------------------------------------------------- the routes


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    return TestClient(create_app(lambda: app, sign_in=entra.verifier))


def call(
    client: TestClient,
    entra: FakeEntra,
    who: str,
    role: Role | None,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    token = entra.token(who, roles=[role] if role else [], name="Arita Test")
    response: httpx.Response = client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    return response


def test_the_admin_keeps_the_table_through_the_routes(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    approver, deputy = str(uuid.uuid4()), str(uuid.uuid4())
    vendor = account()

    def admin(method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return call(client, entra, ARITA, Role.ADMIN, method, path, body)

    for object_id, name in ((approver, "Anna Route"), (deputy, "Lea Route")):
        body = {"name": name, "email": f"{name.split()[0].lower()}@maxasp.example"}
        assert admin("PUT", f"/approvers/{object_id}", body).json() == {"changed": True}
    row = {"approver": approver.upper(), "deputy": deputy, "direct_debit": True}
    assert admin("PUT", f"/vendors/{vendor}/approver", row).json() == {"changed": True}
    assert admin("PUT", f"/vendors/{vendor}/approver", row).json() == {"changed": False}
    assert admin("PUT", f"/approvers/{approver}/absence", {"absent": True}).json() == {
        "changed": True
    }

    table = admin("GET", "/approvers").json()
    assert {
        "vendor_account": vendor,
        "approver": approver,
        "deputy": deputy,
        "direct_debit": True,
    } in table["vendors"]
    assert {
        "object_id": approver,
        "name": "Anna Route",
        "email": "anna@maxasp.example",
        "absent": True,
    } in table["approvers"]

    refused = admin("DELETE", f"/approvers/{approver}")
    assert refused.status_code == 409
    assert refused.json() == {"detail": "this person is still approver or deputy of a vendor"}
    assert admin("DELETE", f"/vendors/{vendor}/approver").status_code == 204
    assert admin("DELETE", f"/vendors/{vendor}/approver").status_code == 404
    assert admin("DELETE", f"/approvers/{approver}").status_code == 204
    assert admin("PUT", f"/approvers/{approver}/absence", {"absent": False}).status_code == 404

    (logged,) = events(app, "vendor_approver_set", "vendor_account", vendor)
    assert logged["actor"] == f"user:{ARITA}"
    assert logged["name"] == "Arita Test"


def test_the_routes_take_an_uppercase_object_id_and_refuse_an_invisible_name(
    client: TestClient, entra: FakeEntra
) -> None:
    object_id = str(uuid.uuid4())
    body = {"name": "Upper Case", "email": "upper@maxasp.example"}

    put = call(client, entra, ARITA, Role.ADMIN, "PUT", f"/approvers/{object_id.upper()}", body)
    assert put.json() == {"changed": True}
    table = call(client, entra, ARITA, Role.ADMIN, "GET", "/approvers").json()
    assert object_id in {p["object_id"] for p in table["approvers"]}

    invisible = {"name": "\u200b", "email": "x@maxasp.example"}
    refused = call(client, entra, ARITA, Role.ADMIN, "PUT", f"/approvers/{uuid.uuid4()}", invisible)
    assert refused.status_code == 409
    assert refused.json() == {"detail": "an approver needs a name"}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/approvers/not-an-object-id", {"name": "X", "email": "x@maxasp.example"}),
        (f"/approvers/{uuid.uuid4()}", {"name": "   ", "email": "x@maxasp.example"}),
        (f"/approvers/{uuid.uuid4()}", {"name": "X", "email": "not an address"}),
        ("/vendors/7001/approver", {"approver": str(uuid.uuid4())}),
        ("/vendors/70001/approver", {"approver": "someone"}),
        ("/vendors/70001/approver", {"approver": str(uuid.uuid4()), "extra": 1}),
    ],
)
def test_the_routes_refuse_wrong_input(
    client: TestClient, entra: FakeEntra, path: str, body: dict[str, Any]
) -> None:
    assert call(client, entra, ARITA, Role.ADMIN, "PUT", path, body).status_code == 422


def test_only_the_admin_changes_the_table(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    vendor = account()
    object_id = str(uuid.uuid4())
    changes = [
        ("PUT", f"/approvers/{object_id}", {"name": "X", "email": "x@maxasp.example"}),
        ("DELETE", f"/approvers/{object_id}", None),
        ("PUT", f"/approvers/{object_id}/absence", {"absent": True}),
        ("PUT", f"/vendors/{vendor}/approver", {"approver": object_id}),
        ("DELETE", f"/vendors/{vendor}/approver", None),
    ]
    for method, path, body in changes:
        for who, role in ((BENJAMIN, Role.FINANCE), (object_id, Role.APPROVER), (ARITA, None)):
            assert call(client, entra, who, role, method, path, body).status_code == 403
        assert client.request(method, path, json=body).status_code == 401

    assert call(client, entra, BENJAMIN, Role.FINANCE, "GET", "/approvers").status_code == 200
    assert call(client, entra, object_id, Role.APPROVER, "GET", "/approvers").status_code == 403
    assert client.get("/approvers").status_code == 401
    with app.connect() as connection:
        assert vendor_approver(connection, vendor) is None


# ---------------------------------------------------------------------------- sign in (ADR 0008)


def test_an_approver_sees_their_vendors_cases_and_the_deputy_only_while_standing_in(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    approver, deputy, other = person(app), person(app), person(app)
    vendor = account()
    set_vendor_approver(app, ADMIN, vendor, approver, deputy, False)
    case_id = new_case(app, partner_account=vendor)
    no_vendor = new_case(app, partner_account=None)

    def sees(who: str, case: int = case_id) -> bool:
        response = call(client, entra, who, Role.APPROVER, "GET", f"/cases/{case}")
        assert response.status_code in (200, 404)
        return response.status_code == 200

    assert sees(approver)
    assert not sees(deputy)
    assert not sees(other)
    assert not sees(approver, no_vendor)

    set_absent(app, ADMIN, approver, True)
    assert sees(deputy)
    assert sees(approver)
    set_absent(app, ADMIN, approver, False)
    assert not sees(deputy)

    remove_vendor_approver(app, ADMIN, vendor)
    assert not sees(approver)


@pytest.mark.parametrize(
    "kind",
    [CaseKind.OUTGOING_INVOICE, CaseKind.OUTGOING_CREDIT_NOTE, CaseKind.BANK_TRANSACTION],
)
def test_an_approver_never_sees_a_case_that_is_not_an_incoming_invoice(
    app: Engine, client: TestClient, entra: FakeEntra, kind: CaseKind
) -> None:
    # A customer number typed into the table by mistake (compliance review of PR 175).
    approver = person(app)
    vendor = account()
    set_vendor_approver(app, ADMIN, vendor, approver, None, False)
    with unit_of_work(app, Actor.system("intake")) as db:
        case_id = create_case(db, kind, partner_account=vendor)
    credit_note = new_case(app, CaseKind.INCOMING_CREDIT_NOTE, partner_account=vendor)

    with app.connect() as connection:
        assert ApproverTable().approvers_of(connection, case_id) == frozenset()
        assert ApproverTable().approvers_of(connection, credit_note) == {approver}
    response = call(client, entra, approver, Role.APPROVER, "GET", f"/cases/{case_id}")
    assert response.status_code == 404

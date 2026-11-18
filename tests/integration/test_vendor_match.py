"""Match cases to their DATEV vendor account, against a real PostgreSQL and the fake DATEV (#35).

Each test opens its own cases with a made up vendor (synthetic VAT IDs, IBANs and account
numbers nobody else uses) and limits the pass to those cases, because the database is shared
with the other tests. A test that needs the vendor to "exist in DATEV" creates it in the fake
DATEV the way Benjamin would, through the fake's test control ``/_fake/creditors``.
"""

import itertools
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.approvers.table import ChangedBy, save_approver, set_vendor_approver
from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
    Outcome,
)
from argus.cases.store import (
    create_case,
    get_case,
    list_open_exceptions,
    open_exception,
    resolve_exception,
    trace,
    unit_of_work,
)
from argus.datev import Account, DatevConnectGateway, DatevUnavailable
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev
from argus.intake.vendors import VENDOR_MATCHED, match_vendors

INTAKE = Actor.system("intake")
ADMIN = ChangedBy(Actor.user("44444444-4444-4444-8444-444444444444"), "Arita Test")
OWN_VAT_IDS = frozenset({"DE099000001"})

# Vendor accounts, VAT IDs and IBANs nobody else uses.
_numbers = itertools.count(820_000_000 + uuid.uuid4().int % 1_000_000 * 100)


class Vendor:
    def __init__(self) -> None:
        n = next(_numbers)
        self.account = n
        self.vat_id = f"DE{n:09d}"
        self.iban = f"DE00{n:018d}"
        self.name = f"Muster Testlieferant {n} GmbH"

    def creditor(self) -> dict[str, Any]:
        return {
            "account_number": self.account,
            "caption": self.name,
            "eu_vat_id_number": self.vat_id,
            "country": "DE",
            "bank_accounts": [{"iban": self.iban, "currency_code": "EUR"}],
        }


@pytest.fixture
def fake() -> Iterator[TestClient]:
    with TestClient(create_fake_datev()) as client:
        yield client


@pytest.fixture
def datev(fake: TestClient) -> DatevConnectGateway:
    return DatevConnectGateway(fake, DEFAULT_CLIENT_ID)


def case_from(
    app: Engine,
    *,
    vat_id: str | None = None,
    iban: str | None = None,
    name: str | None = None,
    kind: CaseKind = CaseKind.INCOMING_INVOICE,
) -> int:
    """An incoming invoice case with its reading, as intake opens it (#30)."""
    values = {"vendor_vat_id": vat_id, "iban_on_document": iban, "issuer_name": name}
    reading = {
        "document": "body",
        "kind": "invoice",
        "fields": {
            field: {"value": value, "source": "page_text", "flag": None}
            for field, value in values.items()
            if value is not None
        },
    }
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(
            db, kind, document_number=f"T-{uuid.uuid4().hex[:8]}", gross_cents=11900
        )
        db.execute(
            text(
                "insert into invoice_reading (case_id, document, role, reading) "
                "values (:id, 'body', 'invoice', cast(:reading as jsonb))"
            ),
            {"id": case_id, "reading": json.dumps(reading)},
        )
    return case_id


def give_approver(app: Engine, vendor: Vendor) -> None:
    object_id = str(uuid.uuid4())
    save_approver(app, ADMIN, object_id, "Approver Test", f"{object_id[:8]}@maxasp.example")
    set_vendor_approver(app, ADMIN, str(vendor.account), object_id, None, False)


def open_of(app: Engine, case_id: int) -> list[tuple[ExceptionReason, str]]:
    with app.connect() as connection:
        found = list_open_exceptions(connection)
    return [(e.reason, e.detail) for e in found if e.case_id == case_id]


def events_of(app: Engine, case_id: int, *types: str) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select event_type, actor_type || ':' || actor as actor, details "
                "from audit_event where case_id = :id and event_type = any(:types) "
                "order by chain_seq"
            ),
            {"id": case_id, "types": list(types)},
        ).all()
    return [{"type": r.event_type, "actor": r.actor, **r.details} for r in rows]


def status_of(app: Engine, case_id: int) -> tuple[CaseStatus, str | None]:
    with app.connect() as connection:
        case = get_case(connection, case_id)
    return case.status, case.partner_account


# ------------------------------------------------------------------------- matching


@pytest.mark.req("PR-02", "VM-02")
def test_a_known_vendor_with_an_approver_is_matched_and_carries_on(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    case_id = case_from(app, vat_id=vendor.vat_id, iban=vendor.iban, name=vendor.name)

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.matched, done.unknown, done.without_approver, done.failed) == (1, 0, 0, 0)
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))
    assert open_of(app, case_id) == []
    [matched] = events_of(app, case_id, VENDOR_MATCHED)
    assert matched == {
        "type": VENDOR_MATCHED,
        "actor": "system:vendor-match",
        "account": str(vendor.account),
        "by": "vat_id_and_iban",
        "weaker": False,
    }


@pytest.mark.req("PR-02")
def test_a_name_only_match_is_marked_weaker_in_the_audit_log(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    case_id = case_from(app, name=vendor.name.upper())

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert done.weaker == [case_id]
    [matched] = events_of(app, case_id, VENDOR_MATCHED)
    assert (matched["by"], matched["weaker"]) == ("name", True)


@pytest.mark.req("VM-03", "EX-02")
def test_an_unknown_vendor_waits_and_carries_on_by_itself_once_it_exists_in_datev(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    give_approver(app, vendor)
    case_id = case_from(app, vat_id=vendor.vat_id, iban=vendor.iban, name=vendor.name)

    first = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert first.unknown == 1
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, None)
    [(reason, detail)] = open_of(app, case_id)
    assert reason is ExceptionReason.UNKNOWN_VENDOR
    assert "create the vendor in DATEV" in detail
    assert vendor.iban not in detail  # masked for people (#36)
    [opened] = events_of(app, case_id, "exception_opened")
    # Facts go into the audit log, which nobody can edit: no name, VAT ID or IBAN (NF-03).
    assert opened["facts"] == {
        "why": "not_found",
        "vat_id_read": True,
        "iban_read": True,
        "name_read": True,
        "accounts": [],
    }

    # Nothing changes while the vendor is still missing; no second exception.
    again = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert (again.unknown, again.resolved) == (0, 0)
    assert len(open_of(app, case_id)) == 1

    fake.post("/_fake/creditors", json=vendor.creditor())  # Benjamin creates it in DATEV
    later = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (later.matched, later.resolved) == (1, 1)
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))
    assert open_of(app, case_id) == []
    [resolved] = events_of(app, case_id, "exception_resolved")
    assert resolved["actor"] == "system:vendor-match"
    assert (resolved["reason"], resolved["outcome"]) == ("unknown_vendor", "fixed_in_datev")


@pytest.mark.req("VM-02", "EX-02")
def test_a_vendor_without_an_approver_waits_until_it_has_one(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    case_id = case_from(app, vat_id=vendor.vat_id, iban=vendor.iban)

    first = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (first.matched, first.without_approver) == (1, 1)
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, str(vendor.account))
    [(reason, detail)] = open_of(app, case_id)
    assert reason is ExceptionReason.VENDOR_WITHOUT_APPROVER
    assert str(vendor.account) in detail

    assert match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id]).resolved == 0

    give_approver(app, vendor)
    later = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert later.resolved == 1
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))
    [resolved] = events_of(app, case_id, "exception_resolved")
    assert (resolved["reason"], resolved["outcome"]) == ("vendor_without_approver", "retried")


@pytest.mark.req("VM-02", "VM-03")
def test_a_new_vendor_without_an_approver_swaps_one_wait_for_the_other(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    case_id = case_from(app, vat_id=vendor.vat_id)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    fake.post("/_fake/creditors", json=vendor.creditor())
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    # The case never left the exception queue in between.
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, str(vendor.account))
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.VENDOR_WITHOUT_APPROVER]
    with app.connect() as connection:
        moves = [
            (e.from_status, e.to_status)
            for e in trace(connection, case_id)
            if e.event_type == "status_changed"
        ]
    assert moves == [(CaseStatus.RECEIVED, CaseStatus.EXCEPTION)]

    give_approver(app, vendor)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))


@pytest.mark.req("VM-03")
def test_a_person_who_resolved_the_exception_first_wins(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    case_id = case_from(app, vat_id=vendor.vat_id)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    with app.connect() as connection:
        [exception] = [e for e in list_open_exceptions(connection) if e.case_id == case_id]
    with unit_of_work(app, Actor.user("55555555-5555-4555-8555-555555555555")) as db:
        resolve_exception(
            db,
            exception.id,
            Outcome.REJECTED,
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )

    fake.post("/_fake/creditors", json=vendor.creditor())
    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.matched, done.resolved, done.failed) == (0, 0, 0)
    assert status_of(app, case_id) == (CaseStatus.CLOSED, None)


@pytest.mark.req("PR-02")
def test_other_exceptions_and_other_kinds_are_left_alone(
    app: Engine, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    outgoing = case_from(app, vat_id=vendor.vat_id, kind=CaseKind.OUTGOING_INVOICE)
    stopped = case_from(app, vat_id=vendor.vat_id)
    with unit_of_work(app, INTAKE) as db:
        open_exception(db, ExceptionReason.DUPLICATE, "same invoice twice", case_id=stopped)

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[outgoing, stopped])

    assert (done.matched, done.unknown) == (0, 0)
    assert status_of(app, outgoing) == (CaseStatus.RECEIVED, None)
    assert [r for r, _ in open_of(app, stopped)] == [ExceptionReason.DUPLICATE]


# ------------------------------------------------------------------------- DATEV


class Watching:
    """A gateway that records what ARGUS asks DATEV, and can be down."""

    def __init__(self, inner: DatevConnectGateway, down: bool = False) -> None:
        self.inner = inner
        self.down = down
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if self.down:
                raise DatevUnavailable("not answering")
            return getattr(self.inner, name)(*args, **kwargs)

        return call

    def list_vendors(self) -> list[Account]:
        return self.__getattr__("list_vendors")()  # type: ignore[no-any-return]

    def list_customers(self) -> list[Account]:
        return self.__getattr__("list_customers")()  # type: ignore[no-any-return]


@pytest.mark.req("VM-01")
def test_argus_only_reads_vendor_accounts_and_never_creates_one(
    app: Engine, datev: DatevConnectGateway
) -> None:
    watching = Watching(datev)
    case_id = case_from(app, vat_id=Vendor().vat_id)

    match_vendors(app, watching, OWN_VAT_IDS, cases=[case_id])

    assert set(watching.calls) == {"list_vendors", "list_customers"}


def test_without_work_datev_is_not_asked(app: Engine, datev: DatevConnectGateway) -> None:
    watching = Watching(datev)

    done = match_vendors(app, watching, OWN_VAT_IDS, cases=[-1])

    assert watching.calls == []
    assert done.matched == 0


@pytest.mark.req("PR-02")
def test_when_datev_is_down_the_cases_wait_for_the_next_pass(
    app: Engine, datev: DatevConnectGateway
) -> None:
    case_id = case_from(app, vat_id=Vendor().vat_id)

    done = match_vendors(
        app,
        Watching(datev, down=True),
        OWN_VAT_IDS,
        cases=[case_id],
    )

    assert done.skipped_datev_unavailable
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, None)
    assert open_of(app, case_id) == []

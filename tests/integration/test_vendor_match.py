"""Match cases to their DATEV vendor account, against a real PostgreSQL and the fake DATEV (#35).

Each test opens its own cases with a made up vendor (synthetic VAT IDs, IBANs and account
numbers nobody else uses) and limits the pass to those cases, because the database is shared
with the other tests. A test that needs the vendor to "exist in DATEV" creates it in the fake
DATEV the way Benjamin would, through the fake's test control ``/_fake/creditors``.
"""

import itertools
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import argus.intake.vendors as vendors
from argus.approvers.table import (
    ChangedBy,
    remove_vendor_approver,
    save_approver,
    set_vendor_approver,
)
from argus.cases.model import (
    Actor,
    CaseKind,
    CaseStatus,
    ClosedReason,
    ExceptionReason,
    Outcome,
)
from argus.cases.store import (
    CaseRuleBroken,
    create_case,
    get_case,
    list_open_exceptions,
    move_case,
    open_exception,
    resolve_exception,
    trace,
    unit_of_work,
)
from argus.datev import Account, DatevConnectGateway, DatevUnavailable
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev
from argus.intake.vendors import VENDOR_MATCHED, match_vendors
from argus.logsafe import SafeFormatter

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
    iban_field: dict[str, Any] | None = None,
) -> int:
    """An incoming invoice case with its reading, as intake opens it (#30). ``iban_field``
    stores the IBAN field as given, for a flagged IBAN."""
    values = {"vendor_vat_id": vat_id, "iban_on_document": iban, "issuer_name": name}
    fields: dict[str, Any] = {
        field: {"value": value, "source": "page_text", "flag": None}
        for field, value in values.items()
        if value is not None
    }
    if iban_field is not None:
        fields["iban_on_document"] = iban_field
    reading = {"document": "body", "kind": "invoice", "fields": fields}
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
    with unit_of_work(app, PERSON) as db:
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


PERSON = Actor.user("55555555-5555-4555-8555-555555555555", "Person Test")


def open_exception_of(app: Engine, case_id: int) -> int:
    with app.connect() as connection:
        [exception] = [e for e in list_open_exceptions(connection) if e.case_id == case_id]
    return exception.id


@pytest.mark.req("VM-02")
def test_carry_on_without_an_approver_puts_the_case_back_in_the_queue(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    """Code review of PR 182: "Checked, carry on" on the invoices page (#62) must not let a
    case go on to approval while its vendor still has no approver."""
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    case_id = case_from(app, vat_id=vendor.vat_id)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    with unit_of_work(app, PERSON) as db:
        resolve_exception(
            db, open_exception_of(app, case_id), Outcome.ACCEPTED_WITH_COMMENT, resolution="ok"
        )
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.matched, done.without_approver) == (0, 1)
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, str(vendor.account))
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.VENDOR_WITHOUT_APPROVER]


@pytest.mark.req("VM-02")
def test_an_approver_removed_before_approval_stops_the_case_again(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    case_id = case_from(app, vat_id=vendor.vat_id)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert open_of(app, case_id) == []

    remove_vendor_approver(app, ADMIN, str(vendor.account))
    # The approver check needs no DATEV, so it runs even while DATEV does not answer.
    done = match_vendors(app, Watching(datev, down=True), OWN_VAT_IDS, cases=[case_id])

    assert done.without_approver == 1
    assert not done.skipped_datev_unavailable
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.VENDOR_WITHOUT_APPROVER]


@pytest.mark.req("VM-03")
def test_an_exception_a_person_resolves_during_the_pass_is_left_alone(
    app: Engine, fake: TestClient, datev: DatevConnectGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass lists its work, then a person rejects the invoice before the pass reaches it."""
    vendor = Vendor()
    case_id = case_from(app, vat_id=vendor.vat_id)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    listed = vendors._work

    def person_resolves_after_listing(*args: Any) -> Any:
        work = listed(*args)
        with unit_of_work(app, PERSON) as db:
            resolve_exception(
                db,
                open_exception_of(app, case_id),
                Outcome.REJECTED,
                case_to=CaseStatus.CLOSED,
                closed_reason=ClosedReason.REJECTED,
            )
        return work

    monkeypatch.setattr(vendors, "_work", person_resolves_after_listing)
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


# ------------------------------------------------------------------------- IBAN check (#36)


def other_iban() -> str:
    """An IBAN no vendor account in the fake DATEV has."""
    return Vendor().iban


def audit_text(app: Engine, case_id: int) -> str:
    """Everything the audit log holds for a case, as one string."""
    with app.connect() as connection:
        rows: list[str] = list(
            connection.execute(
                text("select details::text from audit_event where case_id = :id"),
                {"id": case_id},
            ).scalars()
        )
    return " ".join(rows)


def changed_iban_case(app: Engine, fake: TestClient) -> tuple[Vendor, str, int]:
    """A known vendor with an approver, and an invoice from it that asks to pay a new IBAN."""
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    new_iban = other_iban()
    case_id = case_from(app, vat_id=vendor.vat_id, iban=new_iban, name=vendor.name)
    return vendor, new_iban, case_id


@pytest.mark.req("PR-05", "VM-04", "EX-02")
def test_a_changed_iban_blocks_the_invoice_as_possible_fraud(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor, new_iban, case_id = changed_iban_case(app, fake)

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.matched, done.iban_mismatch, done.without_approver) == (1, 1, 0)
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, str(vendor.account))
    [(reason, detail)] = open_of(app, case_id)
    assert reason is ExceptionReason.IBAN_MISMATCH
    assert detail.startswith("Possible fraud.")
    assert f"DE ... {new_iban[-4:]}" in detail
    assert f"DE ... {vendor.iban[-4:]}" in detail
    assert str(vendor.account) in detail
    # Masked for people, and never in the audit log (NF-03).
    assert new_iban not in detail and vendor.iban not in detail
    log = audit_text(app, case_id)
    assert new_iban not in log and vendor.iban not in log
    [matched] = events_of(app, case_id, VENDOR_MATCHED)
    assert matched["by"] == "vat_id"
    [opened] = events_of(app, case_id, "exception_opened")
    assert opened["facts"] == {
        "account": str(vendor.account),
        "possible_fraud": True,
        "ibans_on_file": 1,
        "ibans_stated": 1,
        "ibans_differing": 1,
        "read_with_flag": False,
        "other_accounts": [],
    }


@pytest.mark.req("PR-05", "VM-04")
def test_argus_never_takes_the_new_iban_from_the_invoice(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor, _, case_id = changed_iban_case(app, fake)
    watching = Watching(datev)

    match_vendors(app, watching, OWN_VAT_IDS, cases=[case_id])
    match_vendors(app, watching, OWN_VAT_IDS, cases=[case_id])

    # Only reads, and DATEV still has the old IBAN only.
    assert set(watching.calls) == {"list_vendors", "list_customers"}
    [on_file] = [v for v in datev.list_vendors() if v.number == vendor.account]
    assert [b.iban for b in on_file.bank_accounts] == [vendor.iban]
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.IBAN_MISMATCH]


@pytest.mark.req("PR-05", "PY-01")
def test_a_blocked_invoice_never_goes_for_approval_or_into_a_payment_run(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    _, _, case_id = changed_iban_case(app, fake)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    for to in (CaseStatus.AWAITING_APPROVAL, CaseStatus.IN_PAYMENT_RUN, CaseStatus.RECEIVED):
        reference = "BOOKING-TEST" if to is CaseStatus.IN_PAYMENT_RUN else None
        with (
            pytest.raises(CaseRuleBroken, match="open exception"),
            unit_of_work(app, INTAKE) as db,
        ):
            move_case(
                db, case_id, expected=CaseStatus.EXCEPTION, to=to, datev_booking_ref=reference
            )
    assert status_of(app, case_id)[0] is CaseStatus.EXCEPTION

    # A person rejects it: the case closes and still can never be paid.
    with unit_of_work(app, PERSON) as db:
        exception_id: int
        [exception_id] = db.execute(
            text("select id from exception_item where case_id = :id and resolved_at is null"),
            {"id": case_id},
        ).scalars()
        resolve_exception(
            db,
            exception_id,
            Outcome.REJECTED,
            resolution="Vendor confirmed by phone they did not change banks.",
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    assert status_of(app, case_id)[0] is CaseStatus.CLOSED
    with pytest.raises(CaseRuleBroken, match="closed"), unit_of_work(app, INTAKE) as db:
        move_case(
            db,
            case_id,
            expected=CaseStatus.CLOSED,
            to=CaseStatus.IN_PAYMENT_RUN,
            datev_booking_ref="BOOKING-TEST",
        )


@pytest.mark.req("PR-05", "VM-04", "EX-02")
def test_the_block_ends_by_itself_once_datev_holds_the_new_iban(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor, new_iban, case_id = changed_iban_case(app, fake)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    # Someone changes the IBAN in DATEV, but not to the invoice's one: still blocked.
    fake.put(
        f"/_fake/creditors/{vendor.account}/bank-accounts",
        json=[{"iban": other_iban(), "currency_code": "EUR"}],
    ).raise_for_status()
    still = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert (still.resolved, still.iban_mismatch) == (0, 0)
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.IBAN_MISMATCH]

    # Benjamin checked with the vendor and put the new IBAN into DATEV.
    fake.put(
        f"/_fake/creditors/{vendor.account}/bank-accounts",
        json=[{"iban": new_iban, "currency_code": "EUR"}],
    ).raise_for_status()
    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert done.resolved == 1
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))
    assert open_of(app, case_id) == []
    [resolved] = events_of(app, case_id, "exception_resolved")
    assert (resolved["actor"], resolved["outcome"]) == ("system:vendor-match", "fixed_in_datev")


@pytest.mark.req("PR-05", "VM-02")
def test_a_fixed_iban_does_not_let_a_vendor_without_an_approver_through(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    new_iban = other_iban()
    case_id = case_from(app, vat_id=vendor.vat_id, iban=new_iban)

    first = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert (first.iban_mismatch, first.without_approver) == (1, 1)
    assert sorted(r for r, _ in open_of(app, case_id)) == sorted(
        [ExceptionReason.IBAN_MISMATCH, ExceptionReason.VENDOR_WITHOUT_APPROVER]
    )

    fake.put(
        f"/_fake/creditors/{vendor.account}/bank-accounts",
        json=[{"iban": new_iban, "currency_code": "EUR"}],
    ).raise_for_status()
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert status_of(app, case_id)[0] is CaseStatus.EXCEPTION
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.VENDOR_WITHOUT_APPROVER]


@pytest.mark.req("PR-05")
def test_an_iban_of_another_vendor_is_named_in_the_detail(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor, other = Vendor(), Vendor()
    for v in (vendor, other):
        fake.post("/_fake/creditors", json=v.creditor())
        give_approver(app, v)
    case_id = case_from(app, vat_id=vendor.vat_id, iban=other.iban)

    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    [(reason, detail)] = open_of(app, case_id)
    assert reason is ExceptionReason.IBAN_MISMATCH
    assert f"another vendor account in DATEV: {other.account}" in detail
    [opened] = events_of(app, case_id, "exception_opened")
    assert opened["facts"]["other_accounts"] == [str(other.account)]


@pytest.mark.req("PR-05", "VM-03")
def test_an_unknown_vendor_that_turns_up_with_another_iban_stays_blocked(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    give_approver(app, vendor)
    new_iban = other_iban()
    case_id = case_from(app, vat_id=vendor.vat_id, iban=new_iban)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.UNKNOWN_VENDOR]

    fake.post("/_fake/creditors", json=vendor.creditor())
    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.resolved, done.iban_mismatch) == (1, 1)
    assert status_of(app, case_id)[0] is CaseStatus.EXCEPTION
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.IBAN_MISMATCH]


@pytest.mark.req("PR-05")
def test_an_invoice_without_a_readable_iban_is_not_compared(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    case_id = case_from(app, vat_id=vendor.vat_id, name=vendor.name)

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert done.iban_mismatch == 0
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))


@pytest.mark.req("PR-05", "NF-03")
def test_ibans_are_masked_in_logs(
    app: Engine,
    fake: TestClient,
    datev: DatevConnectGateway,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor, new_iban, case_id = changed_iban_case(app, fake)
    broken_vendor, broken_iban, broken = changed_iban_case(app, fake)

    real = vendors._queue_iban_mismatch

    def fails_for_one(db: Any, case: int, *args: Any) -> None:
        if case == broken:
            raise RuntimeError(f"boom {broken_iban} {broken_vendor.iban}")
        real(db, case, *args)

    monkeypatch.setattr(vendors, "_queue_iban_mismatch", fails_for_one)
    with caplog.at_level(logging.DEBUG):
        done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id, broken])

    assert (done.iban_mismatch, done.failed) == (1, 1)
    logged = caplog.text + " ".join(str(r.args) for r in caplog.records)
    for iban in (new_iban, vendor.iban):
        assert iban not in logged
    # The job's own formatter writes errors as their type only, never the message.
    formatted = " ".join(SafeFormatter().format(r) for r in caplog.records)
    assert broken_iban not in formatted and broken_vendor.iban not in formatted


@pytest.mark.req("PR-05")
def test_when_datev_is_down_a_blocked_invoice_stays_blocked(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    _, _, case_id = changed_iban_case(app, fake)
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    done = match_vendors(app, Watching(datev, down=True), OWN_VAT_IDS, cases=[case_id])

    assert done.skipped_datev_unavailable
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.IBAN_MISMATCH]


# A flag stops an IBAN from being used to find the vendor, never from being checked
# (compliance review of PR 185). Each flagged reading below is stored the way the readers store
# it (reading/invoice.py, einvoice/parser.py).


def flagged_iban_fields(on_file: str, new: str) -> dict[str, dict[str, Any]]:
    return {
        # The usual "we changed banks" letter: the old and the new IBAN on one page.
        "several_values": {
            "value": None,
            "source": None,
            "flag": "several_values",
            "note": f"the document states 2 IBANs: {new}, {on_file}",
            "values": [new, on_file],
        },
        "several_values_einvoice": {
            "value": None,
            "source": None,
            "flag": "several_values",
            "note": f"the e-invoice states 2 values: {on_file}, {new}",
            "values": [on_file, new],
        },
        # Stored before the values were kept: only the note names them.
        "several_values_note_only": {
            "value": None,
            "source": None,
            "flag": "several_values",
            "note": f"the document states 2 IBANs: {new}, {on_file}",
        },
        "from_model": {
            "value": new,
            "source": "language_model",
            "page": 1,
            "words": f"IBAN {new}",
            "flag": "from_model",
            "note": "only the language model located this value, a person confirms it",
        },
        "low_confidence": {
            "value": new,
            "source": "document_intelligence",
            "confidence": 0.41,
            "flag": "low_confidence",
            "note": "read with low confidence",
        },
        "readers_disagree": {
            "value": on_file,
            "source": "document_intelligence",
            "flag": "readers_disagree",
            "note": f"the reader says {on_file}, the page text says {new}",
            "values": [on_file, new],
        },
        "failed_check": {
            "value": new[:-1] + ("0" if new[-1] != "0" else "1"),
            "source": "page_text",
            "flag": "failed_check",
            "note": "the IBAN check digits are wrong",
        },
    }


@pytest.mark.req("PR-05")
@pytest.mark.parametrize("flag", list(flagged_iban_fields("X", "Y")))
def test_an_iban_read_with_a_flag_is_still_checked(
    app: Engine, fake: TestClient, datev: DatevConnectGateway, flag: str
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    new_iban = other_iban()
    case_id = case_from(
        app,
        vat_id=vendor.vat_id,
        name=vendor.name,
        iban_field=flagged_iban_fields(vendor.iban, new_iban)[flag],
    )

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert (done.matched, done.iban_mismatch) == (1, 1)
    assert status_of(app, case_id) == (CaseStatus.EXCEPTION, str(vendor.account))
    [(reason, detail)] = open_of(app, case_id)
    assert reason is ExceptionReason.IBAN_MISMATCH
    assert detail.startswith("Possible fraud.")
    assert new_iban not in detail and vendor.iban not in detail
    assert new_iban not in audit_text(app, case_id)
    [opened] = events_of(app, case_id, "exception_opened")
    assert opened["facts"]["read_with_flag"] is True
    assert opened["facts"]["ibans_differing"] == 1


@pytest.mark.req("PR-05", "VM-04")
def test_two_ibans_end_the_block_only_when_both_are_in_datev(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    new_iban = other_iban()
    case_id = case_from(
        app,
        vat_id=vendor.vat_id,
        iban_field=flagged_iban_fields(vendor.iban, new_iban)["several_values"],
    )
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    [(_, detail)] = open_of(app, case_id)
    assert "This invoice states 2 IBANs." in detail
    assert f"DE ... {new_iban[-4:]} is not on vendor account" in detail

    # Only the new IBAN in DATEV, the old one removed: the invoice still names an IBAN DATEV
    # does not hold, so it stays blocked.
    fake.put(
        f"/_fake/creditors/{vendor.account}/bank-accounts",
        json=[{"iban": new_iban, "currency_code": "EUR"}],
    ).raise_for_status()
    match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])
    assert [r for r, _ in open_of(app, case_id)] == [ExceptionReason.IBAN_MISMATCH]

    fake.put(
        f"/_fake/creditors/{vendor.account}/bank-accounts",
        json=[
            {"iban": vendor.iban, "currency_code": "EUR"},
            {"iban": new_iban, "currency_code": "EUR"},
        ],
    ).raise_for_status()
    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert done.resolved == 1
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))


@pytest.mark.req("PR-05")
def test_a_flagged_iban_that_is_on_file_carries_on(
    app: Engine, fake: TestClient, datev: DatevConnectGateway
) -> None:
    vendor = Vendor()
    fake.post("/_fake/creditors", json=vendor.creditor())
    give_approver(app, vendor)
    case_id = case_from(
        app,
        vat_id=vendor.vat_id,
        iban_field=flagged_iban_fields(vendor.iban, vendor.iban)["from_model"],
    )

    done = match_vendors(app, datev, OWN_VAT_IDS, cases=[case_id])

    assert done.iban_mismatch == 0
    assert status_of(app, case_id) == (CaseStatus.RECEIVED, str(vendor.account))

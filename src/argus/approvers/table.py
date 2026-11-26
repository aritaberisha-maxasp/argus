"""The ARGUS approver table (#34, migration 0015).

Each DATEV vendor account has exactly one approver, kept here and not in DATEV (AP-01). The same
row says who stands in while the approver is away (AP-06) and whether the vendor is paid by
direct debit, so its invoices never go into a payment run (PY-13). A vendor without a row has no
approver, and its invoices go to the exception queue (VM-02).

Only a person changes the table, and the database writes every change into the audit log with
that person's object ID and name. Every change runs in :func:`argus.cases.store.unit_of_work`,
so whatever the database refuses comes back as :class:`~argus.cases.store.CaseRuleBroken`.
"""

import csv
import re
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work

OBJECT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
VENDOR_ACCOUNT = re.compile(r"^[1-9][0-9]{4,8}$")


@dataclass(frozen=True)
class Approver:
    """A person who can approve invoices, by Entra object ID (ADR 0008)."""

    object_id: str
    name: str
    email: str
    absent: bool
    absent_since: datetime | None


@dataclass(frozen=True)
class VendorApprover:
    """The approver row of one vendor account."""

    vendor_account: str
    approver_id: str
    deputy_id: str | None
    direct_debit: bool


@dataclass(frozen=True)
class ChangedBy:
    """Who changes the table, as they are at that moment (ADR 0008): the object ID alone names
    nobody once the Entra account is gone. Only a person may change it."""

    actor: Actor  # user:<Entra object ID>
    name: str
    sign_in_address: str | None = None


def _changing(engine: Engine, by: ChangedBy) -> AbstractContextManager[Connection]:
    if by.actor.kind != "user":
        raise PermissionError("only a person can change the approver table")
    return unit_of_work(engine, by.actor)


def _name_the_person(db: Connection, by: ChangedBy) -> None:
    for setting, value in (
        ("argus.changed_by_name", by.name),
        ("argus.changed_by_address", by.sign_in_address or ""),
    ):
        db.execute(
            text("select set_config(:setting, :value, true)"),
            {"setting": setting, "value": value},
        )


# ------------------------------------------------------------------------------- reading


def list_approvers(connection: Connection) -> list[Approver]:
    rows = connection.execute(
        text(
            "select object_id, name, email, absent, absent_since from approver "
            "order by name, object_id"
        )
    ).all()
    return [
        Approver(
            object_id=row.object_id,
            name=row.name,
            email=row.email,
            absent=row.absent,
            absent_since=row.absent_since,
        )
        for row in rows
    ]


def list_vendor_approvers(connection: Connection) -> list[VendorApprover]:
    rows = connection.execute(
        text(
            "select vendor_account, approver_id, deputy_id, direct_debit from vendor_approver "
            "order by vendor_account"
        )
    ).all()
    return [_vendor_row(row) for row in rows]


def _vendor_row(row: Any) -> VendorApprover:
    return VendorApprover(
        vendor_account=row.vendor_account,
        approver_id=row.approver_id,
        deputy_id=row.deputy_id,
        direct_debit=row.direct_debit,
    )


def vendor_approver(connection: Connection, vendor_account: str) -> VendorApprover | None:
    row = connection.execute(
        text(
            "select vendor_account, approver_id, deputy_id, direct_debit from vendor_approver "
            "where vendor_account = :account"
        ),
        {"account": vendor_account},
    ).first()
    return _vendor_row(row) if row else None


def approvers_for_vendor(connection: Connection, vendor_account: str) -> frozenset[str]:
    """Who decides on this vendor's invoices: the approver, and the deputy while the approver
    is away (AP-06). Empty when the vendor has no approver (VM-02)."""
    row = connection.execute(
        text(
            "select v.approver_id, v.deputy_id, a.absent from vendor_approver v "
            "join approver a on a.object_id = v.approver_id where v.vendor_account = :account"
        ),
        {"account": vendor_account},
    ).first()
    if row is None:
        return frozenset()
    approver_id: str = row.approver_id
    deputy_id: str | None = row.deputy_id
    if row.absent and deputy_id is not None:
        return frozenset({approver_id, deputy_id})
    return frozenset({approver_id})


def direct_debit_of(connection: Connection, vendor_account: str) -> bool | None:
    """Whether this vendor is paid by direct debit (PY-13). None when it has no approver row,
    so the caller cannot mistake an unknown vendor for one paid by transfer."""
    found = vendor_approver(connection, vendor_account)
    return None if found is None else found.direct_debit


class ApproverTable:
    """Who decides on a case, for sign in (ADR 0008): the approvers of the case's vendor
    account. Only incoming invoices and credit notes have approvers. A case without a vendor
    account, or whose vendor has no approver, has none, so an approver does not see it.
    Finance and admin see every case anyway."""

    def approvers_of(self, connection: Connection, case_id: int) -> frozenset[str]:
        # Only invoices and credit notes from vendors. Outgoing invoices and bank transactions
        # also carry an account, and a customer number typed into the table by mistake must not
        # open them to an approver (NF-03, compliance review of PR 175).
        account = connection.execute(
            text(
                "select partner_account from invoice_case where id = :id "
                "and kind in ('incoming_invoice', 'incoming_credit_note')"
            ),
            {"id": case_id},
        ).scalar_one_or_none()
        if account is None:
            return frozenset()
        return approvers_for_vendor(connection, account)


# ------------------------------------------------------------------------------- changing


def save_approver(engine: Engine, by: ChangedBy, object_id: str, name: str, email: str) -> bool:
    """Add a person who can approve, or change their name or email address. Returns False when
    nothing changed, in which case nothing is logged."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        current = db.execute(
            text("select name, email from approver where object_id = :id for update"),
            {"id": object_id},
        ).first()
        if current is None:
            db.execute(
                text("insert into approver (object_id, name, email) values (:id, :name, :email)"),
                {"id": object_id, "name": name, "email": email},
            )
            return True
        changed = db.execute(
            text(
                "update approver set name = :name, email = :email where object_id = :id "
                "and (name, email) is distinct from (correction_trim(:name), lower(btrim(:email))) "
                "returning object_id"
            ),
            {"id": object_id, "name": name, "email": email},
        ).first()
    return changed is not None


def remove_approver(engine: Engine, by: ChangedBy, object_id: str) -> bool:
    """Remove a person from the table. Refused while they approve or stand in for a vendor.
    Returns False when there was no such person."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        removed = db.execute(
            text("delete from approver where object_id = :id returning object_id"),
            {"id": object_id},
        ).first()
    return removed is not None


def set_absent(engine: Engine, by: ChangedBy, object_id: str, absent: bool) -> bool | None:
    """Mark a person away (their deputies decide in their place, AP-06) or back. Returns False
    when they already were, None when there is no such person."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        exists = db.execute(
            text("select absent from approver where object_id = :id for update"),
            {"id": object_id},
        ).first()
        if exists is None:
            return None
        changed = db.execute(
            text(
                "update approver set absent = :absent where object_id = :id "
                "and absent <> :absent returning object_id"
            ),
            {"id": object_id, "absent": absent},
        ).first()
    return changed is not None


def set_vendor_approver(
    engine: Engine,
    by: ChangedBy,
    vendor_account: str,
    approver_id: str,
    deputy_id: str | None,
    direct_debit: bool,
) -> bool:
    """Give a vendor its approver, deputy and direct debit flag, replacing what it had. Returns
    False when nothing changed, in which case nothing is logged."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        result = db.execute(
            text(
                "insert into vendor_approver "
                "(vendor_account, approver_id, deputy_id, direct_debit) "
                "values (:account, :approver, :deputy, :direct_debit) "
                "on conflict (vendor_account) do update set approver_id = excluded.approver_id, "
                "deputy_id = excluded.deputy_id, direct_debit = excluded.direct_debit "
                "where (vendor_approver.approver_id, vendor_approver.deputy_id, "
                "vendor_approver.direct_debit) is distinct from "
                "(excluded.approver_id, excluded.deputy_id, excluded.direct_debit) "
                "returning vendor_account"
            ),
            {
                "account": vendor_account,
                "approver": approver_id,
                "deputy": deputy_id,
                "direct_debit": direct_debit,
            },
        ).first()
    return result is not None


def remove_vendor_approver(engine: Engine, by: ChangedBy, vendor_account: str) -> bool:
    """Take a vendor out of the table, so its invoices go to the exception queue again (VM-02).
    Returns False when it had no row."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        removed = db.execute(
            text(
                "delete from vendor_approver where vendor_account = :account "
                "returning vendor_account"
            ),
            {"account": vendor_account},
        ).first()
    return removed is not None


# ---------------------------------------------------------------- the second approver (#39)


def second_approver(connection: Connection) -> str | None:
    """The Entra object ID of the person who gives second approvals above EUR 5,000 (AP-09),
    or None while nobody is set."""
    found: str | None = connection.execute(
        text("select approver_id from second_approver")
    ).scalar_one_or_none()
    return found


def set_second_approver(engine: Engine, by: ChangedBy, object_id: str) -> bool:
    """Make this person, already in the approver table, the second approver (Benjamin). Taken by
    Entra object ID, never by email address. Returns False when they already were."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        changed = db.execute(
            text(
                "insert into second_approver (approver_id) values (:id)"
                " on conflict (only_one) do update set approver_id = excluded.approver_id"
                " where second_approver.approver_id is distinct from excluded.approver_id"
                " returning approver_id"
            ),
            {"id": object_id},
        ).first()
    return changed is not None


def remove_second_approver(engine: Engine, by: ChangedBy) -> bool:
    """Set nobody as second approver. Invoices above EUR 5,000 then go to the exception queue
    after the first approval. Returns False when nobody was set."""
    with _changing(engine, by) as db:
        _name_the_person(db, by)
        removed = db.execute(text("delete from second_approver returning approver_id")).first()
    return removed is not None


# ------------------------------------------------------------------------------- tests


@dataclass(frozen=True)
class _CsvPerson:
    object_id: str
    name: str
    email: str


def _people(rows: Iterable[dict[str, str]]) -> dict[str, _CsvPerson]:
    people: dict[str, _CsvPerson] = {}
    for row in rows:
        for role in ("approver", "deputy"):
            object_id = (row.get(f"{role}_object_id") or "").strip()
            if not object_id:
                continue
            person = _CsvPerson(object_id, row[f"{role}_name"], row[f"{role}_email"])
            if people.setdefault(object_id, person) != person:
                raise ValueError(f"object ID {object_id} has two different names or addresses")
    return people


def load_approvers_csv(engine: Engine, path: Path, by: ChangedBy) -> int:
    """Load an approver table in the shape of ``tests/fixtures/master-data/approvers.csv``, for
    tests and the demo. Every person and vendor row goes in through the same logged changes as
    the admin's. Returns the number of vendor rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for person in _people(rows).values():
        save_approver(engine, by, person.object_id, person.name, person.email)
    for row in rows:
        direct_debit = row["direct_debit"].strip().lower()
        if direct_debit not in ("yes", "no"):
            raise ValueError(f"direct_debit of {row['vendor_account']} must be yes or no")
        set_vendor_approver(
            engine,
            by,
            row["vendor_account"].strip(),
            row["approver_object_id"].strip(),
            (row.get("deputy_object_id") or "").strip() or None,
            direct_debit == "yes",
        )
    return len(rows)

"""Match each incoming invoice to its vendor account in DATEV (#35, PR-02, VM-02, VM-03).

After intake opens a case (#30), this step finds the vendor account (Kreditor) the invoice
belongs to and puts its number on the case. It reads DATEV through the gateway only and never
creates or changes a vendor account (VM-01): the gateway has no way to.

How a vendor is found (:func:`match_vendor`), strongest first:

1. The VAT ID on the invoice. When the IBAN on the invoice is also on that vendor account, the
   match is the strongest one. When the IBAN differs, the match stands, and the IBAN check
   below blocks the invoice.
2. The IBAN on the invoice, when no vendor account has the VAT ID.
3. The issuer's name, when neither found one. A name only match is weaker and says so in the
   audit log (``vendor_matched`` with ``weaker: true``).

A match fails, and nothing weaker is tried, when the VAT ID is Max ASP's own, or a customer's
that no vendor account has (PO note on #35, #106); when two vendor accounts fit equally well;
or when the vendor account found has a different VAT ID than the invoice.

What happens next (:func:`match_vendors`, one pass):

- No vendor account fits: the case goes to the exception queue as ``unknown_vendor``. The daily
  digest tells Benjamin (EX-03), and the detail says what to create in DATEV.
- A vendor account fits, but the approver table has no row for it: ``vendor_without_approver``
  (VM-02).
- The invoice states an IBAN that is not on the vendor account in DATEV: ``iban_mismatch``, a
  possible fraud case (#36, PR-05). The invoice stays blocked, so it never goes for approval
  or into a payment run. ARGUS never takes bank details from an invoice (VM-04): only a change
  in DATEV ends the block, or a person rejects the invoice.
- Every pass looks at those three kinds of exception again. Once the vendor exists in DATEV, or
  has an approver, the pass resolves the exception itself and the case carries on where it
  was (VM-03). Nobody has to touch it. An IBAN mismatch ends by itself only once DATEV holds
  the invoice's IBAN on that vendor account.

Only the values ARGUS read without a flag count to find the vendor. A VAT ID or IBAN a person
still has to check is not used for that, because a wrong match would send the invoice to the
wrong approver. A flag never stops the IBAN check, though: once the vendor is found, every IBAN
the document states, flagged or not, is compared with DATEV (compliance review of PR 185). An
invoice that prints two IBANs, the usual "we changed banks" letter, is held like any other.

The pass runs at the end of every live sweep (:func:`argus.intake.sweep.sweep`), so it needs no
job of its own. It calls DATEV only when there is a case to match.
"""

import logging
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, text

from argus.approvers.table import vendor_approver
from argus.cases.model import Actor, CaseKind, CaseStatus, ExceptionReason, Outcome
from argus.cases.store import (
    change_case,
    get_case,
    open_exception,
    record_event,
    resolve_exception,
    unit_of_work,
)
from argus.datev.gateway import DatevError, DatevGateway
from argus.datev.models import Account
from argus.sorting.checks import normalise
from argus.sorting.models import IBAN
from argus.vat_ids import clean_vat_id

log = logging.getLogger("argus.intake.vendors")

VENDOR_ACTOR = Actor.system("vendor-match")
VENDOR_MATCHED = "vendor_matched"
VENDOR_KINDS = (CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE)
# The exceptions this step opens, and so may resolve again by itself.
OWN_REASONS = (
    ExceptionReason.UNKNOWN_VENDOR,
    ExceptionReason.VENDOR_WITHOUT_APPROVER,
    ExceptionReason.IBAN_MISMATCH,
)


class MatchedBy(StrEnum):
    """What the match rests on, strongest first."""

    VAT_ID_AND_IBAN = "vat_id_and_iban"
    VAT_ID = "vat_id"
    IBAN = "iban"
    NAME = "name"  # weaker: names are not unique and are easy to copy

    @property
    def weaker(self) -> bool:
        return self is MatchedBy.NAME


class NoMatchReason(StrEnum):
    NOT_FOUND = "not_found"
    SEVERAL_VENDORS = "several_vendors"
    OWN_VAT_ID = "own_vat_id"
    CUSTOMER_VAT_ID = "customer_vat_id"
    VAT_ID_DIFFERS = "vat_id_differs"


@dataclass(frozen=True)
class InvoiceParty:
    """Who the invoice says it is from, as ARGUS read it. ``vat_id``, ``iban`` and ``name``
    are only values without a flag, used to find the vendor. ``stated_ibans`` is every IBAN the
    document states, flagged or not, for the IBAN check (#36). It always holds ``iban``."""

    vat_id: str | None = None
    iban: str | None = None
    name: str | None = None
    stated_ibans: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Compared cleaned, the way DATEV's values are cleaned.
        object.__setattr__(self, "vat_id", clean_vat_id(self.vat_id) if self.vat_id else None)
        object.__setattr__(self, "iban", clean_iban(self.iban) if self.iban else None)
        object.__setattr__(self, "name", self.name.strip() if self.name else None)
        stated = [clean_iban(i) for i in (self.iban, *self.stated_ibans) if i]
        object.__setattr__(self, "stated_ibans", tuple(dict.fromkeys(stated)))

    @property
    def iban_flagged(self) -> bool:
        """The document states an IBAN, but not one ARGUS read without a flag."""
        return bool(self.stated_ibans) and (self.iban is None or self.stated_ibans != (self.iban,))

    @classmethod
    def from_reading(cls, reading: Mapping[str, Any]) -> "InvoiceParty":
        """From a stored reading (``invoice_reading.reading``, :meth:`InvoiceReading.as_record`)."""
        fields: Mapping[str, Any] = reading.get("fields") or {}

        def value(name: str) -> str | None:
            found = fields.get(name) or {}
            if found.get("flag") or not found.get("value"):
                return None
            return str(found["value"])

        return cls(
            value("vendor_vat_id"),
            value("iban_on_document"),
            value("issuer_name"),
            _stated_ibans(fields.get("iban_on_document") or {}),
        )


def _stated_ibans(found: Mapping[str, Any]) -> tuple[str, ...]:
    """Every IBAN in the stored IBAN field, whatever its flag: the value, the ``values`` a
    flag keeps (several values, readers disagree), and, for readings stored before those were
    kept, the IBANs the note names."""
    stated: list[str] = []
    if found.get("value"):
        stated.append(str(found["value"]))
    stated.extend(str(v) for v in found.get("values") or ())
    note = found.get("note") or ""
    stated.extend(m[1] + m[2] for m in IBAN.finditer(str(note)))
    return tuple(stated)


@dataclass(frozen=True)
class VendorBook:
    """What DATEV knows, read once per pass: the vendor accounts and the customers' VAT IDs,
    plus Max ASP's own VAT IDs."""

    vendors: tuple[Account, ...]
    customer_vat_ids: frozenset[str] = frozenset()
    own_vat_ids: frozenset[str] = frozenset()

    @classmethod
    def from_datev(cls, datev: DatevGateway, own_vat_ids: Iterable[str]) -> "VendorBook":
        return cls(
            vendors=tuple(datev.list_vendors()),
            customer_vat_ids=frozenset(
                clean_vat_id(c.vat_id) for c in datev.list_customers() if c.vat_id
            ),
            own_vat_ids=frozenset(clean_vat_id(v) for v in own_vat_ids),
        )

    def vendor(self, account: str) -> Account | None:
        return next((v for v in self.vendors if str(v.number) == account), None)


@dataclass(frozen=True)
class Match:
    vendor: Account
    by: MatchedBy


@dataclass(frozen=True)
class NoMatch:
    reason: NoMatchReason
    accounts: tuple[int, ...] = ()  # the vendor accounts that fit equally well, if several


def clean_iban(raw: str) -> str:
    return re.sub(r"\s", "", raw).upper()


def masked_iban(iban: str) -> str:
    """Only the country and the last four characters, for texts people read (#36)."""
    return f"{iban[:2]} ... {iban[-4:]}" if len(iban) > 6 else "an IBAN"


def _vat_of(vendor: Account) -> str | None:
    return clean_vat_id(vendor.vat_id) if vendor.vat_id else None


def _ibans_of(vendor: Account) -> frozenset[str]:
    return frozenset(clean_iban(b.iban) for b in vendor.bank_accounts)


def _plain_name(name: str) -> str:
    name = normalise(name)
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(umlaut, plain)
    return name


def _one(candidates: list[Account], by: MatchedBy) -> Match | NoMatch:
    if len(candidates) > 1:
        return NoMatch(NoMatchReason.SEVERAL_VENDORS, tuple(v.number for v in candidates))
    return Match(candidates[0], by)


def _vat_conflict(party: InvoiceParty, vendor: Account) -> bool:
    """The invoice states a VAT ID and the vendor account holds a different one."""
    vat = _vat_of(vendor)
    return party.vat_id is not None and vat is not None and vat != party.vat_id


def match_vendor(party: InvoiceParty, book: VendorBook) -> Match | NoMatch:
    """The one vendor account this invoice belongs to, or why there is none (PR-02)."""
    if party.vat_id is not None:
        if party.vat_id in book.own_vat_ids:
            return NoMatch(NoMatchReason.OWN_VAT_ID)
        by_vat = [v for v in book.vendors if _vat_of(v) == party.vat_id]
        if party.vat_id in book.customer_vat_ids:
            # A customer's VAT ID is often read by mistake from the address block. It only
            # counts when a vendor account has it too and the IBAN confirms that account.
            confirmed = [v for v in by_vat if party.iban and party.iban in _ibans_of(v)]
            if not confirmed:
                return NoMatch(NoMatchReason.CUSTOMER_VAT_ID)
            return _one(confirmed, MatchedBy.VAT_ID_AND_IBAN)
        if by_vat:
            with_iban = [v for v in by_vat if party.iban and party.iban in _ibans_of(v)]
            if with_iban:
                return _one(with_iban, MatchedBy.VAT_ID_AND_IBAN)
            return _one(by_vat, MatchedBy.VAT_ID)
    by_iban = [v for v in book.vendors if party.iban and party.iban in _ibans_of(v)]
    name = _plain_name(party.name) if party.name else ""
    by_name = [v for v in book.vendors if name and _plain_name(v.name) == name]
    for by, candidates in ((MatchedBy.IBAN, by_iban), (MatchedBy.NAME, by_name)):
        if not candidates:
            continue
        found = _one(candidates, by)
        if isinstance(found, Match) and _vat_conflict(party, found.vendor):
            return NoMatch(NoMatchReason.VAT_ID_DIFFERS, (found.vendor.number,))
        return found
    return NoMatch(NoMatchReason.NOT_FOUND)


# ------------------------------------------------------------------------------- the pass


@dataclass
class VendorPass:
    """What one pass did. ``failed`` counts cases that hit an error; the next pass retries."""

    matched: int = 0
    unknown: int = 0
    without_approver: int = 0
    iban_mismatch: int = 0
    resolved: int = 0
    failed: int = 0
    skipped_datev_unavailable: bool = False
    weaker: list[int] = field(default_factory=list)  # case IDs matched by name only


@dataclass(frozen=True)
class _Work:
    case_id: int
    exception_id: int | None
    reason: ExceptionReason | None
    needs_datev: bool  # only finding a vendor reads DATEV; an approver check does not


def _work(connection: Connection, cases: Collection[int] | None) -> list[_Work]:
    """Cases still without a vendor account, matched cases whose vendor has no approver (for
    example after a person pressed carry on, or the approver row was removed), and this step's
    own open exceptions."""
    only = list(cases) if cases is not None else None
    rows = connection.execute(
        text(
            """
            select c.id as case_id, null::bigint as exception_id, null::text as reason,
                   true as needs_datev
            from invoice_case c
            where c.kind in ('incoming_invoice', 'incoming_credit_note')
              and c.status = 'received' and c.partner_account is null
              and exists (select 1 from invoice_reading r
                          where r.case_id = c.id and r.role = 'invoice')
              and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
            union all
            select c.id, null::bigint, null::text, false
            from invoice_case c
            where c.kind in ('incoming_invoice', 'incoming_credit_note')
              and c.status = 'received' and c.partner_account is not null
              and not exists (select 1 from vendor_approver v
                              where v.vendor_account = c.partner_account)
              and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
            union all
            select e.case_id, e.id, e.reason::text, e.reason <> 'vendor_without_approver'
            from exception_item e join invoice_case c on c.id = e.case_id
            where e.resolved_at is null
              and e.reason in ('unknown_vendor', 'vendor_without_approver', 'iban_mismatch')
              and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
            order by 1, 2 nulls first
            """
        ),
        {"only": only},
    ).all()
    return [
        _Work(
            row.case_id,
            row.exception_id,
            ExceptionReason(row.reason) if row.reason else None,
            row.needs_datev,
        )
        for row in rows
    ]


def _party(connection: Connection, case_id: int) -> InvoiceParty | None:
    reading = connection.execute(
        text("select reading from invoice_reading where case_id = :id and role = 'invoice'"),
        {"id": case_id},
    ).scalar_one_or_none()
    return InvoiceParty.from_reading(reading) if reading is not None else None


def match_vendors(
    engine: Engine,
    datev: DatevGateway,
    own_vat_ids: Iterable[str],
    *,
    cases: Collection[int] | None = None,
) -> VendorPass:
    """One pass: match new cases to their vendor account, check the IBAN on the invoice, and
    look again at the open unknown vendor, vendor without approver and IBAN mismatch
    exceptions. ``cases`` limits the pass to these case IDs
    (tests share one database). Each case is its own unit of work, so one failure leaves the
    others alone. DATEV is read once, and only when there is work."""
    result = VendorPass()
    with engine.connect() as connection:
        work = _work(connection, cases)
    if not work:
        return result
    book = VendorBook(())
    if any(item.needs_datev for item in work):
        try:
            book = VendorBook.from_datev(datev, own_vat_ids)
        except DatevError:
            log.warning("DATEV did not answer, vendor matching waits for the next sweep")
            result.skipped_datev_unavailable = True
            work = [item for item in work if not item.needs_datev]
    for item in work:
        try:
            with unit_of_work(engine, VENDOR_ACTOR) as db:
                _handle(db, item, book, result)
        except Exception:
            # The case ID only: no name, VAT ID or IBAN in the log (NF-03).
            log.exception("Vendor matching failed on case %s, next sweep retries", item.case_id)
            result.failed += 1
    return result


def _still_open(db: Connection, exception_id: int) -> bool:
    return bool(
        db.execute(
            text("select resolved_at is null from exception_item where id = :id for update"),
            {"id": exception_id},
        ).scalar()
    )


def _handle(db: Connection, item: _Work, book: VendorBook, result: VendorPass) -> None:
    case = get_case(db, item.case_id)
    if item.exception_id is not None and not _still_open(db, item.exception_id):
        return  # a person resolved it a moment ago
    if item.reason is ExceptionReason.VENDOR_WITHOUT_APPROVER:
        if case.partner_account is not None and vendor_approver(db, case.partner_account):
            resolve_exception(
                db,
                item.exception_id,  # type: ignore[arg-type]
                Outcome.RETRIED,
                resolution=(
                    f"Vendor account {case.partner_account} now has an approver. "
                    "ARGUS checked again and carried on."
                ),
            )
            result.resolved += 1
        return
    if item.reason is ExceptionReason.IBAN_MISMATCH:
        _check_iban_again(db, item, case.partner_account, book, result)
        return
    if item.reason is None and case.status is not CaseStatus.RECEIVED:
        return  # moved on since the work was listed
    if item.reason is None and case.partner_account is not None:
        # Matched before, but the vendor has no approver (again): it must not carry on (VM-02).
        if vendor_approver(db, case.partner_account) is None:
            _queue_without_approver(db, item.case_id, case.partner_account, None)
            result.without_approver += 1
        return
    party = _party(db, item.case_id)
    if party is None:
        return
    found = match_vendor(party, book)
    if isinstance(found, NoMatch):
        if item.reason is None:
            open_exception(
                db,
                ExceptionReason.UNKNOWN_VENDOR,
                _unknown_detail(party, found),
                case_id=item.case_id,
                facts={
                    "why": found.reason.value,
                    "vat_id_read": party.vat_id is not None,
                    "iban_read": party.iban is not None,
                    "name_read": party.name is not None,
                    "accounts": list(found.accounts),
                },
            )
            result.unknown += 1
        return
    account = str(found.vendor.number)
    if case.partner_account != account:
        change_case(db, item.case_id, partner_account=account)
    record_event(
        db,
        VENDOR_MATCHED,
        case_id=item.case_id,
        details={"account": account, "by": found.by.value, "weaker": found.by.weaker},
    )
    result.matched += 1
    if found.by.weaker:
        result.weaker.append(item.case_id)
    differing = [i for i in party.stated_ibans if i not in _ibans_of(found.vendor)]
    if differing:
        _queue_iban_mismatch(db, item.case_id, differing, party, found.vendor, book)
        result.iban_mismatch += 1
    if vendor_approver(db, account) is None:
        _queue_without_approver(db, item.case_id, account, found.vendor.name)
        result.without_approver += 1
    if item.reason is ExceptionReason.UNKNOWN_VENDOR:
        # Opened before resolving, so the case stays stopped while it still has no approver.
        resolve_exception(
            db,
            item.exception_id,  # type: ignore[arg-type]
            Outcome.FIXED_IN_DATEV,
            resolution=(
                f"Vendor account {account} now exists in DATEV. ARGUS matched it and carried on."
            ),
        )
        result.resolved += 1


def _check_iban_again(
    db: Connection,
    item: _Work,
    account: str | None,
    book: VendorBook,
    result: VendorPass,
) -> None:
    """The block ends by itself only when DATEV now holds the invoice's IBAN on the vendor
    account: someone changed the bank details in DATEV, the one place they may change (VM-04).
    Anything else leaves the case blocked until a person rejects it."""
    party = _party(db, item.case_id)
    vendor = book.vendor(account) if account is not None else None
    if party is None or not party.stated_ibans or vendor is None:
        return
    if not set(party.stated_ibans) <= _ibans_of(vendor):
        return  # every IBAN the document states must be on file, not just one
    if vendor_approver(db, account) is None and not _has_open(  # type: ignore[arg-type]
        db, item.case_id, ExceptionReason.VENDOR_WITHOUT_APPROVER
    ):
        # Opened before resolving, so the case never leaves the queue without an approver.
        _queue_without_approver(db, item.case_id, account, vendor.name)  # type: ignore[arg-type]
        result.without_approver += 1
    resolve_exception(
        db,
        item.exception_id,  # type: ignore[arg-type]
        Outcome.FIXED_IN_DATEV,
        resolution=(
            f"Every IBAN on the invoice is now on vendor account {account} in DATEV. "
            "ARGUS checked again and carried on."
        ),
    )
    result.resolved += 1


def _has_open(db: Connection, case_id: int, reason: ExceptionReason) -> bool:
    return bool(
        db.execute(
            text(
                "select exists (select 1 from exception_item where case_id = :id "
                "and reason = :reason and resolved_at is null)"
            ),
            {"id": case_id, "reason": reason.value},
        ).scalar()
    )


def _queue_iban_mismatch(
    db: Connection,
    case_id: int,
    differing: list[str],
    party: InvoiceParty,
    vendor: Account,
    book: VendorBook,
) -> None:
    """Block the invoice as a possible fraud case (PR-05). The detail shows IBANs masked only,
    and the audit log gets no IBAN at all (NF-03)."""
    on_file = sorted(_ibans_of(vendor))
    if on_file:
        has = "which has " + ", ".join(masked_iban(i) for i in on_file)
    else:
        has = "which has no IBAN"
    others = [
        v.number
        for v in book.vendors
        if v.number != vendor.number and _ibans_of(v) & set(differing)
    ]
    belongs = (
        f" An IBAN of this invoice is on another vendor account in DATEV: "
        f"{', '.join(str(n) for n in others)}."
        if others
        else ""
    )
    shown = ", ".join(masked_iban(i) for i in differing)
    if len(party.stated_ibans) > 1:
        stated = (
            f"This invoice states {len(party.stated_ibans)} IBANs. {shown} "
            f"{'is' if len(differing) == 1 else 'are'} not on vendor account"
        )
    else:
        stated = f"The IBAN on this invoice ({shown}) is not on vendor account"
    open_exception(
        db,
        ExceptionReason.IBAN_MISMATCH,
        (
            f"Possible fraud. {stated} {vendor.number} ({vendor.name}) in DATEV, {has}."
            f"{belongs} A changed bank account is a common fraud pattern, so ARGUS has blocked "
            "this invoice and will not pay it. Do not change the bank details because the "
            "invoice or an email asks for it. Check with the vendor on a phone number you "
            "already know. If the new IBAN is right, change it in DATEV and ARGUS carries on by "
            "itself. If not, reject the invoice."
        ),
        case_id=case_id,
        facts={
            "account": str(vendor.number),
            "possible_fraud": True,
            "ibans_on_file": len(on_file),
            "ibans_stated": len(party.stated_ibans),
            "ibans_differing": len(differing),
            "read_with_flag": party.iban_flagged,
            "other_accounts": [str(n) for n in others],
        },
    )


def _queue_without_approver(
    db: Connection, case_id: int, account: str, vendor_name: str | None
) -> None:
    named = f" ({vendor_name})" if vendor_name else ""
    open_exception(
        db,
        ExceptionReason.VENDOR_WITHOUT_APPROVER,
        (
            f"Vendor account {account}{named} has no approver yet. Add one in the approver "
            "table. ARGUS carries on by itself once it has one."
        ),
        case_id=case_id,
        facts={"account": account},
    )


def _unknown_detail(party: InvoiceParty, found: NoMatch) -> str:
    said = [
        part
        for part in (
            f"issuer {party.name}" if party.name else None,
            f"VAT ID {party.vat_id}" if party.vat_id else None,
            f"IBAN {masked_iban(party.iban)}" if party.iban else None,
        )
        if part
    ]
    read = f" The invoice names {', '.join(said)}." if said else ""
    accounts = ", ".join(str(a) for a in found.accounts)
    carry_on = " ARGUS carries on by itself once DATEV has exactly one vendor account for it."
    match found.reason:
        case NoMatchReason.NOT_FOUND:
            return (
                f"No vendor account in DATEV matches this invoice.{read} Please create the "
                "vendor in DATEV. ARGUS carries on by itself once it exists."
            )
        case NoMatchReason.SEVERAL_VENDORS:
            return (
                f"Vendor accounts {accounts} in DATEV all match this invoice.{read} Please "
                f"clean them up in DATEV.{carry_on}"
            )
        case NoMatchReason.VAT_ID_DIFFERS:
            return (
                f"Vendor account {accounts} matches by IBAN or name, but holds a different VAT "
                f"ID than the invoice.{read} Please check the vendor in DATEV.{carry_on}"
            )
        case NoMatchReason.OWN_VAT_ID:
            return (
                "The only VAT ID ARGUS read on this invoice is Max ASP's own, so it cannot "
                f"tell the vendor.{read} Please check the invoice and reject it if it is not "
                "a supplier's invoice."
            )
        case NoMatchReason.CUSTOMER_VAT_ID:
            return (
                "The VAT ID on this invoice belongs to a customer account, so ARGUS cannot "
                f"tell the vendor.{read} Please check the invoice and the vendor in DATEV."
                f"{carry_on}"
            )
    raise AssertionError(f"no text for {found.reason}")

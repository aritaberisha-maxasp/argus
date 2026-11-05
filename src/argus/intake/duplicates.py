"""Catch an invoice that arrives twice (#31, IN-10).

The same invoice often comes twice, for example by email and again as a copy a week later. A
second invoice with the same vendor, invoice number and amount is flagged and not processed again:

1. :func:`duplicate_key` builds the key from the case key of #30 (issuer plus invoice number) and
   the gross amount with its currency. The number is compared in a standard form, so
   ``RE-2026/0042`` and ``re 2026 0042`` match (design note, section 5).
2. :func:`flag_if_duplicate` stores the key on the case. The unique index on
   ``(kind, duplicate_key)`` makes the database the final judge, so two copies sorted at the same
   second cannot both get through. When the key is taken, the new case points to the first one
   (``duplicate_of``) and goes to the exception queue as a duplicate. It is never sent for
   approval, booked or paid while that exception is open.
3. :func:`resolve_duplicate` is the person's answer. Confirmed: the case closes as a duplicate.
   Not a duplicate: the case keeps the key with its own case number added, so it no longer
   collides, and carries on.

The vendor is not matched to a DATEV account yet (#35), so the issuer part of the key is the VAT
ID, or the normalised name when the invoice states no VAT ID, exactly as in the case key.
"""

import re
from typing import Any

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.cases.store import (
    DuplicateRefused,
    change_case,
    open_exception,
    resolve_exception,
)

# Characters left out of an invoice number before comparing (design note, section 5).
_NUMBER_NOISE = re.compile(r"[\s\-/._]")


def duplicate_key(
    case_key: str | None, gross_cents: int | None, currency: str | None
) -> str | None:
    """``<issuer>|<number>|<gross in cents>|<currency>``, for example
    ``vat:DE012345678|RE20260042|11900|EUR``. None while the vendor, the number, the amount or
    the currency is not known, because a key without one of them could match a different
    invoice."""
    if not case_key or gross_cents is None or not currency or "|" not in case_key:
        return None
    issuer, number = case_key.split("|", 1)
    number = _NUMBER_NOISE.sub("", number).upper()
    if not issuer or not number:
        return None
    return f"{issuer}|{number}|{gross_cents}|{currency.upper()}"


def flag_if_duplicate(connection: Connection, case_id: int) -> int | None:
    """Give a case its duplicate key, or flag it as a duplicate when another case of the same
    kind already holds the key. Returns the first case's ID while the case waits as a
    duplicate, else None.

    Runs inside the caller's unit of work. The sweep calls it for every new case. Whoever
    confirms or corrects a number, amount or currency calls it again: the database clears the
    key on such a change and refuses approval without a key (migration 0010), so a case that
    is not checked again gets stuck instead of being paid twice. Calling it twice changes
    nothing the second time.
    """
    row = (
        connection.execute(
            text(
                """
                select kind, status, case_key, document_number, gross_minor, currency,
                       duplicate_key, duplicate_of,
                       (select max((x.facts->>'duplicate_of')::bigint) from exception_item x
                        where x.case_id = c.id and x.reason = 'duplicate'
                          and x.resolved_at is null) as waits_on
                from invoice_case c where id = :id
                """
            ),
            {"id": case_id},
        )
        .mappings()
        .one()
    )
    if row["waits_on"] is not None:
        return int(row["waits_on"])  # already in the queue as a duplicate
    if row["duplicate_key"] is not None or row["status"] == CaseStatus.CLOSED.value:
        return None
    key = _key_of(row)
    if key is None:
        return None
    kind = CaseKind(row["kind"])
    first = _holder(connection, kind, key)
    if first is None:
        try:
            # A savepoint, because a refused statement aborts the whole transaction otherwise
            # (review of PR 79). A parallel sweep may have taken the key a moment ago.
            with connection.begin_nested():
                change_case(connection, case_id, duplicate_key=key)
            return None
        except DuplicateRefused:
            first = _holder(connection, kind, key)
            if first is None:
                raise
    if row["duplicate_of"] is None:
        # Set once (migration 0010). A case a person already cleared keeps its first link;
        # the new exception names the case it collides with now.
        change_case(connection, case_id, duplicate_of=first)
    open_exception(
        connection,
        ExceptionReason.DUPLICATE,
        f"This invoice looks like a second copy of case {first}: same issuer, invoice number"
        " and amount. Confirm it is a duplicate to close this case, or say it is not one to let"
        " it carry on.",
        case_id=case_id,
        facts={"duplicate_of": first},
    )
    return first


def _key_of(row: Any) -> str | None:
    """The key from the issuer of the case key and the case's current number, amount and
    currency, so a corrected number counts too."""
    case_key, number = row["case_key"], row["document_number"]
    if not case_key or not number or "|" not in case_key:
        return None
    issuer = case_key.split("|", 1)[0]
    return duplicate_key(f"{issuer}|{number}", row["gross_minor"], row["currency"])


def _holder(connection: Connection, kind: CaseKind, key: str) -> int | None:
    found = connection.execute(
        text("select id from invoice_case where kind = :kind and duplicate_key = :key"),
        {"kind": kind.value, "key": key},
    ).scalar_one_or_none()
    return int(found) if found is not None else None


def resolve_duplicate(
    connection: Connection,
    exception_id: int,
    *,
    confirmed: bool,
    resolution: str | None = None,
) -> None:
    """A person's answer to a duplicate in the exception queue. Run in a unit of work under
    that person. ``confirmed`` closes the case as a duplicate; otherwise the case gets its key
    with its own case number added and goes back to where it was. "Not a duplicate" needs a
    ``resolution`` saying why, and the database only takes it from a person."""
    row = (
        connection.execute(
            text(
                "select e.reason, c.id, c.case_key, c.document_number, c.gross_minor, c.currency"
                " from exception_item e join invoice_case c on c.id = e.case_id"
                " where e.id = :id"
            ),
            {"id": exception_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["reason"] != ExceptionReason.DUPLICATE.value:
        raise ValueError(f"exception {exception_id} is not a duplicate of a case")
    if not confirmed and not (resolution and resolution.strip()):
        # Letting a possible second payment through needs a reason; the database also refuses
        # it without one, and from anyone but a person (migration 0010).
        raise ValueError("saying a case is not a duplicate needs a reason")
    if confirmed:
        resolve_exception(
            connection,
            exception_id,
            Outcome.CONFIRMED_DUPLICATE,
            resolution=resolution,
            case_to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.DUPLICATE,
        )
        return
    key = _key_of(row)
    if key is not None:
        change_case(connection, row["id"], duplicate_key=f"{key}|case:{row['id']}")
    resolve_exception(connection, exception_id, Outcome.NOT_A_DUPLICATE, resolution=resolution)

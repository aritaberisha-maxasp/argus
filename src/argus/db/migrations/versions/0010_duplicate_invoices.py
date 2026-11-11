"""Rules for the link from a duplicate invoice to its first case (issue #31, IN-10).

Covers IN-10 and NF-01. The duplicate key and its unique index on ``(kind, duplicate_key)`` exist
since 0002: the database already refuses a second case with the same key. The intake now fills
the key (:mod:`argus.intake.duplicates`). When the key is taken, the new case stays without it,
points to the first case with ``duplicate_of`` and goes to the exception queue.

This migration adds what keeps that link honest:

- ``duplicate_of`` is set once and never changes or goes away, so the audit trail always shows
  which case a duplicate was taken for, even after a person decided it was not one.
- A case is never a duplicate of itself.
- A case is only a duplicate of a case of the same kind. An invoice and a credit note with the
  same number and amount are two documents, as the unique index already says.
- An incoming invoice or credit note is never sent for approval or approved without its
  duplicate key, so a case that was never checked gets stuck instead of being paid twice
  (review of PR 109).
- A change to the number, the amount or the currency clears the key, unless the same change
  sets a new one. The case then has to be checked again before approval (review of PR 109).
- A duplicate exception ends only as ``confirmed_duplicate``, ``not_a_duplicate`` or
  ``rejected``. ``not_a_duplicate`` lets a possible second payment through, so only a person
  may decide it, and only with a reason (compliance on PR 109, design note section 5).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-05
"""

from alembic import op

from argus.db import run_script

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = r"""
create function invoice_case_duplicate_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'UPDATE' and old.duplicate_of is not null
     and new.duplicate_of is distinct from old.duplicate_of then
    raise exception 'case % is linked as a duplicate of case % and the link never changes',
      old.id, old.duplicate_of;
  end if;
  if tg_op = 'UPDATE' and new.duplicate_key is not distinct from old.duplicate_key
     and (new.document_number is distinct from old.document_number
          or new.gross_minor is distinct from old.gross_minor
          or new.currency is distinct from old.currency) then
    new.duplicate_key := null;
  end if;
  if tg_op = 'UPDATE' and new.status in ('awaiting_approval', 'approved')
     and new.status is distinct from old.status
     and new.kind in ('incoming_invoice', 'incoming_credit_note')
     and new.duplicate_key is null then
    raise exception 'case % has no duplicate key; check it for duplicates before approval',
      new.id;
  end if;
  if new.duplicate_of is not null then
    if new.duplicate_of = new.id then
      raise exception 'case % cannot be a duplicate of itself', new.id;
    end if;
    if (select kind from invoice_case where id = new.duplicate_of) is distinct from new.kind then
      raise exception 'case % can only be a duplicate of a case of the same kind', new.id;
    end if;
  end if;
  return new;
end $$;
create trigger invoice_case_duplicate_guard before insert or update on invoice_case
  for each row execute function invoice_case_duplicate_guard();

-- The reason is compared as text, like 0008 does, so this needs no new enum value.
create function duplicate_exception_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare a text[] := current_actor();
begin
  if old.reason::text <> 'duplicate' or new.outcome is null or old.outcome is not null then
    return new;
  end if;
  if new.outcome::text not in ('confirmed_duplicate', 'not_a_duplicate', 'rejected') then
    raise exception
      'a duplicate can only end as confirmed_duplicate, not_a_duplicate or rejected';
  end if;
  if new.outcome::text = 'not_a_duplicate' then
    if a[1] <> 'user' then
      raise exception 'only a person can decide that case % is not a duplicate', old.case_id;
    end if;
    if coalesce(btrim(new.resolution), '') = '' then
      raise exception 'deciding that case % is not a duplicate needs a reason', old.case_id;
    end if;
  end if;
  return new;
end $$;
create trigger duplicate_exception_guard before update on exception_item
  for each row execute function duplicate_exception_guard();
"""

DOWNGRADE = r"""
drop trigger duplicate_exception_guard on exception_item;
drop function duplicate_exception_guard();
drop trigger invoice_case_duplicate_guard on invoice_case;
drop function invoice_case_duplicate_guard();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

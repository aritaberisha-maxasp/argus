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
"""

DOWNGRADE = r"""
drop trigger invoice_case_duplicate_guard on invoice_case;
drop function invoice_case_duplicate_guard();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

"""A person corrects the invoice number, amount or currency of a case (issue #112).

Covers EX-01, IN-10, PR-01 and NF-01. When ARGUS cannot read the gross amount or currency of an
incoming invoice with confidence, the case has no duplicate key, and migration 0010 refuses
approval without one. Until now nobody could fix those values, so the case was stuck for good.

This migration adds:

- A new exception reason ``values_to_check``. The intake opens it for a case that has no
  duplicate key because its number, amount or currency is missing or not confirmed. It always
  belongs to a case. It ends only as ``data_corrected``, after a correction of that case for
  this exception, or as ``rejected``.
- ``case_correction``: one row per correction, never changed or removed. A correction needs a
  person (a ``user:`` actor, never a job), the person's name as it was at that moment (ADR 0008)
  and a reason of at least three characters. The database fills in the old values, who and
  when. All three new values are required, so the duplicate key can be built right after.
  Inserting the row is what changes the case: the database copies the new values onto the case
  in the same statement.
- On ``invoice_case`` the number, amount and currency now only change through such a row in
  the same transaction, with exactly those values. Any other change is refused. So a job can
  never change what gets paid, and every change has a person and a reason.
- Every correction is written into the audit log as ``case_corrected`` with the old and new
  values, the person's name and the reason, next to the actor that carries the Entra object ID.
  Only the trigger writes it, like ``intake_mode_changed`` in 0012. The usual
  ``case_changed`` event with old and new values is written as well.

Migration 0010 still clears the duplicate key on such a change, so the case is checked for
duplicates again before it can go to approval.

The new reason is compared as text, because a value added to an enum cannot be used in the
same transaction.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-14
"""

from alembic import op

from argus.db import run_script

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter type exception_reason add value 'values_to_check';

create table case_correction (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  exception_id bigint references exception_item(id),
  old_document_number text,               -- set by the database from the case
  old_gross_minor bigint,
  old_currency char(3),
  document_number text not null check (length(btrim(document_number)) between 1 and 100),
  gross_minor bigint not null,            -- cents, never floats
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  reason text not null check (length(btrim(reason)) between 3 and 500),
  corrected_by text not null,             -- user:<Entra object ID>, set by the database
  corrected_by_name text not null check (length(btrim(corrected_by_name)) between 1 and 200),
  corrected_by_address text check (length(btrim(corrected_by_address)) between 3 and 320),
  corrected_at timestamptz not null,      -- set by the database
  txid bigint not null                    -- the transaction, set by the database
);
create index case_correction_case on case_correction (case_id, id);
create index case_correction_exception on case_correction (exception_id)
  where exception_id is not null;

create function case_correction_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  x record;
begin
  if a[1] <> 'user' then
    raise exception 'only a person can correct the number, amount or currency of case %',
      new.case_id;
  end if;
  if coalesce(length(btrim(new.reason)), 0) < 3 then
    raise exception 'a correction of case % needs a reason', new.case_id;
  end if;
  select document_number, gross_minor, currency into c
  from invoice_case where id = new.case_id for update;
  if not found then
    raise exception 'case % does not exist', new.case_id;
  end if;
  if new.exception_id is not null then
    select case_id, resolved_at into x from exception_item where id = new.exception_id;
    if not found or x.case_id is distinct from new.case_id or x.resolved_at is not null then
      raise exception 'exception % is not an open exception of case %',
        new.exception_id, new.case_id;
    end if;
  end if;
  if (new.document_number, new.gross_minor, new.currency)
     is not distinct from (c.document_number, c.gross_minor, c.currency) then
    raise exception 'the correction of case % changes nothing', new.case_id;
  end if;
  new.old_document_number := c.document_number;
  new.old_gross_minor := c.gross_minor;
  new.old_currency := c.currency;
  new.corrected_by := a[1] || ':' || a[2];
  new.corrected_at := clock_timestamp();
  new.txid := txid_current();
  return new;
end $$;
create trigger case_correction_guard before insert on case_correction
  for each row execute function case_correction_guard();

-- The row is the correction: it puts the new values on the case. Runs as the caller, so the
-- case's own rules (closed, frozen after approval, duplicate key cleared) all apply.
create function case_correction_apply() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  update invoice_case
  set document_number = new.document_number, gross_minor = new.gross_minor,
      currency = new.currency
  where id = new.case_id;
  return null;
end $$;
create trigger case_correction_apply after insert on case_correction
  for each row execute function case_correction_apply();

-- Runs as the owner, like the other audit triggers.
create function case_correction_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, exception_id, event_type, details)
  values (new.case_id, new.exception_id, 'case_corrected',
          jsonb_build_object(
            'correction_id', new.id,
            'document_number', jsonb_build_object('old', new.old_document_number,
                                                  'new', new.document_number),
            'gross_minor', jsonb_build_object('old', new.old_gross_minor,
                                              'new', new.gross_minor),
            'currency', jsonb_build_object('old', new.old_currency, 'new', new.currency),
            'name', new.corrected_by_name,
            'reason', new.reason)
          || jsonb_strip_nulls(jsonb_build_object('sign_in_address', new.corrected_by_address)));
  return null;
end $$;
create trigger case_correction_audit after insert on case_correction
  for each row execute function case_correction_audit();

create function case_correction_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'a correction is kept as it was made and cannot change'; end $$;
create trigger case_correction_no_change before update or delete on case_correction
  for each row execute function case_correction_fixed();
create trigger case_correction_no_truncate before truncate on case_correction
  execute function case_correction_fixed();

-- Number, amount and currency change only through a correction made in this transaction,
-- and only to the values that correction names.
create function invoice_case_correction_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if not exists (
    select 1 from case_correction k
    where k.id = (select max(id) from case_correction where case_id = new.id)
      and k.txid = txid_current()
      and k.document_number = new.document_number
      and k.gross_minor = new.gross_minor
      and k.currency = new.currency) then
    raise exception
      'the number, amount and currency of case % only change through a correction by a person',
      new.id;
  end if;
  return new;
end $$;
create trigger invoice_case_correction_guard before update on invoice_case
  for each row
  when ((new.document_number, new.gross_minor, new.currency)
        is distinct from (old.document_number, old.gross_minor, old.currency))
  execute function invoice_case_correction_guard();

create function values_to_check_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.reason::text = 'values_to_check' and new.case_id is null then
      raise exception 'values to check always belong to a case';
    end if;
    return new;
  end if;
  if old.reason::text <> 'values_to_check' or new.outcome is null or old.outcome is not null then
    return new;
  end if;
  if new.outcome not in ('data_corrected', 'rejected') then
    raise exception 'values to check can only end as data_corrected or rejected';
  end if;
  if new.outcome = 'data_corrected'
     and not exists (select 1 from case_correction where exception_id = old.id) then
    raise exception 'exception % ends as data_corrected only after a person corrected case %',
      old.id, old.case_id;
  end if;
  return new;
end $$;
create trigger values_to_check_guard before insert or update on exception_item
  for each row execute function values_to_check_guard();

-- Like the other database events: only the trigger above writes this one.
create function audit_case_correction_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type = 'case_corrected' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_case_correction_events before insert on audit_event
  for each row execute function audit_case_correction_events();

grant select on case_correction to argus_app;
grant insert (case_id, exception_id, document_number, gross_minor, currency, reason,
              corrected_by_name, corrected_by_address) on case_correction to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from case_correction)
     or exists (select 1 from exception_item where reason::text = 'values_to_check') then
    raise exception 'corrections were made; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
drop trigger audit_case_correction_events on audit_event;
drop function audit_case_correction_events();
drop trigger values_to_check_guard on exception_item;
drop function values_to_check_guard();
drop trigger invoice_case_correction_guard on invoice_case;
drop function invoice_case_correction_guard();
drop table case_correction;
drop function case_correction_fixed();
drop function case_correction_audit();
drop function case_correction_apply();
drop function case_correction_guard();
-- Postgres cannot drop one value of an enum, so the type is built again without it.
alter type exception_reason rename to exception_reason_0013;
create type exception_reason as enum (
  'uncertain_classification','unreadable_document','unknown_vendor','unknown_customer',
  'vendor_without_approver','duplicate','iban_mismatch','invoice_check_failed','booking_error',
  'unmatched_bank_transaction','bank_balance_difference',
  'number_sequence_gap','payment_failed','customer_query','forward_failed',
  'audit_copy_failed','audit_check_failed');
alter table exception_item
  alter column reason type exception_reason using reason::text::exception_reason;
alter table exception_exit
  alter column reason type exception_reason using reason::text::exception_reason;
drop type exception_reason_0013;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

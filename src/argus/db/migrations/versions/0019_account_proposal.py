"""The G/L account and tax key ARGUS proposes for an incoming invoice (issue #37, PR-04).

Covers PR-04, EX-01 and NF-01. Approvers should not have to know the chart of accounts, so once
an incoming invoice or credit note is matched to its vendor account (#35), ARGUS proposes the
G/L account and tax key that vendor was booked to before (:mod:`argus.intake.accounts`).

This migration adds:

- ``account_proposal``: one row per proposal, never changed or removed. It names the case, the
  vendor account it was made for, the G/L account, the tax key (blank when the history has none)
  and the chart of accounts. It says where it came from: ``booking_history`` names the document
  number and date of the DATEV posting it copied and may only be written by a job;
  ``person`` needs a person (a ``user:`` actor), their name as it was at that moment
  (ADR 0008) and a reason of at least three characters other than spaces. The database fills
  in who, when and the transaction. The current proposal of a case is its newest row for the
  vendor account the case has now.
- A proposal is only made for an incoming invoice or credit note that has a vendor account and
  is still in Received or Exception, so it is settled before approval and cannot change after.
- A new exception reason ``no_account_proposal``: the vendor has no usable booking history, or
  its latest postings point to more than one account or tax key, so ARGUS does not guess. It
  always belongs to a case and ends only as ``data_corrected`` (a person entered the account
  for this exception), ``fixed_in_datev`` (a later sweep found history and proposed from it for
  this exception) or ``rejected``.
- Every proposal is written into the audit log as ``account_proposed``. Only the trigger writes
  it, like ``case_corrected`` in 0013.

The new reason is compared as text, because a value added to an enum cannot be used in the same
transaction. A downgrade never loses a proposal: with proposals in the database it keeps the
table and its rules (see the downgrade for why).

Revision ID: 0019
Revises: 0017
Create Date: 2026-11-20
"""

from alembic import op

from argus.db import run_script

revision = "0019"
down_revision = "0017"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter type exception_reason add value if not exists 'no_account_proposal';

create table if not exists account_proposal (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  exception_id bigint references exception_item(id),
  vendor_account text not null,
  gl_account text not null check (gl_account ~ '^[0-9]{4,8}$'),
  tax_key text not null default '' check (tax_key ~ '^[0-9]{0,4}$'),
  chart text not null check (chart in ('SKR03', 'SKR04')),
  source text not null check (source in ('booking_history', 'person')),
  from_document_number text check (length(btrim(from_document_number)) between 1 and 36),
  from_posting_date date,
  reason text check (length(correction_trim(reason)) between 3 and 500),
  proposed_by_name text check (length(correction_trim(proposed_by_name)) between 1 and 200),
  proposed_by_address text
    check (length(correction_trim(proposed_by_address)) between 3 and 320),
  proposed_by text not null,              -- system:<job> or user:<Entra object ID>, by the database
  proposed_at timestamptz not null,       -- set by the database
  txid bigint not null,                   -- the transaction, set by the database
  check ((source = 'booking_history')
         = (from_document_number is not null and from_posting_date is not null)),
  check (source = 'person' or (reason is null and proposed_by_name is null
                               and proposed_by_address is null))
);
create index if not exists account_proposal_case on account_proposal (case_id, id);
create index if not exists account_proposal_exception on account_proposal (exception_id)
  where exception_id is not null;

create or replace function account_proposal_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  x record;
begin
  if new.source = 'booking_history' and a[1] <> 'system' then
    raise exception 'only ARGUS proposes an account from the booking history of case %',
      new.case_id;
  end if;
  if new.source = 'person' then
    if a[1] <> 'user' then
      raise exception 'only a person can enter the account of case % by hand', new.case_id;
    end if;
    if coalesce(length(correction_trim(new.proposed_by_name)), 0) < 1
       or coalesce(length(correction_trim(new.reason)), 0) < 3 then
      raise exception 'an account entered by hand for case % needs the name and a reason',
        new.case_id;
    end if;
  end if;
  select kind, status, partner_account into c from invoice_case where id = new.case_id for update;
  if not found then
    raise exception 'case % does not exist', new.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', new.case_id;
  end if;
  if c.status not in ('received', 'exception') then
    raise exception 'the account of case % is settled before approval and cannot change in %',
      new.case_id, c.status;
  end if;
  if c.partner_account is distinct from new.vendor_account then
    raise exception 'the proposal for case % names another vendor account than the case',
      new.case_id;
  end if;
  if new.exception_id is not null then
    select case_id, reason::text as reason, resolved_at into x
    from exception_item where id = new.exception_id;
    if not found or x.case_id is distinct from new.case_id or x.resolved_at is not null
       or x.reason <> 'no_account_proposal' then
      raise exception 'exception % is not an open missing account exception of case %',
        new.exception_id, new.case_id;
    end if;
  end if;
  new.proposed_by := a[1] || ':' || a[2];
  new.proposed_at := clock_timestamp();
  new.txid := txid_current();
  return new;
end $$;
create or replace trigger account_proposal_guard before insert on account_proposal
  for each row execute function account_proposal_guard();

-- Runs as the owner, like the other audit triggers.
create or replace function account_proposal_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, exception_id, event_type, details)
  values (new.case_id, new.exception_id, 'account_proposed',
          jsonb_strip_nulls(jsonb_build_object(
            'proposal_id', new.id,
            'vendor_account', new.vendor_account,
            'gl_account', new.gl_account,
            'tax_key', new.tax_key,
            'chart', new.chart,
            'source', new.source,
            'from_document_number', new.from_document_number,
            'from_posting_date', new.from_posting_date,
            'name', new.proposed_by_name,
            'sign_in_address', new.proposed_by_address,
            'reason', new.reason)));
  return null;
end $$;
create or replace trigger account_proposal_audit after insert on account_proposal
  for each row execute function account_proposal_audit();

create or replace function account_proposal_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'a proposal is kept as it was made and cannot change'; end $$;
create or replace trigger account_proposal_no_change before update or delete on account_proposal
  for each row execute function account_proposal_fixed();
create or replace trigger account_proposal_no_truncate before truncate on account_proposal
  execute function account_proposal_fixed();

create or replace function no_account_proposal_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.reason::text = 'no_account_proposal' and new.case_id is null then
      raise exception 'a missing account always belongs to a case';
    end if;
    return new;
  end if;
  if old.reason::text <> 'no_account_proposal' or new.outcome is null
     or old.outcome is not null then
    return new;
  end if;
  if new.outcome not in ('data_corrected', 'fixed_in_datev', 'rejected') then
    raise exception
      'a missing account can only end as data_corrected, fixed_in_datev or rejected';
  end if;
  if new.outcome = 'data_corrected' and not exists (
       select 1 from account_proposal where exception_id = old.id and source = 'person') then
    raise exception 'exception % ends as data_corrected only after a person entered the account',
      old.id;
  end if;
  if new.outcome = 'fixed_in_datev' and not exists (
       select 1 from account_proposal
       where exception_id = old.id and source = 'booking_history') then
    raise exception
      'exception % ends as fixed_in_datev only after ARGUS proposed from the booking history',
      old.id;
  end if;
  return new;
end $$;
create or replace trigger no_account_proposal_guard before insert or update on exception_item
  for each row execute function no_account_proposal_guard();

-- Like the other database events: only the trigger above writes this one.
create or replace function audit_account_proposal_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type = 'account_proposed' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_account_proposal_events before insert on audit_event
  for each row execute function audit_account_proposal_events();

grant select on account_proposal to argus_app;
grant insert (case_id, exception_id, vendor_account, gl_account, tax_key, chart, source,
              from_document_number, from_posting_date, reason, proposed_by_name,
              proposed_by_address) on account_proposal to argus_app;
"""

DOWNGRADE = r"""
-- Proposals are part of the audit trail, which is kept for ten years (NF-01). So a downgrade
-- with proposals or missing account exceptions in the database keeps the table and all its
-- rules: code from before 0019 never touches them, and the upgrade picks them up again (every
-- statement above can run twice). Only an unused database loses them. Refusing instead would
-- block every downgrade below this one, such as the check of migration 0017.
--
-- The enum keeps the value 'no_account_proposal' either way. Postgres cannot drop one value of
-- an enum, and building the type again changes its identity under every open connection,
-- which breaks their cached trigger plans. An upgrade adds it only if it is missing, and a
-- downgrade below 0013 builds the type again without it.
do $$ begin
  if exists (select 1 from account_proposal)
     or exists (select 1 from exception_item where reason::text = 'no_account_proposal') then
    raise notice 'account proposals exist; their table and rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_account_proposal_events on audit_event;
  drop function audit_account_proposal_events();
  drop trigger no_account_proposal_guard on exception_item;
  drop function no_account_proposal_guard();
  drop table account_proposal;
  drop function account_proposal_fixed();
  drop function account_proposal_audit();
  drop function account_proposal_guard();
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

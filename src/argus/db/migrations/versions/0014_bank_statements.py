"""The monthly Wise statement: stored unaltered, imported once, balances reconciled (issue #56).

Covers BA-01, BA-07, BA-08 and NF-01. Benjamin sends the statement of each Wise balance once a
month (ADR 0004, Arita's answer on #4). ARGUS stores the file in the document store under its
SHA-256 and records it here. Each transaction becomes a bank transaction case, once.

This migration adds:

- ``bank_statement``: one row per statement file ARGUS looked at, never changed or removed. It
  keeps the fingerprint, the source (Benjamin's email or the agreed folder), the currency, the
  month, the opening and closing balance and the outcome:
  ``imported`` (balances add up, its transactions became cases), ``blocked`` (a balance does
  not add up, nothing was imported, a person looks at it) or ``month_already_imported`` (a
  different file for a month that is already in, nothing imported). Only one statement per
  currency and month can be imported. The same file is recorded once, except that a blocked
  file sent again may be checked again, for example once the month before it is in.
- The database checks the main rule itself: an imported statement opens with the closing
  balance of the last imported month before it, in the same currency (BA-08). So no code path
  can import a month that does not follow on.
- ``bank_transaction`` now names its statement, and the database refuses a transaction whose
  statement is not imported, is in another currency or covers another month. It also keeps the
  counterparty, the payment reference and the description for the matching stories (#57, #58).
  They stay out of the audit log (NF-03).
- ``bank_notice``: the emails ARGUS sends Benjamin about statements: a balance difference, a
  missing statement on the 2nd working day and an overdue one on the 5th. One per statement or
  per kind and month, so a crash or a second run never sends a second one by design. A notice
  starts ``pending`` and ends once as ``sent``, or ``not_sent_shadow`` while ARGUS runs in
  shadow mode (#72).
- ``exception_item.statement_id`` now points at ``bank_statement``.

The database writes the audit events ``bank_statement_recorded`` and ``bank_notice_sent`` or
``bank_notice_not_sent`` itself and refuses them from the application, like the email events of
0004. ``bank_transaction_imported`` (0002) now also carries the statement. The events hold the
fingerprint, IDs, currency, month and amounts, never a file name, counterparty or reference.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-15
"""

from alembic import op

from argus.db import run_script

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = r"""
create table bank_statement (
  id bigint generated always as identity primary key,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  location text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  filename text,                          -- as sent; never in the audit log
  source text not null check (source in ('email','folder')),
  source_email_id bigint references mailbox_email(id),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  period date not null check (extract(day from period) = 1),   -- first day of the month
  opening_minor bigint not null,          -- cents, never floats
  closing_minor bigint not null,
  transaction_count integer not null check (transaction_count >= 1),
  outcome text not null check (outcome in ('imported','blocked','month_already_imported')),
  recorded_at timestamptz not null,       -- set by the database
  check (location = 'sha256/' || sha256)
);
-- One imported statement per balance and month (BA-07), and the same file counts once.
create unique index bank_statement_one_per_month on bank_statement (currency, period)
  where outcome = 'imported';
create unique index bank_statement_file_once on bank_statement (sha256)
  where outcome <> 'blocked';
create index bank_statement_sha256 on bank_statement (sha256);

create function bank_statement_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare prev record;
begin
  new.recorded_at := clock_timestamp();
  if new.outcome = 'imported' then
    if exists (select 1 from bank_statement where currency = new.currency
               and period > new.period and outcome = 'imported') then
      raise exception 'a later month of the % statement is already imported', new.currency;
    end if;
    select closing_minor, period into prev from bank_statement
    where currency = new.currency and period < new.period and outcome = 'imported'
    order by period desc limit 1;
    if found and prev.closing_minor <> new.opening_minor then
      raise exception 'the % statement for % opens at % but % closed at %', new.currency,
        new.period, new.opening_minor, prev.period, prev.closing_minor;
    end if;
  end if;
  return new;
end $$;
create trigger bank_statement_guard before insert on bank_statement
  for each row execute function bank_statement_guard();

create function bank_append_only() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception '% is append only', tg_table_name; end $$;
create trigger bank_statement_no_change before update or delete on bank_statement
  for each row execute function bank_append_only();
create trigger bank_statement_no_truncate before truncate on bank_statement
  execute function bank_append_only();

-- Runs as the owner, like the other audit triggers. No file name.
create function bank_statement_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (email_id, statement_id, event_type, details)
  values (new.source_email_id, new.id, 'bank_statement_recorded',
          jsonb_build_object('sha256', new.sha256, 'location', new.location,
                             'size_bytes', new.size_bytes, 'source', new.source,
                             'currency', new.currency, 'period', new.period,
                             'opening_minor', new.opening_minor,
                             'closing_minor', new.closing_minor,
                             'transaction_count', new.transaction_count,
                             'outcome', new.outcome));
  return null;
end $$;
create trigger bank_statement_audit after insert on bank_statement
  for each row execute function bank_statement_audit();

alter table exception_item add constraint exception_item_statement
  foreign key (statement_id) references bank_statement(id);

alter table bank_transaction
  add column statement_id bigint references bank_statement(id),
  add column counterparty text,           -- payer or payee as Wise names it; not in the audit log
  add column payment_reference text,      -- not in the audit log
  add column description text;            -- not in the audit log

-- A bank transaction belongs to a bank transaction case and to an imported statement of the
-- same currency and month.
create or replace function bank_transaction_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare s record;
begin
  if (select kind from invoice_case where id = new.case_id) is distinct from 'bank_transaction' then
    raise exception 'case % is not a bank transaction', new.case_id;
  end if;
  select currency, period, outcome into s from bank_statement where id = new.statement_id;
  if not found or s.outcome <> 'imported' then
    raise exception 'a bank transaction comes from an imported statement';
  end if;
  if s.currency <> new.wise_balance_currency
     or date_trunc('month', new.booked_on)::date <> s.period then
    raise exception 'bank transaction % is not in the currency and month of statement %',
      new.wise_transaction_id, new.statement_id;
  end if;
  return new;
end $$;

create or replace function bank_transaction_audit() returns trigger language plpgsql
security definer set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, statement_id, event_type, details)
  values (new.case_id, new.statement_id, 'bank_transaction_imported',
          jsonb_build_object('wise_transaction_id', new.wise_transaction_id,
                             'wise_balance_currency', new.wise_balance_currency,
                             'amount_minor', new.amount_minor, 'booked_on', new.booked_on));
  return null;
end $$;

create table bank_notice (
  id bigint generated always as identity primary key,
  kind text not null
    check (kind in ('balance_difference','statement_missing','statement_overdue')),
  period date not null check (extract(day from period) = 1),
  currencies char(3)[] not null check (cardinality(currencies) >= 1),
  statement_id bigint references bank_statement(id),
  status text not null default 'pending' check (status in ('pending','sent','not_sent_shadow')),
  created_at timestamptz not null,        -- set by the database
  finished_at timestamptz,                -- set by the database
  check ((kind = 'balance_difference') = (statement_id is not null)),
  check ((status = 'pending') = (finished_at is null))
);
create unique index bank_notice_once_per_statement on bank_notice (statement_id)
  where statement_id is not null;
create unique index bank_notice_once_per_month on bank_notice (kind, period)
  where statement_id is null;

-- A notice starts pending and ends once.
create function bank_notice_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.status <> 'pending' then
      raise exception 'a notice starts as pending';
    end if;
    new.created_at := clock_timestamp();
    new.finished_at := null;
    return new;
  end if;
  if (new.kind, new.period, new.currencies, new.statement_id, new.created_at)
     is distinct from (old.kind, old.period, old.currencies, old.statement_id, old.created_at) then
    raise exception 'notice % cannot change', old.id;
  end if;
  if old.status <> 'pending' then
    raise exception 'notice % is already finished', old.id;
  end if;
  new.finished_at := case when new.status <> 'pending' then clock_timestamp() end;
  return new;
end $$;
create trigger bank_notice_guard before insert or update on bank_notice
  for each row execute function bank_notice_guard();
create trigger bank_notice_no_delete before delete on bank_notice
  for each row execute function bank_append_only();
create trigger bank_notice_no_truncate before truncate on bank_notice
  execute function bank_append_only();

create function bank_notice_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if old.status = 'pending' and new.status <> 'pending' then
    insert into audit_event (statement_id, event_type, details)
    values (new.statement_id,
            case when new.status = 'sent' then 'bank_notice_sent' else 'bank_notice_not_sent' end,
            jsonb_build_object('notice_id', new.id, 'kind', new.kind, 'period', new.period,
                               'currencies', to_jsonb(new.currencies), 'status', new.status));
  end if;
  return null;
end $$;
create trigger bank_notice_audit after update on bank_notice
  for each row execute function bank_notice_audit();

-- Like the email events of 0004: only the database triggers above write these.
create function audit_bank_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1
     and (new.event_type like 'bank\_statement\_%' or new.event_type like 'bank\_notice\_%') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_bank_events before insert on audit_event
  for each row execute function audit_bank_events();

grant select on bank_statement, bank_notice to argus_app;
grant insert (sha256, location, size_bytes, filename, source, source_email_id, currency, period,
              opening_minor, closing_minor, transaction_count, outcome)
  on bank_statement to argus_app;
grant insert (kind, period, currencies, statement_id) on bank_notice to argus_app;
grant update (status) on bank_notice to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from bank_statement) or exists (select 1 from bank_notice) then
    raise exception 'bank statements were recorded; they are kept for ten years (NF-01)';
  end if;
end $$;
drop trigger audit_bank_events on audit_event;
drop function audit_bank_events();
drop table bank_notice;
drop function bank_notice_audit();
drop function bank_notice_guard();

create or replace function bank_transaction_audit() returns trigger language plpgsql
security definer set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, event_type, details)
  values (new.case_id, 'bank_transaction_imported',
          jsonb_build_object('wise_transaction_id', new.wise_transaction_id,
                             'wise_balance_currency', new.wise_balance_currency,
                             'amount_minor', new.amount_minor, 'booked_on', new.booked_on));
  return null;
end $$;
create or replace function bank_transaction_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if (select kind from invoice_case where id = new.case_id) is distinct from 'bank_transaction' then
    raise exception 'case % is not a bank transaction', new.case_id;
  end if;
  return new;
end $$;
alter table bank_transaction
  drop column statement_id, drop column counterparty, drop column payment_reference,
  drop column description;
alter table exception_item drop constraint exception_item_statement;
drop table bank_statement;
drop function bank_statement_audit();
drop function bank_append_only();
drop function bank_statement_guard();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

"""Append only audit log with a hash chain (ADR 0005, design note section 3).

Covers NF-01 and AP-08: every step is logged with its actor and the time from the database
clock, the application role can only add events, and audit_verify() finds any change made
behind the triggers.

Differences from docs/design/invoice-case/schema-sketch.sql:
- Every function pins search_path to pg_catalog, public, pg_temp, and temporary tables are
  taken away from PUBLIC, so the app cannot shadow a table with a temporary one (QA on PR #79).
- An actor name must not contain spaces, so 'user: ' is refused.
- The hash uses the built in sha256() instead of pgcrypto, so no extension is needed on Azure.
- The argus_app role is created only if it does not exist yet, because roles belong to the
  whole server and not to one database.

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""

from alembic import op

from argus.db import run_script

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'argus_app') then
    create role argus_app nologin;
  end if;
end $$;
grant usage on schema public to argus_app;
-- Every function below pins its search_path with pg_temp last, so a temporary table cannot
-- stand in for a real one. Taking away temporary tables is the second line of defence.
do $$ begin
  execute format('revoke temporary on database %I from public', current_database());
end $$;

create type case_status as enum ('received','awaiting_approval','approved','booked',
                                 'in_payment_run','paid','exception','closed');

create table audit_event (
  id bigint generated always as identity primary key,
  chain_seq bigint not null unique,
  hash_version smallint not null default 1,
  occurred_at timestamptz not null,       -- always the database clock
  case_id bigint, email_id bigint, statement_id bigint, exception_id bigint, payment_run_id bigint,
  event_type text not null,
  actor_type text not null check (actor_type in ('system','user')),
  actor text not null,                    -- job name, or Entra object ID of the person
  from_status case_status, to_status case_status,
  details jsonb not null default '{}',
  prev_hash bytea not null,
  hash bytea not null
);
create index audit_event_case on audit_event (case_id, chain_seq);
create index audit_event_email on audit_event (email_id, chain_seq);

-- The actor of every change comes from the transaction:
-- SET LOCAL argus.actor = 'user:<entra id>' or 'system:<job>'.
create function current_actor() returns text[] language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare a text := current_setting('argus.actor', true);
begin
  if a is null or a !~ '^(system|user):\S+$' then raise exception 'argus.actor is not set'; end if;
  return array[split_part(a, ':', 1), substr(a, strpos(a, ':') + 1)];
end $$;

-- One canonical hash input, used when writing and when checking. Every field is named, nulls
-- stay visible, and the time is seconds since 1970 so the session time zone cannot change it.
create function audit_hash(e audit_event) returns bytea language sql immutable
set search_path = pg_catalog, public, pg_temp as $$
  select sha256(e.prev_hash || convert_to(jsonb_build_object(
    'v', e.hash_version, 'seq', e.chain_seq, 'at', extract(epoch from e.occurred_at),
    'case_id', e.case_id, 'email_id', e.email_id, 'statement_id', e.statement_id,
    'exception_id', e.exception_id, 'payment_run_id', e.payment_run_id,
    'event_type', e.event_type, 'actor_type', e.actor_type, 'actor', e.actor,
    'from_status', e.from_status, 'to_status', e.to_status, 'details', e.details)::text, 'UTF8'))
$$;

create function audit_chain() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare last record; a text[] := current_actor();
begin
  -- Events that describe a case or exception change may only come from the database triggers.
  if pg_trigger_depth() = 1 and (new.event_type in ('case_created','status_changed','case_changed',
                                                    'exception_opened','exception_resolved',
                                                    'bank_transaction_imported')
                                 or new.from_status is not null or new.to_status is not null) then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  -- One short lock keeps the chain in order, so it has no forks.
  perform pg_advisory_xact_lock(hashtext('argus_audit_chain'));
  select chain_seq, hash into last from audit_event order by chain_seq desc limit 1;
  new.hash_version := 1;
  new.occurred_at := clock_timestamp();
  new.actor_type := a[1];
  new.actor := a[2];
  new.chain_seq := coalesce(last.chain_seq, 0) + 1;
  new.prev_hash := coalesce(last.hash, '\x00'::bytea);
  new.hash := audit_hash(new);
  return new;
end $$;
create trigger audit_chain before insert on audit_event
  for each row execute function audit_chain();

-- Refused even for roles that have the right, so history only changes with the trigger off,
-- and audit_verify() then shows it.
create function audit_append_only() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'audit_event is append only'; end $$;
create trigger audit_no_change before update or delete on audit_event
  for each row execute function audit_append_only();
create trigger audit_no_truncate before truncate on audit_event
  execute function audit_append_only();

-- Check the whole chain. Returns the first broken link, or nothing.
create function audit_verify() returns table (broken_at bigint) language sql stable
set search_path = pg_catalog, public, pg_temp as $$
  select chain_seq from (
    select e.chain_seq, e.hash, e.prev_hash, audit_hash(e) as calc,
           lag(e.hash) over (order by e.chain_seq) as prev_row_hash
    from audit_event e) c
  where hash <> calc or prev_hash <> coalesce(prev_row_hash, '\x00'::bytea)
  order by chain_seq limit 1
$$;

grant select on audit_event to argus_app;
-- No time, actor, sequence or hash: the database fills those in.
grant insert (case_id, email_id, statement_id, exception_id, payment_run_id, event_type, details)
  on audit_event to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from audit_event) then
    raise exception 'audit_event holds events; the audit log is kept for ten years (NF-01)';
  end if;
end $$;
drop function audit_verify();
drop function audit_hash(audit_event);
drop table audit_event;
drop function audit_chain();
drop function audit_append_only();
drop function current_actor();
drop type case_status;
revoke usage on schema public from argus_app;
do $$ begin
  execute format('grant temporary on database %I to public', current_database());
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

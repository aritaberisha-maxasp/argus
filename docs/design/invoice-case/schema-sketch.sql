-- Sketch of the case model, audit log and exception queue (ADR 0005, design note README.md).
-- The skeleton story turns this into Alembic migrations.
create extension if not exists pgcrypto;

create type case_kind as enum ('incoming_invoice','incoming_credit_note','outgoing_invoice','outgoing_credit_note','bank_transaction');
create type case_status as enum ('received','awaiting_approval','approved','booked','in_payment_run','paid','exception','closed');

-- Every allowed status change, per kind. Anything not listed is refused by the database.
create table case_status_transition (
  kind case_kind not null, from_status case_status not null, to_status case_status not null,
  primary key (kind, from_status, to_status));

insert into case_status_transition
select k::case_kind, f::case_status, t::case_status from (values
  ('incoming_invoice','received','awaiting_approval'),
  ('incoming_invoice','awaiting_approval','approved'),
  ('incoming_invoice','approved','booked'),
  ('incoming_invoice','booked','in_payment_run'),
  ('incoming_invoice','booked','paid'),            -- direct debit, cleared by the bank import (PY-13)
  ('incoming_invoice','in_payment_run','paid'),
  ('incoming_invoice','in_payment_run','booked'),  -- Wise cancelled the unfunded transfer, back to the pool
  ('incoming_invoice','awaiting_approval','closed'), -- rejected (AP-07)
  ('incoming_credit_note','received','awaiting_approval'),
  ('incoming_credit_note','awaiting_approval','approved'),
  ('incoming_credit_note','approved','booked'),
  ('incoming_credit_note','booked','in_payment_run'),
  ('incoming_credit_note','booked','paid'),
  ('incoming_credit_note','in_payment_run','paid'),
  ('incoming_credit_note','in_payment_run','booked'),
  ('incoming_credit_note','awaiting_approval','closed'),
  ('outgoing_invoice','received','booked'),
  ('outgoing_invoice','booked','paid'),
  ('outgoing_credit_note','received','booked'),
  ('outgoing_credit_note','booked','paid'),
  ('bank_transaction','received','booked')
) v(k,f,t);
-- Any open status may go to exception, and an exception goes back to the status it came from
-- (checked against the exception row) or is closed.
insert into case_status_transition
select k, s, 'exception' from unnest(enum_range(null::case_kind)) k,
  unnest(array['received','awaiting_approval','approved','booked','in_payment_run','paid']::case_status[]) s
on conflict do nothing;
insert into case_status_transition
select k, 'exception', s from unnest(enum_range(null::case_kind)) k,
  unnest(array['received','awaiting_approval','approved','booked','in_payment_run','paid','closed']::case_status[]) s
on conflict do nothing;

create table invoice_case (
  id bigint generated always as identity primary key,
  kind case_kind not null,
  status case_status not null default 'received',
  source_email_id bigint,                 -- email record from ADR 0003
  partner_account text,                   -- DATEV Kreditor or Debitor number once matched
  document_number text,                   -- invoice or credit note number as printed
  gross_minor bigint,                     -- amounts in cents, never floats
  currency char(3),
  duplicate_key text,                     -- set once vendor, number and amount are known (IN-10)
  duplicate_of bigint references invoice_case(id),
  payment_hold boolean not null default false,  -- PY-10
  direct_debit boolean not null default false,  -- PY-13, never in a payment run
  reminder_received_at timestamptz,       -- PY-06, always in the next run
  datev_booking_ref text,                 -- BK-07
  closed_reason text check (closed_reason in ('rejected','duplicate','not_an_invoice','withdrawn')),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check ((status = 'closed') = (closed_reason is not null))
);
create unique index invoice_case_duplicate_key on invoice_case (kind, duplicate_key) where duplicate_key is not null;

create table bank_transaction (
  case_id bigint primary key references invoice_case(id),
  wise_balance_currency char(3) not null,
  wise_transaction_id text not null,
  booked_on date not null,
  amount_minor bigint not null,
  unique (wise_balance_currency, wise_transaction_id)   -- BA-07
);

create type exception_reason as enum (
  'uncertain_classification','unreadable_document','unknown_vendor','unknown_customer',
  'vendor_without_approver','duplicate','iban_mismatch','invoice_check_failed','booking_error',
  'unmatched_bank_transaction','bank_balance_difference',
  'number_sequence_gap','payment_failed','customer_query');

create table exception_item (
  id bigint generated always as identity primary key,
  case_id bigint references invoice_case(id),
  email_id bigint,
  statement_id bigint,
  reason exception_reason not null,
  detail text not null,
  resume_status case_status,              -- where the case goes back to once resolved
  opened_at timestamptz not null default clock_timestamp(),
  resolved_at timestamptz,
  resolved_by text,
  resolution text,
  check (num_nonnulls(case_id, email_id, statement_id) = 1),
  check ((resolved_at is null) = (resolved_by is null))
);
create unique index exception_open_once on exception_item (coalesce(case_id,0), coalesce(email_id,0), coalesce(statement_id,0), reason) where resolved_at is null;

-- Append only audit log with a hash chain.
create table audit_event (
  id bigint generated always as identity primary key,
  chain_seq bigint not null unique,
  occurred_at timestamptz not null default clock_timestamp(),
  case_id bigint, email_id bigint, statement_id bigint, exception_id bigint, payment_run_id bigint,
  event_type text not null,
  actor_type text not null check (actor_type in ('system','user')),
  actor text not null,                    -- job name, or Entra object ID of the person
  from_status case_status, to_status case_status,
  details jsonb not null default '{}',
  prev_hash bytea not null,
  hash bytea not null
);
create index on audit_event (case_id, chain_seq);
create index on audit_event (email_id, chain_seq);

create function audit_chain() returns trigger language plpgsql as $$
declare last record;
begin
  perform pg_advisory_xact_lock(hashtext('argus_audit_chain'));
  select chain_seq, hash into last from audit_event order by chain_seq desc limit 1;
  new.chain_seq := coalesce(last.chain_seq, 0) + 1;
  new.prev_hash := coalesce(last.hash, '\x00'::bytea);
  new.hash := digest(new.prev_hash || convert_to(concat_ws('|', new.chain_seq, new.occurred_at, new.case_id, new.email_id,
      new.statement_id, new.exception_id, new.payment_run_id, new.event_type, new.actor_type, new.actor,
      new.from_status, new.to_status, new.details::text), 'UTF8'), 'sha256');
  return new;
end $$;
create trigger audit_chain before insert on audit_event for each row execute function audit_chain();

create function audit_append_only() returns trigger language plpgsql as $$
begin raise exception 'audit_event is append only'; end $$;
create trigger audit_no_change before update or delete on audit_event for each row execute function audit_append_only();
create trigger audit_no_truncate before truncate on audit_event execute function audit_append_only();

-- The actor of every change comes from the transaction: SET LOCAL argus.actor = 'user:<entra id>' or 'system:<job>'.
create function current_actor() returns text[] language plpgsql as $$
declare a text := current_setting('argus.actor', true);
begin
  if a is null or a = '' or a not like '%:%' then raise exception 'argus.actor is not set'; end if;
  return array[split_part(a, ':', 1), substr(a, strpos(a, ':') + 1)];
end $$;

create function case_status_guard() returns trigger language plpgsql as $$
declare ex exception_item;
begin
  if new.status is distinct from old.status then
    if not exists (select 1 from case_status_transition where kind = new.kind and from_status = old.status and to_status = new.status) then
      raise exception 'status change % -> % not allowed for %', old.status, new.status, new.kind;
    end if;
    if new.status = 'exception' and not exists (select 1 from exception_item where case_id = new.id and resolved_at is null) then
      raise exception 'case % needs an open exception before it can go to exception', new.id;
    end if;
    if old.status = 'exception' then
      if exists (select 1 from exception_item where case_id = new.id and resolved_at is null) then
        raise exception 'case % still has an open exception', new.id;
      end if;
      select * into ex from exception_item where case_id = new.id order by resolved_at desc limit 1;
      if new.status <> 'closed' and new.status is distinct from ex.resume_status then
        raise exception 'case % must go back to %', new.id, ex.resume_status;
      end if;
    end if;
    new.updated_at := clock_timestamp();
  end if;
  return new;
end $$;
create trigger case_status_guard before update on invoice_case for each row execute function case_status_guard();

create function case_audit() returns trigger language plpgsql as $$
declare a text[] := current_actor();
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, event_type, actor_type, actor, to_status, details)
    values (new.id, new.source_email_id, 'case_created', a[1], a[2], new.status, jsonb_build_object('kind', new.kind));
  elsif new.status is distinct from old.status then
    insert into audit_event (case_id, event_type, actor_type, actor, from_status, to_status, details)
    values (new.id, 'status_changed', a[1], a[2], old.status, new.status,
            jsonb_strip_nulls(jsonb_build_object('closed_reason', new.closed_reason,
              'datev_booking_ref', case when new.datev_booking_ref is distinct from old.datev_booking_ref then new.datev_booking_ref end)));
  end if;
  return null;
end $$;
create trigger case_audit after insert or update on invoice_case for each row execute function case_audit();

create function exception_audit() returns trigger language plpgsql as $$
declare a text[] := current_actor();
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, actor_type, actor, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_opened', a[1], a[2],
            jsonb_build_object('reason', new.reason, 'detail', new.detail));
  elsif new.resolved_at is not null and old.resolved_at is null then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, actor_type, actor, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_resolved', a[1], a[2],
            jsonb_build_object('reason', new.reason, 'resolution', new.resolution));
  end if;
  return null;
end $$;
create trigger exception_audit after insert or update on exception_item for each row execute function exception_audit();

-- The app connects as argus_app: it can add audit events and read them, nothing else.
create role argus_app;
grant select, insert, update on invoice_case, bank_transaction, exception_item to argus_app;
grant select, insert on audit_event to argus_app;
grant select on case_status_transition to argus_app;

-- Everything that happened to one case, including the email it came from (AC-8).
-- The payment run and bank import stories add their events through payment_run_id and the matched transaction.
create view case_trace as
select c.id as trace_case_id, e.*
from invoice_case c
join audit_event e on e.case_id = c.id
   or (e.email_id = c.source_email_id and e.case_id is null);
grant select on case_trace to argus_app;

-- Check the whole chain. Returns the first broken link, or nothing.
create function audit_verify() returns table (broken_at bigint) language sql as $$
  with c as (
    select chain_seq, prev_hash, hash, digest(prev_hash || convert_to(concat_ws('|', chain_seq, occurred_at, case_id, email_id,
      statement_id, exception_id, payment_run_id, event_type, actor_type, actor, from_status, to_status, details::text), 'UTF8'), 'sha256') as calc,
      lag(hash) over (order by chain_seq) as prev_row_hash
    from audit_event)
  select chain_seq from c where hash <> calc or prev_hash <> coalesce(prev_row_hash, '\x00'::bytea) order by chain_seq limit 1
$$;

-- Sketch of the case model, audit log and exception queue (ADR 0005, design note README.md).
-- The skeleton story turns this into Alembic migrations.
-- Rule of the sketch: the application role argus_app cannot get round any rule below.
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
-- Any open status may go to exception. Leaving exception is listed generally here, but the guard
-- below only lets a case go back to the status it left, to closed, or to an exit in exception_exit.
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
  kind case_kind not null,                -- never changes
  status case_status not null default 'received',
  resume_status case_status,              -- set by the database when the case enters exception
  exception_since timestamptz,            -- set by the database when the case enters exception
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
  datev_booking_ref text,                 -- BK-07, set once
  closed_reason text check (closed_reason in ('rejected','duplicate','not_an_invoice','withdrawn')),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check ((status = 'closed') = (closed_reason is not null)),
  check ((status = 'exception') = (resume_status is not null))
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
  detail text not null,                   -- for the person who acts on it; never copied into the audit log
  facts jsonb not null default '{}',      -- structured, no personal data (for example iban_last4); goes into the audit log
  opened_at timestamptz not null default clock_timestamp(),
  outcome text check (outcome in ('fixed_in_datev','data_corrected','retried','confirmed_duplicate','not_a_duplicate',
                                  'rejected','accepted_with_comment','answered','rule_saved','category_chosen','put_back_in_pool')),
  resolution text,                        -- free text comment, stays here, not in the audit log
  resolved_at timestamptz,                -- set by the database
  resolved_by text,                       -- set by the database from argus.actor
  check (num_nonnulls(case_id, email_id, statement_id) = 1),
  check ((outcome is null) = (resolved_at is null) and (resolved_at is null) = (resolved_by is null))
);
create unique index exception_open_once on exception_item (coalesce(case_id,0), coalesce(email_id,0), coalesce(statement_id,0), reason) where resolved_at is null;

-- Exits from exception other than "back to where it was". Only these reasons get them.
create table exception_exit (
  reason exception_reason not null, resume_status case_status not null, to_status case_status not null,
  primary key (reason, resume_status, to_status));
insert into exception_exit values
  ('payment_failed', 'paid', 'booked'),             -- the transfer bounced, back to the pool
  ('payment_failed', 'in_payment_run', 'booked');

-- Append only audit log with a hash chain.
create table audit_event (
  id bigint generated always as identity primary key,
  chain_seq bigint not null unique,
  hash_version smallint not null default 1,
  occurred_at timestamptz not null,       -- always the database clock
  case_id bigint, email_id bigint, statement_id bigint, exception_id bigint, payment_run_id bigint,
  event_type text not null,
  actor_type text not null check (actor_type in ('system','user')),
  actor text not null,                    -- job name, or Entra object ID of the person; always from argus.actor
  from_status case_status, to_status case_status,
  details jsonb not null default '{}',
  prev_hash bytea not null,
  hash bytea not null
);
create index on audit_event (case_id, chain_seq);
create index on audit_event (email_id, chain_seq);

-- The actor of every change comes from the transaction: SET LOCAL argus.actor = 'user:<entra id>' or 'system:<job>'.
create function current_actor() returns text[] language plpgsql as $$
declare a text := current_setting('argus.actor', true);
begin
  if a is null or a !~ '^(system|user):.+$' then raise exception 'argus.actor is not set'; end if;
  return array[split_part(a, ':', 1), substr(a, strpos(a, ':') + 1)];
end $$;

-- One canonical hash input, used when writing and when checking. Every field is named, nulls stay
-- visible, and the time is seconds since 1970 so the session time zone cannot change it.
create function audit_hash(e audit_event) returns bytea language sql immutable as $$
  select digest(e.prev_hash || convert_to(jsonb_build_object(
    'v', e.hash_version, 'seq', e.chain_seq, 'at', extract(epoch from e.occurred_at),
    'case_id', e.case_id, 'email_id', e.email_id, 'statement_id', e.statement_id,
    'exception_id', e.exception_id, 'payment_run_id', e.payment_run_id,
    'event_type', e.event_type, 'actor_type', e.actor_type, 'actor', e.actor,
    'from_status', e.from_status, 'to_status', e.to_status, 'details', e.details)::text, 'UTF8'), 'sha256')
$$;

create function audit_chain() returns trigger language plpgsql as $$
declare last record; a text[] := current_actor();
begin
  -- Events that describe a case or exception change may only come from the triggers below.
  if pg_trigger_depth() = 1 and (new.event_type in ('case_created','status_changed','case_changed','exception_opened','exception_resolved')
                                 or new.from_status is not null or new.to_status is not null) then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
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
create trigger audit_chain before insert on audit_event for each row execute function audit_chain();

create function audit_append_only() returns trigger language plpgsql as $$
begin raise exception 'audit_event is append only'; end $$;
create trigger audit_no_change before update or delete on audit_event for each row execute function audit_append_only();
create trigger audit_no_truncate before truncate on audit_event execute function audit_append_only();

create function case_guard() returns trigger language plpgsql as $$
declare
  effective case_status := case when old.status = 'exception' then old.resume_status else old.status end;
begin
  -- Fields only the database writes.
  new.resume_status := old.resume_status;
  new.exception_since := old.exception_since;

  if new.kind is distinct from old.kind then
    raise exception 'the kind of case % cannot change', old.id;
  end if;
  if old.status = 'closed' then
    raise exception 'case % is closed and cannot change', old.id;
  end if;
  -- After approval (or booking, for kinds without approval) the booking data is frozen.
  if effective in ('approved','booked','in_payment_run','paid')
     and (new.partner_account, new.document_number, new.gross_minor, new.currency)
         is distinct from (old.partner_account, old.document_number, old.gross_minor, old.currency) then
    raise exception 'case % is past approval: account, number, amount and currency are frozen', old.id;
  end if;
  if old.datev_booking_ref is not null and new.datev_booking_ref is distinct from old.datev_booking_ref then
    raise exception 'the DATEV booking reference of case % is already set', old.id;
  end if;

  if new.status is distinct from old.status then
    if not exists (select 1 from case_status_transition where kind = new.kind and from_status = old.status and to_status = new.status) then
      raise exception 'status change % -> % not allowed for %', old.status, new.status, new.kind;
    end if;
    -- An open exception stops every move except into exception.
    if new.status <> 'exception' and exists (select 1 from exception_item where case_id = old.id and resolved_at is null) then
      raise exception 'case % has an open exception', old.id;
    end if;
    if new.status = 'exception' then
      if not exists (select 1 from exception_item where case_id = old.id and resolved_at is null) then
        raise exception 'case % needs an open exception before it can go to exception', old.id;
      end if;
      new.resume_status := old.status;
      new.exception_since := clock_timestamp();
    elsif old.status = 'exception' then
      if new.status <> old.resume_status and new.status <> 'closed' and not exists (
           select 1 from exception_item ei join exception_exit x on x.reason = ei.reason
           where ei.case_id = old.id and ei.resolved_at >= old.exception_since
             and x.resume_status = old.resume_status and x.to_status = new.status) then
        raise exception 'case % must go back to %', old.id, old.resume_status;
      end if;
      new.resume_status := null;
      new.exception_since := null;
    end if;
  end if;
  new.updated_at := clock_timestamp();
  return new;
end $$;
create trigger case_guard before update on invoice_case for each row execute function case_guard();

-- Writes the audit event for every new case and every change. Runs as the owner, because the
-- application itself may not write these event types.
create function case_audit() returns trigger language plpgsql security definer set search_path = public as $$
declare
  changes jsonb := '{}';
  f text;
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, event_type, to_status, details)
    values (new.id, new.source_email_id, 'case_created', new.status,
            jsonb_strip_nulls(jsonb_build_object('kind', new.kind, 'partner_account', new.partner_account,
              'document_number', new.document_number, 'gross_minor', new.gross_minor, 'currency', new.currency)));
    return null;
  end if;
  foreach f in array array['partner_account','document_number','gross_minor','currency','duplicate_key','duplicate_of',
                           'payment_hold','direct_debit','reminder_received_at','datev_booking_ref','closed_reason'] loop
    if to_jsonb(new) -> f is distinct from to_jsonb(old) -> f then
      changes := changes || jsonb_build_object(f, jsonb_build_object('old', to_jsonb(old) -> f, 'new', to_jsonb(new) -> f));
    end if;
  end loop;
  if new.status is distinct from old.status then
    insert into audit_event (case_id, event_type, from_status, to_status, details)
    values (new.id, 'status_changed', old.status, new.status, changes);
  elsif changes <> '{}' then
    insert into audit_event (case_id, event_type, details) values (new.id, 'case_changed', changes);
  end if;
  return null;
end $$;
create trigger case_audit after insert or update on invoice_case for each row execute function case_audit();

create function exception_guard() returns trigger language plpgsql as $$
declare a text[] := current_actor();
begin
  if tg_op = 'INSERT' then
    new.opened_at := clock_timestamp();
    if new.outcome is not null or new.resolved_at is not null then
      raise exception 'a new exception cannot be resolved already';
    end if;
    return new;
  end if;
  if old.resolved_at is not null then
    raise exception 'exception % is resolved and cannot change', old.id;
  end if;
  if new.outcome is not null then
    -- PR-05, VM-04: an IBAN mismatch ends only by fixing the IBAN in DATEV or rejecting the invoice.
    if old.reason = 'iban_mismatch' and new.outcome not in ('fixed_in_datev','rejected') then
      raise exception 'an IBAN mismatch can only end as fixed_in_datev or rejected';
    end if;
    new.resolved_at := clock_timestamp();
    new.resolved_by := a[1] || ':' || a[2];
  end if;
  return new;
end $$;
create trigger exception_guard before insert or update on exception_item for each row execute function exception_guard();

create function exception_audit() returns trigger language plpgsql security definer set search_path = public as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_opened',
            jsonb_build_object('reason', new.reason, 'facts', new.facts));
  elsif new.resolved_at is not null and old.resolved_at is null then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_resolved',
            jsonb_build_object('reason', new.reason, 'outcome', new.outcome));
  end if;
  return null;
end $$;
create trigger exception_audit after insert or update on exception_item for each row execute function exception_audit();

-- Checked at commit: a case with an open exception is in exception, and a case in exception has one.
-- So opening an exception without stopping the case cannot be committed.
create function case_exception_consistent() returns trigger language plpgsql as $$
declare cid bigint; st case_status; open_count int;
begin
  if tg_table_name = 'invoice_case' then cid := new.id; else cid := new.case_id; end if;
  if cid is null then return null; end if;
  select status into st from invoice_case where id = cid;
  select count(*) into open_count from exception_item where case_id = cid and resolved_at is null;
  if open_count > 0 and st <> 'exception' then
    raise exception 'case % has an open exception but is in %', cid, st;
  end if;
  if open_count = 0 and st = 'exception' then
    raise exception 'case % is in exception without an open exception', cid;
  end if;
  return null;
end $$;
create constraint trigger exception_consistent_case after update on invoice_case
  deferrable initially deferred for each row execute function case_exception_consistent();
create constraint trigger exception_consistent_item after insert or update on exception_item
  deferrable initially deferred for each row execute function case_exception_consistent();

-- The app connects as argus_app. It may only write the columns listed here.
create role argus_app;
grant select on invoice_case, bank_transaction, exception_item, audit_event, case_status_transition, exception_exit to argus_app;
grant insert (kind, source_email_id, partner_account, document_number, gross_minor, currency,
              duplicate_key, duplicate_of, payment_hold, direct_debit) on invoice_case to argus_app;
grant update (status, partner_account, document_number, gross_minor, currency, duplicate_key, duplicate_of,
              payment_hold, direct_debit, reminder_received_at, datev_booking_ref, closed_reason) on invoice_case to argus_app;
grant insert on bank_transaction to argus_app;
grant insert (case_id, email_id, statement_id, reason, detail, facts) on exception_item to argus_app;
grant update (outcome, resolution) on exception_item to argus_app;
grant insert (case_id, email_id, statement_id, exception_id, payment_run_id, event_type, details) on audit_event to argus_app;

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
  select chain_seq from (
    select e.chain_seq, e.hash, e.prev_hash, audit_hash(e) as calc,
           lag(e.hash) over (order by e.chain_seq) as prev_row_hash
    from audit_event e) c
  where hash <> calc or prev_hash <> coalesce(prev_row_hash, '\x00'::bytea)
  order by chain_seq limit 1
$$;

"""The approver sends an invoice back with a question, and Benjamin answers it (issue #192).

Covers AP-03, AP-05, AP-08, EX-01, EX-02, NF-01, NF-03 and NF-04. Next to approve and reject,
the approval page (#38) lets the person asked send the invoice back with a question. The case
goes to the exception queue, where Benjamin (finance) or Arita (admin) answers it, or rejects
the invoice. An answer sends the case back to Awaiting approval with a new approval request to
the person who asked, so the 3 and 5 working day clock of #40 starts again from that request.

This migration adds:

- A new exception reason ``approver_query``. Only the person the newest approval request went
  to opens it, only while the case waits for approval, and only together with their question.
  It ends only as ``answered`` or ``rejected``, by a person, with an answer or reason of at
  least three characters other than spaces (``correction_trim``). A job can do neither.
  It is answered only when it is the case's only open exception, so the case goes straight
  back to the approver.
- ``approval_query``: one row per question, never changed or removed. ARGUS names the case, the
  request the person saw, the exception and the question. The database fills in the rest: who
  asked (object ID, name and sign in address as they were then, ADR 0008), as approver or
  deputy and for whom, the time and the transaction. It refuses a question from anyone but the
  person the newest request went to, on a case that does not wait, on a request that is not
  the newest or that went to another vendor account than the case has now, without a name, or
  shorter than three characters. One question per request: after the answer a new request goes
  out, and the approver may ask again on that one. A question only commits together with the
  move of the case to Exception.
- The answer is the exception's resolution. The database already keeps who answered, with name
  and sign in address, and when (migration 0016).
- The approval request rule of 0020 is widened on purpose: besides ARGUS in Received, the person
  who answered the question may write the new request, in the same transaction as the answer,
  for a case that waited for approval. The database still picks who is asked: the person who
  asked, as long as they are still the vendor's approver or deputy and are not marked away;
  otherwise the vendor's approver, or the deputy while the approver is away. If the vendor
  account changed while the question was open, the new vendor's approver is asked. A case
  whose question was answered goes back to Awaiting approval only with that new request.
- When the person who asked answers their own question (an approver who also has the finance
  or admin role), the answer is allowed, as Arita decided for #39 on 23 Nov, but marked: the
  audit event says ``same_person``, and the new request is marked ``answered_by_asker`` and
  keeps the reminder clock of the request the question was asked on (``clock_from``), so it
  never restarts the 3 and 5 working day clock (compliance review of PR 209). #40 counts from
  ``coalesce(clock_from, sent_at)``.
- The audit events ``approval_queried`` and ``approval_query_answered``, written by the
  database only. They hold who and when, the request and exception, and a SHA 256 of the
  question or answer. The texts stay in ``approval_query`` and on the exception, out of the
  audit log and the app log (NF-03).

The new reason is compared as text, because a value added to an enum cannot be used in the
same transaction. A downgrade never loses a question: with questions in the database it keeps
the table and all its rules, as 0020 and 0021 do.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-26
"""

from alembic import op

from argus.db import run_script

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter type exception_reason add value if not exists 'approver_query';

create table if not exists approval_query (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  request_id bigint not null references approval_request(id),
  exception_id bigint not null references exception_item(id),
  question text not null check (length(correction_trim(question)) between 3 and 500),
  asked_by text not null,                 -- user:<Entra object ID>, set by the database
  asked_by_name text not null check (length(correction_trim(asked_by_name)) between 1 and 200),
  asked_by_address text check (length(correction_trim(asked_by_address)) between 1 and 320),
  asked_as text not null check (asked_as in ('approver', 'deputy')),
  on_behalf_of text,                      -- the approver who was away, when a deputy asked
  on_behalf_of_name text,
  asked_at timestamptz not null,          -- set by the database
  txid bigint not null,                   -- the transaction, set by the database
  check ((asked_as = 'deputy') = (on_behalf_of is not null))
);
-- One question per request, and one question per exception.
create unique index if not exists approval_query_once on approval_query (request_id);
create unique index if not exists approval_query_exception on approval_query (exception_id);
create index if not exists approval_query_case on approval_query (case_id, id);

-- A request written after an answer names the question it answers. When the person who asked
-- answered it themselves (an approver who also has the finance or admin role), it is marked
-- answered_by_asker and keeps the reminder clock of the request the question was asked on, so
-- asking and answering oneself never restarts the 3 and 5 working day clock of #40. #40 counts
-- from coalesce(clock_from, sent_at). Set by the database only.
alter table approval_request
  add column if not exists query_id bigint references approval_query(id),
  add column if not exists answered_by_asker boolean not null default false,
  add column if not exists clock_from timestamptz;

create or replace function approval_query_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  r record;
  x record;
begin
  if a[1] <> 'user' then
    raise exception 'only a person can send case % back with a question', new.case_id;
  end if;
  select kind, status, partner_account into c from invoice_case where id = new.case_id for update;
  if not found then
    raise exception 'case % does not exist', new.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', new.case_id;
  end if;
  if c.status <> 'awaiting_approval' then
    raise exception 'case % is in % and does not wait for a decision', new.case_id, c.status;
  end if;
  select id, recipient_id, recipient_role, on_behalf_of, on_behalf_of_name, vendor_account
    into r
  from approval_request where case_id = new.case_id order by id desc limit 1;
  if not found or r.id is distinct from new.request_id then
    raise exception 'approval request % is not the newest one of case %',
      new.request_id, new.case_id;
  end if;
  if lower(a[2]) is distinct from lower(r.recipient_id) then
    raise exception 'only the person approval request % went to can ask about case %',
      r.id, new.case_id;
  end if;
  if c.partner_account is distinct from r.vendor_account then
    raise exception 'case % moved to another vendor account since approval request %; '
      'it needs a new request', new.case_id, r.id;
  end if;
  select case_id, reason::text as reason, resolved_at into x
  from exception_item where id = new.exception_id;
  if not found or x.case_id is distinct from new.case_id or x.resolved_at is not null
     or x.reason <> 'approver_query' then
    raise exception 'exception % is not an open approver question of case %',
      new.exception_id, new.case_id;
  end if;
  if coalesce(length(correction_trim(new.question)), 0) < 3 then
    raise exception 'a question about case % needs at least 3 characters', new.case_id;
  end if;
  new.question := correction_trim(new.question);
  new.asked_by := a[1] || ':' || a[2];
  new.asked_by_name :=
    left(nullif(correction_trim(current_setting('argus.actor_name', true)), ''), 200);
  new.asked_by_address :=
    left(nullif(correction_trim(current_setting('argus.actor_address', true)), ''), 320);
  if new.asked_by_name is null then
    raise exception 'a question about case % needs the name of the person', new.case_id;
  end if;
  new.asked_as := r.recipient_role;
  new.on_behalf_of := r.on_behalf_of;
  new.on_behalf_of_name := r.on_behalf_of_name;
  new.asked_at := clock_timestamp();
  new.txid := txid_current();
  return new;
end $$;
create or replace trigger approval_query_guard before insert on approval_query
  for each row execute function approval_query_guard();

create or replace function approval_query_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'a question is kept as it was asked and cannot change'; end $$;
create or replace trigger approval_query_no_change before update or delete
  on approval_query for each row execute function approval_query_fixed();
create or replace trigger approval_query_no_truncate before truncate on approval_query
  execute function approval_query_fixed();

-- Runs as the owner, like the other audit triggers. The question stays out (NF-03); its hash
-- shows a later change to it.
create or replace function approval_query_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, exception_id, event_type, details)
  values (new.case_id, new.exception_id, 'approval_queried',
          jsonb_strip_nulls(jsonb_build_object(
            'query_id', new.id,
            'request_id', new.request_id,
            'name', new.asked_by_name,
            'sign_in_address', new.asked_by_address,
            'as', new.asked_as,
            'on_behalf_of', new.on_behalf_of,
            'on_behalf_of_name', new.on_behalf_of_name,
            'question_sha256', encode(sha256(convert_to(new.question, 'UTF8')), 'hex'))));
  return null;
end $$;
create or replace trigger approval_query_audit after insert on approval_query
  for each row execute function approval_query_audit();

-- Checked at commit: a question only exists together with the move of its case to Exception.
create or replace function approval_query_with_move() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if not exists (
    select 1 from audit_event m
    where m.case_id = new.case_id and m.event_type = 'status_changed'
      and m.from_status = 'awaiting_approval' and m.to_status = 'exception'
      and m.chain_seq > (select q.chain_seq from audit_event q
                         where q.case_id = new.case_id and q.event_type = 'approval_queried'
                           and (q.details ->> 'query_id')::bigint = new.id)) then
    raise exception 'a question about case % needs the case to go to the exception queue '
      'in the same transaction', new.case_id;
  end if;
  return null;
end $$;
drop trigger if exists approval_query_with_move on approval_query;
create constraint trigger approval_query_with_move after insert on approval_query
  deferrable initially deferred for each row execute function approval_query_with_move();

-- Who opens an approver question and how it ends. It leaves a resolved exception alone, so
-- exception_guard refuses any change to one in its own words. It runs before
-- zz_exception_ending_guard (triggers of one event run in name order), which then names the
-- person and checks the general rules of 0016.
create or replace function exception_approver_query_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[];
  c record;
  asked text;
begin
  if tg_op = 'INSERT' then
    if new.reason::text <> 'approver_query' then
      return new;
    end if;
    a := current_actor();
    if new.case_id is null then
      raise exception 'an approver question always belongs to a case';
    end if;
    if a[1] <> 'user' then
      raise exception 'only the person asked to approve case % can send it back with a question',
        new.case_id;
    end if;
    select status into c from invoice_case where id = new.case_id for update;
    if not found or c.status <> 'awaiting_approval' then
      raise exception 'case % does not wait for approval', new.case_id;
    end if;
    select recipient_id into asked
    from approval_request where case_id = new.case_id order by id desc limit 1;
    if lower(a[2]) is distinct from lower(asked) then
      raise exception 'only the person asked to approve case % can send it back with a question',
        new.case_id;
    end if;
    return new;
  end if;
  if old.reason::text <> 'approver_query' or new.outcome is null or old.outcome is not null then
    return new;
  end if;
  a := current_actor();
  if new.outcome not in ('answered', 'rejected') then
    raise exception 'an approver question % can only end as answered or rejected', old.id;
  end if;
  if a[1] <> 'user' then
    raise exception 'only a person can answer or reject approver question %', old.id;
  end if;
  -- An answer, and the reason for rejecting instead, both need 3 real characters.
  if coalesce(length(correction_trim(new.resolution)), 0) < 3 then
    raise exception '% approver question % needs at least 3 characters',
      case new.outcome when 'answered' then 'answering' else 'rejecting' end, old.id;
  end if;
  if new.outcome = 'answered' then
    if exists (select 1 from exception_item
               where case_id = old.case_id and resolved_at is null and id <> old.id) then
      raise exception 'case % has another open exception; resolve it before answering '
        'approver question %', old.case_id, old.id;
    end if;
  end if;
  return new;
end $$;
create or replace trigger exception_approver_query_guard before insert or update
  on exception_item for each row execute function exception_approver_query_guard();

-- Checked at commit: an approver question only exists with the question that opened it.
create or replace function exception_approver_query_asked() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.reason::text = 'approver_query'
     and not exists (select 1 from approval_query where exception_id = new.id) then
    raise exception 'approver question % needs the question in the same transaction', new.id;
  end if;
  return null;
end $$;
drop trigger if exists exception_approver_query_asked on exception_item;
create constraint trigger exception_approver_query_asked after insert on exception_item
  deferrable initially deferred for each row execute function exception_approver_query_asked();

-- Runs as the owner, like the other audit triggers. The answer stays on the exception (NF-03).
create or replace function exception_approver_query_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare q record;
begin
  if old.reason::text <> 'approver_query' or new.outcome is distinct from 'answered'
     or old.outcome is not null then
    return null;
  end if;
  select id, request_id, asked_by into q from approval_query where exception_id = new.id;
  insert into audit_event (case_id, exception_id, event_type, details)
  values (new.case_id, new.id, 'approval_query_answered',
          jsonb_strip_nulls(jsonb_build_object(
            'query_id', q.id,
            'request_id', q.request_id,
            'name', new.resolved_by_name,
            'sign_in_address', new.resolved_by_address,
            'answer_sha256', encode(sha256(convert_to(new.resolution, 'UTF8')), 'hex'),
            -- The person who asked answered it themselves (Arita on #39, 23 Nov: allowed,
            -- but marked).
            'same_person', case when lower(q.asked_by) = lower(new.resolved_by) then true end)));
  return null;
end $$;
create or replace trigger exception_approver_query_audit after update on exception_item
  for each row execute function exception_approver_query_audit();

-- 0020's guard, widened on purpose: the person who answered the approver's question writes the
-- new request, in the same transaction. The database picks who is asked, as before.
create or replace function approval_request_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  v record;
  x record;
  qq record;
  prefer text;
begin
  if tg_op = 'UPDATE' then
    if a[1] <> 'system' then
      raise exception 'only ARGUS marks approval request % as sent', old.id;
    end if;
    if old.sent_at is not null then
      raise exception 'approval request % was sent already and is never sent again', old.id;
    end if;
    if (to_jsonb(new) - 'sent_at') is distinct from (to_jsonb(old) - 'sent_at')
       or new.sent_at is null then
      raise exception 'only the sent time of approval request % can be set', old.id;
    end if;
    new.sent_at := clock_timestamp();
    return new;
  end if;
  if a[1] not in ('system', 'user') then
    raise exception 'only ARGUS sends case % for approval', new.case_id;
  end if;
  select kind, status, resume_status, exception_since, partner_account into c
  from invoice_case where id = new.case_id for update;
  if not found then
    raise exception 'case % does not exist', new.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', new.case_id;
  end if;
  if a[1] = 'system' then
    if c.status <> 'received' then
      raise exception 'case % is in % and cannot be sent for approval', new.case_id, c.status;
    end if;
  else
    -- A person, after answering the approver's question (#192).
    if c.status <> 'exception' or c.resume_status is distinct from 'awaiting_approval' then
      raise exception 'only ARGUS sends case % for approval', new.case_id;
    end if;
    select e.id, e.resolved_by, e.resolved_at into x
    from exception_item e
    where e.case_id = new.case_id and e.reason::text = 'approver_query'
      and e.outcome = 'answered' and e.resolved_at >= c.exception_since
    order by e.id desc limit 1;
    if not found or x.resolved_by is distinct from a[1] || ':' || a[2] then
      raise exception 'only ARGUS, or the person who answered the approver''s question, '
        'sends case % for approval', new.case_id;
    end if;
    if exists (select 1 from approval_request
               where case_id = new.case_id and requested_at >= x.resolved_at) then
      raise exception 'case % went back to its approver already', new.case_id;
    end if;
    select q.id, q.asked_by, r.sent_at, r.requested_at, r.clock_from into qq
    from approval_query q join approval_request r on r.id = q.request_id
    where q.exception_id = x.id;
    prefer := split_part(qq.asked_by, ':', 2);
  end if;
  if c.partner_account is null then
    raise exception 'case % has no vendor account yet', new.case_id;
  end if;
  select ap.object_id, ap.name, ap.email, ap.absent,
         dp.object_id as deputy_id, dp.name as deputy_name, dp.email as deputy_email,
         dp.absent as deputy_absent
    into v
  from vendor_approver va
  join approver ap on ap.object_id = va.approver_id
  left join approver dp on dp.object_id = va.deputy_id
  where va.vendor_account = c.partner_account;
  if not found then
    raise exception 'vendor account % has no approver', c.partner_account;
  end if;
  new.vendor_account := c.partner_account;
  -- The person who asked, while still the vendor's approver or deputy and not away (#192).
  if prefer is not null and lower(prefer) = lower(v.object_id) and not v.absent then
    v.deputy_id := null;
  elsif prefer is not null and lower(prefer) = lower(v.deputy_id) and not v.deputy_absent then
    v.absent := true;
  end if;
  if not v.absent then
    new.recipient_id := v.object_id;
    new.recipient_name := v.name;
    new.recipient_address := v.email;
    new.recipient_role := 'approver';
    new.on_behalf_of := null;
    new.on_behalf_of_name := null;
  elsif v.deputy_id is not null and not v.deputy_absent then
    new.recipient_id := v.deputy_id;
    new.recipient_name := v.deputy_name;
    new.recipient_address := v.deputy_email;
    new.recipient_role := 'deputy';
    new.on_behalf_of := v.object_id;
    new.on_behalf_of_name := v.name;
  else
    raise exception 'the approver of vendor account % is away and it has no deputy who is in',
      c.partner_account;
  end if;
  new.requested_by := a[1] || ':' || a[2];
  new.requested_at := clock_timestamp();
  new.txid := txid_current();
  new.sent_at := null;
  if a[1] = 'user' then
    new.query_id := qq.id;
    new.answered_by_asker := lower(qq.asked_by) = lower(a[1] || ':' || a[2]);
    new.clock_from := case when new.answered_by_asker
                           then coalesce(qq.clock_from, qq.sent_at, qq.requested_at) end;
  else
    new.query_id := null;
    new.answered_by_asker := false;
    new.clock_from := null;
  end if;
  return new;
end $$;

-- As in 0020, and also after an answer, when the case comes back from Exception.
create or replace function approval_request_with_move() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if not exists (
    select 1 from audit_event m
    where m.case_id = new.case_id and m.event_type = 'status_changed'
      and m.from_status in ('received', 'exception') and m.to_status = 'awaiting_approval'
      and m.chain_seq > (select r.chain_seq from audit_event r
                         where r.case_id = new.case_id and r.event_type = 'approval_requested'
                           and (r.details ->> 'request_id')::bigint = new.id)) then
    raise exception
      'approval request for case % needs the case to go for approval in the same transaction',
      new.case_id;
  end if;
  return null;
end $$;

-- As in 0020, plus: a case whose approver question was answered goes back to Awaiting approval
-- only with the new request of the same transaction.
create or replace function case_ready_for_approval() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.status is distinct from 'awaiting_approval' or old.status = 'awaiting_approval' then
    return new;
  end if;
  perform current_actor();  -- no move without an actor, said in the usual words first
  if exists (select 1 from exception_item where case_id = old.id and resolved_at is null) then
    raise exception 'case % has an open exception and cannot go for approval', old.id;
  end if;
  if old.status <> 'received' then
    if old.status = 'exception' and exists (
         select 1 from exception_item
         where case_id = old.id and reason::text = 'approver_query' and outcome = 'answered'
           and resolved_at >= old.exception_since)
       and not exists (
         select 1 from approval_request
         where case_id = old.id and vendor_account = new.partner_account
           and txid = txid_current()) then
      raise exception 'case % goes back to its approver only with a new approval request',
        old.id;
    end if;
    return new;  -- back from exception to where it was; case_guard checks that
  end if;
  if new.partner_account is null or not exists (
       select 1 from account_proposal
       where case_id = old.id and vendor_account = new.partner_account) then
    raise exception 'case % cannot go for approval without an account proposal', old.id;
  end if;
  if not exists (
       select 1 from approval_request
       where case_id = old.id and vendor_account = new.partner_account
         and txid = txid_current()) then
    raise exception 'case % cannot go for approval without an approval request', old.id;
  end if;
  return new;
end $$;

-- Like the other database events: only the triggers above write these.
create or replace function audit_approval_query_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1
     and new.event_type in ('approval_queried', 'approval_query_answered') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_approval_query_events before insert on audit_event
  for each row execute function audit_approval_query_events();

grant select on approval_query to argus_app;
grant insert (case_id, request_id, exception_id, question) on approval_query to argus_app;
"""

# 0020's versions, put back on a downgrade without questions.
OLD_REQUEST_RULES = r"""
create or replace function approval_request_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  v record;
begin
  if tg_op = 'UPDATE' then
    if a[1] <> 'system' then
      raise exception 'only ARGUS marks approval request % as sent', old.id;
    end if;
    if old.sent_at is not null then
      raise exception 'approval request % was sent already and is never sent again', old.id;
    end if;
    if (to_jsonb(new) - 'sent_at') is distinct from (to_jsonb(old) - 'sent_at')
       or new.sent_at is null then
      raise exception 'only the sent time of approval request % can be set', old.id;
    end if;
    new.sent_at := clock_timestamp();
    return new;
  end if;
  if a[1] <> 'system' then
    raise exception 'only ARGUS sends case % for approval', new.case_id;
  end if;
  select kind, status, partner_account into c from invoice_case where id = new.case_id for update;
  if not found then
    raise exception 'case % does not exist', new.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', new.case_id;
  end if;
  if c.status <> 'received' then
    raise exception 'case % is in % and cannot be sent for approval', new.case_id, c.status;
  end if;
  if c.partner_account is null then
    raise exception 'case % has no vendor account yet', new.case_id;
  end if;
  select ap.object_id, ap.name, ap.email, ap.absent,
         dp.object_id as deputy_id, dp.name as deputy_name, dp.email as deputy_email,
         dp.absent as deputy_absent
    into v
  from vendor_approver va
  join approver ap on ap.object_id = va.approver_id
  left join approver dp on dp.object_id = va.deputy_id
  where va.vendor_account = c.partner_account;
  if not found then
    raise exception 'vendor account % has no approver', c.partner_account;
  end if;
  new.vendor_account := c.partner_account;
  if not v.absent then
    new.recipient_id := v.object_id;
    new.recipient_name := v.name;
    new.recipient_address := v.email;
    new.recipient_role := 'approver';
    new.on_behalf_of := null;
    new.on_behalf_of_name := null;
  elsif v.deputy_id is not null and not v.deputy_absent then
    new.recipient_id := v.deputy_id;
    new.recipient_name := v.deputy_name;
    new.recipient_address := v.deputy_email;
    new.recipient_role := 'deputy';
    new.on_behalf_of := v.object_id;
    new.on_behalf_of_name := v.name;
  else
    raise exception 'the approver of vendor account % is away and it has no deputy who is in',
      c.partner_account;
  end if;
  new.requested_by := a[1] || ':' || a[2];
  new.requested_at := clock_timestamp();
  new.txid := txid_current();
  new.sent_at := null;
  return new;
end $$;

create or replace function approval_request_with_move() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if not exists (
    select 1 from audit_event m
    where m.case_id = new.case_id and m.event_type = 'status_changed'
      and m.from_status = 'received' and m.to_status = 'awaiting_approval'
      and m.chain_seq > (select r.chain_seq from audit_event r
                         where r.case_id = new.case_id and r.event_type = 'approval_requested'
                           and (r.details ->> 'request_id')::bigint = new.id)) then
    raise exception
      'approval request for case % needs the case to go for approval in the same transaction',
      new.case_id;
  end if;
  return null;
end $$;

create or replace function case_ready_for_approval() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.status is distinct from 'awaiting_approval' or old.status = 'awaiting_approval' then
    return new;
  end if;
  perform current_actor();  -- no move without an actor, said in the usual words first
  if exists (select 1 from exception_item where case_id = old.id and resolved_at is null) then
    raise exception 'case % has an open exception and cannot go for approval', old.id;
  end if;
  if old.status <> 'received' then
    return new;  -- back from exception to where it was; case_guard checks that
  end if;
  if new.partner_account is null or not exists (
       select 1 from account_proposal
       where case_id = old.id and vendor_account = new.partner_account) then
    raise exception 'case % cannot go for approval without an account proposal', old.id;
  end if;
  if not exists (
       select 1 from approval_request
       where case_id = old.id and vendor_account = new.partner_account
         and txid = txid_current()) then
    raise exception 'case % cannot go for approval without an approval request', old.id;
  end if;
  return new;
end $$;
"""

DOWNGRADE = r"""
-- Questions and their answers are part of the audit trail (NF-01). So a downgrade with
-- questions in the database keeps the table and all its rules, as 0020 and 0021 do: code from
-- before 0022 never touches them, and the upgrade picks them up again (every statement above
-- can run twice). The enum keeps 'approver_query' either way (see 0019 for why).
do $$ begin
  if exists (select 1 from approval_query)
     or exists (select 1 from exception_item where reason::text = 'approver_query') then
    raise notice 'approver questions exist; their table and rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_approval_query_events on audit_event;
  drop function audit_approval_query_events();
  drop trigger exception_approver_query_audit on exception_item;
  drop function exception_approver_query_audit();
  drop trigger exception_approver_query_asked on exception_item;
  drop function exception_approver_query_asked();
  drop trigger exception_approver_query_guard on exception_item;
  drop function exception_approver_query_guard();
  alter table approval_request
    drop column query_id, drop column answered_by_asker, drop column clock_from;
  drop table approval_query;
  drop function approval_query_with_move();
  drop function approval_query_audit();
  drop function approval_query_fixed();
  drop function approval_query_guard();
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    # The request rules go back to 0020's only when no question is kept: an answered question
    # left in the database still needs its new request to be allowed after an upgrade again.
    bind = op.get_bind()
    run_script(bind, DOWNGRADE)
    kept = bind.exec_driver_sql("select to_regclass('public.approval_query') is not null").scalar()
    if not kept:
        run_script(bind, OLD_REQUEST_RULES)

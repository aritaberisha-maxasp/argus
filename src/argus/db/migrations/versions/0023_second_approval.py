"""Benjamin's second approval above EUR 5,000 (issue #39, AP-09).

Covers AP-09, AP-08, AP-10, NF-01, NF-03 and NF-04. An invoice or credit note above EUR 5,000
gross needs a second approval by Benjamin after the vendor's approver approved it. The case
reaches Approved only once both approved, so nothing can be booked after the first approval
alone (AC-3).

This migration adds:

- ``second_approver``: the one person who gives second approvals, Benjamin's Entra account from
  the approver table. Only a person changes it, named in the transaction like a change to the
  approver table (0015), and the database writes ``second_approver_set`` or
  ``second_approver_removed`` to the audit log. It is never taken from an email address.
- ``approval_amount``: for every first approval, the gross amount in EUR the line is checked
  against, and whether it is above EUR 5,000.00. For an invoice in EUR the database writes it
  itself with the decision (rate 1). For another currency ARGUS reads the Wise rate at the time
  of the first approval (#18 question 9) and stores the rate, its time and the EUR amount; the
  database works out the EUR amount and the answer, and refuses a rate from anyone but ARGUS.
  The line uses the amount without its sign, so a credit note above EUR 5,000 needs two
  approvals as well. The audit event ``approval_amount_checked`` holds all of it.
- A second step on approval requests (``step = 'second'``). Only ARGUS writes one, only for a
  case that waits for approval, whose newest request is a first one with an approved decision
  whose amount is above the line, and only with no open exception. The database picks the
  person: the second approver, never the vendor's approver. The request names the first
  decision it follows.
- Two different people approve (AP-09), with one exception Arita accepted on 23 Nov (#39):
  when the second approver is himself the vendor's approver and gave the first approval, the
  case goes to the exception queue as ``second_approver_same_person``. Once a person resolved
  that exception with "carry on", the second request may go to the same person, and it and its
  decision are marked ``same_person``, also in the audit log. For anyone else the database
  refuses the same person on both approvals.
- ``second_approver_missing``: no second approver is set, or he is marked away, so nobody can
  be asked. Both new reasons belong to a case and end only as ``accepted_with_comment`` (by a
  person, rule of 0016) or ``rejected``.
- The rules on the case (``case_settled_by_decision``, replacing 0021's): Approved after a first
  approval only when its EUR amount is known and at or below the line, and after a second
  approval otherwise. A first approval above the line, or one still waiting for the Wise rate,
  commits without moving the case (``approval_decision_with_move``, replacing 0021's).
- AP-10 in ``case_guard`` (0002) stays, with one narrow exception: once ARGUS has read the Wise
  rate of a first approval and the amount is at or below the line, the approval request job
  completes the move to Approved the person already decided. Nothing else changes in
  ``case_guard``; its text is taken from 0002 below so the two cannot drift apart.
- A decision ends the approver's part: once the newest request has a decision, nobody changes
  the account any more (until the second request, whose recipient may, as in 0021).

A downgrade never loses a second request, an amount or the setting: with any of them in the
database it keeps every table and rule, as 0019 to 0021 do.

Revision ID: 0023
Revises: 0021
Create Date: 2026-11-26
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op
from sqlalchemy import Connection, text

from argus.db import run_script

revision = "0023"
down_revision = "0021"
branch_labels = None
depends_on = None

# Above this many euro cents, an invoice needs the second approval (AP-09).
LINE_CENTS = 500_000


def _earlier(prefix: str) -> ModuleType:
    """An earlier migration of this folder, to reuse its SQL instead of copying it."""
    (path,) = Path(__file__).parent.glob(f"{prefix}_*.py")
    spec = importlib.util.spec_from_file_location(f"argus_migration_{prefix}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"migration {prefix} cannot be read")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function(script: str, name: str) -> str:
    """The text of one ``create function`` statement in a migration script."""
    start = script.index(f"create function {name}()")
    end = script.index("end $$;", start) + len("end $$;")
    return script[start:end]


# 0002's AP-10 rule, and the same rule with the one exception of this migration.
OLD_AP10 = """    if old.status = 'awaiting_approval' and new.status = 'approved'
       and (current_actor())[1] <> 'user' then"""
NEW_AP10 = """    if old.status = 'awaiting_approval' and new.status = 'approved'
       and (current_actor())[1] <> 'user' and not approval_completed_after_rate(old.id) then"""


def _case_guard(*, widened: bool) -> str:
    original = _function(_earlier("0002").UPGRADE, "case_guard")
    if original.count(OLD_AP10) != 1:
        raise RuntimeError("case_guard of 0002 no longer holds the AP-10 rule this one widens")
    body = original.replace(OLD_AP10, NEW_AP10) if widened else original
    return body.replace("create function", "create or replace function", 1)


UPGRADE = r"""
alter type exception_reason add value if not exists 'second_approver_missing';
alter type exception_reason add value if not exists 'second_approver_same_person';

-- ------------------------------------------------------------------- the second approver

create table if not exists second_approver (
  only_one boolean primary key default true check (only_one),
  approver_id text not null references approver(object_id),
  changed_at timestamptz not null         -- set by the database
);

create or replace function second_approver_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  perform approver_table_changed_by();
  if tg_op = 'DELETE' then
    return old;
  end if;
  new.only_one := true;
  new.approver_id := lower(btrim(new.approver_id));
  if not exists (select 1 from approver where object_id = new.approver_id) then
    raise exception 'add the second approver to the approver table first';
  end if;
  new.changed_at := clock_timestamp();
  return new;
end $$;
create or replace trigger second_approver_guard before insert or update or delete
  on second_approver for each row execute function second_approver_guard();

create or replace function second_approver_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare
  by_whom jsonb := approver_table_changed_by();
  person record;
begin
  if tg_op = 'DELETE' then
    select name into person from approver where object_id = old.approver_id;
    insert into audit_event (event_type, details)
    values ('second_approver_removed',
            jsonb_build_object('approver', old.approver_id, 'approver_name', person.name)
            || by_whom);
  else
    select name into person from approver where object_id = new.approver_id;
    insert into audit_event (event_type, details)
    values ('second_approver_set',
            jsonb_strip_nulls(jsonb_build_object(
              'approver', new.approver_id, 'approver_name', person.name,
              'before', case when tg_op = 'UPDATE' then old.approver_id end)) || by_whom);
  end if;
  return null;
end $$;
create or replace trigger second_approver_audit after insert or update or delete
  on second_approver for each row execute function second_approver_audit();

-- Clearer than the foreign key's own message, which the admin would see.
create or replace function approver_still_second() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if exists (select 1 from second_approver where approver_id = old.object_id) then
    raise exception 'this person is the second approver; set another one first';
  end if;
  return old;
end $$;
create or replace trigger approver_still_second before delete on approver
  for each row execute function approver_still_second();

-- ------------------------------------------------------- the second step on requests

alter table approval_request add column if not exists step text not null default 'first';
alter table approval_request add column if not exists after_decision_id bigint
  references approval_decision(id);
alter table approval_request add column if not exists same_person boolean not null
  default false;
alter table approval_decision add column if not exists step text not null default 'first';
alter table approval_decision add column if not exists same_person boolean not null
  default false;
do $$ begin
  alter table approval_request add constraint approval_request_step check (
    step in ('first', 'second') and (step = 'second') = (after_decision_id is not null)
    and (step = 'second' or not same_person));
exception when duplicate_object then null;
end $$;
do $$ begin
  alter table approval_decision add constraint approval_decision_step check (
    step in ('first', 'second') and (step = 'second' or not same_person));
exception when duplicate_object then null;
end $$;
-- One second request per first approval.
create unique index if not exists approval_request_second_once
  on approval_request (after_decision_id) where after_decision_id is not null;

-- ------------------------------------------------------------ the amount against the line

create table if not exists approval_amount (
  decision_id bigint primary key references approval_decision(id),
  case_id bigint not null references invoice_case(id),   -- the rest is set by the database
  currency text not null,
  gross_minor bigint not null,
  rate numeric not null check (rate > 0),  -- EUR for one unit of the currency
  rate_time timestamptz not null,          -- the time of the rate, as Wise gave it
  eur_minor bigint not null,
  above_line boolean not null,
  source text not null check (source in ('eur', 'wise')),
  written_by text not null,
  written_at timestamptz not null,
  txid bigint not null
);
create index if not exists approval_amount_case on approval_amount (case_id);

create or replace function approval_amount_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  d record;
  c record;
begin
  select q.case_id, x.decision, x.decided_at, q.step into d
  from approval_decision x join approval_request q on q.id = x.request_id
  where x.id = new.decision_id;
  if not found or d.decision <> 'approved' or d.step <> 'first' then
    raise exception 'only a first approval is checked against EUR 5,000';
  end if;
  select gross_minor, upper(btrim(currency)) as currency into c
  from invoice_case where id = d.case_id;
  if c.gross_minor is null or coalesce(c.currency, '') = '' then
    raise exception 'case % has no gross amount and currency to check', d.case_id;
  end if;
  new.case_id := d.case_id;
  new.currency := c.currency;
  new.gross_minor := c.gross_minor;
  if c.currency = 'EUR' then
    new.rate := 1;
    new.rate_time := d.decided_at;
    new.source := 'eur';
  else
    -- Only the approval request job reads the Wise rate (#18 question 9).
    if a[1] <> 'system' or a[2] <> 'approval-request' then
      raise exception 'only ARGUS stores the Wise rate of case %', d.case_id;
    end if;
    if new.rate is null or new.rate <= 0 or new.rate_time is null
       or new.rate_time > clock_timestamp() + interval '5 minutes' then
      raise exception 'the Wise rate for case % needs a rate above 0 and its time', d.case_id;
    end if;
    new.source := 'wise';
  end if;
  new.eur_minor := round(abs(new.gross_minor) * new.rate);
  new.above_line := new.eur_minor > 500000;
  new.written_by := a[1] || ':' || a[2];
  new.written_at := clock_timestamp();
  new.txid := txid_current();
  return new;
end $$;
create or replace trigger approval_amount_guard before insert on approval_amount
  for each row execute function approval_amount_guard();

create or replace function approval_amount_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'the amount of a first approval is kept as it was checked'; end $$;
create or replace trigger approval_amount_no_change before update or delete
  on approval_amount for each row execute function approval_amount_fixed();
create or replace trigger approval_amount_no_truncate before truncate on approval_amount
  execute function approval_amount_fixed();

create or replace function approval_amount_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, event_type, details)
  values (new.case_id, 'approval_amount_checked',
          jsonb_build_object('decision_id', new.decision_id, 'currency', new.currency,
                             'gross_minor', new.gross_minor, 'rate', new.rate,
                             'rate_time', new.rate_time, 'eur_minor', new.eur_minor,
                             'above_line', new.above_line, 'source', new.source));
  return null;
end $$;
create or replace trigger approval_amount_audit after insert on approval_amount
  for each row execute function approval_amount_audit();

-- An approval in EUR is checked right away, with the decision. Runs as the owner, because the
-- app may not write an amount itself. Named to run after approval_decision_audit, so the trail
-- shows the decision first.
create or replace function approval_decision_amount() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.decision = 'approved' and new.step = 'first' and exists (
       select 1 from invoice_case where id = new.case_id and upper(btrim(currency)) = 'EUR') then
    insert into approval_amount (decision_id) values (new.id);
  end if;
  return null;
end $$;
create or replace trigger approval_decision_with_amount after insert on approval_decision
  for each row execute function approval_decision_amount();

-- ---------------------------------------------------------------- requests and decisions

-- The second step of a request, filled in and checked by the database. Kept apart from the
-- guard, so the guard stays 0020's (later 0022's) with two lines added.
create or replace function approval_request_second(r approval_request)
  returns approval_request language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  v record;
  f record;
begin
  if a[1] <> 'system' then
    raise exception 'only ARGUS sends case % for the second approval', r.case_id;
  end if;
  select kind, status, partner_account into c from invoice_case where id = r.case_id for update;
  if not found then
    raise exception 'case % does not exist', r.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', r.case_id;
  end if;
  if c.status <> 'awaiting_approval' then
    raise exception 'case % is in % and cannot go for a second approval', r.case_id, c.status;
  end if;
  if exists (select 1 from exception_item where case_id = r.case_id and resolved_at is null) then
    raise exception 'case % has an open exception and cannot go for a second approval',
      r.case_id;
  end if;
  select q.id, q.step, q.vendor_account, d.id as decision_id, d.decided_by, d.decided_at
    into f
  from approval_request q
  left join approval_decision d on d.request_id = q.id and d.decision = 'approved'
  where q.case_id = r.case_id order by q.id desc limit 1;
  if not found or f.step <> 'first' or f.decision_id is null then
    raise exception 'case % has no first approval for a second one to follow', r.case_id;
  end if;
  if c.partner_account is distinct from f.vendor_account then
    raise exception 'case % moved to another vendor account since its first approval',
      r.case_id;
  end if;
  if not exists (select 1 from approval_amount where decision_id = f.decision_id and above_line)
  then
    raise exception 'case % is not above EUR 5,000 and needs no second approval', r.case_id;
  end if;
  select ap.object_id, ap.name, ap.email, ap.absent into v
  from second_approver s join approver ap on ap.object_id = s.approver_id;
  if not found then
    raise exception 'no second approver is set for invoices above EUR 5,000';
  end if;
  if v.absent then
    raise exception 'the second approver is marked away';
  end if;
  r.vendor_account := c.partner_account;
  r.recipient_id := v.object_id;
  r.recipient_name := v.name;
  r.recipient_address := v.email;
  r.recipient_role := 'approver';
  r.on_behalf_of := null;
  r.on_behalf_of_name := null;
  r.after_decision_id := f.decision_id;
  r.same_person := false;
  -- Arita's exception (#39, 23 Nov): the second approver gave the first approval as the
  -- vendor's approver. Only after a person let that case carry on from the exception queue.
  if lower(f.decided_by) = lower('user:' || v.object_id) then
    if not exists (
         select 1 from exception_item e
         where e.case_id = r.case_id
           and e.reason::text = 'second_approver_same_person'
           and e.outcome = 'accepted_with_comment'
           and e.resolved_at > f.decided_at) then
      raise exception 'the second approver gave the first approval of case % too; that '
        'needs the same person exception to be resolved first', r.case_id;
    end if;
    r.same_person := true;
  end if;
  r.requested_by := a[1] || ':' || a[2];
  r.requested_at := clock_timestamp();
  r.txid := txid_current();
  r.sent_at := null;
  return r;
end $$;

-- 0020's guard, with the two lines marked #39: a second request is filled in above, and every
-- other request is a first one, exactly as before.
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
  if new.step = 'second' then return approval_request_second(new); end if;  -- #39
  new.step := 'first'; new.after_decision_id := null; new.same_person := false;  -- #39
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

-- A second request goes out without a move: the case already waits for approval.
drop trigger if exists approval_request_with_move on approval_request;
create constraint trigger approval_request_with_move after insert on approval_request
  deferrable initially deferred for each row when (new.step = 'first')
  execute function approval_request_with_move();

-- 0020's audit event, with the step and the same person mark.
create or replace function approval_request_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id, 'approval_requested',
            jsonb_strip_nulls(jsonb_build_object(
              'request_id', new.id,
              'vendor_account', new.vendor_account,
              'recipient', new.recipient_id,
              'name', new.recipient_name,
              'address', new.recipient_address,
              'as', new.recipient_role,
              'on_behalf_of', new.on_behalf_of,
              'on_behalf_of_name', new.on_behalf_of_name,
              'step', case when new.step = 'second' then 'second' end,
              'after_decision_id', new.after_decision_id,
              'same_person', case when new.same_person then true end)));
  elsif new.sent_at is not null and old.sent_at is null then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id, 'approval_request_sent',
            jsonb_build_object('request_id', new.id, 'name', new.recipient_name,
                               'address', new.recipient_address));
  end if;
  return null;
end $$;

-- Runs after approval_decision_guard (name order), which already checked who decides. Copies
-- the step and the mark from the request and holds the four eyes rule (AP-09).
create or replace function approval_decision_step() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  r record;
  first_by text;
begin
  select step, after_decision_id, same_person into r
  from approval_request where id = new.request_id;
  new.step := r.step;
  new.same_person := r.same_person;
  if r.step = 'second' then
    select decided_by into first_by from approval_decision where id = r.after_decision_id;
    if lower(first_by) = lower(new.decided_by) and not r.same_person then
      raise exception 'the same person cannot give both approvals of case %', new.case_id;
    end if;
  end if;
  return new;
end $$;
create or replace trigger approval_decision_step before insert on approval_decision
  for each row execute function approval_decision_step();

-- 0021's audit event, with the step and the same person mark, so an auditor sees it at once.
create or replace function approval_decision_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, event_type, details)
  values (new.case_id, 'approval_decided',
          jsonb_strip_nulls(jsonb_build_object(
            'decision_id', new.id,
            'request_id', new.request_id,
            'decision', new.decision,
            'proposal_id', new.proposal_id,
            'name', new.decided_by_name,
            'sign_in_address', new.decided_by_address,
            'as', new.decided_as,
            'on_behalf_of', new.on_behalf_of,
            'on_behalf_of_name', new.on_behalf_of_name,
            'step', case when new.step = 'second' then 'second' end,
            'same_person', case when new.same_person then true end,
            'comment_sha256',
              encode(sha256(convert_to(new.comment, 'UTF8')), 'hex'))));
  return null;
end $$;

-- 0021's check at commit, except for a first approval that does not settle the case yet: it
-- waits for the Wise rate, or for the second approval (AP-09).
create or replace function approval_decision_with_move() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.decision = 'approved' and new.step = 'first'
     and not exists (select 1 from approval_amount
                     where decision_id = new.id and not above_line) then
    return null;
  end if;
  if not exists (
    select 1 from audit_event m
    where m.case_id = new.case_id and m.event_type = 'status_changed'
      and m.from_status = 'awaiting_approval'
      and m.to_status = case new.decision when 'approved' then 'approved'::case_status
                                          else 'closed'::case_status end
      and m.chain_seq > (select d.chain_seq from audit_event d
                         where d.case_id = new.case_id and d.event_type = 'approval_decided'
                           and (d.details ->> 'decision_id')::bigint = new.id)) then
    raise exception 'a decision on case % needs the case to move in the same transaction',
      new.case_id;
  end if;
  return null;
end $$;

-- Once the newest request has a decision, the account is settled for that step. The second
-- approver may change it on his own request, as any approver may (0021).
create or replace function account_proposal_after_decision() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if exists (
       select 1 from invoice_case c
       join lateral (select id from approval_request where case_id = c.id
                     order by id desc limit 1) q on true
       join approval_decision d on d.request_id = q.id
       where c.id = new.case_id and c.status = 'awaiting_approval') then
    raise exception 'case % was decided on already; its account cannot change now', new.case_id;
  end if;
  return new;
end $$;
create or replace trigger account_proposal_after_decision before insert on account_proposal
  for each row execute function account_proposal_after_decision();

-- ------------------------------------------------------------------------ the case rules

-- Whether ARGUS may complete the approval a person gave: the newest request is a first one, a
-- person approved it, and ARGUS read the Wise rate afterwards and found the amount at or below
-- the line. Only the approval request job asks (case_guard).
create or replace function approval_completed_after_rate(case_id bigint) returns boolean
  language sql stable
set search_path = pg_catalog, public, pg_temp as $$
  select (current_actor())[1] = 'system' and (current_actor())[2] = 'approval-request'
    and exists (
      select 1 from approval_request q
      join approval_decision d on d.request_id = q.id and d.decision = 'approved'
      join approval_amount m on m.decision_id = d.id
      where q.case_id = $1 and q.step = 'first' and d.decided_by like 'user:%'
        and m.source = 'wise' and not m.above_line
        and q.id = (select max(id) from approval_request where approval_request.case_id = $1))
$$;

-- 0021's rule, with the line. Leaving Awaiting approval for Approved or Closed needs the
-- decision of the person asked on the newest request (AP-07, AP-10). Approved also needs a
-- current account proposal (PR-04) and, after a first approval, an EUR amount at or below the
-- line; above it, only the second approval settles the case (AP-09).
create or replace function case_settled_by_decision() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[];
  wanted text;
  q record;
  d record;
  m record;
begin
  -- A waiting invoice that went to the exception queue is closed only by a person there (or
  -- as rejected by the approver below), never by a job (AP-07, AP-10).
  if old.status = 'exception' and old.resume_status = 'awaiting_approval'
     and new.status = 'closed' and (current_actor())[1] <> 'user' then
    raise exception 'case % waited for approval and only a person can close it', old.id;
  end if;
  if old.status is distinct from 'awaiting_approval'
     or new.status not in ('approved', 'closed') then
    return new;
  end if;
  a := current_actor();
  if new.status = 'closed' and new.closed_reason is distinct from 'rejected' then
    raise exception 'case % waits for approval and can only be closed as rejected', old.id;
  end if;
  wanted := case new.status when 'approved' then 'approved' else 'rejected' end;
  select id, step, vendor_account into q
  from approval_request where case_id = old.id order by id desc limit 1;
  if found then
    select id, proposal_id, decided_by, txid into d
    from approval_decision where request_id = q.id and decision = wanted;
  end if;
  if q.id is null or d.id is null or not (
       (d.txid = txid_current() and d.decided_by = a[1] || ':' || a[2])
       or (new.status = 'approved' and approval_completed_after_rate(old.id))) then
    raise exception 'case % can only be % by the person asked, on the approval page',
      old.id, wanted;
  end if;
  if new.status = 'approved' then
    if new.partner_account is distinct from q.vendor_account then
      raise exception 'case % moved to another vendor account since its approval request',
        old.id;
    end if;
    -- The proposal the person approved must still be the current one (AP-08).
    if d.proposal_id is null or d.proposal_id is distinct from (
         select p.id from account_proposal p
         where p.case_id = old.id and p.vendor_account = new.partner_account
         order by p.id desc limit 1) then
      raise exception 'case % can only be approved with the G/L account and tax key proposal '
        'the person saw', old.id;
    end if;
    if q.step = 'first' then
      select above_line into m from approval_amount where decision_id = d.id;
      if not found then
        raise exception 'case % waits for its amount in EUR before it can be approved', old.id;
      end if;
      if m.above_line then
        raise exception 'case % is above EUR 5,000 and needs the second approval', old.id;
      end if;
    end if;
    -- A second request exists only after a first approval above the line (its guard), and
    -- its decision is the person's own, so it settles the case.
  end if;
  return new;
end $$;

-- ------------------------------------------------------------------------ new exceptions

create or replace function second_approver_exception_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.reason::text in ('second_approver_missing', 'second_approver_same_person')
       and new.case_id is null then
      raise exception 'a second approval exception always belongs to a case';
    end if;
    return new;
  end if;
  if old.reason::text not in ('second_approver_missing', 'second_approver_same_person')
     or new.outcome is null or old.outcome is not null then
    return new;
  end if;
  if new.outcome not in ('accepted_with_comment', 'rejected') then
    raise exception 'exception % can only end as carry on or rejected', old.id;
  end if;
  return new;
end $$;
create or replace trigger second_approver_exception_guard before insert or update
  on exception_item for each row execute function second_approver_exception_guard();

-- Like the other database events: only the triggers above write these.
create or replace function audit_second_approval_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type in
     ('approval_amount_checked', 'second_approver_set', 'second_approver_removed') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_second_approval_events before insert on audit_event
  for each row execute function audit_second_approval_events();

grant select on second_approver, approval_amount to argus_app;
grant insert (approver_id), update (approver_id), delete on second_approver to argus_app;
grant insert (decision_id, rate, rate_time) on approval_amount to argus_app;
grant insert (step) on approval_request to argus_app;
"""

DOWNGRADE = r"""
-- Second requests, amounts and the setting are part of the audit trail (NF-01). With any of
-- them in the database, everything stays, as 0019 to 0021 do: code from before 0023 never
-- touches them, and the upgrade picks them up again. The enum keeps its values either way.
do $$ begin
  if exists (select 1 from approval_amount) or exists (select 1 from second_approver)
     or exists (select 1 from approval_request where step <> 'first')
     or exists (select 1 from exception_item where reason::text in
                ('second_approver_missing', 'second_approver_same_person')) then
    raise notice 'second approvals exist; their tables and rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_second_approval_events on audit_event;
  drop function audit_second_approval_events();
  drop trigger second_approver_exception_guard on exception_item;
  drop function second_approver_exception_guard();
  drop trigger account_proposal_after_decision on account_proposal;
  drop function account_proposal_after_decision();
  drop trigger approval_decision_step on approval_decision;
  drop function approval_decision_step();
  drop trigger approval_decision_with_amount on approval_decision;
  drop function approval_decision_amount();
  drop table approval_amount;
  drop function approval_amount_audit();
  drop function approval_amount_fixed();
  drop function approval_amount_guard();
  drop trigger approver_still_second on approver;
  drop function approver_still_second();
  drop table second_approver;
  drop function second_approver_audit();
  drop function approval_request_second(approval_request);
  drop function second_approver_guard();
  drop trigger approval_request_with_move on approval_request;
  create constraint trigger approval_request_with_move after insert on approval_request
    deferrable initially deferred for each row execute function approval_request_with_move();
  drop index approval_request_second_once;
  alter table approval_request drop constraint approval_request_step;
  alter table approval_decision drop constraint approval_decision_step;
  alter table approval_request drop column same_person, drop column after_decision_id,
    drop column step;
  alter table approval_decision drop column same_person, drop column step;
end $$;
"""


def _kept(bind: Connection) -> bool:
    """Whether the downgrade kept 0023's tables, because data stands on them."""
    found = bind.execute(text("select to_regclass('approval_amount') is not null"))
    return bool(found.scalar_one())


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE + _case_guard(widened=True))


def downgrade() -> None:
    bind = op.get_bind()
    run_script(bind, DOWNGRADE)
    if _kept(bind):
        return
    # The functions as 0020, 0021 and 0002 left them. Both scripts run twice safely.
    run_script(bind, _earlier("0020").UPGRADE)
    run_script(bind, _earlier("0021").UPGRADE)
    run_script(bind, _case_guard(widened=False))
    run_script(bind, "drop function approval_completed_after_rate(bigint);")

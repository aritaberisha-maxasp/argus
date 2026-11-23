"""Approval requests: a checked invoice goes to its approver (issue #191, ADR 0010).

Covers AP-02, AP-06, PR-04, PR-05, VM-04, NF-01 and NF-03. Once an incoming invoice or credit
note has passed every check, ARGUS moves it from Received to Awaiting approval and emails the
vendor's approver, or the deputy while the approver is away (:mod:`argus.intake.approvals`).

This migration adds:

- ``approval_request``: one row per request, never removed. ARGUS only names the case; the
  database picks the person from the approver table at that moment (the approver, or the
  deputy while the approver is marked away) and keeps their object ID, name and address as
  they were then, whether they are asked as approver or deputy, and for whom. Only a job may
  write it, and only for an incoming case in Received whose vendor has someone to ask. The one
  later change allowed is the sent time, set once by the database after Graph accepted the
  email. #38 reads the newest request to decide who may approve.
- A rule on the case: it moves from Received to Awaiting approval only with a current account
  proposal (PR-04) and an approval request written in the same transaction, and never while it
  has an open exception (for example an IBAN hold, PR-05). The duplicate key (IN-10) is already
  required by migration 0010.
- A new exception reason ``approver_absent``: the approver is marked away and the vendor has no
  deputy, so nobody can be asked. It always belongs to a case and ends only as ``retried``
  (someone can be asked again) or ``rejected``.
- A rule on account proposals (compliance review of PR 190): once a person entered the account
  for a case and vendor account, a later proposal from the booking history for the same case and
  vendor account is refused, so a job never replaces a person's reasoned choice.
- The audit events ``approval_requested`` and ``approval_request_sent``, written by the database
  only.

The new reason is compared as text, because a value added to an enum cannot be used in the same
transaction. A downgrade never loses a request: with requests in the database it keeps the
table and its rules, as 0019 does.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-23
"""

from alembic import op

from argus.db import run_script

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter type exception_reason add value if not exists 'approver_absent';

create table if not exists approval_request (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  vendor_account text not null,           -- the rest is set by the database
  recipient_id text not null,             -- Entra object ID of the person asked
  recipient_name text not null,
  recipient_address text not null,
  recipient_role text not null check (recipient_role in ('approver', 'deputy')),
  on_behalf_of text,                      -- the approver who is away, when a deputy is asked
  on_behalf_of_name text,
  requested_by text not null,
  requested_at timestamptz not null,
  txid bigint not null,
  sent_at timestamptz,                    -- set once, after Graph accepted the email
  check ((recipient_role = 'deputy') = (on_behalf_of is not null and on_behalf_of_name is not null))
);
create index if not exists approval_request_case on approval_request (case_id, id);
create index if not exists approval_request_unsent on approval_request (id) where sent_at is null;

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
         dp.object_id as deputy_id, dp.name as deputy_name, dp.email as deputy_email
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
  elsif v.deputy_id is not null then
    new.recipient_id := v.deputy_id;
    new.recipient_name := v.deputy_name;
    new.recipient_address := v.deputy_email;
    new.recipient_role := 'deputy';
    new.on_behalf_of := v.object_id;
    new.on_behalf_of_name := v.name;
  else
    raise exception 'the approver of vendor account % is away and it has no deputy',
      c.partner_account;
  end if;
  new.requested_by := a[1] || ':' || a[2];
  new.requested_at := clock_timestamp();
  new.txid := txid_current();
  new.sent_at := null;
  return new;
end $$;
create or replace trigger approval_request_guard before insert or update on approval_request
  for each row execute function approval_request_guard();

create or replace function approval_request_no_delete() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'an approval request is kept as it was made'; end $$;
create or replace trigger approval_request_no_delete before delete on approval_request
  for each row execute function approval_request_no_delete();
create or replace trigger approval_request_no_truncate before truncate on approval_request
  execute function approval_request_no_delete();

-- Runs as the owner, like the other audit triggers. The address goes in: it is where the
-- invoice went, and an auditor must see it (as for the approver table, PR 175).
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
              'on_behalf_of_name', new.on_behalf_of_name)));
  elsif new.sent_at is not null and old.sent_at is null then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id, 'approval_request_sent',
            jsonb_build_object('request_id', new.id, 'name', new.recipient_name,
                               'address', new.recipient_address));
  end if;
  return null;
end $$;
create or replace trigger approval_request_audit after insert or update on approval_request
  for each row execute function approval_request_audit();

-- A case goes for approval only once every check has passed (#191, PR-04, PR-05, IN-10).
-- Named to run after case_guard (triggers of one event run in name order), so a move that is
-- not allowed at all, or has no actor, is refused with case_guard's own words first.
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
  -- The duplicate key (IN-10) is checked by invoice_case_duplicate_guard of migration 0010.
  if not exists (
       select 1 from approval_request
       where case_id = old.id and vendor_account = new.partner_account
         and txid = txid_current()) then
    raise exception 'case % cannot go for approval without an approval request', old.id;
  end if;
  return new;
end $$;
create or replace trigger case_ready_for_approval before update on invoice_case
  for each row execute function case_ready_for_approval();

-- A person's reasoned choice of account wins over the booking history (compliance review of
-- PR 190).
create or replace function account_proposal_person_wins() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.source = 'booking_history' and exists (
       select 1 from account_proposal
       where case_id = new.case_id and vendor_account = new.vendor_account
         and source = 'person') then
    raise exception
      'a person entered the account of case % for vendor account %; ARGUS does not replace it',
      new.case_id, new.vendor_account;
  end if;
  return new;
end $$;
create or replace trigger account_proposal_person_wins before insert on account_proposal
  for each row execute function account_proposal_person_wins();

create or replace function approver_absent_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare account text;
begin
  if tg_op = 'INSERT' then
    if new.reason::text = 'approver_absent' and new.case_id is null then
      raise exception 'an absent approver always belongs to a case';
    end if;
    return new;
  end if;
  if old.reason::text <> 'approver_absent' or new.outcome is null
     or old.outcome is not null then
    return new;
  end if;
  if new.outcome not in ('retried', 'rejected') then
    raise exception 'an absent approver can only end as retried or rejected';
  end if;
  if new.outcome = 'retried' then
    select partner_account into account from invoice_case where id = old.case_id;
    if not exists (
         select 1 from vendor_approver va join approver ap on ap.object_id = va.approver_id
         where va.vendor_account = account
           and (not ap.absent or va.deputy_id is not null)) then
      raise exception 'exception % ends as retried only once someone can approve again', old.id;
    end if;
  end if;
  return new;
end $$;
create or replace trigger approver_absent_guard before insert or update on exception_item
  for each row execute function approver_absent_guard();

-- Like the other database events: only the trigger above writes these.
create or replace function audit_approval_request_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1
     and new.event_type in ('approval_requested', 'approval_request_sent') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_approval_request_events before insert on audit_event
  for each row execute function audit_approval_request_events();

grant select on approval_request to argus_app;
grant insert (case_id) on approval_request to argus_app;
grant update (sent_at) on approval_request to argus_app;
"""

DOWNGRADE = r"""
-- Approval requests are part of the audit trail (NF-01). So a downgrade with requests or
-- absent approver exceptions in the database keeps the table and all its rules, as 0019 does:
-- code from before 0020 never touches them, and the upgrade picks them up again. The enum
-- keeps 'approver_absent' either way (see 0019 for why).
do $$ begin
  if exists (select 1 from approval_request)
     or exists (select 1 from exception_item where reason::text = 'approver_absent') then
    raise notice 'approval requests exist; their table and rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_approval_request_events on audit_event;
  drop function audit_approval_request_events();
  drop trigger approver_absent_guard on exception_item;
  drop function approver_absent_guard();
  drop trigger account_proposal_person_wins on account_proposal;
  drop function account_proposal_person_wins();
  drop trigger case_ready_for_approval on invoice_case;
  drop function case_ready_for_approval();
  drop table approval_request;
  drop function approval_request_audit();
  drop function approval_request_no_delete();
  drop function approval_request_guard();
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

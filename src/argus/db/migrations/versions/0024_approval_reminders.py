"""Remind the approver after 3 working days and tell finance after 5 (issue #40).

Covers AP-05, AP-06, AP-08, NF-01, NF-03 and NF-04. An invoice should not sit with an approver.
3 working days after its approval request was emailed, the person on it gets a reminder; after
5 working days the escalation address (Benjamin) gets an email with a link to the case. Arita
decided on 23 Nov that Benjamin may not decide on an escalated case: only the person on the
newest request still decides. Working days are Baden-Württemberg working days, worked out by
ARGUS (``argus.working_days``); the clock starts at ``coalesce(clock_from, sent_at)`` of the
newest request (migration 0022), so an answered question or a new request starts it again.

This migration adds:

- ``approval_nudge``: one row per reminder or escalation, never removed. ARGUS names the
  request, the kind and the working day it was due; for an escalation also the address, which
  is a setting the database does not know. The database fills in the rest: the case, the start
  of the clock, and for a reminder the person and address of the request. It refuses a row
  from anyone but a job, for a request that is not the newest of its case, not emailed yet or
  decided already, for a case that does not wait for approval, and one due earlier than 3 (or
  5) calendar days after the clock started or later than today. At most one row per case,
  kind and clock, unless an earlier one failed for certain: a new request that keeps the old
  clock (0022, the asker answered their own question) gets no second reminder or escalation
  for the same wait (code review of PR 212).
- A row is written before the email goes out. After that only one thing may change, once:
  the sent time, after Graph accepted the email, or the failed time, when Graph refused it for
  certain. A row with neither is an email whose fate is unknown (a crash or a timeout), and
  ARGUS never sends it again: a reminder is never sent twice, also when a sweep fails halfway.
- A new request for a case that already waits for approval (``renews_id``). Only a job writes
  one, only for the newest request of the case while it has no decision, and only when that
  person can no longer decide: the case moved to another vendor account, or the person is no
  longer that vendor's approver or deputy, or is marked away (AP-06). Also when the case just
  came back from an absent approver or approver address exception, so the clock starts again
  for whoever is asked now. The database picks who is asked, exactly as for a first request
  (the approver, or the deputy while the approver is away). The old request can no longer be
  decided, because only the newest one can (0021). The database also keeps why
  (``renew_reason``: back from the exception queue, vendor changed, person away, person
  replaced) and writes ``approval_request_renewed`` with the old request and the reason, so
  the trail shows which request was replaced (compliance review of PR 212). The clock starts
  again from the new
  request. The guard of 0022 gets one line, marked #40, that hands such a request to
  ``approval_request_renewal``; everything else in it stays as it was.
- The audit events ``approval_reminder_sent``, ``approval_escalation_sent`` and
  ``approval_nudge_failed``, written by the database only. They hold the request, the address
  and the due day, nothing from the invoice (NF-03).

Nothing here approves, books or pays anything (NF-04). A downgrade never loses a reminder or a
new request: with any of them in the database it keeps the table, the column and every rule,
as 0019 to 0022 do.

Revision ID: 0024
Revises: 0022
Create Date: 2026-11-26
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

from argus.db import run_script

revision = "0024"
down_revision = "0022"
branch_labels = None
depends_on = None


def _earlier(prefix: str) -> ModuleType:
    """An earlier migration of this folder, to reuse its SQL instead of copying it."""
    (path,) = Path(__file__).parent.glob(f"{prefix}_*.py")
    spec = importlib.util.spec_from_file_location(f"argus_migration_{prefix}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"migration {prefix} cannot be read")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The migration whose approval request guard this one extends. When #39's 0023 lands before this
# one, this becomes "0023", and the renewal also sets step 'first' (see approval_request_renewal).
GUARD_FROM = "0022"
GUARD_START = "create or replace function approval_request_guard()"
# The end of the UPDATE branch of the guard: the new line goes right after it.
AFTER_UPDATE = "    new.sent_at := clock_timestamp();\n    return new;\n  end if;\n"
RENEWAL_LINE = (
    "  if new.renews_id is not null then return approval_request_renewal(new); end if;  -- #40\n"
)


def _guard(*, renewal: bool) -> str:
    script: str = _earlier(GUARD_FROM).UPGRADE
    if script.count(GUARD_START) != 1:
        raise RuntimeError(f"migration {GUARD_FROM} no longer holds one approval request guard")
    start = script.index(GUARD_START)
    end = script.index("end $$;", start) + len("end $$;")
    body = script[start:end]
    if body.count(AFTER_UPDATE) != 1:
        raise RuntimeError("the approval request guard no longer ends its UPDATE branch as known")
    return body.replace(AFTER_UPDATE, AFTER_UPDATE + RENEWAL_LINE) if renewal else body


UPGRADE = r"""
-- ------------------------------------------------- a new request for a waiting case (AP-06)

alter table approval_request
  add column if not exists renews_id bigint references approval_request(id),
  add column if not exists renew_reason text check (renew_reason in
    ('back_from_exception', 'vendor_changed', 'person_away', 'person_replaced'));
create unique index if not exists approval_request_renewed_once
  on approval_request (renews_id) where renews_id is not null;

create or replace function approval_request_renewal(r approval_request)
  returns approval_request language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  o record;
  v record;
  back boolean;
begin
  if a[1] <> 'system' then
    raise exception 'only ARGUS sends case % to someone else for approval', r.case_id;
  end if;
  select kind, status, partner_account into c from invoice_case where id = r.case_id for update;
  if not found then
    raise exception 'case % does not exist', r.case_id;
  end if;
  if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
    raise exception 'case % is not an incoming invoice or credit note', r.case_id;
  end if;
  if c.status <> 'awaiting_approval' then
    raise exception 'case % is in % and does not wait for approval', r.case_id, c.status;
  end if;
  select q.id, q.vendor_account, q.recipient_id, q.requested_at into o
  from approval_request q where q.case_id = r.case_id order by q.id desc limit 1;
  if not found or o.id is distinct from r.renews_id then
    raise exception 'approval request % is not the newest one of case %', r.renews_id, r.case_id;
  end if;
  if exists (select 1 from approval_decision where request_id = o.id) then
    raise exception 'approval request % has a decision already', o.id;
  end if;
  if c.partner_account is null then
    raise exception 'case % has no vendor account', r.case_id;
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
  -- Back from an absent approver or approver address exception since the old request.
  back := exists (
    select 1 from exception_item e
    where e.case_id = r.case_id
      and e.reason::text in ('approver_absent', 'approver_address_outside')
      and e.outcome = 'retried' and e.resolved_at > o.requested_at);
  if c.partner_account = o.vendor_account and not back and (
       coalesce(lower(o.recipient_id) = lower(v.object_id) and not v.absent, false)
       or coalesce(lower(o.recipient_id) = lower(v.deputy_id) and not v.deputy_absent, false))
  then
    raise exception 'the person asked on approval request % can still decide on case %',
      o.id, r.case_id;
  end if;
  r.renew_reason := case
    when back then 'back_from_exception'
    when c.partner_account is distinct from o.vendor_account then 'vendor_changed'
    when coalesce(lower(o.recipient_id) in (lower(v.object_id), lower(v.deputy_id)), false)
      then 'person_away'
    else 'person_replaced' end;
  r.vendor_account := c.partner_account;
  if not v.absent then
    r.recipient_id := v.object_id;
    r.recipient_name := v.name;
    r.recipient_address := v.email;
    r.recipient_role := 'approver';
    r.on_behalf_of := null;
    r.on_behalf_of_name := null;
  elsif v.deputy_id is not null and not v.deputy_absent then
    r.recipient_id := v.deputy_id;
    r.recipient_name := v.deputy_name;
    r.recipient_address := v.deputy_email;
    r.recipient_role := 'deputy';
    r.on_behalf_of := v.object_id;
    r.on_behalf_of_name := v.name;
  else
    raise exception 'the approver of vendor account % is away and it has no deputy who is in',
      c.partner_account;
  end if;
  r.requested_by := a[1] || ':' || a[2];
  r.requested_at := clock_timestamp();
  r.txid := txid_current();
  r.sent_at := null;
  -- A new person, so a new clock (#40).
  r.query_id := null;
  r.answered_by_asker := false;
  r.clock_from := null;
  return r;
end $$;

-- The trail names the request a new one replaced, and why (compliance review of PR 212). Runs
-- as the owner, like the other audit triggers, after 0020's approval_requested (name order).
create or replace function approval_request_renewal_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.renews_id is not null then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id, 'approval_request_renewed',
            jsonb_build_object('request_id', new.id, 'renews', new.renews_id,
                               'reason', new.renew_reason, 'name', new.recipient_name,
                               'as', new.recipient_role));
  end if;
  return null;
end $$;
create or replace trigger approval_request_renewal_audit after insert on approval_request
  for each row execute function approval_request_renewal_audit();

-- A new request for a waiting case goes out without a move: the case already waits.
drop trigger if exists approval_request_with_move on approval_request;
create constraint trigger approval_request_with_move after insert on approval_request
  deferrable initially deferred for each row when (new.renews_id is null)
  execute function approval_request_with_move();

-- ------------------------------------------------------------ reminders and escalations

create table if not exists approval_nudge (
  id bigint generated always as identity primary key,
  request_id bigint not null references approval_request(id),
  case_id bigint not null references invoice_case(id),   -- set by the database
  kind text not null check (kind in ('reminder', 'escalation')),
  recipient_address text not null check (
    length(recipient_address) between 3 and 320
    and recipient_address ~ '^[^@\s,;<>]+@[^@\s,;<>]+$'),
  recipient_name text,                    -- the person asked, for a reminder
  clock_from timestamptz not null,        -- set by the database from the request
  due_on date not null,                   -- the working day it was due, from ARGUS
  written_by text not null,
  written_at timestamptz not null,
  txid bigint not null,
  sent_at timestamptz,                    -- set once, after Graph accepted the email
  failed_at timestamptz,                  -- set once, when Graph refused it for certain
  check (sent_at is null or failed_at is null)
);
-- One per wait: a new request that keeps the clock (0022) shares the old rows.
create unique index if not exists approval_nudge_once
  on approval_nudge (case_id, kind, clock_from) where failed_at is null;
create index if not exists approval_nudge_case on approval_nudge (case_id, id);

create or replace function approval_nudge_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  q record;
  today date := (clock_timestamp() at time zone 'Europe/Berlin')::date;
  days int;
begin
  if a[1] <> 'system' then
    raise exception 'only ARGUS reminds an approver or escalates a waiting invoice';
  end if;
  if tg_op = 'UPDATE' then
    if old.sent_at is not null or old.failed_at is not null then
      raise exception 'reminder % was sent or failed already and never changes again', old.id;
    end if;
    if (to_jsonb(new) - 'sent_at' - 'failed_at') is distinct from
       (to_jsonb(old) - 'sent_at' - 'failed_at')
       or (new.sent_at is null) = (new.failed_at is null) then
      raise exception 'only the sent time or the failed time of reminder % can be set', old.id;
    end if;
    if new.sent_at is not null then
      new.sent_at := clock_timestamp();
    else
      new.failed_at := clock_timestamp();
    end if;
    return new;
  end if;
  select r.id, r.case_id, r.sent_at, r.clock_from, r.recipient_name, r.recipient_address,
         c.status
    into q
  from approval_request r join invoice_case c on c.id = r.case_id
  where r.id = new.request_id
  for update of c;
  if not found then
    raise exception 'approval request % does not exist', new.request_id;
  end if;
  if q.status <> 'awaiting_approval' then
    raise exception 'case % does not wait for approval', q.case_id;
  end if;
  if exists (select 1 from approval_request
             where case_id = q.case_id and id > q.id) then
    raise exception 'approval request % is not the newest one of case %', q.id, q.case_id;
  end if;
  if q.sent_at is null then
    raise exception 'approval request % was not emailed yet', q.id;
  end if;
  if exists (select 1 from approval_decision where request_id = q.id) then
    raise exception 'approval request % has a decision already', q.id;
  end if;
  new.case_id := q.case_id;
  new.clock_from := coalesce(q.clock_from, q.sent_at);
  -- ARGUS counts the working days (holidays); a working day is never sooner than a day.
  days := case new.kind when 'reminder' then 3 else 5 end;
  if new.due_on < (new.clock_from at time zone 'Europe/Berlin')::date + days then
    raise exception 'a % for approval request % is not due before % working days',
      new.kind, q.id, days;
  end if;
  if new.due_on > today then
    raise exception 'a % for approval request % is not due yet', new.kind, q.id;
  end if;
  if new.kind = 'reminder' then
    new.recipient_name := q.recipient_name;
    new.recipient_address := q.recipient_address;
  else
    new.recipient_name := null;
    new.recipient_address := btrim(new.recipient_address);
  end if;
  new.written_by := a[1] || ':' || a[2];
  new.written_at := clock_timestamp();
  new.txid := txid_current();
  new.sent_at := null;
  new.failed_at := null;
  return new;
end $$;
create or replace trigger approval_nudge_guard before insert or update on approval_nudge
  for each row execute function approval_nudge_guard();

create or replace function approval_nudge_no_delete() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'a reminder is kept as it was made'; end $$;
create or replace trigger approval_nudge_no_delete before delete on approval_nudge
  for each row execute function approval_nudge_no_delete();
create or replace trigger approval_nudge_no_truncate before truncate on approval_nudge
  execute function approval_nudge_no_delete();

-- Runs as the owner, like the other audit triggers. The address goes in, as for requests: it is
-- where the email went, and an auditor must see it.
create or replace function approval_nudge_audit() returns trigger
  language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if old.sent_at is null and new.sent_at is not null then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id,
            case new.kind when 'reminder' then 'approval_reminder_sent'
                          else 'approval_escalation_sent' end,
            jsonb_strip_nulls(jsonb_build_object(
              'nudge_id', new.id,
              'request_id', new.request_id,
              'name', new.recipient_name,
              'address', new.recipient_address,
              'due_on', new.due_on,
              'clock_from', new.clock_from)));
  elsif old.failed_at is null and new.failed_at is not null then
    insert into audit_event (case_id, event_type, details)
    values (new.case_id, 'approval_nudge_failed',
            jsonb_build_object('nudge_id', new.id, 'request_id', new.request_id,
                               'kind', new.kind));
  end if;
  return null;
end $$;
create or replace trigger approval_nudge_audit after update on approval_nudge
  for each row execute function approval_nudge_audit();

-- Like the other database events: only the trigger above writes these.
create or replace function audit_approval_nudge_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type in
       ('approval_reminder_sent', 'approval_escalation_sent', 'approval_nudge_failed',
        'approval_request_renewed') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_approval_nudge_events before insert on audit_event
  for each row execute function audit_approval_nudge_events();

grant insert (renews_id) on approval_request to argus_app;
grant select on approval_nudge to argus_app;
grant insert (request_id, kind, recipient_address, due_on) on approval_nudge to argus_app;
grant update (sent_at, failed_at) on approval_nudge to argus_app;
"""

DOWNGRADE = r"""
-- Reminders and new requests are part of the audit trail (NF-01). So a downgrade with any of
-- them in the database keeps the table, the column and all their rules, as 0020 to 0022 do:
-- code from before 0024 never touches them, and the upgrade picks them up again (every
-- statement above can run twice).
do $$ begin
  if exists (select 1 from approval_nudge)
     or exists (select 1 from approval_request where renews_id is not null) then
    raise notice 'reminders or new approval requests exist; their rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_approval_nudge_events on audit_event;
  drop function audit_approval_nudge_events();
  drop table approval_nudge;
  drop function approval_nudge_audit();
  drop function approval_nudge_no_delete();
  drop function approval_nudge_guard();
  drop trigger approval_request_with_move on approval_request;
  create constraint trigger approval_request_with_move after insert on approval_request
    deferrable initially deferred for each row execute function approval_request_with_move();
  drop trigger approval_request_renewal_audit on approval_request;
  drop function approval_request_renewal_audit();
  alter table approval_request drop column renews_id, drop column renew_reason;
  drop function approval_request_renewal(approval_request);
end $$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    run_script(bind, UPGRADE)
    run_script(bind, _guard(renewal=True))


def downgrade() -> None:
    # The guard goes back to the earlier one only when nothing of this migration is kept.
    bind = op.get_bind()
    run_script(bind, DOWNGRADE)
    kept = bind.exec_driver_sql("select to_regclass('public.approval_nudge') is not null").scalar()
    if not kept:
        run_script(bind, _guard(renewal=False))

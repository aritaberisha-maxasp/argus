"""The approver decides on the approval page (issue #38, ADR 0010).

Covers AP-02, AP-03, AP-04, AP-07, AP-08, AP-10, PR-04, NF-01, NF-03 and NF-04. The approver
opens the link from the approval request email (#191), signs in and approves the invoice, or
rejects it with a comment. Before deciding they may change the proposed G/L account or tax key,
with a reason (Arita on #38, 23 Nov).

This migration adds:

- ``approval_decision``: one row per decision, never changed or removed. ARGUS names the case,
  the request the person saw, approved or rejected, and the comment. The database fills in the
  rest: the person's object ID, their name and sign in address as they were at that moment
  (ADR 0008), whether they decided as approver or as deputy and for whom (copied from the
  request), the time and the transaction. It refuses a decision
  - from anyone but a person (a ``user:`` actor),
  - from anyone but the person the newest approval request of the case went to, also finance
    and admin (ADR 0010),
  - on a request that is no longer the newest one, or on a case that does not wait for approval,
  - when the case moved to another vendor account since the request (AP-02: that vendor's
    approver was never asked),
  - an approval without the account proposal the page showed, or with one that is no longer
    the current proposal (AP-08: nobody approves an account they did not see),
  - without the person's name, or a rejection without a comment of at least three characters
    other than spaces (``correction_trim``, as for corrections),
  - a second time for the same request.
  A decision only commits together with the move it belongs to, so no decision can stand on its
  own.
- A rule on the case: Awaiting approval moves to Approved only with an ``approved`` decision of
  the same transaction and person (AP-10: no job can approve, so a recurring invoice never
  reaches Approved on its own), and only while the case has a current G/L account and tax key
  proposal for its vendor account (PR-04, Arita on PR 190). Awaiting approval moves to Closed
  only as ``rejected``, with a ``rejected`` decision of the same transaction (AP-07). A waiting
  invoice that went to the exception queue is closed from there only by a person, never by a
  job.
- The account proposal rule of 0019 is widened on purpose (ADR 0010): while a case waits for
  approval, the person the newest request went to may enter a new ``person`` proposal, with a
  reason, as long as the case still has the vendor account of that request. Nobody else may,
  and a job never may. ``account_proposed`` now says when a proposal
  was made at approval, so the trail can show "changed by".
- The audit event ``approval_decided``, written by the database only. It holds the decision,
  the person's object ID, name and sign in address, the request, and a SHA 256 of the comment.
  The comment itself stays in ``approval_decision``, out of the audit log and the app log
  (ADR 0005, NF-03).

A downgrade never loses a decision: with decisions in the database it keeps the table and all
its rules, as 0019 and 0020 do.

Revision ID: 0021
Revises: 0018
Create Date: 2026-11-25
"""

from alembic import op

from argus.db import run_script

revision = "0021"
down_revision = "0018"
branch_labels = None
depends_on = None

UPGRADE = r"""
create table if not exists approval_decision (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  request_id bigint not null references approval_request(id),
  -- The G/L account and tax key proposal the page showed. Required to approve, and it must be
  -- the case's current proposal, so a proposal changed in the meantime is never approved unseen.
  proposal_id bigint references account_proposal(id),
  decision text not null check (decision in ('approved', 'rejected')),
  comment text check (length(correction_trim(comment)) between 3 and 500),
  decided_by text not null,               -- user:<Entra object ID>, set by the database
  decided_by_name text not null
    check (length(correction_trim(decided_by_name)) between 1 and 200),
  decided_by_address text
    check (length(correction_trim(decided_by_address)) between 1 and 320),
  decided_as text not null check (decided_as in ('approver', 'deputy')),
  on_behalf_of text,                      -- the approver who was away, when a deputy decided
  on_behalf_of_name text,
  decided_at timestamptz not null,        -- set by the database
  txid bigint not null,                   -- the transaction, set by the database
  check (decision = 'approved' or comment is not null),
  check (decision = 'rejected' or proposal_id is not null),
  check ((decided_as = 'deputy') = (on_behalf_of is not null))
);
-- One decision per request: a second click, a second tab or an old link is refused.
create unique index if not exists approval_decision_once on approval_decision (request_id);
create index if not exists approval_decision_case on approval_decision (case_id, id);

create or replace function approval_decision_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  r record;
begin
  if a[1] <> 'user' then
    raise exception 'only a person can decide on case %', new.case_id;
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
  if not found then
    raise exception 'case % has no approval request', new.case_id;
  end if;
  if r.id is distinct from new.request_id then
    raise exception 'approval request % is not the newest one of case %',
      new.request_id, new.case_id;
  end if;
  if lower(a[2]) is distinct from lower(r.recipient_id) then
    raise exception 'only the person approval request % went to can decide on case %',
      r.id, new.case_id;
  end if;
  -- The request went to the approver of the vendor the case had then (AP-02). Another vendor
  -- needs its own approver, so a new request.
  if c.partner_account is distinct from r.vendor_account then
    raise exception 'case % moved to another vendor account since approval request %; '
      'it needs a new request', new.case_id, r.id;
  end if;
  if new.proposal_id is not null and new.proposal_id is distinct from (
       select p.id from account_proposal p
       where p.case_id = new.case_id and p.vendor_account = c.partner_account
       order by p.id desc limit 1) then
    raise exception 'the G/L account or tax key of case % changed since the page showed it',
      new.case_id;
  end if;
  if new.decision = 'approved' and new.proposal_id is null then
    raise exception 'approving case % needs the G/L account and tax key proposal it approves',
      new.case_id;
  end if;
  if new.decision = 'rejected' and coalesce(length(correction_trim(new.comment)), 0) < 3 then
    raise exception 'rejecting case % needs a comment of at least 3 characters', new.case_id;
  end if;
  new.decided_by := a[1] || ':' || a[2];
  new.decided_by_name :=
    left(nullif(correction_trim(current_setting('argus.actor_name', true)), ''), 200);
  new.decided_by_address :=
    left(nullif(correction_trim(current_setting('argus.actor_address', true)), ''), 320);
  if new.decided_by_name is null then
    raise exception 'a decision on case % needs the name of the person', new.case_id;
  end if;
  new.decided_as := r.recipient_role;
  new.on_behalf_of := r.on_behalf_of;
  new.on_behalf_of_name := r.on_behalf_of_name;
  new.decided_at := clock_timestamp();
  new.txid := txid_current();
  return new;
end $$;
create or replace trigger approval_decision_guard before insert on approval_decision
  for each row execute function approval_decision_guard();

create or replace function approval_decision_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'a decision is kept as it was made and cannot change'; end $$;
create or replace trigger approval_decision_no_change before update or delete
  on approval_decision for each row execute function approval_decision_fixed();
create or replace trigger approval_decision_no_truncate before truncate on approval_decision
  execute function approval_decision_fixed();

-- Runs as the owner, like the other audit triggers. The comment stays out (ADR 0005, NF-03);
-- its hash shows a later change to it.
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
            'comment_sha256',
              encode(sha256(convert_to(new.comment, 'UTF8')), 'hex'))));
  return null;
end $$;
create or replace trigger approval_decision_audit after insert on approval_decision
  for each row execute function approval_decision_audit();

-- Checked at commit: a decision only exists together with the move it belongs to.
create or replace function approval_decision_with_move() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
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
drop trigger if exists approval_decision_with_move on approval_decision;
create constraint trigger approval_decision_with_move after insert on approval_decision
  deferrable initially deferred for each row execute function approval_decision_with_move();

-- Leaving Awaiting approval for Approved or Closed needs the person's decision (AP-07, AP-10)
-- and, for Approved, a current account proposal (PR-04). Named to run after case_guard
-- (triggers of one event run in name order), so a move that is not allowed at all, or has no
-- actor, is refused with case_guard's own words first.
create or replace function case_settled_by_decision() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[];
  wanted text;
  r record;
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
  select d.proposal_id, q.vendor_account into r
  from approval_decision d join approval_request q on q.id = d.request_id
  where d.case_id = old.id and d.decision = wanted and d.txid = txid_current()
    and d.decided_by = a[1] || ':' || a[2]
    and d.request_id = (select max(id) from approval_request where case_id = old.id);
  if not found then
    raise exception 'case % can only be % by the person asked, on the approval page',
      old.id, wanted;
  end if;
  if new.status = 'approved' then
    if new.partner_account is distinct from r.vendor_account then
      raise exception 'case % moved to another vendor account since its approval request',
        old.id;
    end if;
    -- The proposal the person approved must still be the current one (AP-08).
    if r.proposal_id is null or r.proposal_id is distinct from (
         select p.id from account_proposal p
         where p.case_id = old.id and p.vendor_account = new.partner_account
         order by p.id desc limit 1) then
      raise exception 'case % can only be approved with the G/L account and tax key proposal '
        'the person saw', old.id;
    end if;
  end if;
  return new;
end $$;
create or replace trigger case_settled_by_decision before update on invoice_case
  for each row execute function case_settled_by_decision();

-- 0019's guard, widened on purpose (ADR 0010): while the case waits for approval, the person
-- the newest request went to may enter a new account with a reason, and nobody else.
create or replace function account_proposal_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  c record;
  x record;
  asked record;
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
  if c.status = 'awaiting_approval' then
    select recipient_id, vendor_account into asked
    from approval_request where case_id = new.case_id order by id desc limit 1;
    if new.source <> 'person' or new.exception_id is not null
       or lower(a[2]) is distinct from lower(asked.recipient_id) then
      raise exception
        'only the person asked to approve case % can change its account while it waits',
        new.case_id;
    end if;
    if c.partner_account is distinct from asked.vendor_account then
      raise exception 'case % moved to another vendor account since its approval request; '
        'it needs a new request', new.case_id;
    end if;
  elsif c.status not in ('received', 'exception') then
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

-- As in 0019, plus whether the proposal was made at approval.
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
            'reason', new.reason,
            'at_approval', case when (select status from invoice_case where id = new.case_id)
                                     = 'awaiting_approval' then true end)));
  return null;
end $$;

-- Like the other database events: only the trigger above writes this one.
create or replace function audit_approval_decision_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type = 'approval_decided' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create or replace trigger audit_approval_decision_events before insert on audit_event
  for each row execute function audit_approval_decision_events();

grant select on approval_decision to argus_app;
grant insert (case_id, request_id, proposal_id, decision, comment)
  on approval_decision to argus_app;
"""

# 0019's versions, put back on a downgrade without decisions.
OLD_PROPOSAL_RULES = r"""
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
"""

DOWNGRADE = r"""
-- Decisions are part of the audit trail (NF-01). So a downgrade with decisions in the database
-- keeps the table and its rules, as 0019 and 0020 do: code from before 0021 never touches them,
-- and the upgrade picks them up again (every statement above can run twice). The account
-- proposal rules go back to 0019's either way, since code from before 0021 never changes an
-- account at approval.
do $$ begin
  if exists (select 1 from approval_decision) then
    raise notice 'approval decisions exist; their table and rules stay (NF-01)';
    return;
  end if;
  drop trigger audit_approval_decision_events on audit_event;
  drop function audit_approval_decision_events();
  drop trigger case_settled_by_decision on invoice_case;
  drop function case_settled_by_decision();
  drop table approval_decision;
  drop function approval_decision_with_move();
  drop function approval_decision_audit();
  drop function approval_decision_fixed();
  drop function approval_decision_guard();
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)
    run_script(op.get_bind(), OLD_PROPOSAL_RULES)

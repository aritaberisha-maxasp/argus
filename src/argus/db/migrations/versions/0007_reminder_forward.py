"""One forward per payment reminder, sent through a saved draft (ADR 0003, issue #27).

Covers IN-05, PY-06 and NF-01. ARGUS forwards each payment reminder or dunning letter to
Benjamin with the original attachment. Graph first saves the forward as a draft; its ID is
recorded here before the draft is sent, and the email can have only one forward record. So a
crash between draft and send can never send two forwards: a draft whose ID was not recorded is
never sent, and a recorded draft can be sent only once.

The database writes the audit events itself (``reminder_forward_drafted``,
``reminder_forwarded``) and the application cannot write them, like the email step events of
0004. The events hold IDs only, never the recipient, sender, subject or content (NF-03).
Records cannot be deleted.

Follows 0006, the filing of #26 (PR 92), which this story builds on: the email moves to the
step ``routed`` with the route ``forward_reminder``. Migration 0005 is kept for #91; whichever
of the two lands second follows the other so the chain stays one line.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28
"""

from alembic import op

from argus.db import run_script

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = r"""
-- A reminder whose saved forward is gone or was changed goes to the exception queue (EX-01).
alter type exception_reason add value 'forward_failed';

create table reminder_forward (
  id bigint generated always as identity primary key,
  email_id bigint not null unique,  -- one forward per email; the guard checks it exists
  draft_message_id text not null unique,  -- immutable Graph ID of the saved forward
  step text not null default 'drafted' check (step in ('drafted','sent')),
  drafted_at timestamptz not null default clock_timestamp(),  -- set by the database
  sent_at timestamptz,                                        -- set by the database
  matched_case_ids bigint[],     -- open invoice cases the reminder names, once sent (PY-06)
  unclear_numbers integer,       -- numbers that more than one open case carries, not marked
  -- 'sent' when ARGUS sent the draft, 'found_sent' when it found the draft already sent: after
  -- a crash past its own send, or because a person sent it from Outlook (compliance, PR 93).
  -- 'by_person' when the draft was gone and a person resolved the exception, having forwarded
  -- the reminder by hand if it was needed (review of PR 93).
  send_outcome text check (send_outcome in ('sent','found_sent','by_person')),
  check ((step = 'sent') = (sent_at is not null)),
  check ((step = 'sent') = (send_outcome is not null)),
  check ((step = 'sent') = (matched_case_ids is not null and unclear_numbers is not null)),
  check (unclear_numbers is null or unclear_numbers >= 0)
);

-- A forward starts as a draft of a sorted email, and the draft can only be sent once.
create function reminder_forward_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.step <> 'drafted' or new.matched_case_ids is not null
       or new.unclear_numbers is not null or new.send_outcome is not null then
      raise exception 'a reminder forward starts as a draft';
    end if;
    if not exists (select 1 from mailbox_email where id = new.email_id and step = 'classified') then
      raise exception 'email % is not sorted yet, or already routed', new.email_id;
    end if;
    new.drafted_at := clock_timestamp();
    new.sent_at := null;
    return new;
  end if;
  if (new.email_id, new.draft_message_id, new.drafted_at)
     is distinct from (old.email_id, old.draft_message_id, old.drafted_at) then
    raise exception 'the draft of reminder forward % cannot change', old.id;
  end if;
  if old.step = 'sent' then
    raise exception 'reminder forward % is already sent', old.id;
  end if;
  new.sent_at := case when new.step = 'sent' then clock_timestamp() end;
  return new;
end $$;
create trigger reminder_forward_guard before insert or update on reminder_forward
  for each row execute function reminder_forward_guard();

-- One audit event per step, in the same transaction. IDs only (NF-03).
create function reminder_forward_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (email_id, event_type, details)
    values (new.email_id, 'reminder_forward_drafted',
            jsonb_build_object('draft_message_id', new.draft_message_id));
  elsif new.step is distinct from old.step then
    insert into audit_event (email_id, event_type, details)
    values (new.email_id, 'reminder_forwarded',
            jsonb_build_object('draft_message_id', new.draft_message_id,
                               'matched_case_ids', to_jsonb(new.matched_case_ids),
                               'unclear_numbers', new.unclear_numbers,
                               'send_outcome', new.send_outcome));
  end if;
  return null;
end $$;
create trigger reminder_forward_audit after insert or update on reminder_forward
  for each row execute function reminder_forward_audit();

create function reminder_forward_no_removal() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  raise exception 'reminder forwards cannot be deleted, their audit trail points at them';
end $$;
create trigger reminder_forward_no_delete before delete on reminder_forward
  for each row execute function reminder_forward_no_removal();
create trigger reminder_forward_no_truncate before truncate on reminder_forward
  execute function reminder_forward_no_removal();

-- Only the triggers above write the forward events, so the log can never say a reminder was
-- forwarded when it was not. A trigger of its own, so 0005 and 0006 can change
-- audit_document_events without touching this rule.
create function audit_reminder_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type like 'reminder\_%' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_reminder_events before insert on audit_event
  for each row execute function audit_reminder_events();

grant select on reminder_forward to argus_app;
grant insert (email_id, draft_message_id) on reminder_forward to argus_app;
grant update (step, matched_case_ids, unclear_numbers, send_outcome) on reminder_forward
  to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from reminder_forward) then
    raise exception 'reminder forwards exist; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
drop trigger audit_reminder_events on audit_event;
drop function audit_reminder_events();
drop table reminder_forward;
drop function reminder_forward_no_removal();
drop function reminder_forward_audit();
drop function reminder_forward_guard();
-- Postgres cannot drop one value of an enum, so the type is built again without it.
do $$ begin
  if exists (select 1 from exception_item where reason::text = 'forward_failed') then
    raise exception 'forward exceptions exist; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
alter type exception_reason rename to exception_reason_0007;
create type exception_reason as enum (
  'uncertain_classification','unreadable_document','unknown_vendor','unknown_customer',
  'vendor_without_approver','duplicate','iban_mismatch','invoice_check_failed','booking_error',
  'unmatched_bank_transaction','bank_balance_difference',
  'number_sequence_gap','payment_failed','customer_query');
alter table exception_item
  alter column reason type exception_reason using reason::text::exception_reason;
alter table exception_exit
  alter column reason type exception_reason using reason::text::exception_reason;
drop type exception_reason_0007;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

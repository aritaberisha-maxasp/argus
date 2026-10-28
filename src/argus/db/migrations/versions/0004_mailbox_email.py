"""One record per email in the accounts mailbox, and the steps it has gone through (ADR 0003).

Covers IN-01 and NF-01. The record is keyed by the immutable Graph message ID with a unique
constraint, so an email can never get two records. The database checks the order of the steps
(seen, stored, classified, routed, filed): no step can be skipped, so nothing can happen to an
email before it is stored. Each step writes its audit event in the same transaction, and the
application cannot write those events itself. Records cannot be deleted, not even by the owner.

The fingerprints of the raw email and each attachment are recorded by the document store
(stored_document, migration 0003) with their own audit events. This table keeps the fingerprint
of the raw email as well, so the record shows which copy the later steps work on.

Follows 0003, the stored documents of #20.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from alembic import op

from argus.db import run_script

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = r"""
create table mailbox_email (
  id bigint generated always as identity primary key,
  graph_message_id text not null unique,  -- immutable ID (Prefer: IdType="ImmutableId")
  internet_message_id text,               -- to flag an identical email delivered twice
  received_at timestamptz,                -- as Exchange reports it
  step text not null default 'seen'
    check (step in ('seen','stored','classified','routed','filed')),
  raw_sha256 text check (raw_sha256 ~ '^[0-9a-f]{64}$'),
  raw_location text,
  seen_at timestamptz not null default clock_timestamp(),  -- set by the database
  stored_at timestamptz,                                   -- set by the database
  check ((raw_sha256 is null) = (raw_location is null)),
  check ((step = 'seen') = (raw_sha256 is null)),
  check ((step = 'seen') = (stored_at is null))
);

-- Steps only go forward, one at a time. The fingerprint is set once, when the email is stored.
create function mailbox_email_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  steps text[] := array['seen','stored','classified','routed','filed'];
begin
  if tg_op = 'INSERT' then
    if new.step <> 'seen' or new.raw_sha256 is not null or new.raw_location is not null then
      raise exception 'a new email record starts at seen, without a stored copy';
    end if;
    new.seen_at := clock_timestamp();
    new.stored_at := null;
    return new;
  end if;
  if (new.graph_message_id, new.internet_message_id, new.received_at, new.seen_at)
     is distinct from (old.graph_message_id, old.internet_message_id, old.received_at, old.seen_at)
  then
    raise exception 'what Exchange reported for email % cannot change', old.id;
  end if;
  if old.raw_sha256 is not null
     and (new.raw_sha256, new.raw_location) is distinct from (old.raw_sha256, old.raw_location) then
    raise exception 'the stored copy of email % is already recorded', old.id;
  end if;
  new.stored_at := old.stored_at;
  if new.step is distinct from old.step then
    if array_position(steps, new.step) is distinct from array_position(steps, old.step) + 1 then
      raise exception 'email % cannot go from % to %', old.id, old.step, new.step;
    end if;
    if new.step = 'stored' then
      new.stored_at := clock_timestamp();
    end if;
  end if;
  return new;
end $$;
create trigger mailbox_email_guard before insert or update on mailbox_email
  for each row execute function mailbox_email_guard();

-- One audit event per step, in the same transaction. Runs as the owner. No sender, subject or
-- content: the audit log holds no personal data (NF-03).
create function mailbox_email_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_seen', jsonb_build_object('graph_message_id', new.graph_message_id));
  elsif new.step is distinct from old.step then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_' || new.step,
            jsonb_strip_nulls(jsonb_build_object('raw_sha256', new.raw_sha256,
                                                 'raw_location', new.raw_location)));
  end if;
  return null;
end $$;
create trigger mailbox_email_audit after insert or update on mailbox_email
  for each row execute function mailbox_email_audit();

-- A record stays as long as its audit events point at it (NF-01). The step moves forward by
-- update, so only delete and truncate are refused, for the owner too.
create function mailbox_email_no_removal() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'email records cannot be deleted, their audit trail points at them'; end $$;
create trigger mailbox_email_no_delete before delete on mailbox_email
  for each row execute function mailbox_email_no_removal();
create trigger mailbox_email_no_truncate before truncate on mailbox_email
  execute function mailbox_email_no_removal();

-- Like the case events (0001) and the document events (0003): only the triggers above write the
-- email step events, so the log can never say an email was stored when it was not. Replaces the
-- function from 0003, keeping its rule.
create or replace function audit_document_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and (new.event_type in ('document_stored','document_linked')
                                 or new.event_type like 'email\_%') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;

grant select on mailbox_email to argus_app;
grant insert (graph_message_id, internet_message_id, received_at) on mailbox_email to argus_app;
grant update (step, raw_sha256, raw_location) on mailbox_email to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from mailbox_email) then
    raise exception 'email records exist; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
create or replace function audit_document_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type in ('document_stored','document_linked') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
drop table mailbox_email;
drop function mailbox_email_no_removal();
drop function mailbox_email_audit();
drop function mailbox_email_guard();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

"""Where each email is filed, and the removal of Other emails after 30 days (IN-06, ADR 0003).

Adds to the email records of 0004:

- ``route``: where the sorting sent the email, set when it moves to the step ``routed``.
- ``folder``: the ARGUS folder it was moved to, set when it moves to the step ``filed``.
- ``filed_at``: when it was moved there, set by the database.
- ``deleted_at``: when ARGUS recorded it as gone from the mailbox, set by the database.
- ``deletion_outcome``: ``deleted`` when ARGUS deleted it, ``already_gone`` when ARGUS found it
  already gone (a person deleted it, or ARGUS stopped right after its own delete). The log must
  never claim ARGUS deleted an email it did not delete (compliance review of PR 92).

The database holds the rules that keep an invoice from being lost (IN-06 note): only an email
routed to the deletion folder can be filed in ARGUS/Removed, only an email in ARGUS/Removed can be
deleted, and only 30 days or more after it was moved there, by the database clock. Once set,
none of these can change.

Moving and deleting each write an audit event from the database (``email_moved`` and
``email_deleted``), so the application still cannot write any ``email_%`` event (0004). These
come from their own trigger and leave the step events of 0004 as they are.

Migration 0005 is kept for #91 (writing the sorting decision). Until it is on main this one
follows 0004; whichever of the two lands second follows the other.

Revision ID: 0006
Revises: 0004
Create Date: 2026-10-28
"""

from alembic import op

from argus.db import run_script

revision = "0006"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter table mailbox_email
  add column route text check (route in ('invoice_processing','outgoing_booking',
                                         'forward_reminder','deletion_folder','manual_review')),
  add column folder text check (folder ~ '^ARGUS/[^/]+$'),
  add column filed_at timestamptz,   -- set by the database
  add column deleted_at timestamptz, -- set by the database
  add column deletion_outcome text check (deletion_outcome in ('deleted','already_gone')),
  add check ((route is null) = (step in ('seen','stored','classified'))),
  add check ((folder is null) = (step <> 'filed')),
  add check ((folder is null) = (filed_at is null)),
  add check ((deleted_at is null) = (deletion_outcome is null)),
  -- Only Other goes to ARGUS/Removed, and everything routed to the deletion folder goes there.
  add check (folder is null or (folder = 'ARGUS/Removed') = (route = 'deletion_folder')),
  add check (deleted_at is null or (folder = 'ARGUS/Removed'
                                    and deleted_at >= filed_at + interval '30 days'));

-- The route is set with the step routed, the folder with the step filed, and neither changes
-- afterwards. The clock values come from the database only.
create function mailbox_email_filing_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if (new.route, new.folder, new.filed_at, new.deleted_at, new.deletion_outcome)
       is distinct from (null::text, null::text, null::timestamptz, null::timestamptz, null::text)
    then
      raise exception 'a new email record is not routed, filed or deleted';
    end if;
    return new;
  end if;
  if new.route is distinct from old.route
     and not (old.route is null and old.step = 'classified' and new.step = 'routed') then
    raise exception 'the route of email % is set once, when it is routed', old.id;
  end if;
  if new.folder is distinct from old.folder
     and not (old.folder is null and old.step = 'routed' and new.step = 'filed') then
    raise exception 'the folder of email % is set once, when it is filed', old.id;
  end if;
  if old.folder is null and new.folder is not null then
    new.filed_at := clock_timestamp();
  else
    new.filed_at := old.filed_at;
  end if;
  if old.deleted_at is not null then
    if (new.deleted_at, new.deletion_outcome) is distinct from
       (old.deleted_at, old.deletion_outcome) then
      raise exception 'email % is already deleted from the mailbox', old.id;
    end if;
  elsif new.deleted_at is not null then
    if old.folder is distinct from 'ARGUS/Removed' then
      raise exception 'email % is not in ARGUS/Removed and cannot be deleted', old.id;
    end if;
    if clock_timestamp() < old.filed_at + interval '30 days' then
      raise exception 'email % went to ARGUS/Removed less than 30 days ago', old.id;
    end if;
    if new.deletion_outcome is null then
      raise exception 'the deletion of email % needs its outcome', old.id;
    end if;
    new.deleted_at := clock_timestamp();
  elsif new.deletion_outcome is not null then
    raise exception 'the deletion outcome of email % is set with its deletion', old.id;
  end if;
  return new;
end $$;
create trigger mailbox_email_filing_guard before insert or update on mailbox_email
  for each row execute function mailbox_email_filing_guard();

-- One audit event for each move out of the Inbox and each deletion, in the same transaction.
-- Runs as the owner, like the step events of 0004. No sender, subject or content (NF-03).
create function mailbox_email_filing_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if old.folder is null and new.folder is not null then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_moved',
            jsonb_build_object('to_folder', new.folder, 'route', new.route));
  end if;
  if old.deleted_at is null and new.deleted_at is not null then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_deleted',
            jsonb_build_object('outcome', new.deletion_outcome,
                               'folder', new.folder, 'filed_at', new.filed_at,
                               'days_in_folder',
                               extract(day from new.deleted_at - new.filed_at)::int));
  end if;
  return null;
end $$;
create trigger mailbox_email_filing_audit after update on mailbox_email
  for each row execute function mailbox_email_filing_audit();

grant update (route, folder, deleted_at, deletion_outcome) on mailbox_email to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from mailbox_email where route is not null) then
    raise exception 'emails are routed or filed; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
revoke update (route, folder, deleted_at, deletion_outcome) on mailbox_email from argus_app;
drop trigger mailbox_email_filing_audit on mailbox_email;
drop trigger mailbox_email_filing_guard on mailbox_email;
drop function mailbox_email_filing_audit();
drop function mailbox_email_filing_guard();
alter table mailbox_email
  drop column deletion_outcome, drop column deleted_at, drop column filed_at,
  drop column folder, drop column route;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

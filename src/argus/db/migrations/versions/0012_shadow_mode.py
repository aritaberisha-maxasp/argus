"""Shadow mode: ARGUS proposes a route for every email but acts on none (#72).

Covers IN-04, IN-06, IN-09 and NF-01. Arita agreed on #5 that ARGUS first runs two weeks on
real mail without acting, while a person still sorts the Inbox. The thresholds are then set
from what it proposed.

``intake_mode`` holds one row: ``shadow`` or ``live``. It starts in ``shadow``. The database
holds these rules:

- Only a person can change the mode, never a job: the change needs a ``user:`` actor. The app
  lets only the admin role (Arita) do it (ADR 0008). The rows cannot be added or removed.
- Every change names the person as they were at that moment (name, and sign in address when
  Entra gives one) and a reason, next to the object ID that the actor carries. They come from
  the same transaction (``SET LOCAL argus.switch_name``, ``argus.switch_address``,
  ``argus.switch_reason``), so a switch never reuses an earlier one's. ADR 0008 asks
  for that on every decision a person makes, because the object ID names nobody once the
  Entra account is gone. The row keeps the last switch; the audit log keeps them all.
- Every change is written into the audit log as ``intake_mode_changed`` with the mode before
  and after, the name, the address and the reason. Only the trigger writes it; the app cannot
  write that event itself.
- In shadow mode no email can reach the step ``classified``. That step is where cases and
  exceptions are opened (#30, #71), so ARGUS starts nothing for an email while it only proposes.
  The steps after it are not refused here: they are recorded after Graph did something, and the
  log must show what really happened. The app checks the mode before each move, forward and
  delete instead (``argus.intake.shadow``).

On ``mailbox_email`` the shadow run adds:

- ``proposal``: the decision the sorting would have taken, exactly like ``classification``
  (0005), set once, only at step ``stored`` and only in shadow mode. The database writes it
  into the audit log as ``email_proposed``.
- ``person_folder`` and ``person_category``: where the email went after a person took it out
  of the Inbox, and what that folder means. Set once, only for an email with a proposal. The
  database writes ``email_sorted_by_person``. A folder that is not in the folder list has no
  category.

Both new events start with ``email_``, which 0004 already refuses from the app.

The number 0012 was handed out for this story: 0010 is #31 (PR 109) and 0011 is #90. This
branch is built on main at 0009, so it follows 0009 for now and is moved behind whichever of
0010 and 0011 lands last before it merges.

Revision ID: 0012
Revises: 0009
Create Date: 2026-11-07
"""

from alembic import op

from argus.db import run_script

revision = "0012"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = r"""
create table intake_mode (
  id boolean primary key default true check (id),
  mode text not null default 'shadow' check (mode in ('shadow','live')),
  changed_at timestamptz not null default clock_timestamp(),
  changed_by_name text check (length(btrim(changed_by_name)) between 1 and 200),
  changed_by_address text check (length(btrim(changed_by_address)) between 3 and 320),
  reason text check (length(btrim(reason)) between 3 and 500)
);
insert into intake_mode default values;

create function intake_mode_now() returns text language sql stable
set search_path = pg_catalog, public, pg_temp as $$
  select coalesce((select mode from intake_mode), 'shadow')
$$;

create function intake_mode_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if (current_actor())[1] <> 'user' then
    raise exception 'only a person can switch between shadow and live mode';
  end if;
  -- Who and why come from this transaction only (SET LOCAL), so a switch can never reuse the
  -- name or reason of an earlier one.
  new.changed_by_name := nullif(btrim(current_setting('argus.switch_name', true)), '');
  new.changed_by_address := nullif(btrim(current_setting('argus.switch_address', true)), '');
  new.reason := nullif(btrim(current_setting('argus.switch_reason', true)), '');
  if new.changed_by_name is null or new.reason is null then
    raise exception 'a switch between shadow and live mode needs the person''s name and a reason';
  end if;
  new.id := old.id;
  new.changed_at := clock_timestamp();
  return new;
end $$;
create trigger intake_mode_guard before update on intake_mode
  for each row when (new.mode is distinct from old.mode)
  execute function intake_mode_guard();

create function intake_mode_audit() returns trigger language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (event_type, details)
  values ('intake_mode_changed',
          jsonb_build_object('from', old.mode, 'to', new.mode, 'name', new.changed_by_name,
                             'reason', new.reason)
          || jsonb_strip_nulls(jsonb_build_object('sign_in_address', new.changed_by_address)));
  return null;
end $$;
create trigger intake_mode_audit after update on intake_mode
  for each row when (new.mode is distinct from old.mode)
  execute function intake_mode_audit();

create function intake_mode_fixed() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'intake_mode has exactly one row; change its mode instead'; end $$;
create trigger intake_mode_no_insert before insert or delete on intake_mode
  for each row execute function intake_mode_fixed();
create trigger intake_mode_no_truncate before truncate on intake_mode
  execute function intake_mode_fixed();

-- Like the other database events: only the trigger above writes this one.
create function audit_intake_mode_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type = 'intake_mode_changed' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_intake_mode_events before insert on audit_event
  for each row execute function audit_intake_mode_events();

alter table mailbox_email
  add column proposal jsonb
    check (proposal is null or (jsonb_typeof(proposal) = 'object'
           and proposal->>'route' in ('invoice_processing','outgoing_booking',
                                      'forward_reminder','deletion_folder','manual_review'))),
  add column proposed_at timestamptz,
  add column person_folder text check (length(person_folder) between 1 and 200),
  add column person_category text
    check (person_category in ('incoming_invoice','outgoing_invoice','payment_reminder','other')),
  add column person_seen_at timestamptz,
  add check ((proposal is null) = (proposed_at is null)),
  add check ((person_folder is null) = (person_seen_at is null)),
  add check (person_category is null or person_folder is not null),
  add check (person_folder is null or proposal is not null);

create function mailbox_email_shadow_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if num_nonnulls(new.proposal, new.person_folder, new.person_category) > 0 then
      raise exception 'a new email record has no proposal yet';
    end if;
    new.proposed_at := null;
    new.person_seen_at := null;
    return new;
  end if;
  if new.step = 'classified' and old.step = 'stored' and intake_mode_now() <> 'live' then
    raise exception 'email % is not classified: ARGUS runs in shadow mode', old.id;
  end if;
  if new.proposal is distinct from old.proposal then
    if old.proposal is not null then
      raise exception 'the proposal for email % is set once', old.id;
    end if;
    if old.step <> 'stored' or new.step <> 'stored' then
      raise exception 'only an email at step stored gets a proposal';
    end if;
    if intake_mode_now() <> 'shadow' then
      raise exception 'ARGUS proposes only in shadow mode';
    end if;
    new.proposed_at := clock_timestamp();
  else
    new.proposed_at := old.proposed_at;
  end if;
  if (new.person_folder, new.person_category)
     is distinct from (old.person_folder, old.person_category) then
    if old.person_folder is not null then
      raise exception 'what the person did with email % is recorded once', old.id;
    end if;
    new.person_seen_at := clock_timestamp();
  else
    new.person_seen_at := old.person_seen_at;
  end if;
  return new;
end $$;
create trigger mailbox_email_shadow_guard before insert or update on mailbox_email
  for each row execute function mailbox_email_shadow_guard();

-- Runs as the owner. The proposal holds what 0005's decision holds: no sender, subject or
-- content beyond the masked snippets (NF-03). The folder is one from Max ASP's folder list.
create function mailbox_email_shadow_audit() returns trigger language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.proposal is distinct from old.proposal then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_proposed', new.proposal);
  end if;
  if new.person_folder is distinct from old.person_folder then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_sorted_by_person',
            jsonb_build_object('folder', new.person_folder, 'category', new.person_category));
  end if;
  return null;
end $$;
create trigger mailbox_email_shadow_audit after update on mailbox_email
  for each row execute function mailbox_email_shadow_audit();

grant select on intake_mode to argus_app;
grant update (mode) on intake_mode to argus_app;
grant update (proposal, person_folder, person_category) on mailbox_email to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from mailbox_email where proposal is not null)
     or exists (select 1 from audit_event where event_type = 'intake_mode_changed') then
    raise exception 'shadow mode was used; its audit trail is kept for ten years (NF-01)';
  end if;
end $$;
revoke update (proposal, person_folder, person_category) on mailbox_email from argus_app;
drop trigger mailbox_email_shadow_audit on mailbox_email;
drop function mailbox_email_shadow_audit();
drop trigger mailbox_email_shadow_guard on mailbox_email;
drop function mailbox_email_shadow_guard();
alter table mailbox_email
  drop column person_seen_at,
  drop column person_category,
  drop column person_folder,
  drop column proposed_at,
  drop column proposal;
drop trigger audit_intake_mode_events on audit_event;
drop function audit_intake_mode_events();
drop table intake_mode;
drop function intake_mode_fixed();
drop function intake_mode_audit();
drop function intake_mode_guard();
drop function intake_mode_now();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

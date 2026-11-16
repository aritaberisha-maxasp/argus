"""The ARGUS approver table: who approves each vendor, the deputy, and direct debit (#34).

Covers AP-01, AP-06, PY-13, VM-02 and NF-01. The approver per vendor is kept in ARGUS, not in
DATEV. Arita maintains it by hand.

Two tables:

- ``approver``: one row per person who can approve, by Entra object ID (ADR 0008), with the name
  and email address ARGUS shows and writes to. ``absent`` says whether the person is away; the
  database sets ``absent_since``. While an approver is away, the deputy of each of their vendors
  decides in their place (AP-06).
- ``vendor_approver``: one row per DATEV vendor account. The account is the primary key, so a
  vendor has exactly one approver (AP-01). The deputy is optional and is never the approver
  as well. ``direct_debit`` marks a vendor paid by SEPA direct debit, whose invoices never go
  into a payment run (PY-13). A vendor with no row has no approver, and its invoices go to the
  exception queue (VM-02).

The database holds these rules:

- Only a person can change either table, never a job: the change needs a ``user:`` actor. The app
  lets only the admin role do it. A job could otherwise send invoices to another approver.
- Every change names the person as they were at that moment (``SET LOCAL
  argus.changed_by_name`` and, when Entra gives one, ``argus.changed_by_address``), next to the
  object ID that the actor carries (ADR 0008).
- Every change is written into the audit log by the database: ``approver_added``,
  ``approver_changed``, ``approver_removed``, ``approver_absence_changed``,
  ``vendor_approver_set`` and ``vendor_approver_removed``. Old and new values go in, with the
  approver's name but never an email address. The app cannot write these events itself.
- A person who is still approver or deputy of a vendor cannot be removed.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-16
"""

from alembic import op

from argus.db import run_script

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UPGRADE = r"""
create table approver (
  object_id text primary key
    check (object_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
  name text not null check (length(correction_trim(name)) between 1 and 200),
  email text not null
    check (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' and length(email) <= 320),
  absent boolean not null default false,
  absent_since timestamptz,               -- set by the database
  changed_at timestamptz not null,        -- set by the database
  check (absent = (absent_since is not null))
);

create table vendor_approver (
  vendor_account text primary key check (vendor_account ~ '^[1-9][0-9]{4,8}$'),
  approver_id text not null references approver(object_id),
  deputy_id text references approver(object_id),
  direct_debit boolean not null default false,
  changed_at timestamptz not null,        -- set by the database
  check (deputy_id is distinct from approver_id)
);
create index vendor_approver_approver on vendor_approver (approver_id);
create index vendor_approver_deputy on vendor_approver (deputy_id) where deputy_id is not null;

-- Who makes this change: a person, named in this transaction. Returns the details every audit
-- event of the approver table carries.
create function approver_table_changed_by() returns jsonb language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[] := current_actor();
  name text := correction_trim(current_setting('argus.changed_by_name', true));
  address text := correction_trim(current_setting('argus.changed_by_address', true));
begin
  if a[1] <> 'user' then
    raise exception 'only a person can change the approver table';
  end if;
  if coalesce(length(name), 0) = 0 then
    raise exception 'a change to the approver table needs the person''s name';
  end if;
  return jsonb_build_object('name', left(name, 200))
         || jsonb_strip_nulls(jsonb_build_object('sign_in_address',
                                                 left(nullif(address, ''), 320)));
end $$;

create function approver_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  perform approver_table_changed_by();
  if tg_op = 'DELETE' then
    if exists (select 1 from vendor_approver
               where approver_id = old.object_id or deputy_id = old.object_id) then
      raise exception 'this person is still approver or deputy of a vendor';
    end if;
    return old;
  end if;
  new.name := correction_trim(new.name);
  new.email := lower(btrim(new.email));
  if tg_op = 'UPDATE' then
    new.object_id := old.object_id;
    if new.absent is distinct from old.absent then
      new.absent_since := case when new.absent then clock_timestamp() end;
    else
      new.absent_since := old.absent_since;
    end if;
  else
    new.absent_since := case when new.absent then clock_timestamp() end;
  end if;
  new.changed_at := clock_timestamp();
  return new;
end $$;
create trigger approver_guard before insert or update or delete on approver
  for each row execute function approver_guard();

-- Runs as the owner, like the other audit triggers. No email address goes into the log.
create function approver_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare by_whom jsonb := approver_table_changed_by();
begin
  if tg_op = 'INSERT' then
    insert into audit_event (event_type, details)
    values ('approver_added', jsonb_build_object('approver', new.object_id,
                                                 'approver_name', new.name,
                                                 'absent', new.absent) || by_whom);
  elsif tg_op = 'DELETE' then
    insert into audit_event (event_type, details)
    values ('approver_removed', jsonb_build_object('approver', old.object_id,
                                                   'approver_name', old.name) || by_whom);
  else
    if (new.name, new.email) is distinct from (old.name, old.email) then
      insert into audit_event (event_type, details)
      values ('approver_changed',
              jsonb_build_object('approver', new.object_id,
                                 'approver_name', jsonb_build_object('old', old.name,
                                                                     'new', new.name),
                                 'email_changed', new.email is distinct from old.email)
              || by_whom);
    end if;
    if new.absent is distinct from old.absent then
      insert into audit_event (event_type, details)
      values ('approver_absence_changed',
              jsonb_build_object('approver', new.object_id, 'approver_name', new.name,
                                 'absent', new.absent) || by_whom);
    end if;
  end if;
  return null;
end $$;
create trigger approver_audit after insert or update or delete on approver
  for each row execute function approver_audit();

create function vendor_approver_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  perform approver_table_changed_by();
  if tg_op = 'DELETE' then
    return old;
  end if;
  if tg_op = 'UPDATE' then
    new.vendor_account := old.vendor_account;
  end if;
  -- Clearer than the foreign key's own message, which the admin would see.
  if not exists (select 1 from approver where object_id = new.approver_id)
     or (new.deputy_id is not null
         and not exists (select 1 from approver where object_id = new.deputy_id)) then
    raise exception 'add the approver and the deputy to the approver table first';
  end if;
  if new.deputy_id = new.approver_id then
    raise exception 'the deputy cannot be the approver as well';
  end if;
  new.changed_at := clock_timestamp();
  return new;
end $$;
create trigger vendor_approver_guard before insert or update or delete on vendor_approver
  for each row execute function vendor_approver_guard();

create function vendor_approver_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare by_whom jsonb := approver_table_changed_by();
begin
  if tg_op = 'DELETE' then
    insert into audit_event (event_type, details)
    values ('vendor_approver_removed',
            jsonb_build_object('vendor_account', old.vendor_account,
                               'approver', old.approver_id, 'deputy', old.deputy_id,
                               'direct_debit', old.direct_debit) || by_whom);
  elsif tg_op = 'INSERT' then
    insert into audit_event (event_type, details)
    values ('vendor_approver_set',
            jsonb_build_object('vendor_account', new.vendor_account,
                               'approver', jsonb_build_object('old', null, 'new', new.approver_id),
                               'deputy', jsonb_build_object('old', null, 'new', new.deputy_id),
                               'direct_debit', jsonb_build_object('old', null,
                                                                  'new', new.direct_debit))
            || by_whom);
  elsif (new.approver_id, new.deputy_id, new.direct_debit)
        is distinct from (old.approver_id, old.deputy_id, old.direct_debit) then
    insert into audit_event (event_type, details)
    values ('vendor_approver_set',
            jsonb_build_object('vendor_account', new.vendor_account,
                               'approver', jsonb_build_object('old', old.approver_id,
                                                              'new', new.approver_id),
                               'deputy', jsonb_build_object('old', old.deputy_id,
                                                            'new', new.deputy_id),
                               'direct_debit', jsonb_build_object('old', old.direct_debit,
                                                                  'new', new.direct_debit))
            || by_whom);
  end if;
  return null;
end $$;
create trigger vendor_approver_audit after insert or update or delete on vendor_approver
  for each row execute function vendor_approver_audit();

create function approver_table_no_truncate() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception 'the approver table changes one row at a time, each one logged'; end $$;
create trigger approver_no_truncate before truncate on approver
  execute function approver_table_no_truncate();
create trigger vendor_approver_no_truncate before truncate on vendor_approver
  execute function approver_table_no_truncate();

-- Like the other database events: only the triggers above write these.
create function audit_approver_table_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type in ('approver_added','approver_changed',
                                                   'approver_removed','approver_absence_changed',
                                                   'vendor_approver_set',
                                                   'vendor_approver_removed') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_approver_table_events before insert on audit_event
  for each row execute function audit_approver_table_events();

grant select on approver, vendor_approver to argus_app;
grant insert (object_id, name, email, absent), update (name, email, absent), delete
  on approver to argus_app;
grant insert (vendor_account, approver_id, deputy_id, direct_debit),
      update (approver_id, deputy_id, direct_debit), delete
  on vendor_approver to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from audit_event
             where event_type in ('approver_added','approver_changed','approver_removed',
                                  'approver_absence_changed','vendor_approver_set',
                                  'vendor_approver_removed')) then
    raise exception 'the approver table was used; its audit trail is kept for ten years (NF-01)';
  end if;
end $$;
drop trigger audit_approver_table_events on audit_event;
drop function audit_approver_table_events();
drop table vendor_approver;
drop table approver;
drop function approver_table_no_truncate();
drop function vendor_approver_audit();
drop function vendor_approver_guard();
drop function approver_audit();
drop function approver_guard();
drop function approver_table_changed_by();
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

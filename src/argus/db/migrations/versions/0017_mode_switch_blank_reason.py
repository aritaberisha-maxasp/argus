"""A mode switch reason made only of tabs or line breaks counts as blank (#170).

Covers NF-01 and NF-03. Migration 0012 asks for a reason of at least three characters when an
admin switches between shadow and live mode, but it trims with ``btrim``, which removes plain
spaces only. QA found that a reason of tabs or line breaks passed, so the audit log could hold a
switch with an empty reason. Migration 0013 fixed the same gap for corrections with
``correction_trim``; this migration uses that function for the mode switch too.

This migration changes two things:

- The guard on ``intake_mode`` trims the reason with ``correction_trim`` and refuses a switch
  whose reason is shorter than three characters after trimming, with the same sentence as
  before. Blank means only spaces of any kind: spaces, tabs, line breaks, non-breaking and zero
  width spaces. The trimmed reason is what goes into the audit log.
- The check on ``intake_mode.reason`` trims the same way. It is added ``not valid``, so the one
  row that holds the last switch is not checked again. The next switch overwrites it anyway.

Nothing in the audit log changes: earlier ``intake_mode_changed`` events stay as they were
written. The person's name is trimmed as before; the issue asks about the reason only.

Revision ID: 0017
Revises: 0015
Create Date: 2026-11-19
"""

from alembic import op

from argus.db import run_script

revision = "0017"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = r"""
create or replace function intake_mode_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if (current_actor())[1] <> 'user' then
    raise exception 'only a person can switch between shadow and live mode';
  end if;
  -- Who and why come from this transaction only (SET LOCAL), so a switch can never reuse the
  -- name or reason of an earlier one. The reason is trimmed of every kind of space, so tabs or
  -- line breaks alone do not count as a reason (#170).
  new.changed_by_name := nullif(btrim(current_setting('argus.switch_name', true)), '');
  new.changed_by_address := nullif(btrim(current_setting('argus.switch_address', true)), '');
  new.reason := nullif(correction_trim(current_setting('argus.switch_reason', true)), '');
  if new.changed_by_name is null or new.reason is null or length(new.reason) < 3 then
    raise exception 'a switch between shadow and live mode needs the person''s name and a reason';
  end if;
  new.id := old.id;
  new.changed_at := clock_timestamp();
  return new;
end $$;

alter table intake_mode drop constraint intake_mode_reason_check;
alter table intake_mode add constraint intake_mode_reason_check
  check (length(correction_trim(reason)) between 3 and 500) not valid;
"""

DOWNGRADE = r"""
alter table intake_mode drop constraint intake_mode_reason_check;
alter table intake_mode add constraint intake_mode_reason_check
  check (length(btrim(reason)) between 3 and 500) not valid;

create or replace function intake_mode_guard() returns trigger language plpgsql
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
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

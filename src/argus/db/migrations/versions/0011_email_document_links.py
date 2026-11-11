"""Tie every stored document to its email record, and the email record to its raw file (#90).

Covers NF-01 and IN-01. Follow ups from the reviews of PR 88 (#24):

- ``stored_document.email_id`` now references ``mailbox_email``, so a document can never point
  at an email ARGUS has no record of. 0003 could not add this because the table came in 0004.
- At the end of each transaction the database checks both directions of the raw email link:
  an email record past step seen has a raw file row (``kind = 'email'``) with the same
  fingerprint, and a raw file row belongs to an email record that recorded that fingerprint.
  The check is deferred because the sweep marks the email stored and records its files in the
  same transaction, in that order (compliance note on PR 88).
- ``mailbox_email.raw_location`` must be ``'sha256/' || raw_sha256``, the same rule 0003 has
  for ``stored_document.location`` (reviewer note on PR 88).

Both tables are append only or step only, so a link checked once stays right.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-07
"""

from alembic import op

from argus.db import run_script

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter table stored_document
  add constraint stored_document_email_fk foreign key (email_id) references mailbox_email(id);

alter table mailbox_email
  add constraint mailbox_email_raw_location check (raw_location = 'sha256/' || raw_sha256);

-- An email record that has a stored copy has the matching raw file row.
create function mailbox_email_has_raw_file() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.raw_sha256 is not null and not exists (
    select 1 from stored_document
     where email_id = new.id and kind = 'email' and sha256 = new.raw_sha256
  ) then
    raise exception 'email % is stored but its raw file is not recorded with that fingerprint',
      new.id;
  end if;
  return null;
end $$;
create constraint trigger mailbox_email_has_raw_file
  after insert or update of raw_sha256 on mailbox_email
  deferrable initially deferred
  for each row execute function mailbox_email_has_raw_file();

-- A raw file row belongs to an email record that recorded the same fingerprint.
create function stored_document_matches_email() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.kind = 'email' and not exists (
    select 1 from mailbox_email where id = new.email_id and raw_sha256 = new.sha256
  ) then
    raise exception 'raw file % does not match the stored copy of email %', new.id, new.email_id;
  end if;
  return null;
end $$;
create constraint trigger stored_document_matches_email
  after insert on stored_document
  deferrable initially deferred
  for each row execute function stored_document_matches_email();
"""

DOWNGRADE = r"""
drop trigger stored_document_matches_email on stored_document;
drop function stored_document_matches_email();
drop trigger mailbox_email_has_raw_file on mailbox_email;
drop function mailbox_email_has_raw_file();
alter table mailbox_email drop constraint mailbox_email_raw_location;
alter table stored_document drop constraint stored_document_email_fk;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

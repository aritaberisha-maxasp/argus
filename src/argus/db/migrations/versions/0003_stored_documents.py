"""Original documents with their SHA-256 fingerprint (issue #20, ADR 0001).

Covers NF-01 and NF-02. The files themselves live in blob storage under their fingerprint
(argus.documents.store). These tables keep which file came from which email and which case it
belongs to:
- stored_document: one row per file of an email, the raw .eml and each attachment.
- case_document: the files a case is based on, so the fingerprint is recorded on the case.

Both tables are insert only, for the application and for the owner. The database writes the
audit events document_stored and document_linked itself, with the fingerprint, and refuses
them from the application, so no code path can log a fingerprint that was not stored. File
names stay out of the audit log, because they can hold personal names (NF-03).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
"""

from alembic import op

from argus.db import run_script

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = r"""
create type document_kind as enum ('email','attachment');

create table stored_document (
  id bigint generated always as identity primary key,
  email_id bigint not null,               -- email record from ADR 0003 (#24 adds that table)
  kind document_kind not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  location text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  content_type text not null,
  filename text,                          -- as the sender named it; never in the audit log
  stored_at timestamptz not null,         -- set by the database
  check (location = 'sha256/' || sha256),
  check (kind = 'attachment' or filename is null),
  unique (email_id, kind, sha256)         -- recording the same email again adds nothing
);
create unique index stored_document_one_raw_email on stored_document (email_id)
  where kind = 'email';
-- The same file in another email (forwarded twice) is found by its fingerprint.
create index stored_document_sha256 on stored_document (sha256);

create table case_document (
  case_id bigint not null references invoice_case(id),
  document_id bigint not null references stored_document(id),
  linked_at timestamptz not null,         -- set by the database
  primary key (case_id, document_id)
);

create function document_append_only() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin raise exception '% is append only', tg_table_name; end $$;
create trigger stored_document_no_change before update or delete on stored_document
  for each row execute function document_append_only();
create trigger stored_document_no_truncate before truncate on stored_document
  execute function document_append_only();
create trigger case_document_no_change before update or delete on case_document
  for each row execute function document_append_only();
create trigger case_document_no_truncate before truncate on case_document
  execute function document_append_only();

create function stored_document_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  new.stored_at := clock_timestamp();
  return new;
end $$;
create trigger stored_document_guard before insert on stored_document
  for each row execute function stored_document_guard();

-- A case is based on the files of the email it came from, and a closed case takes no new files.
create function case_document_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare c record;
begin
  select status, source_email_id into c from invoice_case where id = new.case_id;
  if c.status = 'closed' then
    raise exception 'case % is closed and cannot change', new.case_id;
  end if;
  if c.source_email_id is distinct from
     (select email_id from stored_document where id = new.document_id) then
    raise exception 'document % did not come with the email of case %',
      new.document_id, new.case_id;
  end if;
  new.linked_at := clock_timestamp();
  return new;
end $$;
create trigger case_document_guard before insert on case_document
  for each row execute function case_document_guard();

-- Runs as the owner, because the application itself may not write these event types.
-- Only the fingerprint, where the file is, its size and type: no file name, no content.
create function stored_document_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (email_id, event_type, details)
  values (new.email_id, 'document_stored',
          jsonb_build_object('document_id', new.id, 'kind', new.kind, 'sha256', new.sha256,
                             'location', new.location, 'size_bytes', new.size_bytes,
                             'content_type', new.content_type));
  return null;
end $$;
create trigger stored_document_audit after insert on stored_document
  for each row execute function stored_document_audit();

create function case_document_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare d record;
begin
  select email_id, kind, sha256 into d from stored_document where id = new.document_id;
  insert into audit_event (case_id, email_id, event_type, details)
  values (new.case_id, d.email_id, 'document_linked',
          jsonb_build_object('document_id', new.document_id, 'kind', d.kind,
                             'sha256', d.sha256));
  return null;
end $$;
create trigger case_document_audit after insert on case_document
  for each row execute function case_document_audit();

-- Like the case events in audit_chain (0001): only the database triggers above write these.
create function audit_document_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type in ('document_stored','document_linked') then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_document_events before insert on audit_event
  for each row execute function audit_document_events();

grant select on stored_document, case_document to argus_app;
grant insert (email_id, kind, sha256, location, size_bytes, content_type, filename)
  on stored_document to argus_app;
grant insert (case_id, document_id) on case_document to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from stored_document) then
    raise exception 'stored documents exist; they are kept for ten years (NF-01)';
  end if;
end $$;
drop trigger audit_document_events on audit_event;
drop function audit_document_events();
drop table case_document;
drop table stored_document;
drop function case_document_audit();
drop function stored_document_audit();
drop function case_document_guard();
drop function stored_document_guard();
drop function document_append_only();
drop type document_kind;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

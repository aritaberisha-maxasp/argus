"""One case per invoice in an email, keyed on issuer plus invoice number (issue #30).

Covers IN-08, NF-01, NF-03 and NF-05. An email can hold several invoices, as separate
attachments or as several invoices in one PDF. Each becomes its own case:

- ``invoice_case.case_key`` is ``<issuer>|<invoice number>``, where the issuer is ``vat:<VAT ID>``
  or, without a VAT ID, ``name:<normalised name>``. It is set once when the case is created and
  never changes. One email has at most one case per key, so two vendors that both use number
  1001 stay two cases, and sorting the same email twice adds nothing. Duplicates across emails
  are #31's (``duplicate_key``).
- ``invoice_reading`` keeps what ARGUS read for each case: the fields with their source, page,
  words and flags (``InvoiceReading.as_record()``), the stored document and the pages it came
  from. It holds full IBANs and quotes from the invoice, so it is its own table and never goes
  into the audit log (compliance on PR 94). Insert only. The database writes the audit event
  ``invoice_read`` with the IDs, the pages, the reader, the kind and the names of the flagged
  fields, never a value.

The number 0008 is taken by #83 (PR 102), which merged first, so this migration follows 0008
and the chain stays one line, as agreed on #91.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-04
"""

from alembic import op

from argus.db import run_script

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter table invoice_case
  add column case_key text
    check (case_key is null or case_key ~ '^(vat|name):[^|]+\|.+$'),
  add check (case_key is null or source_email_id is not null);
create unique index invoice_case_one_per_invoice on invoice_case (source_email_id, case_key)
  where case_key is not null;

-- The key is set once, when the case is created.
create function invoice_case_key_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if new.case_key is distinct from old.case_key then
    raise exception 'the case key of case % is set once and never changes', old.id;
  end if;
  return new;
end $$;
create trigger invoice_case_key_guard before update on invoice_case
  for each row execute function invoice_case_key_guard();

create table invoice_reading (
  id bigint generated always as identity primary key,
  case_id bigint not null references invoice_case(id),
  document_id bigint references stored_document(id),  -- null for an invoice in the email text
  document text not null
    check (document = 'body' or document ~ '^attachment [0-9]+(, embedded XML)?$'),
  pages int[] not null default '{}',     -- pages of the file this invoice stands on
  role text not null check (role in ('invoice','copy')),  -- the case's data, or a PDF copy
  reading jsonb not null check (jsonb_typeof(reading) = 'object'),
  read_at timestamptz not null,          -- set by the database
  check ((document = 'body') = (document_id is null)),
  unique (case_id, document, pages)
);
create unique index invoice_reading_one_invoice on invoice_reading (case_id) where role = 'invoice';

create trigger invoice_reading_no_change before update or delete on invoice_reading
  for each row execute function document_append_only();
create trigger invoice_reading_no_truncate before truncate on invoice_reading
  execute function document_append_only();

-- A reading belongs to a case of the email its document came with, like case_document (0003).
create function invoice_reading_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare c record;
begin
  new.read_at := clock_timestamp();
  select status, source_email_id into c from invoice_case where id = new.case_id;
  if c.status = 'closed' then
    raise exception 'case % is closed and cannot change', new.case_id;
  end if;
  if new.document_id is not null and c.source_email_id is distinct from
     (select email_id from stored_document where id = new.document_id) then
    raise exception 'document % did not come with the email of case %',
      new.document_id, new.case_id;
  end if;
  return new;
end $$;
create trigger invoice_reading_guard before insert on invoice_reading
  for each row execute function invoice_reading_guard();

-- Runs as the owner. IDs, pages, reader, kind and the names of flagged fields: no values,
-- names, IBANs or quotes (NF-03).
create function invoice_reading_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  insert into audit_event (case_id, email_id, event_type, details)
  values (new.case_id, (select source_email_id from invoice_case where id = new.case_id),
          'invoice_read',
          jsonb_build_object(
            'reading_id', new.id, 'document_id', new.document_id, 'document', new.document,
            'pages', to_jsonb(new.pages), 'role', new.role,
            'reader', new.reading->>'reader', 'kind', new.reading->>'kind',
            'flagged', coalesce((select jsonb_agg(f.key order by f.key)
                                 from jsonb_each(coalesce(new.reading->'fields', '{}')) f
                                 where f.value->>'flag' is not null), '[]'::jsonb)));
  return null;
end $$;
create trigger invoice_reading_audit after insert on invoice_reading
  for each row execute function invoice_reading_audit();

-- Only the trigger above writes invoice_read, so the log never claims a reading that is not
-- stored. A trigger of its own, like audit_reminder_events (0007).
create function audit_reading_events() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if pg_trigger_depth() = 1 and new.event_type = 'invoice_read' then
    raise exception 'event % is written by the database, not by the application', new.event_type;
  end if;
  return new;
end $$;
create trigger audit_reading_events before insert on audit_event
  for each row execute function audit_reading_events();

grant insert (case_key) on invoice_case to argus_app;
grant select on invoice_reading to argus_app;
grant insert (case_id, document_id, document, pages, role, reading) on invoice_reading
  to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from invoice_reading)
     or exists (select 1 from invoice_case where case_key is not null) then
    raise exception 'invoice cases exist; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
revoke insert (case_key) on invoice_case from argus_app;
drop trigger audit_reading_events on audit_event;
drop function audit_reading_events();
drop table invoice_reading;
drop function invoice_reading_audit();
drop function invoice_reading_guard();
drop trigger invoice_case_key_guard on invoice_case;
drop function invoice_case_key_guard();
drop index invoice_case_one_per_invoice;
alter table invoice_case drop column case_key;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

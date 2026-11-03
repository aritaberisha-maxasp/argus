"""The sorting decision of each email, written into the audit trail by the database (#91).

Covers IN-04, IN-07, IN-09, NF-01 and NF-05. Adds ``classification`` to the email records of
0004: the decision of the sorting (#25) as :meth:`SortingDecision.audit_details` gives it, that
is the route, the category, what the model and our checks said, the reasons for manual review
and the masked evidence snippets. Never the prompt, the model answer or the email text.

The database holds the rules:

- the decision is set once, together with the step ``classified``, and never changes;
- an email at step ``classified`` or later always has one, an earlier one never does;
- the route the email later gets (0006) is the route of the decision, so the log cannot say
  one thing and the email do another.

The step event ``email_classified`` of 0004 now carries the decision. The trigger writes it, so
the application still cannot write any ``email_%`` event itself. Sorting the same email twice
changes nothing, because the step can only go from ``stored`` to ``classified`` once.

The number 0005 was kept for this story while 0006 (#26) and 0007 (#27) landed first. As agreed
on #91, whichever landed second follows the other, so this one follows 0007 and the chain stays
one line.

Revision ID: 0005
Revises: 0007
Create Date: 2026-11-02
"""

from alembic import op

from argus.db import run_script

revision = "0005"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter table mailbox_email
  add column classification jsonb
    check (classification is null or (jsonb_typeof(classification) = 'object'
           and classification->>'route' in ('invoice_processing','outgoing_booking',
                                            'forward_reminder','deletion_folder',
                                            'manual_review'))),
  add check ((classification is null) = (step in ('seen','stored'))),
  add check (route is null or route = classification->>'route');

-- The decision is set with the step classified and never changes afterwards.
create function mailbox_email_classification_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.classification is not null then
      raise exception 'a new email record is not classified';
    end if;
    return new;
  end if;
  if new.classification is distinct from old.classification
     and not (old.classification is null and old.step = 'stored' and new.step = 'classified')
  then
    raise exception 'the sorting decision of email % is set once, when it is classified', old.id;
  end if;
  return new;
end $$;
create trigger mailbox_email_classification_guard before insert or update on mailbox_email
  for each row execute function mailbox_email_classification_guard();

-- Replaces the step events of 0004: email_classified now carries the decision. Runs as the
-- owner. No sender, subject or content beyond the masked snippets of the decision (NF-03).
create or replace function mailbox_email_audit() returns trigger language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_seen', jsonb_build_object('graph_message_id', new.graph_message_id));
  elsif new.step is distinct from old.step then
    insert into audit_event (email_id, event_type, details)
    values (new.id, 'email_' || new.step,
            case when new.step = 'classified' then new.classification else '{}'::jsonb end
            || jsonb_strip_nulls(jsonb_build_object('raw_sha256', new.raw_sha256,
                                                    'raw_location', new.raw_location)));
  end if;
  return null;
end $$;

grant update (classification) on mailbox_email to argus_app;
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from mailbox_email where classification is not null) then
    raise exception 'emails are classified; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
revoke update (classification) on mailbox_email from argus_app;
create or replace function mailbox_email_audit() returns trigger language plpgsql
security definer
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
drop trigger mailbox_email_classification_guard on mailbox_email;
drop function mailbox_email_classification_guard();
alter table mailbox_email drop column classification;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

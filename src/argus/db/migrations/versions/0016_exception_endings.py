"""The database decides who may end an exception and how, and keeps the person's name (#180).

Covers NF-01, AP-08, EX-01, EX-02, PR-05, VM-04 and NF-04. Since PR 179 finance and admin can
resolve an exception from the invoices page (#62). The page only offers safe endings, but until
now the database accepted some the page refuses. This migration moves those rules into the
database, so no other code path can get round them:

- **Carrying on needs a person and a reason.** Every ending that does not close the case lets
  it move on toward payment: "Checked, carry on", "answered", "fixed in DATEV", but also
  "retried", "put back in the pool", "data corrected" and the rest, because the case goes back
  where it came from whatever the word (compliance review of PR 199). So every ending except
  ``rejected`` and ``confirmed_duplicate`` now comes only from a ``user:`` actor with a
  resolution that is not blank. Blank means only spaces of any kind, tabs or line breaks
  (``correction_trim`` from 0013). This covers a failed §14 UStG check and an unknown vendor,
  and the same rule holds for exceptions of an email or bank statement. Migration 0010 already
  holds it for "not a duplicate". A short list of automatic endings stays allowed, each tied to
  its reason, its outcome and the one job that ends it after checking again:

  - ``unknown_vendor`` as ``fixed_in_datev`` from ``system:vendor-match``, once the case has a
    vendor account (PR 182 matched it in DATEV).
  - ``vendor_without_approver`` as ``retried`` from ``system:vendor-match``, once the vendor
    has an approver (#34, #35).
  - ``no_account_proposal`` as ``fixed_in_datev`` from ``system:account-proposal``. Migration
    0019 already demands the proposal from the booking history for that.
  - ``bank_balance_difference`` as ``data_corrected`` from ``system:bank-import``, once the
    corrected statement of that balance and month is imported (#94).
  - ``approver_absent`` as ``retried`` from ``system:approval-request``, once someone can
    approve the vendor's invoices again (#191, migration 0020).
  - ``approver_address_outside`` as ``retried`` from ``system:approval-request``, once the
    approver table holds a Max ASP address for whoever approves the vendor's invoices (#191).
  - ``iban_mismatch`` is left out of this rule. PR 185 ends it as ``fixed_in_datev`` from
    ``system:vendor-match`` once DATEV holds every IBAN of the invoice, and 0002 already allows
    it only that ending or ``rejected``. #186 sets its own rule so only that job may pick
    ``fixed_in_datev`` there.

  A new automatic ending needs a new row in this list, in a migration, on purpose.

- **Closing only where nothing has moved yet.** ``rejected`` and ``confirmed_duplicate`` close
  the case. The database now refuses them for an outgoing invoice or credit note (it needs a
  credit note instead), a bank transaction (it has already happened) and a case that was
  booked, in a payment run or paid before the exception (a booking or money has already moved).
  Exceptions of an email or bank statement have no case and are not affected.

- **The person's name next to the object ID (ADR 0008).** ``resolved_by`` keeps the Entra
  object ID. ``resolved_by_name`` and ``resolved_by_address`` keep the person's name and sign in
  address as they were at that moment, because the object ID says nothing once the account is
  deleted. The application names the person for the transaction (``argus.actor_name`` and
  ``argus.actor_address``, set next to ``argus.actor``), and the database copies them onto the
  exception. A person's resolution without a name is refused. The sign in address is kept
  whenever the sign in gives one. A job's resolution carries neither.

- **The audit log.** ``exception_resolved`` now also carries the name and sign in address of a
  person, and a SHA 256 of the comment (``resolution_sha256``), so a later change to the comment
  can be detected. The comment text itself stays out of the audit log, as before (NF-03).

Rows resolved before this migration keep what they have: no name is filled in afterwards.

Every statement can run twice. A downgrade keeps the two name columns when any exception holds
a name, because they are part of the audit trail (NF-01); it only removes the rules.

Revision ID: 0016
Revises: 0020
Create Date: 2026-11-23
"""

from alembic import op

from argus.db import run_script

revision = "0016"
down_revision = "0020"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter table exception_item
  add column if not exists resolved_by_name text
    check (length(correction_trim(resolved_by_name)) between 1 and 200),
  add column if not exists resolved_by_address text
    check (length(correction_trim(resolved_by_address)) between 1 and 320);

-- Postgres fires the triggers of one table in name order. The name starts with zz_ so this one
-- runs after every other guard on exception_item: exception_guard refuses any change to a
-- resolved exception, and the rules of each reason (0010, 0013, 0019) give their own, more
-- precise refusal first.
create or replace function exception_ending_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare
  a text[];
  c record;
  automatic boolean;
begin
  if old.outcome is not null or new.outcome is null then
    return new;
  end if;
  a := current_actor();

  -- Every ending that does not close the case lets it carry on, so it needs a person and a
  -- reason, except the automatic endings listed in the module text of 0016.
  if new.outcome not in ('rejected', 'confirmed_duplicate')
     and old.reason::text <> 'iban_mismatch' then  -- #186 sets its own rule
    automatic := a[1] = 'system' and (
      (old.reason::text, new.outcome, a[2]) in (
        ('vendor_without_approver', 'retried', 'vendor-match'),
        ('no_account_proposal', 'fixed_in_datev', 'account-proposal'),
        ('bank_balance_difference', 'data_corrected', 'bank-import'),
        ('approver_absent', 'retried', 'approval-request'),
        ('approver_address_outside', 'retried', 'approval-request'))
      or ((old.reason::text, new.outcome, a[2]) = ('unknown_vendor', 'fixed_in_datev',
                                                   'vendor-match')
          and (select partner_account from invoice_case where id = old.case_id) is not null));
    if not automatic then
      if a[1] <> 'user' then
        raise exception 'only a person can end exception % as %', old.id, new.outcome;
      end if;
      if coalesce(length(correction_trim(new.resolution)), 0) = 0 then
        raise exception 'ending exception % as % needs a reason', old.id, new.outcome;
      end if;
    end if;
  end if;

  -- Closing the case only where no booking and no money has moved yet.
  if new.outcome in ('rejected', 'confirmed_duplicate') and old.case_id is not null then
    select kind, resume_status into c from invoice_case where id = old.case_id;
    if c.kind not in ('incoming_invoice', 'incoming_credit_note') then
      raise exception 'case % is an % and cannot be closed through the exception queue',
        old.case_id, c.kind;
    end if;
    if c.resume_status in ('booked', 'in_payment_run', 'paid') then
      raise exception 'case % was % and cannot be closed through the exception queue',
        old.case_id, c.resume_status;
    end if;
  end if;

  -- Who decided, as they were at that moment (ADR 0008).
  if a[1] = 'user' then
    new.resolved_by_name :=
      left(nullif(correction_trim(current_setting('argus.actor_name', true)), ''), 200);
    new.resolved_by_address :=
      left(nullif(correction_trim(current_setting('argus.actor_address', true)), ''), 320);
    if new.resolved_by_name is null then
      raise exception 'exception % needs the name of the person who resolves it', old.id;
    end if;
  else
    new.resolved_by_name := null;
    new.resolved_by_address := null;
  end if;
  return new;
end $$;
create or replace trigger zz_exception_ending_guard before update on exception_item
  for each row execute function exception_ending_guard();

-- As in 0002, plus the person's name and sign in address and a hash of the comment.
create or replace function exception_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_opened',
            jsonb_build_object('reason', new.reason, 'facts', new.facts));
  elsif new.resolved_at is not null and old.resolved_at is null then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_resolved',
            jsonb_build_object('reason', new.reason, 'outcome', new.outcome)
            || jsonb_strip_nulls(jsonb_build_object(
                 'name', new.resolved_by_name,
                 'sign_in_address', new.resolved_by_address,
                 'resolution_sha256',
                   encode(sha256(convert_to(new.resolution, 'UTF8')), 'hex'))));
  end if;
  return null;
end $$;
"""

DOWNGRADE = r"""
create or replace function exception_audit() returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_opened',
            jsonb_build_object('reason', new.reason, 'facts', new.facts));
  elsif new.resolved_at is not null and old.resolved_at is null then
    insert into audit_event (case_id, email_id, statement_id, exception_id, event_type, details)
    values (new.case_id, new.email_id, new.statement_id, new.id, 'exception_resolved',
            jsonb_build_object('reason', new.reason, 'outcome', new.outcome));
  end if;
  return null;
end $$;
drop trigger if exists zz_exception_ending_guard on exception_item;
drop function if exists exception_ending_guard();
-- Names of people who resolved exceptions are part of the audit trail, kept for ten years
-- (NF-01). With any of them in the database the columns stay; code from before 0016 ignores
-- them and the upgrade picks them up again.
do $$ begin
  if exists (select 1 from exception_item
             where resolved_by_name is not null or resolved_by_address is not null) then
    raise notice 'exceptions name the people who resolved them; the name columns stay (NF-01)';
    return;
  end if;
  alter table exception_item drop column resolved_by_name, drop column resolved_by_address;
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

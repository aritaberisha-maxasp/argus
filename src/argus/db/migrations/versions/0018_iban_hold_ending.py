"""Only ARGUS's own DATEV check may end an IBAN hold as fixed in DATEV (#186).

Covers PR-05, VM-04 and NF-01. Since PR 185 (#36) ARGUS holds an invoice as possible fraud when
it states an IBAN that DATEV does not have on the vendor's account. Migration 0002 already lets
such an ``iban_mismatch`` end only as ``fixed_in_datev`` or ``rejected``, but not who may pick
``fixed_in_datev``. Nothing else uses it today and the invoices page refuses it, yet one stray
call in a later story could end a fraud hold without anyone looking at DATEV.

This migration adds that rule to ``exception_guard``, next to the rule from 0002:

- An ``iban_mismatch`` ends as ``fixed_in_datev`` only from the actor ``system:vendor-match``.
  That is the vendor match pass, which ends the hold once DATEV holds every IBAN the invoice
  states on the vendor account. A person, any other job and a transaction with no actor are
  refused with a plain error.
- ``rejected`` stays open as before, so a person can still reject the invoice from the invoices
  page. The closing rules of 0016 (no closing a booked or paid case) still apply.

Migration 0016 leaves ``iban_mismatch`` out of its person only rule for exactly this reason, so
the two rules do not fight: for an IBAN hold only the vendor match may pick ``fixed_in_datev``,
for every other reason 0016 decides.

The actor is set per transaction, so this rule stops a mistake in another code path, not code
that pretends to be the vendor match. Code review still watches for that.

Exceptions resolved before this migration are not touched: the rule only looks at an ending as
it happens. The downgrade puts back ``exception_guard`` as 0002 wrote it.

Revision ID: 0018
Revises: 0016
Create Date: 2026-11-25
"""

from alembic import op

from argus.db import run_script

revision = "0018"
down_revision = "0016"
branch_labels = None
depends_on = None

# exception_guard as in 0002, with the rule of #186 added after the PR-05 rule.
UPGRADE = r"""
create or replace function exception_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare a text[] := current_actor();
begin
  if tg_op = 'INSERT' then
    new.opened_at := clock_timestamp();
    if new.outcome is not null or new.resolved_at is not null then
      raise exception 'a new exception cannot be resolved already';
    end if;
    return new;
  end if;
  if old.resolved_at is not null then
    raise exception 'exception % is resolved and cannot change', old.id;
  end if;
  if new.outcome is not null then
    -- A case exception can only be resolved while its case is stopped, so the outcome always
    -- decides where the case goes next (see case_guard). Email and statement exceptions have
    -- no case.
    if old.case_id is not null
       and (select status from invoice_case where id = old.case_id) <> 'exception' then
      raise exception 'case % must be in exception before exception % can be resolved',
        old.case_id, old.id;
    end if;
    -- PR-05, VM-04: an IBAN mismatch ends only by fixing the IBAN in DATEV or rejecting.
    if old.reason = 'iban_mismatch' and new.outcome not in ('fixed_in_datev','rejected') then
      raise exception 'an IBAN mismatch can only end as fixed_in_datev or rejected';
    end if;
    -- #186: who may end an IBAN hold. "Fixed in DATEV" comes only from the vendor match pass
    -- (system:vendor-match), after it read DATEV again and found every IBAN of the invoice on
    -- the vendor account. A person, any other job or a missing actor can only reject, and the
    -- closing rules of 0016 apply to that.
    if old.reason = 'iban_mismatch' and new.outcome = 'fixed_in_datev'
       and a is distinct from array['system', 'vendor-match'] then
      raise exception 'only ARGUS''s own DATEV check can end IBAN hold % as fixed in DATEV; %',
        old.id, 'a person can only reject it';
    end if;
    new.resolved_at := clock_timestamp();
    new.resolved_by := a[1] || ':' || a[2];
  end if;
  return new;
end $$;
"""

# exception_guard exactly as 0002 created it.
DOWNGRADE = r"""
create or replace function exception_guard() returns trigger language plpgsql
set search_path = pg_catalog, public, pg_temp as $$
declare a text[] := current_actor();
begin
  if tg_op = 'INSERT' then
    new.opened_at := clock_timestamp();
    if new.outcome is not null or new.resolved_at is not null then
      raise exception 'a new exception cannot be resolved already';
    end if;
    return new;
  end if;
  if old.resolved_at is not null then
    raise exception 'exception % is resolved and cannot change', old.id;
  end if;
  if new.outcome is not null then
    -- A case exception can only be resolved while its case is stopped, so the outcome always
    -- decides where the case goes next (see case_guard). Email and statement exceptions have
    -- no case.
    if old.case_id is not null
       and (select status from invoice_case where id = old.case_id) <> 'exception' then
      raise exception 'case % must be in exception before exception % can be resolved',
        old.case_id, old.id;
    end if;
    -- PR-05, VM-04: an IBAN mismatch ends only by fixing the IBAN in DATEV or rejecting.
    if old.reason = 'iban_mismatch' and new.outcome not in ('fixed_in_datev','rejected') then
      raise exception 'an IBAN mismatch can only end as fixed_in_datev or rejected';
    end if;
    new.resolved_at := clock_timestamp();
    new.resolved_by := a[1] || ':' || a[2];
  end if;
  return new;
end $$;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

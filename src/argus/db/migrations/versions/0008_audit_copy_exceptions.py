"""Exceptions for the nightly copy of the audit log (issue #83, ADR 0005).

Covers NF-01 and EX-01. Every night ARGUS copies the new audit events into the locked document
storage and then compares the database with all copies (see :mod:`argus.audit`). When either
step fails, a person has to look, so it lands in the exception queue with one of two new
reasons: ``audit_copy_failed`` or ``audit_check_failed``.

These items belong to no case, email or bank statement. The rule that an exception names
exactly one of them now allows none, but only for these two reasons. The open-once index
treats a missing target as 0, so each reason has at most one open item: a failure every night
does not pile up while nobody has looked yet.

The new reasons are compared as text in the check, because a value added to an enum cannot be
used in the same transaction.

Revision ID: 0008
Revises: 0005
Create Date: 2026-11-03
"""

from alembic import op

from argus.db import run_script

revision = "0008"
down_revision = "0005"
branch_labels = None
depends_on = None

UPGRADE = r"""
alter type exception_reason add value 'audit_copy_failed';
alter type exception_reason add value 'audit_check_failed';

alter table exception_item drop constraint exception_item_check;
alter table exception_item add constraint exception_item_target check (
  num_nonnulls(case_id, email_id, statement_id) = 1
  or (num_nonnulls(case_id, email_id, statement_id) = 0
      and reason::text in ('audit_copy_failed', 'audit_check_failed')));
"""

DOWNGRADE = r"""
do $$ begin
  if exists (select 1 from exception_item
             where reason::text in ('audit_copy_failed', 'audit_check_failed')) then
    raise exception 'audit copy exceptions exist; their audit trail is kept for ten years (NF-01)';
  end if;
end $$;
alter table exception_item drop constraint exception_item_target;
alter table exception_item add constraint exception_item_check
  check (num_nonnulls(case_id, email_id, statement_id) = 1);
-- Postgres cannot drop one value of an enum, so the type is built again without the two.
alter type exception_reason rename to exception_reason_0008;
create type exception_reason as enum (
  'uncertain_classification','unreadable_document','unknown_vendor','unknown_customer',
  'vendor_without_approver','duplicate','iban_mismatch','invoice_check_failed','booking_error',
  'unmatched_bank_transaction','bank_balance_difference',
  'number_sequence_gap','payment_failed','customer_query','forward_failed');
alter table exception_item
  alter column reason type exception_reason using reason::text::exception_reason;
alter table exception_exit
  alter column reason type exception_reason using reason::text::exception_reason;
drop type exception_reason_0008;
"""


def upgrade() -> None:
    run_script(op.get_bind(), UPGRADE)


def downgrade() -> None:
    run_script(op.get_bind(), DOWNGRADE)

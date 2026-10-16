-- Checks for schema-sketch.sql. Run against an empty database:
--   psql -d argus -f schema-sketch.sql && psql -d argus -f checks.sql
-- Every line marked "refused" must print an ERROR. Covers: NF-01, NF-05, EX-01, IN-10, BA-07, AC-8
\set ON_ERROR_STOP 0
set role argus_app;

\echo '1. a change without a named actor is refused'
insert into invoice_case (kind, source_email_id) values ('incoming_invoice', 1);

\echo '2. an invoice is received and sent for approval'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, source_email_id, partner_account, document_number, gross_minor, currency, duplicate_key)
values ('incoming_invoice', 1, '70001', 'RE-2026-0042', 892500, 'EUR', 'incoming|70001|RE20260042|892500|EUR')
returning id as c \gset
commit;
begin; set local argus.actor = 'system:approval'; update invoice_case set status = 'awaiting_approval' where id = :c; commit;
begin; set local argus.actor = 'user:approver-entra-id';
insert into audit_event (case_id, event_type, actor_type, actor, details)
values (:c, 'approval_decision', 'user', 'approver-entra-id', '{"step": "first", "decision": "approved", "comment": "ok"}');
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
insert into audit_event (case_id, event_type, actor_type, actor, details)
values (:c, 'approval_decision', 'user', 'benjamin-entra-id', '{"step": "second", "decision": "approved", "comment": "ok"}');
update invoice_case set status = 'approved' where id = :c;
commit;

\echo '3. skipping a step (approved to paid) is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set status = 'paid' where id = :c; rollback;

\echo '4. going to exception without an open exception is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set status = 'exception' where id = :c; rollback;

\echo '5. the booking fails, the case goes to the exception queue'
begin; set local argus.actor = 'system:booking';
insert into exception_item (case_id, reason, detail, resume_status) values (:c, 'booking_error', 'period closed', 'approved');
update invoice_case set status = 'exception' where id = :c;
commit;

\echo '6. leaving exception while it is still open is refused'
begin; set local argus.actor = 'user:benjamin-entra-id'; update invoice_case set status = 'approved' where id = :c; rollback;

\echo '7. resolving it into a different status than it came from is refused'
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set resolved_at = now(), resolved_by = 'benjamin-entra-id', resolution = 'period reopened' where case_id = :c;
update invoice_case set status = 'booked' where id = :c;
rollback;

\echo '8. resolved properly, then booked, put in a run and paid'
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set resolved_at = now(), resolved_by = 'benjamin-entra-id', resolution = 'period reopened' where case_id = :c;
update invoice_case set status = 'approved' where id = :c;
commit;
begin; set local argus.actor = 'system:booking'; update invoice_case set status = 'booked', datev_booking_ref = 'DATEV-123' where id = :c; commit;
begin; set local argus.actor = 'system:payment-run'; update invoice_case set status = 'in_payment_run' where id = :c; commit;
begin; set local argus.actor = 'system:wise-status'; update invoice_case set status = 'paid' where id = :c; commit;

\echo '9. the same invoice arriving again is refused by the duplicate key'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, source_email_id, duplicate_key) values ('incoming_invoice', 2, 'incoming|70001|RE20260042|892500|EUR');
rollback;

\echo '10. the same Wise transaction imported twice: the second is refused'
begin; set local argus.actor = 'system:bank-import';
insert into invoice_case (kind) values ('bank_transaction') returning id as b \gset
insert into bank_transaction values (:b, 'EUR', 'TRANSFER-1001', '2026-11-03', -892500);
commit;
begin; set local argus.actor = 'system:bank-import';
insert into invoice_case (kind) values ('bank_transaction') returning id as b \gset
insert into bank_transaction values (:b, 'EUR', 'TRANSFER-1001', '2026-11-03', -892500);
rollback;

\echo '11. the app changing or deleting history is refused'
update audit_event set actor = 'someone' where case_id = :c;
delete from audit_event where case_id = :c;

\echo '12. the trace of the invoice from receipt to payment (AC-8)'
select chain_seq, event_type, actor_type || ':' || actor as actor, from_status, to_status, details
from audit_event where case_id = :c order by chain_seq;
select coalesce((select broken_at::text from audit_verify()), 'chain ok') as chain;

reset role;
\echo '13. a database owner edits a row behind the triggers: the check finds it'
alter table audit_event disable trigger audit_no_change;
update audit_event set actor = 'someone-else' where chain_seq = 3;
alter table audit_event enable trigger audit_no_change;
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain;

\echo '14. the trace view also shows what happened to the email before the case existed'
set role argus_app;
begin; set local argus.actor = 'system:intake';
insert into audit_event (email_id, event_type, actor_type, actor, details) values (1, 'email_classified', 'system', 'intake', '{"category": "incoming_invoice"}');
commit;
select event_type, email_id, details from case_trace where trace_case_id = :c and case_id is null;

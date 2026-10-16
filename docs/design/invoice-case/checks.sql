-- Checks for schema-sketch.sql. Run against an empty database:
--   psql -d argus -f schema-sketch.sql && psql -d argus -f checks.sql
-- Every line marked "refused" must print an ERROR. Every other step must not.
-- Covers: NF-01, NF-03, NF-05, EX-01, AP-08, PR-05, VM-04, IN-10, BA-07, AC-8
\set ON_ERROR_STOP 0
set role argus_app;

\echo '== Happy path =='

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
insert into audit_event (case_id, event_type, details) values (:c, 'approval_decision', '{"step": "first", "decision": "approved", "comment": "ok"}');
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
insert into audit_event (case_id, event_type, details) values (:c, 'approval_decision', '{"step": "second", "decision": "approved", "comment": "ok"}');
update invoice_case set status = 'approved' where id = :c;
commit;

\echo '3. skipping a step (approved to paid) is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set status = 'paid' where id = :c; rollback;

\echo '4. going to exception without an open exception is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set status = 'exception' where id = :c; rollback;

\echo '5. the booking fails, the case goes to the exception queue'
begin; set local argus.actor = 'system:booking';
insert into exception_item (case_id, reason, detail, facts) values (:c, 'booking_error', 'period 10/2026 is closed in DATEV', '{"datev_error": "period_closed"}');
update invoice_case set status = 'exception' where id = :c;
commit;

\echo '6. leaving exception while it is still open is refused'
begin; set local argus.actor = 'user:benjamin-entra-id'; update invoice_case set status = 'approved' where id = :c; rollback;

\echo '7. resolving it into a different status than it came from is refused'
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'retried', resolution = 'period reopened' where case_id = :c;
update invoice_case set status = 'booked' where id = :c;
rollback;

\echo '8. resolved properly, then booked, put in a run and paid'
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'retried', resolution = 'period reopened' where case_id = :c;
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

\echo '== Ways round the rules found in review (all refused) =='

\echo '12. skipping approval through the exception queue (received to exception to approved) is refused'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind) values ('incoming_invoice') returning id as a \gset
insert into exception_item (case_id, reason, detail) values (:a, 'unknown_vendor', 'vendor not in DATEV');
update invoice_case set status = 'exception' where id = :a;
update exception_item set outcome = 'fixed_in_datev' where case_id = :a;
update invoice_case set status = 'approved' where id = :a;
rollback;

\echo '13. writing resume_status from the app is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set resume_status = 'paid' where id = :c; rollback;

\echo '14. booked to paid through the exception queue without a run is refused'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, partner_account, gross_minor, currency) values ('outgoing_invoice', '10001', 119000, 'EUR') returning id as o \gset
update invoice_case set status = 'booked' where id = :o;
commit;
begin; set local argus.actor = 'system:test';
insert into exception_item (case_id, reason, detail) values (:o, 'number_sequence_gap', 'gap before this number');
update invoice_case set status = 'exception' where id = :o;
update exception_item set outcome = 'accepted_with_comment' where case_id = :o;
update invoice_case set status = 'paid' where id = :o;
rollback;

\echo '15. an open IBAN mismatch: moving the case on is refused'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, partner_account, gross_minor, currency) values ('incoming_invoice', '70002', 50000, 'EUR') returning id as i \gset
update invoice_case set status = 'awaiting_approval' where id = :i;
update invoice_case set status = 'approved' where id = :i;
update invoice_case set status = 'booked' where id = :i;
commit;
begin; set local argus.actor = 'system:iban-check';
insert into exception_item (case_id, reason, detail, facts) values (:i, 'iban_mismatch', 'IBAN on invoice differs from DATEV', '{"iban_last4": "4321", "matches_datev": false}');
update invoice_case set status = 'in_payment_run' where id = :i;
rollback;

\echo '16. opening an IBAN mismatch without stopping the case: the commit is refused'
begin; set local argus.actor = 'system:iban-check';
insert into exception_item (case_id, reason, detail) values (:i, 'iban_mismatch', 'IBAN on invoice differs from DATEV');
commit;

\echo '17. ending an IBAN mismatch by accepting the invoice IBAN is refused'
begin; set local argus.actor = 'system:iban-check';
insert into exception_item (case_id, reason, detail) values (:i, 'iban_mismatch', 'IBAN on invoice differs from DATEV');
update invoice_case set status = 'exception' where id = :i;
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'accepted_with_comment', resolution = 'looks fine' where case_id = :i and resolved_at is null;
rollback;

\echo '18. changing the kind of a case is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set kind = 'outgoing_invoice' where id = :i; rollback;

\echo '19. changing the amount or vendor of an approved invoice is refused'
begin; set local argus.actor = 'system:test'; update invoice_case set gross_minor = 99999999, partner_account = '79999' where id = :c; rollback;

\echo '20. rewriting a resolved exception is refused'
begin; set local argus.actor = 'system:test'; update exception_item set resolution = 'something else' where case_id = :c; rollback;
begin; set local argus.actor = 'system:test'; update exception_item set reason = 'unknown_vendor' where case_id = :c; rollback;

\echo '21. the app naming its own actor or time in an audit event is refused'
begin; set local argus.actor = 'system:test';
insert into audit_event (case_id, event_type, actor_type, actor, occurred_at) values (:c, 'approval_decision', 'user', 'benjamin', '2020-01-01');
rollback;

\echo '22. the app writing a status event itself is refused'
begin; set local argus.actor = 'system:test';
insert into audit_event (case_id, event_type, details) values (:c, 'status_changed', '{}');
rollback;

\echo '23. a resolved IBAN mismatch (fixed in DATEV) goes back to booked'
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'fixed_in_datev', resolution = 'Benjamin updated the vendor IBAN in DATEV' where case_id = :i and resolved_at is null;
update invoice_case set status = 'booked' where id = :i;
commit;

\echo '24. a failed payment goes from paid back to the pool, but only for payment_failed'
begin; set local argus.actor = 'system:wise-status';
insert into exception_item (case_id, reason, detail, facts) values (:c, 'payment_failed', 'transfer bounced', '{"wise_status": "bounced_back"}');
update invoice_case set status = 'exception' where id = :c;
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'put_back_in_pool' where case_id = :c and resolved_at is null;
update invoice_case set status = 'booked' where id = :c;
commit;

\echo '24a. a rejected IBAN mismatch going back to booked is refused, it can only be closed as rejected'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, partner_account, gross_minor, currency) values ('incoming_invoice', '70004', 20000, 'EUR') returning id as r \gset
update invoice_case set status = 'awaiting_approval' where id = :r;
update invoice_case set status = 'approved' where id = :r;
update invoice_case set status = 'booked' where id = :r;
insert into exception_item (case_id, reason, detail, facts) values (:r, 'iban_mismatch', 'IBAN on invoice differs from DATEV', '{"iban_last4": "9876", "matches_datev": false}');
update invoice_case set status = 'exception' where id = :r;
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'rejected' where case_id = :r;
update invoice_case set status = 'booked' where id = :r;
rollback;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'rejected' where case_id = :r;
update invoice_case set status = 'closed', closed_reason = 'withdrawn' where id = :r;
rollback;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'rejected' where case_id = :r;
update invoice_case set status = 'closed', closed_reason = 'rejected' where id = :r;
commit;

\echo '24b. a confirmed duplicate going on to approval is refused, it can only be closed as duplicate'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, partner_account, gross_minor, currency) values ('incoming_invoice', '70001', 892500, 'EUR') returning id as d \gset
insert into exception_item (case_id, reason, detail, facts) values (:d, 'duplicate', 'same as an earlier case', jsonb_build_object('duplicate_of', :c));
update invoice_case set status = 'exception' where id = :d;
commit;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'confirmed_duplicate' where case_id = :d;
update invoice_case set status = 'received' where id = :d;
rollback;
begin; set local argus.actor = 'user:benjamin-entra-id';
update exception_item set outcome = 'confirmed_duplicate' where case_id = :d;
update invoice_case set status = 'closed', closed_reason = 'duplicate' where id = :d;
commit;
select id, status, closed_reason from invoice_case where id in (:r, :d) order by id;

\echo '25. a correction while awaiting approval is allowed and logged with old and new values'
begin; set local argus.actor = 'system:intake';
insert into invoice_case (kind, partner_account, gross_minor, currency) values ('incoming_invoice', '70003', 10000, 'EUR') returning id as w \gset
update invoice_case set status = 'awaiting_approval' where id = :w;
commit;
begin; set local argus.actor = 'user:arita-entra-id'; update invoice_case set gross_minor = 11900 where id = :w; commit;
select event_type, actor, details from audit_event where case_id = :w and event_type = 'case_changed';

\echo '26. exception text does not reach the audit log (NF-03)'
select count(*) as events_with_free_text from audit_event where details::text like '%differs from DATEV%' or details::text like '%period reopened%';

\echo '== Trace and chain =='

\echo '27. the trace of the first invoice from receipt to payment (AC-8)'
select chain_seq, event_type, actor_type || ':' || actor as actor, from_status, to_status, details
from audit_event where case_id = :c order by chain_seq;

\echo '28. the trace view also shows what happened to the email before the case existed'
begin; set local argus.actor = 'system:intake';
insert into audit_event (email_id, event_type, details) values (1, 'email_classified', '{"category": "incoming_invoice"}');
commit;
select event_type, email_id, details from case_trace where trace_case_id = :c and case_id is null;

\echo '29. the chain checks out in UTC and in Berlin time'
set timezone = 'UTC';
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain_utc;
set timezone = 'Europe/Berlin';
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain_berlin;

reset role;
\echo '30. a database owner edits a row behind the triggers: the check finds it'
alter table audit_event disable trigger audit_no_change;
select actor as original_actor from audit_event where chain_seq = 3 \gset
update audit_event set actor = 'someone-else' where chain_seq = 3;
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain_after_actor_edit;
update audit_event set actor = :'original_actor' where chain_seq = 3;
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain_after_undo;

\echo '31. moving a value from one column to the next is found too'
update audit_event set email_id = case_id, case_id = null where chain_seq = 4 and email_id is null;
select coalesce((select 'broken at ' || broken_at from audit_verify()), 'chain ok') as chain_after_column_shift;
alter table audit_event enable trigger audit_no_change;

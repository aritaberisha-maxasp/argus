"""The invoice case store (ADR 0005): one case per invoice, credit note or bank transaction,
its status, the exception queue and the audit log. The database enforces the rules; this
package is the one way the rest of ARGUS talks to it."""

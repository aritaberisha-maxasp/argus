# 0007: Backups of the case database

Status: proposed
Requirements: NF-01, NF-03

Issue #82. Builds on ADR 0001 (Azure, PostgreSQL Flexible Server, locked blob store), ADR 0005 (the audit log and its hash chain) and the nightly audit copy from #83.

## Context

The case database holds every invoice case and the audit log. If it is lost or damaged, Max ASP must still be able to show the trail of every invoice for the legal retention period (GoBD, § 147 AO). ADR 0001 proposes 10 years for everything until the tax advisor confirms the period.

What we have and what we need:

- PostgreSQL Flexible Server keeps its own backups for at most 35 days. That covers "someone broke something last week", not ten years.
- The nightly audit copy (#83) keeps every audit event for ten years in the locked blob store. It does not hold the cases themselves.
- Every backup must stay in the EU: Germany West Central, or Germany North for a second copy.
- The period must be a setting, so it can change once the tax advisor answers.

## Options

1. **Azure Backup long term retention for PostgreSQL Flexible Server.** Azure's own service: a full logical backup (`pg_dump`) once a week into a Backup vault, kept up to 10 years. Generally available in every public Azure region, Germany West Central included. The vault can be geo redundant (second copy in Germany North), immutable and locked. Pro: nothing to run ourselves, protected outside our subscription against deletion and ransomware. Con: weekly only, a restore gives back files that we load into a new server with `pg_restore`, and databases owned by `azure_pg_admin` or `azuresu` are left out.
2. **A monthly dump by ARGUS into the locked blob store.** A Container Apps job runs `pg_dump` and writes the file next to the original documents. Pro: same lock as the documents. Con: our own job, our own monitoring, a job that holds a database admin right and writes to the document store, and nothing guards against the job silently stopping.
3. **Azure Backup v2 (snapshot based, daily, restore straight to a server).** Con: still in preview. Not for production data under GoBD.

## Decision

Option 1, as the issue asks first.

- **Short term:** point in time restore on the server, 35 days, the maximum (`infra/modules/database.bicep`). It stays in Germany West Central.
- **Long term:** a Backup vault in Germany West Central (`infra/modules/backup.bicep`), geo redundant into Germany North. One full backup every Sunday at 01:00 UTC. The first backup of each month is kept for `databaseBackupYears` years, 10 by default and the most Azure allows. The other weekly backups are kept 3 months.
- **Why the first backup of each month is enough for ten years:** the audit log and the cases are never deleted from the database (ADR 0005). Any record of year Y is in the first monthly backup of year Y+1, which is kept until year Y+11, past the end of the ten year period that starts at the end of year Y. The newest backup always holds everything.
- **Locking:** the vault is immutable from the start, so nobody can delete a backup early or shorten its retention without first switching immutability off. It becomes locked for good, with soft delete always on, together with the retention lock of the original documents (`retentionLocked`). One decision by Arita, after the tax advisor confirms, locks both.
- **Owner of the database:** the deploy hands the ARGUS database to the database admin when Azure created it owned by `azure_pg_admin` (`tools/deploy.py database-owner`), because the long term backup skips such databases.
- **Rights:** the vault signs in to the server with its own identity. It needs two roles that the deploy identity may not hand out (docs/operations/deploy.md, step 5), so Arita grants them once and then switches `databaseBackupProtected` on. Until then the vault and its policy exist, but no long term backup runs.
- **Restore:** to a new server, then `tools/restore_check.py` checks the audit chain with `audit_verify()`, looks for gaps and compares the restore with the nightly audit copies. The runbook is [docs/operations/backup-and-restore.md](../operations/backup-and-restore.md).

## Consequences

- No backup job of our own to run or watch. Azure Backup reports failed backups in the vault.
- Up to a week of changes since the last long term backup is covered only by the 35 day point in time restore. For a record older than 35 days that changed in the last week, the nightly audit copy still shows every step.
- A restore of a long term backup takes manual steps (download the files, `pg_restore`), written down in the runbook. That is acceptable for an event we expect never to need outside audits and drills.
- When Azure Backup v2 is generally available, we can switch to it for daily backups and a direct restore to a server. Only one protection per server is allowed, so the switch replaces this one.
- Cost: a protected instance fee per started 250 GB plus storage. For a database of a few GB, a few euros a month.
- Once the vault is locked, the period can be raised but never lowered, and backups cannot be deleted before they expire. That is the point, and why the lock waits for the tax advisor.

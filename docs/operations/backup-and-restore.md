# Backups of the case database and how to restore them

The case database holds every invoice case and the audit log. This runbook says what is backed up, the one time step Arita does to start the long term backup, how to restore, and how to check a restore. The decision behind it is [ADR 0007](../adr/0007-database-backups.md), the issue is #82.

## What is backed up

| | Short term | Long term |
| --- | --- | --- |
| What | Point in time restore of the whole server | Full backup (`pg_dump`) of every ARGUS database |
| How often | Continuous | Every Sunday, 01:00 UTC |
| Kept | 35 days, the maximum | First backup of each month: `databaseBackupYears` (10). Other weeks: 3 months |
| Where | Germany West Central | Backup vault in Germany West Central, second copy in Germany North |
| Set in | `infra/modules/database.bicep` | `infra/modules/backup.bicep`, `infra/main.bicepparam` |
| Restores to | A new server, any second in the last 35 days | Files, which you load into a new server |

Nothing leaves Germany. The vault is immutable: no backup can be deleted before it expires and the retention cannot be shortened, unless someone switches immutability off first. The vault is locked for good, together with the original documents, when Arita switches `retentionLocked` on (infra/README.md).

The nightly audit copy (#83) is a third line of defence for the audit log: one file per night in the locked document store, kept as long as the originals.

## One time step: start the long term backup (Arita)

The first deploy creates the Backup vault and its policy, but no backup runs yet. The vault signs in to the database server with its own identity, and that identity needs two roles the deploy identity may not hand out. Run this once in Azure Cloud Shell, after the first deploy:

```
server=$(az postgres flexible-server list --resource-group rg-argus --query "[0].name" --output tsv)
server_id=$(az postgres flexible-server show --resource-group rg-argus --name "$server" --query id --output tsv)
vault=$(az resource list --resource-group rg-argus --resource-type Microsoft.DataProtection/backupVaults --query "[0].name" --output tsv)
vault_principal=$(az resource show --resource-group rg-argus --resource-type Microsoft.DataProtection/backupVaults --name "$vault" --query identity.principalId --output tsv)

az role assignment create --assignee-object-id "$vault_principal" \
  --assignee-principal-type ServicePrincipal \
  --role "PostgreSQL Flexible Server Long Term Retention Backup Role" --scope "$server_id"

az role assignment create --assignee-object-id "$vault_principal" \
  --assignee-principal-type ServicePrincipal \
  --role Reader --scope /subscriptions/<subscription id>/resourceGroups/rg-argus
```

Then change `param databaseBackupProtected = false` to `true` in `infra/main.bicepparam`, in a PR of its own. When it is merged, the deploy connects the server to the vault. The first backup runs the next Sunday. To see it at once: portal, Backup vault, Backup instances, the server, Backup now.

If the deploy fails with `UserErrorMissingRequiredPermissions`, one of the two roles is missing or has not spread yet. Wait ten minutes and run the deploy again (Actions, Deploy, Run workflow).

## Check that backups run

Portal, Backup vault, Backup jobs. Every Sunday should show one completed backup job. A failed job shows its reason there. Once a quarter, look at Backup instances, the server, Restore, and check the list of recovery points goes back as far as it should.

## Restore

Always restore into a **new** server, never over the running one. Check the restore before anyone uses it. The new server holds the same personal data as the original, so keep it in `rg-argus` in Germany West Central, and delete it when it is no longer needed. Work in Azure Cloud Shell, not on a laptop, so no copy of the data lands on a personal device.

### A. Any moment in the last 35 days

```
az postgres flexible-server restore --resource-group rg-argus \
  --name psql-argus-restore-<yyyymmdd> --source-server "$server" \
  --restore-time "<yyyy-mm-dd>T<hh:mm>:00+00:00"
```

The new server has the same Entra admins and settings as the original. Continue with [Check the restore](#check-the-restore).

### B. From a long term backup

1. **A storage account for the files.** Azure Backup gives the backup back as files in a storage container. Make a temporary one, in Germany West Central, that allows the copy from Microsoft's backup storage:

   ```
   az storage account create --resource-group rg-argus --name strestore<yyyymmdd> \
     --location germanywestcentral --sku Standard_LRS --min-tls-version TLS1_2 \
     --allow-blob-public-access false --allow-cross-tenant-replication true
   az storage container create --account-name strestore<yyyymmdd> --name restore --auth-mode login
   az role assignment create --assignee-object-id "$vault_principal" \
     --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" \
     --scope $(az storage account show --resource-group rg-argus --name strestore<yyyymmdd> --query id --output tsv)
   ```

   Do not use the account for original documents: its retention lock would keep the restore files for ten years.

2. **Restore as files.** Portal, Backup vault, Backup instances, the server, Restore. Pick the recovery point, then the storage account and container from step 1, Validate, Restore. Follow the job under Backup jobs. When it is done, the container holds one file per database (`argus.sql` and the like, in `pg_dump`'s custom format despite the name) and `roles.sql`.

3. **A new server.** Same settings as the original, with Entra sign in only and yourself as Entra admin:

   ```
   az postgres flexible-server create --resource-group rg-argus --name psql-argus-restore-<yyyymmdd> \
     --location germanywestcentral --tier Burstable --sku-name Standard_B1ms --version 16 \
     --storage-size 32 --microsoft-entra-auth Enabled --password-auth Disabled \
     --public-access None
   az postgres flexible-server microsoft-entra-admin create --resource-group rg-argus \
     --server-name psql-argus-restore-<yyyymmdd> --display-name "<your sign in name>" \
     --object-id $(az ad signed-in-user show --query id --output tsv) --type User
   az postgres flexible-server firewall-rule create --resource-group rg-argus \
     --name psql-argus-restore-<yyyymmdd> --rule-name cloud-shell \
     --start-ip-address $(curl -s https://api.ipify.org) --end-ip-address $(curl -s https://api.ipify.org)
   ```

4. **Load the backup.** ARGUS needs only its own role `argus_app`, so create that by hand instead of running all of `roles.sql`. Then load the ARGUS database:

   ```
   export PGHOST=psql-argus-restore-<yyyymmdd>.postgres.database.azure.com PGSSLMODE=require
   export PGUSER="<your sign in name>"
   export PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv)
   psql --dbname postgres -c "create role argus_app nologin" -c "create database argus_restored"
   az storage blob download --account-name strestore<yyyymmdd> --container-name restore \
     --name <file of the argus database> --file argus.dump --auth-mode login
   pg_restore --no-owner --exit-on-error --dbname argus_restored argus.dump
   rm argus.dump
   ```

   `pg_restore` loads the data first and the triggers after it, so the audit events come back exactly as they were and the audit log is append only again right after.

### Check the restore

Get the newest nightly audit copies from the document store, then run the check from a checkout of this repository in Cloud Shell (`pip install -e .` first):

```
az storage blob download-batch --account-name <documents account> --source documents \
  --pattern "audit-log/<yyyy-mm>-*.jsonl" --destination . --auth-mode login

ARGUS_DATABASE_URL="postgresql+psycopg://$PGHOST:5432/argus_restored?sslmode=require" \
  python tools/restore_check.py --audit-copy audit-log/<yyyy-mm-dd>.jsonl [--audit-copy ...]
```

It only reads, and prints only numbers, times and hashes, so its output can go into the record below. It passes when:

- `audit_verify()` finds the hash chain whole;
- no chain number is missing;
- every event of the copies that the restore holds has exactly the hash in the copy.

Events in the copies that are newer than the backup are counted, not failed: that number is how much the restore lost since the backup was taken. Those events are still in the nightly copies.

When done, delete the temporary server and storage account:

```
az postgres flexible-server delete --resource-group rg-argus --name psql-argus-restore-<yyyymmdd> --yes
az storage account delete --resource-group rg-argus --name strestore<yyyymmdd> --yes
```

### If the running database is lost

Pointing ARGUS at another server is not automated. The templates name the server `psql-argus-<suffix>`, and the deploy expects that name.

- **The server still exists but its data is damaged:** restore as in A to a new name, check it, and ask the team for a PR that moves ARGUS over. Do not delete the damaged server: it is evidence, and its own backups still run.
- **The server is gone:** remove the delete lock if it is still there, restore as in A (or B when the 35 days are gone too) under the old server name, check it, then run the deploy by hand (Actions, Deploy, Run workflow). The deploy maps the app identity again and runs any missing migrations.

## Changing the retention period

`databaseBackupYears` in `infra/main.bicepparam`. The CI check in `tools/check_infra.py` refuses less than 10 years; lowering the period means changing that rule too, in the same PR, with the tax advisor's answer named in it. 10 is the most Azure Backup allows. While the vault is immutable, Azure refuses to shorten the retention of backups that already exist.

## Restore record

Write every restore here: the date, which backup, who, and the output of `tools/restore_check.py`.

### 6 Nov 2026: rehearsal without Azure (#82)

Azure is not set up yet, so this rehearsed route B on two local PostgreSQL 16 servers, with the same tools Azure Backup uses.

1. Source: a database at migration 0009 with 25 audit events, owned by its admin (`tools/deploy.py database-owner`: "the database stays owned by argus_owner").
2. Backup: `pg_dump --format=custom`, as Azure Backup takes it. Then 3 more events were written to the source, so the backup is behind by three.
3. A nightly copy of events 21 to 28 was written as #83 writes it.
4. A second server with another admin and no ARGUS roles: `create role argus_app nologin`, `create database argus_restored`, `pg_restore --no-owner --exit-on-error`. No errors.
5. `tools/restore_check.py --audit-copy 2026-11-06.jsonl`:

```
migration: 0009 (the newest, code is at 0009)
audit events: 25, last chain number 25
newest event: 2026-11-06T18:03:23Z
newest hash: 791f67de50e92ae6025d6988f1d88c5285fe289a9b280795e1681e48568f51f1
hash chain: whole
nightly copies: 1 read, 5 events match, 3 events are newer than the backup
restore check: passed
```

6. On the restore, `argus_app` could read the audit log, and the owner could not delete from it (append only).

The same route runs in CI on every PR: `tests/integration/test_backup_restore.py` dumps the test database, restores it into a new database and checks it, and proves the check fails on a changed event, a gap, or a hash that differs from the nightly copy.

### Still to do: the first restore in Azure

After the one time step above and the first long term backup, run route B once for real and add the result here. Until then, the real `Restore as files`, the vault's roles and the Entra sign in on the new server are untested.

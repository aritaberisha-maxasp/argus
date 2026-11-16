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

The same switch also creates the backup alerts (#115), and one of them is an alert processing rule. Azure only accepts it when the subscription has the resource provider `Microsoft.AlertsManagement` registered, and the deploy identity may not register providers. Check it once; if it does not print `Registered`, register it (this needs a right on the subscription, so Arita or Max ASP IT):

```
az provider show --namespace Microsoft.AlertsManagement --query registrationState --output tsv
az provider register --namespace Microsoft.AlertsManagement
```

Then change `param databaseBackupProtected = false` to `true` in `infra/main.bicepparam`, in a PR of its own. When it is merged, the deploy connects the server to the vault and switches on the backup alerts. The first backup runs the next Sunday. To see it at once: portal, Backup vault, Backup instances, the server, Backup now.

If the deploy fails with `UserErrorMissingRequiredPermissions`, one of the two roles is missing or has not spread yet. Wait ten minutes and run the deploy again (Actions, Deploy, Run workflow). If it fails with `MissingSubscriptionRegistration` for `Microsoft.AlertsManagement`, the provider above is not registered yet.

## Check that backups run

Nobody has to look every week. Once the long term backup is switched on, Azure emails `ARGUS_ALERT_EMAIL` through the action group `ag-argus-ops` (#115):

- when a backup or restore job of the vault fails, or someone deletes backup data;
- when no backup job has completed since Sunday. The rule checks every Monday between 23:00 and 24:00 UTC, so the last good backup is then at least 8 days old.

Once a quarter, still look at Backup instances, the server, Restore, and check the list of recovery points goes back as far as it should.

### When a backup alert arrives

A missing weekly backup is not urgent for the database itself (the server keeps its own 35 days), but every missed month leaves a gap in the ten year trail. Fix it the same week.

- **A failed backup job.** The email comes from Azure Backup and names the vault, the backup instance (the database server), the job and an error code. Portal, Backup vault, Backup jobs, the failed job: it shows the reason. `UserErrorMissingRequiredPermissions` means one of the vault's two roles is gone (see the one time step above). For anything else, start a new backup at once (Backup instances, the server, Backup now) and check that it completes. Backup now asks for a retention rule: pick `Monthly` if this month has no `Monthly` recovery point yet (look under Restore), otherwise `Default`. With `Default` the backup is deleted after 3 months, and the month would have no 10 year copy. If it fails again, look up the error code in the Azure Backup docs or ask the team.
- **`alert-bvault-...-no-backup`.** No backup job completed since Sunday. Look at Backup jobs first. If there is a failed job, see above. If there is no job at all, look at Backup instances: the server must be listed with protection on. If it is gone or stopped, someone stopped or removed it by hand (switching `databaseBackupProtected` off does not remove it, since deploys never delete anything); the next deploy with the switch on brings a removed instance back, and a stopped one is resumed under Backup instances, Resume backup. If the jobs look fine but the alert still came, the job records did not reach Log Analytics: check the diagnostic setting `argus-backup-jobs` on the vault. Then run Backup now, so the month has its backup. Backup now asks for a retention rule: pick `Monthly` if this month has no `Monthly` recovery point yet (look under Restore), otherwise `Default`. With `Default` the backup is deleted after 3 months, and the month would have no 10 year copy.
- **Deleted backup data.** Treat it as an incident and tell Arita at once. Soft delete keeps deleted backups for 14 days, so they can still be brought back (Backup vault, Backup instances, show soft deleted items).

The alert names the vault, the server, the job, resource IDs, the time and Azure's error code and message. It never holds invoice or personal data, and the link in it opens the Azure portal, where you sign in as usual.

## Restore

Always restore into a **new** server, never over the running one. Check the restore before anyone uses it. The new server holds the same personal data as the original, so keep it in `rg-argus` in Germany West Central, and delete it when it is no longer needed.

### Where you work: an EU Cloud Shell, files only in a temporary folder

Work in Azure Cloud Shell, not on a laptop, so no copy of the data lands on a personal device. Route B and the check download a full dump and the nightly audit copies, so where Cloud Shell keeps files matters (NF-03):

- **Use an ephemeral session.** Portal, Cloud Shell, Settings, Reset user settings, then start Bash with **No storage account required**. Such a session keeps nothing after it ends. Do not use a session with a file share: its `$HOME` lives in a storage account that can sit in any region, and `rm` does not reliably remove a file from the saved home image.
- **Check the region** before you download anything. This must print an EU region, for example `westeurope` or `germanywestcentral`:

  ```
  echo "$ACC_LOCATION"
  ```

  If it prints a region outside the EU, stop and ask the team.
- **Keep every file in one temporary folder outside `$HOME`**, removed when the shell exits, also when a step fails. Start every restore with:

  ```
  work=$(mktemp -d /tmp/argus-restore.XXXXXX)
  trap 'rm -rf "$work"' EXIT
  cd "$work"
  ```

  At the end, run `cd / && rm -rf "$work"` before you close the session.

### A. Any moment in the last 35 days

This needs the original server to still exist. If it was deleted, use B.

```
az postgres flexible-server restore --resource-group rg-argus \
  --name psql-argus-restore-<yyyymmdd> --source-server "$server" \
  --restore-time "<yyyy-mm-dd>T<hh:mm>:00+00:00"
```

The new server has the same Entra admins and settings as the original, and the ARGUS database keeps its name (the deployment output `databaseName`, usually `argus`). Continue with [Check the restore](#check-the-restore).

### B. From a long term backup

1. **A storage account for the files.** Azure Backup gives the backup back as files in a storage container. Make a temporary one, in Germany West Central, that allows the copy from Microsoft's backup storage and only Entra sign in:

   ```
   az storage account create --resource-group rg-argus --name strestore<yyyymmdd> \
     --location germanywestcentral --sku Standard_LRS --min-tls-version TLS1_2 \
     --allow-blob-public-access false --allow-shared-key-access false \
     --allow-cross-tenant-replication true
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
   ```

4. **Load the backup.** First do the sign in block at the top of [Check the restore](#check-the-restore). ARGUS needs only its own role `argus_app`, so create that by hand instead of running all of `roles.sql`. Use `pg_restore` of PostgreSQL 16, the server's version: a newer one writes settings that 16 refuses (`pg_restore --version` shows it). Then, in the temporary folder:

   ```
   psql --dbname postgres -c "create role argus_app nologin" -c "create database argus_restored"
   az storage blob download --account-name strestore<yyyymmdd> --container-name restore \
     --name <file of the argus database> --file "$work/argus.dump" --auth-mode login
   pg_restore --no-owner --exit-on-error --dbname argus_restored "$work/argus.dump"
   rm -f "$work/argus.dump"
   ```

   If `pg_restore` fails, the `trap` still removes the folder when the shell exits. `pg_restore` loads the data first and the triggers after it, so the audit events come back exactly as they were and the audit log is append only again right after.

### Check the restore

Sign in to the restored server from Cloud Shell. This is the same after A and B:

```
restored=psql-argus-restore-<yyyymmdd>
ip=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create --resource-group rg-argus \
  --server-name "$restored" --name cloud-shell --start-ip-address "$ip" --end-ip-address "$ip"
export PGHOST=$restored.postgres.database.azure.com PGSSLMODE=require
export PGUSER="<your sign in name>"
export PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv)
```

After A, `PGUSER` must be one of the Entra admins the original server had (Arita, or the name in `ARGUS_DATABASE_ADMIN_NAME`). The token lasts about an hour; run the last line again if a step says the password is wrong.

Get the newest nightly audit copies into the temporary folder, then run the check from a checkout of this repository (`pip install -e .` first). The database is `argus_restored` after B and the original name (usually `argus`) after A:

```
az storage blob download-batch --account-name <documents account> --source documents \
  --pattern "audit-log/<yyyy-mm>-*.jsonl" --destination "$work" --auth-mode login

ARGUS_DATABASE_URL="postgresql+psycopg://$PGHOST:5432/<database>?sslmode=require" \
  python tools/restore_check.py --audit-copy "$work"/audit-log/<yyyy-mm-dd>.jsonl [--audit-copy ...]
```

It only reads, and prints only numbers, times and hashes, so its output can go into the record below. It passes when:

- `audit_verify()` finds the hash chain whole;
- no chain number is missing;
- every event of the copies that the restore holds has exactly the hash in the copy.

Events in the copies that are newer than the backup are counted, not failed: that number is how much the restore lost since the backup was taken. Those events are still in the nightly copies.

When done, remove the temporary folder and delete the temporary server and storage account:

```
cd / && rm -rf "$work"
az postgres flexible-server delete --resource-group rg-argus --name "$restored" --yes
az storage account delete --resource-group rg-argus --name strestore<yyyymmdd> --yes
```

### If the running database is lost

Pointing ARGUS at another server is not automated. The templates name the server `psql-argus-<suffix>`, and the deploy expects that name.

- **The server still exists but its data is damaged:** restore as in A to a new name, check it, and ask the team for a PR that moves ARGUS over. Do not delete the damaged server: it is evidence, and its own backups still run.
- **The server is gone:** route A no longer works, because it restores from the server itself. Azure can bring back a deleted server only within a few days through its "restore a dropped server" route, which the delete lock is there to make unnecessary. So the normal path is B, under the old server name, then a check, then the deploy by hand (Actions, Deploy, Run workflow). The deploy maps the app identity again and runs any missing migrations.

**Before ARGUS runs again on a restored database, deal with the events the restore lost.** Those are the events the check counts as "newer than the backup". They exist only in the nightly copies, and the backup lost the cases and emails they belong to.

1. Write into the restore record: the backup used, the lost chain numbers (from the last chain number in the restore to the last one in the newest copy) and the copy files that hold them.
2. Keep ARGUS stopped (scale the app to zero, no deploy) until Arita decides how the lost window is handled.
3. Expect the nightly audit check (#83) to report that range as missing, and as changed once new events reuse those chain numbers. That is correct: it shows the gap in the trail.
4. Re importing the lost events, or letting the chain continue after the highest copied number, and sweeping the emails and documents of the lost window again, is not built yet. It needs its own story (see the PR for #82).

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

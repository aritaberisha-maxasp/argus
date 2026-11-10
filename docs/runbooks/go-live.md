# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

## Checks the team runs

- [ ] Mailbox sweep follow ups against the real mailbox (#90).
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72).

## One time steps for Arita in Azure

- [ ] **Switch demo mode off** (#124, [ADR 0009](../adr/0009-demo-mode-with-stand-ins.md)). In this order, because the stand ins run as the app identity, which can read every Key Vault secret:
  1. Delete by hand the container apps `ca-argus-standin-graph`, `ca-argus-standin-datev`, `ca-argus-standin-wise` and the job `caj-argus-demo-seed`. **Before** any real secret goes into Key Vault.
  2. One PR that sets `param demo = false` in `infra/main.bicepparam`. ARGUS refuses to start with both demo mode and real settings. After its deploy, delete the database `argus_demo`, the storage account tagged `role=demo` and the Key Vault secrets `argus-demo-documents-connection-string` (it holds that account's key), `argus-graph-standin-token` and `argus-wise-standin-token`.
  3. Only then the real Microsoft 365, DATEV and Wise secrets into Key Vault, and a PR with the real addresses and `manualSecrets`.

  Steps in [docs/runbooks/demo.md](demo.md).

- [ ] The one time setup in [docs/operations/deploy.md](../operations/deploy.md), up to a first green deploy (#81).
- [ ] Start the long term database backup: two roles for the backup vault, then `databaseBackupProtected = true` ([docs/operations/backup-and-restore.md](../operations/backup-and-restore.md), #82).
- [ ] One real restore of a long term backup, checked with `tools/restore_check.py` and written into the restore record of that runbook (#82).
- [ ] The retention lock switch in [infra/README.md](../../infra/README.md), after the tax advisor confirms the period. It locks the backup vault too (ADR 0007).

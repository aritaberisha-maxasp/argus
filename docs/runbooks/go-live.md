# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

## Checks the team runs

- [ ] Mailbox sweep follow ups against the real mailbox (#90).
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72).

## One time steps for Arita in Azure

- [ ] **Switch demo mode off** (#124, [ADR 0009](../adr/0009-demo-mode-with-stand-ins.md)). One PR that sets `param demo = false` in `infra/main.bicepparam` and adds the real Microsoft 365, DATEV and Wise addresses and secrets. ARGUS refuses to start if it gets both. After the deploy, delete by hand what the pipeline leaves behind: the container apps `ca-argus-standin-graph`, `ca-argus-standin-datev`, `ca-argus-standin-wise`, the job `caj-argus-demo-seed`, the database `argus_demo` and the storage account tagged `role=demo`. Steps in [docs/runbooks/demo.md](demo.md).

- [ ] The one time setup in [docs/operations/deploy.md](../operations/deploy.md), up to a first green deploy (#81).
- [ ] **Who Max ASP is, for the intake jobs** (#125). The GitHub variables `ARGUS_OWN_DOMAIN`, `ARGUS_INVOICING_AGENT`, `ARGUS_OWN_VAT_IDS`, `ARGUS_VENDOR_DOMAINS` and `ARGUS_CUSTOMER_DOMAINS` from [deploy.md](../operations/deploy.md), step 7. With demo mode off, the sweep refuses to start until the first three are set, and the alert says so.
- [ ] **Operations alerts reach Arita** (#125). `ARGUS_ALERT_EMAIL` is set, and **Test action group** on `ag-argus-ops` delivered an email.
- [ ] **The sweep signs in to the real mailbox** (#125, ADR 0003). The intake jobs need a Microsoft 365 sign in for the real mailbox before they can run live; until then they fail with "not set up" and the sweep alert fires. Keep `intakeJobs` on in `main.bicepparam` only once that is in.
- [ ] Start the long term database backup: two roles for the backup vault, then `databaseBackupProtected = true` ([docs/operations/backup-and-restore.md](../operations/backup-and-restore.md), #82).
- [ ] One real restore of a long term backup, checked with `tools/restore_check.py` and written into the restore record of that runbook (#82).
- [ ] The retention lock switch in [infra/README.md](../../infra/README.md), after the tax advisor confirms the period. It locks the backup vault too (ADR 0007).

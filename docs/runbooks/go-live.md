# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

- [ ] **Mail folder list for the shadow run.** The folders the person who sorts the accounts Inbox files emails into today, and what each one holds (incoming invoices, outgoing invoices, payment reminders, other). ARGUS uses it to see what the person did with each email during the two weeks (#72). Folder names may only use letters and spaces for now. Paths start at the top of the mailbox, so a subfolder of the Inbox is written with the Inbox's own name, for example `Posteingang/Rechnungen` in a German mailbox. Arita asks Max ASP for it.

## Checks the team runs

- [ ] **Graph paging link on the sandbox mailbox (#90).** Graph lists the Inbox 50 emails at a time. The tests use the link shape of our fake Graph and a few shapes real Graph is known to write, but only the real service shows which one it uses. Once the sandbox mailbox is connected (#65), the team puts at least 60 synthetic test emails in its Inbox and runs one sweep. Pass: the log line `Mailbox sweep: N in Inbox` shows the same number as the Inbox in Outlook, and there is no `refused Graph paging link` error. If that error shows up, it names the link's path: a developer adds that shape to `argus.intake.graph` with a test, and this check runs again before ARGUS reads the real accounts mailbox. While the link is refused, each sweep stops after the first page, so ARGUS also stops moving emails into its folders until the fix is deployed.
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72). ARGUS starts in shadow mode on its own. See [docs/design/shadow-mode.md](../design/shadow-mode.md).
  - [ ] The folder list below is set as `ARGUS_SHADOW_FOLDERS` before the two weeks start.
  - [ ] After two weeks Arita opens the shadow report (`GET /intake/shadow-report`). Both lists of emails ARGUS would have sent to Other (`invoices_to_other` and `not_listed_to_other`) must be empty, and `warnings` must be empty too: a warning means a list may be empty for the wrong reason, for example because the folder list is missing or incomplete. If not, the thresholds (`ARGUS_SORTING_THRESHOLD_*`) are raised and the run goes on.
  - [ ] Arita switches to live mode (`PUT /intake/mode` with `{"mode": "live", "reason": "<which report, what it showed>"}`). Only the admin role can, and the switch lands in the audit log.

## Settings in production

- [ ] **Invoice reading is switched on in the sweep.** The sweep reads the invoices of an email only when `Sorting.invoicing` is set; it is off by default. Without it no case is opened, and the safety gates that need the invoice data (failed plausibility checks, a PDF that disagrees with its XML, an e-invoice that cannot be read safely, an unreadable document) never fire. Flagged by the compliance review of PR 108 (#71). The team sets it in the production wiring and checks it in the shadow run (#72).
- [ ] **Max ASP's own VAT IDs are set once for both sorting and reading.** `SortingConfig.own_vat_ids` and `Invoicing.own_vat_ids` hold every VAT ID Max ASP prints on documents. The sweep refuses to start when the two lists differ. If the list is empty, supplier invoices that print Max ASP's VAT ID above their own go to a person instead of opening a case (#106).

## One time steps for Arita in Azure

- [ ] **Switch demo mode off** (#124, [ADR 0009](../adr/0009-demo-mode-with-stand-ins.md)). In this order, because the stand ins run as the app identity, which can read every Key Vault secret:
  1. Delete by hand the container apps `ca-argus-standin-graph`, `ca-argus-standin-datev`, `ca-argus-standin-wise` and the job `caj-argus-demo-seed`. **Before** any real secret goes into Key Vault.
  2. One PR that sets `param demo = false` in `infra/main.bicepparam`. ARGUS refuses to start with both demo mode and real settings. After its deploy, delete the database `argus_demo`, the storage account tagged `role=demo` and the Key Vault secrets `argus-demo-documents-connection-string` (it holds that account's key), `argus-graph-standin-token` and `argus-wise-standin-token`.
  3. Only then the real Microsoft 365, DATEV and Wise secrets into Key Vault, and a PR with the real addresses and `manualSecrets`.

  Steps in [docs/runbooks/demo.md](demo.md).

- [ ] The one time setup in [docs/operations/deploy.md](../operations/deploy.md), up to a first green deploy (#81).
- [ ] **Who Max ASP is, for the intake jobs** (#125). The GitHub variables `ARGUS_OWN_DOMAIN`, `ARGUS_INVOICING_AGENT`, `ARGUS_OWN_VAT_IDS`, `ARGUS_VENDOR_DOMAINS` and `ARGUS_CUSTOMER_DOMAINS` from [deploy.md](../operations/deploy.md), step 7. With demo mode off, the sweep refuses to start until the first three are set, and the alert says so.
- [ ] **Operations alerts reach Arita** (#125). `ARGUS_ALERT_EMAIL` is set, and **Test action group** on `ag-argus-ops` delivered an email.
- [ ] **The sweep signs in to the real mailbox** (#125, ADR 0003). The intake jobs need a Microsoft 365 sign in for the real mailbox before they can run live; until then they fail with "not set up" and the sweep alert fires. Keep `intakeJobs` on in `main.bicepparam` only once that is in.
- [ ] Start the long term database backup: two roles for the backup vault, then `databaseBackupProtected = true` ([docs/operations/backup-and-restore.md](../operations/backup-and-restore.md), #82).
- [ ] **The backup alerts are live** (#115). After the deploy with `databaseBackupProtected = true`: the alert processing rule `apr-bvault-...-to-ops` and the log alert `alert-bvault-...-no-backup` exist in `rg-argus`, the vault has the diagnostic setting `argus-backup-jobs`, and **Test action group** on `ag-argus-ops` delivered an email to Arita. After the first Sunday, Backup jobs shows a completed job and the Log Analytics table `AddonAzureBackupJobs` has its record with `JobOperation` = `Backup` and a `JobStatus` that starts with `Completed`, the values the "no backup" alert looks for. If Azure writes other values, the alert would fire every Monday: fix the query in `infra/modules/backup-alerts.bicep`.
- [ ] One real restore of a long term backup, checked with `tools/restore_check.py` and written into the restore record of that runbook (#82).
- [ ] Sign in with Microsoft: the ARGUS app registration, the three groups and the five settings in [docs/operations/sign-in.md](../operations/sign-in.md) (#23). The demo already needs them, so they are set up in step 7a of [deploy.md](../operations/deploy.md) (#133). At go live, check that the right people are in each group and that the approval page (#38) has its own redirect address.
- [ ] The retention lock switch in [infra/README.md](../../infra/README.md), after the tax advisor confirms the period. It locks the backup vault too (ADR 0007).

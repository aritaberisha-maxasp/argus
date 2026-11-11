# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

## Checks the team runs

- [ ] Mailbox sweep follow ups against the real mailbox (#90).
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72).

## Settings in production

- [ ] **Invoice reading is switched on in the sweep.** The sweep reads the invoices of an email only when `Sorting.invoicing` is set; it is off by default. Without it no case is opened, and the safety gates that need the invoice data (failed plausibility checks, a PDF that disagrees with its XML, an e-invoice that cannot be read safely, an unreadable document) never fire. Flagged by the compliance review of PR 108 (#71). The team sets it in the production wiring and checks it in the shadow run (#72).

## One time steps for Arita in Azure

- [ ] The one time setup in [docs/operations/deploy.md](../operations/deploy.md), up to a first green deploy (#81).
- [ ] Start the long term database backup: two roles for the backup vault, then `databaseBackupProtected = true` ([docs/operations/backup-and-restore.md](../operations/backup-and-restore.md), #82).
- [ ] One real restore of a long term backup, checked with `tools/restore_check.py` and written into the restore record of that runbook (#82).
- [ ] Sign in with Microsoft: the ARGUS app registration, the three groups and the five settings in [docs/operations/sign-in.md](../operations/sign-in.md) (#23). Needed before anyone uses the approval page (#38).
- [ ] The retention lock switch in [infra/README.md](../../infra/README.md), after the tax advisor confirms the period. It locks the backup vault too (ADR 0007).

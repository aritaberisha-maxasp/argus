# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

- [ ] **Mail folder list for the shadow run.** The folders the person who sorts the accounts Inbox files emails into today, and what each one holds (incoming invoices, outgoing invoices, payment reminders, other). ARGUS uses it to see what the person did with each email during the two weeks (#72). Folder names may only use letters and spaces for now. Arita asks Max ASP for it.

## Checks the team runs

- [ ] Mailbox sweep follow ups against the real mailbox (#90).
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72). ARGUS starts in shadow mode on its own. See [docs/design/shadow-mode.md](../design/shadow-mode.md).
  - [ ] The folder list below is set as `ARGUS_SHADOW_FOLDERS` before the two weeks start.
  - [ ] After two weeks Arita opens the shadow report (`GET /intake/shadow-report`). The list of invoices ARGUS would have sent to Other must be empty. If it is not, the thresholds (`ARGUS_SORTING_THRESHOLD_*`) are raised and the run goes on.
  - [ ] Arita switches to live mode (`PUT /intake/mode` with `{"mode": "live", "reason": "<which report, what it showed>"}`). Only the admin role can, and the switch lands in the audit log.

## One time steps for Arita in Azure

- [ ] The one time setup in [docs/operations/deploy.md](../operations/deploy.md), up to a first green deploy (#81).
- [ ] Sign in with Microsoft: the ARGUS app registration, the three groups and the five settings in [docs/operations/sign-in.md](../operations/sign-in.md) (#23). Needed before anyone uses the approval page (#38).
- [ ] The retention lock switch in [infra/README.md](../../infra/README.md), after the tax advisor confirms the period.

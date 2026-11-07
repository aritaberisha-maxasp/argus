# Go live runbook

Things that must be done before ARGUS works on the real accounts mailbox, the real DATEV books or real Wise payments. Each item names who does it and where it was decided. Tick an item only when it is really done.

## Data Max ASP must provide

- [ ] **Partner email domain list.** Max ASP sends one list of vendors and customers with their DATEV account number and the email domain they send from, for example `muster-buerobedarf.de`. ARGUS loads it once. Afterwards the exception queue tells a person when an invoice comes from a new domain, so it can be added. Without the list every partner counts as an unknown sender, which is safe but sends more invoices to manual checks. Decided by Arita on #70 (4 Nov 2026). Arita asks Max ASP for it.

## Checks the team runs

- [ ] **Graph paging link on the sandbox mailbox (#90).** Graph lists the Inbox 50 emails at a time. The tests use the link shape of our fake Graph and a few shapes real Graph is known to write, but only the real service shows which one it uses. Once the sandbox mailbox is connected (#65), the team puts at least 60 synthetic test emails in its Inbox and runs one sweep. Pass: the log line `Mailbox sweep: N in Inbox` shows the same number as the Inbox in Outlook, and there is no `refused Graph paging link` error. If that error shows up, it names the link's path: a developer adds that shape to `argus.intake.graph` with a test, and this check runs again before ARGUS reads the real accounts mailbox.
- [ ] Two weeks of shadow mode before ARGUS routes anything (#72).

## One time steps for Arita in Azure

- [ ] The one time setup and the retention lock switch in [infra/README.md](../../infra/README.md).

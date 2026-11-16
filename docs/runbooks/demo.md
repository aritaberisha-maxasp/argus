# Demo runbook

How Arita shows ARGUS in Azure with the stand ins: a fake accounts mailbox, a fake DATEV and a Wise stand in (#124). The design is in [ADR 0009](../adr/0009-demo-mode-with-stand-ins.md).

Everything in the demo is synthetic. ARGUS cannot reach the real mailbox, DATEV or Wise while demo mode is on: it refuses to start if a real address or secret is set. The Wise stand in cannot fund or release a payment, just like the real Wise for ARGUS.

## Once, before the first demo

1. **The one time Azure setup.** Follow [docs/operations/deploy.md](../operations/deploy.md) up to a first green run of the Deploy workflow. Nothing shows up in Azure before that.
2. **Check demo mode is on.** In [infra/main.bicepparam](../../infra/main.bicepparam) the line `param demo = true` switches it on. It is on today. A merge to `main` deploys ARGUS, the stand ins and the seed job.
3. **Check that sign in works** (#133). Open `https://<argus address>/queue` and sign in with your Max ASP account. You should see the exception queue (empty before the first seed). If the page answers 503 or Microsoft shows an error about the redirect address, see "When a deploy fails" in [deploy.md](../operations/deploy.md). Benjamin needs to be in the finance or admins group to see the queue ([sign-in.md](../operations/sign-in.md), step 2).
4. **Check the stand ins are there.** In the Azure portal, resource group `rg-argus`, you should see the container apps `ca-argus-standin-graph`, `ca-argus-standin-datev` and `ca-argus-standin-wise`, and the job `caj-argus-demo-seed`. They have no public address, on purpose.

## Before each demo

Start the demo from a clean state:

1. GitHub, **Actions**, workflow **Demo**, **Run workflow**.
2. Branch `main`, action **reset and seed**, then **Run workflow**.
3. Wait for the green tick, about 3 to 5 minutes.

The run empties the demo database, the demo document store and the stand ins, then puts the ten demo emails into the fake mailbox.

## During the demo

The fake mailbox holds ten emails, one for each thing ARGUS can show:

| Email | What it shows |
|---|---|
| E01 | Incoming invoice as a PDF |
| E07 | Incoming invoice as a paper scan |
| E08 | Incoming XRechnung |
| E09 | Incoming ZUGFeRD invoice |
| E17 | Outgoing invoice of Max ASP |
| E25 | Payment reminder |
| E15 | The invoice of E01 sent a second time |
| E49 | Invoice that fails the §14 UStG check: addressed to another company |
| E43 | Doubtful sender: SPF, DKIM and DMARC fail |
| E32 | Marketing mail from a known supplier |

ARGUS picks the emails up on its own: the job `caj-argus-sweep` runs every 5 minutes, so a seeded email is sorted and filed at the latest 5 minutes after the seed, without anyone starting anything (#125). To show it right away, start the job by hand in the portal: job `caj-argus-sweep`, **Run now**. The exception queue page shows what needs a person (#126). Until that one is merged, the demo shows the sorted emails in the fake mailbox folders.

Since shadow mode (#72), a fresh database starts in shadow mode: the sweep only proposes where each email should go and opens no cases. To show sorted emails and cases, a person switches the demo database to live mode before the demo, as #72 describes. A reset starts the demo database in shadow mode again.

### Show where ARGUS sorted the emails

The fake mailbox has no public address, so ARGUS has a read only page for it, in demo mode only (#135): open `https://<argus address>/mailbox` in the same browser tab where you signed in to the queue. If you open it in a new tab, it sends you through the Microsoft sign in once and then back to the mailbox. Benjamin needs the finance or admins group, as for the queue.

The page lists every folder of the fake mailbox, Inbox first, then the ARGUS folders (Incoming invoices, Outgoing invoices, Payment reminders, Manual review, Removed) and Deleted Items, with the subject, sender and received time of each email. It changes nothing in the mailbox. Press **Refresh** to read it again.

When to open it:

1. Right after **reset and seed**, before the sweep runs: all demo emails sit in the Inbox. This is the "before" picture.
2. After `caj-argus-sweep` has run (in live mode): the Inbox is empty or nearly so and each email is in its ARGUS folder. Point at the invoices in Incoming invoices, the reminder E25 in Payment reminders and the marketing mail E32 in Removed.
3. After **send one email** and a sweep: the new email moved from the Inbox into its folder.

The page and its data do not exist when demo mode is off, so at go live it disappears on its own. People then look at the real mailbox in Outlook.

### Show an email arriving

To show Benjamin an email coming in while you watch, drop one more test email into the fake mailbox (#136):

1. GitHub, **Actions**, workflow **Demo**, **Run workflow**.
2. Branch `main`, action **send one email**, and under **Email** pick the one to send, then **Run workflow**.
3. Wait for the green tick, about 1 to 2 minutes. The run summary says which email went in.

Try it once before the meeting, after the one time setup: run **send one email**, check the green tick, then **reset and seed** so the demo starts clean. Sending is new in Azure, so a problem shows up then and not in front of Benjamin.

The email lands in the Inbox of the fake mailbox. The sweep picks it up within 5 minutes; start `caj-argus-sweep` by hand to show it at once. All 50 synthetic test emails in `tests/fixtures/emails/` can be picked. [manifest.csv](../../tests/fixtures/emails/manifest.csv) says what each one is and what ARGUS should do with it. Good ones to show that are not in the ten above:

| Email | What it shows |
|---|---|
| E12 | Invoice from a vendor that is not in DATEV, so it goes to the exception queue (the default) |
| E14 | Three invoices in one email, so three cases |
| E05 | Incoming invoice over EUR 5,000 |
| E04 | English invoice in GBP from a UK vendor |
| E34 | Phishing email |

Pick an email that is not in the mailbox yet. Sending the same email twice adds no second copy, the same as seeding twice. Sending only works in demo mode: the workflow checks first that ARGUS runs in demo mode, and the command itself refuses unless demo mode is on and the mailbox is the fake one. It never reaches the real accounts mailbox. The **Email** choice does nothing for the other actions.

To show it again from the start, run the Demo workflow with **reset and seed** again. To add the demo emails once more without emptying anything, run it with **seed**. Seeding twice does no harm: the mailbox keeps one copy of each email.

## If something goes wrong

- **"Check that no deploy runs or waits" failed.** A merge is being deployed right now. Wait until the Deploy run has finished, then run the Demo workflow again.
- **"Check that Azure is set up" failed.** The one time setup is not finished. See step 1 above.
- **"Check that ARGUS runs in demo mode" failed.** Either demo mode is off in `main.bicepparam`, or the deploy has not run since it was switched on. Merge a change or run the Deploy workflow by hand.
- **"Empty the demo database" failed.** The log says why. "must end in _demo" means the app is not on the demo database, so nothing was changed.
- **"Send the chosen email to the fake mailbox" failed.** Like the seed, its own log is in the Azure portal: job `caj-argus-demo-seed`, Execution history, Console logs. "refused" there means demo mode is off or the mailbox setting is not the fake one, so nothing was sent.
- **"Put the demo emails into the fake mailbox" failed.** The seed job's own log is in the Azure portal: job `caj-argus-demo-seed`, Execution history, Console logs.

## After the demos: going live

Switching demo mode off is part of the [go live runbook](go-live.md). The order matters, since the stand ins run as the app identity, which can read every secret in Key Vault:

1. Delete the three stand in apps and the seed job, before any real secret goes into Key Vault:
   `az containerapp delete --resource-group rg-argus --name <name> --yes` for `ca-argus-standin-graph`, `ca-argus-standin-datev`, `ca-argus-standin-wise`, and `az containerapp job delete --resource-group rg-argus --name caj-argus-demo-seed --yes`.
2. A PR with `param demo = false` in `main.bicepparam`. After its deploy, delete the database `argus_demo`, the storage account tagged `role=demo` and the Key Vault secrets `argus-demo-documents-connection-string`, `argus-graph-standin-token` and `argus-wise-standin-token`. The pipeline never deletes anything, so this is by hand.
3. Then the real secrets and addresses, as the go live runbook says.

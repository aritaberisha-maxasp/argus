# 0001: Tech stack, hosting and data storage

Status: proposed
Requirements: NF-01, NF-03, NF-06 (also touches NF-04, BA-01, PY-02, PY-03, AP-05, EX-03)

## Context

Before anyone writes product code we need one agreed answer to five questions: which language and framework, where ARGUS runs, where documents and data live, how secrets are kept, and how the scheduled jobs start.

What constrains us:

- **EU only (NF-03).** Every byte of invoice, bank and personal data must be processed and stored in the EU.
- **GoBD (NF-01).** Original documents must be kept unaltered for the legal retention period, and every step must be traceable.
- **Vendor interfaces only (NF-06).** Microsoft 365 through Microsoft Graph, Wise through its API, DATEV through whatever DATEV supports (issue #2 finds out).
- **Max ASP already runs on Microsoft 365.** The mailbox, the approvers and Benjamin all have Microsoft accounts.
- **DATEV runs in house at Max ASP.** ARGUS may need a small piece running inside Max ASP's network to reach it. Issue #2 decides that.
- **Small volume, small team.** A few hundred documents a month. Agents build at night and Arita runs operations, so we want managed services with as little to patch as possible.
- **Cloud agents cannot reach Max ASP's systems yet.** Everything must run locally and in CI against stand ins.

## Options

### Where ARGUS runs

1. **Microsoft Azure, region Germany West Central (Frankfurt), in Max ASP's own Microsoft tenant.** Same sign in as Microsoft 365, managed identity to reach Graph and secrets without stored passwords, immutable blob storage for GoBD. Con: ties us to Azure services.
2. **AWS, region Frankfurt.** Equally capable, S3 Object Lock covers GoBD. Con: a second identity world next to Microsoft 365, so approver sign in and Graph access need extra setup and extra secrets.
3. **A German hoster such as Hetzner.** Cheapest. Con: we would run and patch the database, the secrets store and the backups ourselves, and no locked retention storage comes ready made.
4. **A workspace in Max ASP's existing Microsoft Fabric capacity.** Already paid for. Con: Fabric is an analytics platform. It cannot host the approval web app or receive Graph notifications, OneLake has no locked retention for GoBD, and notebooks and pipelines are a poor fit for transactional work such as approvals and payment runs. Fabric stays a good place for reporting on ARGUS data later.
5. **A server inside Max ASP next to DATEV.** Easy DATEV access. Con: Max ASP would carry patching, backups and uptime, and Graph webhooks need a public address anyway.

### Language and framework

1. **Python 3.12 with FastAPI.** The strongest libraries for what ARGUS mostly does: reading PDFs and scans, parsing XRechnung and ZUGFeRD, calling AI models, handling CSV. Easy for agents and reviewers to read. Con: weaker typing than C#, so we enforce type checks in CI.
2. **C# on .NET.** First class Azure support and strict typing. Con: thinner ecosystem for e-invoice parsing and document AI, more ceremony for a small team.
3. **TypeScript on Node.js.** Good for a web front end. Con: weaker libraries for PDF, OCR and e-invoice XML.

### Database

1. **PostgreSQL** (Azure Database for PostgreSQL Flexible Server). Open source, runs the same in Docker locally and in CI, strong JSON and constraint support for the case model and the audit log. Con: none that matters at our size.
2. **Azure SQL.** Fine, but harder to run locally and in CI, and locks us further into one vendor.

### Running scheduled jobs

1. **Azure Container Apps jobs on a fixed daily or frequent schedule, with the business calendar in our own code.** The scheduler only says "it is time to check". Whether today is a payment run day (the 15th, the 30th, the last day of February, the previous working day before a weekend or a Baden-Württemberg holiday) is decided in tested Python code. Con: the job starts on days when it then does nothing, which costs almost nothing.
2. **Encoding the run dates in cron expressions.** Con: cron cannot express "previous working day before a public holiday", and the Azure scheduler runs in UTC, so daylight saving would shift every job.
3. **Azure Functions timer triggers.** Con: a second runtime and packaging model next to the web app, and harder to test locally.

### Secrets

1. **Azure Key Vault, read through the app's managed identity.** No password to reach the vault, every read is logged. Con: none at our size.
2. **Environment variables set in the hosting platform.** Con: secrets become visible to anyone who can read the app configuration, and there is no access log.

## Decision

| Question | Choice | Main alternative we did not pick |
| --- | --- | --- |
| Language | Python 3.12, FastAPI for the web part, SQLAlchemy and Alembic for the database, Pydantic for data | C# on .NET, thinner document and e-invoice libraries |
| Hosting | Azure Container Apps in Germany West Central, in its own resource group in Max ASP's existing Azure subscription (the one that pays for Fabric) | AWS Frankfurt, a second identity world next to Microsoft 365 |
| Case data and audit log | Azure Database for PostgreSQL Flexible Server, Germany West Central | Azure SQL, hard to run locally and in CI |
| Original documents | Azure Blob Storage with a locked time based retention policy (WORM), geo redundant inside Germany | Keeping files in the database, no legal lock |
| Secrets | Azure Key Vault, read by managed identity | Plain environment variables |
| Scheduled jobs | Container Apps jobs on simple schedules, business calendar in code | Run dates written as cron expressions |
| Sign in for approvers and Benjamin | Microsoft Entra ID (their existing Microsoft 365 accounts), roles from Entra groups | Separate ARGUS user accounts |

### How original documents are kept (NF-01)

- Every email that is an invoice, credit note or payment reminder is stored exactly as received: the raw email file (.eml) and each attachment byte for byte, including the XML of e-invoices (NF-02). We never store only an extracted or converted copy.
- On arrival we compute a SHA-256 fingerprint of each file and record it on the case and in the audit log. Anyone can later prove the file is unchanged.
- The storage container has a **time based retention policy that, once locked, nobody can shorten or remove**, not Arita, not an admin, not Microsoft support. Files can be read but not changed or deleted until the period ends.
- **Retention period:** we propose 10 years for everything, counted from the end of the calendar year. The law requires 10 years for books and 8 years for booking vouchers such as invoices (§ 147 AO since 2025), so one 10 year rule is simple and always safe. Max ASP's tax advisor should confirm (question 2 below).
- The lock is switched on only in production and only after Arita confirms the period, because a locked policy can be extended later but never shortened.
- Emails classified as Other (IN-06) are **not** put in the locked store. They go to a deletion folder in the mailbox that is purged after 30 days, as the source document suggests.
- The storage is geo redundant between Germany West Central and Germany North, so a copy survives the loss of a whole data centre region and never leaves Germany.

### How the scheduled jobs run

All jobs run as Container Apps jobs from the same code base as the web app. Each one is safe to start twice: it checks what was already done before doing anything.

| Job | Schedule | What it decides in code |
| --- | --- | --- |
| Mailbox sweep (IN-01) | Every 5 minutes, as a backup to Graph change notifications | Picks up any email the webhook missed |
| Approval reminders (AP-05) | Daily, early morning | Who is past 3 or 5 working days |
| Payment run (PY-02, PY-03) | Daily, early morning | Is today the hand over day? If yes, build the batch, send Benjamin the summary (PY-09) and create the unfunded transfers in Wise |
| Wise bank import (BA-01) | Daily from the 1st to the 5th | Has last month's import completed? If not, try again |
| Exception digest (EX-03) | Daily, early morning | Send Benjamin the open exceptions |

The business calendar (weekends and Baden-Württemberg public holidays) lives in one tested module, used by the payment run, the reminders and the escalations.

### How secrets are kept (NF-06)

- The Microsoft Graph app registration, the Wise API token and any Wise signing key, and any DATEV credentials live in Key Vault. The app reads them through its managed identity. Nothing is stored in code, in the repo, or in plain app settings.
- Locally and in CI there are no real secrets at all: the code runs against Docker stand ins (PostgreSQL, Azurite for blob storage, fake Graph, fake Wise and fake DATEV) and a `.env.example` with placeholders.
- Arita puts the real values into Key Vault. Agents never see them.
- Deployments from GitHub use OpenID Connect federation to Azure, so no deploy password is stored in GitHub either. Deploying stays Arita's step.

### Rough monthly running cost

Azure list prices for Germany West Central, at our volume. These are estimates to check in the Azure pricing calculator once the subscription exists.

| Item | About per month |
| --- | --- |
| Container Apps (web app and jobs, mostly inside the free monthly allowance) | EUR 0 to 15 |
| PostgreSQL Flexible Server, smallest burstable size, 32 GB, 35 days of backups | EUR 20 to 30 |
| Blob Storage, a few GB, geo redundant | EUR 2 to 5 |
| Key Vault | under EUR 1 |
| Container Registry, Basic | about EUR 5 |
| Logs and monitoring (Log Analytics) | EUR 5 to 10 |
| **Total** | **about EUR 35 to 65** |

Not included: the cost of reading documents with AI or OCR (decided in issue #5, likely the largest item and usage based) and any machine at Max ASP for DATEV (issue #2). Microsoft 365 accounts for approvers already exist, so sign in adds no cost.

## Consequences

**Easier**
- Approvers and Benjamin sign in with the Microsoft account they already have, and roles come from Entra groups (NF-03).
- The app reaches Graph and Key Vault with its managed identity, so there are fewer secrets to rotate or leak.
- GoBD immutability comes from the storage platform, not from our own code.
- The same code and the same PostgreSQL run on a laptop, in CI and in Azure, so cloud agents can build and test everything before any real access exists.
- Payment run dates are plain Python with tests for every edge case (February, weekends, holidays).

**Harder**
- We depend on Azure. Moving later means replacing Container Apps, Key Vault and the blob retention lock, but the code itself stays portable because it only talks to them through thin adapters.
- A locked retention policy is permanent. A mistake in the period cannot be undone, which is why it is switched on only after Arita confirms.
- Graph change notifications need a public HTTPS address, so the web app is reachable from the internet. It accepts only signed Microsoft notifications and Entra sign ins.

**To watch**
- If issue #2 shows DATEV can only be reached from inside Max ASP's network, we add a small connector there that calls out to ARGUS. ARGUS never opens a way into Max ASP's network. That connector gets its own ADR.
- The AI and OCR service chosen in issue #5 must also process data in the EU.
- The Wise token must be scoped so ARGUS can create transfers but never fund them (NF-04). Issue #4 checks what Wise allows.
- How the audit log is made tamper evident inside PostgreSQL is decided in issue #7. It can also write a daily copy into the locked blob store.

## Questions for Arita (need Max ASP's approval)

Arita answered yes to all four on 15 Oct 2026. Arita also confirmed that ARGUS goes in its own resource group in the Azure subscription that already pays for Max ASP's Fabric capacity, with access limited to Arita. That capacity must be in an EU region.

1. **Azure subscription.** May ARGUS run in an Azure subscription owned by Max ASP, in its existing Microsoft tenant, in Germany West Central? Someone with admin rights at Max ASP has to create it and add a payment method.
2. **Retention.** May we lock original documents for 10 years, so that nobody can delete them before then? Max ASP's tax advisor should confirm the period.
3. **Budget.** Is about EUR 35 to 65 a month for hosting acceptable, plus the document reading cost from issue #5?
4. **Data processing agreement.** Max ASP's Microsoft agreement already includes Microsoft's data processing terms for Azure. Can Arita confirm Max ASP is fine relying on them, with the EU Data Boundary?

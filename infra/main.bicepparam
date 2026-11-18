// Production parameters for infra/main.bicep. No secret belongs in this file.
using 'main.bicep'

// Switching this to true locks the original documents for good: the period can then be
// raised but never lowered or removed. Only Arita changes this line, in a PR she merges
// herself, after the first deploy and after the tax advisor confirms the period.
param retentionLocked = false

// 10 years from the end of the calendar year the file arrives in, plus the longest run to
// year end with leap days (ADR 0001). Can be raised later, never lowered once locked.
param retentionDays = 4018

// Long term backup of the database (#82, ADR 0007): the first weekly backup of each month is
// kept this many years. 10 is the most Azure Backup allows. A change applies to new backups
// only. Once the vault is locked (it follows retentionLocked), the period can be raised but
// never lowered.
param databaseBackupYears = 10

// Switch to true in a PR of its own once Arita has given the backup vault's identity its two
// roles (docs/operations/backup-and-restore.md, step 2). Before that, the deploy would fail.
param databaseBackupProtected = false

// The identity that runs the migrations (#81). Not secret; the pipeline passes the real
// values as GitHub variables. The fallback lets CI build this file without Azure.
param databaseAdminObjectId = readEnvironmentVariable('ARGUS_DATABASE_ADMIN_OBJECT_ID', '00000000-0000-0000-0000-000000000000')
param databaseAdminName = readEnvironmentVariable('ARGUS_DATABASE_ADMIN_NAME', 'argus-deploy')

param aiLocation = 'germanywestcentral'

param modelDeployments = [
  {
    name: 'argus-sorting'
    model: 'gpt-4.1-mini'
    version: '2025-04-14'
    sku: 'DataZoneStandard'
    capacity: 20
  }
]

// Demo mode (#124, ADR 0009): ARGUS runs against the fake mailbox, the fake DATEV and the Wise
// stand in, with a database and a document store of its own. On for now, because the demo for
// Max ASP comes first. Switching it to false is a go live step (docs/runbooks/go-live.md),
// together with the real addresses and secrets. ARGUS refuses to start with both.
param demo = true

// Keep one copy of the web app running at all times. With 0, Azure stops the app after a few
// quiet minutes, and the next visit waits for it to start again (pull the image, start Python,
// fetch the database token and the Entra keys), which made the pages feel very slow. One
// always running copy of 0.5 CPU and 1 GiB costs a few euros a month while it is idle.
param appMinReplicas = 1

// The pipeline passes the argus-demo image when it deploys a new version (#124). Without it the
// running stand ins stay as they are.
param demoImage = readEnvironmentVariable('ARGUS_DEMO_IMAGE', '')

// The mailbox sweep every 5 minutes and the daily removal (#125). On, because the demo mailbox
// is connected while demo = true. At go live it stays on once the real mailbox settings and the
// intakeSettings below are in (docs/runbooks/go-live.md). The jobs never run with the
// placeholder image of the very first deploy.
param intakeJobs = true

// Who Max ASP is, for the sorting and the invoice reading (#125). Not secret, but company data,
// so the values come from GitHub variables (docs/operations/deploy.md), not from this file.
// Not used while demo = true, which has synthetic values. The jobs refuse to start live while
// ownDomain, invoicingAgent or ownVatIds is empty.
param intakeSettings = {
  ownDomain: readEnvironmentVariable('ARGUS_OWN_DOMAIN', '')
  invoicingAgent: readEnvironmentVariable('ARGUS_INVOICING_AGENT', '')
  ownVatIds: readEnvironmentVariable('ARGUS_OWN_VAT_IDS', '')
  vendorDomains: readEnvironmentVariable('ARGUS_VENDOR_DOMAINS', '')
  customerDomains: readEnvironmentVariable('ARGUS_CUSTOMER_DOMAINS', '')
}

// Sign in with Microsoft (#23, #133): the IDs of the ARGUS app registration and its three groups
// (docs/operations/sign-in.md). Not secret, but they come from GitHub variables
// (docs/operations/deploy.md, step 7) so this file stays the same for any tenant. Until they are
// set, ARGUS answers 503 on every route with data. ARGUS_ENTRA_AUTHORITY never goes to Azure.
param signInSettings = {
  tenantId: readEnvironmentVariable('ARGUS_ENTRA_TENANT_ID', '')
  apiClientId: readEnvironmentVariable('ARGUS_ENTRA_API_CLIENT_ID', '')
  approverGroupId: readEnvironmentVariable('ARGUS_ENTRA_GROUP_APPROVER', '')
  financeGroupId: readEnvironmentVariable('ARGUS_ENTRA_GROUP_FINANCE', '')
  adminGroupId: readEnvironmentVariable('ARGUS_ENTRA_GROUP_ADMIN', '')
}

// Who ARGUS emails, and who may email it Wise statements (#167, EX-03, BA-08): Benjamin's
// address at go live. Personal data, so the values come from GitHub secrets
// (docs/runbooks/go-live.md), never from this file; secrets so run logs show *** for them. Not used while demo = true, which has
// synthetic values. Once demo is false, tools/check_infra.py refuses an empty ARGUS_DIGEST_TO or
// ARGUS_REMINDER_FORWARD_TO, and the digest job refuses to run without its address.
param notifySettings = {
  digestTo: readEnvironmentVariable('ARGUS_DIGEST_TO', '')
  reminderForwardTo: readEnvironmentVariable('ARGUS_REMINDER_FORWARD_TO', '')
  statementSenders: readEnvironmentVariable('ARGUS_STATEMENT_SENDERS', '')
}

// Where operations alerts go, for example a failed sweep (#125). Arita's address lives in the
// GitHub secret ARGUS_ALERT_EMAIL, never in the repo (a secret, so run logs show *** for it, #167).
param alertEmail = readEnvironmentVariable('ARGUS_ALERT_EMAIL', '')

// Add WISE_API_TOKEN and the Graph secrets here once Arita has put them in Key Vault. Only
// with demo = false: tools/check_infra.py and ARGUS itself refuse real secrets in demo mode.
param manualSecrets = []

// The deploy pipeline (#81) sets these: first the running image, then the new one after the
// migrations. Without them CI builds this file with the public placeholder.
param appImage = readEnvironmentVariable('ARGUS_APP_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param appPort = int(readEnvironmentVariable('ARGUS_APP_PORT', '80'))

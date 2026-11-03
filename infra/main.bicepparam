// Production parameters for infra/main.bicep. No secret belongs in this file.
using 'main.bicep'

// Switching this to true locks the original documents for good: the period can then be
// raised but never lowered or removed. Only Arita changes this line, in a PR she merges
// herself, after the first deploy and after the tax advisor confirms the period.
param retentionLocked = false

// 10 years from the end of the calendar year the file arrives in, plus the longest run to
// year end with leap days (ADR 0001). Can be raised later, never lowered once locked.
param retentionDays = 4018

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

// Add WISE_API_TOKEN and the Graph secrets here once Arita has put them in Key Vault.
param manualSecrets = []

param appImage = readEnvironmentVariable('ARGUS_APP_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param appPort = int(readEnvironmentVariable('ARGUS_APP_PORT', '80'))

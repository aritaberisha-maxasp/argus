// ARGUS in Azure (ADR 0001, ADR 0006, issue #65).
//
// Deployed into ARGUS's own resource group. The pipeline from #81 applies this on every merge
// to main. The one time steps and the retention lock switch stay Arita's (infra/README.md).
// No secret appears here: secrets live in Key Vault and the app reads them through its
// managed identity (NF-06).

targetScope = 'resourceGroup'

import { intakeSettingValues, modelDeployment, manualSecret, notifySettingValues, signInSettingValues } from 'types.bicep'

@description('Every ARGUS resource runs in Germany West Central (ADR 0001, NF-03).')
@allowed([
  'germanywestcentral'
])
param location string = 'germanywestcentral'

@description('EU region for Azure OpenAI and Document Intelligence (ADR 0006). Only EU regions are allowed here and by the Azure Policy.')
@allowed([
  'germanywestcentral'
  'swedencentral'
  'francecentral'
  'westeurope'
  'northeurope'
  'polandcentral'
  'italynorth'
  'spaincentral'
])
param aiLocation string = 'germanywestcentral'

@description('Short prefix for every resource name.')
@minLength(3)
@maxLength(10)
param prefix string = 'argus'

@description('Days every original document stays unchangeable. 4018 days covers 10 years counted from the end of the calendar year (ADR 0001, § 147 (4) AO).')
@minValue(4018)
param retentionDays int = 4018

@description('Lock the document retention policy for good. Off by default. Only Arita switches it on (infra/README.md).')
param retentionLocked bool = false

@description('Years the monthly long term backup of the database is kept (#82, ADR 0007). 10 is the most Azure Backup allows and the default until the tax advisor confirms the period.')
@minValue(1)
@maxValue(10)
param databaseBackupYears int = 10

@description('Start the long term backup of the database. Switch on only after the backup vault identity has its two roles (docs/operations/backup-and-restore.md).')
param databaseBackupProtected bool = false

@description('Entra object ID of the database admin, the identity that runs the migrations (#81).')
@minLength(36)
@maxLength(36)
param databaseAdminObjectId string

@description('Display name of the database admin in Entra ID.')
param databaseAdminName string

@allowed([
  'ServicePrincipal'
  'Group'
  'User'
])
param databaseAdminType string = 'ServicePrincipal'

@description('Model deployments in Azure OpenAI. EU deployment types only (ADR 0006).')
@minLength(1)
param modelDeployments modelDeployment[]

@description('Secrets Arita puts in Key Vault by hand (Graph, Wise). Add one here only after the secret exists in the vault.')
param manualSecrets manualSecret[] = []

@description('Container image of the app. The pipeline (#81) passes the image to run; the default is a public placeholder for the very first deploy.')
param appImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Port the app listens on. The placeholder uses 80, the ARGUS image 8000 (Dockerfile).')
param appPort int = 80

@description('Run ARGUS against the stand ins for the demo (#124, ADR 0009): its own database and document store, the fake mailbox, the fake DATEV and the Wise stand in. Off by default; main.bicepparam switches it.')
param demo bool = false

@description('The argus-demo image with the stand ins. The pipeline passes it when it deploys a new version; empty leaves the running stand ins as they are.')
param demoImage string = ''

@description('Run the mailbox sweep every 5 minutes and the daily removal as Container Apps jobs (#125). Switch on only once a mailbox is connected: the fake one in demo mode, the real one at go live. The jobs never run with the placeholder image.')
param intakeJobs bool = false

@description('Who Max ASP is, for the sorting and the invoice reading (#125). Not used in demo mode, which has synthetic values.')
param intakeSettings intakeSettingValues = {
  ownDomain: ''
  invoicingAgent: ''
  ownVatIds: ''
  vendorDomains: ''
  customerDomains: ''
}

@description('Where the operations alerts go, for example a failed sweep (#125). Comes from the GitHub variable ARGUS_ALERT_EMAIL, never from the repo. Empty: the alerts still fire in Azure but email nobody.')
param alertEmail string = ''

@description('The IDs for sign in with Microsoft (#23, #133). Come from GitHub variables, never from the repo. Empty: ARGUS answers 503 on every route with data until they are set.')
param signInSettings signInSettingValues = {
  tenantId: ''
  apiClientId: ''
  approverGroupId: ''
  financeGroupId: ''
  adminGroupId: ''
}

@description('Who ARGUS emails, and who may email it Wise statements (#167). Come from GitHub variables, never from the repo. Not used in demo mode, which has synthetic values. With demo off, tools/check_infra.py refuses an empty digestTo or reminderForwardTo.')
param notifySettings notifySettingValues = {
  digestTo: ''
  reminderForwardTo: ''
  statementSenders: ''
}

@minValue(0)
param appMinReplicas int = 0

@minValue(1)
param appMaxReplicas int = 1

var suffix = uniqueString(resourceGroup().id)
var tags = {
  app: 'argus'
  owner: 'max-asp-finance'
  adr: '0001'
}

// Key Vault secret names. The app sees them as the environment variables set in apps.bicep.
var documentsSecretName = 'argus-documents-connection-string'
var openAiKeySecretName = 'azure-openai-api-key'
var documentIntelligenceKeySecretName = 'azure-document-intelligence-api-key'

// Demo mode (#124, ADR 0009). The real database and the locked document store stay empty
// while ARGUS runs against the stand ins.
var demoDocumentsSecretName = 'argus-demo-documents-connection-string'
var graphStandinTokenSecretName = 'argus-graph-standin-token'
var wiseStandinTokenSecretName = 'argus-wise-standin-token'
var standinNames = {
  graph: 'ca-${prefix}-standin-graph'
  datev: 'ca-${prefix}-standin-datev'
  wise: 'ca-${prefix}-standin-wise'
  seed: 'caj-${prefix}-demo-seed'
}
var demoMailbox = 'accounts@maxasp.example'

// The scheduled intake jobs (#125). They run the ARGUS image, so never with the public
// placeholder of the very first deploy (appImage's default).
var runIntakeJobs = intakeJobs && appImage != 'mcr.microsoft.com/k8se/quickstart:latest'
var intakeJobNames = {
  sweep: 'caj-${prefix}-sweep'
  removal: 'caj-${prefix}-removal'
  digest: 'caj-${prefix}-digest'
}

// The nightly audit copy (#103). It needs only the database and the document store, so it runs
// whenever the ARGUS image does: live, and in demo mode against the demo database and store.
var runAuditJob = appImage != 'mcr.microsoft.com/k8se/quickstart:latest'
var auditJobName = 'caj-${prefix}-audit-copy'

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-app'
  location: location
  tags: tags
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    name: 'log-${prefix}'
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    name: take('cr${prefix}${suffix}', 50)
    tags: tags
    appPrincipalId: appIdentity.properties.principalId
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    name: take('kv-${prefix}-${suffix}', 24)
    tags: tags
    appPrincipalId: appIdentity.properties.principalId
  }
}

module documents 'modules/documents.bicep' = {
  name: 'documents'
  params: {
    location: location
    name: take('st${prefix}docs${suffix}', 24)
    tags: tags
    keyVaultName: keyVault.outputs.name
    retentionDays: retentionDays
    retentionLocked: retentionLocked
    connectionStringSecretName: documentsSecretName
  }
}

module database 'modules/database.bicep' = {
  name: 'database'
  params: {
    location: location
    name: 'psql-${prefix}-${suffix}'
    tags: tags
    adminObjectId: databaseAdminObjectId
    adminName: databaseAdminName
    adminType: databaseAdminType
    demo: demo
  }
}

module demoStore 'modules/demo.bicep' = if (demo) {
  name: 'demo'
  params: {
    location: location
    name: take('st${prefix}demo${suffix}', 24)
    tags: tags
    keyVaultName: keyVault.outputs.name
    documentsSecretName: demoDocumentsSecretName
    graphTokenSecretName: graphStandinTokenSecretName
    wiseTokenSecretName: wiseStandinTokenSecretName
  }
}

module backup 'modules/backup.bicep' = {
  name: 'backup'
  params: {
    location: location
    name: 'bvault-${prefix}-${suffix}'
    tags: tags
    serverId: database.outputs.id
    serverName: database.outputs.name
    retentionYears: databaseBackupYears
    retentionLocked: retentionLocked
    protectDatabase: databaseBackupProtected
  }
}

// Alerts on the long term backups (#115): a failed backup job, or no backup for 8 days. They
// go to the operations action group, and only once the backups are switched on.
module backupAlerts 'modules/backup-alerts.bicep' = {
  name: 'backup-alerts'
  params: {
    location: location
    tags: tags
    vaultName: backup.outputs.name
    workspaceId: monitoring.outputs.id
    actionGroupId: alerts.outputs.actionGroupId
    watchBackups: databaseBackupProtected
  }
}

module ai 'modules/ai.bicep' = {
  name: 'ai'
  params: {
    location: aiLocation
    openAiName: 'oai-${prefix}-${suffix}'
    documentIntelligenceName: 'di-${prefix}-${suffix}'
    tags: tags
    deployments: modelDeployments
    keyVaultName: keyVault.outputs.name
    openAiKeySecretName: openAiKeySecretName
    documentIntelligenceKeySecretName: documentIntelligenceKeySecretName
  }
}

module apps 'modules/apps.bicep' = {
  name: 'apps'
  params: {
    location: location
    environmentName: 'cae-${prefix}'
    appName: 'ca-${prefix}-web'
    tags: tags
    logAnalyticsWorkspaceName: monitoring.outputs.name
    identityId: appIdentity.id
    identityClientId: appIdentity.properties.clientId
    identityName: appIdentity.name
    registryLoginServer: registry.outputs.loginServer
    keyVaultUri: keyVault.outputs.uri
    image: appImage
    targetPort: appPort
    minReplicas: appMinReplicas
    maxReplicas: appMaxReplicas
    databaseHost: database.outputs.fqdn
    databaseName: database.outputs.databaseName
    documentsSecretName: documentsSecretName
    openAiKeySecretName: openAiKeySecretName
    documentIntelligenceKeySecretName: documentIntelligenceKeySecretName
    openAiEndpoint: ai.outputs.openAiEndpoint
    openAiDeploymentName: modelDeployments[0].name
    documentIntelligenceEndpoint: ai.outputs.documentIntelligenceEndpoint
    manualSecrets: manualSecrets
    demo: demo
    demoDocumentsSecretName: demoDocumentsSecretName
    graphStandinTokenSecretName: graphStandinTokenSecretName
    wiseStandinTokenSecretName: wiseStandinTokenSecretName
    standinNames: standinNames
    demoMailbox: demoMailbox
    runIntakeJobs: runIntakeJobs
    intakeJobNames: intakeJobNames
    runAuditJob: runAuditJob
    auditJobName: auditJobName
    intakeSettings: intakeSettings
    signInSettings: signInSettings
    notifySettings: notifySettings
  }
  // The secrets must exist in Key Vault before the app revision reads them.
  dependsOn: [
    documents
    demoStore
  ]
}

module standins 'modules/standins.bicep' = if (demo && !empty(demoImage)) {
  name: 'standins'
  params: {
    location: location
    tags: tags
    environmentId: apps.outputs.environmentId
    identityId: appIdentity.id
    registryLoginServer: registry.outputs.loginServer
    keyVaultUri: keyVault.outputs.uri
    image: demoImage
    graphName: standinNames.graph
    datevName: standinNames.datev
    wiseName: standinNames.wise
    seedJobName: standinNames.seed
    graphTokenSecretName: graphStandinTokenSecretName
    wiseTokenSecretName: wiseStandinTokenSecretName
    mailbox: demoMailbox
  }
  dependsOn: [
    demoStore
  ]
}

module alerts 'modules/alerts.bicep' = {
  name: 'alerts'
  params: {
    location: location
    tags: tags
    actionGroupName: 'ag-${prefix}-ops'
    alertEmail: alertEmail
    workspaceId: monitoring.outputs.id
    watchIntakeJobs: runIntakeJobs
    intakeJobNames: intakeJobNames
    watchAuditJob: runAuditJob
    auditJobName: auditJobName
  }
  // The rules watch the logs of the jobs, so the jobs come first.
  dependsOn: [
    apps
  ]
}

output appIdentityPrincipalId string = appIdentity.properties.principalId
output appIdentityName string = appIdentity.name
output registryLoginServer string = registry.outputs.loginServer
output keyVaultName string = keyVault.outputs.name
output databaseHost string = database.outputs.fqdn
output databaseServerName string = database.outputs.name
output databaseName string = database.outputs.databaseName
output registryName string = registry.outputs.name
output appName string = apps.outputs.appName
output documentsAccountName string = documents.outputs.name
output backupVaultName string = backup.outputs.name
output backupVaultPrincipalId string = backup.outputs.principalId
output appUrl string = apps.outputs.appUrl
output demo bool = demo
output demoDocumentsAccountName string = demo ? demoStore.outputs.accountName : ''
output demoSeedJobName string = standinNames.seed
output intakeJobs bool = runIntakeJobs
output auditJob bool = runAuditJob

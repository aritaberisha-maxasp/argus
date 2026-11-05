// ARGUS in Azure (ADR 0001, ADR 0006, issue #65).
//
// Deployed into ARGUS's own resource group. The pipeline from #81 applies this on every merge
// to main. The one time steps and the retention lock switch stay Arita's (infra/README.md).
// No secret appears here: secrets live in Key Vault and the app reads them through its
// managed identity (NF-06).

targetScope = 'resourceGroup'

import { modelDeployment, manualSecret } from 'types.bicep'

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
  }
  // The secrets must exist in Key Vault before the app revision reads them.
  dependsOn: [
    documents
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
output appUrl string = apps.outputs.appUrl

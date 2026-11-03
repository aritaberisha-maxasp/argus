// Key Vault for every secret ARGUS needs (ADR 0001, NF-06).
// Access is through Azure roles only, never access policies. The app identity may read
// secrets. Nobody's password or key appears here: the vault starts empty, the secrets that
// come from other Azure resources are written by documents.bicep and ai.bicep, and Arita
// puts the Microsoft Graph and Wise secrets in by hand (infra/README.md).

param location string
param name string
param tags object

@description('Principal ID of the app identity, which may read secrets.')
param appPrincipalId string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

// Built in role "Key Vault Secrets User": read secret values, nothing else.
var secretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource appCanReadSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, appPrincipalId, secretsUserRoleId)
  scope: vault
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: secretsUserRoleId
  }
}

output id string = vault.id
output name string = vault.name
output uri string = vault.properties.vaultUri

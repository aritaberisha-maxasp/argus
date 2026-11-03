// Container Registry for the ARGUS image (ADR 0001). No admin user: the app pulls with its
// managed identity, and the pipeline from #81 pushes with its federated identity.

param location string
param name string
param tags object

@description('Principal ID of the app identity, which gets pull rights only.')
param appPrincipalId string

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// Built in role "AcrPull".
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource appCanPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

output id string = registry.id
output name string = registry.name
output loginServer string = registry.properties.loginServer

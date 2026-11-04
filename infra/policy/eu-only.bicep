// Guard rails for the ARGUS resource group (ADR 0001, ADR 0006, NF-03).
//
// Arita deploys this once, at subscription scope, as part of the one time setup
// (infra/README.md). The pipeline from #81 has no right to change policies, so these rules
// hold even if someone edits main.bicep later:
//   1. Every resource in the group sits in an allowed EU region.
//   2. Azure OpenAI and Document Intelligence accounts sit in an EU region.
//   3. Model deployments only use EU deployment types. Global types are refused.

targetScope = 'subscription'

@description('Name of the ARGUS resource group.')
param resourceGroupName string

@description('Regions resources in the ARGUS group may use.')
param allowedLocations array = [
  'germanywestcentral'
]

@description('EU regions Azure OpenAI and Document Intelligence may use (ADR 0006).')
param allowedAiLocations array = [
  'germanywestcentral'
  'swedencentral'
  'francecentral'
  'westeurope'
  'northeurope'
  'polandcentral'
  'italynorth'
  'spaincentral'
]

@description('Deployment types allowed for model deployments. Must match euDeploymentSku in infra/types.bicep.')
param allowedDeploymentSkus array = [
  'DataZoneStandard'
  'Standard'
]

resource aiLocations 'Microsoft.Authorization/policyDefinitions@2023-04-01' = {
  name: 'argus-ai-accounts-in-eu'
  properties: {
    displayName: 'ARGUS: AI accounts only in EU regions'
    description: 'Refuses Azure OpenAI and other Azure AI accounts outside the allowed EU regions (ADR 0006, NF-03).'
    policyType: 'Custom'
    mode: 'Indexed'
    parameters: {
      allowedLocations: {
        type: 'Array'
      }
    }
    policyRule: {
      if: {
        allOf: [
          {
            field: 'type'
            equals: 'Microsoft.CognitiveServices/accounts'
          }
          {
            field: 'location'
            notIn: '[parameters(\'allowedLocations\')]'
          }
        ]
      }
      then: {
        effect: 'deny'
      }
    }
  }
}

resource deploymentTypes 'Microsoft.Authorization/policyDefinitions@2023-04-01' = {
  name: 'argus-ai-eu-deployment-types'
  properties: {
    displayName: 'ARGUS: model deployments only as Data Zone EU or regional'
    description: 'Refuses every model deployment type outside the allowed list, so Global deployment types that may process prompts outside the EU are never used (ADR 0006, NF-03).'
    policyType: 'Custom'
    mode: 'All'
    parameters: {
      allowedSkus: {
        type: 'Array'
      }
    }
    policyRule: {
      if: {
        allOf: [
          {
            field: 'type'
            equals: 'Microsoft.CognitiveServices/accounts/deployments'
          }
          {
            field: 'Microsoft.CognitiveServices/accounts/deployments/sku.name'
            notIn: '[parameters(\'allowedSkus\')]'
          }
        ]
      }
      then: {
        effect: 'deny'
      }
    }
  }
}

module assignments 'eu-only-assignments.bicep' = {
  name: 'argus-eu-only-assignments'
  scope: resourceGroup(resourceGroupName)
  params: {
    allowedLocations: allowedLocations
    allowedAiLocations: allowedAiLocations
    allowedDeploymentSkus: allowedDeploymentSkus
    aiLocationsDefinitionId: aiLocations.id
    deploymentTypesDefinitionId: deploymentTypes.id
  }
}

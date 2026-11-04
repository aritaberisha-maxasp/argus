// Assigns the EU rules from eu-only.bicep to the ARGUS resource group.

targetScope = 'resourceGroup'

param allowedLocations array
param allowedAiLocations array
param allowedDeploymentSkus array
param aiLocationsDefinitionId string
param deploymentTypesDefinitionId string

// Built in policy "Allowed locations".
var allowedLocationsDefinitionId = tenantResourceId(
  'Microsoft.Authorization/policyDefinitions',
  'e56962a6-4747-49cd-b67b-bf8b01975c4c'
)

resource locations 'Microsoft.Authorization/policyAssignments@2024-04-01' = {
  name: 'argus-allowed-locations'
  properties: {
    displayName: 'ARGUS: resources only in the allowed regions'
    policyDefinitionId: allowedLocationsDefinitionId
    enforcementMode: 'Default'
    parameters: {
      // The AI accounts may sit in another EU region; the AI rule below narrows them.
      listOfAllowedLocations: {
        value: union(allowedLocations, allowedAiLocations)
      }
    }
  }
}

resource aiLocations 'Microsoft.Authorization/policyAssignments@2024-04-01' = {
  name: 'argus-ai-accounts-in-eu'
  properties: {
    displayName: 'ARGUS: AI accounts only in EU regions'
    policyDefinitionId: aiLocationsDefinitionId
    enforcementMode: 'Default'
    parameters: {
      allowedLocations: {
        value: allowedAiLocations
      }
    }
  }
}

resource deploymentTypes 'Microsoft.Authorization/policyAssignments@2024-04-01' = {
  name: 'argus-ai-eu-deployment-types'
  properties: {
    displayName: 'ARGUS: model deployments only as Data Zone EU or regional'
    policyDefinitionId: deploymentTypesDefinitionId
    enforcementMode: 'Default'
    parameters: {
      allowedSkus: {
        value: allowedDeploymentSkus
      }
    }
  }
}

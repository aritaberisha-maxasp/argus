// Azure OpenAI and Azure AI Document Intelligence (ADR 0006).
// Both accounts sit in an EU region, and model deployments may only use the EU deployment
// types from types.bicep. The Azure Policy in infra/policy/ refuses anything else, so nobody
// can switch to a Global deployment type later.

import { modelDeployment } from '../types.bicep'

@description('EU region for both accounts.')
param location string

param openAiName string
param documentIntelligenceName string
param tags object

param deployments modelDeployment[]

@description('Key Vault that receives the two API keys the app reads.')
param keyVaultName string

param openAiKeySecretName string
param documentIntelligenceKeySecretName string

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: openAiName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
    // The app still signs in with an API key from Key Vault (src/argus/sorting/language_model.py).
    disableLocalAuth: false
  }
}

// Azure handles one model deployment per account at a time.
@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [
  for deployment in deployments: {
    parent: openAi
    name: deployment.name
    sku: {
      name: deployment.sku
      capacity: deployment.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: deployment.model
        version: deployment.version
      }
      versionUpgradeOption: 'NoAutoUpgrade'
    }
  }
]

resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: documentIntelligenceName
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: documentIntelligenceName
    publicNetworkAccess: 'Enabled'
    // The app still signs in with an API key from Key Vault (src/argus/reading/document_intelligence.py).
    disableLocalAuth: false
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// Both keys go straight into Key Vault, so they never show up in code, a parameter or an output.
resource openAiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: openAiKeySecretName
  properties: {
    value: openAi.listKeys().key1
  }
}

resource documentIntelligenceKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: documentIntelligenceKeySecretName
  properties: {
    value: documentIntelligence.listKeys().key1
  }
}

output openAiEndpoint string = openAi.properties.endpoint
output documentIntelligenceEndpoint string = documentIntelligence.properties.endpoint

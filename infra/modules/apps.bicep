// The Container Apps environment and the ARGUS web app (ADR 0001).
//
// The scheduled jobs from ADR 0001 (mailbox sweep, reminders, payment run, bank import,
// exception digest) run in this environment as Container Apps jobs. Each job story adds its
// job here once the app has a command for it; today none has one yet.
//
// Every secret comes from Key Vault through the app's managed identity. The image is a public
// placeholder until the pipeline from #81 builds and deploys the real one.

import { manualSecret } from '../types.bicep'

param location string
param environmentName string
param appName string
param tags object

param logAnalyticsWorkspaceName string

@description('Resource ID of the user assigned identity the app runs as.')
param identityId string

@description('Client ID of that identity, so the Azure SDK picks the right one.')
param identityClientId string

param registryLoginServer string
param keyVaultUri string

param image string
param targetPort int
param minReplicas int
param maxReplicas int

param databaseHost string
param databaseName string

param documentsSecretName string
param openAiKeySecretName string
param documentIntelligenceKeySecretName string
param openAiEndpoint string
param openAiDeploymentName string
param documentIntelligenceEndpoint string

param manualSecrets manualSecret[]

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

// Secrets that ARGUS's own infrastructure fills in, and the environment variable each one
// becomes. The names on the left match what the code reads (src/argus/secrets.py callers).
var generatedSecrets = [
  {
    secretName: documentsSecretName
    envName: 'ARGUS_DOCUMENTS_CONNECTION_STRING'
  }
  {
    secretName: openAiKeySecretName
    envName: 'AZURE_OPENAI_API_KEY'
  }
  {
    secretName: documentIntelligenceKeySecretName
    envName: 'AZURE_DOCUMENT_INTELLIGENCE_API_KEY'
  }
]

var allSecrets = concat(generatedSecrets, manualSecrets)

var plainSettings = [
  {
    name: 'AZURE_CLIENT_ID'
    value: identityClientId
  }
  {
    // No password: the app signs in as argus_app with an Entra token (#81).
    name: 'ARGUS_DATABASE_URL'
    value: 'postgresql+psycopg://argus_app@${databaseHost}:5432/${databaseName}?sslmode=require'
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: openAiEndpoint
  }
  {
    name: 'AZURE_OPENAI_DEPLOYMENT'
    value: openAiDeploymentName
  }
  {
    name: 'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT'
    value: documentIntelligenceEndpoint
  }
]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        // Graph change notifications need a public HTTPS address (ADR 0001).
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryLoginServer
          identity: identityId
        }
      ]
      secrets: [
        for secret in allSecrets: {
          name: toLower(secret.secretName)
          keyVaultUrl: '${keyVaultUri}secrets/${secret.secretName}'
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'argus'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            plainSettings,
            map(allSecrets, secret => {
              name: secret.envName
              secretRef: toLower(secret.secretName)
            })
          )
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

output environmentId string = environment.id
output appName string = app.name
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'

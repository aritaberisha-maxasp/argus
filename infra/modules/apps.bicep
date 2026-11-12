// The Container Apps environment and the ARGUS web app (ADR 0001).
//
// The scheduled jobs from ADR 0001 (mailbox sweep, reminders, payment run, bank import,
// exception digest) run in this environment as Container Apps jobs. Each job story adds its
// job here once the app has a command for it; today none has one yet.
//
// Every secret comes from Key Vault through the app's managed identity. The pipeline from #81
// builds the ARGUS image and passes it in. The very first deploy starts with a public
// placeholder, because the registry for the real image does not exist yet.

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

@description('Name of that identity. The pipeline maps it to a database role of the same name (#81).')
param identityName string

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

@description('Demo mode (#124, ADR 0009): ARGUS talks to the stand ins and keeps documents in the demo store.')
param demo bool

param demoDocumentsSecretName string
param graphStandinTokenSecretName string
param wiseStandinTokenSecretName string

@description('Names of the stand in apps and the seed job, from main.bicep.')
param standinNames object

@description('The fake accounts mailbox, a reserved .example address (RFC 2606).')
param demoMailbox string

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
// In demo mode the documents go to the demo store, never to the locked one (ADR 0009).
var generatedSecrets = concat(
  [
    {
      secretName: demo ? demoDocumentsSecretName : documentsSecretName
      envName: 'ARGUS_DOCUMENTS_CONNECTION_STRING'
    }
  ],
  aiSecrets,
  demo ? standinSecrets : []
)

var standinSecrets = [
  {
    secretName: graphStandinTokenSecretName
    envName: 'ARGUS_GRAPH_STANDIN_TOKEN'
  }
  {
    secretName: wiseStandinTokenSecretName
    envName: 'ARGUS_WISE_STANDIN_TOKEN'
  }
]

var aiSecrets = [
  {
    secretName: openAiKeySecretName
    envName: 'AZURE_OPENAI_API_KEY'
  }
  {
    secretName: documentIntelligenceKeySecretName
    envName: 'AZURE_DOCUMENT_INTELLIGENCE_API_KEY'
  }
]

// Where ARGUS finds Microsoft 365, DATEV and Wise (src/argus/connections.py). In demo mode it is
// the stand ins inside this environment, called by name over plain http. The app refuses to
// start if a real address or secret is set next to them, and the other way round. The real
// addresses come with go live (docs/runbooks/go-live.md).
var demoSettings = [
  {
    name: 'ARGUS_GRAPH_URL'
    value: 'http://${standinNames.graph}/v1.0'
  }
  {
    name: 'ARGUS_MAILBOX'
    value: demoMailbox
  }
  {
    name: 'ARGUS_REMINDER_FORWARD_TO'
    value: 'reminders@maxasp.example'
  }
  {
    name: 'ARGUS_DATEV_URL'
    value: 'http://${standinNames.datev}'
  }
  {
    // The client the fake DATEV serves (docker/demo/Dockerfile).
    name: 'ARGUS_DATEV_CLIENT'
    value: 'muster-gmbh'
  }
  {
    name: 'ARGUS_WISE_URL'
    value: 'http://${standinNames.wise}'
  }
  {
    name: 'ARGUS_WISE_PROFILE_ID'
    value: '16000001'
  }
]

var allSecrets = concat(generatedSecrets, manualSecrets)

var plainSettings = [
  {
    name: 'AZURE_CLIENT_ID'
    value: identityClientId
  }
  {
    // No password: the app signs in as its own Entra role with a token from its managed
    // identity, then works as argus_app (#81, src/argus/db/__init__.py).
    name: 'ARGUS_DATABASE_URL'
    value: 'postgresql+psycopg://${identityName}@${databaseHost}:5432/${databaseName}?sslmode=require'
  }
  {
    name: 'ARGUS_DATABASE_AUTH'
    value: 'entra'
  }
  {
    name: 'ARGUS_DATABASE_ROLE'
    value: 'argus_app'
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
  {
    name: 'ARGUS_DEMO'
    value: demo ? 'true' : 'false'
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
            demo ? demoSettings : [],
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
output appImage string = image
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'

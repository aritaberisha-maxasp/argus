// The Container Apps environment and the ARGUS web app (ADR 0001).
//
// The scheduled jobs from ADR 0001 (mailbox sweep, reminders, payment run, bank import,
// exception digest) run in this environment as Container Apps jobs. Each job story adds its
// job here once the app has a command for it. Today: the mailbox sweep and the daily removal
// (#125). The nightly audit copy (#103) follows the same pattern.
//
// Every secret comes from Key Vault through the app's managed identity. The pipeline from #81
// builds the ARGUS image and passes it in. The very first deploy starts with a public
// placeholder, because the registry for the real image does not exist yet.

import { intakeSettingValues, manualSecret, signInSettingValues } from '../types.bicep'

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

@description('Run the mailbox sweep and the daily removal as scheduled jobs (#125).')
param runIntakeJobs bool

@description('Names of the two intake jobs, from main.bicep.')
param intakeJobNames object

@description('Who Max ASP is, for the sorting and the invoice reading. Used only with demo off.')
param intakeSettings intakeSettingValues

@description('The IDs for sign in with Microsoft (#23, #133). Plain app settings, none is secret.')
param signInSettings signInSettingValues

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

// Who Max ASP is and who its partners are, for the sorting and the invoice reading of the
// intake jobs (src/argus/intake/jobs.py). In demo mode these are the synthetic values of the
// test fixtures (tests/fixtures/master-data/sender-domains.csv and the fixture VAT ID), so the
// seeded emails sort the way the tests expect. Live, they come from GitHub variables
// (docs/operations/deploy.md); the jobs refuse to start while one is missing.
var demoIntakeSettings = {
  ownDomain: 'maxasp.example'
  invoicingAgent: 'rechnungen@maxasp.example'
  ownVatIds: 'DE099000001'
  vendorDomains: join(
    [
      'muster-buerobedarf.example'
      'beispiel-cloud.example'
      'muster-immobilien.example'
      'example-software.example'
      'beispiel-beratung.example'
      'muster-telekom.example'
      'beispiel-reinigung.example'
      'muster-logistik.example'
    ],
    ','
  )
  customerDomains: join(
    [
      'beispiel-handel.example'
      'example-retail.example'
      'example-tech.example'
      'muster-stadtwerke.example'
    ],
    ','
  )
}
var intake = demo ? demoIntakeSettings : intakeSettings
var intakeEnv = [
  {
    name: 'ARGUS_OWN_DOMAIN'
    value: intake.ownDomain
  }
  {
    name: 'ARGUS_INVOICING_AGENT'
    value: intake.invoicingAgent
  }
  {
    name: 'ARGUS_OWN_VAT_IDS'
    value: intake.ownVatIds
  }
  {
    name: 'ARGUS_VENDOR_DOMAINS'
    value: intake.vendorDomains
  }
  {
    name: 'ARGUS_CUSTOMER_DOMAINS'
    value: intake.customerDomains
  }
]

// Sign in with Microsoft (#23, ADR 0008, docs/operations/sign-in.md). Real Entra ID in demo mode
// too, because the people who look at the demo sign in with their own Max ASP accounts. Only the
// web app checks tokens, so the jobs do not get these. ARGUS_ENTRA_AUTHORITY is for the local
// fake only and is never set here; tools/check_infra.py fails if it shows up.
var signInEnv = [
  {
    name: 'ARGUS_ENTRA_TENANT_ID'
    value: signInSettings.tenantId
  }
  {
    name: 'ARGUS_ENTRA_API_CLIENT_ID'
    value: signInSettings.apiClientId
  }
  {
    name: 'ARGUS_ENTRA_GROUP_APPROVER'
    value: signInSettings.approverGroupId
  }
  {
    name: 'ARGUS_ENTRA_GROUP_FINANCE'
    value: signInSettings.financeGroupId
  }
  {
    name: 'ARGUS_ENTRA_GROUP_ADMIN'
    value: signInSettings.adminGroupId
  }
]

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
            signInEnv,
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

// The scheduled intake jobs (#125, ADR 0001). Each run does one pass and stops; Azure starts
// the next one on schedule whether the last one failed or not. No retries: a failed run logs
// `argus-job failed`, the alert in alerts.bicep tells Arita, and the next run tries again. Only
// one sweep works at a time, because the sweep holds a database lock (src/argus/intake/sweep.py);
// a run that starts while the last one still works does nothing. Same image, identity,
// settings and Key Vault secrets as the web app.
// Plain values only, so tools/check_infra.py can read the schedules from the compiled template.
var intakeJobList = [
  {
    job: 'sweep'
    // Every 5 minutes (ADR 0003).
    cron: '*/5 * * * *'
  }
  {
    job: 'removal'
    // Once a day at 02:30 UTC, after the nightly audit copy at 01:00 (#103).
    cron: '30 2 * * *'
  }
]

resource intakeJobs 'Microsoft.App/jobs@2024-03-01' = [
  for job in intakeJobList: if (runIntakeJobs) {
    name: intakeJobNames[job.job]
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
        triggerType: 'Schedule'
        // A run that hangs is stopped after 30 minutes; the next one starts on schedule.
        replicaTimeout: 1800
        replicaRetryLimit: 0
        scheduleTriggerConfig: {
          cronExpression: job.cron
          parallelism: 1
          replicaCompletionCount: 1
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
            name: job.job
            image: image
            command: [
              'python'
              '-m'
              'argus.intake'
              job.job
            ]
            resources: {
              cpu: json('0.5')
              memory: '1Gi'
            }
            env: concat(
              plainSettings,
              demo ? demoSettings : [],
              intakeEnv,
              map(allSecrets, secret => {
                name: secret.envName
                secretRef: toLower(secret.secretName)
              })
            )
          }
        ]
      }
    }
  }
]

output environmentId string = environment.id
output appName string = app.name
output appImage string = image
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'

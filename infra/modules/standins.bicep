// The stand ins ARGUS talks to in demo mode (#124, ADR 0009): the fake Microsoft Graph with
// the accounts mailbox, the fake DATEV and the Wise stand in, plus the job that puts the demo
// emails into the fake mailbox.
//
// All three run from the argus-demo image (docker/demo/Dockerfile), each with its own command.
// Their ingress is internal: only apps in the same Container Apps environment reach them, the
// internet does not (there is no public address). They keep their data in memory, so each runs
// exactly one replica, and a restart empties it. The Wise stand in has no way to fund or release
// a payment, just like Wise for ARGUS (NF-04).
//
// Deployed only while `demo` is true and the pipeline passes the image. Without an image the
// running stand ins stay as they are.

param location string
param tags object
param environmentId string

@description('Resource ID of the user assigned identity: pulls the image and reads the tokens.')
param identityId string

param registryLoginServer string
param keyVaultUri string

@description('The argus-demo image built by the pipeline.')
param image string

param graphName string
param datevName string
param wiseName string
param seedJobName string

param graphTokenSecretName string
param wiseTokenSecretName string

@description('The fake mailbox, a reserved .example address (RFC 2606).')
param mailbox string

var standinTags = union(tags, { role: 'demo-standin' })

var standins = [
  {
    name: graphName
    port: 8081
    command: [
      'uvicorn'
      'standins.graph.app:app'
      '--host'
      '0.0.0.0'
      '--port'
      '8081'
    ]
    secret: graphTokenSecretName
    env: [
      {
        name: 'GRAPH_STANDIN_TOKEN'
        secretRef: toLower(graphTokenSecretName)
      }
      {
        // The demo starts with an empty mailbox; the seed job puts the demo emails in.
        name: 'GRAPH_STANDIN_START_EMPTY'
        value: 'true'
      }
    ]
  }
  {
    name: datevName
    port: 58452
    command: [
      'uvicorn'
      'argus.datev.fake:app_from_env'
      '--factory'
      '--host'
      '0.0.0.0'
      '--port'
      '58452'
    ]
    secret: ''
    env: []
  }
  {
    name: wiseName
    port: 8080
    command: [
      'uvicorn'
      'standins.wise.app:app'
      '--host'
      '0.0.0.0'
      '--port'
      '8080'
    ]
    secret: wiseTokenSecretName
    env: [
      {
        name: 'WISE_STANDIN_TOKEN'
        secretRef: toLower(wiseTokenSecretName)
      }
    ]
  }
]

resource apps 'Microsoft.App/containerApps@2024-03-01' = [
  for standin in standins: {
    name: standin.name
    location: location
    tags: standinTags
    identity: {
      type: 'UserAssigned'
      userAssignedIdentities: {
        '${identityId}': {}
      }
    }
    properties: {
      environmentId: environmentId
      workloadProfileName: 'Consumption'
      configuration: {
        activeRevisionsMode: 'Single'
        ingress: {
          // Inside the environment only. ARGUS calls them by name over plain http, which the
          // clients accept only for a stand in (argus.connections).
          external: false
          targetPort: standin.port
          transport: 'http'
          allowInsecure: true
        }
        registries: [
          {
            server: registryLoginServer
            identity: identityId
          }
        ]
        secrets: empty(standin.secret)
          ? []
          : [
              {
                name: toLower(standin.secret)
                keyVaultUrl: '${keyVaultUri}secrets/${standin.secret}'
                identity: identityId
              }
            ]
      }
      template: {
        containers: [
          {
            name: 'standin'
            image: image
            command: standin.command
            resources: {
              cpu: json('0.25')
              memory: '0.5Gi'
            }
            env: standin.env
          }
        ]
        scale: {
          minReplicas: 1
          maxReplicas: 1
        }
      }
    }
  }
]

// Started by hand through the Demo workflow (.github/workflows/demo.yml). It runs inside the
// environment, because the fake mailbox is not reachable from anywhere else.
resource seedJob 'Microsoft.App/jobs@2024-03-01' = {
  name: seedJobName
  location: location
  tags: standinTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 300
      replicaRetryLimit: 0
      manualTriggerConfig: {
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
        {
          name: toLower(graphTokenSecretName)
          keyVaultUrl: '${keyVaultUri}secrets/${graphTokenSecretName}'
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'seed'
          image: image
          command: [
            'python'
            '-m'
            'argus.demo'
            'seed'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'ARGUS_DEMO'
              value: 'true'
            }
            {
              name: 'ARGUS_GRAPH_URL'
              value: 'http://${graphName}/v1.0'
            }
            {
              name: 'ARGUS_MAILBOX'
              value: mailbox
            }
            {
              name: 'ARGUS_GRAPH_STANDIN_TOKEN'
              secretRef: toLower(graphTokenSecretName)
            }
          ]
        }
      ]
    }
  }
  dependsOn: [
    apps
  ]
}

output names array = [for (standin, i) in standins: apps[i].name]
output seedJobName string = seedJob.name

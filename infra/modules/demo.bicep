// What ARGUS needs in demo mode besides the stand ins themselves (#124, ADR 0009).
//
// - A document store of its own. The real one keeps every file for 10 years and nobody can
//   delete it, so demo emails must never land there. This one has no retention policy, keeps
//   nothing for long and can be emptied by `python -m argus.demo reset`.
// - The tokens of the fake Graph and the Wise stand in, written into Key Vault like every
//   other secret, so the app gets them only as Key Vault references (NF-06). They open nothing
//   but the stand ins, which are reachable only inside the Container Apps environment.
//
// Deployed only while the `demo` parameter is true. Nothing here touches the real store.

param location string
param name string
param tags object

@description('Key Vault that receives the connection string and the stand in tokens.')
param keyVaultName string

param documentsSecretName string
param graphTokenSecretName string
param wiseTokenSecretName string

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: union(tags, { role: 'demo' })
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
}

// Same container name as the real store (DEFAULT_CONTAINER in src/argus/documents/store.py).
resource documents 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'documents'
  properties: {
    publicAccess: 'None'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource connectionString 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: documentsSecretName
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${account.name};AccountKey=${account.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

// The fake Graph wants a token shaped like an Entra app token, because the Graph client reads
// its permissions (src/argus/intake/graph.py). It is unsigned and only the fake accepts it.
// The nonce makes it different in every resource group.
var tokenHeader = replace(replace(replace(base64('{"alg":"none","typ":"JWT"}'), '+', '-'), '/', '_'), '=', '')
var tokenClaims = replace(
  replace(
    replace(
      base64(string({
        aud: 'https://graph.microsoft.com'
        iss: 'fake-graph'
        roles: [
          'Mail.ReadWrite'
          'Mail.Send'
        ]
        nonce: guid(resourceGroup().id, 'argus-graph-standin')
      })),
      '+',
      '-'
    ),
    '/',
    '_'
  ),
  '=',
  ''
)

resource graphToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: graphTokenSecretName
  properties: {
    value: '${tokenHeader}.${tokenClaims}.fake-signature'
  }
}

resource wiseToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: wiseTokenSecretName
  properties: {
    value: 'standin-${guid(resourceGroup().id, 'argus-wise-standin')}'
  }
}

output accountName string = account.name

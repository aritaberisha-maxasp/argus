// The locked store for original documents (ADR 0001, NF-01, NF-02).
//
// A storage account of its own, geo redundant between Germany West Central and Germany North,
// with a time based retention policy (WORM) on every blob version. While the policy is
// unlocked no file can be changed or deleted, but an admin could still shorten or remove the
// policy. Once locked, nobody can shorten or remove it, not even Microsoft support. Locking
// cannot be undone, so it is a parameter that is off by default and only Arita switches on
// (infra/README.md).

param location string
param name string
param tags object

@description('Key Vault that receives the connection string the app reads.')
param keyVaultName string

@description('Days every original stays unchangeable. Can be raised later, never lowered once locked.')
@minValue(3653)
param retentionDays int

@description('Lock the retention policy for good. Off by default. Only Arita switches it on, after the first deploy and after the tax advisor confirms the period.')
param retentionLocked bool = false

@description('Name of the Key Vault secret that holds the connection string.')
param connectionStringSecretName string

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_GRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    // Version level WORM for the whole account. Azure only accepts Unlocked (or Disabled)
    // when the policy is first created, so the first deploy always runs with the lock off.
    immutableStorageWithVersioning: {
      enabled: true
      immutabilityPolicy: {
        immutabilityPeriodSinceCreationInDays: retentionDays
        allowProtectedAppendWrites: false
        state: retentionLocked ? 'Locked' : 'Unlocked'
      }
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

// The container name matches DEFAULT_CONTAINER in src/argus/documents/store.py.
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

// The app still reads the store with a connection string (src/argus/documents/store.py).
// It is written straight into Key Vault here, so it never shows up in the code, a parameter
// or a deployment output.
resource connectionString 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: connectionStringSecretName
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${account.name};AccountKey=${account.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

output id string = account.id
output name string = account.name
output containerName string = documents.name

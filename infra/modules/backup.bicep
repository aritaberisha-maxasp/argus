// Long term backups of the case database (issue #82, ADR 0007, NF-01).
//
// The database server keeps its own backups for 35 days (database.bicep). This module adds
// Azure Backup's long term retention: one full backup a week into a Backup vault, the first
// backup of every month kept for `retentionYears` years (10 by default, the most Azure Backup
// allows). The vault stores its copies geo redundant, in Germany West Central with a second
// copy in Germany North, its paired region. Nothing leaves Germany.
//
// The vault signs in to the server with its own system assigned identity. That identity needs
// two roles that the deploy identity may not hand out (docs/operations/deploy.md, step 5), so
// Arita grants them once by hand and then switches `protectDatabase` on
// (docs/operations/backup-and-restore.md). Until then the vault and the policy exist, but no
// long term backup runs.

param location string
param name string
param tags object

@description('Resource ID of the PostgreSQL Flexible Server to back up.')
param serverId string

@description('Name of that server.')
param serverName string

@description('Years the first weekly backup of every month is kept. Azure Backup allows at most 10.')
@minValue(1)
@maxValue(10)
param retentionYears int

@description('Lock the vault for good: immutability and soft delete can then never be switched off. Follows the retention lock of the original documents.')
param retentionLocked bool

@description('Start the long term backup. Only after the vault identity has its two roles.')
param protectDatabase bool

// Weekly, Sunday 01:00 UTC, after the nightly audit copy (#83) and well before office hours.
// The generally available long term backup takes at most one backup a week.
var weeklySchedule = 'R/2026-11-08T01:00:00+00:00/P1W'

var vaultStore = {
  dataStoreType: 'VaultStore'
  objectType: 'DataStoreInfoBase'
}

resource vault 'Microsoft.DataProtection/backupVaults@2024-04-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    storageSettings: [
      {
        datastoreType: 'VaultStore'
        // Germany West Central plus its paired region Germany North (ADR 0007).
        type: 'GeoRedundant'
      }
    ]
    securitySettings: {
      // Unlocked immutability already refuses to delete a backup early or to shorten its
      // retention. Locked takes away the switch to turn that off, for good.
      immutabilitySettings: {
        state: retentionLocked ? 'Locked' : 'Unlocked'
      }
      softDeleteSettings: {
        state: retentionLocked ? 'AlwaysOn' : 'On'
        retentionDurationInDays: 14
      }
    }
    // A failed backup or restore job raises a built in Azure Monitor alert. backup-alerts.bicep
    // sends those alerts to the operations action group (#115).
    monitoringSettings: {
      azureMonitorAlertSettings: {
        alertsForAllJobFailures: 'Enabled'
      }
    }
  }
}

// Azure applies a changed policy (schedule, the 3 month rule, the years) to new backups only.
// Existing backups keep the retention they were taken with. A change of the years gives the
// policy a new name, so the instance moves to it and the old policy stays for reference.
resource policy 'Microsoft.DataProtection/backupVaults/backupPolicies@2024-04-01' = {
  parent: vault
  name: 'argus-database-monthly-${retentionYears}y'
  properties: {
    objectType: 'BackupPolicy'
    datasourceTypes: [
      'Microsoft.DBforPostgreSQL/flexibleServers'
    ]
    policyRules: [
      {
        name: 'BackupWeekly'
        objectType: 'AzureBackupRule'
        backupParameters: {
          objectType: 'AzureBackupParams'
          backupType: 'Full'
        }
        dataStore: vaultStore
        trigger: {
          objectType: 'ScheduleBasedTriggerContext'
          schedule: {
            repeatingTimeIntervals: [
              weeklySchedule
            ]
            timeZone: 'UTC'
          }
          taggingCriteria: [
            {
              isDefault: true
              taggingPriority: 99
              tagInfo: {
                tagName: 'Default'
              }
            }
            {
              isDefault: false
              taggingPriority: 15
              tagInfo: {
                tagName: 'Monthly'
              }
              criteria: [
                {
                  objectType: 'ScheduleBasedBackupCriteria'
                  absoluteCriteria: [
                    'FirstOfMonth'
                  ]
                }
              ]
            }
          ]
        }
      }
      {
        // Every other weekly backup: three months, so a mistake found late can still be undone.
        name: 'Default'
        objectType: 'AzureRetentionRule'
        isDefault: true
        lifecycles: [
          {
            deleteAfter: {
              objectType: 'AbsoluteDeleteOption'
              duration: 'P3M'
            }
            sourceDataStore: vaultStore
            targetDataStoreCopySettings: []
          }
        ]
      }
      {
        // The first backup of every month: the legal retention period (NF-01).
        name: 'Monthly'
        objectType: 'AzureRetentionRule'
        isDefault: false
        lifecycles: [
          {
            deleteAfter: {
              objectType: 'AbsoluteDeleteOption'
              duration: 'P${retentionYears}Y'
            }
            sourceDataStore: vaultStore
            targetDataStoreCopySettings: []
          }
        ]
      }
    ]
  }
}

var serverInfo = {
  resourceID: serverId
  resourceUri: serverId
  datasourceType: 'Microsoft.DBforPostgreSQL/flexibleServers'
  resourceName: serverName
  resourceType: 'Microsoft.DBforPostgreSQL/flexibleServers'
  resourceLocation: location
}

resource database 'Microsoft.DataProtection/backupVaults/backupInstances@2024-04-01' = if (protectDatabase) {
  parent: vault
  name: '${serverName}-${uniqueString(serverId)}'
  properties: {
    objectType: 'BackupInstance'
    friendlyName: serverName
    dataSourceInfo: union(serverInfo, {
      objectType: 'Datasource'
    })
    dataSourceSetInfo: union(serverInfo, {
      objectType: 'DatasourceSet'
    })
    policyInfo: {
      policyId: policy.id
    }
  }
}

output name string = vault.name
output principalId string = vault.identity.principalId

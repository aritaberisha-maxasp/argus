// Alerts on the long term backups of the case database (#115, NF-01).
//
// Two things can leave a hole in the ten year backup trail without anyone noticing:
// - a weekly backup job fails;
// - no backup runs at all, for example because protection was switched off or the schedule
//   stopped.
//
// Failed job. Azure Backup raises a built in Azure Monitor alert for every failed backup or
// restore job of the vault (`alertsForAllJobFailures` in backup.bicep), and for security events
// such as deleted backup data. Those alerts email nobody on their own. The alert processing
// rule below adds the operations action group from alerts.bicep to every Azure Backup alert in
// the resource group.
//
// No backup. The vault sends its job records to the Log Analytics workspace (table
// AddonAzureBackupJobs), and a log alert looks for a completed backup job. A log alert can look
// back at most two days, so it cannot ask "anything in the last 8 days?" directly. It checks the
// weekly run instead: the backup runs on Sunday at 01:00 UTC (backup.bicep), and every Monday
// in the 23:00 UTC hour the rule fires if no backup job completed since Saturday night. By then
// the last good backup is at least 8 days old. If the job records stop arriving at all, the
// rule fires too, so a broken diagnostic setting cannot hide a missing backup.
// tools/check_infra.py fails when the rule's day no longer follows the schedule's day.
//
// What the emails hold: the built in alert names the vault, the backup instance (the database
// server), the job and the error code. The log alert names only the rule and the time. Neither
// holds invoice or personal data (NF-03).
//
// Regions: the diagnostic setting has no region of its own, the log alert runs in Germany West
// Central and reads the workspace there. The alert processing rule, like the action group, only
// exists as a global Azure resource. It holds a filter and a link to the action group, no data.
//
// Everything here waits for `watchBackups` (the same switch that starts the backups), so there
// is never an alert about a backup that is not meant to run yet.

param location string
param tags object

@description('Name of the Backup vault from backup.bicep.')
param vaultName string

@description('Resource ID of the Log Analytics workspace.')
param workspaceId string

@description('Resource ID of the operations action group from alerts.bicep.')
param actionGroupId string

@description('Watch the backups. Follows databaseBackupProtected.')
param watchBackups bool

resource vault 'Microsoft.DataProtection/backupVaults@2024-04-01' existing = {
  name: vaultName
}

// Only the job records: what the "no backup" rule needs, nothing more.
resource jobLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (watchBackups) {
  name: 'argus-backup-jobs'
  scope: vault
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'AddonAzureBackupJobs'
        enabled: true
      }
    ]
  }
}

resource routeBackupAlerts 'Microsoft.AlertsManagement/actionRules@2021-08-08' = if (watchBackups) {
  name: 'apr-${vaultName}-to-ops'
  location: 'global'
  tags: tags
  properties: {
    description: 'Sends every Azure Backup alert in this resource group (failed backup or restore job, deleted backup data) to the operations action group (#115).'
    enabled: true
    scopes: [
      resourceGroup().id
    ]
    conditions: [
      {
        field: 'MonitorService'
        operator: 'Equals'
        values: [
          'Azure Backup'
        ]
      }
    ]
    actions: [
      {
        actionType: 'AddActionGroups'
        actionGroupIds: [
          actionGroupId
        ]
      }
    ]
  }
}

// The empty datatable keeps the query valid before the first job record has arrived.
var noBackupQuery = 'union isfuzzy=true (datatable(TimeGenerated: datetime, JobOperation: string, JobStatus: string)[]), (AddonAzureBackupJobs) | where TimeGenerated > ago(2d) | where JobOperation == "Backup" and JobStatus startswith "Completed" | summarize completed = count() | where completed == 0 and dayofweek(now()) == 1d and hourofday(now()) == 23'

resource noBackupAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (watchBackups) {
  name: 'alert-${vaultName}-no-backup'
  location: location
  tags: tags
  properties: {
    displayName: 'alert-${vaultName}-no-backup'
    description: 'No backup of the case database completed since Sunday, so the last good backup is at least 8 days old. Check Backup jobs and Backup instances in the vault.'
    severity: 1
    enabled: true
    scopes: [
      workspaceId
    ]
    evaluationFrequency: 'PT1H'
    windowSize: 'P2D'
    criteria: {
      allOf: [
        {
          query: noBackupQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    // The rule can only be true in one hour a week. A stateful alert would send a "resolved"
    // email an hour later although the backup is still missing, so it fires once and stays.
    autoMitigate: false
    actions: {
      actionGroups: [
        actionGroupId
      ]
    }
  }
}

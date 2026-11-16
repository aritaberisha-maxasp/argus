// Operations alerts: tell Arita when a scheduled job fails or stops running (#125).
//
// One action group emails the address from the GitHub variable ARGUS_ALERT_EMAIL. Every
// operations alert sends to it: the intake jobs (#125), the nightly audit copy (#103) and
// later the backup alerts (#115).
//
// The scheduled jobs end every run with one log line, `argus-job ok <job>` or
// `argus-job failed <job>` (src/argus/logsafe.py). The rules read those lines from the Log
// Analytics workspace the Container Apps environment writes to:
// - a failed line: the run failed, or an email in it failed;
// - no ok line for too long: the job crashed before it could log, did not start, or its
//   schedule stopped.
// The lines name the job only. The alert email holds the rule name, the time and the count,
// never an email, a sender or a subject (NF-03).
//
// Action groups are global resources in Azure. The group holds one email address and sends
// the notification; the rules, the logs and the jobs stay in Germany West Central.

param location string
param tags object

@description('Name of the action group for operations alerts.')
param actionGroupName string

@description('Where the alerts go. Empty: the rules still fire in Azure, but nobody gets an email.')
param alertEmail string

@description('Resource ID of the Log Analytics workspace of the Container Apps environment.')
param workspaceId string

@description('Watch the intake jobs. Off while the jobs are off, so a job that does not exist never raises a "no run" alert.')
param watchIntakeJobs bool

@description('Names of the intake jobs by job, from main.bicep: sweep, removal and digest.')
param intakeJobNames object

@description('Watch the nightly audit copy job (#103). Off while the job is off.')
param watchAuditJob bool

@description('Name of the nightly audit copy job, from main.bicep.')
param auditJobName string

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'argus-ops'
    enabled: true
    emailReceivers: empty(alertEmail)
      ? []
      : [
          {
            name: 'operations'
            emailAddress: alertEmail
            useCommonAlertSchema: true
          }
        ]
  }
}

// One entry per rule. `silent` rules fire when fewer than one ok line arrived in the window,
// `failed` rules when at least one failed line arrived. The windows leave room for the few
// minutes Log Analytics takes to show a line. Plain values only, so tools/check_infra.py can
// read the queries from the compiled template.
var intakeRules = [
  {
    job: 'sweep'
    kind: 'failed'
    description: 'A mailbox sweep run failed, or an email in it failed. The next run starts on time and tries the failed emails again.'
    query: 'ContainerAppConsoleLogs_CL | where Log_s contains "argus-job failed sweep"'
    frequency: 'PT5M'
    window: 'PT15M'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    job: 'sweep'
    kind: 'silent'
    description: 'No mailbox sweep finished in the last 30 minutes, although it should run every 5 minutes.'
    query: 'ContainerAppConsoleLogs_CL | where Log_s contains "argus-job ok sweep"'
    frequency: 'PT10M'
    window: 'PT30M'
    operator: 'LessThan'
    threshold: 1
  }
  {
    job: 'removal'
    kind: 'failed'
    description: 'The daily removal from ARGUS/Removed failed, or an email in it failed. The next day tries again.'
    query: 'ContainerAppConsoleLogs_CL | where Log_s contains "argus-job failed removal"'
    frequency: 'PT1H'
    window: 'PT2H'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    job: 'removal'
    kind: 'silent'
    description: 'The daily removal from ARGUS/Removed has not finished in the last 26 hours.'
    query: 'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(26h) | where Log_s contains "argus-job ok removal"'
    frequency: 'PT1H'
    window: 'P2D'
    operator: 'LessThan'
    threshold: 1
  }
  {
    job: 'digest'
    kind: 'failed'
    description: 'The daily digest of open exceptions failed, or its saved draft was gone or changed and nothing was sent. The next day tries again.'
    query: 'ContainerAppConsoleLogs_CL | where Log_s contains "argus-job failed digest"'
    frequency: 'PT1H'
    window: 'PT2H'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    job: 'digest'
    kind: 'silent'
    description: 'The daily digest of open exceptions has not finished in the last 26 hours.'
    query: 'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(26h) | where Log_s contains "argus-job ok digest"'
    frequency: 'PT1H'
    window: 'P2D'
    operator: 'LessThan'
    threshold: 1
  }
]

resource intakeAlerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [
  for rule in intakeRules: if (watchIntakeJobs) {
    name: 'alert-${intakeJobNames[rule.job]}-${rule.kind}'
    location: location
    tags: tags
    properties: {
      displayName: 'alert-${intakeJobNames[rule.job]}-${rule.kind}'
      description: rule.description
      severity: 2
      enabled: true
      scopes: [
        workspaceId
      ]
      evaluationFrequency: rule.frequency
      windowSize: rule.window
      criteria: {
        allOf: [
          {
            query: rule.query
            timeAggregation: 'Count'
            operator: rule.operator
            threshold: rule.threshold
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      // Fires once while the problem lasts and clears on its own once runs are clean again.
      autoMitigate: true
      actions: {
        actionGroups: [
          actionGroup.id
        ]
      }
    }
  }
]

// The nightly audit copy (#103). A failed run has normally put an item in the exception queue,
// but when the database cannot be reached it cannot, and then this alert is the only sign
// (review of PR 102). The job runs at 01:00 UTC.
var auditRules = [
  {
    kind: 'failed'
    description: 'The nightly audit copy or its check failed. Look in the exception queue; if nothing is there, the job could not reach the database. The next night tries again.'
    query: 'ContainerAppConsoleLogs_CL | where Log_s contains "argus-job failed audit-copy"'
    frequency: 'PT1H'
    window: 'PT2H'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    kind: 'silent'
    description: 'The nightly audit copy has not finished in the last 26 hours.'
    query: 'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(26h) | where Log_s contains "argus-job ok audit-copy"'
    frequency: 'PT1H'
    window: 'P2D'
    operator: 'LessThan'
    threshold: 1
  }
]

resource auditAlerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [
  for rule in auditRules: if (watchAuditJob) {
    name: 'alert-${auditJobName}-${rule.kind}'
    location: location
    tags: tags
    properties: {
      displayName: 'alert-${auditJobName}-${rule.kind}'
      description: rule.description
      severity: 2
      enabled: true
      scopes: [
        workspaceId
      ]
      evaluationFrequency: rule.frequency
      windowSize: rule.window
      criteria: {
        allOf: [
          {
            query: rule.query
            timeAggregation: 'Count'
            operator: rule.operator
            threshold: rule.threshold
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      actions: {
        actionGroups: [
          actionGroup.id
        ]
      }
    }
  }
]

output actionGroupId string = actionGroup.id

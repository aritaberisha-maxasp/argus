// Logs for the Container Apps environment (ADR 0001).

@description('Azure region, always Germany West Central (NF-03).')
param location string

@description('Name of the Log Analytics workspace.')
param name string

param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

output id string = workspace.id
output name string = workspace.name

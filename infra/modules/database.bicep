// PostgreSQL for the case data and the audit log (ADR 0001, ADR 0005).
// Entra ID sign in only: there is no password at all, so there is no database password to
// store or leak. The admin is the identity that runs the migrations (#81). The app signs in
// as argus_app with its managed identity; the migrations create that role.

param location string
param name string
param tags object

@description('Entra object ID of the database admin, the identity that runs the migrations.')
@minLength(36)
@maxLength(36)
param adminObjectId string

@description('Display name of that admin as Entra ID knows it.')
param adminName string

@description('What kind of Entra principal the admin is.')
@allowed([
  'ServicePrincipal'
  'Group'
  'User'
])
param adminType string

@description('Name of the ARGUS database.')
param databaseName string = 'argus'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 35
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource admin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2023-12-01-preview' = {
  parent: server
  name: adminObjectId
  properties: {
    principalName: adminName
    principalType: adminType
    tenantId: subscription().tenantId
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    admin
  ]
}

// Container Apps on the consumption plan have no fixed outgoing address, so the firewall lets
// in Azure services only. Every connection still needs a valid Entra token and TLS.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: server
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [
    database
  ]
}

output id string = server.id
output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name

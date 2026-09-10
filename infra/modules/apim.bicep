// ---------------------------------------------------------------------------
// API Management - the single gateway every option is published through.
//
// SKU choice: Developer is the cheapest tier that still supports llm-token-limit
// (Consumption does not, which would gut the point of the gateway layer). It has
// no SLA, which is fine for a demo. Basic v2 is the drop-in upgrade if you want
// faster provisioning and an SLA - it is ~3x the cost.
//
// Provisioning takes 30-45 minutes on Developer. Deploy this first.
//
// Observability is wired here rather than in apim-apis.bicep so that token
// metrics land in Application Insights for every route, including Option 4
// which reaches a laptop through a Dev Tunnel.
// ---------------------------------------------------------------------------

@description('Azure region.')
param location string

@description('Globally unique APIM instance name.')
param apimName string

@description('APIM SKU. Developer is cheapest with llm-token-limit support.')
@allowed([
  'Developer'
  'Basic'
  'Standard'
  'Premium'
  'BasicV2'
  'StandardV2'
])
param sku string = 'Developer'

@description('Publisher email shown on the developer portal and used for notifications.')
param publisherEmail string

@description('Publisher organisation name.')
param publisherName string

@description('Log Analytics workspace name.')
param logAnalyticsName string

@description('Application Insights component name.')
param appInsightsName string

@description('Tags applied to every resource.')
param tags object = {}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: sku
    capacity: 1
  }
  identity: {
    // Used by authentication-managed-identity in the per-API policies to reach
    // Foundry. Assigned per-API only - a global policy would hand a Cognitive
    // Services token to the VM and laptop backends too.
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// Named value holding the App Insights connection string, referenced by the logger.
resource appInsightsConnectionString 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'appinsights-connection-string'
  properties: {
    displayName: 'appinsights-connection-string'
    value: appInsights.properties.ConnectionString
    secret: true
  }
}

resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    description: 'Token usage and request telemetry for all four hosting options.'
    resourceId: appInsights.id
    credentials: {
      connectionString: '{{appinsights-connection-string}}'
    }
  }
  dependsOn: [
    appInsightsConnectionString
  ]
}

// Gateway-wide diagnostics. Individual APIs inherit this unless they override it.
resource apimDiagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: apimLogger.id
    alwaysLog: 'allErrors'
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
    logClientIp: true
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: {
        headers: [
          'x-shs-option'
        ]
      }
    }
  }
}

output apimName string = apim.name
output apimId string = apim.id
output gatewayUrl string = apim.properties.gatewayUrl
output apimPrincipalId string = apim.identity.principalId
output loggerId string = apimLogger.id
output appInsightsName string = appInsights.name
output appInsightsId string = appInsights.id
output logAnalyticsId string = logAnalytics.id

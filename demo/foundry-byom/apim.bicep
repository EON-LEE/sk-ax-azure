// Stage 2 (apim): API Management Standard v2, the smallest tier Foundry's ApiManagement connection supports
// besides Premium, plus its Application Insights logger. It bills about 0.96 USD per hour from creation until the
// resource group is deleted; it cannot be stopped.
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param apimName string
param aiName string

@description('Required by API Management at create time. Not a person; no mail is sent to it.')
param publisherEmail string = 'noreply@example.com'
param publisherName string = 'A.X K2 demo'

param tags object = {}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: aiName
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: 'StandardV2'
    capacity: 1
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    publicNetworkAccess: 'Enabled'
  }
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: insights.id
    credentials: {
      instrumentationKey: insights.properties.InstrumentationKey
    }
    isBuffered: true
  }
}

// Requests and dependencies only: no headers and no request or response bodies are logged.
resource diagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
    logClientIp: false
    frontend: {
      request: {
        headers: []
        body: {
          bytes: 0
        }
      }
      response: {
        headers: []
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: []
        body: {
          bytes: 0
        }
      }
      response: {
        headers: []
        body: {
          bytes: 0
        }
      }
    }
  }
}

output gatewayUrl string = apim.properties.gatewayUrl

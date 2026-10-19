// Stage 5 (tracing): lets the Foundry console show agent runs by connecting the project to Application Insights.
// Creates the account-level and the project-level connection of category AppInsights, both pointing at the
// EXISTING component that also backs the APIM logger. The connection string is resolved inside this template and
// is never an output or a parameter.
// Based on microsoft-foundry/foundry-samples, infrastructure/infrastructure-setup-bicep/01-connections/
// connection-application-insights.bicep. The sample's two reader role assignments for the project identity are left
// out on purpose: they are only needed for evaluations. Add them only if the portal reports a permission error.
// A project accepts only one Application Insights connection.
targetScope = 'resourceGroup'

param accountName string
param projectName string = 'axk2-demo'
param aiName string
param isSharedToAll bool = true

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: projectName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: aiName
}

resource accountConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = {
  parent: account
  name: '${accountName}-appinsights'
  properties: {
    category: 'AppInsights'
    target: insights.id
    authType: 'ApiKey'
    isSharedToAll: isSharedToAll
    credentials: {
      key: insights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: insights.id
    }
  }
}

resource projectConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: aiName
  properties: {
    category: 'AppInsights'
    target: insights.id
    authType: 'ApiKey'
    isSharedToAll: isSharedToAll
    credentials: {
      key: insights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: insights.id
    }
  }
}

output connectionNames array = [
  accountConnection.name
  projectConnection.name
]

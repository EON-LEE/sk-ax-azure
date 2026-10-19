// Stage 1 (foundation): Foundry account and project without a model deployment, Log Analytics,
// Application Insights, and the Foundry User role for the operator on the project.
// Nothing here bills while idle apart from log ingestion.
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param accountName string
param projectName string = 'axk2-demo'
param logName string
param aiName string

@description('Entra object ID of the operator that creates and runs agents in the project. Not a secret.')
param operatorObjectId string

@allowed(['User', 'ServicePrincipal'])
param operatorPrincipalType string = 'User'

param tags object = {}

// Built-in role "Foundry User" (formerly "Azure AI User"): agents data actions on the project. The control-plane
// role the operator already holds does not grant them.
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('0.1')
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: aiName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    IngestionMode: 'LogAnalytics'
    RetentionInDays: 30
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'A.X K2 BYOM demo'
    description: 'Foundry agents that call A.X K2 through an API Management gateway (BYOM pre-validation).'
  }
}

resource operatorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, operatorObjectId, foundryUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: operatorObjectId
    principalType: operatorPrincipalType
  }
}

output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'

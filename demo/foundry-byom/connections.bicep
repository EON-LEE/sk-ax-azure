// Stage 4 (connections): Foundry project connections of category ApiManagement, one per model API.
// Static model list, model name in the request body (deploymentInPath=false), ApiKey auth with the APIM
// subscription key read inside the deployment, so the key never leaves ARM.
targetScope = 'resourceGroup'

param accountName string
param projectName string = 'axk2-demo'
param apimName string
param apimSubscriptionName string = 'foundry-byom'

@description('One entry per connection: connection name, APIM API name and the model id served by that API.')
param connections array = [
  {
    name: 'axk2-nvfp4'
    apiName: 'axk2-nvfp4'
    modelName: 'ax-k2-nvfp4'
  }
  {
    name: 'axk2-fp8'
    apiName: 'axk2-fp8'
    modelName: 'ax-k2-fp8'
  }
]

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: projectName
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource apimSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' existing = {
  parent: apim
  name: apimSubscriptionName
}

resource apis 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = [for c in connections: {
  parent: apim
  name: c.apiName
}]

resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = [for (c, i) in connections: {
  parent: project
  name: c.name
  properties: {
    category: 'ApiManagement'
    target: '${apim.properties.gatewayUrl}/${apis[i].properties.path}'
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: apimSubscription.listSecrets(apimSubscription.apiVersion).primaryKey
    }
    metadata: {
      deploymentInPath: 'false'
      models: string([
        {
          name: c.modelName
          properties: {
            model: {
              name: c.modelName
              version: ''
              format: 'OpenAI'
            }
          }
        }
      ])
    }
  }
}]

output connectionNames array = [for (c, i) in connections: connection[i].name]

// Stage 3 module: one model-specific API on the gateway.
// Foundry appends /chat/completions to the connection target, so each model gets its own API path and operations
// that mirror what the web app serves under its own /v1 prefix.
targetScope = 'resourceGroup'

param apimName string
param productName string
param apiName string
param displayName string

@description('Path segment under the gateway, without leading or trailing slash.')
param apiPath string

@description('Web app base URL of this model API, without trailing slash.')
param serviceUrl string

@description('Rendered policy document shared by all model APIs.')
param policyXml string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' existing = {
  parent: apim
  name: productName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: apiName
  properties: {
    displayName: displayName
    description: 'OpenAI-compatible chat completions for ${displayName}.'
    path: apiPath
    protocols: [
      'https'
    ]
    serviceUrl: serviceUrl
    subscriptionRequired: true
    // Foundry sends the connection key in the api-key header by default.
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'api-key'
    }
  }
}

resource chatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/chat/completions'
    request: {
      queryParameters: [
        {
          name: 'api-version'
          type: 'string'
          required: false
        }
      ]
    }
  }
}

resource listModels 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'list-models'
  properties: {
    displayName: 'List models'
    method: 'GET'
    urlTemplate: '/models'
    request: {
      queryParameters: [
        {
          name: 'api-version'
          type: 'string'
          required: false
        }
      ]
    }
  }
}

resource policy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policyXml
  }
  dependsOn: [
    chatCompletions
    listModels
  ]
}

resource productApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: api.name
}

output apiName string = api.name
output apiPath string = api.properties.path

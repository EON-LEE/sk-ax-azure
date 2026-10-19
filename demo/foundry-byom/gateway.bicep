// Stage 3 (gateway): product, APIM subscription for the Foundry connection, and one API per served model.
// The backend key named value (axk2-backend-key) must already exist: `byom.py key` creates it from the private
// key file, so no secret is ever part of a template or parameter file.
targetScope = 'resourceGroup'

param apimName string

@description('Web app base URL that serves the model API, for example https://<site>.azurewebsites.net. No trailing slash.')
param backendBaseUrl string

param productName string = 'axk2-models'
param subscriptionName string = 'foundry-byom'

@description('Served models. backendPath is the web app prefix that holds /chat/completions and /models.')
param models array = [
  {
    key: 'nvfp4'
    displayName: 'A.X K2 NVFP4'
    apiPath: 'nvfp4/v1'
    backendPath: '/models/nvfp4/v1'
  }
  {
    key: 'fp8'
    displayName: 'A.X K2 FP8'
    apiPath: 'fp8/v1'
    backendPath: '/v1'
  }
]

var policyXml = loadTextContent('policies/model-api.xml')

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: productName
  properties: {
    displayName: 'A.X K2 models'
    description: 'Model APIs that Foundry connects to. A subscription key is required.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

// Scoped to this product only; the built-in all-access subscription is not used.
resource foundrySubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: subscriptionName
  properties: {
    displayName: 'Foundry BYOM'
    scope: product.id
    state: 'active'
    allowTracing: false
  }
}

module modelApis 'modules/model-api.bicep' = [for m in models: {
  name: 'api-${m.key}'
  params: {
    apimName: apimName
    productName: product.name
    apiName: 'axk2-${m.key}'
    displayName: m.displayName
    apiPath: m.apiPath
    serviceUrl: '${backendBaseUrl}${m.backendPath}'
    policyXml: policyXml
  }
}]

output subscriptionName string = foundrySubscription.name
output apiPaths array = [for (m, i) in models: modelApis[i].outputs.apiPath]

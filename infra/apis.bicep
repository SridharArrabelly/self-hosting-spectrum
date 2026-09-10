// ---------------------------------------------------------------------------
// Publishes the four routes onto an APIM instance that already exists.
//
//   az deployment group create -g <rg> -f infra/apis.bicep -p infra/apis.bicepparam
//
// Split out from main.bicep on purpose. main.bicep creates long-lived, slow
// things (APIM takes 30-45 minutes). This deployment is seconds, and you will
// run it repeatedly: every time a policy changes, every time the managed
// compute endpoint is created, every time the Dev Tunnel URL rotates.
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Short suffix used when the infrastructure was deployed.')
param nameSuffix string = 'shs01'

@description('Per-route token budget enforced by llm-token-limit.')
param tokensPerMinute int = 20000

@description('Entra audience APIM requests when calling Foundry.')
@allowed([
  'https://cognitiveservices.azure.com'
  'https://ai.azure.com'
])
param foundryAudience string = 'https://cognitiveservices.azure.com'

@description('Option 3 backend URL, e.g. http://10.42.1.4:11434/v1. Empty leaves the route stubbed.')
param vmBackendUrl string = ''

@description('Option 4 Dev Tunnel URL. Empty leaves the route stubbed.')
param foundryLocalBackendUrl string = ''

var apimName = 'apim-spectrum-${nameSuffix}'
var foundryAccountName = 'aif-spectrum-${nameSuffix}'

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName

  resource logger 'loggers' existing = {
    name: 'appinsights'
  }
}

// Options 1 and 2 are different deployments on the same Foundry account, so
// they share an endpoint and differ only by the model name in the request
// body. Keeping them as separate APIM routes is what makes the swap visible.
var foundryEndpoint = foundry.properties.endpoint
var foundryBase = endsWith(foundryEndpoint, '/') ? foundryEndpoint : '${foundryEndpoint}/'
var foundryOpenAiV1 = '${foundryBase}openai/v1'

module apis 'modules/apim-apis.bicep' = {
  name: 'apim-apis'
  params: {
    apimName: apim.name
    loggerId: apim::logger.id
    tokensPerMinute: tokensPerMinute
    foundryAudience: foundryAudience
    managedComputeBackendUrl: foundryOpenAiV1
    fireworksBackendUrl: foundryOpenAiV1
    vmBackendUrl: vmBackendUrl
    foundryLocalBackendUrl: foundryLocalBackendUrl
  }
}

var routePaths = [
  'v1/managed-compute'
  'v1/fireworks'
  'v1/azure-vm'
  'v1/foundry-local'
]

output apimName string = apim.name
output gatewayUrl string = apim.properties.gatewayUrl
output subscriptionName string = apis.outputs.subscriptionName
output routes array = [for path in routePaths: '${apim.properties.gatewayUrl}/${path}/chat/completions']

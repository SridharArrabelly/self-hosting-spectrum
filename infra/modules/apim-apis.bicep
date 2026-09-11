// ---------------------------------------------------------------------------
// The APIM surface: four APIs, one per hosting option, all shaped identically.
//
// Deployed separately from apim.bicep because the backend URLs are not all
// known when APIM is created. Option 1's managed compute endpoint only exists
// after 01-managed-compute/deploy_managed_compute.py runs, and Option 4's
// tunnel URL changes whenever the tunnel is recreated. Both are named values,
// so refreshing them is a one-line update rather than a redeployment.
//
// Every API exposes the same two operations at the same relative paths, so the
// only thing that differs between hosting a model on rented GPUs and hosting
// it on your own laptop is the path segment in the URL.
// ---------------------------------------------------------------------------

@description('Name of the existing APIM instance.')
param apimName string

@description('Application Insights logger resource id, from apim.bicep.')
param loggerId string

@description('Per-route token budget enforced by llm-token-limit.')
param tokensPerMinute int = 20000

@description('How callers authenticate to the gateway. "entra" is keyless and the default; "key" is the subscription-key fallback; "both" accepts either while you migrate.')
@allowed([
  'entra'
  'key'
  'both'
])
param gatewayAuthMode string = 'entra'

@description('Tenant whose tokens the gateway will accept.')
param tenantId string = tenant().tenantId

@description('Audience the caller must request a token for. Must match the Audience set on the Foundry BYOM connection.')
param entraAudience string = 'https://cognitiveservices.azure.com'

@description('Optional comma-separated Entra application (client) IDs allowed to call the gateway. Empty means any client in the tenant holding a token for the audience. Populated by infra/scripts/setup_entra.py.')
param entraAllowedClientIds string = ''

@description('Entra audience APIM requests a token for when calling Foundry. Learn documents two; this is the one to flip if you get a 401.')
@allowed([
  'https://cognitiveservices.azure.com'
  'https://ai.azure.com'
])
param foundryAudience string = 'https://cognitiveservices.azure.com'

@description('Option 1 backend. The Foundry account /openai/v1 route.')
param managedComputeBackendUrl string

@description('Option 2 backend. Same shape as Option 1 - the difference is the deployment behind it.')
param fireworksBackendUrl string

@description('Option 3 backend, e.g. http://10.42.1.4:11434/v1. Empty until the VM exists.')
param vmBackendUrl string = ''

@description('Option 4 backend, the Dev Tunnel URL. Empty until tunnel.py runs.')
param foundryLocalBackendUrl string = ''

// A backend that is not wired up yet still needs a syntactically valid URL, or
// the API will not create. This one fails fast and obviously if it is ever hit.
var notWiredYet = 'https://not-configured.invalid/v1'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

// --- Named values ----------------------------------------------------------
// Everything the policies need that might change without a code change.

resource productSpectrum 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: 'spectrum'
  properties: {
    displayName: 'Self Hosting Spectrum'
    description: 'All four hosting options behind one gateway.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

// The gateway key. Created as a real APIM subscription so APIM owns the secret
// and it never appears in a parameter file or a deployment output. The policy
// fragment compares against it by named value reference.
resource gatewaySubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'spectrum-demo'
  properties: {
    displayName: 'Self Hosting Spectrum demo key'
    scope: productSpectrum.id
    state: 'active'
    allowTracing: true
  }
}

resource nvGatewayKey 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-gateway-key'
  properties: {
    displayName: 'spectrum-gateway-key'
    value: gatewaySubscription.listSecrets().primaryKey
    secret: true
  }
}

resource nvTokensPerMinute 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-tokens-per-minute'
  properties: {
    displayName: 'spectrum-tokens-per-minute'
    value: string(tokensPerMinute)
  }
}

resource nvFoundryAudience 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-foundry-audience'
  properties: {
    displayName: 'spectrum-foundry-audience'
    value: foundryAudience
  }
}

// --- Entra (keyless) inbound auth ------------------------------------------
// These four drive the validate-azure-ad-token block in fragment-llm-common.xml.
// They are named values rather than literals so the gateway can be switched
// between keyless and key auth without touching policy XML.

resource nvAuthMode 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-auth-mode'
  properties: {
    displayName: 'spectrum-auth-mode'
    value: gatewayAuthMode
  }
}

resource nvTenantId 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-tenant-id'
  properties: {
    displayName: 'spectrum-tenant-id'
    value: tenantId
  }
}

resource nvEntraAudience 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-entra-audience'
  properties: {
    displayName: 'spectrum-entra-audience'
    value: entraAudience
  }
}

// Comma-separated, and deliberately allowed to be empty: an empty list means
// "any client in the tenant that holds a token for the audience". The policy
// treats empty as skip-the-check rather than deny-everything, so a gateway is
// never accidentally bricked by an unset value.
resource nvEntraClientIds 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'spectrum-entra-client-ids'
  properties: {
    displayName: 'spectrum-entra-client-ids'
    value: empty(entraAllowedClientIds) ? ' ' : entraAllowedClientIds
  }
}

resource nvManagedComputeUrl 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'mc-backend-url'
  properties: {
    displayName: 'mc-backend-url'
    value: empty(managedComputeBackendUrl) ? notWiredYet : managedComputeBackendUrl
  }
}

resource nvFireworksUrl 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'fireworks-backend-url'
  properties: {
    displayName: 'fireworks-backend-url'
    value: empty(fireworksBackendUrl) ? notWiredYet : fireworksBackendUrl
  }
}

resource nvVmUrl 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'vm-backend-url'
  properties: {
    displayName: 'vm-backend-url'
    value: empty(vmBackendUrl) ? notWiredYet : vmBackendUrl
  }
}

resource nvFoundryLocalUrl 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'foundry-local-backend-url'
  properties: {
    displayName: 'foundry-local-backend-url'
    value: empty(foundryLocalBackendUrl) ? notWiredYet : foundryLocalBackendUrl
  }
}

// --- Shared policy fragment ------------------------------------------------

resource llmCommonFragment 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = {
  parent: apim
  name: 'llm-common'
  properties: {
    description: 'Auth, token limits and metrics shared by all four hosting options.'
    format: 'rawxml'
    value: loadTextContent('../policies/fragment-llm-common.xml')
  }
  dependsOn: [
    nvGatewayKey
    nvTokensPerMinute
    nvAuthMode
    nvTenantId
    nvEntraAudience
    nvEntraClientIds
  ]
}

// --- The four APIs ---------------------------------------------------------

var apiDefinitions = [
  {
    name: 'managed-compute'
    displayName: '01 - Foundry Managed Compute'
    description: 'Dedicated GPU capacity managed by Microsoft Foundry.'
    path: 'v1/managed-compute'
    serviceUrl: empty(managedComputeBackendUrl) ? notWiredYet : managedComputeBackendUrl
    policy: loadTextContent('../policies/managed-compute.xml')
  }
  {
    name: 'fireworks'
    displayName: '02 - Fireworks on Foundry'
    description: 'Partner-managed inference, billed per token.'
    path: 'v1/fireworks'
    serviceUrl: empty(fireworksBackendUrl) ? notWiredYet : fireworksBackendUrl
    policy: loadTextContent('../policies/fireworks.xml')
  }
  {
    name: 'azure-vm'
    displayName: '03 - Customer-managed Azure VM'
    description: 'Your own inference server on a VM you own.'
    path: 'v1/azure-vm'
    serviceUrl: empty(vmBackendUrl) ? notWiredYet : vmBackendUrl
    policy: loadTextContent('../policies/azure-vm.xml')
  }
  {
    name: 'foundry-local'
    displayName: '04 - Foundry Local'
    description: 'A model on your own machine, published through a Dev Tunnel.'
    path: 'v1/foundry-local'
    serviceUrl: empty(foundryLocalBackendUrl) ? notWiredYet : foundryLocalBackendUrl
    policy: loadTextContent('../policies/foundry-local.xml')
  }
]

resource apis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [
  for api in apiDefinitions: {
    parent: apim
    name: api.name
    properties: {
      displayName: api.displayName
      description: api.description
      path: api.path
      serviceUrl: api.serviceUrl
      protocols: [
        'https'
      ]
      // The fragment does the credential check instead, so that one unmodified
      // OpenAI client works against every route regardless of which header it
      // puts the key in. See fragment-llm-common.xml.
      subscriptionRequired: false
      isCurrent: true
    }
  }
]

// Operations are declared per API rather than as a wildcard so the developer
// portal and the OpenAPI export describe a real OpenAI-shaped surface.
resource chatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'chat-completions'
    properties: {
      displayName: 'Create chat completion'
      method: 'POST'
      urlTemplate: '/chat/completions'
      description: 'OpenAI-compatible chat completion. Body: {"model": "...", "messages": [...]}'
    }
  }
]

resource listModels 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'list-models'
    properties: {
      displayName: 'List models'
      method: 'GET'
      urlTemplate: '/models'
      description: 'What this backend will answer to. Useful for verifying a route before sending a prompt.'
    }
  }
]

// Foundry Agent Service does not just forward a BYOM request. Its model gateway
// first probes GET <connection-target>/deployments/<model-name> to validate that
// the deployment exists, in the shape Azure OpenAI would answer. A route that
// only implements /chat/completions returns 404 to that probe, and the agent run
// fails with "Model gateway error: Upstream gateway returned NotFound" - which
// points at the model, not at a missing discovery endpoint.
//
// None of the four backends here are Azure OpenAI, so none of them can answer
// it. The gateway synthesises the response instead: the route's existence IS the
// deployment. This is answered entirely in policy and never reaches a backend.
resource getDeployment 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'get-deployment'
    properties: {
      displayName: 'Get deployment (Foundry BYOM probe)'
      method: 'GET'
      urlTemplate: '/deployments/{deploymentId}'
      description: 'Deployment-discovery probe issued by Foundry Agent Service before a BYOM run.'
      templateParameters: [
        {
          name: 'deploymentId'
          type: 'string'
          required: true
          description: 'Model name as the agent references it.'
        }
      ]
    }
  }
]

resource getDeploymentPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: getDeployment[i]
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: loadTextContent('../policies/operation-get-deployment.xml')
    }
  }
]

resource listDeployments 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'list-deployments'
    properties: {
      displayName: 'List deployments (Foundry BYOM probe)'
      method: 'GET'
      urlTemplate: '/deployments'
      description: 'Deployment-discovery probe issued by Foundry Agent Service before a BYOM run.'
    }
  }
]

resource listDeploymentsPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: listDeployments[i]
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: loadTextContent('../policies/operation-list-deployments.xml')
    }
  }
]

resource apiPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: api.policy
    }
    dependsOn: [
      llmCommonFragment
      nvFoundryAudience
      nvManagedComputeUrl
      nvFireworksUrl
      nvVmUrl
      nvFoundryLocalUrl
      chatCompletions
      listModels
      getDeployment
      listDeployments
    ]
  }
]

// Per-API diagnostics so token metrics are attributed to the right option.
resource apiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: apis[i]
    name: 'applicationinsights'
    properties: {
      loggerId: loggerId
      alwaysLog: 'allErrors'
      verbosity: 'information'
      httpCorrelationProtocol: 'W3C'
      logClientIp: true
      sampling: {
        samplingType: 'fixed'
        percentage: 100
      }
    }
  }
]

resource productApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [
  for (api, i) in apiDefinitions: {
    parent: productSpectrum
    name: api.name
    dependsOn: [
      apis[i]
    ]
  }
]

output apiPaths array = [for api in apiDefinitions: api.path]
output productName string = productSpectrum.name
output subscriptionName string = gatewaySubscription.name

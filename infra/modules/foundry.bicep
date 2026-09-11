// ---------------------------------------------------------------------------
// Microsoft Foundry account + project.
//
// This is the *new* Foundry resource model: a Microsoft.CognitiveServices
// account of kind 'AIServices' with allowProjectManagement enabled. There is
// deliberately no AI Hub and no Machine Learning workspace here.
//
// It is the backing resource for THREE things:
//   Option 1  Managed Compute deployments      (created later by the beta SDK -
//             there is no ARM type for them yet)
//   Option 2  the Fireworks partner deployment (infra/modules/fireworks.bicep)
//   Agents    the Foundry project that hosts the BYOM agents for all 4 options
// ---------------------------------------------------------------------------

@description('Azure region for the Foundry account. Fireworks pay-as-you-go is US-only.')
param location string

@description('Globally unique Foundry account name.')
param accountName string

@description('Foundry project name. Projects are the unit agents live in.')
param projectName string

@description('Friendly display name for the project.')
param projectDisplayName string = 'Self-Hosting Spectrum'

@description('Tags applied to every resource.')
param tags object = {}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
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
    // Required for the https://<name>.services.ai.azure.com endpoint to exist.
    customSubDomainName: accountName

    // Enables the Foundry project model (and therefore Agent Service).
    allowProjectManagement: true

    // Keys stay enabled: the Managed Compute preview API and several portal
    // flows still fall back to key auth. APIM itself uses managed identity.
    disableLocalAuth: false

    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectDisplayName
    description: 'Agents and connections for the four self-hosting options.'
  }
}

output accountName string = account.name
output accountId string = account.id
output accountPrincipalId string = account.identity.principalId

@description('Base endpoint, e.g. https://<account>.services.ai.azure.com/')
output endpoint string = account.properties.endpoint

@description('OpenAI-compatible base path. APIM points its backends at this + /chat/completions.')
output openAiV1Endpoint string = '${account.properties.endpoint}openai/v1'

output projectName string = project.name
output projectId string = project.id

@description('''
Object ID of the project's system-assigned managed identity. This is the
identity Foundry Agent Service presents when it calls the gateway on an agent's
behalf, so it is what the APIM allow-list has to contain. Note this is the
*object* ID; infra/scripts/setup_entra.py resolves it to the application
(client) ID that actually appears in the token's appid claim.
''')
output projectPrincipalId string = project.identity.principalId

@description('Endpoint consumed by azure-ai-projects (AIProjectClient).')
output projectEndpoint string = '${account.properties.endpoint}api/projects/${project.name}'

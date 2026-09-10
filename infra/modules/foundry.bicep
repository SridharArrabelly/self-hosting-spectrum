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

@description('Principal IDs granted data-plane access to the account (e.g. the APIM managed identity).')
param dataPlanePrincipalIds array = []

// Azure AI User - the data-plane role used to call inference endpoints.
// Managed Compute docs are inconsistent about whether this or 'Foundry User' is
// required; this is the role the working deploy-models-managed sample uses.
var azureAiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

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

resource dataPlaneRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (principalId, i) in dataPlanePrincipalIds: {
    name: guid(account.id, principalId, azureAiUserRoleId)
    scope: account
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        azureAiUserRoleId
      )
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output accountName string = account.name
output accountId string = account.id
output accountPrincipalId string = account.identity.principalId

@description('Base endpoint, e.g. https://<account>.services.ai.azure.com/')
output endpoint string = account.properties.endpoint

@description('OpenAI-compatible base path. APIM points its backends at this + /chat/completions.')
output openAiV1Endpoint string = '${account.properties.endpoint}openai/v1'

output projectName string = project.name
output projectId string = project.id

@description('Endpoint consumed by azure-ai-projects (AIProjectClient).')
output projectEndpoint string = '${account.properties.endpoint}api/projects/${project.name}'

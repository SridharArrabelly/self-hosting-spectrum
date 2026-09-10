// ---------------------------------------------------------------------------
// Data-plane access from APIM's managed identity to the Foundry account.
//
// This is a separate module for a wall-clock reason, not a purity one. If the
// role assignment lives inside foundry.bicep, the Foundry account has to wait
// on apim.outputs.apimPrincipalId - which means it waits out APIM's 30-45
// minute provision before it even starts. Splitting the grant out lets the
// account, the project and the Fireworks deployment build in parallel with the
// gateway, and only the grant itself waits for both.
//
// Role note: Microsoft Learn is inconsistent about which role Managed Compute
// needs. managed-compute-overview documents 'Foundry User' with the
// https://ai.azure.com audience; the deploy-models-managed sample uses
// 'Azure AI User' with https://cognitiveservices.azure.com. Both are assigned
// here because they are free, and the audience APIM actually asks for is a
// named value (spectrum-foundry-audience) so it can be flipped in one command.
// ---------------------------------------------------------------------------

@description('Name of the existing Foundry account.')
param accountName string

@description('Principal IDs granted data-plane access, e.g. the APIM managed identity.')
param principalIds array

@description('Principal type, so role assignment does not fail on AAD replication lag.')
param principalType string = 'ServicePrincipal'

// Azure AI User - data-plane access to inference endpoints.
var azureAiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
// Cognitive Services User - covers the /openai/v1 route used by Options 1 and 2.
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

var roleIds = [
  azureAiUserRoleId
  cognitiveServicesUserRoleId
]

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for pair in flatten(map(principalIds, principalId => map(roleIds, roleId => {
    principalId: principalId
    roleId: roleId
  }))): {
    name: guid(account.id, pair.principalId, pair.roleId)
    scope: account
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', pair.roleId)
      principalId: pair.principalId
      principalType: principalType
    }
  }
]

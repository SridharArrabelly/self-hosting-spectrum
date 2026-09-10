// ---------------------------------------------------------------------------
// self-hosting-spectrum - infrastructure orchestrator (resource group scope).
//
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam
//
// Build order matters for wall-clock time, not correctness:
//   APIM on the Developer SKU takes 30-45 minutes, so it is started in the same
//   deployment as everything cheap. Options 1 and 4 are not created here -
//   Managed Compute has no ARM type yet (see 01-managed-compute/) and Foundry
//   Local runs on your machine (see 04-foundry-local/).
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('Short suffix for globally unique names. 3-8 lowercase alphanumerics.')
@minLength(3)
@maxLength(8)
param nameSuffix string = 'shs01'

@description('APIM SKU. Developer is the cheapest with llm-token-limit support.')
param apimSku string = 'Developer'

@description('Publisher email for APIM notifications.')
param publisherEmail string

@description('Publisher organisation name for APIM.')
param publisherName string = 'Self Hosting Spectrum'

@description('Foundry project name.')
param projectName string = 'proj-spectrum'

@description('Deploy the Option 3 VM and its network. Set false to skip.')
param deployVm bool = true

@description('Option 3 VM size. preflight.py verifies availability and quota.')
param vmSize string = 'Standard_D4s_v7'

@description('Option 3 inference runtime.')
@allowed([
  'ollama'
  'vllm'
])
param vmRuntime string = 'ollama'

@description('Model pulled on the Option 3 VM.')
param vmModel string = 'qwen2.5:1.5b-instruct'

@description('Admin username for the Option 3 VM.')
param vmAdminUsername string = 'azureuser'

@description('SSH public key for the Option 3 VM. Required when deployVm is true.')
@secure()
param vmAdminPublicKey string = ''

@description('Optional CIDR allowed to SSH to the Option 3 VM. Empty means no SSH rule.')
param vmSshSourceAddressPrefix string = ''

@description('Deploy the Fireworks partner deployment (Option 2).')
param deployFireworks bool = true

@description('Fireworks catalog model id. Small models are PTU-only; this one is pay-as-you-go.')
param fireworksModel string = 'FW-Nemotron-Lightning-3.5-30B-A3B'

@description('Name of the Fireworks deployment. This is what clients send as "model".')
param fireworksDeploymentName string = 'fireworks'

var tags = {
  project: 'self-hosting-spectrum'
  managedBy: 'bicep'
}

var foundryAccountName = 'aif-spectrum-${nameSuffix}'
var apimName = 'apim-spectrum-${nameSuffix}'

// --- Gateway ---------------------------------------------------------------
// Deployed first because it is the slowest thing here by an order of magnitude.
module apim 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    location: location
    apimName: apimName
    sku: apimSku
    publisherEmail: publisherEmail
    publisherName: publisherName
    logAnalyticsName: 'log-spectrum-${nameSuffix}'
    appInsightsName: 'appi-spectrum-${nameSuffix}'
    tags: tags
  }
}

// --- Foundry ---------------------------------------------------------------
// Backs Option 1 (Managed Compute), Option 2 (Fireworks) and the agents.
// APIM's managed identity gets data-plane access so its policies can call
// Foundry without a key.
module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: location
    accountName: foundryAccountName
    projectName: projectName
    tags: tags
    dataPlanePrincipalIds: [
      apim.outputs.apimPrincipalId
    ]
  }
}

// --- Option 2: Fireworks ---------------------------------------------------
module fireworks 'modules/fireworks.bicep' = if (deployFireworks) {
  name: 'fireworks'
  params: {
    foundryAccountName: foundry.outputs.accountName
    deploymentName: fireworksDeploymentName
    modelName: fireworksModel
  }
}

// --- Option 3: customer-managed VM -----------------------------------------
module vm 'modules/vm-inference.bicep' = if (deployVm) {
  name: 'vm-inference'
  params: {
    location: location
    nameSuffix: nameSuffix
    vmSize: vmSize
    runtime: vmRuntime
    model: vmModel
    adminUsername: vmAdminUsername
    adminPublicKey: vmAdminPublicKey
    sshSourceAddressPrefix: vmSshSourceAddressPrefix
    tags: tags
  }
}

output apimName string = apim.outputs.apimName
output apimGatewayUrl string = apim.outputs.gatewayUrl
output apimPrincipalId string = apim.outputs.apimPrincipalId
output appInsightsName string = apim.outputs.appInsightsName

output foundryAccountName string = foundry.outputs.accountName
output foundryEndpoint string = foundry.outputs.endpoint
output foundryOpenAiV1Endpoint string = foundry.outputs.openAiV1Endpoint
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint

output fireworksDeploymentName string = deployFireworks ? fireworks!.outputs.deploymentName : ''
output vmBackendUrl string = deployVm ? vm!.outputs.backendUrl : ''
output vmPrivateIp string = deployVm ? vm!.outputs.privateIp : ''

// ---------------------------------------------------------------------------
// Parameters for infra/main.bicep.
//
// Values come from environment variables so this file can be committed without
// leaking anything. infra/scripts/deploy.py loads .env and exports them before
// invoking the deployment, so in practice you edit .env, not this file.
//
//   uv run python infra/scripts/deploy.py
// ---------------------------------------------------------------------------

using './main.bicep'

param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus2')
param nameSuffix = readEnvironmentVariable('NAME_SUFFIX', 'shs01')

param apimSku = readEnvironmentVariable('APIM_SKU', 'Developer')
param publisherEmail = readEnvironmentVariable('APIM_PUBLISHER_EMAIL', 'admin@example.com')
param publisherName = readEnvironmentVariable('APIM_PUBLISHER_NAME', 'Self Hosting Spectrum')

param projectName = readEnvironmentVariable('FOUNDRY_PROJECT_NAME', 'proj-spectrum')

// az ad signed-in-user show --query id -o tsv
param developerPrincipalId = readEnvironmentVariable('DEVELOPER_PRINCIPAL_ID', '')

param deployFireworks = bool(readEnvironmentVariable('DEPLOY_FIREWORKS', 'true'))
param fireworksModel = readEnvironmentVariable('FIREWORKS_MODEL', 'FW-GLM-5.3-Flash')
param fireworksSku = readEnvironmentVariable('FIREWORKS_SKU', 'GlobalStandard')
param fireworksDeploymentName = readEnvironmentVariable('FIREWORKS_DEPLOYMENT_NAME', 'fireworks')

param deployVm = bool(readEnvironmentVariable('DEPLOY_VM', 'true'))
param vmSize = readEnvironmentVariable('VM_SIZE', 'Standard_D4s_v7')
param vmRuntime = readEnvironmentVariable('VM_RUNTIME', 'ollama')
param vmModel = readEnvironmentVariable('VM_MODEL', 'qwen2.5:1.5b-instruct')
param vmAdminUsername = readEnvironmentVariable('VM_ADMIN_USERNAME', 'azureuser')
param vmAdminPublicKey = readEnvironmentVariable('VM_ADMIN_PUBLIC_KEY', '')
param vmSshSourceAddressPrefix = readEnvironmentVariable('VM_SSH_SOURCE_CIDR', '')

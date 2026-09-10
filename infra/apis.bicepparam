using 'apis.bicep'

// Values come from the environment so nothing secret or machine-specific is
// committed. infra/scripts/deploy_apis.py loads .env and exports them.

param nameSuffix = readEnvironmentVariable('NAME_SUFFIX', 'shs01')
param tokensPerMinute = int(readEnvironmentVariable('APIM_TOKENS_PER_MINUTE', '20000'))
param foundryAudience = readEnvironmentVariable('FOUNDRY_AAD_AUDIENCE', 'https://cognitiveservices.azure.com')
param vmBackendUrl = readEnvironmentVariable('VM_BACKEND_URL', '')
param foundryLocalBackendUrl = readEnvironmentVariable('FOUNDRY_LOCAL_BACKEND_URL', '')
param managedComputeDeploymentName = readEnvironmentVariable('MC_DEPLOYMENT_NAME', '')

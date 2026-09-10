// ---------------------------------------------------------------------------
// Option 2 - Fireworks on Foundry.
//
// Fireworks is NOT a Marketplace SaaS offer. It is an ordinary Cognitive
// Services deployment whose model format is 'Fireworks', which is why it can be
// created in plain Bicep with no 'az term accept' dance.
//
// Two things routinely trip people up:
//
//   1. The subscription needs the preview feature Fireworks.EnableDeploy
//      registered, and it takes ~30 minutes to propagate.
//      infra/scripts/preflight.py --fix does this.
//
//   2. Nearly every *small* Fireworks model is provisioned-throughput only,
//      priced at 40-550 PTU. The default here is deliberately a
//      pay-as-you-go model so a smoke test costs a fraction of a cent.
//      Per-token models get only a 15-day retirement notice, hence the param.
// ---------------------------------------------------------------------------

@description('Name of the existing Foundry (AIServices) account.')
param foundryAccountName string

@description('Deployment name. NOTE: this is what clients send as "model", not the FW- catalog id.')
param deploymentName string = 'fireworks'

@description('Fireworks catalog model id.')
param modelName string = 'FW-Nemotron-Lightning-3.5-30B-A3B'

@description('Model version. Leave at 1 unless the catalog says otherwise.')
param modelVersion string = '1'

@description('Capacity in thousands of tokens per minute for the pay-as-you-go SKU.')
// DataZoneStandard bills per token, so capacity is purely a rate-limit dial and
// raising it does not raise the bill. It does raise the request ceiling though:
// capacity 1 yields exactly 1 request/min, which makes back-to-back calls
// (benchmark.py, client.py --all) fail with HTTP 429 RateLimitReached.
param capacity int = 50

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: deploymentName
  sku: {
    // DataZoneStandard is the pay-as-you-go SKU for partner models.
    // GlobalProvisionedManaged would be the PTU path - far more expensive.
    name: 'DataZoneStandard'
    capacity: capacity
  }
  properties: {
    model: {
      format: 'Fireworks'
      name: modelName
      version: modelVersion
    }
  }
}

output deploymentName string = deployment.name
output deploymentId string = deployment.id
output modelName string = modelName

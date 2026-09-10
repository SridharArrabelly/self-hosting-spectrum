// ---------------------------------------------------------------------------
// Option 3 - customer-managed inference on an Azure VM.
//
// You own everything above the hypervisor: the OS, the serving engine, the
// model weights, the patching. In exchange you can run any model, any runtime,
// on any SKU you have quota for.
//
// SKU / runtime pairing (both are parameters so this module serves CPU-now and
// GPU-later without a rewrite):
//
//   runtime=ollama  CPU or GPU. llama.cpp under the hood. Default.
//   runtime=vllm    GPU only, and only SM75+ - mainline vLLM >= 0.20 dropped
//                   SM70, so a V100 (NC6s_v3) needs Ollama instead.
//
// Network exposure: the VM gets a public IP, but the NSG only admits the
// inference port from the ApiManagement service tag, so APIM is the sole
// ingress path. That keeps this module deployable in parallel with APIM
// (referencing APIM's IP directly would serialise a 40-minute dependency).
// For production, inject APIM into a VNet and drop the public IP entirely -
// see the README.
// ---------------------------------------------------------------------------

@description('Azure region.')
param location string

@description('Short suffix for resource names.')
param nameSuffix string

@description('VM size. Verify with preflight.py - regions differ a lot on which v-series they offer.')
param vmSize string = 'Standard_D4s_v7'

@description('Inference runtime to install.')
@allowed([
  'ollama'
  'vllm'
])
param runtime string = 'ollama'

@description('Model tag pulled at first boot.')
param model string = 'qwen2.5:1.5b-instruct'

@description('Admin username.')
param adminUsername string = 'azureuser'

@description('SSH public key for the admin user.')
@secure()
param adminPublicKey string

@description('Optional CIDR allowed to SSH. Leave empty to create no SSH rule at all.')
param sshSourceAddressPrefix string = ''
@description('Public IPs the APIM gateway calls backends from. See the NSG rule below for why the ApiManagement service tag is the wrong answer here.')
param apimOutboundIpAddresses array
@description('Tags applied to every resource.')
param tags object = {}

var vmName = 'vm-inference-${nameSuffix}'
var inferencePort = runtime == 'ollama' ? 11434 : 8000

// Ollama and vLLM both expose an OpenAI-compatible API rooted at /v1.
var backendUrl = 'http://${publicIp.properties.dnsSettings.fqdn}:${inferencePort}/v1'

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-inference-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    securityRules: concat(
      [
        {
          name: 'allow-apim-inference'
          properties: {
            description: 'Only API Management may reach the inference port.'
            protocol: 'Tcp'
            sourcePortRange: '*'
            destinationPortRange: string(inferencePort)
            // The ApiManagement service tag covers the *inbound management*
            // endpoints of APIM, not the address a gateway calls a backend
            // from. Using the tag here looks right and silently fails: the
            // gateway request times out and APIM reports a bare HTTP 500.
            // A non-VNet APIM egresses from its own instance public IP, so
            // that exact address is what has to be allowed.
            sourceAddressPrefixes: apimOutboundIpAddresses
            destinationAddressPrefix: '*'
            access: 'Allow'
            priority: 100
            direction: 'Inbound'
          }
        }
      ],
      empty(sshSourceAddressPrefix)
        ? []
        : [
            {
              name: 'allow-ssh'
              properties: {
                description: 'Operator SSH for troubleshooting.'
                protocol: 'Tcp'
                sourcePortRange: '*'
                destinationPortRange: '22'
                sourceAddressPrefix: sshSourceAddressPrefix
                destinationAddressPrefix: '*'
                access: 'Allow'
                priority: 110
                direction: 'Inbound'
              }
            }
          ]
    )
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-spectrum-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-inference'
        properties: {
          addressPrefix: '10.42.1.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-inference-${nameSuffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: {
      // Stable FQDN so the APIM backend URL survives a VM rebuild.
      domainNameLabel: 'shs-inference-${nameSuffix}'
    }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'nic-inference-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: vnet.properties.subnets[0].id
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: 128
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminPublicKey
            }
          ]
        }
      }
      customData: base64(
        replace(
          replace(loadTextContent('../../03-azure-gpu-vm/cloud-init.yaml'), '__RUNTIME__', runtime),
          '__MODEL__',
          model
        )
      )
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

output vmName string = vm.name
output privateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output publicIp string = publicIp.properties.ipAddress
output fqdn string = publicIp.properties.dnsSettings.fqdn

@description('OpenAI-compatible base URL. APIM appends /chat/completions.')
output backendUrl string = backendUrl

output inferencePort int = inferencePort

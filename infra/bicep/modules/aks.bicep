@description('AKS cluster name.')
param clusterName string

@description('Azure location for this AKS cluster.')
param location string

@description('Globally unique DNS prefix for the AKS API endpoint.')
param dnsPrefix string

@description('A supported non-preview Kubernetes version chosen by preflight.')
param kubernetesVersion string

@description('SSH public key only. Private keys are never accepted by this template.')
param sshPublicKey string

param adminUsername string
param nodeVmSize string
param nodeCount int
param maxPods int
param tags object

resource cluster 'Microsoft.ContainerService/managedClusters@2024-10-01' = {
  name: clusterName
  location: location
  tags: tags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dnsPrefix: dnsPrefix
    kubernetesVersion: kubernetesVersion
    enableRBAC: true
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    linuxProfile: {
      adminUsername: adminUsername
      ssh: {
        publicKeys: [
          {
            keyData: sshPublicKey
          }
        ]
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: nodeCount
        vmSize: nodeVmSize
        osType: 'Linux'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
        osDiskType: 'Managed'
        osDiskSizeGB: 64
        maxPods: maxPods
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
    }
  }
}

output clusterId string = cluster.id
output fqdn string = cluster.properties.fqdn

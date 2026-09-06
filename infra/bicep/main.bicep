// Subscription-scoped entry point.  This file creates one resource group and
// two independent AKS clusters; it does not create application secrets or
// deploy workloads.
targetScope = 'subscription'

@description('Name of the single resource group that holds the research resources.')
param resourceGroupName string

@description('Azure location for the first independent benchmark cluster.')
param primaryLocation string

@description('Azure location for the second independent benchmark cluster.')
param secondaryLocation string

@description('AKS name for the primary regional cluster.')
param primaryClusterName string

@description('AKS name for the secondary regional cluster.')
param secondaryClusterName string

@description('A globally unique DNS prefix for the primary cluster.')
param primaryDnsPrefix string

@description('A globally unique DNS prefix for the secondary cluster.')
param secondaryDnsPrefix string

@description('A supported non-preview AKS version selected during preflight.')
param kubernetesVersion string

@description('SSH public key used for AKS node access. Do not put private keys in parameter files.')
param sshPublicKey string

@description('Linux administrator name for AKS worker nodes.')
param adminUsername string = 'azureuser'

@description('VM SKU to validate and provision in both regional node pools.')
param nodeVmSize string = 'Standard_B2s'

@minValue(1)
@maxValue(3)
@description('Fixed number of system nodes in each cluster. AKS is stopped between sessions instead of scaled by this deployment.')
param nodeCount int = 2

@minValue(30)
@maxValue(250)
@description('Maximum number of pods per node. Keep this small for a student-scale deployment.')
param maxPods int = 30

@description('Tags applied to every resource created by this deployment.')
param tags object = {
  project: 'trust-gated-carbon-scheduler'
  environment: 'research'
  managedBy: 'bicep'
}

resource researchResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: primaryLocation
  tags: tags
}

module primaryCluster 'modules/aks.bicep' = {
  name: 'primary-aks'
  scope: researchResourceGroup
  params: {
    adminUsername: adminUsername
    clusterName: primaryClusterName
    dnsPrefix: primaryDnsPrefix
    kubernetesVersion: kubernetesVersion
    location: primaryLocation
    maxPods: maxPods
    nodeCount: nodeCount
    nodeVmSize: nodeVmSize
    sshPublicKey: sshPublicKey
    tags: union(tags, {
      schedulerRegionRole: 'primary'
    })
  }
}

module secondaryCluster 'modules/aks.bicep' = {
  name: 'secondary-aks'
  scope: researchResourceGroup
  params: {
    adminUsername: adminUsername
    clusterName: secondaryClusterName
    dnsPrefix: secondaryDnsPrefix
    kubernetesVersion: kubernetesVersion
    location: secondaryLocation
    maxPods: maxPods
    nodeCount: nodeCount
    nodeVmSize: nodeVmSize
    sshPublicKey: sshPublicKey
    tags: union(tags, {
      schedulerRegionRole: 'secondary'
    })
  }
}

output resourceGroupId string = researchResourceGroup.id
output primaryClusterId string = primaryCluster.outputs.clusterId
output secondaryClusterId string = secondaryCluster.outputs.clusterId

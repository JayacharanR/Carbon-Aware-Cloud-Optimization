# Azure infrastructure

`main.bicep` is a subscription-scoped deployment that creates one tagged resource group and two independent AKS clusters. It deliberately does not create a cross-region network, database replication, a container registry, secrets, or a workload deployment. The project treats a region change as a new independent benchmark session.

Before running it, use `scripts/azure-preflight.ps1` to check the selected regions and VM size against the active subscription. Create the subscription budget with `budget.bicep` before creating clusters. Both files require real values and fail rather than inventing an Azure region, Kubernetes version, billing amount, or notification address.

Copy `main.bicepparam.example` to an untracked file, fill it with the selected values, and use `scripts/deploy-aks.ps1 -Apply`. The generated resource group and clusters are tagged for cost tracking. The template uses the AKS Free control-plane tier; worker-node and associated Azure resource charges still apply.

The chosen node count and SKU are inputs, not capacity guarantees. The full Social Network chart has many services, so a smoke deployment and pilot must verify that the selected student-subscription quota and node pool can actually schedule it in both regions.

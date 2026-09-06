<#
.SYNOPSIS
Collects artifacts through the project collector, then stops exactly the two
named AKS clusters. It never deletes Azure resources or Kubernetes data.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$PrimaryCluster,
    [Parameter(Mandatory)][string]$SecondaryCluster,
    [string]$SubscriptionId,
    [string]$RunId,
    [string]$ArtifactDirectory,
    [switch]$SkipCollection
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is required.' }
if ($SubscriptionId) {
    & az account set --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Unable to select subscription $SubscriptionId." }
}

if (-not $SkipCollection) {
    if ([string]::IsNullOrWhiteSpace($RunId) -or [string]::IsNullOrWhiteSpace($ArtifactDirectory)) {
        throw 'RunId and ArtifactDirectory are required unless -SkipCollection is explicitly supplied.'
    }
    $collector = Join-Path $repoRoot 'scripts/collect_results.py'
    if (-not (Test-Path $collector -PathType Leaf)) {
        throw "Required collector does not exist: $collector. Refusing to stop clusters before results are collected."
    }
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python is required to collect results.' }
    & python $collector --run-id $RunId --output-directory $ArtifactDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Artifact collection failed. Clusters were not stopped.' }
}

foreach ($cluster in @($PrimaryCluster, $SecondaryCluster)) {
    $powerState = & az aks show --resource-group $ResourceGroup --name $cluster --query 'powerState.code' --output tsv
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect AKS cluster $cluster." }
    if ($powerState -eq 'Stopped') {
        Write-Host "$cluster is already stopped."
        continue
    }
    Write-Host "Stopping $cluster..."
    & az aks stop --resource-group $ResourceGroup --name $cluster --output none
    if ($LASTEXITCODE -ne 0) { throw "Unable to stop AKS cluster $cluster." }
}

Write-Host 'Requested clusters are stopped. No resources were deleted.'

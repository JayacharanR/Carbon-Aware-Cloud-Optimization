<#
.SYNOPSIS
Starts exactly the two named AKS clusters and optionally refreshes user
kubeconfig credentials. It never creates clusters or changes node-pool sizes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$PrimaryCluster,
    [Parameter(Mandatory)][string]$SecondaryCluster,
    [string]$SubscriptionId,
    [string]$KubeConfigPath,
    [switch]$SkipCredentials,
    [ValidateRange(1, 60)][int]$TimeoutMinutes = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$envLoader = Join-Path $PSScriptRoot 'load-project-env.ps1'
if (Test-Path -LiteralPath $envLoader -PathType Leaf) {
    . $envLoader
    Import-ProjectDotEnv -Path (Join-Path $repoRoot '.env')
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is required.' }
if (-not $SkipCredentials -and -not (Get-Command kubectl -ErrorAction SilentlyContinue)) { throw 'kubectl is required unless -SkipCredentials is supplied.' }
if ($SubscriptionId) {
    & az account set --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Unable to select subscription $SubscriptionId." }
}
if (-not $SkipCredentials -and [string]::IsNullOrWhiteSpace($KubeConfigPath)) {
    $KubeConfigPath = Join-Path $env:USERPROFILE '.kube/config'
}

function Start-Cluster {
    param([Parameter(Mandatory)][string]$ClusterName)
    $powerState = & az aks show --resource-group $ResourceGroup --name $ClusterName --query 'powerState.code' --output tsv
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect AKS cluster $ClusterName." }
    if ($powerState -ne 'Running') {
        Write-Host "Starting $ClusterName..."
        & az aks start --resource-group $ResourceGroup --name $ClusterName --output none
        if ($LASTEXITCODE -ne 0) { throw "Unable to start AKS cluster $ClusterName." }
    }
    else {
        Write-Host "$ClusterName is already running."
    }

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        $state = & az aks show --resource-group $ResourceGroup --name $ClusterName --query 'powerState.code' --output tsv
        if ($LASTEXITCODE -ne 0) { throw "Unable to inspect power state for $ClusterName." }
        if ($state -eq 'Running') { break }
        Start-Sleep -Seconds 15
    } while ((Get-Date) -lt $deadline)
    if ($state -ne 'Running') { throw "$ClusterName did not reach Running within $TimeoutMinutes minutes." }

    if (-not $SkipCredentials) {
        & az aks get-credentials --resource-group $ResourceGroup --name $ClusterName --file $KubeConfigPath --overwrite-existing
        if ($LASTEXITCODE -ne 0) { throw "Unable to refresh kubeconfig credentials for $ClusterName." }
    }
}

Start-Cluster $PrimaryCluster
Start-Cluster $SecondaryCluster
Write-Host 'Both named AKS clusters are running.'

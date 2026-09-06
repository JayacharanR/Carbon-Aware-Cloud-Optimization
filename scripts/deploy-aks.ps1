<#
.SYNOPSIS
Runs a subscription-scope AKS Bicep what-if by default, or creates resources
only when -Apply is supplied.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path $_ -PathType Leaf })]
    [string]$ParameterFile,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$BudgetName,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$DeploymentLocation,

    [string]$SubscriptionId,

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$template = Join-Path $repoRoot 'infra/bicep/main.bicep'
$content = Get-Content -Raw $ParameterFile

if ($content -match 'REPLACE_ME') {
    throw 'The AKS parameter file still contains REPLACE_ME values.'
}
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) is required.'
}
if ($SubscriptionId) {
    & az account set --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Unable to select subscription $SubscriptionId." }
}

# This makes cost protection a deployment prerequisite rather than an optional
# post-provisioning task. Do not use -Apply if Azure cannot show the budget.
& az consumption budget show --budget-name $BudgetName --output none
if ($LASTEXITCODE -ne 0) {
    throw "The required subscription budget '$BudgetName' could not be verified. Create it with deploy-budget.ps1 first."
}

$deploymentName = "carbon-scheduler-aks-$((Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss'))"
$arguments = @('deployment', 'sub')
if ($Apply) {
    $arguments += @('create', '--name', $deploymentName)
}
else {
    $arguments += @('what-if')
}
$arguments += @('--location', $DeploymentLocation, '--template-file', $template, '--parameters', $ParameterFile)

Write-Host ($(if ($Apply) { 'Creating/updating AKS resources.' } else { 'Showing AKS what-if only. Re-run with -Apply to create resources.' }))
& az @arguments
if ($LASTEXITCODE -ne 0) { throw 'AKS deployment command failed.' }

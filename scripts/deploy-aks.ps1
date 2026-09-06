<#
.SYNOPSIS
Runs a subscription-scope AKS Bicep what-if by default, or creates resources
only when -Apply is supplied.
#>
[CmdletBinding()]
param(
    [string]$ParameterFile,

    [string]$BudgetName,

    [string]$DeploymentLocation,

    [string]$SubscriptionId,

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$envLoader = Join-Path $PSScriptRoot 'load-project-env.ps1'
if (Test-Path -LiteralPath $envLoader -PathType Leaf) {
    . $envLoader
    Import-ProjectDotEnv -Path (Join-Path $repoRoot '.env')
}
$ParameterFile = if ($ParameterFile) { $ParameterFile } else { Join-Path $repoRoot 'infra/bicep/main.bicepparam' }
if (-not (Test-Path -LiteralPath $ParameterFile -PathType Leaf)) {
    throw "AKS parameter file was not found: $ParameterFile. Run python scripts/bootstrap.py --phase infra first."
}
$SubscriptionId = if ($SubscriptionId) { $SubscriptionId } else { $env:AZURE_SUBSCRIPTION_ID }
$BudgetName = if ($BudgetName) { $BudgetName } else { $env:AZURE_BUDGET_NAME }
$DeploymentLocation = if ($DeploymentLocation) { $DeploymentLocation } else { $env:AZURE_DEPLOYMENT_LOCATION }
if ([string]::IsNullOrWhiteSpace($BudgetName)) { throw 'BudgetName is required; set AZURE_BUDGET_NAME in .env.' }
if ([string]::IsNullOrWhiteSpace($DeploymentLocation)) { throw 'DeploymentLocation is required; set AZURE_DEPLOYMENT_LOCATION in .env.' }
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

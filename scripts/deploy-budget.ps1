<#
.SYNOPSIS
Creates or updates the subscription budget only when -Apply is specified.
#>
[CmdletBinding()]
param(
    [string]$ParameterFile,

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
$ParameterFile = if ($ParameterFile) { $ParameterFile } else { Join-Path $repoRoot 'infra/bicep/budget.bicepparam' }
if (-not (Test-Path -LiteralPath $ParameterFile -PathType Leaf)) {
    throw "Budget parameter file was not found: $ParameterFile. Run python scripts/bootstrap.py --phase infra first."
}
$SubscriptionId = if ($SubscriptionId) { $SubscriptionId } else { $env:AZURE_SUBSCRIPTION_ID }
$template = Join-Path $repoRoot 'infra/bicep/budget.bicep'
$content = Get-Content -Raw $ParameterFile

if ($content -match 'REPLACE_ME') {
    throw 'The budget parameter file still contains REPLACE_ME values.'
}
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) is required.'
}
if ($SubscriptionId) {
    & az account set --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Unable to select subscription $SubscriptionId." }
}

$deploymentName = "carbon-scheduler-budget-$((Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss'))"
$arguments = @('deployment', 'sub')
if ($Apply) {
    $arguments += @('create', '--name', $deploymentName)
}
else {
    $arguments += @('what-if')
}
$arguments += @('--location', 'eastus', '--template-file', $template, '--parameters', $ParameterFile)

Write-Host ($(if ($Apply) { 'Applying subscription budget deployment.' } else { 'Showing budget what-if only. Re-run with -Apply to create it.' }))
& az @arguments
if ($LASTEXITCODE -ne 0) { throw 'Budget deployment command failed.' }

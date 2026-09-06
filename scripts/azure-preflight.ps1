<#
.SYNOPSIS
Performs non-mutating local and subscription checks before any AKS deployment.

.DESCRIPTION
This script intentionally verifies availability signals, not a capacity
guarantee. Azure quota, spot capacity, Electricity Maps provider-region
coverage, and the selected DeathStarBench revision must still be confirmed
before creating a research run.
#>
[CmdletBinding()]
param(
    [string]$PrimaryRegion,

    [string]$SecondaryRegion,

    [ValidatePattern('^[A-Za-z0-9_\.\-]+$')]
    [string]$NodeVmSize,

    [string]$SubscriptionId,

    [string]$BudgetName,

    [switch]$SkipBudgetCheck,

    [switch]$SkipOllamaCheck,

    [switch]$SkipManifestRender
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$envLoader = Join-Path $PSScriptRoot 'load-project-env.ps1'
if (Test-Path -LiteralPath $envLoader -PathType Leaf) {
    . $envLoader
    Import-ProjectDotEnv -Path (Join-Path $repoRoot '.env')
}
$PrimaryRegion = if ($PrimaryRegion) { $PrimaryRegion } else { $env:AZURE_PRIMARY_REGION }
$SecondaryRegion = if ($SecondaryRegion) { $SecondaryRegion } else { $env:AZURE_SECONDARY_REGION }
$NodeVmSize = if ($NodeVmSize) { $NodeVmSize } else { $env:AZURE_NODE_VM_SIZE }
$SubscriptionId = if ($SubscriptionId) { $SubscriptionId } else { $env:AZURE_SUBSCRIPTION_ID }
$BudgetName = if ($BudgetName) { $BudgetName } else { $env:AZURE_BUDGET_NAME }
foreach ($region in @{'PrimaryRegion'=$PrimaryRegion; 'SecondaryRegion'=$SecondaryRegion}) {
    if ([string]::IsNullOrWhiteSpace($region.Value) -or $region.Value -notmatch '^[a-z0-9]+$') {
        throw "$($region.Key) must be a lowercase Azure region; set it in .env or pass the parameter."
    }
}
if ([string]::IsNullOrWhiteSpace($NodeVmSize)) { $NodeVmSize = 'Standard_B2s' }
$failures = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

function Require-Command {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        $script:failures.Add("Required command was not found: $Name")
    }
}

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $output = & az @Arguments --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az $($Arguments -join ' ') failed: $($output | Out-String)"
    }
    return ($output | Out-String | ConvertFrom-Json)
}

Require-Command az
Require-Command kubectl
Require-Command helm
Require-Command git
Require-Command docker
Require-Command python

if ($failures.Count -eq 0) {
    try {
        $account = Invoke-AzJson @('account', 'show')
        if ($SubscriptionId) {
            if ($account.id -ne $SubscriptionId) {
                & az account set --subscription $SubscriptionId
                if ($LASTEXITCODE -ne 0) {
                    throw "Unable to select subscription $SubscriptionId."
                }
                $account = Invoke-AzJson @('account', 'show')
            }
        }
        Write-Host "Azure subscription: $($account.name) ($($account.id))"
    }
    catch {
        $failures.Add($_.Exception.Message)
    }
}

if ($failures.Count -eq 0) {
    foreach ($region in @($PrimaryRegion, $SecondaryRegion)) {
        try {
            $skus = @(Invoke-AzJson @('vm', 'list-skus', '--location', $region, '--size', $NodeVmSize, '--all'))
            $sku = $skus | Where-Object { $_.resourceType -eq 'virtualMachines' } | Select-Object -First 1
            if (-not $sku) {
                $failures.Add("VM SKU $NodeVmSize was not returned for $region.")
                continue
            }
            if (@($sku.restrictions).Count -gt 0) {
                $warnings.Add("Azure returned restrictions for $NodeVmSize in $region; inspect them before deployment.")
            }

            $versions = Invoke-AzJson @('aks', 'get-versions', '--location', $region)
            $stableVersions = @($versions.orchestrators | Where-Object { $_.isPreview -ne $true })
            if ($stableVersions.Count -eq 0) {
                $failures.Add("No non-preview AKS version was returned for $region.")
            }
            else {
                Write-Host "AKS region check passed: $region ($NodeVmSize; $($stableVersions.Count) non-preview versions returned)"
            }
        }
        catch {
            $failures.Add("Azure region check failed for ${region}: $($_.Exception.Message)")
        }
    }
}

if (-not $SkipBudgetCheck) {
    if ([string]::IsNullOrWhiteSpace($BudgetName)) {
        $failures.Add('BudgetName is required unless -SkipBudgetCheck is explicitly supplied.')
    }
    elseif ($failures.Count -eq 0) {
        try {
            $null = Invoke-AzJson @('consumption', 'budget', 'show', '--budget-name', $BudgetName)
            Write-Host "Budget check passed: $BudgetName"
        }
        catch {
            $failures.Add("Budget '$BudgetName' could not be verified: $($_.Exception.Message)")
        }
    }
}

if (-not $env:ELECTRICITY_MAPS_API_KEY) {
    $failures.Add('ELECTRICITY_MAPS_API_KEY is not set. The Python carbon preflight must validate both configured provider-region identifiers with this key.')
}
else {
    Write-Host 'Electricity Maps API key is present in the environment (value not printed).'
}

if (-not $SkipOllamaCheck) {
    if ([string]::IsNullOrWhiteSpace($env:OLLAMA_BASE_URL)) {
        $failures.Add('OLLAMA_BASE_URL is not set. Set it to the already-secured endpoint the controller will use.')
    }
    else {
        try {
            $endpoint = "$($env:OLLAMA_BASE_URL.TrimEnd('/'))/api/tags"
            $response = Invoke-WebRequest -Uri $endpoint -Method Get -TimeoutSec 10 -UseBasicParsing
            if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
                throw "Unexpected HTTP status $($response.StatusCode)."
            }
            Write-Host 'Ollama endpoint check passed from this machine.'
        }
        catch {
            $failures.Add("Ollama endpoint check failed: $($_.Exception.Message)")
        }
    }
}

if (-not $SkipManifestRender) {
    try {
        & az bicep build --file (Join-Path $repoRoot 'infra/bicep/main.bicep') --stdout 1>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'Bicep compilation failed.'
        }
        & kubectl kustomize (Join-Path $repoRoot 'deploy/controller') 1>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'Controller Kustomize render failed.'
        }
        Write-Host 'Local Bicep and controller-manifest render checks passed.'
    }
    catch {
        $failures.Add($_.Exception.Message)
    }
}

foreach ($warning in $warnings) {
    Write-Warning $warning
}

if ($failures.Count -gt 0) {
    Write-Error ("Preflight failed:`n - " + ($failures -join "`n - "))
    exit 1
}

Write-Host 'Preflight completed. This does not prove regional capacity, quota headroom, or Electricity Maps provider-region coverage; validate those with the completed experiment configuration before deployment.'

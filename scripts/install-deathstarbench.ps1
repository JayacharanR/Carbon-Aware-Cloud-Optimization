<#
.SYNOPSIS
Deploys one pinned DeathStarBench Social Network Helm release to one already
selected cluster and stores a rendered-manifest artifact for graph ingestion.

.DESCRIPTION
The caller must provide a local checkout already detached at the exact
immutable commit. This script does not checkout a moving branch or guess an
application endpoint. It does not reset a live release or claim that an
existing database fixture is clean.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Context,
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path $_ -PathType Container })]
    [string]$DeathStarBenchSource,
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$DeathStarBenchCommit,
    [Parameter(Mandatory)][string]$DsbToolsImage,
    [Parameter(Mandatory)][string]$RenderedManifestPath,
    [string]$Namespace = 'benchmark',
    [string]$ReleaseName = 'social-network',
    [ValidateSet('compose-post.lua', 'read-home-timeline.lua', 'read-user-timeline.lua')]
    [string]$WorkloadScript = 'compose-post.lua',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $Apply) {
    throw 'This command deploys/updates a Helm release. Re-run with -Apply after reviewing the target context.'
}
foreach ($command in @('kubectl', 'helm', 'git')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "$command is required." }
}
if ($DsbToolsImage -match 'REPLACE_ME' -or [string]::IsNullOrWhiteSpace($DsbToolsImage)) {
    throw 'DsbToolsImage must be a pushed image built from this exact DeathStarBench commit.'
}

$sourceGitDirectory = Join-Path $DeathStarBenchSource '.git'
if (-not (Test-Path $sourceGitDirectory)) {
    throw "DeathStarBenchSource is not a Git checkout: $DeathStarBenchSource"
}
$actualCommit = (& git -C $DeathStarBenchSource rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to read DeathStarBench checkout commit.' }
if ($actualCommit.ToLowerInvariant() -ne $DeathStarBenchCommit.ToLowerInvariant()) {
    throw "Checkout is at $actualCommit, not required commit $DeathStarBenchCommit. Checkout the immutable revision before deploying."
}
$submoduleStatus = @(& git -C $DeathStarBenchSource submodule status --recursive)
if ($LASTEXITCODE -ne 0 -or ($submoduleStatus | Where-Object { $_ -match '^[-+]' })) {
    throw 'DeathStarBench submodules are not fully initialized. Run git submodule update --init --recursive in the source checkout.'
}

$chartPath = Join-Path $DeathStarBenchSource 'socialNetwork/helm-chart/socialnetwork'
$valuesPath = Join-Path $repoRoot 'deploy/deathstarbench/values.yaml'
$workloadPath = Join-Path $DeathStarBenchSource "socialNetwork/wrk2/scripts/social-network/$WorkloadScript"
if (-not (Test-Path $chartPath -PathType Container)) { throw "Expected Social Network Helm chart was not found: $chartPath" }
if (-not (Test-Path $workloadPath -PathType Leaf)) { throw "Expected upstream workload script was not found: $workloadPath" }

& helm dependency build $chartPath
if ($LASTEXITCODE -ne 0) { throw 'Helm dependency build failed.' }

$renderedDirectory = Split-Path -Parent $RenderedManifestPath
if ($renderedDirectory) { New-Item -ItemType Directory -Path $renderedDirectory -Force | Out-Null }
$renderedManifest = & helm template $ReleaseName $chartPath --namespace $Namespace --values $valuesPath
if ($LASTEXITCODE -ne 0) { throw 'Helm template failed; no release was installed.' }
Set-Content -LiteralPath $RenderedManifestPath -Value $renderedManifest -Encoding utf8

& kubectl --context $Context apply -f (Join-Path $repoRoot 'deploy/common/namespaces.yaml')
if ($LASTEXITCODE -ne 0) { throw 'Unable to create required namespace.' }

$workloadConfigYaml = & kubectl --context $Context --namespace $Namespace create configmap benchmark-workload `
    "--from-file=traffic.lua=$workloadPath" --dry-run=client --output=yaml
if ($LASTEXITCODE -ne 0) { throw 'Unable to render workload ConfigMap.' }
$workloadConfigYaml | & kubectl --context $Context apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply workload ConfigMap.' }

$sourceConfigYaml = & kubectl --context $Context --namespace $Namespace create configmap deathstarbench-source `
    "--from-literal=commit=$actualCommit" `
    "--from-literal=workload-script=socialNetwork/wrk2/scripts/social-network/$WorkloadScript" `
    "--from-literal=tools-image=$DsbToolsImage" --dry-run=client --output=yaml
if ($LASTEXITCODE -ne 0) { throw 'Unable to render DeathStarBench source ConfigMap.' }
$sourceConfigYaml | & kubectl --context $Context apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply DeathStarBench source ConfigMap.' }

& helm upgrade --install $ReleaseName $chartPath --namespace $Namespace --create-namespace --values $valuesPath --wait --timeout 15m
if ($LASTEXITCODE -ne 0) { throw 'Helm installation failed. The rendered manifest artifact was retained for diagnosis.' }

Write-Host "Installed $ReleaseName in $Context at immutable DeathStarBench commit $actualCommit. Seed/reset separately and record its output before treating the fixture as equivalent."

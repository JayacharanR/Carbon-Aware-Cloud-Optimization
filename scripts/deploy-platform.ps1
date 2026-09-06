<#
.SYNOPSIS
Applies the controller-side platform manifests to the primary cluster and the
shared namespaces/RBAC to both regional clusters, only when -Apply is present.
#>
[CmdletBinding()]
param(
    [string]$PrimaryContext,
    [string]$SecondaryContext,
    [string]$ExperimentConfigPath,
    [string]$ControllerImage,
    [string]$OllamaBaseUrl,
    [string]$ControllerCommand,
    [ValidateSet('llm_only', 'milp_only', 'hybrid', 'static_reference')]
    [string]$ExperimentMode,
    [string]$ReplayTracePath,
    [string]$GraphPath,
    [string]$TargetConfigPath,
    [string]$LatencyCatalogPath,
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

$PrimaryContext = if ($PrimaryContext) { $PrimaryContext } else { $env:TARGET_PRIMARY_KUBE_CONTEXT }
$SecondaryContext = if ($SecondaryContext) { $SecondaryContext } else { $env:TARGET_SECONDARY_KUBE_CONTEXT }
$ExperimentConfigPath = if ($ExperimentConfigPath) { $ExperimentConfigPath } else { Join-Path $repoRoot 'config/experiment.yaml' }
$ControllerImage = if ($ControllerImage) { $ControllerImage } else { $env:CONTROLLER_IMAGE }
$OllamaBaseUrl = if ($OllamaBaseUrl) { $OllamaBaseUrl } else { $env:OLLAMA_BASE_URL }
$ControllerCommand = if ($ControllerCommand) { $ControllerCommand } else { $env:CONTROLLER_COMMAND }
$ControllerCommand = if ($ControllerCommand) { $ControllerCommand } else { 'python -m carbon_scheduler.controller' }
foreach ($required in @{
    PrimaryContext = $PrimaryContext
    SecondaryContext = $SecondaryContext
    ExperimentConfigPath = $ExperimentConfigPath
    ControllerImage = $ControllerImage
    OllamaBaseUrl = $OllamaBaseUrl
}) {
    if ([string]::IsNullOrWhiteSpace($required.Value)) { throw "$($required.Key) is required; set it in .env or pass the parameter." }
}
if (-not (Test-Path -LiteralPath $ExperimentConfigPath -PathType Leaf)) {
    throw "ExperimentConfigPath was not found: $ExperimentConfigPath. Render it with bootstrap.py first."
}

if (-not $Apply) {
    throw 'This command changes Kubernetes clusters. Re-run with -Apply after reviewing the active contexts and completed configuration.'
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { throw 'kubectl is required.' }
if ([string]::IsNullOrWhiteSpace($ControllerImage) -or $ControllerImage -eq 'carbon-scheduler-controller') {
    throw 'ControllerImage must be a registry image that both AKS clusters can pull.'
}
if ([string]::IsNullOrWhiteSpace($OllamaBaseUrl)) {
    throw 'OllamaBaseUrl must be the already secured endpoint reachable from the controller cluster.'
}

$configContent = Get-Content -Raw $ExperimentConfigPath
if ($configContent -match 'REPLACE_ME' -or $configContent -match '(?m):\s*null\s*$') {
    throw 'ExperimentConfigPath appears incomplete. Use a completed, validated experiment.yaml rather than the draft.'
}

$requiredEnvironment = @('ELECTRICITY_MAPS_API_KEY', 'NEO4J_USERNAME', 'NEO4J_PASSWORD', 'QDRANT_API_KEY')
foreach ($name in $requiredEnvironment) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "$name must be set in the calling environment. Its value is not read from repository files."
    }
}

function Invoke-Kubectl {
    param(
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    & kubectl --context $Context @Arguments
    if ($LASTEXITCODE -ne 0) { throw "kubectl command failed for context '$Context': $($Arguments -join ' ')" }
}

function Apply-File {
    param([Parameter(Mandatory)][string]$Context, [Parameter(Mandatory)][string]$Path)
    Invoke-Kubectl -Context $Context -Arguments @('apply', '-f', $Path)
}

$namespaces = Join-Path $repoRoot 'deploy/common/namespaces.yaml'
$remoteRbac = Join-Path $repoRoot 'deploy/deathstarbench/remote-executor-rbac.yaml'
foreach ($context in @($PrimaryContext, $SecondaryContext)) {
    Apply-File -Context $context -Path $namespaces
    # This service account is for a separately issued, least-privilege remote
    # credential. It does not create or expose a token by itself.
    Apply-File -Context $context -Path $remoteRbac
}

# Create/update in-cluster config and secrets without writing their values to a
# file. The resulting Secret is stored by Kubernetes; ensure the AKS cluster's
# standard secret-management controls are appropriate for the study.
$configMapYaml = & kubectl --context $PrimaryContext --namespace scheduler-system create configmap scheduler-experiment-config `
    "--from-file=experiment.yaml=$ExperimentConfigPath" --dry-run=client --output=yaml
if ($LASTEXITCODE -ne 0) { throw 'Unable to render scheduler-experiment-config ConfigMap.' }
$configMapYaml | & kubectl --context $PrimaryContext apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply scheduler-experiment-config ConfigMap.' }

$neo4jAuth = "$($env:NEO4J_USERNAME)/$($env:NEO4J_PASSWORD)"
$secretYaml = & kubectl --context $PrimaryContext --namespace scheduler-system create secret generic scheduler-secrets `
    "--from-literal=electricity-maps-api-key=$($env:ELECTRICITY_MAPS_API_KEY)" `
    "--from-literal=neo4j-username=$($env:NEO4J_USERNAME)" `
    "--from-literal=neo4j-password=$($env:NEO4J_PASSWORD)" `
    "--from-literal=neo4j-auth=$neo4jAuth" `
    "--from-literal=qdrant-api-key=$($env:QDRANT_API_KEY)" `
    --dry-run=client --output=yaml
if ($LASTEXITCODE -ne 0) { throw 'Unable to render scheduler-secrets.' }
$secretYaml | & kubectl --context $PrimaryContext apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply scheduler-secrets.' }

foreach ($path in @(
    (Join-Path $repoRoot 'deploy/neo4j/neo4j.yaml'),
    (Join-Path $repoRoot 'deploy/qdrant/qdrant.yaml'),
    (Join-Path $repoRoot 'deploy/observability/jaeger.yaml'),
    (Join-Path $repoRoot 'deploy/observability/otel-collector.yaml')
)) {
    Apply-File -Context $PrimaryContext -Path $path
}

$controllerDirectory = Join-Path $repoRoot 'deploy/controller'
# Apply the configuration-bearing pieces first. Patching ConfigMap values after
# a Deployment starts would not update environment variables in an existing
# Pod, so the Deployment is intentionally applied last.
foreach ($path in @(
    (Join-Path $controllerDirectory 'runtime-config.yaml'),
    (Join-Path $controllerDirectory 'inputs-configmap.yaml'),
    (Join-Path $controllerDirectory 'rbac.yaml'),
    (Join-Path $controllerDirectory 'persistence.yaml')
)) {
    Apply-File -Context $PrimaryContext -Path $path
}
$runtimePatch = @{ data = @{ OLLAMA_BASE_URL = $OllamaBaseUrl; CONTROLLER_COMMAND = $ControllerCommand } } | ConvertTo-Json -Compress

# A concrete replay run is opt-in.  When supplied, place its immutable input
# files in one ConfigMap and point the one-shot controller at the mounted
# paths.  With no run inputs the deployment stays dormant because the runtime
# ConfigMap still has an empty EXPERIMENT_MODE; this avoids accidental cloud
# spend while the platform is being assembled.
$hasAnyRunInput = $ExperimentMode -or $ReplayTracePath -or $GraphPath -or $TargetConfigPath -or $LatencyCatalogPath
if ($hasAnyRunInput) {
    if ([string]::IsNullOrWhiteSpace($ExperimentMode) -or
        [string]::IsNullOrWhiteSpace($ReplayTracePath) -or
        [string]::IsNullOrWhiteSpace($GraphPath) -or
        [string]::IsNullOrWhiteSpace($TargetConfigPath) -or
        [string]::IsNullOrWhiteSpace($LatencyCatalogPath)) {
        throw 'ExperimentMode, ReplayTracePath, GraphPath, TargetConfigPath, and LatencyCatalogPath must be supplied together for a run.'
    }
    foreach ($runInput in @($ReplayTracePath, $GraphPath, $TargetConfigPath, $LatencyCatalogPath)) {
        if (-not (Test-Path -LiteralPath $runInput -PathType Leaf)) {
            throw "Configured run input was not found: $runInput"
        }
    }
    $inputConfigYaml = & kubectl --context $PrimaryContext --namespace scheduler-system create configmap scheduler-run-inputs `
        "--from-file=carbon-trace.json=$ReplayTracePath" `
        "--from-file=graph.json=$GraphPath" `
        "--from-file=targets.yaml=$TargetConfigPath" `
        "--from-file=latency-catalog.yaml=$LatencyCatalogPath" `
        --dry-run=client --output=yaml
    if ($LASTEXITCODE -ne 0) { throw 'Unable to render scheduler-run-inputs ConfigMap.' }
    $inputConfigYaml | & kubectl --context $PrimaryContext apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Unable to apply scheduler-run-inputs ConfigMap.' }
    $runtimeData = @{
        OLLAMA_BASE_URL = $OllamaBaseUrl
        CONTROLLER_COMMAND = $ControllerCommand
        EXPERIMENT_MODE = $ExperimentMode
        REPLAY_TRACE_PATH = '/etc/carbon-scheduler/inputs/carbon-trace.json'
        GRAPH_PATH = '/etc/carbon-scheduler/inputs/graph.json'
        TARGET_CONFIG_PATH = '/etc/carbon-scheduler/inputs/targets.yaml'
        LATENCY_CATALOG_PATH = '/etc/carbon-scheduler/inputs/latency-catalog.yaml'
    }
    $runtimePatch = @{ data = $runtimeData } | ConvertTo-Json -Compress
}
Invoke-Kubectl -Context $PrimaryContext -Arguments @('-n', 'scheduler-system', 'patch', 'configmap', 'scheduler-runtime-config', '--type', 'merge', '--patch', $runtimePatch)

$controllerDeployment = Get-Content -Raw (Join-Path $controllerDirectory 'deployment.yaml')
$controllerDeployment = $controllerDeployment.Replace('image: carbon-scheduler-controller', "image: $ControllerImage")
$controllerDeployment | & kubectl --context $PrimaryContext apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply controller Deployment.' }

Write-Host 'Platform manifests applied. Deploy each independent DeathStarBench release with install-deathstarbench.ps1, then wait for component readiness before beginning a pilot.'

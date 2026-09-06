<#
.SYNOPSIS
Checks or installs the local prerequisites used by the scheduler.

.DESCRIPTION
The default action is read-only.  Use -InstallPythonPackages to run
`uv sync --all-extras`.  Use -UsePipFallback only for a legacy machine that
cannot install uv.  Use
-InstallWindowsTools only when Windows Package Manager/winget is available and
you have reviewed the package installation prompts.  Azure resources are never
created by this script.
#>
[CmdletBinding()]
param(
    [switch]$InstallPythonPackages,
    [switch]$InstallWindowsTools,
    [switch]$SkipPipUpgrade,
    [switch]$UsePipFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Test-Tool {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$windowsInstallFailures = [System.Collections.Generic.List[string]]::new()

if ($InstallWindowsTools) {
    if (-not (Test-Tool 'winget')) {
        throw 'winget is required for -InstallWindowsTools. Install App Installer from Microsoft Store first.'
    }
    $packages = @(
        @{ Name = 'Git'; Command = 'git'; Id = 'Git.Git' },
        @{ Name = 'Azure CLI'; Command = 'az'; Id = 'Microsoft.AzureCLI' },
        @{ Name = 'kubectl'; Command = 'kubectl'; Id = 'Kubernetes.kubectl' },
        @{ Name = 'Helm'; Command = 'helm'; Id = 'Helm.Helm' },
        @{ Name = 'Docker Desktop'; Command = 'docker'; Id = 'Docker.DockerDesktop' },
        @{ Name = 'Ollama'; Command = 'ollama'; Id = 'Ollama.Ollama' },
        @{ Name = 'Tailscale'; Command = 'tailscale'; Id = 'Tailscale.Tailscale' }
    )
    foreach ($package in $packages) {
        if (Test-Tool $package.Command) {
            Write-Host "$($package.Name) already appears to be installed."
            continue
        }
        Write-Host "Installing $($package.Name) ($($package.Id))..."
        $wingetArguments = @(
            'install', '--id', $package.Id, '--exact', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements'
        )
        if ($package.Id -eq 'Docker.DockerDesktop') {
            # The current community WinGet manifest is machine-scoped and
            # elevates itself. Keep the installer interactive so its UAC and
            # Docker-specific prerequisite messages remain visible.
            $wingetArguments += @('--interactive')
        }
        try {
            & winget @wingetArguments
            if ($LASTEXITCODE -ne 0) {
                throw "exit code $LASTEXITCODE"
            }
        }
        catch {
            $message = "$($package.Name): $($_.Exception.Message)"
            $windowsInstallFailures.Add($message)
            Write-Warning "winget could not install $message"
            Write-Warning "You can rerun this package manually after resolving its installer prerequisites."
            if ($package.Id -eq 'Docker.DockerDesktop') {
                Write-Warning 'Docker Desktop normally uses WSL 2 on Windows; run `wsl --status` and complete any required WSL update/reboot before retrying.'
            }
        }
    }
    if ($windowsInstallFailures.Count -gt 0) {
        Write-Warning ('Windows package installation had failures: ' + ($windowsInstallFailures -join '; '))
    }
}

if (-not (Test-Tool 'python')) {
    throw 'Python 3.11-3.13 is required. Install it, then rerun this script.'
}

if ($InstallPythonPackages) {
    Push-Location $repoRoot
    try {
        if (Test-Tool 'uv') {
            & uv sync --all-extras
            if ($LASTEXITCODE -ne 0) { throw 'uv dependency synchronization failed.' }
        }
        elseif ($UsePipFallback) {
            Write-Warning 'uv is not installed; using the explicitly requested pip fallback.'
            if (-not $SkipPipUpgrade) {
                & python -m pip install --upgrade pip
                if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
            }
            & python -m pip install -e '.[kubernetes,storage,agent,observability,analysis,dev]'
            if ($LASTEXITCODE -ne 0) { throw 'project dependency installation failed.' }
        }
        else {
            throw 'uv is required for dependency installation. Install uv and rerun, or explicitly pass -UsePipFallback.'
        }
    }
    finally {
        Pop-Location
    }
}

$checks = @(
    @{ Name = 'python'; Required = $true },
    @{ Name = 'uv'; Required = $true },
    @{ Name = 'git'; Required = $true },
    @{ Name = 'az'; Required = $false },
    @{ Name = 'kubectl'; Required = $false },
    @{ Name = 'helm'; Required = $false },
    @{ Name = 'docker'; Required = $false },
    @{ Name = 'wsl'; Required = $false },
    @{ Name = 'ollama'; Required = $false },
    @{ Name = 'tailscale'; Required = $false }
)

$missing = [System.Collections.Generic.List[string]]::new()
foreach ($check in $checks) {
    if (Test-Tool $check.Name) {
        $command = Get-Command $check.Name
        Write-Host "OK  $($check.Name): $($command.Source)"
    }
    elseif ($check.Required) {
        $missing.Add($check.Name)
        Write-Warning "MISSING (required for all local work): $($check.Name)"
    }
    else {
        Write-Warning "MISSING (required for Azure/cloud execution): $($check.Name)"
    }
}

if ($missing.Count -gt 0) {
    throw ('Required local tools are missing: ' + ($missing -join ', '))
}

if ($windowsInstallFailures.Count -gt 0) {
    throw ('Windows package installation did not complete: ' + ($windowsInstallFailures -join '; '))
}

Write-Host 'Prerequisite check completed. Cloud tools are only required for the real AKS path.'

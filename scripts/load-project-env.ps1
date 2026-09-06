<#
.SYNOPSIS
Loads the ignored repository .env file into the current PowerShell process.

.DESCRIPTION
This is intentionally a small parser for the dotenv subset used by this
project. Existing process variables win unless -Override is supplied. Values
are never printed.
#>
[CmdletBinding()]
param(
    [string]$Path = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'),
    [switch]$Override
)

function Import-ProjectDotEnv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Override
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        if ($trimmed.StartsWith('export ')) {
            $trimmed = $trimmed.Substring(7).TrimStart()
        }
        if ($trimmed -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            throw "Invalid .env line in ${Path}: $line"
        }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $existing = [Environment]::GetEnvironmentVariable($name, 'Process')
        if (-not $Override -and -not [string]::IsNullOrWhiteSpace($existing)) {
            continue
        }
        Set-Item -Path "Env:$name" -Value $value
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Import-ProjectDotEnv -Path $Path -Override:$Override
}

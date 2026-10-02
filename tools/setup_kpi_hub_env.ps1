# Creates/updates the .venv used for KPI-hub instrumentation (HW + workflow KPI collection)
# Usage: .\tools\setup_kpi_hub_env.ps1
param(
    [string]$Proxy = "http://proxy-dmz.intel.com:911/"
)
$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$venvPath = Join-Path $repoRoot ".venv"

# Set proxy variables for pip without changing machine-wide WinHTTP settings.
. (Join-Path $PSScriptRoot "set_proxy_env.ps1") -Proxy $Proxy -ProcessOnly

if (-not (Test-Path $venvPath)) {
    python -m venv $venvPath
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $repoRoot "tools\KPI-hub\requirements.txt")

Write-Host "KPI-hub venv ready at $venvPath"

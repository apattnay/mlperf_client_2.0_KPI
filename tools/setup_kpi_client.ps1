param(
    [string]$Proxy = "http://proxy-dmz.intel.com:911/",
    [string]$InstallDir = "C:\Applications\mlperf_client\mlperf_v2p0"
)
$ErrorActionPreference = "Stop"

$setupKpiHub = Join-Path $PSScriptRoot "setup_kpi_hub_env.ps1"
$setupMlperf = Join-Path $PSScriptRoot "setup_mlperf_v2.ps1"

& $setupKpiHub -Proxy $Proxy
if ($LASTEXITCODE -ne 0) {
    throw "KPI-hub environment setup failed with exit code $LASTEXITCODE."
}

& $setupMlperf -InstallDir $InstallDir -Proxy $Proxy
if ($LASTEXITCODE -ne 0) {
    throw "MLPerf setup failed with exit code $LASTEXITCODE."
}

Write-Host "KPI client setup complete."
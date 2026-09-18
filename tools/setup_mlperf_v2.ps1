<#
.SYNOPSIS
    Downloads and extracts the official MLPerf Client 2.0.0 Windows x64 CLI release.

.DESCRIPTION
    All KPI presets (see tools/run_kpi_preset.py) run against this install - it's required
    because only mlperf 2.0 supports the "IsAgentic" scenario field the SWE Agent presets (5/6)
    need. This is a ~190MB binary release from the official MLCommons GitHub repo, not part of
    this git repo. Idempotent: skips the download if mlperf-windows.exe already exists at
    -InstallDir.

    On an Intel corporate network, dot-source tools/set_proxy_env.ps1 first (or pass -Proxy) so
    the download can actually reach github.com/objects/releases through the corporate proxy.

.PARAMETER InstallDir
    Where to extract the release. Must match run_kpi_preset.py's DEFAULT_MLPERF_DIR.

.PARAMETER Proxy
    Optional proxy URL (e.g. http://proxy-dmz.intel.com:911/) for the download itself.

.EXAMPLE
    .\tools\setup_mlperf_v2.ps1
    .\tools\setup_mlperf_v2.ps1 -Proxy "http://proxy-dmz.intel.com:911/"
#>
param(
    [string]$InstallDir = "C:\Applications\mlperf_client\mlperf_v2p0",
    [string]$Proxy = $env:HTTPS_PROXY
)
$ErrorActionPreference = "Stop"

$exePath = Join-Path $InstallDir "mlperf-windows.exe"
if (Test-Path $exePath) {
    Write-Host "mlperf-windows.exe already present at $InstallDir - skipping download."
    & $exePath -v
    exit 0
}

$releaseUrl = "https://github.com/mlcommons/mlperf_client/releases/download/v2.0/mlperf-client-2.0.0-c8d2dc0-windows-x64.zip"
$zipPath = "$InstallDir.zip"

New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) | Out-Null

Write-Host "Downloading mlperf-client 2.0.0 (~190MB) from $releaseUrl ..."
$iwrArgs = @{ Uri = $releaseUrl; OutFile = $zipPath; UseBasicParsing = $true }
if ($Proxy) { $iwrArgs["Proxy"] = $Proxy }
Invoke-WebRequest @iwrArgs

Write-Host "Extracting to $InstallDir ..."
Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
Remove-Item $zipPath

Write-Host "Done. Verifying:"
& $exePath -v

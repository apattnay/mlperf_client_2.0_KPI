<#
.SYNOPSIS
    Sets the Intel corporate proxy for the current PowerShell session and system-wide WinHTTP.

.DESCRIPTION
    mlperf-windows.exe needs a proxy to reach external hosts such as client.mlcommons-storage.org
    from an Intel corporate network. It does not honor a single mechanism reliably, so this script
    sets both:
      1. HTTP(S)_PROXY env vars (process-scoped, this session only).
      2. The machine-wide WinHTTP proxy via `netsh winhttp set proxy` (persists across reboots;
         requires an elevated/admin shell - if this part fails, ask an admin to run it once).

    Without this, downloads for presets 5/6 (agentic SWE Agent scenario - model, prompts,
    tools_sandbox.zip, IHV runtime DLLs) fail with "could not connect to the download server"
    even though the host is reachable through the proxy.

.EXAMPLE
    . .\tools\set_proxy_env.ps1
    .venv\Scripts\python.exe tools\run_kpi_preset.py --preset 5
#>
$proxyHost = "proxy-dmz.intel.com:911"
$proxy = "http://$proxyHost/"

$env:HTTP_PROXY  = $proxy
$env:http_proxy  = $proxy
$env:HTTPS_PROXY = $proxy
$env:https_proxy = $proxy
$env:FTP_PROXY   = $proxy
$env:ftp_proxy   = $proxy
$env:NO_PROXY    = "localhost,intel.com,192.168.0.0/16,172.16.0.0/12,127.0.0.0/8,10.0.0.0/8"
$env:no_proxy    = $env:NO_PROXY

Write-Host "Proxy environment variables set for this session (HTTP(S)_PROXY=$proxy)."

try {
    netsh winhttp set proxy $proxyHost "localhost;*.intel.com;192.168.*;172.16.*;127.*;10.*" | Out-Null
    Write-Host "WinHTTP proxy set machine-wide to $proxyHost (netsh winhttp show proxy to verify)."
} catch {
    Write-Warning "Could not set WinHTTP proxy (may need an elevated shell): $($_.Exception.Message)"
}


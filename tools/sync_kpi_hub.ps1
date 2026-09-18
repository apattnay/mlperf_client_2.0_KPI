# Refreshes the vendored tools/KPI-hub snapshot from its source repo.
# Requires access to the KPI-hub repo. Review `git diff tools/KPI-hub` and commit after running.
# Usage: .\tools\sync_kpi_hub.ps1 [-Ref main]
param(
    [string]$Ref = "main",
    [string]$RepoUrl = "https://github.com/intel-sandbox/KPI-hub"
)
$ErrorActionPreference = "Stop"

$dest = Join-Path $PSScriptRoot "KPI-hub"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kpi-hub-sync-" + [guid]::NewGuid())

git clone --depth 1 --branch $Ref $RepoUrl $tmp
$commit = git -C $tmp rev-parse --short HEAD
$subject = git -C $tmp log -1 --format=%s
Remove-Item (Join-Path $tmp ".git") -Recurse -Force

Get-ChildItem $dest -Force | Remove-Item -Recurse -Force
Get-ChildItem $tmp -Force | Copy-Item -Destination $dest -Recurse -Force
Remove-Item $tmp -Recurse -Force

$snapshotInfo = @{
    source_repo           = $RepoUrl
    vendored_ref          = $Ref
    vendored_commit       = $commit
    vendored_commit_subject = $subject
    vendored_at           = (Get-Date -Format "yyyy-MM-dd")
    note                  = "This directory is a vendored snapshot, not a git submodule. Contributors without access to the source repo do not need it to build or run this project. To pull a newer snapshot (requires access to the source repo), run tools/sync_kpi_hub.ps1 and commit the resulting changes."
} | ConvertTo-Json
Set-Content -Path (Join-Path $dest ".vendor-snapshot.json") -Value $snapshotInfo

Write-Host "KPI-hub snapshot updated to $commit ($subject)."
Write-Host "Review with 'git diff tools/KPI-hub' and commit the update."

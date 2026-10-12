# Updates the tools/KPI-hub submodule to the requested upstream branch.
# Review and commit the resulting gitlink change in the parent repository.
# Usage: .\tools\sync_kpi_hub.ps1 [-Ref main]
param(
    [string]$Ref = "main"
)
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$submodulePath = Join-Path $repoRoot "tools\KPI-hub"
$relativeSubmodulePath = "tools/KPI-hub"

if (-not (Test-Path -LiteralPath (Join-Path $submodulePath ".git"))) {
    git -C $repoRoot submodule update --init --recursive -- $relativeSubmodulePath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to initialize the KPI-Hub submodule."
    }
}

$dirty = git -C $submodulePath status --porcelain
if ($dirty) {
    throw "KPI-Hub has local changes. Commit or preserve them before updating the submodule."
}

git -C $submodulePath fetch origin $Ref
if ($LASTEXITCODE -ne 0) {
    throw "Failed to fetch KPI-Hub ref '$Ref'."
}
git -C $submodulePath checkout --detach FETCH_HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Failed to check out KPI-Hub ref '$Ref'."
}

$commit = git -C $submodulePath rev-parse --short HEAD
$subject = git -C $submodulePath log -1 --format=%s
Write-Host "KPI-Hub submodule checked out at $commit ($subject)."
Write-Host "Review and commit the parent gitlink change with 'git diff --submodule=log -- tools/KPI-hub'."

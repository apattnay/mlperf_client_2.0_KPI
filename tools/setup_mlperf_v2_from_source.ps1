<#
.SYNOPSIS
    Builds the MLPerf Client from this source repository and stages a Windows CLI install.

.DESCRIPTION
    Uses the CMake/Visual Studio build described in README_BUILD.md. The Python venv is
    not used for the native build; it remains responsible for the KPI tooling. The native
    executable and runtime files are staged into a separate install directory so the
    official prebuilt MLPerf installation is not overwritten.

    The staged directory can be passed to run_kpi_preset.py with --mlperf-dir.

.PARAMETER SourceDir
    MLPerf Client source directory. Defaults to the repository root containing this script.

.PARAMETER BuildDir
    CMake build directory. Defaults to <SourceDir>\build-custom.

.PARAMETER InstallDir
    Runtime installation directory. Defaults to
    C:\Applications\mlperf_client\mlperf_v2p0_custom.

.PARAMETER Configuration
    Visual Studio configuration. Defaults to Release.

.PARAMETER Architecture
    CMake Visual Studio architecture. Defaults to x64.

.PARAMETER Generator
    CMake generator. Defaults to Visual Studio 17 2022.

.PARAMETER CleanBuild
    Remove the selected CMake build directory before configuring.

.PARAMETER SkipData
    Do not copy the source data directory into the staged runtime install.

.PARAMETER SkipDiffusers
    Disable optional NVIDIA and AMD Diffusers integrations during the native build.

.PARAMETER InstallPrerequisites
    Install missing CMake and Visual Studio 2022 C++ Build Tools prerequisites with winget.
    This installs the Build Tools workload, not the full Visual Studio IDE.

.EXAMPLE
    .\tools\setup_mlperf_v2_from_source.ps1

.EXAMPLE
    .\tools\setup_mlperf_v2_from_source.ps1 -CleanBuild -Configuration Release

.EXAMPLE
    .\.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 5 `
        --mlperf-dir C:\Applications\mlperf_client\mlperf_v2p0_custom

.EXAMPLE
    .\tools\setup_mlperf_v2_from_source.ps1 -InstallPrerequisites
#>
[CmdletBinding()]
param(
    [string]$SourceDir = (Split-Path -Parent $PSScriptRoot),
    [string]$BuildDir = "",
    [string]$InstallDir = "C:\Applications\mlperf_client\mlperf_v2p0_custom",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [ValidateSet("x64", "ARM64")]
    [string]$Architecture = "x64",
    [string]$Generator = "Visual Studio 17 2022",
    [switch]$CleanBuild,
    [switch]$SkipData,
    [switch]$SkipDiffusers,
    [switch]$InstallPrerequisites
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host ("> {0} {1}" -f $Command, ($Arguments -join " "))
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Command"
    }
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Repair-WingetSource {
    & winget.exe source update --name winget
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Warning "WinGet source update failed; resetting the winget source metadata."
    & winget.exe source reset --force
    if ($LASTEXITCODE -eq 0) {
        & winget.exe source update --name winget
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    Write-Warning "WinGet source reset did not restore the source; re-registering the official winget endpoint."
    & winget.exe source remove --name winget
    & winget.exe source add --name winget --arg "https://cdn.winget.microsoft.com/cache"
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet could not register the official package source. Repair or update the App Installer package, then retry."
    }
    & winget.exe source update --name winget
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet could not refresh the official package source. Check App Installer and network/proxy settings, then retry."
    }
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,
        [string]$Override = ""
    )

    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "winget.exe is required to install prerequisites automatically. Install App Installer or rerun without -InstallPrerequisites for manual setup."
    }

    # WinGet can retain a broken or empty repository cache after an App Installer
    # update. Repair the source before attempting the package install.
    Repair-WingetSource

    $arguments = @(
        "install", "--id", $Id, "--exact", "--source", "winget",
        "--accept-source-agreements", "--accept-package-agreements", "--silent"
    )
    if ($Override) {
        $arguments += @("--override", $Override)
    }
    Write-Host "Installing prerequisite package: $Id"
    & winget.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install $Id with exit code $LASTEXITCODE. Review the WinGet output above."
    }
    Refresh-ProcessPath
}

function Install-ChocoPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,
        [string]$PackageParameters = ""
    )

    if (-not (Get-Command choco.exe -ErrorAction SilentlyContinue)) {
        throw "choco.exe is not installed."
    }
    $arguments = @("install", $Id, "--yes", "--no-progress")
    if ($PackageParameters) {
        $arguments += @("--package-parameters", $PackageParameters)
    }
    Write-Host "Installing prerequisite package with Chocolatey: $Id"
    & choco.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Chocolatey failed to install $Id with exit code $LASTEXITCODE."
    }
    Refresh-ProcessPath
}

function Install-VsBuildToolsFallback {
    $installer = Join-Path $env:TEMP "vs_buildtools.exe"
    Write-Host "WinGet/App Installer is unavailable; downloading the official Visual Studio Build Tools bootstrapper."
    Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vs_buildtools.exe" -OutFile $installer
    $arguments = "--wait --passive --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) {
        throw "Visual Studio Build Tools bootstrapper failed with exit code $($process.ExitCode). Run this script from an elevated PowerShell and retry."
    }
    Refresh-ProcessPath
}

function Get-VsWherePath {
    $command = Get-Command vswhere.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $defaultPath = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $defaultPath -PathType Leaf) {
        return $defaultPath
    }
    return $null
}

function Get-VsCppBuildToolsPath {
    $vswhere = Get-VsWherePath
    if (-not $vswhere) {
        return $null
    }
    $path = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $path) {
        return $null
    }
    return ($path | Select-Object -First 1).Trim()
}

function Ensure-Prerequisites {
    $cmake = Get-Command cmake.exe -ErrorAction SilentlyContinue
    $clangTidy = Get-Command clang-tidy.exe -ErrorAction SilentlyContinue
    $vsPath = Get-VsCppBuildToolsPath
    $missing = @()
    if (-not $cmake) { $missing += "CMake" }
    if (-not $clangTidy) { $missing += "clang-tidy (LLVM)" }
    if (-not $vsPath) { $missing += "Visual Studio 2022 C++ Build Tools + Windows SDK" }

    if ($missing.Count -eq 0) {
        Write-Host "Prerequisites found: CMake $(& cmake.exe --version | Select-Object -First 1); clang-tidy at $($clangTidy.Source); VS Build Tools at $vsPath"
        return
    }

    if (-not $InstallPrerequisites) {
        throw ("Missing prerequisites: {0}. Install LLVM/clang-tidy, the VS 2022 C++ Build Tools workload, and CMake, " +
               "or rerun this script with -InstallPrerequisites. The full Visual Studio IDE is not required.") -f ($missing -join ", ")
    }

    if (-not $cmake) {
        try {
            Install-WingetPackage -Id "Kitware.CMake"
        } catch {
            Write-Warning $_.Exception.Message
            try {
                Install-ChocoPackage -Id "cmake.install"
            } catch {
                Write-Warning $_.Exception.Message
                throw "CMake installation failed through both WinGet and Chocolatey. Run 'choco install cmake.install -y' from an elevated PowerShell, open a new PowerShell, and rerun this script."
            }
        }
    }
    if (-not $clangTidy) {
        try {
            Install-WingetPackage -Id "LLVM.LLVM"
        } catch {
            Write-Warning $_.Exception.Message
            Install-ChocoPackage -Id "llvm"
        }
    }
    if (-not $vsPath) {
        try {
            Install-WingetPackage -Id "Microsoft.VisualStudio.2022.BuildTools" -Override "--wait --passive --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
        } catch {
            Write-Warning $_.Exception.Message
            try {
                Install-ChocoPackage -Id "visualstudio2022buildtools" -PackageParameters "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --norestart"
            } catch {
                Write-Warning $_.Exception.Message
                Install-VsBuildToolsFallback
            }
        }
    }

    $cmake = Get-Command cmake.exe -ErrorAction SilentlyContinue
    $clangTidy = Get-Command clang-tidy.exe -ErrorAction SilentlyContinue
    $vsPath = Get-VsCppBuildToolsPath
    if (-not $cmake -or -not $clangTidy -or -not $vsPath) {
        $stillMissing = @()
        if (-not $cmake) { $stillMissing += "CMake (cmake.exe)" }
        if (-not $clangTidy) { $stillMissing += "clang-tidy (LLVM)" }
        if (-not $vsPath) { $stillMissing += "Visual Studio C++ Build Tools workload (vswhere detection)" }
        throw ("Prerequisite installation finished, but these components are still unavailable: {0}. " +
               "Open a new PowerShell or Developer PowerShell after installation and rerun the script. " +
               "The full Visual Studio IDE is not required.") -f ($stillMissing -join ", ")
    }
    Write-Host "Prerequisite installation verified."
}

function Ensure-SourceSubmodules {
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw "git.exe is required to initialize the source dependencies. Install Git and rerun this script."
    }

    Write-Host "Initializing pinned Git submodules"
    $requiredSubmodulePaths = @(
        "deps/cpp-httplib",
        "deps/JSON",
        "deps/JSONSchema",
        "deps/minizip-ng"
    )
    & git.exe -C $SourceDir submodule update --init --recursive -- $requiredSubmodulePaths
    if ($LASTEXITCODE -ne 0) {
        throw "Git submodule initialization failed with exit code $LASTEXITCODE. Check repository access and retry."
    }

    $requiredSubmodules = @(
        "deps\JSON\CMakeLists.txt",
        "deps\JSONSchema\CMakeLists.txt",
        "deps\cpp-httplib\CMakeLists.txt",
        "deps\minizip-ng\CMakeLists.txt"
    )
    $missing = @($requiredSubmodules | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $SourceDir $_) -PathType Leaf)
    })
    if ($missing.Count -gt 0) {
        throw ("Source dependencies are still incomplete: {0}. Run 'git submodule status' " +
               "and verify network access, then rerun this script.") -f ($missing -join ", ")
    }
}

$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
if (-not $BuildDir) {
    $BuildDir = Join-Path $SourceDir "build-custom"
}
$BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$cmakeLists = Join-Path $SourceDir "CMakeLists.txt"

if (-not (Test-Path -LiteralPath $cmakeLists -PathType Leaf)) {
    throw "CMakeLists.txt was not found under SourceDir: $SourceDir"
}

Ensure-SourceSubmodules
Ensure-Prerequisites

$gitCommit = "unknown"
if (Get-Command git.exe -ErrorAction SilentlyContinue) {
    try {
        $gitCommit = (& git -C $SourceDir rev-parse HEAD 2>$null).Trim()
    } catch {
        $gitCommit = "unknown"
    }
}

if ($CleanBuild -and (Test-Path -LiteralPath $BuildDir)) {
    Write-Host "Removing CMake build directory: $BuildDir"
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

Write-Host "Configuring MLPerf Client source"
$cmakeConfigureArgs = @(
    "-G", $Generator,
    "-A", $Architecture,
    "-S", $SourceDir,
    "-B", $BuildDir
)
if ($SkipDiffusers) {
    $cmakeConfigureArgs += @(
        "-DMLPERF_IHV_DIFFUSERS_NVIDIA=OFF",
        "-DMLPERF_IHV_DIFFUSERS_AMD=OFF",
        "-DMLPERF_IHV_DIFFUSERS_APPLE=OFF"
    )
}
Invoke-Checked cmake.exe $cmakeConfigureArgs

Write-Host "Building MLPerf Client ($Configuration, $Architecture)"
Invoke-Checked cmake.exe @(
    "--build", $BuildDir,
    "--config", $Configuration,
    "--parallel"
)

$expectedBinDir = Join-Path $SourceDir ("Bin\Windows\{0}" -f $Configuration)
$exeCandidates = @(
    (Join-Path $expectedBinDir "mlperf-windows.exe"),
    (Join-Path $BuildDir ("Bin\Windows\{0}\mlperf-windows.exe" -f $Configuration)),
    (Join-Path $BuildDir ("{0}\mlperf-windows.exe" -f $Configuration))
)
$exePath = $exeCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $exePath) {
    $exePath = Get-ChildItem -LiteralPath $SourceDir -Filter "mlperf-windows.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch "\\(build-custom|\.git)\\" } |
        Select-Object -ExpandProperty FullName -First 1
}
if (-not $exePath) {
    throw "Build completed but mlperf-windows.exe was not found. Inspect: $BuildDir"
}
$exePath = (Resolve-Path -LiteralPath $exePath).Path
$runtimeSourceDir = Split-Path -Parent $exePath

Write-Host "Staging runtime from: $runtimeSourceDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Get-ChildItem -LiteralPath $runtimeSourceDir -Force | Copy-Item -Destination $InstallDir -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "Logs") | Out-Null

if (-not $SkipData) {
    $sourceDataDir = Join-Path $SourceDir "data"
    if (Test-Path -LiteralPath $sourceDataDir -PathType Container) {
        Write-Host "Staging runtime data directory"
        Copy-Item -LiteralPath $sourceDataDir -Destination (Join-Path $InstallDir "data") -Recurse -Force
    }
}

$manifest = [ordered]@{
    build_type = "source"
    source_dir = $SourceDir
    source_commit = $gitCommit
    build_dir = $BuildDir
    configuration = $Configuration
    architecture = $Architecture
    generator = $Generator
    built_executable = $exePath
    installed_executable = Join-Path $InstallDir "mlperf-windows.exe"
    install_dir = $InstallDir
    built_at = (Get-Date).ToString("o")
}
$manifestPath = Join-Path $InstallDir "custom_build_manifest.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

$installedExe = Join-Path $InstallDir "mlperf-windows.exe"
if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
    throw "Staging completed without an installed executable: $installedExe"
}

Write-Host "Verifying custom executable"
& $installedExe -v
if ($LASTEXITCODE -ne 0) {
    throw "Custom executable verification failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Custom MLPerf Client installation complete:"
Write-Host "  Executable: $installedExe"
Write-Host "  Manifest:   $manifestPath"
Write-Host "  Source:     $SourceDir"
Write-Host "  Commit:     $gitCommit"
Write-Host ""
Write-Host "Run a preset explicitly with:"
Write-Host ("  .\.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 5 --mlperf-dir `"{0}`"" -f $InstallDir)

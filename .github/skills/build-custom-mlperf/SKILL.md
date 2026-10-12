---
name: build-custom-mlperf
description: "Build and stage the custom MLPerf Client binary from source. Use when a benchmark requests custom MLPerf and C:\\Applications\\mlperf_client\\mlperf_v2p0_custom\\mlperf-windows.exe is missing, or when the user asks to build the custom MLPerf binary."
---
- Run this workflow from the current MLPerf repository root; do not assume a fixed checkout path.
- Use a subagent to execute and monitor the build so the calling benchmark workflow can remain focused. The subagent must use the repository's `tools/setup_mlperf_v2_from_source.ps1` script rather than reimplementing the build steps.
- By default, run:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup_mlperf_v2_from_source.ps1`
- The script configures and builds the pinned source with the default `Release`/`x64` settings, then stages the runtime at `C:\Applications\mlperf_client\mlperf_v2p0_custom`. It may install missing native prerequisites when invoked with `-InstallPrerequisites`; ask the user before adding that switch because it can change the machine and require elevation.
- If the user explicitly requests a clean build, pass `-CleanBuild`. Preserve other user-selected script parameters when provided.
- On success, verify both `C:\Applications\mlperf_client\mlperf_v2p0_custom\mlperf-windows.exe` and `C:\Applications\mlperf_client\mlperf_v2p0_custom\custom_build_manifest.json` exist. Report the executable, manifest, source commit, and any prerequisite or elevation requirement.
- On failure, report the command output and stop. Do not run a benchmark with the official binary as an implicit fallback.
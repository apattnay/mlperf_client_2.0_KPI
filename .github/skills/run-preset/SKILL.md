---
name: run-preset
description: "Run an MLPerf benchmark preset, optionally with the custom MLPerf binary. Use when the user asks to run a benchmark preset or says 'with custom mlperf'."
---
- The list of presets and their numeric values is in `tools/run_kpi_preset.py`. If the user specifies an invalid preset, stop and display an error with the valid preset values.
- Detect whether the user requested the custom binary. Treat wording such as `with custom mlperf`, `custom MLPerf`, or `custom binary` as a custom-binary request.
- Use `C:\Applications\mlperf_client\mlperf_v2p0` unless custom MLPerf was requested. For custom MLPerf, use `C:\Applications\mlperf_client\mlperf_v2p0_custom` and verify that `mlperf-windows.exe` exists there.
- If the custom executable is missing, ask the user whether they want to build it. Do not run the benchmark until they answer. If they agree, invoke the `build-custom-mlperf` skill through a subagent, wait for it to complete, and verify `C:\Applications\mlperf_client\mlperf_v2p0_custom\mlperf-windows.exe` before continuing. If they decline, stop without running the benchmark.
- For a valid preset, set proxies by executing `tools/set_proxy_env.ps1`, then run from the repository root:
  `tools/run_kpi_preset.py --preset {preset_value} --mlperf-dir {selected_mlperf_dir}`
- When the benchmark completes successfully:
  - Identify the newly created `kpi_runs/{scenario_name}_{timestamp}` directory, using the scenario name for the selected preset and the newest matching timestamp.
  - Copy `{selected_mlperf_dir}\Logs\debug.log` into that directory as `mlperf_detailed_execution.log`.
  - Display a workspace link to the results directory.
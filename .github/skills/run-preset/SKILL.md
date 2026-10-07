---
name: run-preset
description: Run the benchmark preset.
---
- The list of presets and their preset value is in `tools/run_kpi_preset.py`.
- If the user specified an invalid preset, stop and display an error message along with the list of valid presets.
- If a valid preset is specified, set proxies by executing `tools/set_proxy_env.ps1` and thenrun the benchmark using that preset by executing `tools/run_kpi_preset.py --preset {preset_value}`.
- When the benchmark is complete:
  - Display a link to the results directory which is `kpi_runs/{scenario_name}_{timestamp}`. See `tools/run_kpi_workflow.py` for more detail if necessary.
  - Copy `C:\Applications\mlperf_client\mlperf_v2p0\Logs\debug.log` to the results directory and rename the file to `mlperf_detailed_execution.log`.
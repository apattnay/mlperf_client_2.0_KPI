# Run Benchmark Presets (KPI-hub instrumentation)

Presets wrap `mlperf-windows.exe` with KPI-hub HW telemetry sampling and per-prompt workflow KPI
collection (see [tools/run_kpi_workflow.py](../tools/run_kpi_workflow.py)), producing:

- `dashboard.html` - interactive HW KPI (CPU/iGPU/NPU utilization, power, DRAM bandwidth)
- `kpi_report.html` - Workflow KPI (per-prompt tokens/sec, TTFT, Gantt timeline)

Output lands under `kpi_runs/<preset-name>_<timestamp>/` (gitignored).

## Quickstart (fresh machine)

```powershell
git clone --recurse-submodules <this-repo>
cd mlperf_client_2.0_KPI

# For an existing clone, initialize KPI-Hub before setup:
git submodule update --init --recursive

# 1. Python env for KPI-hub instrumentation (creates .venv + installs deps)
.\tools\setup_kpi_hub_env.ps1

# 2. Activate the venv. Re-run this once per NEW terminal session (activation doesn't
#    persist across sessions); lets you use `python` instead of `.venv\Scripts\python.exe`.
.\.venv\Scripts\Activate.ps1

# 3. mlperf-windows.exe 2.0.0 (NOT part of this repo - downloads ~190MB from the official
#    MLCommons GitHub release). Idempotent - safe to re-run, skips if already installed.
.\tools\setup_mlperf_v2.ps1

# 4. On an Intel corporate network only: proxy is required to reach the model/prompt CDN.
#    Re-run this once per NEW terminal session (env vars don't persist across sessions).
. .\tools\set_proxy_env.ps1

# 5. Run any preset - first run per device type downloads its model fresh (~4GB, a few
#    minutes); subsequent runs on the same device type reuse the cached model.
python tools\run_kpi_preset.py --preset 1
```

If step 5 fails with `could not connect to the download server`, you skipped/need step 4.
If the venv's python crashes with `Failed to import encodings module`, some other tool
on the machine (e.g. an OVMS install) has set a stray `PYTHONHOME` env var - clear it first:
`Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue`.

## Prerequisites

```powershell
.\tools\setup_kpi_hub_env.ps1   # one-time: creates .venv + installs KPI-hub requirements
.\tools\setup_mlperf_v2.ps1     # one-time: downloads + installs mlperf-windows.exe 2.0.0
```

## Presets

| Preset | Scope | Device | Config |
|--------|-------|--------|--------|
| 1 | Full base prompt set (all agents: content generation, creative writing, summarization, code analysis) | NPU | `llm/Llama3.1/Intel_NativeOpenVINO_NPU_Default.json` |
| 2 | Full base prompt set (all agents) | iGPU | `llm/Llama3.1/Intel_NativeOpenVINO_GPU_Default.json` |
| 3 | Code-analysis prompts only (`command_parser_cpp_2k`, `performance_counter_group_cpp_2k`) | NPU | `data/configs/kpi_presets/Llama3.1_NativeOpenVINO_NPU_SWEAgent.json` |
| 4 | Code-analysis prompts only | iGPU | `data/configs/kpi_presets/Llama3.1_NativeOpenVINO_GPU_SWEAgent.json` |
| 5 | SWE Agent agentic workflow (multi-turn, uses the `execute` tool + `tools_sandbox.zip`) | NPU | `data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_NPU.json` |
| 6 | SWE Agent agentic workflow | iGPU | `data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_GPU.json` |
| 7 | Data Agent agentic workflow (same config as 5, run with `-q ext`) | NPU | `data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_NPU.json` |
| 8 | Data Agent agentic workflow (same config as 6, run with `-q ext`) | iGPU | `data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_GPU.json` |
| 9 | SWE Agent workflow using the NPU CHw model assets with iGPU execution | iGPU | `data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_GPU_CHw.json` |

All presets run against a single **mlperf 2.0** install (`C:\Applications\mlperf_client\mlperf_v2p0`,
default for `--mlperf-dir`) - only v2.0 supports the `IsAgentic` scenario field the real SWE Agent
scenario (presets 5/6) needs, so all presets standardize on it. Presets 1/2 use the config files
bundled with the v2.0 install itself (`llm/Llama3.1/...`, resolved relative to `--mlperf-dir`);
presets 3/4/5/6/7/8/9 use configs from this repo (`data/configs/...`, resolved as absolute paths).

Presets 3/4 run only the two code-analysis prompts (warmup + 1 iteration each = 4 inference
stages) as a quick, single-shot (non-agentic) stand-in. Presets 5/6 run the **actual** "SWE Agent"
scenario (`category: "SWE Agent"` in
`data/prompts/llama_3_1_8b_instruct/swe_agent/swe-agent-prompts.json`): a multi-turn agentic
conversation (`swe_warmup.md` → `swe_system.md`/`swe_user_0.md` → `swe_agent_0.md` → `swe_user_1.md`
→ ... ) where the model actually invokes the CLI's `execute` tool against a `tools_sandbox.zip`
sandbox, with `Iterations: 3`.

Presets 7/8 run the **actual** "Data Analyst Agent" scenario
(`data/prompts/llama_3_1_8b_instruct/data_agent/da-agent-scenario.json`): a similar multi-turn
agentic conversation (`da_warmup.md` → `da_system.md`/`da_user_0.md` → `da_agent_0.md` → ... →
`da_user_3.md` → `da_agent_3.md`) using the same tools sandbox. The `Intel_NativeOpenVINO_{NPU,GPU}.json`
config files used by presets 5/6 already bundle both scenarios (`InputFilePath.base` = SWE Agent,
`InputFilePath.extended` = Data Agent) - presets 7/8 reuse the exact same config files as 5/6 and
select the Data Agent scenario by passing `-q ext` (`--prompts ext`, an `mlperf-windows.exe` flag)
instead of running the default base/SWE Agent scenario.

Preset 9 follows preset 6's SWE Agent workflow but uses the NPU CHw model URLs with
`device_type: GPU`. It is intended to test this model-variant/device combination; compatibility
and performance are not assumed.

### All presets: network + proxy prerequisites

- **Network access** on every preset — none of the models/prompts are pre-cached in the v2.0
  install (it's a fresh download-on-demand layout, unlike the old v1.5 install). `run_kpi_preset.py`
  passes `-b normal` for all presets so missing files get downloaded and cached under the v2.0
  install for subsequent runs (NPU model assets are shared across presets 1/3/5/7/9; GPU model
  assets are shared across 2/4/6/8).
- **Corporate proxy** — on an Intel corporate network, `client.mlcommons-storage.org` is only
  reachable through the proxy, not via a direct connection. `mlperf-windows.exe` needs both the
  `HTTP(S)_PROXY` env vars *and* the machine-wide WinHTTP proxy set (it doesn't reliably use just
  one) - the WinHTTP part persists across sessions/reboots once set, but the env vars are
  session-scoped, so run [tools/set_proxy_env.ps1](../tools/set_proxy_env.ps1) at the start of
  every new terminal session before running any preset:
  ```powershell
  . .\tools\set_proxy_env.ps1
  .venv\Scripts\python.exe tools\run_kpi_preset.py --preset 1
  ```
  Without this, downloads fail with `could not connect to the download server - check your
  internet connection` even though the host is actually reachable through the proxy.

### Preset 5/6/7/8/9 additional prerequisites

- **Python for the agentic `execute` tool** — this mlperf build has no bundled portable Python, so
  `run_kpi_preset.py` bakes in `--python-path system` for presets 5/6/7/8/9 (uses the machine's
  installed Python). Override by passing your own `--python-path <dir>` after `--preset 5`/`6`/`7`/`8`.
- Expect a notably longer run time than presets 3/4 (3 iterations, multi-turn, tool-execution
  overhead).

## Running a preset

```powershell
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 1
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 2
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 3
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 4
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 5
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 6
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 7
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 8
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 9
```

Override the mlperf installation directory if it's not at the default path:

```powershell
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 4 --mlperf-dir "C:\path\to\mlperf_v2p0"
```

## Optional ETW/GPUView stage capture

Use this only after a KPI-hub run identifies a suspicious stage. The baseline
`workflow_kpi.json` is an output and must not be edited into a replay config; generate a separate
`etl_trace_plan.json`, select stage names from its `stages` object, then replay the same preset.
During replay, `run_kpi_workflow.py` watches live executor-log `power_begin`/`power_end` events,
starts WPR only for selected stages, writes `KPIHUB_ETW_BEGIN/END` WPR markers, and stops at the
stage end or the configured cap. PTAT's default monitor is started once at workflow launch and
stopped at workflow end, so its 1-second CSV covers the selected ETL windows and benefits from
initialization warmup. If a PTAT process is already running, the runner leaves it untouched. The
PTAT CSV is saved beside `hw_samples.csv` and passed into KPI-hub for timestamp alignment; the
shared dashboard shows PTAT as the primary power source, with PDH RAPL then EMON estimates as
fallbacks when the plotter supports `--ptat-csv`. If the bundled `tools/KPI-hub` plotter is older,
set `KPI_HUB_PLOTTER` to a PTAT-capable `plot_utilization_interactive.py` before replay (on this
SUT: `$env:KPI_HUB_PLOTTER = 'C:\KPI-hub-imc-tooltip\plot_utilization_interactive.py'`). Otherwise
the runner warns and creates a dashboard without PTAT while preserving `ptat_power.csv`.
EMON continues to collect DRAM bandwidth/page-hit/latency independently. Baseline offsets are
reference metadata only, not replay timers.

Example for one 20-second capture of the sixth SWE Agent inference stage:

```powershell
python tools\create_etl_trace_plan.py `
  --workflow-kpi kpi_runs\preset9_sweagent_gpu_chw_<baseline>\workflow_kpi.json `
  --output kpi_runs\preset9_sweagent_gpu_chw_<baseline>\etl_trace_plan.json `
  --capture-stage 06_swe_agent_0 `
  --max-capture-seconds 20

python tools\run_kpi_preset.py --preset 9 `
  --etl-trace-plan kpi_runs\preset9_sweagent_gpu_chw_<baseline>\etl_trace_plan.json
```

The replay writes selected `.etl` files under the new run's `etw/` directory, plus
`etl_trace_manifest.json` and a copy of the plan. The active default profile is the installed
GPUView WPR profile. ETL capture requires WPR and the Windows Performance Toolkit; it can
fail cleanly if WPR is already in use. Keep captures short: the prior broad GPUView trace was
393 MB for about 21 seconds. Use the same config, prompt selection, and iteration count as the
baseline so stage ordinals remain comparable. This replays the workflow; it does not add a trace
to the old run. When PTAT data is present, each saved ETL also has a matching `.ptat.csv` in
`etw/`. Its rows fall strictly within the active WPR start/stop-request window (not ETL flush
time); `etl_trace_manifest.json` records `ptat_csv_path` and `ptat_sample_count` for each capture.
Keep the large raw `.etl` files local; commit only the compact plan, manifest, and telemetry
evidence needed to describe a run.

## Notes

**Verified on 2026-10-13 (JF04WVAW0862-TA, preset 9):** a baseline 12-stage KPI file selected
only `06_swe_agent_0`; the replay parsed all 12 stages, saved one GPUView ETL, and exited with
workflow code 0. Evidence is under
`results/evidence/JF04WVAW0862-TA/etl_trace_replay_20261013/preset9_sweagent_gpu_chw_20261013_235856/`:
`etl_trace_manifest.json`, `workflow_kpi.json`, and `etw/stage_06_06_swe_agent_0.etl`.
The manifest's initial `captured_seconds` includes WPR stop/finalization latency; the controller
now records capture-active time separately from ETL flush time for subsequent runs.

**Additional multi-stage validation (2026-10-14, same SUT/preset):** the edited plan selected
`09_warmup` and `10_swe_agent_0` through `12_swe_agent_2`. All four captures were saved; workflow
exit code was 0 and WPR was no longer recording afterward.

| Stage | Capture-active seconds | ETL size | Stop reason |
|---|---:|---:|---|
| `09_warmup` | 2.36 | 96 MiB | stage end |
| `10_swe_agent_0` | 20.13 | 282 MiB | 20-second cap |
| `11_swe_agent_1` | 20.09 | 286 MiB | 20-second cap |
| `12_swe_agent_2` | 4.30 | 113 MiB | stage end |

WPR finalization took an additional ~2.1–6.9 seconds after capture stop was requested; this is
reported separately from the active capture duration. This confirms that even stage-scoped
GPUView traces are sizable, so select only diagnostic stages and keep the duration cap conservative.
Evidence: `results/evidence/JF04WVAW0862-TA/etl_trace_replay_20261014/preset9_sweagent_gpu_chw_20261014_001308/`.

**Combined PTAT + ETW validation (2026-10-14, same SUT/preset):** the edited plan captured stages
09-12 with workflow exit code 0. All four ETLs were saved (104, 326, 336, and 79 MiB); capture
durations were 2.375, 20.14, 20.14, and 3.922 seconds respectively. PTAT recorded 484 samples,
including 3, 20, 20, and 4 samples within those ETL windows. The PTAT-capable plotter aligned
482/499 KPI samples and generated `dashboard.html`. PTAT stopped and WPR returned to idle.
The individual PTAT CSVs were backfilled from the full-run log using those same capture windows.
Evidence: `results/evidence/JF04WVAW0862-TA/ptat_etl_replay_final_20261014/preset9_sweagent_gpu_chw_20261014_010923/`.

**Preset-6 E2E verification (2026-10-14, same SUT):** a plan generated from preset 6's completed
12-stage baseline selected stages 09-12. Workflow exit code was 0, and all four ETLs were saved
with no WPR marker warnings. Active capture times were 3.328, 20.141, 20.14, and 6.812 seconds;
their paired PTAT slices have 4, 20, 20, and 7 samples. The dashboard aligned PTAT to 498/516
KPI samples. WPR was idle and PTAT had stopped afterward. The plan, manifest, and sample CSVs
are under `results/evidence/JF04WVAW0862-TA/ptat_etl_e2e_preset6_20261014/`; the raw ETLs
and generated dashboard remain local and are not included in the repository.
- The iGPU (GPU) presets have previously hit an OpenVINO driver bug
  (`CL_OUT_OF_RESOURCES`, see `Logs/error.log` in the mlperf install dir) on this machine. If a
  GPU preset fails or hangs, a reboot may be needed to reset the GPU driver state before retrying.
  Not reproduced since migrating to the v2.0 binary, but keep an eye out.
- `tools/run_kpi_preset.py` is a thin dispatcher over
  [tools/run_kpi_workflow.py](../tools/run_kpi_workflow.py); any extra CLI args after the preset
  flags are forwarded to it (e.g. `--hw-profile simulation`).
- `tools/set_proxy_env.ps1`'s `netsh winhttp set proxy` line needs an elevated/admin shell to take
  effect machine-wide; if it warns instead of confirming, re-run PowerShell as Administrator once.
- The `tools_sandbox.zip`/`tools_sandbox/` used by presets 5/6's `execute` tool is committed to
  this repo under `data/prompts/llama_3_1_8b_instruct/` - no separate download needed for it.
- Disk/time budget: mlperf-windows.exe 2.0.0 itself is ~190MB; each device-type's Llama-3.1-8B
  model is ~4GB (downloaded once, cached thereafter); presets 1/2 (full prompt set, 3 iterations)
  take tens of minutes, presets 5/6 (agentic, tool-execution) take roughly 10-15 minutes each.
- After a run, project its wall time / throughput onto a hypothetical heavier-duty machine (more
  CPU cores / iGPU XeCores / NPU MACs / memory bandwidth) with
  `what-if\tools\run_roofline_projection.py --run kpi_runs\<experiment> --what-if` - see
  [what-if/docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md](../what-if/docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md).

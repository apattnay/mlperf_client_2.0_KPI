# Run Benchmark Presets (KPI-hub instrumentation)

Presets wrap `mlperf-windows.exe` with KPI-hub HW telemetry sampling and per-prompt workflow KPI
collection (see [tools/run_kpi_workflow.py](../tools/run_kpi_workflow.py)), producing:

- `dashboard.html` - interactive HW KPI (CPU/iGPU/NPU utilization, power, DRAM bandwidth)
- `kpi_report.html` - Workflow KPI (per-prompt tokens/sec, TTFT, Gantt timeline)

Output lands under `kpi_runs/<preset-name>_<timestamp>/` (gitignored).

## Quickstart (fresh machine)

```powershell
git clone <this-repo>
cd mlperf_client_2.0_KPI

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

All presets run against a single **mlperf 2.0** install (`C:\Applications\mlperf_client\mlperf_v2p0`,
default for `--mlperf-dir`) - only v2.0 supports the `IsAgentic` scenario field the real SWE Agent
scenario (presets 5/6) needs, so all presets standardize on it. Presets 1/2 use the config files
bundled with the v2.0 install itself (`llm/Llama3.1/...`, resolved relative to `--mlperf-dir`);
presets 3/4/5/6 use configs from this repo (`data/configs/...`, resolved as absolute paths).

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

### All presets: network + proxy prerequisites

- **Network access** on every preset — none of the models/prompts are pre-cached in the v2.0
  install (it's a fresh download-on-demand layout, unlike the old v1.5 install). `run_kpi_preset.py`
  passes `-b normal` for all presets so missing files get downloaded and cached under the v2.0
  install for subsequent runs (the Llama3.1 NPU/GPU model is shared across presets 1/3/5/7 and
  2/4/6/8 respectively, so it's only downloaded once per device type).
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

### Preset 5/6/7/8 additional prerequisites

- **Python for the agentic `execute` tool** — this mlperf build has no bundled portable Python, so
  `run_kpi_preset.py` bakes in `--python-path system` for presets 5/6/7/8 (uses the machine's
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
```

Override the mlperf installation directory if it's not at the default path:

```powershell
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 4 --mlperf-dir "C:\path\to\mlperf_v2p0"
```

## Notes

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
  `tools\run_roofline_projection.py --run kpi_runs\<experiment> --what-if` - see
  [docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md](ROOFLINE_HW_PROJECTION_METHODOLOGY.md).

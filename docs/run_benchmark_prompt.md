# Run Benchmark Presets (KPI-hub instrumentation)

Presets wrap `mlperf-windows.exe` with KPI-hub HW telemetry sampling and per-prompt workflow KPI
collection (see [tools/run_kpi_workflow.py](../tools/run_kpi_workflow.py)), producing:

- `dashboard.html` - interactive HW KPI (CPU/iGPU/NPU utilization, power, DRAM bandwidth)
- `kpi_report.html` - Workflow KPI (per-prompt tokens/sec, TTFT, Gantt timeline)

Output lands under `kpi_runs/<preset-name>_<timestamp>/` (gitignored).

## Prerequisites

```powershell
.\tools\setup_kpi_hub_env.ps1   # one-time: creates .venv + installs KPI-hub requirements
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

### All presets: network + proxy prerequisites

- **Network access** on every preset — none of the models/prompts are pre-cached in the v2.0
  install (it's a fresh download-on-demand layout, unlike the old v1.5 install). `run_kpi_preset.py`
  passes `-b normal` for all presets so missing files get downloaded and cached under the v2.0
  install for subsequent runs (the Llama3.1 NPU/GPU model is shared across presets 1/3/5 and 2/4/6
  respectively, so it's only downloaded once per device type).
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

### Preset 5/6 additional prerequisites

- **Python for the agentic `execute` tool** — this mlperf build has no bundled portable Python, so
  `run_kpi_preset.py` bakes in `--python-path system` for presets 5/6 (uses the machine's
  installed Python). Override by passing your own `--python-path <dir>` after `--preset 5`/`6`.
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
```

Override the mlperf installation directory if it's not at the default path:

```powershell
.venv\Scripts\python.exe tools\run_kpi_preset.py --preset 4 --mlperf-dir "C:\path\to\mlperf_v2p0"
```

## Notes

- The iGPU (GPU) presets have previously hit an OpenVINO driver bug
  (`CL_OUT_OF_RESOURCES`, see `Logs/error.log` in the mlperf install dir) on this machine. If a
  GPU preset fails or hangs, a reboot may be needed to reset the GPU driver state before retrying.
- `tools/run_kpi_preset.py` is a thin dispatcher over
  [tools/run_kpi_workflow.py](../tools/run_kpi_workflow.py); any extra CLI args after the preset
  flags are forwarded to it (e.g. `--hw-profile simulation`).

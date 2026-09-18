# Agentic Workflow Characterization (MLPerf Client 2.0, Llama-3.1-8B-Instruct)

Learning memory from a clean end-to-end run of presets 1-6 on 2026-09-17 against the official
mlperf-client 2.0.0.c8d2dc0 Windows x64 binary, Intel NativeOpenVINO backend (NPU + iGPU). Raw
run data (dashboards, reports, logs, CSVs) for every run below lives under
[`kpi_runs/`](../kpi_runs/) (tracked via Git LFS).

## 1. What "agentic" means in this benchmark

Presets 5/6 (`data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_{NPU,GPU}.json`,
`"IsAgentic": true`) run the **SWE Agent** scenario: a fixed, pre-scripted multi-turn conversation
(`swe_warmup.md` → `swe_system.md`+`swe_user_0.md` → `swe_agent_0.md`+`swe_user_1.md` → ...),
repeated `Iterations: 3` times. The model is given a system prompt (`data/prompts/llama_3_1_8b_instruct/swe_agent/swe_system.md`)
defining **4 tools** in an Anthropic-style `tool_use` JSON format:

```json
{"type": "tool_use", "name": "read_file", "input": {"path": "bootstrap_mean_ci.py"}}
```

| Tool | Purpose |
|------|---------|
| `read_file` | Read a file (optionally a line range) from the `tools_sandbox` project |
| `write_file` | Overwrite a file with new content |
| `apply_patch` | Apply a unified diff |
| `execute_command` | Run a shell command (e.g. `pytest -q`) |

The Data Agent variant (`data_agent/da_system.md`, used by presets 5/6's "extended" prompt set)
defines an equivalent but differently-named set: `read_file`/`write_file`/`execute`.

**Important caveat**: this is a benchmark, not a live coding-agent product. The turn sequence is
fixed regardless of what the model outputs — the harness does not branch based on tool results.
What genuinely varies per run is (a) whether/how many times the model *attempts* a tool call in a
given turn's output, and (b) the wall-clock time spent actually running that tool.

## 2. Per-turn structure of one SWE Agent iteration

Each of the 3 iterations follows the same 4-stage pattern (observed identically across all 3
iterations of every run — see `preset5_sweagent_npu_*/workflow_kpi.json`):

| Stage | Role | Input tok | Output tok | Wall (s) | TTFT (s) | Tool call(s) |
|-------|------|-----------|------------|----------|----------|--------------|
| `NN_warmup` | Warmup ping | 40 | 44 | ~3.2 | ~1.15 | — |
| `NN_swe_agent` (turn 1) | System + user_0: "explain/refactor this function" | 8198 | 747 | ~60-62 | ~14.5-15.3 | `read_file×1` |
| `NN_swe_agent` (turn 2) | user_1: continue the task | 8979 | 1000 | ~64-65 | ~2.7-2.8 | `read×1, apply_patch×1` |
| `NN_swe_agent` (turn 3) | user_2: final step | 10251 | 43 | ~10.5-11 | ~7.7-7.8 | — (none observed) |

Notable: turn 3 has the largest input context (10.2K tokens, all accumulated history) but the
*smallest* output (43 tokens) and no tool call — the model just produces a short closing response.

### Tool execution timing (NEW instrumentation, see §4)

The gap between one stage's `end_epoch` and the next stage's `start_epoch` is when the harness
actually executes the requested tool (NPU/iGPU sit idle during this window — confirmed via
`hw_samples.csv`). Measured durations, consistent across all 3 iterations and both NPU/GPU runs:

| Tool call | Measured duration |
|-----------|--------------------|
| `read_file×1` | ~5.0s |
| `apply_patch×1` + `read×1` | ~10.0-10.1s |

These gaps are pure CPU/filesystem overhead (script parsing, sandbox extraction bookkeeping), not
attributable to a device — the "CPU" bar in the Workflow Timeline segment now shows this directly.

**Caveat**: a stage's "Tools" badge reflects what the *model attempted to call* (parsed from
`Logs/results.json`'s `Output` field), not proof the harness executed it end-to-end. In one
observed run the model repeated a `read_file` call in a loop (a known small-model failure mode —
its own system prompt explicitly warns against "re-trying the exact same failing command"), yet a
`tools_sandbox/__pycache__/*.pyc` still appeared, which more likely came from an internal
mlperf setup/import step than that specific call.

## 3. Cross-preset comparison (this run, 2026-09-17)

| Preset | Scenario | Device | Stages | Total tokens | Workflow wall time | Output tok/s (workflow avg) |
|--------|----------|--------|--------|---------------|---------------------|------------------------------|
| 1 | Full base prompt set (5 categories × 8) | NPU | 40 | 58,432 | 1141.6s | 4.48 |
| 2 | Full base prompt set | iGPU | 40 | 63,552 | 829.9s | 12.34 |
| 3 | Code-analysis only (2 prompts) | NPU | 4 | 7,936 | 65.1s | 7.87 |
| 4 | Code-analysis only | iGPU | 4 | 7,936 | 57.7s | 8.87 |
| 5 | SWE Agent (agentic, real tool use) | NPU | 12 | 87,906 | 518.6s | 10.61 |
| 6 | SWE Agent (agentic, real tool use) | iGPU | 12 | 91,563 | 838.3s | 10.93 |

"Workflow avg tok/s" includes inter-prompt `Delay` and (for 5/6) tool-execution gaps, so it's a
*system throughput* figure, not a pure-decode device metric — see per-category breakdown below for
that.

### Preset 1 vs 2: per-category decode throughput (output tokens/s, avg of 8 prompts each)

| Category | NPU | iGPU |
|----------|-----|------|
| content_generation | 21.59 | 22.32 |
| creative_writing | 20.06 | 24.28 |
| structured_text | 19.01 | 14.84 |
| code_analysis | 16.25 | 22.70 |
| summarization_intermediate | 12.50 | 18.93 |

iGPU generally out-throughputs NPU here except on `structured_text`, where NPU is notably faster
(19.0 vs 14.8 tok/s) — worth further investigation if this pattern repeats across runs.

## 4. Instrumentation added this session (see [tools/KPI-hub](../tools/KPI-hub) local patches)

- **RAPL power** (CPU/iGPU/NPU/SoC) — was silently disabled by default; now always collected.
- **KV-Cache Size (Estimated)** panel in `dashboard.html` — analytical, derived from per-stage
  token counts × Llama-3.1-8B architecture constants (32 layers, 8 KV heads, head_dim 128, fp16
  cache ⇒ 0.125 MB/token), for scenarios (all of ours) that don't run through an OVMS server.
- **Tools column** in `kpi_report.html`'s Per-Agent KPIs table — per-stage tool-call counts
  parsed from `Logs/results.json`'s `Output` field.
- **CPU tool-execution bar** in the Workflow Timeline — the inter-stage gap described in §2.
- Fixed executor-log parsing for two scenario types: agentic logs have no `Category:` line
  (classify from prompt body instead), and back-to-back (`Delay=0`) prompts pipeline
  `power_begin`/`power_end` across stage boundaries (previously caused `0 stages parsed`).

## 5. Key operational learnings (fresh-machine setup)

- Only **mlperf-windows.exe 2.0.0** supports the `IsAgentic` config field these scenarios need
  (v1.5 rejects it outright at config validation). See [tools/setup_mlperf_v2.ps1](../tools/setup_mlperf_v2.ps1).
- On an Intel corporate network, `client.mlcommons-storage.org` needs the corporate proxy
  (`tools/set_proxy_env.ps1`, re-run per terminal session) — without it, downloads fail with a
  misleading "check your internet connection" even though the proxy path works fine.
- First run per device type downloads the ~4GB Llama-3.1-8B model fresh; subsequent runs reuse it.
- Full details: [docs/run_benchmark_prompt.md](run_benchmark_prompt.md).

## 6. Raw data index

| Run | Path |
|-----|------|
| Preset 1 (NPU, full) | `kpi_runs/preset1_full_npu_20260917_211825/` |
| Preset 2 (iGPU, full) | `kpi_runs/preset2_full_gpu_20260917_213836/` |
| Preset 3 (NPU, code-analysis) | `kpi_runs/preset3_swe_npu_20260917_215335/` |
| Preset 4 (iGPU, code-analysis) | `kpi_runs/preset4_swe_gpu_20260917_215500/` |
| Preset 5 (NPU, SWE Agent agentic) | `kpi_runs/preset5_sweagent_npu_20260917_215620/` |
| Preset 6 (iGPU, SWE Agent agentic) | `kpi_runs/preset6_sweagent_gpu_20260917_220525/` |

Each directory contains `dashboard.html` (HW telemetry), `kpi_report.html` (workflow KPIs),
`workflow_kpi.json`, `experiment.json`, `hw_samples.csv`, and the raw `mlperf_stdout.log`.

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

### Tool execution timing (NEW instrumentation, see §5)

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

## 4. RCA: iGPU (GRw) never completes `apply_patch` — deterministic, not an anomaly

Comparing preset 5 (NPU, `Llama-3.1-8B-Instruct_ov-int4-**CHw**`, channel-wise int4) against preset 6
(iGPU, `Llama-3.1-8B-Instruct_ov-int4-**GRw**`, group-wise int4) on the *same* agentic SWE Agent
config turned up a real, reproducible divergence, not a one-off fluke:

| Turn | NPU (CHw) behavior | iGPU (GRw) behavior |
|------|---------------------|----------------------|
| turn 1 (`02/06/10_swe_agent`) | `read_file×1` | no tool call, 1000/1000 output tokens (hits cap) |
| turn 2 (`03/07/11_swe_agent`) | `read×1` + `apply_patch×1` — completes the task | `read_file×4` in a loop, **no `apply_patch` ever emitted**, 1000/1000 output tokens (hits cap) |
| turn 3 (`04/08/12_swe_agent`) | short closing reply (43 tok) | no tool call, 1000/1000 output tokens (hits cap) |

**Root cause, confirmed via mlperf's `Logs/results.json` (JSON-lines, one entry per process launch)
mined across every historical run of this session:**

1. The scenario config (`data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_{NPU,GPU}.json`)
   references `"ResultsVerificationFile": "...generation-greedy-results.json"` — decoding is
   **greedy** (always argmax, no sampling/temperature). With greedy decoding, output is a pure
   function of (model weights, backend numerics, input tokens) — there is no randomness to
   average out.
2. Cross-checking **5 independent NPU launches** and **3 independent iGPU launches** (separate
   `mlperf-windows.exe` process invocations at different points in the session, not just the 3
   `Iterations` within one launch) showed the **exact same** tool-call pattern every single time,
   per device — 5/5 for NPU, 3/3 for iGPU. Diffing the raw `Output` text between the
   earliest and latest run of each device confirmed the generated text is **byte-for-byte
   identical** across independent launches.
3. Inspecting the actual generated text for the iGPU turn-2 stage shows the model stuck in a
   genuine repetition loop: it calls `read_file` on the same path, explains the function, says
   "I will now refactor the function to improve its readability and maintainability", then calls
   `read_file` again on the *same* path — repeating this cycle 4 times until the 1000-token
   output cap is hit, never reaching the point of emitting an `apply_patch` call.

**Why "better" GRw quantization didn't help:** quantization fidelity (GRw's finer per-group
scales vs CHw's per-channel scales) is normally measured as average perplexity/accuracy — it says
nothing about avoiding degenerate repetition loops, a well-known pathology of *any*
greedy-decoded transformer once a repeated phrase becomes locally arg-max-preferred at some
decoding step. NPU and iGPU also run different OpenVINO kernel implementations (different
matmul/attention fusion, accumulation order, int4 dequant paths) even for nominally "the same"
math; at ~9K accumulated input tokens, small numeric differences are enough to flip a single
argmax tie at the "wrap up and call `apply_patch`" decision point, and with greedy decoding (no
repetition penalty, no sampling escape hatch) there is no way back out of the resulting loop. This
is not a compute-capacity problem — iGPU's larger compute budget doesn't fix an argmax tie-break
going the wrong way.

**Conclusion:** this is a systematic, 100%-reproducible defect in the iGPU/GRw agentic SWE Agent
path as currently configured (greedy decoding, no repetition penalty) — worth flagging to
MLCommons/the model-quantization owners, since it means the iGPU run of this scenario *never*
exercises the `apply_patch` tool in practice, only `read_file` in a loop.

## 5. Instrumentation added this session (see [tools/KPI-hub](../tools/KPI-hub) local patches)

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

## 6. Key operational learnings (fresh-machine setup)

- Only **mlperf-windows.exe 2.0.0** supports the `IsAgentic` config field these scenarios need
  (v1.5 rejects it outright at config validation). See [tools/setup_mlperf_v2.ps1](../tools/setup_mlperf_v2.ps1).
- On an Intel corporate network, `client.mlcommons-storage.org` needs the corporate proxy
  (`tools/set_proxy_env.ps1`, re-run per terminal session) — without it, downloads fail with a
  misleading "check your internet connection" even though the proxy path works fine.
- First run per device type downloads the ~4GB Llama-3.1-8B model fresh; subsequent runs reuse it.
- Full details: [docs/run_benchmark_prompt.md](run_benchmark_prompt.md).

## 7. Raw data index

| Run | Path |
|-----|------|
| Preset 1 (NPU, full) | `kpi_runs/preset1_full_npu_20260917_211825/` |
| Preset 2 (iGPU, full) | `kpi_runs/preset2_full_gpu_20260917_213836/` |
| Preset 3 (NPU, code-analysis) | `kpi_runs/preset3_swe_npu_20260917_215335/` |
| Preset 4 (iGPU, code-analysis) | `kpi_runs/preset4_swe_gpu_20260917_215500/` |
| Preset 5 (NPU, SWE Agent agentic) | `kpi_runs/preset5_sweagent_npu_20260917_215620/` |
| Preset 6 (iGPU, SWE Agent agentic) | `kpi_runs/preset6_sweagent_gpu_20260917_220525/` |
| Preset 5 + cold/warm/Prefill/ITL instrumentation (NPU, §8) | `kpi_runs/preset5_sweagent_npu_itl_20260918_002637/` |
| Preset 5 + roofline projection (NPU, §10) | `kpi_runs/preset5_roofline_20260923_140129/` |
| Preset 6 + roofline projection (iGPU, §10) | `kpi_runs/preset6_roofline_20260923_141035/` |

Each directory contains `dashboard.html` (HW telemetry), `kpi_report.html` (workflow KPIs),
`workflow_kpi.json`, `experiment.json`, `hw_samples.csv`, and the raw `mlperf_stdout.log`.

## 8. Identifying inference phases: Prefill, TTFT, Decode, Cold vs Warm

Every agentic turn goes through: input tokens → **Prefill** (build the KV cache from the full
prompt) → **first output token** → **Decode** (one token at a time, appending to the KV cache
each step) → turn ends → next turn either starts a fresh context (**cold**) or continues the
same conversation, carrying its KV cache forward (**warm**). `mlperf-windows.exe` doesn't log a
named "prefill" or "decode" event, but `Logs/<scenario>_executor.log` (the file
`run_kpi_workflow.py` already parses) contains everything needed to reconstruct these phase
boundaries:

```
- inference task added. history: 149, user: 632, expected: 1212
...
TTFT 2756.537500ms, 2nd+ token latency 61.461646ms
...
Average 2nd+ Token Latency: (ms) 61.462 (+-4.021)
```

| Phase | How it's identified |
|---|---|
| **Cold vs Warm** | The `history:` field on the `inference task added` line. `history: 0` ⇒ **cold** (fresh context). `history: N > 0` ⇒ **warm** — `N` is literally the *previous turn's own generated-token count*, fed back in as context for this turn. |
| **Prefill (+ KV cache build)** | Starts at `power_begin`, ends when the first token is emitted. Not logged as its own timestamp, but estimated as `prefill_ms ≈ TTFT − avg_2nd+_token_latency` (i.e. TTFT minus the cost of one normal decode step — the remainder is prefill/KV-warmup). |
| **First Token / TTFT** | Logged directly: `TTFT <x>ms`. This is prefill + emitting token #1, combined (the standard TTFT definition). |
| **Decode phase (steady-state, one token + KV update at a time)** | `2nd+ token latency` per call, and the aggregated `Average 2nd+ Token Latency: (ms) <mean> (+-<stddev>)` line (mean + std-dev across the individual per-token decode steps — not full percentiles, so true p50/p99 ITL isn't derivable from this log). Decode ends at `power_end` / `Ran inference and got N tokens...`, matching `output_tokens`. |
| **Turn boundary / KV carry-forward** | The *next* turn's `history:` value equals *this* turn's generated-token count — direct evidence the conversation (and KV state) is being extended turn-over-turn rather than starting over. |

**Is the KV cache actually being reused across turns, or is every turn re-prefilled from
scratch?** TTFT scaling answers this. On the NPU SWE Agent run:

| Turn | New tokens (history+user) | Total context | AVG TTFT |
|---|---|---|---|
| 1 (cold) | 8,198 | 8,198 | 14,634 ms |
| 2 (warm) | 781 | 8,979 | 2,752 ms |
| 3 (warm) | 1,272 | 10,251 | 7,818 ms |

Turn 2 has a *larger total context* than turn 1 but a *5x smaller* TTFT. If the runtime were
re-prefilling the whole context from scratch every turn, turn 2's TTFT would have to be ≥ turn
1's — it isn't, which is direct evidence the OpenVINO GenAI pipeline reuses/extends the KV cache
across turns instead of recomputing it. The per-new-token cost still rises with accumulated
context (1.79 → 3.52 → 6.15 ms/new-token for turns 1→2→3), consistent with attention cost per new
token scaling with the total KV cache length already resident.

**Instrumentation added to close this gap:** `parse_executor_log()` now also extracts the
`history:`/`user:` fields (→ per-stage `is_cold` / `history_tokens` / `turn_new_tokens`) and the
`2nd+ token latency` mean + std-dev (→ `avg_itl_ms` / `itl_stddev_ms`, and a derived
`prefill_ms_est`). `kpi_report.html`'s Per-Agent KPIs table now shows a cold/warm badge next to
each stage name, a **Prefill (ms)** column, and a populated **Avg ITL (ms)** column (previously
always `-` for these in-process NativeOpenVINO scenarios — the data existed in the log but wasn't
being parsed). `p50/p99 ITL` remains `-`: the log only exposes mean + std-dev per turn, not a full
per-token latency distribution, so real percentiles aren't derivable without further
instrumentation upstream in mlperf-windows.exe itself.

## 9. Roofline projection methodology

The [Roofline model](https://en.wikipedia.org/wiki/Roofline_model) bounds achievable performance
by the lesser of two "roofs": a device's peak compute rate, and its peak memory bandwidth scaled
by the workload's **arithmetic intensity** (AI, FLOPs processed per byte moved from memory). Where
a workload sits relative to these roofs tells you whether it's compute-bound or memory-bound, and
how much headroom is left. Section 8 already gives us the two phases (prefill/decode) needed to
apply this per turn.

### Deriving arithmetic intensity for prefill vs decode

Both phases do the same `2 × params` FLOPs of work per output token (standard transformer FLOPs
estimate), but access memory completely differently:

- **Prefill** processes the entire prompt as one batched pass — the model weights are read from
  memory **once** but reused across every input token, so intensity scales *with prompt length*:
  `AI_prefill = (2 × N_input_tokens) / bytes_per_weight`. Longer prompts push further right on the
  roofline (deeper into the compute-bound region).
- **Decode** generates one token at a time (batch=1) — the *entire* weight has to be re-read from
  memory for every single token, so intensity is a small **constant**, independent of context
  length or parameter count: `AI_decode = 2 / bytes_per_weight`. For our int4 models
  (0.5 bytes/weight), that's `AI_decode = 4 FLOPs/Byte` — always the same point on the x-axis,
  deep in the memory-bound region. This is the textbook reason LLM decode is memory-bandwidth
  bound regardless of how fast the compute unit is.

### Achieved performance per phase

Using the already-instrumented per-stage fields from section 8:

- `GFLOPs/s_prefill = (2 × params × input_tokens) / (prefill_ms_est / 1000) / 1e9`
- `GFLOPs/s_decode = (2 × params) / (avg_itl_ms / 1000) / 1e9`

`params` is estimated from the *actual* downloaded model weight file size (`model_weight_mb`) and
the quantization scheme's bytes/weight (0.5 for int4, our presets' scheme) —
`params = model_weight_mb × 1024² / bytes_per_weight`. This was previously broken for every
NPU/iGPU run in this benchmark: `resolve_model()` only sized `model_weight_mb` for `file://`-style
configs, and every LLM/agentic preset uses `https://`-downloaded models, so it silently stayed `0`,
which cascaded into `Est. Parameters` falling back to a hardcoded `~4.0B` (half the real ~8B) and
`DRAM BW Achieved`/`Memory BW Utilization` always showing `0.0`/`0.0%` in the efficiency table.
Fixed by checking the predictable local cache path mlperf-windows.exe actually downloads
`https://` models into: `dependencies/llm/<scenario>/models/<backend>/<model_name>/` (e.g.
`.../models/NativeOpenVINO/Llama-3.1-8B-Instruct_ov-int4-CHw/`, confirmed on disk at 3835 MB for
CHw / 3950 MB for GRw — both correctly resolve to ~8.0B params once sized).

### The two roofs

- **Memory bandwidth roof** (diagonal on log-log axes): peak system DRAM bandwidth (89 GB/s DDR5,
  shared by CPU/iGPU/NPU on this platform — confirmed via the SUT hardware section showing the
  iGPU has no dedicated VRAM, only "shared USM"). NVIDIA (Ollama) configs use the dedicated VRAM
  bandwidth instead (672 GB/s GDDR7).
- **Compute roof** (flat line): device-dependent, and NOT equally knowable for every device here:
  - **iGPU**: a *theoretical* peak (4.096 TFLOPS FP16 FMA, derived from this exact iGPU's detected
    96 EU / 2300 MHz spec) **and** a *measured* practical ceiling (78 GFLOPS/s int4 GEMM,
    empirically observed) — both plotted, since real achievable throughput is well below the
    theoretical spec sheet number.
  - **NVIDIA**: theoretical tensor-core peak (123.4 TFLOPS FP16) from vendor spec sheets.
  - **NPU**: this benchmark's hardware probing only detects a generic `"Intel(R) NPU"` string, with
    no exposed EU count/clock or vendor TOPS spec to compute a theoretical peak from (unlike the
    iGPU case above). Rather than guess a number we can't verify, the NPU compute roof is derived
    **empirically from the run itself**: the maximum observed prefill-phase GFLOPs/s across all
    stages (prefill sits deep in the compute-bound region, making it a reasonable practical ceiling
    proxy) — labeled clearly as "Empirically Observed Peak (this run)", not a vendor spec.

### Implementation

`tools/KPI-hub/generate_kpi_report.py`'s new `_build_roofline_html()` renders this as a log-log
Plotly scatter chart (one point per stage per phase — ▲ prefill, ● decode, colored/labeled with
cold/warm) against the roofline curves, plus a plain data table, in a new "*Device* Roofline
Projection" report section (next to the existing Efficiency Analysis section, which now also
reports the corrected `Est. Parameters`/`DRAM BW Achieved`/`Memory BW Utilization` numbers).

**Worked example** (preset 5, NPU, turn 1 / `02_swe_agent_0`, cold, 8198 input tokens): prefill AI
≈ 32,792 FLOPs/Byte (deep compute-bound) at ~10 TFLOPs/s achieved; decode AI = 4 FLOPs/Byte
(memory-bound, constant across every stage) at ~281 GFLOPs/s achieved, cross-checking to
`achieved_BW = 281 GFLOPs/s / 4 FLOPs/Byte ≈ 70 GB/s` — 79% of the 89 GB/s DDR5 peak for that
specific turn (the previously-reported `0.0%` was purely the `model_weight_mb` sizing bug, not a
real efficiency finding).

## 10. Preset 5 vs 6 roofline comparison (fresh run, 2026-09-23)

Clean re-run of both presets back-to-back to validate section 9's methodology and compare NPU vs
iGPU roofline positioning directly (`kpi_runs/preset5_roofline_20260923_140129/` NPU,
`kpi_runs/preset6_roofline_20260923_141035/` iGPU).

| Metric | NPU (CHw) | iGPU (GRw) |
|---|---|---|
| Est. Parameters | ~8.1B | ~8.4B |
| Model Weight | 3,868 MB | 3,984 MB |
| End-to-End Throughput | 11.2 tok/s | 13.3 tok/s |
| Decode Throughput (sum-of-agents) | 17.3 tok/s | 24.8 tok/s |
| DRAM BW Achieved | 65.2 GB/s | 96.6 GB/s |
| Memory BW Utilization | 73.2% | 108.6% ⚠️ (see caveat below) |

### The roofline split: NPU wins prefill, iGPU wins decode

Pulling the actual (AI, achieved GFLOPs/s) points from both reports' roofline charts:

| Phase | NPU achieved | iGPU achieved | Winner |
|---|---|---|---|
| Prefill, turn 0 (cold, AI≈32,792) | ~10,000-10,400 GFLOPs/s | ~2,770-2,780 GFLOPs/s | **NPU, ~3.7x** |
| Prefill, turn 1 (warm, AI≈35,916) | ~59,000-61,200 GFLOPs/s | ~16,540-16,610 GFLOPs/s | **NPU, ~3.6x** |
| Prefill, turn 2 (warm, AI≈41,004) | ~24,070-24,360 GFLOPs/s | ~10,340-10,360 GFLOPs/s | **NPU, ~2.3x** |
| Decode, all turns (AI=4, constant) | ~250-370 GFLOPs/s | ~410-460 GFLOPs/s | **iGPU, ~1.5x** |

A consistent, clean architectural split: **NPU dominates the compute-bound prefill phase by
2-4x**, **iGPU dominates the memory-bound decode phase by ~1.5x** — plausible given NPUs typically
carry dedicated matmul/systolic-array acceleration well-suited to large batched GEMMs (prefill),
while the iGPU's memory subsystem sustains higher single-token streaming reads (decode).

### Data-quality caveats surfaced by this comparison

1. **RESOLVED 2026-09-23** — iGPU's 108.6% Memory BW Utilization was physically impossible
   (achieved can't exceed true peak). Root-caused via `tools/KPI-hub/DATA_SOURCES.md`'s own
   "Hardware Platform Reference" table: the hardcoded `89 GB/s` constant was measured on a
   *different* reference platform (2 channels × 64-bit DDR5-6400 = 102.4 GB/s theoretical), not
   this machine. Queried this machine's actual memory config directly
   (`Get-CimInstance Win32_PhysicalMemory`: 8 channels × 16-bit × 8533 MT/s configured clock =
   **136.5 GB/s** real theoretical peak — a fundamentally different, higher-bandwidth memory
   subsystem). `generate_kpi_report.py` now detects this dynamically per-machine instead of
   assuming a fixed constant (see `docs/KPI_HUB_INTEGRATION_NOTES.md` §7). Recomputed with the
   correct peak: NPU 47.8%, iGPU 70.8% — both physically valid, and their ratio (1.48x) matches
   the ~1.5x decode-speed advantage already established above.
2. **PARTIALLY EXPLAINED 2026-09-23** — the `IGPU_MEASURED_TOPS_INT4 = 0.078` TOPS (78 GFLOPs/s)
   constant isn't simply "stale"; `DATA_SOURCES.md` §6/§10 documents it was measured via a
   synthetic SYCL GEMM microbenchmark on only 16 of a **128-EU reference iGPU** — a different chip
   than this machine's 96-EU iGPU (confirmed via the SUT hardware section), using a fundamentally
   different measurement method (isolated matmul kernel vs. real OpenVINO decode throughput). Left
   as-is but now labeled `"(different reference chip)"` in the report so it's not read as directly
   comparable to this machine's real decode numbers.
3. Per the section 4 RCA: **every iGPU turn hits the 1000-token cap** (repetition loop, never
   calls `apply_patch`) while NPU completes tasks with variable, smaller output (747/1000/43). So
   iGPU's decode throughput reflects *how fast it loops*, not *how fast it completes real work* —
   the per-token GFLOPs/s comparison is still valid, but the two devices aren't producing
   equivalent amounts of useful output in this scenario.
4. **RESOLVED 2026-09-23** — caveat 2 above (the 128-EU reference iGPU constant) turned out to have
   a third independent problem beyond the chip mismatch: the reference GEMM microbenchmark used a
   synthetic 2048×2048×2048 square matrix, while this workload's real GEMMs (Llama-3.1-8B: prefill
   `M×4096×14336` tall-skinny, decode pure GEMV with `M=1`) never take that shape — so the 78
   GFLOPs/s "ceiling" isn't just measured on the wrong chip, it's also measured on the wrong problem
   shape. Since no reliable external compute-peak constant exists for this workload on any given
   machine, `generate_kpi_report.py` now computes an **empirical compute peak** (this run's own best
   observed prefill GFLOPs/s) consistently for NPU, iGPU, and NVIDIA alike, and reports a new
   **"Compute Util. %"** column against that self-referential peak instead of the Nova Lake
   constants (kept on the chart only as clearly-labeled context, not used in any percentage math).

## 11. Projecting to different (hypothetical) hardware

Sections 9-10 above characterize how efficiently *this run* uses *this machine's* hardware. A
separate, complementary tool - `tools/roofline_projection/` (CLI: `tools/run_roofline_projection.py`)
- answers a different question: given this run's measured behavior, how would wall time /
tokens-per-second / tokens-per-Joule change on a **different, hypothetical** system (more CPU
cores, iGPU XeCores, NPU MACs, memory bandwidth)? It decomposes each stage into compute-bound
(prefill), memory-bound (decode), CPU-bound (tool execution), and fixed-overhead buckets, scales
each independently by the relevant hardware ratio, and sums back up to a projected total - plus a
standalone interactive dropdown "what-if" calculator (`what_if_calculator.html`). See
[docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md](ROOFLINE_HW_PROJECTION_METHODOLOGY.md) for the full
methodology, equations, and a worked example on this same `preset5_roofline_20260923_140129` run.



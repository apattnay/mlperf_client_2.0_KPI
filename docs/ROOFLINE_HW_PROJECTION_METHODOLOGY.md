# Roofline Hardware Projection Methodology

Canonical reference for `tools/roofline_projection/` (+ CLI `tools/run_roofline_projection.py`):
a bottom-up, macro-component pipeline that projects a **measured** `kpi_runs/<experiment>` run
(this machine) onto a **hypothetical target system** with more CPU cores, iGPU XeCores, NPU MACs,
and/or memory bandwidth — answering "if we ran this same agentic workload on a heavier-duty
machine, how much would wall time / tokens-per-second / tokens-per-Joule improve, and *why*?"

This builds directly on the roofline analysis already in `tools/KPI-hub/generate_kpi_report.py`
(see [AGENTIC_WORKFLOW_CHARACTERIZATION.md §9-11](AGENTIC_WORKFLOW_CHARACTERIZATION.md)) which
plots *this machine's own* measured (arithmetic intensity, achieved GFLOPs/s) points against
*this machine's own* roofs. That answers "how efficiently does this run use *this* hardware?".
This tool answers a different question: "how would the *same measured behavior* change on
*different* hardware?" — it is a projection/what-if tool, not a new measurement.

## 1. Why bottom-up macro components, not a single wall-time multiplier

A single "the CPU is N% faster so wall time is 1/N" multiplier is wrong for an agentic LLM
workflow because different parts of the wall-clock timeline are bound by *different* resources:

| Macro component | Bound by | Scales with |
|---|---|---|
| **Prefill** (prompt/context processing) | Compute (large batched GEMMs) | Accelerator compute capability (NPU MACs × freq, or iGPU XeCores × freq) |
| **Decode** (autoregressive token generation, batch=1) | Memory bandwidth (re-reads full model weight per token) | Memory channels × width × transfer rate |
| **Tool execution** (git apply, pytest, file IO, ...) | CPU (mostly single/few-threaded processes) | CPU cores × frequency, via Amdahl's law (not everything parallelizes) |
| **Stage/harness overhead** (logging, IPC, bookkeeping) | Fixed cost of the software stack | Nothing — assumed constant |

Scaling the whole wall time by one factor would over- or under-credit whichever component
happens to dominate a given stage. The bottom-up approach decomposes each stage into these four
buckets, projects **each bucket independently** by the resource ratio that actually governs it,
then **sums back up** — giving a physically-grounded total, not a guess.

```mermaid
flowchart TD
    A[kpi_runs/&lt;experiment&gt;<br/>workflow_kpi.json + experiment.json + hw_samples.csv] --> B[baseline_extractor.py<br/>per-stage macro-component profile]
    B --> C{scaling_engine.py}
    D[hw_spec.py<br/>SystemSpec: baseline &amp; target] --> C
    C --> E[Projected per-stage times]
    E --> F[report.py<br/>HTML + JSON report]
    E --> G[what_if_calculator.py<br/>standalone dropdown calculator]
```

## 2. Step 1 — Baseline extraction (`baseline_extractor.py`)

For every non-orchestrator stage in `workflow_kpi.json`, already-measured fields are turned into
four time buckets (no new measurement, pure arithmetic on existing fields):

```
prefill_s          = prefill_ms_est / 1000                         # already derived: ttft_ms - avg_itl_ms
decode_s           = avg_itl_ms / 1000 * output_tokens              # per-token decode latency × tokens generated
                     # (if prefill_s + decode_s > wall_time_s due to measurement noise, both are
                     # rescaled proportionally so the invariant below holds exactly, not just usually)
stage_overhead_s   = max(wall_time_s - prefill_s - decode_s, 0)     # residual bookkeeping inside the stage
tool_exec_gap_s    = max(next_stage.start_epoch - this_stage.end_epoch, 0)   # harness runs a tool here (NPU/iGPU idle)
                     # for the LAST stage, "next_stage.start_epoch" is workflow timeline.end_epoch,
                     # not 0 - a final verification/tool call after the last LLM turn is still
                     # CPU-bound tool-exec time, not workflow-level fixed overhead (see below)
```

By construction: `sum(all four buckets across all stages) + fixed_overhead_s == workflow_wall_time_s`,
where `fixed_overhead_s` is whatever's left over — with the last-stage fix above, this is now (for
all 4 validated runs) essentially just the gap BEFORE the first stage starts (process startup,
model load), not a mix of that plus an unaccounted-for tail after the last stage.

> **2026-09-24 fix**: an earlier version always set `tool_exec_gap_s = 0` for a run's last stage
> (it only looked at the *next* stage's start, and there is none), so any real tool-call time after
> the final LLM turn silently fell into `fixed_overhead_s`, which never scales with target hardware
> — understating projected speedup for workloads whose last action is a heavy CPU-bound tool call.
> Fixed by using the workflow's own `timeline.end_epoch` as the "next start" for the last stage.

If `hw_samples.csv` is present, RAPL power (`rapl_cpu_w`/`rapl_igpu_w`/`rapl_npu_w`/`rapl_soc_w`)
is averaged over each stage's **whole bucket window** (`start_iso` through the *next* stage's
`start_iso`, or `workflow_end_iso` for the last stage — i.e. prefill+decode+overhead+tool_exec_gap,
not just the stage's own active `start_iso`→`end_iso`) for optional energy/Tokens-per-Joule
projection (best-effort — silently omitted if pandas isn't installed or the columns are
absent/zero, never a hard requirement for the wall-time projection). The window is deliberately
widened past `end_iso` because the averaged watts get multiplied by that same whole-bucket
duration downstream (`scaling_engine.py`'s `energy_j` calc) — averaging only over the active
sub-window and then applying it across the full bucket would misattribute the (typically higher)
active-generation power to the (typically lower, accelerator-idle) tool-exec-gap time too,
overstating energy for any stage with a real gap. **The lookup window uses `start_iso`/`end_iso` (naive
local-time strings), NOT `start_epoch`/`end_epoch`** — this matters and is not an arbitrary choice:
`hw_samples.csv`'s `timestamp` column is naive local time written by the sampler subprocess, while
`start_epoch`/`end_epoch` are true UTC Unix epoch from `time.time()` in the orchestrator process.
Converting the CSV's naive-local strings to Unix epoch (e.g. via pandas `datetime64->int64`)
silently produces a wrong, timezone-offset-shifted value with **no error** — every stage window
then matches zero rows, and Tokens/Joule always comes back `None` with no indication why. This
exact bug was hit and fixed during development (see `tools/roofline_projection/baseline_extractor.py`
`_load_power_lookup`'s docstring) by switching to the same naive-datetime-comparison approach
already proven correct in `tools/KPI-hub/plot_utilization_interactive.py::load_phases()`.

TTFT and per-token decode latency (ITL) are also captured per stage for direct reporting
(`ttft_ms = ttft_s*1000` straight from the log when present, else `prefill_s*1000 + itl_ms` as a
fallback; `itl_ms = avg_itl_ms` straight from the log) — these mirror the same definitions
established in `docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §8.

## 3. Step 2 — Hardware spec + capability ratios (`hw_spec.py`)

A `SystemSpec` is the exact set of knobs the what-if dropdown UI exposes:

```
cpu_cores, cpu_freq_ghz
igpu_xecores, igpu_freq_ghz
npu_macs, npu_freq_ghz
mem_channels, mem_width_bits, mem_freq_mts
```

Derived "relative capability" numbers (linear in count × frequency — **not** claimed to be
physical FLOPs/s, since no verified vendor FLOPs-per-MAC / FLOPs-per-XeCore constant exists for
this exact silicon+workload combination, see `docs/KPI_HUB_INTEGRATION_NOTES.md`):

```
cpu_capability   = cpu_cores × cpu_freq_ghz
igpu_capability  = igpu_xecores × igpu_freq_ghz
npu_capability   = npu_macs × npu_freq_ghz
mem_bw_peak_gbs  = mem_channels × (mem_width_bits / 8) × mem_freq_mts / 1000
```

Because every projection downstream only ever uses **ratios** between a baseline and a target
`SystemSpec` (never an absolute value), the fact that these are relative units rather than
verified physical peaks doesn't matter — the ratio is dimensionally consistent and self-cancels
any unknown proportionality constant. This is the same self-referential-calibration principle
already used by the empirical-compute-peak fix in `generate_kpi_report.py`'s roofline chart.

**Important**: `cpu_cores`/`igpu_xecores`/`npu_macs` counts are **not auto-detectable** from this
benchmark's tooling (only `cpu_cores`, and `mem_channels`/`mem_width_bits`/`mem_freq_mts` via
`Get-CimInstance Win32_PhysicalMemory`, are). You must supply your baseline machine's real iGPU
XeCore / NPU MAC counts from its datasheet — see
`data/configs/roofline_targets/current_baseline_TEMPLATE.json`.

## 4. Step 3 — Scaling engine (`scaling_engine.py`)

**The complete closed-form wall-time equation** (every term defined in detail below):

```
Total projected wall time =
    Σ over all stages i ( prefill_s[i]/E_c + decode_s[i]/E_m + tool_exec_gap_s[i]/E_cpu + stage_overhead_s[i] )
    + fixed_overhead_s

  where, computed ONCE per projection (same for every stage - depend only on baseline vs target
  hardware, not on any per-stage data):
    E_c   = effective_speedup( compute_capability(target)/compute_capability(baseline), compute_efficiency_retention )
    E_m   = effective_speedup( mem_bw_peak_gbs(target)/mem_bw_peak_gbs(baseline),       memory_efficiency_retention )
    E_cpu = effective_speedup( amdahl_speedup(cores_ratio, freq_ratio, tool_parallel_fraction), cpu_efficiency_retention )

Speedup             = Total baseline wall time / Total projected wall time
Wall time reduction = (Total baseline wall time - Total projected wall time) / Total baseline wall time × 100%
Tokens/s             = total_output_tokens / Total wall time
```

Per stage, per macro component:

```
raw_speedup(target, baseline)              = target / baseline                          (guarded against /0)

effective_speedup(raw, retention)          = 1 + (raw - 1) × retention
    # retention ∈ [0,1]: 1.0 = ideal ("iso-efficiency") scaling - the resource ratio is fully
    # realized. Lower values model real-world losses at scale (NUMA, contention, driver
    # overhead) that keep bigger systems from scaling perfectly linearly.

amdahl_speedup(cores_ratio, freq_ratio, p) = freq_ratio / ((1 - p) + p / cores_ratio)
    # Amdahl's law for CPU-bound tool execution. p = assumed parallel fraction of that time
    # (default 0.5 - process spawn/disk IO/etc. don't scale with core count, only some work does).
    # freq_ratio multiplies the WHOLE expression, not just the parallel term: a faster clock
    # speeds up every instruction regardless of how many cores it runs on, so the serial fraction
    # benefits from freq_ratio exactly as much as the parallel fraction does - only extra CORES are
    # gated by p. (A fixed 2026-09-24 bug: an earlier version divided only the parallel term by
    # freq_ratio, which implied a 100%-serial component, p=0, gets ZERO benefit from a faster clock
    # - clearly wrong, since serial code still runs faster on a faster core. Corrected here.)
```

**2026-09-24: `efficiency_retention` is now THREE independent knobs, not one shared value** -
`compute_efficiency_retention` (prefill), `memory_efficiency_retention` (decode),
`cpu_efficiency_retention` (tool-exec, on top of Amdahl's law). See §4.1 for why: NPU-MAC,
iGPU-XeCore, and memory-bandwidth paths on a real SoC do NOT achieve the same fraction of their
own theoretical peak, so one shared "efficiency" number was hiding a real, measurable difference.
A legacy single `--efficiency-retention` / `efficiency_retention` JSON key still works and sets all
three at once (for backward compatibility) unless a per-domain value is also given.

Applied per stage:

```
compute_eff  = effective_speedup( raw_speedup(target.compute_capability(device_type),
                                               baseline.compute_capability(device_type)),
                                   compute_efficiency_retention )
prefill_target = prefill_s / compute_eff

mem_eff      = effective_speedup( raw_speedup(target.mem_bw_peak_gbs, baseline.mem_bw_peak_gbs),
                                   memory_efficiency_retention )
decode_target = decode_s / mem_eff

cpu_eff      = effective_speedup( amdahl_speedup(cores_ratio, freq_ratio, p), cpu_efficiency_retention )
tool_target  = tool_exec_gap_s / cpu_eff

overhead_target = stage_overhead_s          # unchanged - not resource-bound

projected_stage_wall = prefill_target + decode_target + tool_target + overhead_target

# TTFT/ITL (ms): ITL scales with the same per-token memory speedup that drives decode_target,
# since decode_target IS itl_ms/1000*output_tokens / mem_eff.
itl_target_ms    = itl_ms / mem_eff
ttft_target_ms   = prefill_target * 1000 + itl_target_ms
```

Rolled up:

```
Total baseline wall  = Σ(stage buckets) + fixed_overhead_s      == workflow_wall_time_s (sanity check)
Total projected wall = Σ(projected stage buckets) + fixed_overhead_s   (fixed_overhead_s unchanged)

Speedup             = Total baseline wall / Total projected wall
Wall time reduction = (Total baseline wall - Total projected wall) / Total baseline wall × 100%
Tokens/s            = total_output_tokens / Total wall (baseline and projected)
Avg TTFT / Avg ITL  = output-token-weighted mean(ttft_ms) / mean(itl_ms) across every stage that
                      actually decoded output (weighted so a 1000-token turn counts more than a
                      44-token warmup call - a plain per-stage average would misrepresent what a
                      user actually experienced across the session)
```

`device_type` (NPU / GPU-iGPU / CPU) is read straight from `experiment.json` and determines which
accelerator capability (`npu_capability`, `igpu_capability`, or `cpu_capability`) governs that run's
prefill scaling — matching which device actually ran the workload. An unrecognized `device_type`
raises rather than silently defaulting (an earlier version defaulted anything that wasn't `"NPU"`
to `igpu_capability`, which would have silently mis-scaled prefill for a hypothetical CPU-only run).

### 4.1 Measured baseline efficiency (diagnostic) & why the retention knobs are split per-domain

`hw_samples.csv` already contains REAL per-sample telemetry this tool wasn't using at all before
2026-09-24: `npu_pct`/`igpu_pct` (accelerator busy%) and `dram_total_gbs` (achieved DRAM
bandwidth). `baseline_extractor.py` now averages these over each stage's own active window
(`start_iso`→`end_iso`, i.e. while the accelerator is actually generating tokens - NOT diluted by
idle tool-exec gap time) and rolls them up into `BaselineProfile.measured_accel_busy_pct` /
`measured_mem_bw_gbs` (both `None` if the columns aren't present). These are **diagnostic only** -
shown in the report / what-if calculator to calibrate the retention knobs against reality, never
fed back into the projection math (a baseline machine's own achieved efficiency says nothing about
what a *different* target machine will achieve - that's still necessarily an assumption).

Measured on the 4 primary validated runs (default placeholder baseline spec,
`mem_bw_peak_gbs = 136.5 GB/s`):

| Run | device | measured accel busy% (active windows) | measured mem BW achieved | % of theoretical peak |
|---|---|---|---|---|
| preset5 (SWE Agent) | NPU | 91.8% | 76.6 GB/s | 56.1% |
| preset6 (SWE Agent) | GPU | 59.9% | 61.2 GB/s | 44.9% |
| preset7 (Data Agent) | NPU | 89.5% | 65.6 GB/s | 48.1% |
| preset8 (Data Agent) | GPU | 36.2% | 31.8 GB/s | 23.3% |

This is real, measured evidence that the NPU path and the iGPU path on the *same* class of SoC
achieve genuinely different fractions of theoretical peak (busy% and mem-BW-efficiency both run
meaningfully higher on NPU runs than GPU runs in this data) - a single shared `efficiency_retention`
knob was masking that difference by construction. Splitting it into
`compute_efficiency_retention` / `memory_efficiency_retention` / `cpu_efficiency_retention` lets a
user pick different, better-informed values per accelerator type instead of one blind guess for
everything. It does **not** mean these measured percentages should be plugged in directly as the
retention values for a *target* machine - see the limitations below - but they're a much better
starting point than an unexamined `0.85` for every domain on every run.

**On "contention due to parallelism" / USM path (per the second-round review question this section
answers):** this benchmark's timeline is fully **serial per the measured stages** - one stage
(prefill+decode on the accelerator) runs to completion, then the harness runs any tool call
(`tool_exec_gap_s`, CPU-only, accelerator idle), then the next stage starts. There is no point in
the current data where NPU/iGPU and CPU workloads execute *concurrently* and contend for the same
shared-memory (USM) bus at the same time - contention in that sense doesn't apply to this
single-agent, batch=1, request-response workload structure. The "contention" that Amdahl's law
already captures is a *different* thing: how much of a **single** CPU-bound phase (tool-exec) can
actually be sped up by adding more cores vs. being effectively single-threaded (`tool_parallel_fraction`).
If a future workload profile ever overlaps LLM inference and tool execution in time (e.g. streaming
generation while a background tool runs), true cross-resource memory-bus contention would need a
new macro-component this tool doesn't have yet - that's out of scope for the current serial-timeline
model, and is called out as limitation #12 below rather than silently assumed away.

### Power / energy (Tokens/Joule)

Only computed if at least one stage has measured RAPL power (`hw_samples.csv`'s
`rapl_cpu_w`/`rapl_igpu_w`/`rapl_npu_w`/`rapl_soc_w`, averaged per stage - see §2):

```
ratio(domain) = target.<domain>_capability / baseline.<domain>_capability     # cpu/igpu/npu
ratio("soc")  = 1.0   # uncore/SoC rail assumed roughly fixed regardless of core/EU/MAC count

projected_power_w[domain] = baseline_power_w[domain] × ratio(domain) ^ power_scaling_exponent
    # power_scaling_exponent: 1.0 = linear (dynamic power ∝ resource count × freq, the common
    # simplifying assumption at iso process-node). Default 1.0.

Total baseline energy_j  = Σ(stage)  Σ(domain, baseline_power_w[domain])  × stage.baseline_wall_s
Total projected energy_j = Σ(stage)  Σ(domain, projected_power_w[domain]) × stage.projected_wall_s

Tokens/Joule = total_output_tokens / Total energy_j   (baseline and projected)
```

### 4.2 Calibration cross-check (`calibration.py`, `tools/calibrate_roofline_baseline.py`)

A second, independent estimate of achieved-vs-theoretical efficiency, mined from "clean repeated
fixed-shape trial" runs (e.g. `kpi_mode=full` presets: many stages, similar input/output token
shapes, no tool-call gaps) instead of `hw_samples.csv`'s ~1Hz OS-level telemetry:

```
prefill_gflops_s      = 2 × params × input_tokens / prefill_s / 1e9      (per stage)
decode_achieved_gbs   = params × bytes_per_weight / itl_s / 1e9         (per stage - full weight
                                                                          re-read once per token)
best_*  = max(...) across all repeated trials in the run     (same "self-referential empirical
                                                                peak" idea as generate_kpi_report.py's
                                                                roofline chart, §7 doc reference)
```

This is diagnostic only (same caveat as §4.1 - never fed into the projection math), but it's a
**second, methodologically-different** measurement of the same "how efficient is this baseline"
question - derived from the model's own precise per-token timing across many repeated trials,
not an external sampler's coarse average. Run via:

```powershell
.venv\Scripts\python.exe tools\calibrate_roofline_baseline.py `
    --run kpi_runs\preset1_full_npu_20260917_211825 --run kpi_runs\preset2_full_gpu_20260917_213836
```

**Result on this repo's two `full`-mode calibration runs** (2026-09-25, default placeholder spec,
`mem_bw_peak_gbs=136.5 GB/s`):

| Run | device | best decode achieved (calibration) | telemetry achieved (§4.1) | delta |
|---|---|---|---|---|
| preset1_full_npu | NPU | 45.2 GB/s (33.1% of peak) | 69.1 GB/s (50.6% of peak) | **-34.5%** |
| preset2_full_gpu | GPU | 58.3 GB/s (42.7% of peak) | 55.9 GB/s (41.0% of peak) | **+4.2%** |

The GPU run's two independent methods agree closely (+4.2%). The NPU run's do **not**
(-34.5%) - the per-token-timing-derived calibration number is substantially lower than the
telemetry-averaged one. This was NOT expected to be a clean match and isn't fully explained yet:
plausible contributing factors include quantization-metadata bytes (scales/zero-points) not
counted by the `bytes_per_weight`-only formula above, the OS-level `dram_total_gbs` counter
including non-model DRAM traffic the model-only formula doesn't, or the NPU driver path having
different KV-cache/activation memory traffic characteristics than the simple "re-read weights
once per token" model assumes. Treat this as an open question the two-method cross-check exists
specifically to surface, not a bug to silently paper over - see limitation #14.

### 4.3 Purpose-built calibration presets (`tools/roofline_calibration/`)

§4.2's calibration mining works on *existing* runs, but those runs were designed for KPI
characterization, not calibration - their shapes/repetition counts aren't optimized for isolating
one macro-component cleanly. `tools/roofline_calibration/` is a **thin wrapper**, separate from
`tools/run_kpi_preset.py` (which owns the fixed production SWE/Data Agent presets), that
generates purpose-built presets - one per macro-component/knob:

| Preset | Equation isolated | Confidence | Mechanism |
|---|---|---|---|
| `prefill_sweep` | `compute_efficiency_retention` (prefill) | validated | Fixed short output, sweeping input context length (128/512/2048/8192 tokens) across 4 non-agentic stages |
| `thin_serving` | `stage_overhead_s`/`fixed_overhead_s` (assumed constant) | validated | Minimal round trips repeated many times, isolating fixed per-request software/IPC overhead |
| `kv_cache_growth` | `memory_efficiency_retention` (decode/ITL vs. KV-cache depth) | validated | Agentic multi-turn conversation where each scripted `agent` turn adds a KNOWN, roughly-equal token increment to history |
| `tool_exec_only` | `cpu_efficiency_retention` + `tool_parallel_fraction` (Amdahl) | validated | Agentic scenario prompting direct `tools_sandbox/` invocations with minimal reasoning in between |

**All 4 presets were run for real on NPU hardware on 2026-09-25** (Intel(R) NPU + iGPU, both
confirmed present via `Get-PnpDevice`, models already cached locally) - see results below. The two
non-agentic presets worked on the first try; the two agentic ones needed real fixes, documented
here so they aren't silently rediscovered:

1. **Agentic scenarios need `--python-path system` even when no tool ever executes** -
   `kv_cache_growth`'s first attempt exited 0 but parsed 0 stages (`Bundled Python not found` ->
   `Inferences preparation failed, skipping...` in the install's `Logs/error.log`). Fixed in
   `run_calibration.py`: auto-adds `--python-path system` for any `is_agentic` preset (same flag
   `run_kpi_preset.py` already passes for the production SWE/Data Agent presets 5-8).
2. **The scripted `prompts` array must have an ODD total count, ending on a trailing `agent`
   entry** - `llama3 ERROR - Invalid number of prompts for agentic inference. Must be at least 3
   and odd.` A draft version ended on a dangling final `user` turn (even count) to squeeze one
   extra measured stage out of `kv_cache_growth`; that broke the harness's `prompts[i+2]`
   task-pairing lookup entirely (collapsed the whole run to a single no-op task, <1s, 0 stages).
   Fixed: always end on `agent`, matching `swe-agent-prompts.json`'s exact shape (odd count).
3. **`"execute_command"` (SWE Agent's own *documented* tool name, per `swe_system.md` §5.4) is
   never actually dispatched by the real harness** - confirmed by trying it and getting the
   generic, detail-free `One or more tool calls failed` / `There is empty output in the result.`
   repeatedly, unchanged across several unrelated fixes (python path, asset path) that should have
   mattered if those were the real cause. None of the real committed `swe_agent_N.md` reply files
   invoke `execute_command` either (only `read`/`apply_patch`) - it's plausibly prompt-level
   documentation the model is taught to reference, that the C++ dispatch backend never
   implemented under that name. `"execute"` (Data Agent's name, e.g. `da_agent_0.md`) is the one
   name proven dispatched - switching to it turned the generic failure into a real, detailed
   `{"exit_code":2,"output":"...: No such file or directory"}` error (proof the tool itself fired
   correctly; the remaining problem was just the path, see next point).
4. **`tools_sandbox` assets stage under `data/<ScenarioName>/tools_sandbox/`** (relative to the
   mlperf install root, where `execute`'s `cwd` defaults to) - NOT flat, and NOT under
   `tools_sandbox/` alone. Confirmed by recursively searching the install directory for the actual
   staged file after the exit_code:2 error above. `Logs/results.json`'s own `"Assets File Names"`
   list (which shows flat names with no path prefix) is misleading here - it's just a name
   manifest, not the real staged path.
5. **No benefit to a machine-specific system Python path for the `execute` command** - the
   `tools_sandbox/*.py` scripts only use stdlib (`csv`/`argparse`/`pathlib`), so the project's own
   `.venv\Scripts\python.exe` (resolved dynamically relative to the repo root, portable to any
   machine that ran `setup_kpi_hub_env.ps1`) works identically to a hardcoded system install path
   and is used instead.

**Real results (NPU, `kpi_runs/*_npu_20260925_*`, analyzed via `--analyze`):**

- `prefill_sweep`: prefill throughput *rises* 128->2048 tokens (~18k->26k GFLOPs/s, pipeline
  warmup) then *drops sharply* at 8192 tokens (~12.5k GFLOPs/s) - real evidence prefill efficiency
  is not flat across context lengths the way a single-point calibration would assume. Decode also
  slows at long context (96->65 GB/s), suggesting KV-cache-depth affects decode bandwidth even
  though the current model treats `memory_efficiency_retention` as context-length-independent.
- `thin_serving`: telemetry-based mem-BW efficiency (7.3% of peak) was far below the per-token-
  timing-derived calibration number (71.0%) for these very short (~2-output-token) stages - direct
  confirmation of §4.2's "coarse ~1Hz sampler smooths over real peak/trough swings" caveat: the
  sampler barely catches any samples during a sub-second active window.
- `kv_cache_growth`: input tokens grew 40->129->817->1505 across turns (tracking the designed
  ~600-token-per-turn scripted `agent` reply size); decode throughput stayed roughly flat-to-
  slightly-varying (72-87 GB/s) across this modest range - consistent with `prefill_sweep`'s
  finding that decode degradation only becomes pronounced at much larger contexts (~8k tokens),
  not in the few-hundred-to-~1.5k range this run covered.
- `tool_exec_only`: real `tool_exec_gap_s` values of 5.01s/5.87s/6.15s/2.33s per stage (three real
  `tools_sandbox/*.py` invocations via the fixed `execute` tool_use path) - genuine CPU-bound
  tool-execution time, the actual signal `cpu_efficiency_retention`/`tool_parallel_fraction` are
  meant to model. Note `tool_calls` stays `{}` in `baseline_extractor.py`'s output for this run -
  that field scrapes the *model's own* generated text (`results.json`'s `Output` array), which is
  independent of the scripted `agent`-file tool dispatch that actually ran the script (see
  `baseline_extractor.py`'s ground-truth notes on this same distinction for the production
  SWE/Data Agent presets) - expected, not a bug.

**All 4 presets were also run on iGPU** (`kpi_runs/*_gpu_20260925_*`) to complete full 8/8
(4 presets × NPU/GPU) real-hardware coverage:

- `prefill_sweep` (GPU): achieved prefill GFLOPs/s shows the **same rise-then-drop shape** as the
  NPU run, independently — best-per-length roughly 65k (n≈169) → 53k (n≈514) → 41k (n≈1894) →
  19k (n≈7414), i.e. prefill efficiency degrades at long context on iGPU too, not just NPU. Decode
  stayed a roughly flat 108-118 GB/s (86.7% of peak best-observed) across the whole sweep.
- `thin_serving` (GPU): telemetry-based mem-BW efficiency (7.0% of peak) was, again, far below the
  per-token-timing-derived calibration number (85.6% of peak) for these very short stages — the
  same coarse-sampler-undercounts-short-stages effect as the NPU run (7.3% vs 71.0%), confirming
  it's a sampler-resolution artifact, not something specific to one accelerator.
- `kv_cache_growth` (GPU): decode achieved 65.4/113.0/112.6/101.2 GB/s (82.7% of theoretical peak
  best-observed) - notably higher %-of-peak than the NPU run (63.5%), consistent with §4.1's
  finding that NPU and iGPU achieve genuinely different fractions of theoretical memory bandwidth.
- `tool_exec_only` (GPU): `tool_exec_gap_s` = 5.01s/5.97s/6.21s/1.87s - **nearly identical to the
  NPU run's 5.01s/5.87s/6.15s/2.33s**, exactly as expected since tool execution is CPU-bound and
  independent of which accelerator ran the LLM inference. A strong, independent consistency check
  that both this preset and the underlying `tool_exec_gap_s` concept behave as physically expected.

> **These 8 runs' raw output (`kpi_runs/{prefill_sweep,thin_serving,kv_cache_growth,
> tool_exec_only}_{npu,gpu}_20260925_*/`) are committed to this repo as evidence** - `workflow_kpi.json`,
> `hw_samples.csv`, `experiment.json`, `kpi_report.html`, `dashboard.html` per run (`kpi_runs/` is
> gitignored by default; these 8 were force-added as a deliberate, one-time exception since they
> back the specific numbers quoted above). **They are from ONE specific development machine, NOT a
> universal reference baseline**: NPU = "Intel(R) AI Boost", iGPU = "Intel(R) Graphics [PF] GPU"
> (driver 32.0.101.8949), 16 logical CPU cores, ~63 GB RAM, Windows 11 Enterprise (the harness's
> own `SysInfo_CPUModel` reports a non-identifying placeholder, `"Genuine Intel(R) 0000"` - not a
> real model string, don't read anything into it). Re-running these presets on a *different*
> machine will produce *different* absolute numbers (different SoC, different achieved
> GFLOPs/s/GB/s) - only the *shapes of the curves* (prefill rising-then-dropping vs. context
> length, tool_exec_gap_s being NPU/GPU-independent, etc.) are expected to generalize, not the
> specific values quoted in this section. Every failed/intermediate debugging attempt from this
> session (`kv_cache_growth_npu_20260925_115819`/`_120159`, `tool_exec_only_npu_20260925_121800`
> through `_124535`, `thin_serving_npu_20260925_104241`) was deliberately left uncommitted/
> gitignored - only the final, successful run per preset×device is kept as evidence.

Usage:

```powershell
# Generate only (no hardware/network required) - writes to data/configs/kpi_presets/roofline_calibration/
# and data/prompts/llama_3_1_8b_instruct/roofline_calibration/
.venv\Scripts\python.exe tools\roofline_calibration\run_calibration.py --preset prefill_sweep --device NPU

# Generate AND run (requires a real installed mlperf_v2p0 + NPU/iGPU hardware + network access,
# plus - on an Intel corporate network - the proxy env vars: `. .\tools\set_proxy_env.ps1` first)
.venv\Scripts\python.exe tools\roofline_calibration\run_calibration.py --preset prefill_sweep --device NPU --run

# Analyze a completed run (reuses calibration.py + baseline_extractor.py from §4.2)
.venv\Scripts\python.exe tools\roofline_calibration\run_calibration.py --analyze kpi_runs\prefill_sweep_npu_<timestamp>
```

## 5. Step 4 — Outputs (`report.py`, `what_if_calculator.py`)

- **`roofline_projection_report.html` / `.json`** (`tools/run_roofline_projection.py`): a static
  report for one specific baseline-spec/target-spec/assumptions combination — summary cards,
  spec-comparison table, a stacked-bar chart (baseline vs. projected macro components per stage),
  and a per-stage speedup table. Visual style matches `tools/KPI-hub/generate_kpi_report.py`.
- **`what_if_calculator.html`** (`--what-if` flag): a **standalone, offline** HTML file with the
  baseline profile embedded as JSON and the exact same scaling math re-implemented in vanilla JS
  (`_JS_ENGINE` in `what_if_calculator.py` — kept in sync with `hw_spec.py`/`scaling_engine.py` by
  convention, no shared code since this must run with no server/build step). Dropdowns for CPU
  cores/frequency, iGPU XeCores/frequency, NPU MACs/frequency, and memory channels/width/transfer
  rate, plus sliders for **compute / memory / cpu efficiency retention** (split per-domain, see
  §4.1), tool-exec parallel fraction, and power scaling exponent, recompute the projected wall
  time, speedup, tokens/s, avg TTFT/ITL, and Tokens/Joule (when RAPL power data exists) live on
  every change; a "quick preset" dropdown loads any `SystemSpec` JSON from
  `data/configs/roofline_targets/`. If the baseline run has real busy%/mem-BW telemetry, a
  "Measured Baseline Efficiency" card shows it (diagnostic only, doesn't move any of the sliders).
  A "Generate a Persisted Report" section lets you take the current selection back to the CLI
  (copy a ready-to-run command with all three efficiency flags, or download a
  `--target-spec`-compatible JSON) since this page has no filesystem/server access to write a
  report itself.

  **Only one accelerator group is ever "hot" per run.** `computeCapability()` picks either
  `npuCapability` or `igpuCapability` based on the baseline run's own `device_type` (NPU vs
  GPU/iGPU) — never both. The calculator reflects this directly: whichever accelerator group
  doesn't match the loaded run's `device_type` is visually greyed out and its dropdowns disabled
  (labeled "not used - this run used NPU/GPU"), so it's never ambiguous which knobs actually move
  the numbers. This also means the tool is symmetric — running it against an NPU-baseline run
  (e.g. `preset5_roofline_*`) makes the NPU MACs/frequency dropdowns live and iGPU inert; running
  it against an iGPU-baseline run (e.g. `preset6_roofline_*`) flips that, making iGPU XeCores/
  frequency live and NPU inert. There is intentionally no cross-accelerator projection (e.g.
  projecting an NPU-measured run onto an iGPU-capability target) — MAC-count×frequency and
  XeCore-count×frequency are not calibrated to the same physical FLOPs-per-unit constant (see
  §8.2 below), so mixing them across accelerator types would produce numbers with no defensible
  basis. To get a real iGPU projection, generate the calculator from an actual iGPU baseline run.

## 6. Usage

```powershell
# One-shot report against an illustrative target preset (fills in a generic placeholder baseline)
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset5_roofline_20260923_140129 `
    --target-spec data\configs\roofline_targets\example_heavy_duty_workstation.json --what-if

# Recommended: fill in your real baseline spec once (data/configs/roofline_targets/current_baseline_TEMPLATE.json),
# then reuse it for every projection
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset5_roofline_20260923_140129 `
    --baseline-spec data\configs\roofline_targets\current_baseline_TEMPLATE.json `
    --target-spec data\configs\roofline_targets\example_heavy_duty_workstation.json `
    --efficiency-retention 0.8 --tool-parallel-fraction 0.4 --what-if

# Per-domain overrides (compute/memory/cpu efficiency retention can differ - see §4.1) - each
# --*-efficiency-retention flag overrides --efficiency-retention for just that one domain
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset5_roofline_20260923_140129 `
    --target-spec data\configs\roofline_targets\example_heavy_duty_workstation.json `
    --compute-efficiency-retention 0.9 --memory-efficiency-retention 0.75 --cpu-efficiency-retention 0.6 --what-if
```

Output defaults to `<run>/roofline_projection/` (report + `what_if_calculator.html` if `--what-if`
was passed). Individual target-spec fields can also be overridden directly on the CLI, e.g.
`--cpu-cores 24 --mem-freq-mts 9600`, without needing a JSON file.

## 7. Worked example (validated 2026-09-24)

Projecting `kpi_runs/preset5_roofline_20260923_140129/` (NPU, Llama-3.1-8B-Instruct, 12 real
inference stages across 3 SWE-Agent iterations) from the built-in default baseline spec (8 cores
@ 4 GHz / 96 XeCores @ 2 GHz / 2048 NPU MACs @ 1.4 GHz / 8ch×16-bit×8533 MT/s = 136.5 GB/s) onto
`example_heavy_duty_workstation.json` (32 cores @ 5 GHz / 384 XeCores @ 2.4 GHz / 8192 NPU MACs @
1.6 GHz / 8ch×64-bit×8800 MT/s = 563.2 GB/s — a ~4x jump in every resource) at default assumptions
(efficiency retention 0.85, tool-exec parallel fraction 0.5):

| | Baseline | Projected |
|---|---|---|
| Wall time | 492.5s | 168.0s |
| Speedup | — | **2.93x** |
| Wall time reduction | — | **65.9%** |
| Tokens/s | 11.2 | 32.7 |
| Tokens/Joule | 0.35 | 0.48 |

Per-resource effective speedups actually applied: compute (NPU) 4.04x, memory 3.66x, CPU/tool-exec
(Amdahl, p=0.5) **1.85x** — note none of these equal the raw 4x/4.13x/4x hardware ratios, because the
0.85 efficiency-retention damping and (for CPU) Amdahl's law both intentionally pull the realized
speedup below the ideal. Reproducible via both the CLI (`tools/run_roofline_projection.py`) and
the generated `what_if_calculator.html` (verified to produce identical numbers).

> **2026-09-24 correction**: this table now reflects a fixed `amdahl_speedup` (see §4 equation box)
> — an earlier version divided only the parallel term by `freq_ratio`, giving a 100%-serial tool-exec
> fraction zero benefit from clock-frequency increases. The fix raises the realized CPU/tool-exec
> speedup (1.57x → 1.85x here), which is why wall-time speedup/reduction/Tokens-per-s are all
> slightly higher than an earlier version of this doc reported. The `Avg TTFT`/`Avg ITL` rows were
> also dropped from this static table because their definition changed to an output-token-weighted
> mean (see §4) — use the CLI or the what-if calculator for current per-run TTFT/ITL numbers.

The same projection against the **iGPU** baseline (`kpi_runs/preset6_roofline_20260923_141035/`,
`device_type=GPU`, 689.8s baseline) onto the same target spec:

| | Baseline | Projected |
|---|---|---|
| Wall time | 689.8s | 212.6s |
| Speedup | — | **3.25x** |
| Wall time reduction | — | **69.2%** |
| Tokens/s | 13.3 | 43.1 |
| Tokens/Joule | 0.25 | 0.33 |

Here `compute_capability()` resolves to `igpu_capability` instead (this run's `device_type` is
`GPU`, not `NPU`) — verified in the what-if calculator: the NPU MACs/frequency dropdowns render
greyed out/disabled ("not used - this run used GPU") while iGPU XeCores/frequency are the live
ones.

Also validated (no code changes needed) against the Data Agent scenario, both device types:
`kpi_runs/preset7_dataagent_npu_20260923_220921/` (788.7s → 343.2s, 2.30x, 56.5% reduction,
Tokens/J 0.35→0.40) and `kpi_runs/preset8_dataagent_gpu_20260923_225621/` (910.2s → 288.6s, 3.15x,
68.3% reduction, Tokens/J 0.40→0.50) — confirming the pipeline generalizes across both agent
scenarios (SWE Agent / Data Agent) and both accelerator types without any scenario-specific code.

> **2026-09-24 correction**: Tokens/Joule numbers above also reflect the power-lookup-window
> widening fix (see §2) — an earlier version averaged RAPL power only over each stage's own
> active window then applied it across that stage's whole bucket (including its tool-exec gap),
> which overstated energy whenever the accelerator's gap-time power was lower than its
> active-generation power (confirmed on preset8: e.g. one stage measured 10.48W active vs 0.00W
> during its own trailing gap). Wall-time/speedup numbers are unaffected by this fix.

## 8. Known limitations / assumptions (be explicit about these when presenting results)

1. **Iso-efficiency projection, not a cycle-accurate simulator.** Compute and memory scaling
   assume the *utilization %* achieved on the baseline machine is achievable on the target machine
   too (subject to the `compute`/`memory_efficiency_retention` knobs) — real silicon can behave
   non-linearly (cache effects, NUMA topology changes, driver/firmware differences) in ways this
   model cannot capture. §4.1's measured busy%/mem-BW numbers describe the *baseline*, not what the
   *target* will achieve — extrapolating from one machine's measured efficiency to a different
   machine's future efficiency is still fundamentally an assumption, not a measurement.
2. **iGPU/NPU peak-capability ratios use count × frequency as a linear proxy**, not a verified
   FLOPs-per-MAC/FLOPs-per-XeCore constant (none exists for this exact silicon+workload — see
   `docs/KPI_HUB_INTEGRATION_NOTES.md`). This is fine for *ratios* between two specs using the
   same proxy, but the absolute "capability" numbers are not physical FLOPs/s.
3. **Fixed overhead and stage overhead are assumed constant** regardless of target hardware. In
   reality faster storage/single-thread performance could shrink model-load time somewhat — this
   model treats that as a conservative (i.e. the real system would do at least this well) simplification.
4. **Tool execution's Amdahl parallel fraction (default 0.5) is a modeling assumption**, not
   measured per-tool — `git apply`/`pytest`/file IO have different real parallelizability. Treat
   the CPU-scaling column as directional, and adjust `--tool-parallel-fraction` if you have better
   data for your actual tool mix.
5. **Power/energy projection (Tokens/Joule) requires RAPL power columns actually populated in
   `hw_samples.csv`** for the relevant domain (cpu/igpu/npu/soc) — silently omitted (shown as
   `N/A`) if `hw_samples.csv` is missing, pandas isn't installed, or every column reads zero for
   that run. Assumes power scales with the same capability ratio as compute (dynamic power ∝
   resource count × frequency, `power_scaling_exponent` default 1.0/linear) while the "soc"/uncore
   rail is assumed roughly fixed regardless of core/EU/MAC count. Verified working (non-null,
   physically plausible values) on all 4 validated runs once the lookup used naive `start_iso`/
   `end_iso` matching instead of an epoch/timezone-mismatched conversion (see §2) — if you see
   `N/A` on a run with `--power` known to have been enabled during collection, suspect this same
   class of alignment bug before assuming the hardware simply didn't report power for that domain.
   **Fixed 2026-09-24**: the averaging window itself was also widened to span each stage's whole
   bucket (through its tool-exec gap), not just its own active `start_iso`→`end_iso` — see §2.
6. ~~The trailing gap after a run's LAST stage falls into `fixed_overhead_s` (unscaled), not
   `tool_exec_gap_s` (Amdahl-scaled).~~ **Fixed 2026-09-24**: the last stage's `tool_exec_gap_s` is
   now computed against the workflow's `timeline.end_epoch`, not hardcoded to `0` — any real
   tool-call/verification time after the final LLM turn is now Amdahl-scaled like every other
   inter-stage gap, and `fixed_overhead_s` now represents only the gap *before* the first stage
   (process startup/model load). See `baseline_extractor.py::extract_baseline`.
7. ~~`compute_capability()` only distinguishes `NPU` from everything else.~~ **Fixed 2026-09-24**:
   `compute_capability()` now explicitly handles `NPU` / `GPU`&`IGPU` / `CPU` and raises a clear
   error on anything else, instead of silently routing an unrecognized `device_type` (e.g. a
   hypothetical `CPU` baseline) to `igpu_capability`. See `hw_spec.py::SystemSpec.compute_capability`.
8. **`cpu_efficiency_retention` and Amdahl's law compound multiplicatively for tool-exec.** The
   CPU/tool-exec speedup is Amdahl-damped *and then* further discounted by `cpu_efficiency_retention`
   — two independent "be conservative" knobs stack, so tool-exec ends up more heavily discounted
   for a given retention value than compute/memory would be for the same value. This is intentional
   (tool-exec involves OS scheduling/process-spawn overhead that compute/memory scaling doesn't),
   but keep it in mind when tuning both the CPU efficiency slider and the parallel-fraction slider
   in the what-if calculator — their effects are not independent.
9. **Tokens/Joule silently treats any stage with no RAPL-power match as 0 energy for that stage**,
   not as "unknown"/excluded. `has_power` only requires *one* stage in the whole run to have
   power data to turn Tokens/Joule on at all; any other stage whose lookup window happened to miss
   every CSV row (very short stage, sampler gap, etc.) contributes `0` watts × its wall time to the
   energy total instead of being excluded, which would silently bias Tokens/Joule optimistic (too
   high) for that run. Not observed in any of the 13 runs in this repo (every stage in every run
   has non-empty `avg_power_w`), but there's no coverage check or warning if it ever happens —
   treat a Tokens/Joule number as suspect if `hw_samples.csv`'s sampling interval is coarse relative
   to a run's shortest stage.
10. ~~`stage_overhead_s = max(wall_time_s - prefill_s - decode_s, 0)` could silently break the
    "sum of buckets == wall_time_s" invariant on noisy telemetry~~ **Fixed 2026-09-24**:
    `prefill_s`/`decode_s` are now rescaled proportionally (not just clamped) whenever their sum
    would exceed `wall_time_s`, so the invariant holds exactly for every stage, not just when the
    telemetry happens to agree (verified: `invariant_diff == 0.0` on all 13 runs in this repo,
    both before and after this fix — it was never actually triggered by current data, but is now
    guaranteed rather than assumed).
11. ~~`ttft_ms` was recomputed (`prefill_s*1000 + itl_ms`) instead of reading the log's own
    `ttft_s` field~~ **Fixed 2026-09-24**: now reads `ttft_s` directly when present (falling back
    to the recomputed value only if it's absent), so this stays "no new measurement, pure
    arithmetic on existing fields" even if a future log format ever changes how `ttft_s` itself is
    derived. Numerically identical to the old behavior on all 13 runs in this repo.
12. **No cross-resource contention modeling for concurrent accelerator+CPU use.** The current
    macro-component timeline is strictly serial (prefill+decode on the accelerator, THEN the
    harness runs any tool call with the accelerator idle, see §4.1) - there is no macro-component
    for "NPU/iGPU and CPU both active at once, contending for the same USM/DRAM bus," because that
    never happens in this benchmark's measured data. If a future workload profile overlaps
    generation and tool execution in time (e.g. streaming output while a background tool runs),
    this model would need a new contention-aware macro-component; today it's simply out of scope,
    not silently assumed away as zero-cost.
13. **Measured `accel_busy_pct`/`mem_bw_gbs` diagnostics (§4.1) depend on `hw_samples.csv` having
    `npu_pct`/`igpu_pct`/`dram_total_gbs` columns with real values** - both are `None` (silently
    omitted from the report) on 6 of the 13 runs in this repo whose `hw_samples.csv` predates this
    telemetry or lacks power/utilization sampling entirely. Absence of these diagnostics doesn't
    affect the wall-time/speedup projection at all (they're purely informational), but means you
    can't cross-check the retention knobs against measured reality for those older runs.
14. **The two independent efficiency-measurement methods (§4.1 telemetry vs §4.2 calibration)
    don't agree on the NPU run tested (-34.5% delta)**, only on the GPU run (+4.2%) - see §4.2.
    This is an open, unresolved discrepancy, not a bug that's been root-caused - a genuine
    uncertainty in how well either method characterizes the NPU path's true achieved bandwidth.
    Prefer the telemetry-based number (§4.1) as the primary diagnostic until this is resolved; use
    the calibration number (§4.2) as a sanity-check flag, not a replacement.
15. **`extract_baseline()` derives `prefill_ms_est`/`avg_itl_ms` from `ttft_s`/`wall_time_s`/
    `output_tokens` for log formats that predate those fields** (e.g. `kpi_mode=full` presets) -
    algebraically solving `ttft_s = prefill_s + itl_s` and `itl_s = (wall_time_s - ttft_s) /
    output_tokens` for the two unknowns. **Fixed 2026-09-25**: before this, any such run silently
    got `prefill_s = decode_s = 0` and its *entire* wall time was misclassified as fixed
    `stage_overhead_s` (never scales with target hardware) - this affected `preset1_full_npu` and
    `preset2_full_gpu` specifically. The derived values are algebraically exact given the same
    `ttft = prefill + itl` convention the newer log format itself uses, not an approximation.

## 9. Open refinement — the `prefill_s` bucket is currently 100% compute-scaled, not split (reference methodology + this repo's own supporting evidence)

> **Final Wall-Time(s) expression** (slide/headline form — see §9.5 for the fully-annotated
> version and the real executed numbers behind it):
>
> $$\text{WallTime}(s)=\sum_{i\in\text{stages}}\left[\underbrace{\frac{\text{Prefill}_i(n_i)}{R_{TTFT}(n_i)}}_{\text{Prefill / TTFT}}+\underbrace{\frac{\text{Decode}_i}{E_{mem}}}_{\text{Decode}}+\underbrace{\frac{\text{ToolExec}_i}{E_{cpu}}}_{\text{Tool Execution}}+\underbrace{\text{Overhead}_i}_{\text{Stage Overhead}}\right]+\underbrace{\text{Overhead}_{fixed}}_{\text{Startup / Model Load}}$$
>
> Plain-text form (paste into PowerPoint/Slides where LaTeX won't render):
> `WallTime(s) = Σᵢ [ Prefillᵢ(n) / R_TTFT(n)  +  Decodeᵢ / E_mem  +  ToolExecᵢ / E_cpu  +  Overheadᵢ ]  +  Overhead_fixed`
>
> | Term | Meaning | Scaled by |
> |---|---|---|
> | `Prefillᵢ(n) / R_TTFT(n)` | Prompt/context processing | External compute+memory-split TTFT ratio (§9.1) |
> | `Decodeᵢ / E_mem` | Autoregressive generation | Memory bandwidth ratio |
> | `ToolExecᵢ / E_cpu` | git apply / pytest / file IO | CPU cores×freq via Amdahl's law |
> | `Overheadᵢ` | Per-stage bookkeeping | Fixed, unscaled |
> | `Overhead_fixed` | Process startup/model load | Fixed, unscaled |
>
> Four terms, one per macro-component, each scaled by the resource ratio that actually governs it
> (§1's table) — summed per stage, then rolled up across the whole workflow. `R_TTFT(n)` is the
> external per-`n` compute+memory-split ratio (§9.1/§9.5); `E_mem`/`E_cpu` are this repo's own
> `effective_speedup`/`amdahl_speedup` terms (§4), calibrated from the real runs in §4.3/§9.6, and
> can optionally be seeded from each stage's own real EMON-measured achieved efficiency instead of
> a flat guess (`--use-measured-memory-efficiency`/`--use-measured-compute-efficiency`, §9.7).

§4's equation scales the **entire** `prefill_s` bucket by a single `compute_eff` ratio — i.e. it
implicitly assumes 100% of prefill/TTFT time is compute-bound (NPU-MAC/iGPU-XeCore-limited) and
0% is memory-bound. An external reference project (`llama31_8b_roofline_projection_formulas.md` /
`llama31_8b_roofline_pipeline_review.md`, a separate repo doing the analogous Llama-3.1-8B-Instruct
INT4 prefill/TTFT projection on a 96-EU Intel iGPU via raw OpenVINO `PERF_COUNT`, not this repo)
shows that assumption is only sometimes true — a real, physically-motivated **memory-bound**
component exists and can dominate at longer context lengths, and treating it as compute-bound
mis-projects the benefit of a hardware upgrade. This section records that methodology as a
reference/target model for this repo's own prefill bucket, plus concrete evidence this repo's
*own* calibration data (§4.3) already hints at the same effect. §9.4 explains why a *fully*
independent, this-repo-measured version of the split isn't possible with today's tooling; §9.5
then closes the gap a different way — borrowing the external project's own per-n ratio (not its
absolute numbers) and applying it to this repo's real measured data — and executes it end to end.

### 9.1 The reference TTFT(n) equation

That project decomposes per-input-length prefill time (`TTFT(n)`) into five real, separately-fitted
components instead of one lump sum, by running OpenVINO's own `PERF_COUNT` counters directly
(bypassing the higher-level `openvino_genai`/`mlperf-windows.exe` layers this repo depends on):

$$TTFT(n) = \underbrace{\left[T_{linear}(n) + \text{missing\_linear}(n)\right]}_{\text{corrected\_linear\_ms (compute-bound)}} + T_{attention}(n) + T_{fused\_ops}(n) + T_{other}(n) + \underbrace{\text{attention\_buffer}(n)}_{\text{memory-bound}}$$

- `T_linear(n)`, `T_fused_ops(n)`, `T_other(n)` are linear in `n` (fixed-K/N GEMMs, per-token
  elementwise ops) — compute-bound by construction, degree-1 polynomial fits.
- `T_attention(n)` is quadratic in `n` (the `[n,n]` score matrix) — degree-2 fit, required not
  assumed (degree-1 R²=0.917 vs degree-2 R²=0.999 on their real measured data).
- `missing_linear(n)` and `attention_buffer(n)` split an otherwise-unattributed `host_overhead`
  residual (`wall_ms − Σ(profiled kernel times)`) into a compute-bound piece (cross-checked
  against an *independent* raw INT4 GEMM ceiling microbenchmark on the same silicon) and a
  memory-bound remainder (hypothesized as the `[heads, n, n]` fp16 attention-score buffer never
  chunked/flash-attended) — never a guess, always bracketed against a real measured ceiling.
- Only `T_linear + missing_linear` (the compute-bound bracket) and `T_attention`/`T_fused_ops`
  scale with more accelerator compute (EU/XeCore count); only `attention_buffer(n)` scales with
  more memory bandwidth — mirroring exactly the compute/memory split this repo's `scaling_engine.py`
  already applies *between* `prefill_s` and `decode_s`, just not applied *within* `prefill_s` yet.

### 9.2 Reference projected-TTFT numbers (what a correct split changes)

Real measured baseline (96-EU iGPU, today's hardware) vs. a projection onto a hypothetical
256-EU/300-GB/s target, using the compute/memory-split equation above (`sut_projected_wall_ms`),
compared against what a naive **100%-compute-bound** assumption (this repo's current `prefill_s`
treatment) would have projected instead:

| n (input tokens) | baseline TTFT (ms) | correctly-split projection (ms) | speedup | naive 100%-compute-bound speedup* |
|---|---|---|---|---|
| 2048 | 2,773.18 | 1,806.75 | 1.54× | ~2.77× |
| 8192 | 12,287.39 | 8,073.81 | **1.52×** | **2.77×** (borrowed-profile estimate, since superseded) |
| 16384 | 40,055.04 | 19,260.67 | 2.08× | 3.72× |
| 32768 | 146,326.68 | 51,330.35 | 2.85× | 4.98× |

*The "naive" column is that same project's own **earlier, superseded** estimate from before it
calibrated real compute/attention axis fractions — included here specifically because it shows
the size of the error a 100%-compute-bound assumption produces: **treating a real memory-bound
component as if it were compute-bound overstated the hardware-upgrade benefit by roughly 2×** at
every `n` shown. This repo's current `prefill_s / compute_eff` (§4) makes structurally the same
100%-compute-bound assumption for every stage, at every `n` — so a similar overstatement of
projected speedup is plausible wherever a stage's prefill time has a real memory-bound share,
until this is split.

### 9.3 This repo's own evidence the same effect is present, not just a different project's finding

§4.3's real `prefill_sweep` calibration run (NPU, this repo's own hardware/model/harness) already
shows the same *symptom*, independently: achieved prefill GFLOPs/s **rises 128→2048 tokens
(~18k→26k) then drops sharply at 8192 tokens (~12.5k)** — i.e. the workload gets *less* compute-
efficient at longer context, exactly what you'd expect if a growing, non-compute-scaling
(memory-bound) time component is eating an increasing share of `prefill_s` as `n` grows, same
shape as the reference project's `attention_buffer(n)` (zero at their measured `n=8192`, but
already documented there to turn sharply positive and grow quadratically for any `n` beyond that
model's own real hard-fail boundary). This repo's own agentic presets (§ notes in
`docs/KPI_HUB_INTEGRATION_NOTES.md` — "8198→8979→10251 input-token growth across a round") already
operate right at and beyond that inflection point, so this isn't a purely theoretical concern for
long-context stages in this repo's real SWE/Data Agent runs.

### 9.4 Why this is not implemented yet (the honest gap)

Replicating §9.1's split for *this repo's* own model/hardware would require the same evidence
chain the reference project used: (1) a static GEMM census of this repo's exact quantized model
graph, (2) a raw `PERF_COUNT` sweep across `n`, separating linear/attention/fused/overhead kernel
time, and (3) an independent GEMM-ceiling microbenchmark on the same silicon to bracket the
compute-bound share. **None of these are available through this repo's current tooling**:
`mlperf-windows.exe` is a closed-source CLI that only surfaces aggregate `TTFT`/`wall_ms`/
`Tokens Per Second` per stage (see `docs/KPI_HUB_INTEGRATION_NOTES.md`'s ground-truth trace of
`txt2txt_executor.cpp`) — there is no `PERF_COUNT`-level or raw `ov.Core` access at this layer to
attribute a sub-kernel breakdown, and no independent GEMM-ceiling sweep exists for this repo's
NPU/iGPU today. Fabricating a compute/memory split **from this repo's own data alone** without
that evidence would violate this project's own "no silent guesses" standard (§8's limitations are
written the same way) — which is why §9.5 below deliberately borrows the external project's own
ratio instead of inventing this repo's own absolute compute/memory split. Recorded here as an
**open action item for a fully independent version**: if a future harness change ever exposes raw
OpenVINO `PERF_COUNT` (or an equivalent counter) for this repo's benchmark path, revisit
`scaling_engine.py` to split `prefill_s` natively using §9.1's equation as the target shape and
§4.3's `prefill_sweep` preset (already built) as the natural place to collect the calibration
sweep — replacing §9.5's cross-repo-borrowed ratio with a properly first-party-measured one.

### 9.5 The full Wall-Time(s) equation, with §9.1's TTFT split plugged in (executed, real run)

Rather than wait on §9.4's data gap, `tools/roofline_projection/integrate_external_ttft.py` closes
it a different way: instead of transplanting the external project's *absolute* millisecond values
(measured on different silicon/quantization, which would be indefensible), it reads off that
project's own **per-n baseline-vs-projected speedup ratio** — `ttft_ratio(n) =
wall_ms_total(n) / sut_projected_wall_ms(n)`, log-log-interpolated between its measured `n` grid
points — and applies that ratio as a multiplier to *this repo's own* measured `prefill_s` for the
stage whose `input_tokens` is closest to that `n`. This is the same "only ratios ever cross a
baseline/target boundary" principle `hw_spec.py` already uses for every other bucket (§3) — it's
just sourcing the compute/memory-split ratio for the prefill bucket from an external, more
rigorously-decomposed study instead of this repo's own (currently 100%-compute-bound) `SystemSpec`
ratio. `decode_s`/`tool_exec_gap_s`/`stage_overhead_s` are projected exactly as `scaling_engine.py`
already does (§4), onto a target `SystemSpec` built from this run's own CLI args.

**Guard, added 2026-09-29**: the external CSV's `ttft_ratio(n)` was derived on a real iGPU
(EU-count compute axis) — applying it to an NPU-baseline run would mix two physically different
compute axes with no defensible basis, the same principle behind §5's "no cross-accelerator
projection" rule for the what-if calculator. `integrate_external_ttft.py` now refuses to run
against a non-GPU/iGPU `--run` (`error: --run's device_type is 'NPU', ... has no defensible
basis`, exit code 1) unless `--force` is passed, in which case the report is flagged
(`device_type_axis_mismatch: true` in the JSON) rather than silently producing a number that looks
just as confident as the iGPU case. Verified: blocks `preset5_roofline_20260923_140129` (NPU) with
this exact message; `preset6`/`preset8` (both GPU) are unaffected (same numbers as before).

**The full boxed equation:**

$$\text{WallTime}(s) = \sum_{i \in \text{stages}} \left[ \underbrace{\frac{\text{prefill}_s[i]}{\text{ttft\_ratio}(n_i)}}_{\text{TTFT}(n)\text{ compute+memory split (external, §9.1)}} + \underbrace{\frac{\text{decode}_s[i]}{E_m}}_{\text{memory-bound}} + \underbrace{\frac{\text{tool\_exec\_gap}_s[i]}{E_{cpu}}}_{\text{CPU-bound (Amdahl)}} + \underbrace{\text{stage\_overhead}_s[i]}_{\text{fixed}} \right] + \text{fixed\_overhead}_s$$

`E_m` and `E_cpu` are exactly §4's `effective_speedup(...)`/`amdahl_speedup(...)` terms (memory
bandwidth ratio, and Amdahl-damped CPU-core-count/frequency ratio respectively) — only the
prefill term's scaling source changes here, from a flat `compute_eff` ratio to the external
per-n curve.

**Executed** (2026-09-29) against `kpi_runs/preset6_roofline_20260923_141035` (real iGPU SWE-Agent
run, `device_type=GPU`, matching the external CSV's own iGPU-EU axis) onto the exact target the
user specified — **256 EU, 300 GB/s memory bandwidth, 20 P-cores + 48 E-cores (68 total; this
repo's Amdahl model doesn't distinguish P/E core types, so they're summed into one `cpu_cores`
count, same limitation as elsewhere in this doc)**:

```powershell
.venv\Scripts\python.exe tools\roofline_projection\integrate_external_ttft.py `
    --run kpi_runs\preset6_roofline_20260923_141035 `
    --igpu-xecores 256 --mem-bw-gbs 300 --p-cores 20 --e-cores 48
```

| stage | n (input tokens) | ttft_ratio(n) applied | baseline (s) | projected (s) |
|---|---|---|---|---|
| 01_warmup | 40 | 1.58× | 7.09 | 4.04 |
| 02_swe_agent_0 | 8,198 | 1.52× | 94.55 | 55.35 |
| 03_swe_agent_1 | 8,979 | 1.55× | 59.33 | 31.79 |
| 04_swe_agent_2 | 10,251 | 1.64× | 67.62 | 36.46 |
| *(iterations 2 and 3 repeat the same pattern - 12 stages total)* | | | | |
| **TOTAL (+ fixed_overhead_s)** | | | **689.77** | **391.21** |

**Wall-Time(s): 689.77s → 391.21s, 1.76× speedup, 43.3% wall-time reduction, 13.3 → 23.4 tok/s.**

**Apples-to-apples check against the naive (100%-compute-bound, §4/§9.2) projection**, re-run with
the exact same target spec (`tools/run_roofline_projection.py --igpu-xecores 256 --cpu-cores 68
--mem-channels 8 --mem-width-bits 16 --mem-freq-mts 18750` — same channel/width topology as the
default baseline, transfer-rate solved to hit 300 GB/s — `--efficiency-retention 0.85
--tool-parallel-fraction 0.5`, identical to the defaults above):

| Methodology | Projected wall time | Speedup | Reduction |
|---|---|---|---|
| Naive 100%-compute-bound prefill (§4, existing `scaling_engine.py`) | 339.15s | 2.03× | 50.8% |
| §9.1 TTFT compute/memory split (this section) | 391.21s | **1.76×** | **43.3%** |

Confirms §9.2's prediction directly, on this repo's own real data: treating the entire prefill
bucket as compute-bound **overstates** the projected benefit of this exact hardware upgrade by
about +0.27x speedup (2.03x vs the more physically-grounded 1.76x) — a real, non-trivial gap, not
just a theoretical concern from a different project's numbers.

**Caveats specific to this integration** (in addition to §9.4's underlying data-gap caveat):
this repo's own quantization (`Llama-3.1-8B-Instruct_ov-int4-GRw`) and silicon differ from the
external CSV's source machine, so `ttft_ratio(n)` is borrowed cross-repo, not independently
verified on this repo's own hardware — treat the 1.76x figure as **more physically defensible
than the naive 2.03x**, not as a fully independently-measured number. The external CSV's rows
at `n≥9,216` are themselves flagged by that project as past its own SUT's real hard-fail boundary
(formula-extrapolated) — this run's `n=10,251` stage's `1.64×` ratio falls in that extrapolated
region and should be read with correspondingly lower confidence than the `n=8,198`/`8,979` rows.

**Update, 2026-09-29 — this caveat was too optimistic, see §9.6b**: a real ground-truth run on
`JF04WVAW0381-TA` (a real, different-EU-count iGPU) showed `ttft_ratio(n)` predicting the *wrong
direction* for that machine's real prefill behavior under `mlperf-windows.exe`/`openvino_genai`
(predicted ~2.14× slower at n≈8192; real measurement was ~3.3× *faster*). This doesn't necessarily
mean the 256-EU/300-GB/s numbers above are equally wrong (a different, larger EU-count jump on
possibly-similar driver/software vintage may behave differently), but it means "more physically
defensible than the naive method" can no longer be asserted as a general property of this
integration — it should be read as "a different, not-yet-validated set of assumptions," not as a
confirmed improvement, until §9.6b's open question is root-caused.

**Generalization check — Data Agent scenario, same target spec** (matching §7's own convention of
validating every feature on both SWE Agent and Data Agent, not just one): run against
`kpi_runs/preset8_dataagent_gpu_20260923_225621/` (`device_type=GPU`, input tokens 4,513-11,451
across its 4 turns/round, all within the external CSV's `n=4,096`-`n=32,768` interpolation range,
no extrapolation-flagged rows this time):

```powershell
# Exact demo one-liner (includes the PYTHONHOME gotcha fix - see docs/KPI_HUB_INTEGRATION_NOTES.md)
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue; .venv\Scripts\python.exe tools\roofline_projection\integrate_external_ttft.py --run "kpi_runs\preset8_dataagent_gpu_20260923_225621" --igpu-xecores 256 --mem-bw-gbs 300 --p-cores 20 --e-cores 48
```

**910.23s → 504.73s, 1.80× speedup, 44.5% wall-time reduction, 6.9 → 12.4 tok/s** — consistent with
preset6's 1.76×/43.3% (same target spec, different scenario), confirming the integration
generalizes across scenarios the same way every other feature in this doc already does (§7).
Tool-exec time here is a noticeably larger share of the total (16.3%→17.6%) than preset6's
(11.7%→12.4%), reflecting Data Agent's heavier `execute` tool usage relative to SWE Agent's
lighter `read_file`/`apply_patch` mix.

### 9.6a Real second-SUT cross-check (2026-09-29) — prefill/TTFT only, not the full equation

A genuinely different, useful validation question: instead of projecting onto a *hypothetical*
256-EU/300-GB/s target, cross-check against a **real second machine** — the external project's
`JF04WVAW0381-TA` (Nova Lake client, onboarded 2026-09-28): a real **16-EU** iGPU (confirmed via
OpenVINO's `gpu_execution_units_count` device query, not a guess), **96 GB DDR5-6400** (2×48 GB),
**28 total CPU cores** (12 P-cores + 16 E-cores — this repo's Amdahl model sums them into one
`cpu_cores` count, same limitation noted elsewhere in this doc).

**What real data actually exists there**: a raw OpenVINO `PERF_COUNT` prefill/TTFT sweep
(`llama31_8b_perfcount_sweep_gpu.csv`, real measured `wall_ms(n)` at n=64…8192, matching the
96-EU reference SUT's own measured `n` grid exactly) plus a GEMM census and microbenchmark suite.
**No full agentic mlperf SWE-Agent/Data-Agent run has been executed there** — so `decode_s`/
`tool_exec_gap_s`/`stage_overhead_s` (the other three-quarters of the Wall-Time(s) equation)
cannot yet be cross-checked against real ground truth on this machine, only the prefill/TTFT
bucket can.

**Real-vs-real prefill/TTFT speedup** (16-EU actual `wall_ms(n)` vs. 96-EU actual `wall_ms_total(n)`,
both real measurements, no fitting or extrapolation):

| n | 16-EU actual (ms) | 96-EU actual (ms) | Real measured speedup (16→96 EU) |
|---|---|---|---|
| 64 | 189.24 | 91.40 | 2.07× |
| 128 | 253.44 | 172.06 | 1.47× |
| 256 | 465.84 | 341.05 | 1.37× |
| 512 | 846.44 | 680.71 | 1.24× |
| 1024 | 1,733.08 | 1,370.13 | 1.26× |
| 2048 | 3,830.21 | 2,773.18 | 1.38× |
| 4096 | 9,466.36 | 5,682.97 | 1.67× |
| 8192 | 26,309.71 | 12,287.39 | 2.14× |

Notably **non-monotonic** (dips to ~1.24× around n=512-1024, climbs back to >2× at both extremes)
— a genuinely real, unexplained shape, not a modeling artifact, and a concrete demonstration of
why a single flat `compute_efficiency_retention` ratio (§4) can't be right across all context
lengths. **Important caveat on how to read this**: this exact 16-EU/96-EU pair is the SAME pair
the external project's own `llama31_8b_compute`/`llama31_8b_attention` axis fractions (§9.1) were
calibrated FROM — so this is a self-consistency check on real data, not independent held-out
validation of that calibration (comparing a fitted model against its own training pair will
always look good). It's still useful: it confirms the real numbers behind that calibration
directly, in this repo's own docs, rather than trusting the external project's summary numbers
un-inspected.

**To close the loop for real** (validate the *whole* Wall-Time(s) equation, not just prefill),
this repo would need someone to physically run `tools\run_kpi_preset.py --preset 6` (or a
`roofline_calibration` preset) on `JF04WVAW0381-TA` itself and bring back its `kpi_runs/`
directory — genuine ground truth for `decode_s`/`tool_exec_gap_s`/`stage_overhead_s` at this
exact real spec. `data/configs/roofline_targets/JF04WVAW0381-TA_16EU.json` (16 EU, 28 cores
12P+16E, 102.4 GB/s DDR5-6400 theoretical) has been added to this repo for exactly this.

**Two advance PREDICTIONS generated now, before any real full-workflow run exists on that
machine** (deliberately predict-then-verify, not fit-then-compare) — both project
`preset6_roofline_20260923_141035` (this repo's real 96-EU baseline, 689.77s) onto that exact spec:

```powershell
# (1) Naive/existing method - prefill 100%-compute-scaled (scaling_engine.py, §4)
.venv\Scripts\python.exe tools\run_roofline_projection.py --run kpi_runs\preset6_roofline_20260923_141035 `
    --target-spec data\configs\roofline_targets\JF04WVAW0381-TA_16EU.json --efficiency-retention 0.85 --tool-parallel-fraction 0.5

# (2) Corrected method - prefill uses this machine's own REAL measured TTFT ratio (§9.6a's table,
# saved as data\configs\roofline_targets\JF04WVAW0381-TA_real_ttft_ratio.csv) via integrate_external_ttft.py
.venv\Scripts\python.exe tools\roofline_projection\integrate_external_ttft.py --run kpi_runs\preset6_roofline_20260923_141035 `
    --external-ttft-csv data\configs\roofline_targets\JF04WVAW0381-TA_real_ttft_ratio.csv `
    --igpu-xecores 16 --mem-bw-gbs 102.4 --p-cores 12 --e-cores 16
```

| Method | Predicted wall time | Speedup | Reduction |
|---|---|---|---|
| Naive (100%-compute-bound prefill) | 1,320.13s | 0.52× (≈1.91× *slower*) | −91.4% |
| Corrected (real per-n TTFT ratio) | 1,020.16s | 0.68× (≈1.48× *slower*) | −47.9% |

Both correctly predict this weaker machine runs the SWE-Agent workflow *slower* (both a 16-EU
iGPU and a 102.4 GB/s theoretical mem BW are below this repo's default 96-EU/136.5-GB/s baseline
placeholder) — but they disagree by **~300s / ~45%** on *how much* slower, a real, falsifiable
difference between the two methodologies that a genuine ground-truth run on `JF04WVAW0381-TA`
would settle. Note the memory-domain speedup here is `0.788×` (i.e. `decode_s` gets *slower*, not
faster) — this target's 102.4 GB/s theoretical DDR5-6400 dual-channel figure is genuinely below
the 136.5 GB/s baseline placeholder, unlike the 256-EU/300-GB/s target used everywhere else in §9.
**Update, 2026-09-29 — checked against reality, see §9.6b**: a real run on `JF04WVAW0381-TA`
became available the same day. Both predictions were wrong, in the same direction (both far too
pessimistic) — the real measured wall time (778.62s) is much closer to the original 96-EU run
(689.77s) than either prediction above, because prefill actually got *faster*, not slower, on the
real 16-EU machine. Full breakdown in §9.6b.

### 9.6b REAL ground-truth result from `JF04WVAW0381-TA` (2026-09-29) — both prediction methods were wrong, in the same direction

**Correction to how this section was first written**: `kpi_runs/preset6_sweagent_gpu_20260929_211533`
was initially logged here as "a second independent real baseline run on this repo's own 96-EU
machine" used only for a noise/reproducibility check. That was wrong. Cross-checking the run's own
raw telemetry against `JF04WVAW0381-TA`'s documented hardware (§9.6a) confirms **this run actually
executed on `JF04WVAW0381-TA` itself** — the real second SUT, not a second run of the original
96-EU box:

- `hw_samples.csv`'s `cpu_cores_csv` column has **28** per-core entries (this repo's own dev box has
  16 logical cores; `JF04WVAW0381-TA` is documented as 28 cores/28 threads).
- `nvidia_mem_used_mb` shows **12,227 MiB** — the exact figure `JF04WVAW0381-TA`'s own onboarding
  doc records for its RTX 5070 (`nvidia-smi`-reported).
- `mlperf_stdout.log` explicitly logs `"device_name":"Intel(R) Graphics (iGPU)","device_type":"GPU"`
  — confirming the real Intel iGPU ran the workload (not the NVIDIA card, which OpenVINO's `GPU`
  plugin can't target anyway).

**This means §9.6a's two advance predictions can now be checked against real measured ground
truth** — the actual point of this whole exercise:

| Method | Predicted wall time | Real measured wall time | Error (predicted vs. real) |
|---|---|---|---|
| Naive (100%-compute-bound prefill) | 1,320.13s | **778.62s** | **+69.6% too pessimistic** |
| Corrected (real per-n TTFT ratio, §9.1) | 1,020.16s | **778.62s** | **+31.0% too pessimistic** |

**Both methods predicted this machine would run noticeably slower — it barely did (+12.9% vs. the
original 96-EU run's 689.77s), and for a completely different reason than either model assumed.**
Breaking down the same stage (`n=8198`) directly, real vs. real:

| | Original 96-EU run | `JF04WVAW0381-TA` (16-EU) run | Change |
|---|---|---|---|
| `ttft_s` (prefill) | 49.4466s | **14.9501s** | **3.3× *faster*** — opposite of both predictions |
| `avg_itl_ms` (per-token decode) | 40.13ms | 77.289ms | ~1.9× *slower* — direction matches theory |
| Stage wall time | 89.541s | 87.529s | roughly flat |

Decode got slower, as the memory-bandwidth theory predicts (though a 1.9× slowdown from a
theoretical 136.5→102.4 GB/s drop, only 1.33×, implies real achieved efficiency also differs
between the two machines - not modeled). **Prefill got dramatically *faster*, not slower** — the
opposite of what both §9.6a predictions assumed, and opposite of the real 16-EU-vs-96-EU
`PERF_COUNT` ratio table in §9.6a itself (which showed 16-EU should be ~2.14× *slower* at n≈8192).
This is the clearest evidence yet that §9.1/§9.5's borrowed `ttft_ratio(n)` — measured via raw
`ov.Core` `PERF_COUNT`, bypassing `openvino_genai` — does **not** reliably transfer to real
`mlperf-windows.exe`/`openvino_genai`-measured `ttft_s`: the two measurement pipelines evidently
don't scale with EU count the same way (plausibly a much more favorable OpenVINO GPU-plugin
version/driver on `JF04WVAW0381-TA`, dwarfing the raw EU-count effect this whole external
methodology was built on). **The corrected method's smaller error (31.0% vs 69.6%) is real and
directionally useful, but it is not itself validated** — it happened to be less wrong mostly
because a "smaller assumed slowdown" is closer to "barely any slowdown" than "assume 100% of the
gap is compute-bound" is, not because its underlying ratio was confirmed correct.

**The previous "cross-run reproducibility"/"benchmark noise" framing and its interpretation below
are retracted** — they compared two *different physical machines*' real behavior against each
other while incorrectly assuming both were the same 96-EU box, so "12.9% run-to-run noise" and the
"2.1% vs 15.6% spread reflects measurement noise" conclusion do not hold. The raw numbers from that
analysis are kept below (they're real, correctly computed arithmetic) but must be read as **naive
vs. corrected agreement on two different real machines**, not noise-sensitivity on one.

```powershell
# Recheck of the naive method (§9.6a method 1), on the ORIGINAL 2026-09-23 run, to confirm reproducibility
.venv\Scripts\python.exe tools\run_roofline_projection.py --run kpi_runs\preset6_roofline_20260923_141035 `
    --baseline-spec data\configs\roofline_targets\current_baseline_TEMPLATE.json `
    --target-spec data\configs\roofline_targets\JF04WVAW0381-TA_16EU.json `
    --out kpi_runs\preset6_roofline_20260923_141035\roofline_projection_16EU_naive_recheck

# Recheck of the corrected method (§9.6a method 2), same original run
.venv\Scripts\python.exe tools\roofline_projection\integrate_external_ttft.py --run kpi_runs\preset6_roofline_20260923_141035 `
    --baseline-spec data\configs\roofline_targets\current_baseline_TEMPLATE.json `
    --external-ttft-csv data\configs\roofline_targets\JF04WVAW0381-TA_real_ttft_ratio.csv `
    --igpu-xecores 16 --mem-bw-gbs 102.4 --p-cores 12 --e-cores 16 --cpu-freq-ghz 3.3 `
    --out kpi_runs\preset6_roofline_20260923_141035\roofline_projection_16EU_corrected_recheck.json
```

| Baseline run used for the prediction | Naive prediction | Corrected prediction | Naive − Corrected gap* |
|---|---|---|---|
| `preset6_roofline_20260923_141035` (this repo's 96-EU dev box, 2026-09-23) | 1,320.13s | 1,030.42s | 28.1% |
| `preset6_sweagent_gpu_20260929_211533` — **this is the real `JF04WVAW0381-TA` run itself, its own numbers projected back onto its own spec, not an independent prediction** | 1,114.53s | 1,008.84s | 10.5% |

\* Kept for the record, but this comparison is **not a validity check** — the second row projects
`JF04WVAW0381-TA`'s own real run onto its own real spec, which is a circular exercise, not a fresh
prediction. The only real prediction-vs-ground-truth comparison is the first table in this section.

**Files referenced** (all committed under `kpi_runs/`):
- `kpi_runs/preset6_sweagent_gpu_20260929_211533/` — the real `JF04WVAW0381-TA` run (`workflow_kpi.json`, `hw_samples.csv`, `experiment.json`, `mlperf_stdout.log`, `dashboard.html`, `kpi_report.html`)
- `kpi_runs/preset6_roofline_20260923_141035/roofline_projection_16EU_naive_recheck/roofline_projection_report.json`
- `kpi_runs/preset6_roofline_20260923_141035/roofline_projection_16EU_corrected_recheck.json`
- `kpi_runs/preset6_sweagent_gpu_20260929_211533/roofline_projection_16EU_sameMem/roofline_projection_report.json` —
  a control run (naive method, target = baseline spec with only `igpu_xecores` overridden 16→96,
  memory/CPU/NPU held identical) isolating the EU-count-only effect: with memory held constant,
  cutting EUs 6× (96→16) only degrades wall time 778.62s→977.93s (+25.6%) in the naive model — most
  of this decode-dominated agentic run's wall time doesn't scale with iGPU EU count at all. This
  control was designed to isolate a variable inside the *naive model itself*; it does not explain
  the real result above, since the real result shows prefill got faster, not slower, which no
  variant of the naive model (EU-only or EU+memory) predicts.

**Open question — now with a strong, quantified candidate root cause (2026-09-29)**: why does
`JF04WVAW0381-TA`'s real 16-EU iGPU prefill 3.3× *faster* than this repo's 96-EU reference on the
exact same input length, under the exact same `mlperf-windows.exe`/`openvino_genai` pipeline, when
the companion project's own raw `PERF_COUNT` sweep on the same two chips shows the opposite
direction? Comparing both runs' real EMON hardware counters (`hw_samples.csv`, `bw_source=emon` on
both — genuine IMC uncore counters, not the coarse ~1 Hz PDH sampler) points at **DRAM read
latency and page-hit rate, not EU count, driver version, or raw bandwidth**:

| Metric (during each run's own `n=8198` prefill stages, all 3 iterations) | `JF04WVAW0381-TA` (16-EU) | This repo's 96-EU box |
|---|---|---|
| `dram_rd_latency_ns` (mean/median across 3 stages) | ~21-21.5 / ~22.1-22.4 ns | ~48-51 / ~44-46 ns |
| `dram_page_hit_rate_rd` | 0.940-0.950 | 0.611-0.672 |
| `dram_total_gbs` achieved | 46-52 GB/s | 48-54 GB/s (similar!) |

**The 16-EU machine's DRAM subsystem has dramatically better row-buffer locality (94-95% page-hit
rate vs 61-67%) and correspondingly ~2.2× lower real read latency (~22ns vs ~45-51ns), while
achieving essentially the *same* raw bandwidth during prefill** — this is a latency-bound, not
bandwidth-bound, difference. This matters specifically for prefill because §9.1's own
`attention_buffer`/`host_overhead` hypothesis is exactly this kind of scattered, less-sequential
memory-access pattern (KV-cache population, attention-score-buffer writes) — a workload phase far
more sensitive to per-access latency than to peak sequential bandwidth. The same pattern holds
during decode too (`03_swe_agent_1`, `07_swe_agent_1`, `11_swe_agent_1`): the 96-EU box's page-hit
rate degrades *further* under decode's heavier, more scattered access pattern (55-58%, latency up
to ~63-68ns, bandwidth jumping to ~85-87 GB/s) while the 16-EU machine stays flat (~95%, ~22ns)
across both phases — this looks like a systemic property of this repo's own dev box's memory
subsystem/configuration, not a one-off artifact of one stage or one run.

**This is a strong, quantified, testable correlation — not yet proven causal.** The ~2.2×
latency/page-hit gap doesn't fully explain the observed ~3.3× TTFT gap on its own (pipelined GPU
stalls can compound non-linearly, so this is plausible, not automatically the whole story), and no
single-variable controlled experiment (same machine, only the memory config changed) has been run
to confirm causation vs. correlation. It **is** a far more concrete, evidence-backed candidate than
the earlier driver-version guess this section previously led with (kept below for completeness,
now demoted to a secondary candidate). Until this is confirmed causal, **§9.5's `ttft_ratio(n)`
borrowing should be treated as unvalidated for cross-machine prefill projection**, not merely
"more physically defensible than the naive method" as earlier language in this doc claimed — that
claim is now directly contradicted by real ground truth.

**Secondary candidate, not yet ruled out**: a materially newer OpenVINO GPU-plugin/driver version
on `JF04WVAW0381-TA` (see its `.md` onboarding doc — driver 32.0.101.8907 vs the reference box's
32.0.101.8949, close but not identical; worth checking `openvino_genai`'s own version too)
dominating over raw EU count for this specific kernel path; or `openvino_genai`'s prefill
implementation using a fundamentally different, non-`PERF_COUNT`-comparable code path (e.g.
different batching/kernel-fusion strategy) than the raw `ov.Core` sweep this whole external-ratio
methodology (§9.1/§9.5) is built on. The DRAM-latency finding above doesn't rule these out either
— a genuinely rigorous root-cause would isolate memory config, driver version, and OpenVINO
version as independent variables, which no run so far has done.

**Why the page-hit rate/latency differs — a grounded (not guessed) architectural hypothesis**:
this repo's own docs already record *why* these two machines' memory subsystems are physically
different kinds of memory, not just different speed grades. `docs/KPI_HUB_INTEGRATION_NOTES.md`
documents this dev box's real memory (via `Get-CimInstance Win32_PhysicalMemory`) as **8 channels
× 16-bit × 8533 MT/s** — a many-narrow-channel layout characteristic of on-package LPDDR5X (typical
of a mobile SoC). `JF04WVAW0381-TA`'s onboarding doc records **96 GB DDR5, 2×48 GB SK Hynix DIMMs,
6400 MT/s** — a conventional 2-DIMM desktop/laptop DDR5 layout. These are two genuinely different
memory *architectures* (many narrow interleaved channels vs. few wide channels), not just a clock-
speed difference. It is a well-known, physically-motivated property of memory systems that
many-narrow-channel designs (smaller row buffer per channel, finer address interleaving to
maximize peak sequential bandwidth) can show *worse* row-buffer/page-hit behavior than fewer-wider-
channel designs for workloads whose access stride doesn't align well with the narrower per-channel
granularity — while still achieving similar or higher peak sequential bandwidth, exactly the
pattern measured above (similar `dram_total_gbs`, very different `dram_page_hit_rate_rd`). **This
is a reasoned hypothesis grounded in two already-documented, verifiable facts (the two real memory
configs), not a new guess** — but it is still not proof: confirming it would require knowing each
platform's actual physical row-buffer size and memory-controller page policy (open- vs
closed-page), which neither machine's documentation currently states, and which this repo has no
tooling to query directly.

**Same-day control run, 2026-09-29 — rules out time-based drift as the explanation**: to check
whether the 6-day gap between the original 96-EU run (2026-09-23) and the `JF04WVAW0381-TA` run
(2026-09-29) could itself explain part of the anomaly (e.g. an OpenVINO/driver update landing on
*either* machine in between), a fresh `--preset 6` was run on this repo's own 96-EU dev box the
same day: `kpi_runs/preset6_sweagent_gpu_20260929_225911` — real wall time **723.6s**, `ttft_s` at
`n=8198` across its 3 iterations: **55.38s / 55.18s / 50.88s**. This is close to (within normal
day-to-day variance of) the original run's 49.3-49.4s, and nowhere near `JF04WVAW0381-TA`'s
~15s — confirming **the anomaly is a real, specific property of `JF04WVAW0381-TA`, not a
time-based software drift affecting every machine**:

| Run | Date | Device | Wall time | `ttft_s` @ n=8198 (3 iterations) |
|---|---|---|---|---|
| `preset6_roofline_20260923_141035` | 2026-09-23 | this repo's 96-EU box | 689.77s | 49.4466 / 49.3289 / 49.2944 |
| `preset6_sweagent_gpu_20260929_225911` | 2026-09-29 (same day) | this repo's 96-EU box | 723.6s | 55.3805 / 55.179 / 50.8779 |
| `preset6_sweagent_gpu_20260929_211533` | 2026-09-29 (same day) | `JF04WVAW0381-TA`, 16-EU | 778.62s | 14.9501 / 14.8583 / 14.981 |

This machine's own day-to-day TTFT variance (49.3-49.4s → 50.9-55.4s, roughly +3-12%) is normal
run-to-run noise. `JF04WVAW0381-TA`'s ~15s is a categorically different result (~3.3-3.7×
faster), not an extension of that noise band — reinforcing that this is worth root-causing on
that specific machine (driver/OpenVINO version check, kernel-cache-clear rerun per the earlier
recommendation) rather than dismissed as measurement drift.

### 9.6 §9.1-§9.5 only ever covers `prefill_s`/TTFT — the other three buckets rely on this repo's own §4.3 evidence

Easy to lose track of, given how much of §9 is about the external project: **the external
`TTFT(n)` reference (§9.1) is a prefill-only model.** It has nothing to say about `decode_s`,
`tool_exec_gap_s`, or `stage_overhead_s`/`fixed_overhead_s` — those three buckets are, and always
were, projected using this repo's *own* `SystemSpec`-ratio math (§4), and their retention/Amdahl
knobs should be calibrated from this repo's *own* real calibration runs (§4.3), not the external
project. The table below is the explicit component→evidence map, since §9's focus on the prefill
piece could otherwise read as if the whole equation had been externally validated:

| Macro component | Scaling knob | Evidence source | GPU run (real, committed) | NPU run (real, committed) |
|---|---|---|---|---|
| `prefill_s` | `ttft_ratio(n)` (§9.1, external) — or `compute_efficiency_retention` (§4) if not using §9.5's integration | External CSV (§9.1) **or** native `prefill_sweep` (§4.3) | `prefill_sweep_gpu_20260925_110109` | `prefill_sweep_npu_20260925_104803` |
| `decode_s` | `memory_efficiency_retention` | `kv_cache_growth` (§4.3) | `kv_cache_growth_gpu_20260925_130236` | `kv_cache_growth_npu_20260925_115819`/`_120159`/`_120523` |
| `tool_exec_gap_s` | `cpu_efficiency_retention` + `tool_parallel_fraction` (Amdahl) | `tool_exec_only` (§4.3) | `tool_exec_only_gpu_20260925_130333` | `tool_exec_only_npu_20260925_121800`…`_124826` (7 iterations) |
| `stage_overhead_s` / `fixed_overhead_s` | (assumed constant, never scaled) | `thin_serving` (§4.3) | `thin_serving_gpu_20260925_105525` | `thin_serving_npu_20260925_104241`/`_104413` |

**Yes, iGPU decode-calibration data exists** — `kv_cache_growth_gpu_20260925_130236` (§4.3's
"All 4 presets were also run on iGPU" bullet list): 65.4/113.0/112.6/101.2 GB/s achieved, 82.7% of
theoretical peak. All four GPU-side calibration runs now exist (`prefill_sweep_gpu`,
`thin_serving_gpu`, `kv_cache_growth_gpu`, `tool_exec_only_gpu`), matching NPU's already-complete
4/4 coverage — full symmetric evidence for every bucket on both accelerator types.

**New same-shape confirmation from `prefill_sweep_gpu_20260925_110109`** (re-analyzed here,
2026-09-29): achieved prefill GFLOPs/s falls **65k (n≈169) → 53k (n≈514) → 41k (n≈1894) → 19k
(n≈7414)** — the same rise-then-drop-at-long-context shape §9.3 already noted on the NPU run,
now independently confirmed on iGPU too. Decode stayed a roughly flat 108-118 GB/s (86.7% of
best-observed peak) across the same sweep — consistent with `kv_cache_growth`'s finding that
decode degradation is a longer-context effect than this sweep's 128-8192 range fully exposes.

**A genuine bidirectional evidence flow, not just one-way borrowing**: the external project's own
`llama31_8b_roofline_pipeline_review.md` (2026-09-29 revision) now cross-references this exact
repo's `prefill_sweep_gpu_20260925_110109` run directly (copied into its own evidence tree as
`kpi_prefill_sweep_gpu_20260925_110109/` — confirmed identical `workflow_kpi.json`/`hw_samples.csv`
contents) to partially narrow its own `attention_buffer` memory-bound hypothesis, using this
repo's real EMON/`zes_mem_used_mb` counters as independent evidence the external project's own
tooling doesn't otherwise have access to. §9.5's `ttft_ratio(n)` borrowing therefore isn't a
one-directional dependency — both projects are now cross-checking each other's real measurements
on the same underlying model family, which is a stronger evidentiary position than either project
achieves alone.

### 9.7 EMON-informed efficiency retention (opt-in, 2026-09-29) — closing the loop from §9.6b's DRAM finding

**Memory/iGPU/CPU frequency were already first-class inputs to the equation** — `mem_freq_mts`,
`igpu_freq_ghz`, and `cpu_freq_ghz` feed directly into `mem_bw_peak_gbs`/`igpu_capability`/
`cpu_capability` (§3), which drive the *raw* ratio inside each `E_domain = effective_speedup(raw,
retention)` term (§4). What was **not** informed by real evidence was the `retention` half of that
formula — `compute_efficiency_retention`/`memory_efficiency_retention` were flat, hand-picked
constants (default `0.85`), not derived from any measurement. §9.6b's DRAM finding is exactly a
case where that flat guess is wrong for this repo's own real hardware (measured memory efficiency
is 44.9% of theoretical peak, not 85%) — so it's now wired into the projection as an opt-in
refinement rather than left as a diagnostic-only footnote.

**What changed**: `baseline_extractor.py` now also captures `dram_page_hit_rate_rd`/
`dram_rd_latency_ns` per stage (`StageMacroProfile.measured_util`, alongside the pre-existing
`accel_busy_pct`/`mem_bw_gbs`) when `hw_samples.csv` has those columns (EMON or PDH). Two new
flags — `--use-measured-compute-efficiency` / `--use-measured-memory-efficiency` (both
`tools/run_roofline_projection.py` and `tools/roofline_projection/integrate_external_ttft.py`) —
replace the flat `*_efficiency_retention` constant with **each stage's own real measured
achieved-vs-theoretical-peak fraction** (`accel_busy_pct/100` for compute; `mem_bw_gbs ÷
baseline_spec.mem_bw_peak_gbs` for memory), falling back to the flat value for any stage where no
measurement exists (`scaling_engine._resolve_retention`). Default behavior is unchanged (opt-in
only, off by default) — this doesn't silently override the existing "never fed back" principle
(§4.1), it gives an evidence-based alternative to reach for explicitly.

**Validated, both tools, same 256-EU/300-GB/s target, same baseline run** (`preset6_roofline_
20260923_141035`):

| Tool | Flat retention (0.85) | EMON-informed (`--use-measured-*-efficiency`) |
|---|---|---|
| `run_roofline_projection.py` (naive) | 339.15s (2.03×) | 421.10s (1.64×) |
| `integrate_external_ttft.py` (corrected) | 391.21s (1.76×) | 446.25s (1.55×) |

Both projections become **more conservative** (lower speedup) once fed this dev box's real,
measured memory efficiency — directly reflecting the poor DRAM page-hit rate found in §9.6b,
instead of assuming an optimistic flat 85% retention that this specific hardware doesn't actually
achieve. The memory-domain speedup also stops being one flat number and now varies per stage
(1.019×-1.768× across the 12 stages in the corrected-method run), since each stage's own active
window has its own real measured achieved bandwidth.

**This still only measures the BASELINE's own achieved efficiency** — using it as a stand-in for
what an unmeasured hypothetical target (e.g. 256 EU/300 GB/s) would itself achieve is still an
assumption, same as §4.1 already states; there's no way to measure a machine that doesn't exist.
What this closes is a narrower, real gap: for a *known baseline* (this repo's own dev box), the
retention no longer has to be a blind guess — it can be read directly off real EMON telemetry.

```powershell
.venv\Scripts\python.exe tools\roofline_projection\integrate_external_ttft.py --run kpi_runs\preset6_roofline_20260923_141035 `
    --igpu-xecores 256 --mem-bw-gbs 300 --p-cores 20 --e-cores 48 --use-measured-memory-efficiency
```

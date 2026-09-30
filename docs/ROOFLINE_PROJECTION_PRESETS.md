# Roofline Projection Presets

Companion to [docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md](ROOFLINE_HW_PROJECTION_METHODOLOGY.md)
(the full methodology/equations/limitations — always read that first). This doc is the
**reusable, named preset** layer: instead of re-deriving a target hardware spec and CLI command
from scratch each time, a **projection preset** bundles three things under one name:

1. A **persona workflow** — which real `kpi_runs/<experiment>` baseline run represents the
   scenario you care about (e.g. "SWE Agent developer on iGPU").
2. A **target spec** JSON — the hypothetical/future hardware to project onto
   (`data/configs/roofline_targets/*.json`).
3. The **exact command** to reproduce the projection, plus its last-known real output, so a
   result can be sanity-checked without re-running anything.

This mirrors the existing `tools/roofline_calibration/presets.py` pattern (named, reusable,
one-per-scenario) but for the *projection* step (`tools/run_roofline_projection.py`) instead of
the *calibration* step.

## Quick reference — all presets (Performance + Power KPIs)

All four presets below share the same real baseline: SWE Agent, iGPU, 96-EU dev box
(`kpi_runs/preset6_roofline_20260923_141035`, `current_baseline_REAL_96EU_devbox.json`) — baseline
wall time 689.77s, 13.3 tokens/s, 0.25 tokens/J. Report artifacts are committed under each preset's
`kpi_runs/preset6_roofline_20260923_141035/roofline_projection_<name>/` (linked below), so these
numbers can be re-checked without re-running anything.

| Preset | Target spec | Wall time | Speedup | Reduction | Tokens/s | Tokens/J | Peak package power | Report |
|---|---|---|---|---|---|---|---|---|
| 1 — Future iGPU workstation (LPDDR6) | 48 cores@5GHz, 256-EU@2.8GHz, ~300 GB/s | 383.11s | 1.80× | 44.5% | 23.9 | 0.30 | 88.6W | [report](../kpi_runs/preset6_roofline_20260923_141035/roofline_projection_persona_future_igpu/roofline_projection_report.html) |
| 2 — Moderate upgrade | 12 cores@4.6GHz, 160-EU@2.2GHz, ~273 GB/s | 494.53s | 1.39× | 28.3% | 18.5 | 0.31 | 65.3W | [report](../kpi_runs/preset6_roofline_20260923_141035/roofline_projection_moderate_upgrade/roofline_projection_report.html) |
| 3 — Heavy-duty workstation | 32 cores@5.0GHz, 384-EU@2.4GHz, ~563 GB/s | 289.04s | 2.39× | 58.1% | 31.7 | 0.37 | 98.5W | [report](../kpi_runs/preset6_roofline_20260923_141035/roofline_projection_heavy_duty_workstation/roofline_projection_report.html) |
| 4 — Datacenter-class | 64 cores@3.8GHz, 512-EU@2.6GHz, ~1,229 GB/s (HBM-class) | 203.54s | 3.39× | 70.5% | 45.0 | 0.44 | 123.3W | [report](../kpi_runs/preset6_roofline_20260923_141035/roofline_projection_datacenter_class/roofline_projection_report.html) |

All four used `--use-measured-compute-efficiency --use-measured-memory-efficiency
--use-duty-cycle-power` (§9.7/Power section of the main doc). None of these target specs declare a
`power_budget_w`, so no power-budget-exceeded warning fires for any of them (see main doc's Power
section for what that check does).

## Preset 1 — "SWE Agent Developer (iGPU)" → "Future iGPU Workstation (LPDDR6)"

**Persona**: a developer running the SWE-Agent agentic coding scenario on an Intel iGPU today,
asking "how much would a specific future SoC actually help?"

| | |
|---|---|
| Baseline run | `kpi_runs/preset6_roofline_20260923_141035` (real, SWE Agent, iGPU, 96-EU dev box, 12 stages / 3 iterations) |
| Baseline spec | `data/configs/roofline_targets/current_baseline_REAL_96EU_devbox.json` — corrected real spec (16 cores, 1.8 GHz iGPU; see `ROOFLINE_HW_PROJECTION_METHODOLOGY.md` §9's frequency-mismatch diagnostic for why this isn't the datasheet-guessed template) |
| Target spec | `data/configs/roofline_targets/persona_future_igpu_workstation.json` — 48 CPU cores @ 5 GHz, 256-EU iGPU @ 2.8 GHz, ~300 GB/s memory via **LPDDR6** (real JEDEC JESD209-6 max speed grade, 14,400 MT/s, 14 channels × 12-bit = 302.4 GB/s — see that file's own `notes` field for the derivation) |
| Method | Naive Method (§10 of the main doc) — currently the more accurate of the two available methods against real ground truth (§9.7a) |
| Efficiency knobs | `--use-measured-compute-efficiency --use-measured-memory-efficiency --use-duty-cycle-power` (§9.7 — real EMON-measured retention + duty-cycle-weighted power scaling instead of flat guesses) |

**Exact command:**

```powershell
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset6_roofline_20260923_141035 `
    --baseline-spec data\configs\roofline_targets\current_baseline_REAL_96EU_devbox.json `
    --target-spec data\configs\roofline_targets\persona_future_igpu_workstation.json `
    --use-measured-compute-efficiency --use-measured-memory-efficiency --use-duty-cycle-power `
    --tool-parallel-fraction 0.5 --what-if `
    --out kpi_runs\preset6_roofline_20260923_141035\roofline_projection_persona_future_igpu
```

**Last-known real output** (2026-09-30, no frequency-mismatch warning — baseline spec is
corrected/accurate; no power-budget warning either, since this target spec doesn't declare a
`power_budget_w` yet — add one if you have a real TDP number for this future SoC):

| KPI type | Metric | Baseline | Projected |
|---|---|---|---|
| **Performance** | Wall time | 689.77s | **383.11s** |
| **Performance** | Speedup | — | **1.80×** |
| **Performance** | Wall time reduction | — | **44.5%** |
| **Performance** | Tokens/s | 13.3 | 23.9 |
| **Power** | Tokens/Joule | 0.25 | **0.30** |
| **Power** | Peak projected package power | — | **88.6W** (stage `08_swe_agent_2`, no budget check performed) |

Report + standalone `what_if_calculator.html` written to
`kpi_runs/preset6_roofline_20260923_141035/roofline_projection_persona_future_igpu/`.

**Caveats specific to this preset** (in addition to the main doc's general limitations, §8):
this is the **Naive Method** — its "100% compute-bound prefill" assumption is known-incomplete
(§9.3), and its current measured error against a real 16-EU ground-truth machine is **+19.2%**
(§9.7a) even with EMON-informed efficiency enabled. Treat `1.80×` as directional, not exact — a
real future SoC with this spec could plausibly land anywhere in roughly the 1.4×-2.2× range given
the demonstrated error margin.

## Presets 2-4 — "SWE Agent Developer (iGPU)" → illustrative upgrade tiers

Same baseline persona/run/spec as Preset 1 (SWE Agent, iGPU, `preset6_roofline_20260923_141035`,
`current_baseline_REAL_96EU_devbox.json`), projected onto three **illustrative, not-vendor-verified**
target tiers already checked into `data/configs/roofline_targets/` (`example_moderate_upgrade.json`,
`example_heavy_duty_workstation.json`, `example_datacenter_class.json` — each file's own `notes`
field says so explicitly; edit them with real datasheet numbers before treating results as
anything but a rough "what direction/magnitude would this kind of upgrade move the needle"
sanity check). Same command pattern as Preset 1, only `--target-spec`/`--out` change:

```powershell
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset6_roofline_20260923_141035 `
    --baseline-spec data\configs\roofline_targets\current_baseline_REAL_96EU_devbox.json `
    --target-spec data\configs\roofline_targets\<example_moderate_upgrade|example_heavy_duty_workstation|example_datacenter_class>.json `
    --use-measured-compute-efficiency --use-measured-memory-efficiency --use-duty-cycle-power `
    --tool-parallel-fraction 0.5 --what-if `
    --out kpi_runs\preset6_roofline_20260923_141035\roofline_projection_<moderate_upgrade|heavy_duty_workstation|datacenter_class>
```

**Last-known real output** (2026-09-30, baseline wall time 689.77s / 13.3 tokens/s / 0.25 tokens/J
for all three; no target spec here declares a `power_budget_w` yet, so no power-budget warning
fires — add one to a target spec JSON if you have a real TDP number to check against):

| Preset | Target spec | ~vs. baseline | Projected wall time | Speedup | Reduction | Tokens/s | Tokens/J | Peak package power |
|---|---|---|---|---|---|---|---|---|
| 2 — Moderate upgrade | `example_moderate_upgrade.json` (12 cores@4.6GHz, 160-EU@2.2GHz, 8ch×32-bit@8533MT/s ≈ 273 GB/s) | ~1.5-2x each resource | **494.53s** | **1.39×** | 28.3% | 18.5 | 0.31 | 65.3W |
| 3 — Heavy-duty workstation | `example_heavy_duty_workstation.json` (32 cores@5.0GHz, 384-EU@2.4GHz, 8ch×64-bit@8800MT/s ≈ 563 GB/s) | ~4x each resource | **289.04s** | **2.39×** | 58.1% | 31.7 | 0.37 | 98.5W |
| 4 — Datacenter-class | `example_datacenter_class.json` (64 cores@3.8GHz, 512-EU@2.6GHz, 16ch×64-bit@9600MT/s ≈ 1,229 GB/s, HBM-class) | ~8x+ each resource | **203.54s** | **3.39×** | 70.5% | 45.0 | 0.44 | 123.3W |

Reports + `what_if_calculator.html` written to `kpi_runs/preset6_roofline_20260923_141035/roofline_projection_<moderate_upgrade|heavy_duty_workstation|datacenter_class>/`.

Note the sub-linear scaling (~4x hardware → 2.39x speedup, ~8x+ hardware → only 3.39x speedup): this
is Amdahl's-law behavior from the non-prefill stages (decode, tool-exec, fixed overhead — see §1-§2
of the main doc) that don't scale with compute/bandwidth at all, and is expected, not a projection
bug. Same accuracy caveats as Preset 1 apply (Naive Method, EMON-informed efficiency, +19.2% known
error margin against the one real ground-truth cross-check available today).

## Adding a new preset

1. Pick a real baseline run under `kpi_runs/` that represents the persona/workflow you care about
   (SWE Agent vs Data Agent, NPU vs iGPU — see `docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §1-3 for
   what each preset scenario actually does).
2. If this is a new physical baseline machine, create/correct its `SystemSpec` JSON — check for a
   frequency-mismatch warning on first run (§9 of the main doc) and correct `igpu_freq_ghz`/
   `cpu_freq_ghz`/`cpu_cores` from real measured values, not just datasheet guesses.
3. Create a target spec JSON under `data/configs/roofline_targets/` with real, defensible numbers
   for every field — cite a real spec/datasheet in the `notes` field (see Preset 1's LPDDR6
   derivation above as the pattern to follow), not an arbitrary round number.
4. Run the exact command pattern above, capture the real output, and add both as a new `## Preset
   N` section in this file.
5. If a real second SUT ever becomes available for the target hardware family (as happened with
   `JF04WVAW0381-TA` for the 16-EU case, see the main doc's §9.6a-§9.7a), re-run and compare
   against real ground truth rather than trusting the projection alone.

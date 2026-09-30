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

## Preset 1 — "SWE Agent Developer (iGPU)" → "Future iGPU Workstation (LPDDR6)"

**Persona**: a developer running the SWE-Agent agentic coding scenario on an Intel iGPU today,
asking "how much would a specific future SoC actually help?"

| | |
|---|---|
| Baseline run | `kpi_runs/preset6_roofline_20260923_141035` (real, SWE Agent, iGPU, 96-EU dev box, 12 stages / 3 iterations) |
| Baseline spec | `data/configs/roofline_targets/current_baseline_REAL_96EU_devbox.json` — corrected real spec (16 cores, 1.8 GHz iGPU; see `ROOFLINE_HW_PROJECTION_METHODOLOGY.md` §9's frequency-mismatch diagnostic for why this isn't the datasheet-guessed template) |
| Target spec | `data/configs/roofline_targets/persona_future_igpu_workstation.json` — 48 CPU cores @ 5 GHz, 256-EU iGPU @ 2.8 GHz, ~300 GB/s memory via **LPDDR6** (real JEDEC JESD209-6 max speed grade, 14,400 MT/s, 14 channels × 12-bit = 302.4 GB/s — see that file's own `notes` field for the derivation) |
| Method | Naive Method (§10 of the main doc) — currently the more accurate of the two available methods against real ground truth (§9.7a) |
| Efficiency knobs | `--use-measured-compute-efficiency --use-measured-memory-efficiency` (§9.7 — real EMON-measured retention instead of a flat 0.85 guess) |

**Exact command:**

```powershell
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
.venv\Scripts\python.exe tools\run_roofline_projection.py `
    --run kpi_runs\preset6_roofline_20260923_141035 `
    --baseline-spec data\configs\roofline_targets\current_baseline_REAL_96EU_devbox.json `
    --target-spec data\configs\roofline_targets\persona_future_igpu_workstation.json `
    --use-measured-compute-efficiency --use-measured-memory-efficiency `
    --tool-parallel-fraction 0.5 --what-if `
    --out kpi_runs\preset6_roofline_20260923_141035\roofline_projection_persona_future_igpu
```

**Last-known real output** (2026-09-30, no frequency-mismatch warning — baseline spec is
corrected/accurate):

| | Baseline | Projected |
|---|---|---|
| Wall time | 689.77s | **383.11s** |
| Speedup | — | **1.80×** |
| Wall time reduction | — | **44.5%** |
| Tokens/s | 13.3 | 23.9 |
| Tokens/Joule | 0.25 | 0.20 |

Report + standalone `what_if_calculator.html` written to
`kpi_runs/preset6_roofline_20260923_141035/roofline_projection_persona_future_igpu/`.

**Caveats specific to this preset** (in addition to the main doc's general limitations, §8):
this is the **Naive Method** — its "100% compute-bound prefill" assumption is known-incomplete
(§9.3), and its current measured error against a real 16-EU ground-truth machine is **+19.2%**
(§9.7a) even with EMON-informed efficiency enabled. Treat `1.80×` as directional, not exact — a
real future SoC with this spec could plausibly land anywhere in roughly the 1.4×-2.2× range given
the demonstrated error margin.

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

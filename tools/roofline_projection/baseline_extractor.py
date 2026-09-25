"""Extract a per-stage MACRO-COMPONENT baseline profile from a real kpi_runs/ experiment dir.

This is the "bottom" of the bottom-up pipeline: it turns the already-measured
workflow_kpi.json (+ experiment.json, + hw_samples.csv if present) into a small set of
physically-meaningful time buckets per stage, each of which the scaling engine knows how
to project independently:

  prefill_s          - compute-bound (prompt processing), scales with accelerator compute peak
  decode_s           - memory-bandwidth-bound (autoregressive generation), scales with mem BW
  stage_overhead_s   - residual bookkeeping/logging inside the stage (assumed fixed)
  tool_exec_gap_s    - wall-clock time the harness spends running a tool call after this stage
                       (git apply, pytest, file IO, ...) - CPU-bound, scales via Amdahl's law.
                       For the LAST stage this is the gap up to workflow end (timeline.end_epoch),
                       not 0 - any final verification/tool call belongs here, not in fixed_overhead_s.
  fixed_overhead_s   - workflow-level constant (process startup, model load, shutdown, ...); by
                       construction this ends up being ~ the gap BEFORE the first stage starts

  Total baseline wall time = sum(prefill_s + decode_s + stage_overhead_s + tool_exec_gap_s)
                              over all stages + fixed_overhead_s
                           == workflow_wall_time_s (by construction, see _build_stage_profiles)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Quantization -> bytes/weight table + params estimator, intentionally duplicated (not imported)
# from tools/KPI-hub/generate_kpi_report.py: "KPI-hub" is a vendored, hyphenated directory name
# (not a valid Python package/import path) that gets wholesale-replaced by sync_kpi_hub.ps1 - this
# tool must not depend on it. Keep these two small tables in sync manually if the vendored
# original ever changes (tools/KPI-hub/generate_kpi_report.py::_bytes_per_weight/_estimate_params_b).
_QUANT_BYTES_PER_WEIGHT = [
    ("int4", 0.5),
    ("int8", 1.0),
    ("fp16", 2.0),
    ("bf16", 2.0),
    ("fp32", 4.0),
]


def bytes_per_weight(model_name: str) -> float:
    name_lower = (model_name or "").lower()
    for tag, b in _QUANT_BYTES_PER_WEIGHT:
        if tag in name_lower:
            return b
    return 0.5  # default: every current preset is int4-quantized


def estimate_params_b(model_weight_mb, bpw: float) -> float:
    if model_weight_mb and model_weight_mb > 0:
        return (model_weight_mb * 1024 * 1024) / bpw / 1e9
    return 4.0


@dataclass
class StageMacroProfile:
    name: str
    is_cold: Optional[bool]
    input_tokens: int
    output_tokens: int
    wall_time_s: float
    prefill_s: float
    decode_s: float
    stage_overhead_s: float
    tool_exec_gap_s: float
    itl_ms: float               # per-token decode latency (avg_itl_ms, straight from the log)
    ttft_ms: float              # log's own ttft_s*1000 if present, else prefill_s*1000 + itl_ms
    tool_calls: Dict[str, int] = field(default_factory=dict)
    avg_power_w: Dict[str, float] = field(default_factory=dict)
    # Best-effort MEASURED (not modeled) accelerator busy% / achieved mem BW during this stage's
    # own active window (start_iso->end_iso, i.e. NOT the tool-exec gap) - diagnostic only, never
    # fed into the scaling math (a baseline machine's own efficiency says nothing about what a
    # different target machine will achieve) - see BaselineProfile.measured_*  and §4.1 of the doc.
    measured_util: Dict[str, float] = field(default_factory=dict)


@dataclass
class BaselineProfile:
    run_dir: str
    model: str
    device_type: str
    backend: str
    model_weight_mb: float
    bytes_per_weight: float
    params_b: float
    workflow_wall_time_s: float
    fixed_overhead_s: float
    stages: List[StageMacroProfile]
    # Active-time-weighted averages of StageMacroProfile.measured_util across all stages -
    # diagnostic only (see above), None if hw_samples.csv/its columns were unavailable.
    measured_accel_busy_pct: Optional[float] = None
    measured_mem_bw_gbs: Optional[float] = None

    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.stages)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "stages"}
        d["stages"] = [s.__dict__ for s in self.stages]
        return d


def _load_power_lookup(run_dir: Path):
    """Return a function (start_epoch, end_epoch) -> {rapl component: mean watts}, best-effort.

    Returns a no-op lookup (always {}) if pandas isn't installed or hw_samples.csv is missing/
    doesn't contain the expected columns - power/energy projection is an optional enrichment,
    never a hard requirement for the wall-time projection itself.
    """
    df = _load_hw_samples_df(run_dir)
    empty = lambda a, b: {}
    if df is None:
        return empty
    import pandas as pd  # already imported successfully inside _load_hw_samples_df above

    rapl_cols = {
        "cpu": "rapl_cpu_w", "igpu": "rapl_igpu_w", "npu": "rapl_npu_w", "soc": "rapl_soc_w",
    }
    present = {k: c for k, c in rapl_cols.items() if c in df.columns}
    if not present:
        return empty

    def lookup(start_iso: str, end_iso: str) -> Dict[str, float]:
        start_dt, end_dt = pd.Timestamp(start_iso), pd.Timestamp(end_iso)
        window = df[(df["_dt"] >= start_dt) & (df["_dt"] <= end_dt)]
        if window.empty:
            return {}
        out = {}
        for key, col in present.items():
            vals = window[col].dropna()
            if len(vals) and vals.mean() > 0:
                out[key] = float(vals.mean())
        return out

    return lookup


def _load_hw_samples_df(run_dir: Path):
    """Shared hw_samples.csv loader (naive-local `timestamp` -> `_dt`) for power + utilization
    lookups. Returns None (never raises) if pandas is missing or the CSV is absent/unparseable -
    both lookups built on top of this are best-effort diagnostics, never hard requirements.
    """
    csv_path = run_dir / "hw_samples.csv"
    if not csv_path.exists():
        return None
    try:
        import pandas as pd
    except ImportError:
        return None
    try:
        df = pd.read_csv(csv_path)
        # Naive-datetime comparison (NOT epoch/Unix-timestamp conversion) - matches the proven
        # approach in tools/KPI-hub/plot_utilization_interactive.py::load_phases(). hw_samples.csv's
        # "timestamp" column is naive LOCAL time (whatever machine/timezone ran the sampler);
        # workflow_kpi.json's start_epoch/end_epoch are true UTC Unix epoch from time.time() in a
        # different process. Converting the CSV's naive-local strings to Unix epoch (e.g. via
        # pandas datetime64->int64) silently produces a wrong, TZ-offset-shifted value with no
        # error - every stage window then matches zero rows. start_iso/end_iso are naive strings
        # written by the SAME process/clock convention as the CSV, so comparing them directly
        # (no epoch conversion at all) is the only alignment that's actually correct.
        df["_dt"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return None


def _load_utilization_lookup(run_dir: Path, device_type: str):
    """Return a function (start_iso, end_iso) -> {"accel_busy_pct", "mem_bw_gbs"}, best-effort.

    MEASURED (not modeled) accelerator busy% (npu_pct/igpu_pct, whichever matches device_type)
    and achieved DRAM bandwidth (dram_total_gbs) - i.e. the ACTUAL fraction of theoretical peak
    this baseline machine achieved, straight from hw_samples.csv. Diagnostic only: shown in
    reports so a user can sanity-check their `--*-efficiency-retention` guesses against real
    numbers instead of picking them blind - never fed back into the projection math itself,
    since a baseline machine's own achieved efficiency doesn't tell you what a *different*
    target machine will achieve (see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §4.1).
    """
    df = _load_hw_samples_df(run_dir)
    empty = lambda a, b: {}
    if df is None:
        return empty
    import pandas as pd  # already imported successfully inside _load_hw_samples_df above

    busy_col = {"NPU": "npu_pct", "GPU": "igpu_pct", "IGPU": "igpu_pct", "CPU": "cpu_total_pct"}.get(
        (device_type or "").upper()
    )
    has_busy = busy_col in df.columns if busy_col else False
    has_bw = "dram_total_gbs" in df.columns
    if not has_busy and not has_bw:
        return empty

    def lookup(start_iso: str, end_iso: str) -> Dict[str, float]:
        start_dt, end_dt = pd.Timestamp(start_iso), pd.Timestamp(end_iso)
        window = df[(df["_dt"] >= start_dt) & (df["_dt"] <= end_dt)]
        if window.empty:
            return {}
        out = {}
        if has_busy:
            vals = pd.to_numeric(window[busy_col], errors="coerce").dropna()
            if len(vals):
                out["accel_busy_pct"] = float(vals.mean())
        if has_bw:
            vals = pd.to_numeric(window["dram_total_gbs"], errors="coerce").dropna()
            if len(vals):
                out["mem_bw_gbs"] = float(vals.mean())
        return out

    return lookup


def extract_baseline(run_dir: str) -> BaselineProfile:
    run_path = Path(run_dir)
    with open(run_path / "workflow_kpi.json", "r", encoding="utf-8") as fh:
        wkpi = json.load(fh)
    exp_meta = {}
    exp_path = run_path / "experiment.json"
    if exp_path.exists():
        with open(exp_path, "r", encoding="utf-8") as fh:
            exp_meta = json.load(fh)

    model = wkpi.get("model") or exp_meta.get("model", "")
    device_type = (exp_meta.get("device_type") or "").upper()
    backend = wkpi.get("backend") or exp_meta.get("backend", "")
    model_weight_mb = exp_meta.get("model_weight_mb", 0)
    bpw = bytes_per_weight(model)
    params_b = estimate_params_b(model_weight_mb, bpw)

    power_lookup = _load_power_lookup(run_path)
    utilization_lookup = _load_utilization_lookup(run_path, device_type)

    workflow_end_epoch = (wkpi.get("timeline") or {}).get("end_epoch")

    raw_stages = {
        name: s for name, s in wkpi.get("stages", {}).items() if name != "task_agent"
    }
    ordered = sorted(raw_stages.items(), key=lambda kv: kv[1].get("start_epoch", 0))

    stages: List[StageMacroProfile] = []
    for i, (name, s) in enumerate(ordered):
        wall_time_s = s.get("wall_time_s", 0.0)
        output_tokens = s.get("output_tokens", 0)
        prefill_ms_est = s.get("prefill_ms_est")
        avg_itl_ms = s.get("avg_itl_ms")
        if prefill_ms_est is None and avg_itl_ms is None:
            # Older log format (e.g. "full" kpi_mode presets, pre-dating per-token profiling
            # fields) has no prefill_ms_est/avg_itl_ms at all - without this, prefill_s/decode_s
            # both silently collapse to 0 and the ENTIRE stage gets misclassified as fixed
            # stage_overhead_s (never scales with target hardware). Derive equivalent values from
            # ttft_s/wall_time_s/output_tokens using the same ttft = prefill + itl relationship
            # the newer format itself uses (see ttft_ms field below) - same arithmetic, just
            # solved for the two unknowns instead of read directly.
            ttft_s = s.get("ttft_s")
            if ttft_s is not None and output_tokens > 0 and wall_time_s > ttft_s:
                avg_itl_ms = (wall_time_s - ttft_s) / output_tokens * 1000.0
                prefill_ms_est = max(ttft_s * 1000.0 - avg_itl_ms, 0.0)
            else:
                avg_itl_ms = 0.0
                prefill_ms_est = 0.0
        prefill_s = (prefill_ms_est or 0.0) / 1000.0
        avg_itl_ms = avg_itl_ms or 0.0
        decode_s = avg_itl_ms / 1000.0 * output_tokens
        # Measurement noise can occasionally make prefill_s+decode_s slightly exceed wall_time_s;
        # rescale both proportionally (rather than just clamping stage_overhead_s to 0) so the
        # "sum(buckets) == wall_time_s" invariant documented above always holds exactly, not just
        # when the underlying telemetry happens to agree.
        raw_active_s = prefill_s + decode_s
        if raw_active_s > wall_time_s > 0:
            scale = wall_time_s / raw_active_s
            prefill_s *= scale
            decode_s *= scale
        stage_overhead_s = max(wall_time_s - prefill_s - decode_s, 0.0)

        this_end = s.get("end_epoch", 0)
        if i + 1 < len(ordered):
            next_start = ordered[i + 1][1].get("start_epoch", 0)
            next_start_iso = ordered[i + 1][1].get("start_iso")
        else:
            # Last stage: gap to workflow end (e.g. a final test/verification tool call) is still
            # CPU-bound tool-exec time, not workflow-level fixed overhead - see module docstring.
            next_start = workflow_end_epoch or this_end
            next_start_iso = wkpi.get("workflow_end_iso")
        tool_exec_gap_s = max(next_start - this_end, 0.0)

        avg_power_w = {}
        if s.get("start_iso"):
            # Window spans the WHOLE bucket (prefill+decode+overhead+tool_exec_gap_s), i.e. up to
            # the next stage's start (or workflow end for the last stage) - NOT just s["end_iso"].
            # avg_power_w gets multiplied by that whole bucket's duration downstream (see
            # scaling_engine.py's energy_j calc), so averaging only over the "active" sub-window
            # and then applying it to the full bucket would misattribute the (typically lower)
            # accelerator-idle tool-exec-gap power as if it were the (higher) active-generation
            # power throughout the gap too - overstating energy for any stage with a real gap.
            power_window_end_iso = next_start_iso or s.get("end_iso")
            avg_power_w = power_lookup(s["start_iso"], power_window_end_iso)

        measured_util = {}
        if s.get("start_iso") and s.get("end_iso"):
            # Deliberately the NARROW active window (start_iso->end_iso, not the gap-widened one
            # used for power above) - this is meant to characterize busy%/achieved-BW WHILE the
            # accelerator is actually generating tokens, not diluted by idle tool-exec time.
            measured_util = utilization_lookup(s["start_iso"], s["end_iso"])

        stages.append(StageMacroProfile(
            name=name,
            is_cold=s.get("is_cold"),
            input_tokens=s.get("input_tokens", 0),
            output_tokens=output_tokens,
            wall_time_s=wall_time_s,
            prefill_s=prefill_s,
            decode_s=decode_s,
            stage_overhead_s=stage_overhead_s,
            tool_exec_gap_s=tool_exec_gap_s,
            itl_ms=avg_itl_ms,
            # Prefer the log's own ttft_s (ground truth) over recomputing it; only derive it when
            # absent, so this stays "no new measurement, pure arithmetic on existing fields" even
            # if a future log format ever changes how ttft_s itself is derived.
            ttft_ms=(s["ttft_s"] * 1000.0) if s.get("ttft_s") is not None else (prefill_s * 1000.0 + avg_itl_ms),
            tool_calls=s.get("tool_calls", {}) or {},
            avg_power_w=avg_power_w,
            measured_util=measured_util,
        ))

    workflow_wall_time_s = wkpi.get("workflow_wall_time_s") or exp_meta.get("workflow_duration_s", 0.0)
    accounted = sum(s.wall_time_s + s.tool_exec_gap_s for s in stages)
    fixed_overhead_s = max(workflow_wall_time_s - accounted, 0.0)

    def _active_weighted_avg(key: str) -> Optional[float]:
        # Weight by each stage's own active window (prefill_s+decode_s), matching the window the
        # underlying measurement was averaged over - see measured_util's narrow-window comment above.
        weighted_sum, weight_total = 0.0, 0.0
        for st in stages:
            v = st.measured_util.get(key)
            if v is None:
                continue
            w = st.prefill_s + st.decode_s
            weighted_sum += v * w
            weight_total += w
        return (weighted_sum / weight_total) if weight_total else None

    return BaselineProfile(
        run_dir=str(run_path),
        model=model,
        device_type=device_type,
        backend=backend,
        model_weight_mb=model_weight_mb,
        bytes_per_weight=bpw,
        params_b=params_b,
        workflow_wall_time_s=workflow_wall_time_s,
        fixed_overhead_s=fixed_overhead_s,
        stages=stages,
        measured_accel_busy_pct=_active_weighted_avg("accel_busy_pct"),
        measured_mem_bw_gbs=_active_weighted_avg("mem_bw_gbs"),
    )

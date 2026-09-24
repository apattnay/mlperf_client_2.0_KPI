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
    ttft_ms: float              # prefill_s*1000 + itl_ms - matches the log's own TTFT definition
    tool_calls: Dict[str, int] = field(default_factory=dict)
    avg_power_w: Dict[str, float] = field(default_factory=dict)


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
    csv_path = run_dir / "hw_samples.csv"
    empty = lambda a, b: {}
    if not csv_path.exists():
        return empty
    try:
        import pandas as pd
    except ImportError:
        return empty

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
    except Exception:
        return empty

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

    workflow_end_epoch = (wkpi.get("timeline") or {}).get("end_epoch")

    raw_stages = {
        name: s for name, s in wkpi.get("stages", {}).items() if name != "task_agent"
    }
    ordered = sorted(raw_stages.items(), key=lambda kv: kv[1].get("start_epoch", 0))

    stages: List[StageMacroProfile] = []
    for i, (name, s) in enumerate(ordered):
        wall_time_s = s.get("wall_time_s", 0.0)
        output_tokens = s.get("output_tokens", 0)
        prefill_s = (s.get("prefill_ms_est") or 0.0) / 1000.0
        avg_itl_ms = s.get("avg_itl_ms") or 0.0
        decode_s = avg_itl_ms / 1000.0 * output_tokens
        stage_overhead_s = max(wall_time_s - prefill_s - decode_s, 0.0)

        this_end = s.get("end_epoch", 0)
        if i + 1 < len(ordered):
            next_start = ordered[i + 1][1].get("start_epoch", 0)
        else:
            # Last stage: gap to workflow end (e.g. a final test/verification tool call) is still
            # CPU-bound tool-exec time, not workflow-level fixed overhead - see module docstring.
            next_start = workflow_end_epoch or this_end
        tool_exec_gap_s = max(next_start - this_end, 0.0)

        avg_power_w = {}
        if s.get("start_iso") and s.get("end_iso"):
            avg_power_w = power_lookup(s["start_iso"], s["end_iso"])

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
            ttft_ms=prefill_s * 1000.0 + avg_itl_ms,
            tool_calls=s.get("tool_calls", {}) or {},
            avg_power_w=avg_power_w,
        ))

    workflow_wall_time_s = wkpi.get("workflow_wall_time_s") or exp_meta.get("workflow_duration_s", 0.0)
    accounted = sum(s.wall_time_s + s.tool_exec_gap_s for s in stages)
    fixed_overhead_s = max(workflow_wall_time_s - accounted, 0.0)

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
    )

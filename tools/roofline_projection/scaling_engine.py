"""Bottom-up macro-component scaling engine.

Projects a BaselineProfile (from baseline_extractor.py, measured on THIS machine) onto a
TARGET SystemSpec (hypothetical heavier-duty machine) by scaling each macro time bucket by
the physically-relevant hardware ratio between baseline and target, then summing back up to
a total projected wall time. See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md for the full
equations and worked example.

Per-stage macro components and what they scale with:
  prefill_s          -> accelerator compute capability ratio (NPU: MACs x freq, iGPU: XeCores x freq)
  decode_s           -> memory bandwidth ratio (channels x width x transfer-rate)
  tool_exec_gap_s    -> CPU capability ratio via Amdahl's law (cores x freq, partly parallel)
  stage_overhead_s   -> unchanged (fixed bookkeeping/logging cost, not resource-bound)
  fixed_overhead_s   -> unchanged (process startup/model load/shutdown - workflow-level constant)

Total projected wall time = sum(projected stage components) + fixed_overhead_s (unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .baseline_extractor import BaselineProfile, StageMacroProfile
from .hw_spec import SystemSpec, raw_speedup, amdahl_speedup, effective_speedup


@dataclass
class ProjectionAssumptions:
    # How much of the *ideal* HW speedup is actually realized (1.0 = perfect/iso-efficiency
    # scaling; lower values model real-world NUMA/contention/driver-overhead losses at scale).
    # Split per-domain (rather than one shared knob) because NPU-MAC, iGPU-XeCore, and
    # CPU/mem-bandwidth paths on a real SoC do NOT achieve the same fraction of their own
    # theoretical peak - see baseline_extractor.py's measured_accel_busy_pct/measured_mem_bw_gbs
    # (real telemetry) and docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §4.1 for measured evidence
    # that NPU/iGPU/mem achieved-vs-peak utilization genuinely differ, sometimes by 2x.
    compute_efficiency_retention: float = 0.85   # prefill: NPU MACs or iGPU XeCores, whichever ran
    memory_efficiency_retention: float = 0.85    # decode: memory bandwidth (USM path of that accelerator)
    cpu_efficiency_retention: float = 0.85       # tool-exec: stacks with Amdahl's law below (see doc limitation #8)
    # Amdahl parallel fraction for tool-execution time (git apply / pytest / file IO / ...):
    # how much of that wall-clock time is assumed to actually benefit from extra CPU cores
    # vs. being effectively serial (single-thread-bound: process spawn, disk IO, ...). This is
    # the "contention due to parallelism" factor - NOT a duplicate of cpu_efficiency_retention
    # above (that's "how much of the ideal ratio is realized"; this is "how much of the work can
    # even use extra cores in the first place").
    tool_parallel_fraction: float = 0.5
    # Dynamic power ~ (resource_count x freq) ^ power_scaling_exponent. 1.0 = linear (common
    # simplifying assumption at iso process-node); has no effect if RAPL power wasn't measured.
    power_scaling_exponent: float = 1.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ProjectionAssumptions":
        if not d:
            return cls()
        d = dict(d)
        # Backward-compat: a legacy single "efficiency_retention" sets all three per-domain knobs
        # at once, unless the caller also specifies one of the per-domain keys explicitly.
        if "efficiency_retention" in d:
            legacy = d.pop("efficiency_retention")
            d.setdefault("compute_efficiency_retention", legacy)
            d.setdefault("memory_efficiency_retention", legacy)
            d.setdefault("cpu_efficiency_retention", legacy)
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class ProjectedStage:
    name: str
    is_cold: Optional[bool]
    input_tokens: int
    output_tokens: int
    baseline_wall_s: float
    projected_wall_s: float
    baseline: Dict[str, float]
    projected: Dict[str, float]
    speedups: Dict[str, float]
    baseline_ttft_ms: float
    projected_ttft_ms: float
    baseline_itl_ms: float
    projected_itl_ms: float
    tool_calls: Dict[str, int]
    baseline_power_w: Dict[str, float]
    projected_power_w: Dict[str, float]


@dataclass
class ProjectionResult:
    baseline_spec: SystemSpec
    target_spec: SystemSpec
    assumptions: ProjectionAssumptions
    device_type: str
    model: str
    params_b: float
    stages: List[ProjectedStage]
    baseline_wall_time_s: float
    projected_wall_time_s: float
    fixed_overhead_s: float
    total_output_tokens: int
    baseline_tok_s: float
    projected_tok_s: float
    baseline_energy_j: Optional[float]
    projected_energy_j: Optional[float]
    baseline_tok_per_j: Optional[float]
    projected_tok_per_j: Optional[float]
    avg_baseline_ttft_ms: Optional[float]
    avg_projected_ttft_ms: Optional[float]
    avg_baseline_itl_ms: Optional[float]
    avg_projected_itl_ms: Optional[float]
    # Real MEASURED (not modeled) baseline efficiency, straight from hw_samples.csv - diagnostic
    # only, to help calibrate the *_efficiency_retention knobs above against reality instead of
    # guessing; never fed back into the projection math itself. None if unavailable.
    measured_accel_busy_pct: Optional[float]
    measured_mem_bw_gbs: Optional[float]
    measured_mem_bw_efficiency_pct: Optional[float]

    @property
    def wall_time_speedup_x(self) -> float:
        if not self.projected_wall_time_s:
            return 1.0
        return self.baseline_wall_time_s / self.projected_wall_time_s

    @property
    def wall_time_reduction_pct(self) -> float:
        if not self.baseline_wall_time_s:
            return 0.0
        return (self.baseline_wall_time_s - self.projected_wall_time_s) / self.baseline_wall_time_s * 100.0

    def to_dict(self) -> dict:
        return {
            "baseline_spec": self.baseline_spec.to_dict(),
            "target_spec": self.target_spec.to_dict(),
            "assumptions": self.assumptions.to_dict(),
            "device_type": self.device_type,
            "model": self.model,
            "params_b": self.params_b,
            "stages": [s.__dict__ for s in self.stages],
            "baseline_wall_time_s": self.baseline_wall_time_s,
            "projected_wall_time_s": self.projected_wall_time_s,
            "fixed_overhead_s": self.fixed_overhead_s,
            "total_output_tokens": self.total_output_tokens,
            "baseline_tok_s": self.baseline_tok_s,
            "projected_tok_s": self.projected_tok_s,
            "baseline_energy_j": self.baseline_energy_j,
            "projected_energy_j": self.projected_energy_j,
            "baseline_tok_per_j": self.baseline_tok_per_j,
            "projected_tok_per_j": self.projected_tok_per_j,
            "avg_baseline_ttft_ms": self.avg_baseline_ttft_ms,
            "avg_projected_ttft_ms": self.avg_projected_ttft_ms,
            "avg_baseline_itl_ms": self.avg_baseline_itl_ms,
            "avg_projected_itl_ms": self.avg_projected_itl_ms,
            "measured_accel_busy_pct": self.measured_accel_busy_pct,
            "measured_mem_bw_gbs": self.measured_mem_bw_gbs,
            "measured_mem_bw_efficiency_pct": self.measured_mem_bw_efficiency_pct,
            "wall_time_speedup_x": self.wall_time_speedup_x,
            "wall_time_reduction_pct": self.wall_time_reduction_pct,
        }


def _project_stage(
    stage: StageMacroProfile,
    device_type: str,
    baseline_spec: SystemSpec,
    target_spec: SystemSpec,
    assumptions: ProjectionAssumptions,
) -> ProjectedStage:
    # ---- compute-bound prefill ----
    compute_raw = raw_speedup(target_spec.compute_capability(device_type), baseline_spec.compute_capability(device_type))
    compute_eff = effective_speedup(compute_raw, assumptions.compute_efficiency_retention)
    prefill_target = stage.prefill_s / compute_eff

    # ---- memory-bandwidth-bound decode ----
    mem_raw = raw_speedup(target_spec.mem_bw_peak_gbs, baseline_spec.mem_bw_peak_gbs)
    mem_eff = effective_speedup(mem_raw, assumptions.memory_efficiency_retention)
    decode_target = stage.decode_s / mem_eff

    # ---- CPU-bound tool execution (Amdahl) ----
    cores_ratio = raw_speedup(target_spec.cpu_cores, baseline_spec.cpu_cores)
    freq_ratio = raw_speedup(target_spec.cpu_freq_ghz, baseline_spec.cpu_freq_ghz)
    cpu_raw = amdahl_speedup(cores_ratio, freq_ratio, assumptions.tool_parallel_fraction)
    cpu_eff = effective_speedup(cpu_raw, assumptions.cpu_efficiency_retention)
    tool_gap_target = stage.tool_exec_gap_s / cpu_eff

    # ---- fixed, non-resource-bound bookkeeping (unchanged) ----
    overhead_target = stage.stage_overhead_s

    projected_wall_s = prefill_target + decode_target + overhead_target + tool_gap_target
    baseline_wall_s = stage.prefill_s + stage.decode_s + stage.stage_overhead_s + stage.tool_exec_gap_s

    # ---- TTFT/ITL (ms): same per-token decode speedup (mem_eff) drives ITL directly, since
    # decode_target IS itl_ms/1000*output_tokens / mem_eff - i.e. per-token latency scales
    # identically to the aggregate decode bucket it's derived from ----
    projected_itl_ms = stage.itl_ms / mem_eff
    projected_ttft_ms = prefill_target * 1000.0 + projected_itl_ms

    # ---- best-effort power/energy projection (only if RAPL power was measured for this stage) ----
    projected_power_w = {}
    for domain, watts in stage.avg_power_w.items():
        if domain == "cpu":
            ratio = raw_speedup(target_spec.cpu_capability, baseline_spec.cpu_capability)
        elif domain == "igpu":
            ratio = raw_speedup(target_spec.igpu_capability, baseline_spec.igpu_capability)
        elif domain == "npu":
            ratio = raw_speedup(target_spec.npu_capability, baseline_spec.npu_capability)
        else:  # "soc"/uncore rail: assumed roughly fixed regardless of core/EU/MAC count
            ratio = 1.0
        projected_power_w[domain] = watts * (ratio ** assumptions.power_scaling_exponent)

    return ProjectedStage(
        name=stage.name,
        is_cold=stage.is_cold,
        input_tokens=stage.input_tokens,
        output_tokens=stage.output_tokens,
        baseline_wall_s=baseline_wall_s,
        projected_wall_s=projected_wall_s,
        baseline={
            "prefill_s": stage.prefill_s, "decode_s": stage.decode_s,
            "stage_overhead_s": stage.stage_overhead_s, "tool_exec_gap_s": stage.tool_exec_gap_s,
        },
        projected={
            "prefill_s": prefill_target, "decode_s": decode_target,
            "stage_overhead_s": overhead_target, "tool_exec_gap_s": tool_gap_target,
        },
        speedups={"compute": compute_eff, "memory": mem_eff, "cpu_tool_exec": cpu_eff},
        baseline_ttft_ms=stage.ttft_ms,
        projected_ttft_ms=projected_ttft_ms,
        baseline_itl_ms=stage.itl_ms,
        projected_itl_ms=projected_itl_ms,
        tool_calls=stage.tool_calls,
        baseline_power_w=stage.avg_power_w,
        projected_power_w=projected_power_w,
    )


def project(
    baseline_profile: BaselineProfile,
    baseline_spec: SystemSpec,
    target_spec: SystemSpec,
    assumptions: Optional[ProjectionAssumptions] = None,
) -> ProjectionResult:
    assumptions = assumptions or ProjectionAssumptions()
    device_type = baseline_profile.device_type

    projected_stages = [
        _project_stage(s, device_type, baseline_spec, target_spec, assumptions)
        for s in baseline_profile.stages
    ]

    baseline_wall = sum(s.baseline_wall_s for s in projected_stages) + baseline_profile.fixed_overhead_s
    projected_wall = sum(s.projected_wall_s for s in projected_stages) + baseline_profile.fixed_overhead_s

    total_output_tokens = baseline_profile.total_output_tokens()
    baseline_tok_s = total_output_tokens / baseline_wall if baseline_wall else 0.0
    projected_tok_s = total_output_tokens / projected_wall if projected_wall else 0.0

    # Energy/Tokens-per-Joule: only computable if at least one stage has measured RAPL power.
    has_power = any(s.baseline_power_w for s in projected_stages)
    baseline_energy_j = projected_energy_j = None
    baseline_tok_per_j = projected_tok_per_j = None
    if has_power:
        baseline_energy_j = 0.0
        projected_energy_j = 0.0
        for s in projected_stages:
            total_power_b = sum(s.baseline_power_w.values())
            total_power_p = sum(s.projected_power_w.values())
            baseline_energy_j += total_power_b * s.baseline_wall_s
            projected_energy_j += total_power_p * s.projected_wall_s
        baseline_tok_per_j = total_output_tokens / baseline_energy_j if baseline_energy_j else None
        projected_tok_per_j = total_output_tokens / projected_energy_j if projected_energy_j else None

    # Aggregate TTFT/ITL: output-token-weighted mean across stages that actually decoded output
    # (itl_ms > 0) - excludes any degenerate stage with zero output tokens, which would otherwise
    # have itl_ms/ttft_ms == 0. Weighted (not a plain per-stage average) so that a 1000-token SWE
    # turn counts proportionally more than a 44-token warmup call - a straight per-stage mean would
    # give both equal weight and misrepresent what a user actually experienced across the session.
    decoded = [s for s in projected_stages if s.baseline_itl_ms > 0]
    weight_total = sum(s.output_tokens for s in decoded)

    def _weighted_avg(values: List[float]) -> Optional[float]:
        if not decoded:
            return None
        if not weight_total:
            return sum(values) / len(decoded)
        return sum(v * s.output_tokens for v, s in zip(values, decoded)) / weight_total

    avg_baseline_ttft_ms = _weighted_avg([s.baseline_ttft_ms for s in decoded])
    avg_projected_ttft_ms = _weighted_avg([s.projected_ttft_ms for s in decoded])
    avg_baseline_itl_ms = _weighted_avg([s.baseline_itl_ms for s in decoded])
    avg_projected_itl_ms = _weighted_avg([s.projected_itl_ms for s in decoded])

    measured_mem_bw_gbs = baseline_profile.measured_mem_bw_gbs
    measured_mem_bw_efficiency_pct = (
        (measured_mem_bw_gbs / baseline_spec.mem_bw_peak_gbs * 100.0)
        if measured_mem_bw_gbs and baseline_spec.mem_bw_peak_gbs else None
    )

    return ProjectionResult(
        baseline_spec=baseline_spec,
        target_spec=target_spec,
        assumptions=assumptions,
        device_type=device_type,
        model=baseline_profile.model,
        params_b=baseline_profile.params_b,
        stages=projected_stages,
        baseline_wall_time_s=baseline_wall,
        projected_wall_time_s=projected_wall,
        fixed_overhead_s=baseline_profile.fixed_overhead_s,
        total_output_tokens=total_output_tokens,
        baseline_tok_s=baseline_tok_s,
        projected_tok_s=projected_tok_s,
        baseline_energy_j=baseline_energy_j,
        projected_energy_j=projected_energy_j,
        baseline_tok_per_j=baseline_tok_per_j,
        projected_tok_per_j=projected_tok_per_j,
        avg_baseline_ttft_ms=avg_baseline_ttft_ms,
        avg_projected_ttft_ms=avg_projected_ttft_ms,
        avg_baseline_itl_ms=avg_baseline_itl_ms,
        avg_projected_itl_ms=avg_projected_itl_ms,
        measured_accel_busy_pct=baseline_profile.measured_accel_busy_pct,
        measured_mem_bw_gbs=measured_mem_bw_gbs,
        measured_mem_bw_efficiency_pct=measured_mem_bw_efficiency_pct,
    )

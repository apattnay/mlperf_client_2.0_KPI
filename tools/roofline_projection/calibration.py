"""Mine a "clean, repeated fixed-shape trial" run (e.g. a kpi_mode=full preset: many stages,
similar input/output token shapes, no tool-call gaps between them) for a workload-native,
low-noise "best observed achieved" compute/memory reference.

Why this exists (see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md Sec 4.2): hw_samples.csv's
measured_accel_busy_pct/measured_mem_bw_gbs (baseline_extractor.py) come from an external OS-level
sampler at ~1Hz, averaged over a window that mixes prefill+decode+cold-start - useful, but noisier
than deriving the same kind of number directly from the model's own precise per-token timing
(prefill_ms_est/avg_itl_ms), across MANY repeated same-shape trials instead of one mixed run. This
module computes that second, independent estimate so the two can be cross-checked against each
other - it does not replace either the telemetry-based diagnostic or the *_efficiency_retention
knobs (which are still necessarily assumptions about a hypothetical TARGET machine).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .baseline_extractor import extract_baseline
from .hw_spec import SystemSpec


@dataclass
class CalibrationPoint:
    stage: str
    input_tokens: int
    output_tokens: int
    prefill_gflops_s: Optional[float]     # None if this stage had no measurable prefill phase
    decode_achieved_gbs: Optional[float]  # None if this stage had no measurable decode phase


@dataclass
class CalibrationProfile:
    run_dir: str
    model: str
    device_type: str
    params_b: float
    bytes_per_weight: float
    points: List[CalibrationPoint]

    def _decode_values(self) -> List[float]:
        return [p.decode_achieved_gbs for p in self.points if p.decode_achieved_gbs]

    def _prefill_values(self) -> List[float]:
        return [p.prefill_gflops_s for p in self.points if p.prefill_gflops_s]

    @property
    def best_prefill_gflops_s(self) -> Optional[float]:
        vals = self._prefill_values()
        return max(vals) if vals else None

    @property
    def best_decode_achieved_gbs(self) -> Optional[float]:
        vals = self._decode_values()
        return max(vals) if vals else None

    @property
    def median_decode_achieved_gbs(self) -> Optional[float]:
        vals = sorted(self._decode_values())
        if not vals:
            return None
        n, mid = len(vals), len(vals) // 2
        return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    def mem_bw_efficiency_pct(self, spec: SystemSpec, use_best: bool = True) -> Optional[float]:
        """% of `spec`'s theoretical mem_bw_peak_gbs achieved, from this run's own best/median
        decode trial - an independent (non-telemetry) cross-check for measured_mem_bw_gbs."""
        gbs = self.best_decode_achieved_gbs if use_best else self.median_decode_achieved_gbs
        if not gbs or not spec.mem_bw_peak_gbs:
            return None
        return gbs / spec.mem_bw_peak_gbs * 100.0


def build_calibration_profile(run_dir: str) -> CalibrationProfile:
    """Reuses baseline_extractor.extract_baseline() (same prefill_s/decode_s/itl_ms it derives
    for the main projection pipeline, including its fallback for logs without prefill_ms_est/
    avg_itl_ms) - this module just re-expresses those into GFLOPs/s and achieved-GB/s per stage.
    """
    profile = extract_baseline(run_dir)
    params = profile.params_b * 1e9
    points = []
    for s in profile.stages:
        prefill_gflops_s = None
        if s.prefill_s > 0 and s.input_tokens > 0:
            # 2 FLOPs/MAC x params x input_tokens, over the measured prefill time - same formula
            # tools/KPI-hub/generate_kpi_report.py's roofline chart uses for its prefill points.
            prefill_gflops_s = (2 * params * s.input_tokens) / s.prefill_s / 1e9

        decode_achieved_gbs = None
        if s.itl_ms > 0:
            # Decode re-reads the full quantized weight once per token (batch=1) - bytes moved
            # per token / measured per-token latency = achieved bandwidth for that token.
            decode_achieved_gbs = (params * profile.bytes_per_weight) / (s.itl_ms / 1000.0) / 1e9

        points.append(CalibrationPoint(
            stage=s.name, input_tokens=s.input_tokens, output_tokens=s.output_tokens,
            prefill_gflops_s=prefill_gflops_s, decode_achieved_gbs=decode_achieved_gbs,
        ))

    return CalibrationProfile(
        run_dir=profile.run_dir, model=profile.model, device_type=profile.device_type,
        params_b=profile.params_b, bytes_per_weight=profile.bytes_per_weight, points=points,
    )

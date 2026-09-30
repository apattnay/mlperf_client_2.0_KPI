"""CLI entry point: project a measured kpi_runs/ experiment onto a hypothetical target
hardware system and emit an HTML+JSON report (and optionally a standalone interactive
what-if dropdown calculator).

Usage examples:
  # Quick projection with CLI overrides on top of the built-in default baseline spec
  .venv\\Scripts\\python.exe tools\\run_roofline_projection.py --run kpi_runs\\preset5_roofline_20260923_140129 ^
      --target-spec data\\configs\\roofline_targets\\example_heavy_duty_workstation.json --what-if

  # Fully explicit baseline + target spec files (recommended: fill in your real baseline spec once)
  .venv\\Scripts\\python.exe tools\\run_roofline_projection.py --run kpi_runs\\preset5_roofline_20260923_140129 ^
      --baseline-spec data\\configs\\roofline_targets\\current_baseline_TEMPLATE.json ^
      --target-spec data\\configs\\roofline_targets\\example_heavy_duty_workstation.json ^
      --out kpi_runs\\preset5_roofline_20260923_140129\\roofline_projection --what-if

  # Per-domain efficiency-retention overrides (compute/memory/cpu can differ - see the doc's §4.1)
  .venv\\Scripts\\python.exe tools\\run_roofline_projection.py --run kpi_runs\\preset5_roofline_20260923_140129 ^
      --target-spec data\\configs\\roofline_targets\\example_heavy_duty_workstation.json ^
      --compute-efficiency-retention 0.9 --memory-efficiency-retention 0.75 --cpu-efficiency-retention 0.6

See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md for the full methodology.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.roofline_projection.baseline_extractor import extract_baseline
from tools.roofline_projection.hw_spec import SystemSpec
from tools.roofline_projection.scaling_engine import ProjectionAssumptions, project
from tools.roofline_projection.report import save_report
from tools.roofline_projection.what_if_calculator import save_what_if_calculator

_TARGET_OVERRIDE_FIELDS = [
    "cpu_cores", "cpu_freq_ghz", "igpu_xecores", "igpu_freq_ghz",
    "npu_macs", "npu_freq_ghz", "mem_channels", "mem_width_bits", "mem_freq_mts",
]


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="Path to a kpi_runs/<experiment> directory (must contain workflow_kpi.json)")
    p.add_argument("--baseline-spec", help="Path to a SystemSpec JSON describing THIS (measured) machine. Defaults to a generic placeholder - fill in your real spec for accurate projections.")
    p.add_argument("--target-spec", help="Path to a SystemSpec JSON describing the hypothetical target machine. Starts from the baseline spec, then CLI overrides below are applied on top.")
    p.add_argument("--out", help="Output directory for the report (default: <run>/roofline_projection/)")
    p.add_argument("--what-if", action="store_true", help="Also generate a standalone interactive what-if dropdown calculator (what_if_calculator.html)")
    p.add_argument("--presets-dir", default="data/configs/roofline_targets", help="Directory of SystemSpec JSON files to offer as quick-pick presets in the what-if calculator")

    for field in _TARGET_OVERRIDE_FIELDS:
        p.add_argument(f"--{field.replace('_', '-')}", type=float, help=f"Override target_spec.{field}")

    p.add_argument("--efficiency-retention", type=float, help="0-1 convenience shortcut: sets compute/memory/cpu efficiency retention below all at once (default 0.85 if none of the four --*-efficiency-retention flags are given)")
    p.add_argument("--compute-efficiency-retention", type=float, help="0-1, how much of the ideal prefill (NPU/iGPU compute) speedup is realized - overrides --efficiency-retention for this domain only")
    p.add_argument("--memory-efficiency-retention", type=float, help="0-1, how much of the ideal decode (memory bandwidth) speedup is realized - overrides --efficiency-retention for this domain only")
    p.add_argument("--cpu-efficiency-retention", type=float, help="0-1, how much of the ideal tool-exec (CPU, on top of Amdahl's law) speedup is realized - overrides --efficiency-retention for this domain only")
    p.add_argument("--tool-parallel-fraction", type=float, default=0.5, help="0-1, Amdahl parallel fraction for tool-execution time (default 0.5)")
    p.add_argument("--power-scaling-exponent", type=float, default=1.0, help="Exponent for power-vs-capability scaling (default 1.0 = linear)")
    p.add_argument("--use-measured-compute-efficiency", action="store_true", help="Use each stage's own real measured accelerator busy%% (hw_samples.csv) as the compute-domain retention instead of --compute-efficiency-retention, falling back to it where no measurement exists")
    p.add_argument("--use-measured-memory-efficiency", action="store_true", help="Use each stage's own real measured achieved-vs-theoretical-peak DRAM bandwidth (hw_samples.csv) as the memory-domain retention instead of --memory-efficiency-retention, falling back to it where no measurement exists - see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9.6b for why this can matter a lot on real hardware")
    return p


def _load_presets(presets_dir: str) -> list:
    presets = []
    d = Path(presets_dir)
    if not d.exists():
        return presets
    for f in sorted(d.glob("*.json")):
        try:
            presets.append(SystemSpec.from_file(str(f)))
        except Exception as e:
            print(f"warning: skipping preset {f}: {e}", file=sys.stderr)
    return presets


def _warn_on_frequency_mismatch(baseline_profile, baseline_spec) -> None:
    """Flag (never auto-correct) a declared baseline_spec frequency that diverges >=15% from this
    run's own real EMON/PDH-measured average clock - a diagnostic only, same principle as the
    existing measured busy%/mem-BW checks. Motivated by docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md
    §9.6b: plugging a declared/nameplate frequency into compute_capability (EU-count x freq)
    mispredicted this repo's own real 96EU-vs-16EU pair's relative speed by ~15x once real
    measured clocks were substituted in instead - a spec typed from a datasheet can be badly wrong
    for what the silicon actually sustains under real load (DVFS/turbo throttling, power limits).
    """
    def _check(measured_mhz, declared_ghz, label):
        if measured_mhz is None or not declared_ghz:
            return
        declared_mhz = declared_ghz * 1000.0
        diff_pct = abs(measured_mhz - declared_mhz) / declared_mhz * 100.0
        if diff_pct >= 8.0:
            print(
                f"warning: baseline_spec's declared {label} ({declared_mhz:.0f} MHz) diverges from "
                f"this run's real measured average ({measured_mhz:.0f} MHz) by {diff_pct:.0f}% - "
                "consider updating the baseline spec to the measured value, or see "
                "docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9.6b for why a declared/nameplate "
                "frequency can meaningfully mislead the compute_capability ratio.",
                file=sys.stderr,
            )

    device_type = (baseline_profile.device_type or "").upper()
    if device_type in ("GPU", "IGPU"):
        _check(baseline_profile.measured_igpu_freq_mhz, baseline_spec.igpu_freq_ghz, "igpu_freq_ghz")
    _check(baseline_profile.measured_cpu_freq_mhz, baseline_spec.cpu_freq_ghz, "cpu_freq_ghz")


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)

    baseline_profile = extract_baseline(args.run)

    if args.baseline_spec:
        baseline_spec = SystemSpec.from_file(args.baseline_spec)
    else:
        baseline_spec = SystemSpec(name="default_placeholder_baseline (EDIT ME - see --baseline-spec)")
        print(
            "warning: no --baseline-spec given, using a generic placeholder (CPU/iGPU/NPU counts are "
            "NOT auto-detectable). Projection ratios will be wrong unless you supply your real machine's "
            "spec via --baseline-spec. See data/configs/roofline_targets/current_baseline_TEMPLATE.json.",
            file=sys.stderr,
        )

    if args.target_spec:
        target_spec = SystemSpec.from_file(args.target_spec)
    else:
        target_spec = SystemSpec.from_dict(baseline_spec.to_dict())
        target_spec.name = "custom_target"

    for field in _TARGET_OVERRIDE_FIELDS:
        val = getattr(args, field)
        if val is not None:
            setattr(target_spec, field, val)

    base_eff = args.efficiency_retention if args.efficiency_retention is not None else 0.85
    assumptions = ProjectionAssumptions(
        compute_efficiency_retention=args.compute_efficiency_retention if args.compute_efficiency_retention is not None else base_eff,
        memory_efficiency_retention=args.memory_efficiency_retention if args.memory_efficiency_retention is not None else base_eff,
        cpu_efficiency_retention=args.cpu_efficiency_retention if args.cpu_efficiency_retention is not None else base_eff,
        tool_parallel_fraction=args.tool_parallel_fraction,
        power_scaling_exponent=args.power_scaling_exponent,
        use_measured_compute_efficiency=args.use_measured_compute_efficiency,
        use_measured_memory_efficiency=args.use_measured_memory_efficiency,
    )

    _warn_on_frequency_mismatch(baseline_profile, baseline_spec)

    result = project(baseline_profile, baseline_spec, target_spec, assumptions)

    out_dir = args.out or str(Path(args.run) / "roofline_projection")
    save_report(result, out_dir)

    print(f"Baseline wall time:   {result.baseline_wall_time_s:.2f}s")
    print(f"Projected wall time:  {result.projected_wall_time_s:.2f}s")
    print(f"Speedup:              {result.wall_time_speedup_x:.2f}x")
    print(f"Wall time reduction:  {result.wall_time_reduction_pct:.1f}%")
    print(f"Tokens/s (base->proj): {result.baseline_tok_s:.1f} -> {result.projected_tok_s:.1f}")
    if result.baseline_tok_per_j is not None:
        print(f"Tokens/J (base->proj): {result.baseline_tok_per_j:.2f} -> {result.projected_tok_per_j:.2f}")
    if result.measured_accel_busy_pct is not None:
        print(f"Measured baseline accelerator busy%: {result.measured_accel_busy_pct:.1f}% (diagnostic only, see report)")
    if result.measured_mem_bw_efficiency_pct is not None:
        print(
            f"Measured baseline mem BW achieved:    {result.measured_mem_bw_gbs:.1f} GB/s "
            f"({result.measured_mem_bw_efficiency_pct:.1f}% of baseline_spec's theoretical peak)"
        )
    print(f"Report written to:    {out_dir}")

    if args.what_if:
        presets = _load_presets(args.presets_dir)
        what_if_path = str(Path(out_dir) / "what_if_calculator.html")
        save_what_if_calculator(baseline_profile, baseline_spec, presets, what_if_path, assumptions)
        print(f"What-if calculator:   {what_if_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

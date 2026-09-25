"""CLI: mine one or more "clean repeated-trial" runs (kpi_mode=full presets, or any run with
many similar-shape stages) for a workload-native achieved compute/memory reference, and cross-
check it against hw_samples.csv-telemetry-based diagnostics from the SAME run (if present).

See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md Sec 4.2 for the methodology and why this is a
useful SECOND, independent measurement (not a replacement for either the telemetry-based
diagnostic or the --*-efficiency-retention assumptions, which remain necessary for the
hypothetical TARGET side of a projection).

Usage:
  .venv\\Scripts\\python.exe tools\\calibrate_roofline_baseline.py ^
      --run kpi_runs\\preset1_full_npu_20260917_211825 --run kpi_runs\\preset2_full_gpu_20260917_213836 ^
      --baseline-spec data\\configs\\roofline_targets\\current_baseline_TEMPLATE.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.roofline_projection.baseline_extractor import extract_baseline
from tools.roofline_projection.calibration import build_calibration_profile
from tools.roofline_projection.hw_spec import SystemSpec


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", action="append", required=True, help="Path to a kpi_runs/<experiment> directory; repeatable")
    p.add_argument("--baseline-spec", help="SystemSpec JSON to compute mem-BW efficiency %% against (defaults to the generic placeholder spec)")
    return p


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    spec = SystemSpec.from_file(args.baseline_spec) if args.baseline_spec else SystemSpec()

    for run_dir in args.run:
        calib = build_calibration_profile(run_dir)
        telemetry = extract_baseline(run_dir)  # for the same-run hw_samples.csv cross-check

        n_prefill = sum(1 for p in calib.points if p.prefill_gflops_s)
        n_decode = sum(1 for p in calib.points if p.decode_achieved_gbs)

        print(f"=== {run_dir} ({calib.model}, {calib.device_type}) ===")
        print(f"  Stages usable for calibration: {n_prefill} prefill points, {n_decode} decode points (of {len(calib.points)} total)")
        if calib.best_prefill_gflops_s:
            print(f"  Best observed prefill:  {calib.best_prefill_gflops_s:.1f} GFLOPs/s (across {n_prefill} repeated trials)")
        if calib.best_decode_achieved_gbs:
            eff = calib.mem_bw_efficiency_pct(spec, use_best=True)
            med = calib.median_decode_achieved_gbs
            med_eff = calib.mem_bw_efficiency_pct(spec, use_best=False)
            print(f"  Best observed decode:   {calib.best_decode_achieved_gbs:.1f} GB/s "
                  f"({eff:.1f}% of spec's {spec.mem_bw_peak_gbs:.1f} GB/s theoretical peak)" if eff is not None else "")
            if med is not None:
                print(f"  Median observed decode: {med:.1f} GB/s "
                      f"({med_eff:.1f}% of theoretical peak)" if med_eff is not None else "")

        # Cross-check against the SAME run's hw_samples.csv telemetry-based diagnostic, if present.
        if telemetry.measured_mem_bw_gbs is not None:
            tel_eff = (telemetry.measured_mem_bw_gbs / spec.mem_bw_peak_gbs * 100.0) if spec.mem_bw_peak_gbs else None
            print(f"  Telemetry (hw_samples.csv) achieved mem BW: {telemetry.measured_mem_bw_gbs:.1f} GB/s "
                  f"({tel_eff:.1f}% of theoretical peak)" if tel_eff is not None else "")
            if calib.best_decode_achieved_gbs:
                delta_pct = (calib.best_decode_achieved_gbs - telemetry.measured_mem_bw_gbs) / telemetry.measured_mem_bw_gbs * 100.0
                print(f"  -> Calibration vs telemetry delta: {delta_pct:+.1f}% "
                      f"(best-observed-trial vs hw_samples.csv average - a large delta suggests the ~1Hz "
                      f"telemetry sampler is smoothing over real peak/trough swings this coarse-grained)")
        else:
            print("  (no hw_samples.csv telemetry available on this run for a same-run cross-check)")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

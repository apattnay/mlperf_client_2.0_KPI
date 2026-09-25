"""Thin wrapper for roofline-calibration preset generation, execution, and analysis - see
tools/roofline_calibration/presets.py for what each preset isolates and
docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md Sec 4.3 for the full methodology.

This does NOT replace tools/run_kpi_preset.py (production SWE/Data Agent presets) - it's a
separate, purpose-built entry point for roofline-methodology development/calibration presets,
which have a different lifecycle (generated on demand, iterated on, not part of the fixed
production preset list).

Usage:
  # 1) Generate the prompt/config files only (safe, no hardware/network required)
  .venv\\Scripts\\python.exe tools\\roofline_calibration\\run_calibration.py --preset prefill_sweep --device NPU

  # 2) Also run it (requires a real installed mlperf_v2p0 + NPU/iGPU hardware + network access -
  #    this step CANNOT be executed in a sandboxed/CI environment without that hardware)
  .venv\\Scripts\\python.exe tools\\roofline_calibration\\run_calibration.py --preset prefill_sweep --device NPU --run

  # 3) Analyze a completed run's output directory against a SystemSpec
  .venv\\Scripts\\python.exe tools\\roofline_calibration\\run_calibration.py --analyze kpi_runs\\prefill_sweep_npu_<ts>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.roofline_calibration.generate_assets import generate
from tools.roofline_calibration.presets import CALIBRATION_PRESETS
from tools.roofline_projection.baseline_extractor import extract_baseline
from tools.roofline_projection.calibration import build_calibration_profile
from tools.roofline_projection.hw_spec import SystemSpec

RUN_KPI_WORKFLOW = REPO_ROOT / "tools" / "run_kpi_workflow.py"
DEFAULT_MLPERF_DIR = r"C:\Applications\mlperf_client\mlperf_v2p0"


def _analyze(run_dir: str, baseline_spec_path: str = None) -> None:
    spec = SystemSpec.from_file(baseline_spec_path) if baseline_spec_path else SystemSpec()
    calib = build_calibration_profile(run_dir)
    telemetry = extract_baseline(run_dir)

    print(f"=== {run_dir} ({calib.model}, {calib.device_type}) ===")
    if calib.best_prefill_gflops_s:
        print(f"  Best observed prefill:  {calib.best_prefill_gflops_s:.1f} GFLOPs/s")
    if calib.best_decode_achieved_gbs:
        eff = calib.mem_bw_efficiency_pct(spec, use_best=True)
        print(f"  Best observed decode:   {calib.best_decode_achieved_gbs:.1f} GB/s"
              + (f" ({eff:.1f}% of theoretical peak)" if eff is not None else ""))
    if telemetry.measured_mem_bw_gbs is not None:
        tel_eff = (telemetry.measured_mem_bw_gbs / spec.mem_bw_peak_gbs * 100.0) if spec.mem_bw_peak_gbs else None
        print(f"  Telemetry achieved mem BW: {telemetry.measured_mem_bw_gbs:.1f} GB/s"
              + (f" ({tel_eff:.1f}% of theoretical peak)" if tel_eff is not None else ""))
    print()
    print("Per-stage detail:")
    for p in calib.points:
        print(f"  {p.stage:20s} in={p.input_tokens:5d} out={p.output_tokens:5d} "
              f"prefill_gflops_s={p.prefill_gflops_s} decode_gbs={p.decode_achieved_gbs}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=sorted(CALIBRATION_PRESETS), help="Which calibration preset to generate/run")
    ap.add_argument("--device", choices=["NPU", "GPU"], help="Target accelerator for --preset")
    ap.add_argument("--run", action="store_true", help="Also invoke run_kpi_workflow.py (needs real hardware + --mlperf-dir)")
    ap.add_argument("--mlperf-dir", default=DEFAULT_MLPERF_DIR)
    ap.add_argument("--analyze", metavar="RUN_DIR", help="Skip generation; analyze a completed run directory instead")
    ap.add_argument("--baseline-spec", help="SystemSpec JSON for --analyze's mem-BW efficiency %% (default: generic placeholder)")
    args, extra_args = ap.parse_known_args(argv)

    if args.analyze:
        _analyze(args.analyze, args.baseline_spec)
        return 0

    if not args.preset or not args.device:
        ap.error("--preset and --device are required unless using --analyze")

    preset = CALIBRATION_PRESETS[args.preset]
    print(f"Preset: {preset.key} ({preset.confidence} confidence) - isolates {preset.equation}")
    print(f"  {preset.description}")
    config_path = generate(args.preset, args.device)
    print(f"Generated config: {config_path}")

    if not args.run:
        print("(pass --run to also execute it via run_kpi_workflow.py - requires a real installed "
              "mlperf_v2p0 + NPU/iGPU hardware + network access, none of which this generation step needs)")
        return 0

    name = f"{args.preset}_{args.device.lower()}"
    cmd = [sys.executable, str(RUN_KPI_WORKFLOW), "--mlperf-dir", args.mlperf_dir,
           "--config", str(config_path), "--name", name, "--download-behaviour", "normal"]
    cmd += extra_args
    print("Command:", " ".join(cmd))
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())

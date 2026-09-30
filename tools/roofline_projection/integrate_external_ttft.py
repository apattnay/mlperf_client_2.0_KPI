"""Final aggregation: full Wall-Time(s) projection combining an EXTERNAL, real per-n TTFT/prefill
projection (a companion repo's Llama-3.1-8B-Instruct PERF_COUNT-derived compute/memory-split
model, see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9) with THIS repo's own already-validated
decode / tool-execution / stage-overhead macro-component projections (scaling_engine.py).

Why a separate script instead of extending scaling_engine.py directly: the external CSV's
prefill/TTFT numbers were derived on a DIFFERENT machine (a real 96-EU Intel iGPU) with a
DIFFERENT quantization scheme than this repo's own runs - they cannot be substituted in as
absolute milliseconds. Instead, per input length n, the external data's own
(baseline_ms(n) / projected_ms(n)) RATIO is read off and applied as a multiplier to THIS repo's
own measured prefill_s for the stage with that n - the same "only ratios ever cross a baseline/
target boundary" principle hw_spec.py already uses throughout this pipeline (see its module
docstring). decode_s / tool_exec_gap_s / stage_overhead_s are projected exactly as
scaling_engine.py already does, onto a target SystemSpec built from this run's own CLI args.

Full equation (see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9.5 for the boxed form):

    WallTime(s) = sum_i [ prefill_s[i] / ttft_ratio(n_i)  +  decode_s[i] / E_m
                           + tool_exec_gap_s[i] / E_cpu  +  stage_overhead_s[i] ]
                  + fixed_overhead_s

Usage:
  .venv\\Scripts\\python.exe tools\\roofline_projection\\integrate_external_ttft.py `
      --run kpi_runs\\preset6_roofline_20260923_141035 `
      --igpu-xecores 256 --mem-bw-gbs 300 --p-cores 20 --e-cores 48
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.roofline_projection.baseline_extractor import extract_baseline
from tools.roofline_projection.hw_spec import SystemSpec, raw_speedup, amdahl_speedup, effective_speedup
from tools.roofline_projection.scaling_engine import _resolve_retention

# The companion repo's real per-n TTFT projection for THIS exact target (256 EU / 300 GB/s),
# measured on a real 96-EU Intel iGPU running Llama-3.1-8B-Instruct INT4-GRw (see
# docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9). An absolute, external, out-of-workspace path -
# expected to exist only on machines that also have that companion checkout; --external-ttft-csv
# lets any other machine point at wherever it keeps (or regenerates) the same CSV.
_DEFAULT_EXTERNAL_TTFT_CSV = (
    r"C:\portable_agentic_soc_suite\results\evidence\JF04WVAW0867-TA"
    r"\llama31_8b_wall_kv_projection_256eu300gbps.csv"
)


def _load_ttft_ratio_curve(csv_path: str):
    """Read (n, baseline_ms, projected_ms) rows and return a sorted list of (n, ratio) points.

    ratio = baseline_ms / projected_ms (i.e. the speedup the external study found at that n) -
    only rows where both fields are present/positive are usable.
    """
    points = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                n = float(row["n"])
                baseline_ms = float(row["wall_ms_total"])
                projected_ms = float(row["sut_projected_wall_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            if n > 0 and baseline_ms > 0 and projected_ms > 0:
                points.append((n, baseline_ms / projected_ms))
    points.sort(key=lambda p: p[0])
    if not points:
        raise ValueError(f"No usable (n, wall_ms_total, sut_projected_wall_ms) rows found in {csv_path}")
    return points


def _interp_ratio(points, n: float) -> float:
    """Log-log-n linear interpolation of the speedup ratio curve; clamps outside the measured range."""
    if n <= points[0][0]:
        return points[0][1]
    if n >= points[-1][0]:
        return points[-1][1]
    log_n = math.log(n)
    for (n0, r0), (n1, r1) in zip(points, points[1:]):
        if n0 <= n <= n1:
            if n1 == n0:
                return r0
            t = (log_n - math.log(n0)) / (math.log(n1) - math.log(n0))
            return r0 + (r1 - r0) * t
    return points[-1][1]  # unreachable given the clamps above, kept defensive


def _build_target_spec(baseline: SystemSpec, args) -> SystemSpec:
    target = SystemSpec.from_dict(baseline.to_dict())
    target.name = "external_ttft_target"

    target.igpu_xecores = args.igpu_xecores
    if args.igpu_freq_ghz is not None:
        target.igpu_freq_ghz = args.igpu_freq_ghz

    total_cores = args.p_cores + args.e_cores
    target.cpu_cores = total_cores
    if args.cpu_freq_ghz is not None:
        target.cpu_freq_ghz = args.cpu_freq_ghz

    # Keep the baseline's own channel/width topology, solve transfer-rate for the requested
    # peak bandwidth - same physical memory subsystem shape, just faster, rather than picking
    # arbitrary channel/width numbers that happen to multiply out to the right figure.
    target.mem_freq_mts = args.mem_bw_gbs * 1000.0 / (target.mem_channels * (target.mem_width_bits / 8.0))

    target.notes = (
        f"{args.igpu_xecores:g} EU iGPU / {args.mem_bw_gbs:g} GB/s mem / "
        f"{args.p_cores:g}P+{args.e_cores:g}E ({total_cores:g} total) CPU cores - hypothetical, "
        "no such machine has been benchmarked (same caveat as the companion repo's own spec)."
    )
    return target


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="Path to a kpi_runs/<experiment> directory (must contain workflow_kpi.json)")
    p.add_argument("--baseline-spec", help="SystemSpec JSON for THIS (measured) machine. Defaults to the built-in placeholder (96 XeCores/136.5 GB/s), same as every other worked example in the doc.")
    p.add_argument("--external-ttft-csv", default=_DEFAULT_EXTERNAL_TTFT_CSV, help="Path to the companion repo's per-n baseline-vs-projected TTFT CSV")
    p.add_argument("--igpu-xecores", type=float, default=256, help="Target iGPU EU/XeCore count (default 256, matching the external TTFT CSV's own target)")
    p.add_argument("--igpu-freq-ghz", type=float, help="Target iGPU frequency (default: same as baseline - only EU count changes)")
    p.add_argument("--mem-bw-gbs", type=float, default=300, help="Target peak memory bandwidth GB/s (default 300, matching the external TTFT CSV's own target)")
    p.add_argument("--p-cores", type=float, default=20, help="Target P-core count (default 20)")
    p.add_argument("--e-cores", type=float, default=48, help="Target E-core count (default 48)")
    p.add_argument("--cpu-freq-ghz", type=float, help="Target CPU frequency (default: same as baseline - only core count changes)")
    p.add_argument("--memory-efficiency-retention", type=float, default=0.85, help="0-1, decode-bucket efficiency retention (default 0.85)")
    p.add_argument("--cpu-efficiency-retention", type=float, default=0.85, help="0-1, tool-exec-bucket efficiency retention on top of Amdahl's law (default 0.85)")
    p.add_argument("--tool-parallel-fraction", type=float, default=0.5, help="0-1, Amdahl parallel fraction for tool-execution time (default 0.5)")
    p.add_argument("--use-measured-memory-efficiency", action="store_true", help="Use each stage's own real measured achieved-vs-theoretical-peak DRAM bandwidth (hw_samples.csv) as the memory-domain retention instead of --memory-efficiency-retention, falling back to it where no measurement exists - see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §9.6b")
    p.add_argument("--out", help="Output JSON path (default: <run>/roofline_projection/integrated_wall_time_report.json)")
    p.add_argument("--force", action="store_true", help="Proceed even if --run's device_type isn't GPU/iGPU (the external TTFT ratio is iGPU-EU-axis specific - see the warning this would otherwise print)")
    return p


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)

    baseline_spec = SystemSpec.from_file(args.baseline_spec) if args.baseline_spec else SystemSpec()
    target_spec = _build_target_spec(baseline_spec, args)
    baseline_profile = extract_baseline(args.run)
    ttft_curve = _load_ttft_ratio_curve(args.external_ttft_csv)

    # The external CSV's ttft_ratio(n) was derived on a real iGPU (EU-count axis) - applying it to
    # an NPU-baseline run (a physically different compute axis, MAC count) has no defensible basis,
    # same principle as hw_spec.py's "no cross-accelerator projection" (see doc §5/§9.5). Warn loudly
    # and flag it in the report rather than silently guessing, instead of raising - this is a
    # directional what-if tool, not a hard requirement, and --force lets a caller override knowingly.
    device_type_axis_mismatch = baseline_profile.device_type.upper() not in ("GPU", "IGPU")
    if device_type_axis_mismatch and not args.force:
        print(
            f"error: --run's device_type is {baseline_profile.device_type!r}, but the external TTFT "
            "ratio (--external-ttft-csv) was derived on a real iGPU (EU-count compute axis) - applying "
            "it to an NPU-driven prefill (a different physical axis, MAC count) has no defensible basis. "
            "Pass --force to proceed anyway (the report will be flagged accordingly).",
            file=sys.stderr,
        )
        return 1
    if device_type_axis_mismatch:
        print(
            f"warning: --force set - applying an iGPU-EU-axis TTFT ratio to a {baseline_profile.device_type} "
            "baseline anyway. Treat prefill_target_s as illustrative only, not physically defensible.",
            file=sys.stderr,
        )

    mem_raw = raw_speedup(target_spec.mem_bw_peak_gbs, baseline_spec.mem_bw_peak_gbs)
    cores_ratio = raw_speedup(target_spec.cpu_cores, baseline_spec.cpu_cores)
    freq_ratio = raw_speedup(target_spec.cpu_freq_ghz, baseline_spec.cpu_freq_ghz)
    cpu_raw = amdahl_speedup(cores_ratio, freq_ratio, args.tool_parallel_fraction)
    cpu_eff = effective_speedup(cpu_raw, args.cpu_efficiency_retention)

    stage_rows = []
    total_baseline_s = 0.0
    total_projected_s = 0.0
    for stage in baseline_profile.stages:
        ttft_ratio = _interp_ratio(ttft_curve, max(stage.input_tokens, 1))
        prefill_target_s = stage.prefill_s / ttft_ratio if ttft_ratio > 0 else stage.prefill_s
        measured_bw = stage.measured_util.get("mem_bw_gbs")
        measured_mem_fraction = (measured_bw / baseline_spec.mem_bw_peak_gbs) if (measured_bw and baseline_spec.mem_bw_peak_gbs) else None
        mem_retention = _resolve_retention(measured_mem_fraction, args.memory_efficiency_retention, args.use_measured_memory_efficiency)
        mem_eff = effective_speedup(mem_raw, mem_retention)
        decode_target_s = stage.decode_s / mem_eff
        tool_target_s = stage.tool_exec_gap_s / cpu_eff
        overhead_target_s = stage.stage_overhead_s

        baseline_wall_s = stage.prefill_s + stage.decode_s + stage.tool_exec_gap_s + stage.stage_overhead_s
        projected_wall_s = prefill_target_s + decode_target_s + tool_target_s + overhead_target_s
        total_baseline_s += baseline_wall_s
        total_projected_s += projected_wall_s

        stage_rows.append({
            "name": stage.name,
            "input_tokens": stage.input_tokens,
            "output_tokens": stage.output_tokens,
            "ttft_ratio_applied": ttft_ratio,
            "mem_eff_applied": mem_eff,
            "mem_retention_applied": mem_retention,
            "baseline": {
                "prefill_s": stage.prefill_s, "decode_s": stage.decode_s,
                "tool_exec_gap_s": stage.tool_exec_gap_s, "stage_overhead_s": stage.stage_overhead_s,
                "wall_s": baseline_wall_s,
            },
            "projected": {
                "prefill_s": prefill_target_s, "decode_s": decode_target_s,
                "tool_exec_gap_s": tool_target_s, "stage_overhead_s": overhead_target_s,
                "wall_s": projected_wall_s,
            },
        })

    total_baseline_s += baseline_profile.fixed_overhead_s
    total_projected_s += baseline_profile.fixed_overhead_s
    total_output_tokens = baseline_profile.total_output_tokens()
    mem_effs = [row["mem_eff_applied"] for row in stage_rows]
    mem_eff_summary = {
        "min": min(mem_effs), "max": max(mem_effs), "mean": sum(mem_effs) / len(mem_effs),
    } if mem_effs else {}

    result = {
        "run": args.run,
        "device_type": baseline_profile.device_type,
        "device_type_axis_mismatch": device_type_axis_mismatch,
        "model": baseline_profile.model,
        "external_ttft_csv": args.external_ttft_csv,
        "baseline_spec": baseline_spec.to_dict(),
        "target_spec": target_spec.to_dict(),
        "use_measured_memory_efficiency": args.use_measured_memory_efficiency,
        "domain_speedups": {"memory": mem_eff_summary, "cpu_tool_exec": cpu_eff},
        "fixed_overhead_s": baseline_profile.fixed_overhead_s,
        "stages": stage_rows,
        "baseline_wall_time_s": total_baseline_s,
        "projected_wall_time_s": total_projected_s,
        "wall_time_speedup_x": (total_baseline_s / total_projected_s) if total_projected_s else 1.0,
        "wall_time_reduction_pct": (
            (total_baseline_s - total_projected_s) / total_baseline_s * 100.0 if total_baseline_s else 0.0
        ),
        "total_output_tokens": total_output_tokens,
        "baseline_tok_s": (total_output_tokens / total_baseline_s) if total_baseline_s else 0.0,
        "projected_tok_s": (total_output_tokens / total_projected_s) if total_projected_s else 0.0,
    }

    out_path = Path(args.out) if args.out else Path(args.run) / "roofline_projection" / "integrated_wall_time_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"Run: {args.run}  ({baseline_profile.device_type}, {baseline_profile.model})")
    print(f"External TTFT curve: {args.external_ttft_csv}")
    print(f"Target: {target_spec.igpu_xecores:g} EU / {target_spec.mem_bw_peak_gbs:.1f} GB/s / "
          f"{args.p_cores:g}P+{args.e_cores:g}E ({target_spec.cpu_cores:g}) CPU cores")
    if args.use_measured_memory_efficiency:
        print(f"Domain speedups applied -> memory: {mem_eff_summary['min']:.3f}x-{mem_eff_summary['max']:.3f}x "
              f"(per-stage, from real measured DRAM bandwidth)   cpu/tool-exec: {cpu_eff:.3f}x  (per-stage TTFT ratio varies with n)")
    else:
        print(f"Domain speedups applied -> memory: {mem_eff_summary.get('mean', 1.0):.3f}x   cpu/tool-exec: {cpu_eff:.3f}x  (per-stage TTFT ratio varies with n)")
    print()
    # tool_exec_s columns made explicit here (base/proj) - previously only visible in the JSON,
    # not the console summary, which understated how much of the projected total it accounts for.
    header = (f"{'stage':<20} {'n_in':>7} {'ttft_x':>8} "
              f"{'base_s':>9} {'proj_s':>9} {'base_tool_s':>12} {'proj_tool_s':>12}")
    print(header)
    print("-" * len(header))
    total_baseline_tool_s = 0.0
    total_projected_tool_s = 0.0
    for row in stage_rows:
        base_tool_s = row["baseline"]["tool_exec_gap_s"]
        proj_tool_s = row["projected"]["tool_exec_gap_s"]
        total_baseline_tool_s += base_tool_s
        total_projected_tool_s += proj_tool_s
        print(f"{row['name']:<20} {row['input_tokens']:>7} {row['ttft_ratio_applied']:>8.2f} "
              f"{row['baseline']['wall_s']:>9.2f} {row['projected']['wall_s']:>9.2f} "
              f"{base_tool_s:>12.2f} {proj_tool_s:>12.2f}")
    print("-" * len(header))
    print(f"{'TOTAL (+fixed_overhead_s)':<20} {'':>7} {'':>8} "
          f"{total_baseline_s:>9.2f} {total_projected_s:>9.2f} "
          f"{total_baseline_tool_s:>12.2f} {total_projected_tool_s:>12.2f}")
    print()
    tool_share_baseline_pct = (total_baseline_tool_s / total_baseline_s * 100.0) if total_baseline_s else 0.0
    tool_share_projected_pct = (total_projected_tool_s / total_projected_s * 100.0) if total_projected_s else 0.0
    print(f"Tool-exec time (explicit component, Amdahl-scaled): "
          f"{total_baseline_tool_s:.2f}s ({tool_share_baseline_pct:.1f}% of baseline wall) -> "
          f"{total_projected_tool_s:.2f}s ({tool_share_projected_pct:.1f}% of projected wall)")
    print(f"Wall-Time(s) speedup: {result['wall_time_speedup_x']:.2f}x   "
          f"reduction: {result['wall_time_reduction_pct']:.1f}%   "
          f"Tokens/s: {result['baseline_tok_s']:.2f} -> {result['projected_tok_s']:.2f}")
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

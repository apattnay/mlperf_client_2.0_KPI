"""Render an HTML + JSON projection report comparing a measured baseline run against a
projected target-hardware profile (stacked-bar macro-component breakdown per stage, spec
comparison table, and summary KPI cards). Visual style intentionally matches
tools/KPI-hub/generate_kpi_report.py's dark theme for consistency across reports.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .scaling_engine import ProjectionResult

_CSS = """
:root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text2: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
       background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5; }
h1 { font-size: 1.6rem; margin-bottom: 4px; }
h2 { font-size: 1.15rem; color: var(--accent); margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
.header { margin-bottom: 24px; }
.header .subtitle { color: var(--text2); font-size: 0.9rem; }
.section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 20px; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 4px; }
.card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; min-width: 170px; flex: 1; }
.card .label { color: var(--text2); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }
.card .value { font-size: 1.4rem; font-weight: 600; margin-top: 2px; }
.card .value.green { color: var(--green); }
.card .sub { color: var(--text2); font-size: 0.78rem; margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th { text-align: left; color: var(--text2); font-weight: 500; padding: 8px 10px; border-bottom: 2px solid var(--border); }
td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
td.num { text-align: right; font-variant-numeric: tabular-nums; font-family: 'SF Mono', Consolas, monospace; }
tr:hover td { background: rgba(88,166,255,0.06); }
.note { color: var(--text2); font-size: 0.8rem; margin-top: 8px; }
"""


def _fmt_s(v: float) -> str:
    return f"{v:.2f}s"


def _spec_rows(baseline_spec, target_spec) -> str:
    fields = [
        ("CPU cores", "cpu_cores", ""), ("CPU freq (GHz)", "cpu_freq_ghz", ""),
        ("iGPU XeCores", "igpu_xecores", ""), ("iGPU freq (GHz)", "igpu_freq_ghz", ""),
        ("NPU MACs", "npu_macs", ""), ("NPU freq (GHz)", "npu_freq_ghz", ""),
        ("Memory channels", "mem_channels", ""), ("Memory width (bits)", "mem_width_bits", ""),
        ("Memory transfer rate (MT/s)", "mem_freq_mts", ""),
    ]
    rows = []
    for label, attr, _ in fields:
        b = getattr(baseline_spec, attr)
        t = getattr(target_spec, attr)
        ratio = (t / b) if b else 1.0
        rows.append(
            f"<tr><td>{label}</td><td class='num'>{b:g}</td><td class='num'>{t:g}</td>"
            f"<td class='num'>{ratio:.2f}x</td></tr>"
        )
    rows.append(
        f"<tr><td>Peak memory BW (derived)</td><td class='num'>{baseline_spec.mem_bw_peak_gbs:.1f} GB/s</td>"
        f"<td class='num'>{target_spec.mem_bw_peak_gbs:.1f} GB/s</td>"
        f"<td class='num'>{(target_spec.mem_bw_peak_gbs/baseline_spec.mem_bw_peak_gbs if baseline_spec.mem_bw_peak_gbs else 1.0):.2f}x</td></tr>"
    )
    return "".join(rows)


def _stage_chart_html(result: ProjectionResult) -> str:
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p class='note'>plotly not installed - stage breakdown chart skipped.</p>"

    names = [s.name for s in result.stages]
    components = ["prefill_s", "decode_s", "tool_exec_gap_s", "stage_overhead_s"]
    labels = {"prefill_s": "Prefill (compute)", "decode_s": "Decode (memory)",
              "tool_exec_gap_s": "Tool exec (CPU)", "stage_overhead_s": "Overhead (fixed)"}
    colors = {"prefill_s": "#58a6ff", "decode_s": "#ff9838", "tool_exec_gap_s": "#a371f7", "stage_overhead_s": "#8b949e"}

    fig = go.Figure()
    for group, dataset in (("Baseline", "baseline"), ("Projected", "projected")):
        for comp in components:
            fig.add_trace(go.Bar(
                x=[f"{n}<br><span style='font-size:0.7rem'>{group}</span>" for n in names],
                y=[getattr(s, dataset)[comp] for s in result.stages],
                name=labels[comp], marker_color=colors[comp],
                legendgroup=comp, showlegend=(group == "Baseline"),
            ))
    fig.update_layout(
        barmode="stack", template="plotly_dark", height=460,
        margin=dict(l=50, r=20, t=20, b=90),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(22,27,34,1)",
        yaxis_title="Wall time (s)",
    )
    return fig.to_html(include_plotlyjs="cdn", full_html=False)


def build_report_html(result: ProjectionResult) -> str:
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    power_cards = ""
    if result.baseline_tok_per_j is not None:
        power_cards = f"""
        <div class="card"><div class="label">Tokens/Joule (baseline)</div><div class="value">{result.baseline_tok_per_j:.2f}</div></div>
        <div class="card"><div class="label">Tokens/Joule (projected)</div><div class="value green">{result.projected_tok_per_j:.2f}</div></div>"""

    latency_cards = ""
    if result.avg_baseline_ttft_ms is not None:
        latency_cards = f"""
        <div class="card"><div class="label">Avg TTFT (baseline)</div><div class="value">{result.avg_baseline_ttft_ms:.0f} ms</div></div>
        <div class="card"><div class="label">Avg TTFT (projected)</div><div class="value green">{result.avg_projected_ttft_ms:.0f} ms</div></div>
        <div class="card"><div class="label">Avg ITL (baseline)</div><div class="value">{result.avg_baseline_itl_ms:.1f} ms</div></div>
        <div class="card"><div class="label">Avg ITL (projected)</div><div class="value green">{result.avg_projected_itl_ms:.1f} ms</div></div>"""

    measured_cards = ""
    if result.measured_accel_busy_pct is not None or result.measured_mem_bw_gbs is not None:
        busy_row = (
            f"<div class='card'><div class='label'>Measured accelerator busy%</div>"
            f"<div class='value'>{result.measured_accel_busy_pct:.1f}%</div>"
            f"<div class='sub'>avg during active LLM windows</div></div>"
        ) if result.measured_accel_busy_pct is not None else ""
        bw_row = (
            f"<div class='card'><div class='label'>Measured mem BW achieved</div>"
            f"<div class='value'>{result.measured_mem_bw_gbs:.1f} GB/s</div>"
            f"<div class='sub'>{result.measured_mem_bw_efficiency_pct:.1f}% of baseline_spec's theoretical peak</div></div>"
        ) if result.measured_mem_bw_gbs is not None else ""
        measured_cards = f"""
<div class="section">
<h2>Measured Baseline Efficiency (real telemetry, diagnostic only)</h2>
<p class="note">Straight from this run's own <code>hw_samples.csv</code> - NOT used in the projection math above
(a baseline machine's own achieved efficiency doesn't tell you what a different target machine will achieve).
Use it to sanity-check the <code>--compute/--memory/--cpu-efficiency-retention</code> assumptions below against
reality instead of guessing. See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §4.1.</p>
<div class="cards">{busy_row}{bw_row}</div>
</div>"""

    stage_rows = "".join(
        f"<tr><td>{html.escape(s.name)}</td>"
        f"<td class='num'>{_fmt_s(s.baseline_wall_s)}</td><td class='num'>{_fmt_s(s.projected_wall_s)}</td>"
        f"<td class='num'>{(s.baseline_wall_s/s.projected_wall_s if s.projected_wall_s else 1.0):.2f}x</td>"
        f"<td class='num'>{s.speedups['compute']:.2f}x</td><td class='num'>{s.speedups['memory']:.2f}x</td>"
        f"<td class='num'>{s.speedups['cpu_tool_exec']:.2f}x</td>"
        f"<td class='num'>{s.baseline_ttft_ms:.0f} &rarr; {s.projected_ttft_ms:.0f}</td>"
        f"<td class='num'>{s.baseline_itl_ms:.1f} &rarr; {s.projected_itl_ms:.1f}</td></tr>"
        for s in result.stages
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roofline HW Projection Report — {report_time}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="header">
    <h1>Roofline Hardware Projection Report</h1>
    <div class="subtitle">Generated {report_time} &nbsp;|&nbsp; Model: <code>{html.escape(result.model)}</code>
    ({result.params_b:.1f}B params, device: {html.escape(result.device_type)})</div>
</div>

<div class="section">
<h2>Summary</h2>
<div class="cards">
    <div class="card"><div class="label">Baseline wall time</div><div class="value">{_fmt_s(result.baseline_wall_time_s)}</div></div>
    <div class="card"><div class="label">Projected wall time</div><div class="value green">{_fmt_s(result.projected_wall_time_s)}</div></div>
    <div class="card"><div class="label">Speedup</div><div class="value green">{result.wall_time_speedup_x:.2f}x</div></div>
    <div class="card"><div class="label">Wall time reduction</div><div class="value green">{result.wall_time_reduction_pct:.1f}%</div></div>
    <div class="card"><div class="label">Tokens/s (baseline)</div><div class="value">{result.baseline_tok_s:.1f}</div></div>
    <div class="card"><div class="label">Tokens/s (projected)</div><div class="value green">{result.projected_tok_s:.1f}</div></div>
    {latency_cards}
    {power_cards}
</div>
<p class="note">Assumptions: compute efficiency retention={result.assumptions.compute_efficiency_retention:.2f},
memory efficiency retention={result.assumptions.memory_efficiency_retention:.2f},
cpu efficiency retention={result.assumptions.cpu_efficiency_retention:.2f}, tool-exec parallel
fraction={result.assumptions.tool_parallel_fraction:.2f}, power scaling exponent={result.assumptions.power_scaling_exponent:.2f}.
See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md for the full equations.</p>
</div>
{measured_cards}

<div class="section">
<h2>Target Hardware Spec vs. Baseline ({html.escape(result.baseline_spec.name)} &rarr; {html.escape(result.target_spec.name)})</h2>
<table><tr><th>Resource</th><th style="text-align:right">Baseline</th><th style="text-align:right">Target</th><th style="text-align:right">Ratio</th></tr>
{_spec_rows(result.baseline_spec, result.target_spec)}</table>
</div>

<div class="section">
<h2>Per-Stage Macro-Component Breakdown (Baseline vs. Projected)</h2>
{_stage_chart_html(result)}
<table><tr><th>Stage</th><th style="text-align:right">Baseline (s)</th><th style="text-align:right">Projected (s)</th>
<th style="text-align:right">Speedup</th><th style="text-align:right">Compute&nbsp;x</th><th style="text-align:right">Memory&nbsp;x</th><th style="text-align:right">CPU&nbsp;x</th>
<th style="text-align:right">TTFT (ms)</th><th style="text-align:right">ITL (ms)</th></tr>
{stage_rows}</table>
<p class="note">Compute/Memory/CPU columns are the per-stage EFFECTIVE speedups actually applied (after damping by
efficiency retention) to prefill (compute-bound), decode (memory-bound), and tool-execution (CPU-bound, Amdahl's
law) time respectively. Stage overhead (fixed bookkeeping) is not scaled. TTFT/ITL columns show baseline &rarr;
projected values in milliseconds (ITL = per-token decode latency, scales with the same memory-bandwidth ratio as
the decode bucket it's derived from).</p>
</div>

<div style="color:var(--text2); font-size:0.75rem; text-align:center; margin-top:20px;">
Generated by tools/roofline_projection — projection only, not a measured result.
</div>
</body>
</html>"""


def save_report(result: ProjectionResult, out_dir: str) -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "roofline_projection_report.html").write_text(build_report_html(result), encoding="utf-8")
    (out_path / "roofline_projection_report.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )

"""Generate a standalone, self-contained interactive "what-if" HW dropdown calculator.

Single HTML file, no server/backend needed: embeds one baseline run's macro-component
profile (from baseline_extractor.py) plus the baseline SystemSpec, and re-implements the
exact same scaling-engine math (hw_spec.py / scaling_engine.py) in vanilla JS so the user
can pick CPU cores / iGPU XeCores / NPU MACs / memory bandwidth from dropdowns and see the
projected wall time, speedup, tokens/s and tokens/Joule update live, client-side.

IMPORTANT: keep the JS math below in sync with hw_spec.py + scaling_engine.py if those
change - there is intentionally no shared code between Python and JS here (this file must
work standalone, opened directly in a browser with no build step or server).
"""
from __future__ import annotations

import json
from pathlib import Path

from .baseline_extractor import BaselineProfile
from .hw_spec import SystemSpec
from .scaling_engine import ProjectionAssumptions

_CPU_CORE_OPTIONS = [4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128]
_CPU_FREQ_OPTIONS = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
_XECORE_OPTIONS = [16, 32, 48, 64, 96, 128, 160, 256, 384, 512]
_XE_FREQ_OPTIONS = [1.0, 1.4, 1.8, 2.0, 2.4, 2.8, 3.2]
_NPU_MAC_OPTIONS = [512, 1024, 2048, 4096, 8192, 16384, 32768]
_NPU_FREQ_OPTIONS = [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
_MEM_CHANNEL_OPTIONS = [1, 2, 4, 8, 12, 16]
_MEM_WIDTH_OPTIONS = [16, 32, 64, 128]
_MEM_FREQ_OPTIONS = [3200, 4800, 6400, 7200, 8000, 8533, 9600, 10667, 12000, 12800]

_CSS = """
:root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text2: #8b949e; --accent: #58a6ff; --green: #3fb950;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5; }
h1 { font-size: 1.5rem; margin-bottom: 4px; }
h2 { font-size: 1.05rem; color: var(--accent); margin-bottom: 10px; }
.subtitle { color: var(--text2); font-size: 0.88rem; margin-bottom: 20px; }
.section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
label { display: block; color: var(--text2); font-size: 0.78rem; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.4px; }
select, input[type=range] { width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--border);
       border-radius: 6px; padding: 6px 8px; font-size: 0.9rem; }
button { background: var(--accent); color: #0d1117; border: none; border-radius: 6px; padding: 8px 14px;
       font-size: 0.85rem; font-weight: 600; cursor: pointer; }
button:hover { opacity: 0.85; }
.field-value { font-size: 0.75rem; color: var(--accent); margin-top: 2px; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 6px; }
.card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; min-width: 170px; flex: 1; }
.card .label { color: var(--text2); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.5px; }
.card .value { font-size: 1.5rem; font-weight: 600; margin-top: 2px; }
.card .value.green { color: var(--green); }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 10px; }
th { text-align: left; color: var(--text2); font-weight: 500; padding: 6px 8px; border-bottom: 2px solid var(--border); }
td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
td.num { text-align: right; font-variant-numeric: tabular-nums; font-family: 'SF Mono', Consolas, monospace; }
.note { color: var(--text2); font-size: 0.78rem; margin-top: 10px; }
.preset-row { margin-bottom: 16px; }
.accel-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 14px; }
.accel-group { border: 1px solid var(--border); border-radius: 8px; padding: 12px; transition: opacity 0.15s; }
.accel-group-title { font-size: 0.8rem; color: var(--text2); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.4px; }
.accel-badge { text-transform: none; letter-spacing: normal; font-size: 0.72rem; padding: 1px 7px; border-radius: 3px; margin-left: 4px; }
.accel-badge.active { background: rgba(63,185,80,0.18); color: #56d364; }
.accel-badge.inactive { background: rgba(139,148,158,0.18); color: var(--text2); }
.accel-group.is-inactive { opacity: 0.45; }
.accel-group.is-inactive select { cursor: not-allowed; }
"""

# NOTE: keep in sync with hw_spec.py / scaling_engine.py (see module docstring).
_JS_ENGINE = r"""
function memBwPeakGbs(spec) {
    return spec.mem_channels * (spec.mem_width_bits / 8.0) * spec.mem_freq_mts / 1000.0;
}
function cpuCapability(spec) { return spec.cpu_cores * spec.cpu_freq_ghz; }
function igpuCapability(spec) { return spec.igpu_xecores * spec.igpu_freq_ghz; }
function npuCapability(spec) { return spec.npu_macs * spec.npu_freq_ghz; }
function computeCapability(spec, deviceType) {
    return deviceType === "NPU" ? npuCapability(spec) : igpuCapability(spec);
}
function rawSpeedup(target, baseline) { return baseline ? (target / baseline) : 1.0; }
function amdahlSpeedup(coresRatio, freqRatio, parallelFraction) {
    const perCore = Math.max(freqRatio, 1e-9);
    const serial = 1.0 - parallelFraction;
    const denom = serial + parallelFraction / (coresRatio * perCore);
    return denom <= 0 ? coresRatio * perCore : 1.0 / denom;
}
function effectiveSpeedup(raw, retention) {
    retention = Math.min(Math.max(retention, 0.0), 1.0);
    return 1.0 + (raw - 1.0) * retention;
}

function projectStage(stage, deviceType, baselineSpec, targetSpec, assumptions) {
    const computeRaw = rawSpeedup(computeCapability(targetSpec, deviceType), computeCapability(baselineSpec, deviceType));
    const computeEff = effectiveSpeedup(computeRaw, assumptions.efficiency_retention);
    const prefillT = stage.prefill_s / computeEff;

    const memRaw = rawSpeedup(memBwPeakGbs(targetSpec), memBwPeakGbs(baselineSpec));
    const memEff = effectiveSpeedup(memRaw, assumptions.efficiency_retention);
    const decodeT = stage.decode_s / memEff;

    const coresRatio = rawSpeedup(targetSpec.cpu_cores, baselineSpec.cpu_cores);
    const freqRatio = rawSpeedup(targetSpec.cpu_freq_ghz, baselineSpec.cpu_freq_ghz);
    const cpuRaw = amdahlSpeedup(coresRatio, freqRatio, assumptions.tool_parallel_fraction);
    const cpuEff = effectiveSpeedup(cpuRaw, assumptions.efficiency_retention);
    const toolT = stage.tool_exec_gap_s / cpuEff;

    const overheadT = stage.stage_overhead_s;
    const projectedWall = prefillT + decodeT + overheadT + toolT;
    const baselineWall = stage.prefill_s + stage.decode_s + stage.stage_overhead_s + stage.tool_exec_gap_s;
    return { name: stage.name, baselineWall, projectedWall, computeEff, memEff, cpuEff,
             prefillT, decodeT, toolT, overheadT, output_tokens: stage.output_tokens };
}

function projectAll(profile, baselineSpec, targetSpec, assumptions) {
    const stages = profile.stages.map(s => projectStage(s, profile.device_type, baselineSpec, targetSpec, assumptions));
    const baselineWall = stages.reduce((a, s) => a + s.baselineWall, 0) + profile.fixed_overhead_s;
    const projectedWall = stages.reduce((a, s) => a + s.projectedWall, 0) + profile.fixed_overhead_s;
    const totalTokens = stages.reduce((a, s) => a + s.output_tokens, 0);
    return {
        stages, baselineWall, projectedWall, totalTokens,
        baselineTokS: baselineWall ? totalTokens / baselineWall : 0,
        projectedTokS: projectedWall ? totalTokens / projectedWall : 0,
        speedup: projectedWall ? baselineWall / projectedWall : 1.0,
        reductionPct: baselineWall ? (baselineWall - projectedWall) / baselineWall * 100.0 : 0.0,
    };
}
"""


def _options_html(select_id: str, values, selected_value, suffix: str = "", extra_values=()) -> str:
    """`extra_values` (e.g. every preset's value for this field) is unioned in so that JS-driven
    `select.value = x` assignments (from applyPresetToDropdowns) always match an existing
    <option> - a <select> silently ignores .value assignments with no matching option, which
    would otherwise leave the field blank (-> NaN) whenever a preset uses a value not already
    in the hardcoded quick-pick list. The `value` attribute uses the same `:g` minimal-digit
    formatting JS's `Number.prototype.toString()` produces (e.g. 5.0 -> "5"), so a JS-assigned
    numeric value (from JSON, always a plain number) round-trips to the exact same string as
    the option's `value` attribute - using Python's default float str() ("5.0") would mismatch
    and silently fail to select anything.
    """
    opts = "".join(
        f'<option value="{v:g}"{" selected" if abs(v - selected_value) < 1e-9 else ""}>{v:g}{suffix}</option>'
        for v in sorted(set(values) | set(extra_values) | {selected_value})
    )
    return f'<select id="{select_id}">{opts}</select>'


def build_what_if_html(
    profile: BaselineProfile,
    baseline_spec: SystemSpec,
    presets: list,
    default_assumptions: ProjectionAssumptions = None,
) -> str:
    default_assumptions = default_assumptions or ProjectionAssumptions()

    profile_json = json.dumps(profile.to_dict())
    baseline_spec_json = json.dumps(baseline_spec.to_dict())
    presets_json = json.dumps([p.to_dict() for p in presets])

    def preset_values(attr):
        return [getattr(p, attr) for p in presets]

    cpu_cores_sel = _options_html("cpuCores", _CPU_CORE_OPTIONS, baseline_spec.cpu_cores, extra_values=preset_values("cpu_cores"))
    cpu_freq_sel = _options_html("cpuFreq", _CPU_FREQ_OPTIONS, baseline_spec.cpu_freq_ghz, " GHz", preset_values("cpu_freq_ghz"))
    xecores_sel = _options_html("igpuXecores", _XECORE_OPTIONS, baseline_spec.igpu_xecores, extra_values=preset_values("igpu_xecores"))
    xefreq_sel = _options_html("igpuFreq", _XE_FREQ_OPTIONS, baseline_spec.igpu_freq_ghz, " GHz", preset_values("igpu_freq_ghz"))
    npu_macs_sel = _options_html("npuMacs", _NPU_MAC_OPTIONS, baseline_spec.npu_macs, extra_values=preset_values("npu_macs"))
    npu_freq_sel = _options_html("npuFreq", _NPU_FREQ_OPTIONS, baseline_spec.npu_freq_ghz, " GHz", preset_values("npu_freq_ghz"))
    mem_ch_sel = _options_html("memChannels", _MEM_CHANNEL_OPTIONS, baseline_spec.mem_channels, extra_values=preset_values("mem_channels"))
    mem_width_sel = _options_html("memWidth", _MEM_WIDTH_OPTIONS, baseline_spec.mem_width_bits, "-bit", preset_values("mem_width_bits"))
    mem_freq_sel = _options_html("memFreq", _MEM_FREQ_OPTIONS, baseline_spec.mem_freq_mts, " MT/s", preset_values("mem_freq_mts"))

    preset_options = "".join(
        f'<option value="{i}">{p.name}</option>' for i, p in enumerate(presets)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roofline What-If HW Calculator</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Roofline What-If Hardware Calculator</h1>
<div class="subtitle">Baseline run: <code>{profile.run_dir}</code> &mdash; {profile.model} ({profile.params_b:.1f}B, {profile.device_type})
&mdash; pick a target system below to see the projected wall time / throughput / energy efficiency, live.</div>

<div class="section">
<h2>Target System</h2>
<div class="preset-row">
<label>Quick preset</label>
<select id="presetSelect"><option value="-1">Custom (use dropdowns below)</option>{preset_options}</select>
</div>
<div class="grid">
<div><label>CPU cores</label>{cpu_cores_sel}</div>
<div><label>CPU frequency</label>{cpu_freq_sel}</div>
<div><label>Memory channels</label>{mem_ch_sel}</div>
<div><label>Memory bus width/channel</label>{mem_width_sel}</div>
<div><label>Memory transfer rate</label>{mem_freq_sel}</div>
</div>
<div class="accel-groups">
<div class="accel-group" id="npuGroup">
<div class="accel-group-title">NPU compute <span id="npuGroupBadge" class="accel-badge"></span></div>
<div class="grid">
<div><label>NPU MAC units</label>{npu_macs_sel}</div>
<div><label>NPU frequency</label>{npu_freq_sel}</div>
</div>
</div>
<div class="accel-group" id="igpuGroup">
<div class="accel-group-title">iGPU compute <span id="igpuGroupBadge" class="accel-badge"></span></div>
<div class="grid">
<div><label>iGPU XeCores</label>{xecores_sel}</div>
<div><label>iGPU frequency</label>{xefreq_sel}</div>
</div>
</div>
</div>
<div class="grid" style="margin-top:14px;">
<div><label>Efficiency retention: <span id="effRetentionVal"></span></label>
<input type="range" id="effRetention" min="0" max="1" step="0.05" value="{default_assumptions.efficiency_retention}"></div>
<div><label>Tool-exec parallel fraction: <span id="parFracVal"></span></label>
<input type="range" id="parFrac" min="0" max="1" step="0.05" value="{default_assumptions.tool_parallel_fraction}"></div>
</div>
<div class="note">Target memory bandwidth (derived): <b id="memBwOut"></b> GB/s &nbsp;|&nbsp; Baseline: {baseline_spec.mem_bw_peak_gbs:.1f} GB/s</div>
</div>

<div class="section">
<h2>Projected Result</h2>
<div class="cards">
    <div class="card"><div class="label">Baseline wall time</div><div class="value" id="baselineWallOut"></div></div>
    <div class="card"><div class="label">Projected wall time</div><div class="value green" id="projectedWallOut"></div></div>
    <div class="card"><div class="label">Speedup</div><div class="value green" id="speedupOut"></div></div>
    <div class="card"><div class="label">Wall time reduction</div><div class="value green" id="reductionOut"></div></div>
    <div class="card"><div class="label">Tokens/s (baseline &rarr; projected)</div><div class="value" id="tokSOut"></div></div>
</div>
<table><thead><tr><th>Stage</th><th style="text-align:right">Baseline (s)</th><th style="text-align:right">Projected (s)</th>
<th style="text-align:right">Speedup</th><th style="text-align:right">Compute&nbsp;x</th><th style="text-align:right">Memory&nbsp;x</th><th style="text-align:right">CPU&nbsp;x</th></tr></thead>
<tbody id="stageTableBody"></tbody></table>
<p class="note">Bottom-up macro-component projection: prefill scales with accelerator compute capability
(MACs/XeCores &times; frequency), decode scales with memory bandwidth, tool execution scales via Amdahl's law
with CPU cores/frequency, fixed overhead is unchanged. See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md.</p>
</div>

<div class="section">
<h2>Generate a Persisted Report</h2>
<p class="note">This page is a live, in-browser preview only - it can't write files itself. Use one of the
buttons below to take your current dropdown selection back to the CLI and generate a real
<code>roofline_projection_report.html</code>/<code>.json</code> for it (add <code>--what-if</code> to also
refresh this calculator with the new selection baked in as its default).</p>
<div class="grid">
<div><button id="downloadSpecBtn" type="button">Download target_spec.json</button></div>
<div><button id="copyCliBtn" type="button">Copy CLI command</button></div>
</div>
<p class="note" id="exportStatus">&nbsp;</p>
<pre id="cliPreview" style="white-space:pre-wrap;word-break:break-all;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin-top:6px;font-size:0.78rem;color:var(--text2);"></pre>
</div>

<script>
const PROFILE = {profile_json};
const BASELINE_SPEC = {baseline_spec_json};
const PRESETS = {presets_json};
{_JS_ENGINE}

function readTargetSpec() {{
    return {{
        cpu_cores: parseFloat(document.getElementById('cpuCores').value),
        cpu_freq_ghz: parseFloat(document.getElementById('cpuFreq').value),
        igpu_xecores: parseFloat(document.getElementById('igpuXecores').value),
        igpu_freq_ghz: parseFloat(document.getElementById('igpuFreq').value),
        npu_macs: parseFloat(document.getElementById('npuMacs').value),
        npu_freq_ghz: parseFloat(document.getElementById('npuFreq').value),
        mem_channels: parseFloat(document.getElementById('memChannels').value),
        mem_width_bits: parseFloat(document.getElementById('memWidth').value),
        mem_freq_mts: parseFloat(document.getElementById('memFreq').value),
    }};
}}

function applyPresetToDropdowns(spec) {{
    document.getElementById('cpuCores').value = spec.cpu_cores;
    document.getElementById('cpuFreq').value = spec.cpu_freq_ghz;
    document.getElementById('igpuXecores').value = spec.igpu_xecores;
    document.getElementById('igpuFreq').value = spec.igpu_freq_ghz;
    document.getElementById('npuMacs').value = spec.npu_macs;
    document.getElementById('npuFreq').value = spec.npu_freq_ghz;
    document.getElementById('memChannels').value = spec.mem_channels;
    document.getElementById('memWidth').value = spec.mem_width_bits;
    document.getElementById('memFreq').value = spec.mem_freq_mts;
}}

// This baseline run's device_type determines which ONE of the NPU/iGPU dropdown groups actually
// feeds the prefill compute-scaling math (see computeCapability() above) - the other group has no
// effect on any output number for this run, so grey it out + disable it instead of leaving it
// silently inert (that ambiguity is exactly what caused user confusion before this was added).
function markActiveAccelGroup() {{
    const npuActive = PROFILE.device_type === "NPU";
    const groups = [
        {{ id: 'npuGroup', badgeId: 'npuGroupBadge', active: npuActive, inputs: ['npuMacs', 'npuFreq'] }},
        {{ id: 'igpuGroup', badgeId: 'igpuGroupBadge', active: !npuActive, inputs: ['igpuXecores', 'igpuFreq'] }},
    ];
    for (const g of groups) {{
        document.getElementById(g.id).classList.toggle('is-inactive', !g.active);
        const badge = document.getElementById(g.badgeId);
        badge.textContent = g.active ? 'active for this run' : 'not used - this run used ' + PROFILE.device_type;
        badge.className = 'accel-badge ' + (g.active ? 'active' : 'inactive');
        for (const inputId of g.inputs) {{
            document.getElementById(inputId).disabled = !g.active;
        }}
    }}
}}

function recompute() {{
    const target = readTargetSpec();
    const assumptions = {{
        efficiency_retention: parseFloat(document.getElementById('effRetention').value),
        tool_parallel_fraction: parseFloat(document.getElementById('parFrac').value),
    }};
    document.getElementById('effRetentionVal').textContent = assumptions.efficiency_retention.toFixed(2);
    document.getElementById('parFracVal').textContent = assumptions.tool_parallel_fraction.toFixed(2);
    document.getElementById('memBwOut').textContent = memBwPeakGbs(target).toFixed(1);

    const r = projectAll(PROFILE, BASELINE_SPEC, target, assumptions);
    document.getElementById('baselineWallOut').textContent = r.baselineWall.toFixed(1) + 's';
    document.getElementById('projectedWallOut').textContent = r.projectedWall.toFixed(1) + 's';
    document.getElementById('speedupOut').textContent = r.speedup.toFixed(2) + 'x';
    document.getElementById('reductionOut').textContent = r.reductionPct.toFixed(1) + '%';
    document.getElementById('tokSOut').textContent = r.baselineTokS.toFixed(1) + ' \u2192 ' + r.projectedTokS.toFixed(1);

    const tbody = document.getElementById('stageTableBody');
    tbody.innerHTML = r.stages.map(s => `<tr><td>${{s.name}}</td>
        <td class="num">${{s.baselineWall.toFixed(2)}}</td><td class="num">${{s.projectedWall.toFixed(2)}}</td>
        <td class="num">${{(s.baselineWall / (s.projectedWall || 1)).toFixed(2)}}x</td>
        <td class="num">${{s.computeEff.toFixed(2)}}x</td><td class="num">${{s.memEff.toFixed(2)}}x</td>
        <td class="num">${{s.cpuEff.toFixed(2)}}x</td></tr>`).join('');

    document.getElementById('cliPreview').textContent = buildCliCommand(target, assumptions);
}}

// ---- Export current selection back to the CLI, so it can produce a persisted report/JSON ----
// (this page is a live in-browser preview only - it has no filesystem/server access to write one itself)
// NOTE: uses forward slashes even though this is a Windows tool - python.exe accepts them fine on
// Windows, and it sidesteps JS template-literal backslash-escaping pitfalls (an unrecognized
// escape like backslash-S silently drops the backslash - a double backslash would be needed instead).
function buildCliCommand(target, assumptions) {{
    return `.venv/Scripts/python.exe tools/run_roofline_projection.py --run "${{PROFILE.run_dir}}" ` +
        `--cpu-cores ${{target.cpu_cores}} --cpu-freq-ghz ${{target.cpu_freq_ghz}} ` +
        `--igpu-xecores ${{target.igpu_xecores}} --igpu-freq-ghz ${{target.igpu_freq_ghz}} ` +
        `--npu-macs ${{target.npu_macs}} --npu-freq-ghz ${{target.npu_freq_ghz}} ` +
        `--mem-channels ${{target.mem_channels}} --mem-width-bits ${{target.mem_width_bits}} --mem-freq-mts ${{target.mem_freq_mts}} ` +
        `--efficiency-retention ${{assumptions.efficiency_retention}} --tool-parallel-fraction ${{assumptions.tool_parallel_fraction}} --what-if`;
}}

function downloadTargetSpecJson() {{
    const target = readTargetSpec();
    const spec = Object.assign({{ name: "custom_what_if_selection", notes: "Exported from what_if_calculator.html" }}, target);
    const blob = new Blob([JSON.stringify(spec, null, 2)], {{ type: "application/json" }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = "target_spec_custom.json";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    document.getElementById('exportStatus').textContent =
        `Downloaded target_spec_custom.json - pass it to --target-spec, e.g.: ` +
        `.venv/Scripts/python.exe tools/run_roofline_projection.py --run "${{PROFILE.run_dir}}" ` +
        `--target-spec <downloads-folder>/target_spec_custom.json --what-if`;
}}

function copyCliCommand() {{
    const target = readTargetSpec();
    const assumptions = {{
        efficiency_retention: parseFloat(document.getElementById('effRetention').value),
        tool_parallel_fraction: parseFloat(document.getElementById('parFrac').value),
    }};
    const cmd = buildCliCommand(target, assumptions);
    const done = (ok) => {{
        document.getElementById('exportStatus').textContent = ok
            ? "Copied - paste into a terminal at the repo root (with the .venv activated) to regenerate the report."
            : "Clipboard access blocked - the command is shown in the box below, copy it manually.";
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(cmd).then(() => done(true)).catch(() => done(false));
    }} else {{
        done(false);
    }}
}}

document.getElementById('downloadSpecBtn').addEventListener('click', downloadTargetSpecJson);
document.getElementById('copyCliBtn').addEventListener('click', copyCliCommand);

document.getElementById('presetSelect').addEventListener('change', (e) => {{
    const idx = parseInt(e.target.value);
    if (idx >= 0) {{ applyPresetToDropdowns(PRESETS[idx]); recompute(); }}
}});
document.querySelectorAll('select, input[type=range]').forEach(el => {{
    if (el.id !== 'presetSelect') el.addEventListener('input', recompute);
}});
markActiveAccelGroup();
recompute();
</script>
</body>
</html>"""


def save_what_if_calculator(
    profile: BaselineProfile,
    baseline_spec: SystemSpec,
    presets: list,
    out_path: str,
    default_assumptions: ProjectionAssumptions = None,
) -> None:
    Path(out_path).write_text(
        build_what_if_html(profile, baseline_spec, presets, default_assumptions), encoding="utf-8"
    )

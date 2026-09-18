#!/usr/bin/env python3
"""
Interactive heterogeneous device utilization dashboard.

Generates an interactive HTML file with Plotly for:
- Zoom/pan on any axis
- Hover to see exact values per sample
- Range selector (last 30s, 1m, 5m, all)
- Crosshair cursor
- Per-core utilization and frequency with P-core/E-core color coding

Usage:
    python plot_utilization_interactive.py [--input outputs/utilization_samples.csv] [--output outputs/utilization_dashboard.html]
"""
import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from agentic_instrumentation.dashboard_controls import inject_dashboard_controls

# Phase colors for agent timeline overlay
PHASE_COLORS = {
    "task_agent":              ("rgba(76,114,176,0.12)", "rgba(76,114,176,0.7)"),
    "analysis_agent":          ("rgba(85,168,104,0.15)", "rgba(85,168,104,0.8)"),
    "summary_agent_1":         ("rgba(196,78,82,0.15)",  "rgba(196,78,82,0.8)"),
    "summary_agent_2":         ("rgba(129,114,178,0.15)", "rgba(129,114,178,0.8)"),
    "executive_summary_agent": ("rgba(204,185,116,0.15)", "rgba(204,185,116,0.8)"),
}
DEFAULT_PHASE_COLOR = ("rgba(100,181,246,0.12)", "rgba(100,181,246,0.7)")


def load_phases(kpi_path: str) -> list:
    """Load agent phase intervals from workflow_kpi.json.

    Returns list of (name, start_dt, end_dt, fill_color, line_color) sorted by start.
    """
    with open(kpi_path) as f:
        kpi = json.load(f)
    phases = []
    # Try stages dict (flat format: stages[name] has start_iso/end_iso)
    for name, data in kpi.get("stages", {}).items():
        start_iso = data.get("start_iso")
        end_iso = data.get("end_iso")
        if not start_iso or not end_iso:
            continue
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        fill, line = PHASE_COLORS.get(name, DEFAULT_PHASE_COLOR)
        phases.append((name, start_dt, end_dt, fill, line))
    # Try timeline.phases list (hw_benchmark format)
    if not phases:
        for entry in kpi.get("timeline", {}).get("phases", []):
            name = entry.get("name", "")
            start_iso = entry.get("start_iso")
            end_iso = entry.get("end_iso")
            if not start_iso or not end_iso:
                continue
            start_dt = datetime.fromisoformat(start_iso)
            end_dt = datetime.fromisoformat(end_iso)
            fill, line = PHASE_COLORS.get(name, DEFAULT_PHASE_COLOR)
            phases.append((name, start_dt, end_dt, fill, line))
    phases.sort(key=lambda p: p[1])
    return phases


# KV-cache size per token, by model family (analytical estimate, not device-measured):
#   bytes/token = 2 (K+V) * num_hidden_layers * num_key_value_heads * head_dim * dtype_bytes
# Llama-3.1-8B: 32 layers, 8 KV heads (GQA), head_dim 128, fp16 KV cache (2 bytes) ->
#   2*32*8*128*2 = 131072 bytes/token = 0.125 MB/token.
KV_CACHE_MB_PER_TOKEN = [
    ("Llama-3.1-8B", 0.125),
]


def _kv_cache_mb_per_token(model_name: str) -> float | None:
    for prefix, mb_per_token in KV_CACHE_MB_PER_TOKEN:
        if prefix in (model_name or ""):
            return mb_per_token
    return None


def load_phase_tokens(kpi_path: str) -> tuple:
    """Load per-stage token counts + timing for the KV-cache size estimate.

    Returns (mb_per_token, [{start_dt, end_dt, ttft_s, input_tokens, output_tokens}, ...]),
    or (None, []) if the model isn't in KV_CACHE_MB_PER_TOKEN or there's no stage data.
    """
    with open(kpi_path) as f:
        kpi = json.load(f)
    mb_per_token = _kv_cache_mb_per_token(kpi.get("model", ""))
    if mb_per_token is None:
        return None, []
    stages = []
    for data in kpi.get("stages", {}).values():
        start_iso = data.get("start_iso")
        end_iso = data.get("end_iso")
        if not start_iso or not end_iso:
            continue
        stages.append({
            "start_dt": datetime.fromisoformat(start_iso),
            "end_dt": datetime.fromisoformat(end_iso),
            "ttft_s": data.get("ttft_s", 0) or 0,
            "input_tokens": data.get("input_tokens", 0),
            "output_tokens": data.get("output_tokens", 0),
        })
    stages.sort(key=lambda s: s["start_dt"])
    return mb_per_token, stages


def estimate_kv_cache_series(ts, mb_per_token: float, stages: list):
    """Estimate KV-cache size (MB) at each sample timestamp.

    Within a stage: jumps to input_tokens right after TTFT (prefill), then ramps linearly
    from input_tokens to input_tokens+output_tokens over the remaining decode time. This
    mirrors the prefill-spike-then-decode-ramp shape already used elsewhere for phase shading.
    """
    import numpy as np

    values = np.zeros(len(ts))
    for i, t in enumerate(ts):
        for s in stages:
            if s["start_dt"] <= t <= s["end_dt"]:
                total_s = (s["end_dt"] - s["start_dt"]).total_seconds()
                elapsed = (t - s["start_dt"]).total_seconds()
                if elapsed <= s["ttft_s"] or s["output_tokens"] == 0:
                    tokens = s["input_tokens"]
                else:
                    decode_total = max(total_s - s["ttft_s"], 1e-6)
                    decode_elapsed = min(elapsed - s["ttft_s"], decode_total)
                    tokens = s["input_tokens"] + s["output_tokens"] * (decode_elapsed / decode_total)
                values[i] = tokens * mb_per_token
                break
    return values


def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, on_bad_lines="skip")
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])

    # Coerce numeric columns (corrupted rows may leave strings)
    skip = {"timestamp", "cpu_cores_csv", "cpu_freq_csv", "bw_source"}
    for col in df.columns:
        if col not in skip and df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Unpack per-core CSV columns
    if "cpu_cores_csv" in df.columns:
        core_lists = df["cpu_cores_csv"].apply(
            lambda x: [int(v) for v in str(x).split(";")] if pd.notna(x) else []
        )
        max_cores = core_lists.apply(len).max()
        for i in range(max_cores):
            df[f"cpu_core{i}_pct"] = core_lists.apply(
                lambda lst, idx=i: lst[idx] if idx < len(lst) else 0
            )

    if "cpu_freq_csv" in df.columns:
        freq_lists = df["cpu_freq_csv"].apply(
            lambda x: [int(v) for v in str(x).split(";")] if pd.notna(x) else []
        )
        max_freq_cores = freq_lists.apply(len).max()
        for i in range(max_freq_cores):
            df[f"cpu_freq{i}_mhz"] = freq_lists.apply(
                lambda lst, idx=i: lst[idx] if idx < len(lst) else 0
            )

    return df


def classify_cores(df: pd.DataFrame) -> tuple:
    """Classify P-cores vs E-cores based on frequency patterns."""
    freq_cols = sorted(
        [c for c in df.columns if re.match(r"cpu_freq(\d+)_mhz$", c)],
        key=lambda c: int(re.match(r"cpu_freq(\d+)_mhz$", c).group(1)),
    )
    if not freq_cols:
        return set(), set()

    medians = {i: df[col].median() for i, col in enumerate(freq_cols)}
    if not medians:
        return set(), set()

    sorted_freqs = sorted(medians.values())
    threshold = sorted_freqs[len(sorted_freqs) // 2] * 1.15

    p_cores = {i for i, freq in medians.items() if freq >= threshold}
    e_cores = {i for i, freq in medians.items() if freq < threshold}
    return p_cores, e_cores


def build_dashboard(df: pd.DataFrame, output_path: str, phases: list | None = None, kv_cache_phases: tuple | None = None):
    ts = df["timestamp"]
    duration_min = (ts.iloc[-1] - ts.iloc[0]).total_seconds() / 60
    n_samples = len(df)

    # Only match numbered per-core columns (exclude cpu_core_min, cpu_freq_min_mhz etc.)
    core_cols = sorted(
        [c for c in df.columns if re.match(r"cpu_core(\d+)_pct$", c)],
        key=lambda c: int(re.match(r"cpu_core(\d+)_pct$", c).group(1)),
    )
    freq_cols = sorted(
        [c for c in df.columns if re.match(r"cpu_freq(\d+)_mhz$", c)],
        key=lambda c: int(re.match(r"cpu_freq(\d+)_mhz$", c).group(1)),
    )
    has_rapl = "rapl_cpu_w" in df.columns
    has_nvidia = "nvidia_gpu_pct" in df.columns and df["nvidia_gpu_pct"].sum() > 0
    has_bw = "dram_total_gbs" in df.columns and df["dram_total_gbs"].sum() > 0
    has_l0 = "l0_gpu_busy" in df.columns and df["l0_gpu_busy"].notna().any() and df["l0_gpu_busy"].sum() > 0
    has_npu = "npu_pct" in df.columns or ("rapl_npu_w" in df.columns and df["rapl_npu_w"].notna().any())
    has_igpu = "igpu_pct" in df.columns
    has_dram_latency = "dram_rd_latency_ns" in df.columns and df["dram_rd_latency_ns"].notna().any()
    has_npu_infer = "npu_infer_ms" in df.columns and df["npu_infer_ms"].sum() > 0
    has_kv_cache_ovms = "ovms_kv_cache_pct" in df.columns and df["ovms_kv_cache_pct"].notna().any() and df["ovms_kv_cache_pct"].sum() > 0
    kv_cache_est = None
    if kv_cache_phases:
        mb_per_token, kv_stages = kv_cache_phases
        if mb_per_token is not None and kv_stages:
            kv_cache_est = estimate_kv_cache_series(ts, mb_per_token, kv_stages)
            if kv_cache_est.sum() <= 0:
                kv_cache_est = None
    has_kv_cache = has_kv_cache_ovms or kv_cache_est is not None

    p_cores, e_cores = classify_cores(df)

    # Determine panel count
    n_panels = 2  # CPU summary + RAM
    if core_cols:
        n_panels += 1
    if freq_cols:
        n_panels += 1
    if has_igpu:
        n_panels += 1
    if has_l0:
        n_panels += 2  # EU activity + GPU memory BW
    if has_npu:
        n_panels += 1
    if has_npu_infer:
        n_panels += 1
    if has_nvidia:
        n_panels += 1
    if has_rapl:
        n_panels += 1
    if has_bw:
        n_panels += 1
    if has_dram_latency:
        n_panels += 1
    if has_kv_cache:
        n_panels += 1

    # --- Detect actual data sources from CSV columns ---
    has_emon = any(c.startswith("emon_") or c == "imc_freq_ghz" for c in df.columns)
    has_sysman = any(c.startswith("zes_") for c in df.columns)
    bw_src_val = ""
    if "bw_source" in df.columns:
        bw_vals = df["bw_source"].dropna().unique()
        bw_src_val = bw_vals[0] if len(bw_vals) > 0 else ""

    # DRAM latency/page-hit source: detect from bw_source or EMON columns
    dram_detail_src = "EMON/SEP (Uncore IMC PMU)" if has_emon or bw_src_val == "emon" else "PCM (Uncore IMC PMU)"

    # Power source: PDH reads Intel RAPL via OS driver; EMON can also provide
    rapl_src = "PDH Energy Meter (Intel RAPL)"
    if has_emon and any(c == "emon_pkg_power_w" for c in df.columns):
        rapl_src = "PDH Energy Meter (Intel RAPL) + EMON/SEP"

    titles = ["CPU Utilization — source: psutil (OS perf counters)"]
    if core_cols:
        titles.append(f"Per-Core Utilization ({len(core_cols)} cores) — source: psutil")
    if freq_cols:
        titles.append("Per-Core Frequency — source: PDH (Processor Information\\Actual Frequency)")
    if has_igpu:
        titles.append("Intel iGPU Utilization & Power — source: PDH GPU Engine Counters + RAPL")
    if has_l0:
        titles.append("iGPU EU Activity — source: Level Zero Metrics API (OA/TBS HW Counters)")
        titles.append("iGPU Memory BW & Frequency — source: Level Zero Metrics API (OA/TBS)")
    if has_npu:
        # NPU uses two sources: PDH GPU Engine (engtype_Neural via WDDM/NPU UMD) for
        # engine utilization + memory, and App /metrics for app-level busy% + infer latency.
        # PDH is the fallback when App /metrics is unavailable. Power is from PDH RAPL.
        npu_has_app = has_npu_infer or ("npu_pct" in df.columns and df["npu_pct"].sum() > 0)
        if npu_has_app:
            npu_src = "PDH GPU Engine (WDDM engtype_Neural) + App /metrics + RAPL"
        else:
            npu_src = "PDH GPU Engine (WDDM engtype_Neural) + RAPL"
        titles.append(f"Intel NPU Utilization & Power — source: {npu_src}")
    if has_npu_infer:
        titles.append("NPU Inference Latency — source: npu_embedding_server /metrics")
    if has_nvidia:
        titles.append("NVIDIA GPU — source: NVML (nvidia-smi + pynvml)")
    if has_rapl:
        nv_suffix = " + nvidia-smi" if has_nvidia else ""
        titles.append(f"Power — source: {rapl_src}{nv_suffix}")
    if has_bw:
        if bw_src_val == "emon":
            titles.append("DRAM Bandwidth — source: EMON/SEP (UNC_M_CAS_COUNT via PMU)")
        elif bw_src_val == "pcm":
            titles.append("DRAM Bandwidth — source: PCM (Uncore IMC PMU)")
        elif bw_src_val == "proxy":
            titles.append("DRAM Bandwidth — source: RAPL Power Proxy Estimation")
        else:
            titles.append(f"DRAM Bandwidth — source: {bw_src_val or 'unknown'}")
    if has_dram_latency:
        titles.append(f"DRAM Read Latency & Page Hit — source: {dram_detail_src}")
    if has_kv_cache:
        if has_kv_cache_ovms and kv_cache_est is not None:
            titles.append("KV-Cache Size — source: OVMS Runtime Log + Analytical Estimate")
        elif has_kv_cache_ovms:
            titles.append("KV-Cache Usage — source: OVMS Runtime Log (llm_executor)")
        else:
            titles.append("KV-Cache Size (Estimated) — source: token count × model architecture")
    titles.append("System RAM — source: psutil (OS WMI)")

    fig = make_subplots(
        rows=n_panels, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        subplot_titles=titles,
        specs=[[{"secondary_y": True}]] * n_panels,
    )

    # Restyle subplot titles: left-aligned, smaller, outside the plot area
    for ann in fig.layout.annotations:
        ann.update(
            font=dict(size=11, color="rgba(80,80,80,0.9)"),
            xanchor="left",
            x=0.0,
        )

    row = 1

    # Track per-core trace indices for group toggle buttons
    _trace_base = 0  # will be set before per-core panels
    _p_core_indices = []
    _e_core_indices = []
    _p_freq_indices = []
    _e_freq_indices = []

    # ── Panel 1: CPU Total ──
    fig.add_trace(go.Scatter(
        x=ts, y=df["cpu_total_pct"],
        mode="lines", name="CPU Total %",
        line=dict(color="steelblue", width=1.5),
        hovertemplate="CPU Total: %{y:.1f}%<extra></extra>",
    ), row=row, col=1)

    if "cpu_core_min" in df.columns:
        fig.add_trace(go.Scatter(
            x=ts, y=df["cpu_core_max"],
            mode="lines", name="Core Max",
            line=dict(width=0), showlegend=False,
            hovertemplate="Core Max: %{y}%<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["cpu_core_min"],
            mode="lines", name="Core Min-Max band",
            fill="tonexty", fillcolor="rgba(255,165,0,0.15)",
            line=dict(width=0),
            hovertemplate="Core Min: %{y}%<extra></extra>",
        ), row=row, col=1)

    fig.update_yaxes(range=[0, 105], title_text="CPU %", row=row, col=1)
    row += 1

    # ── Panel 2: Per-Core Utilization ──
    if core_cols:
        n_p = len(p_cores)
        n_e = len(e_cores)
        for i, col in enumerate(core_cols):
            trace_idx = len(fig.data)
            is_p = i in p_cores
            color = f"rgba(70,130,180,0.6)" if is_p else f"rgba(255,140,0,0.5)"
            name = f"P-Core {i}" if is_p else f"E-Core {i}"
            grp = "P-cores" if is_p else "E-cores"
            # Show group title only on first trace of each group
            grp_title = None
            if is_p and p_cores and i == min(p_cores):
                grp_title = dict(text=f"P-Cores ({n_p})")
            elif not is_p and e_cores and i == min(e_cores):
                grp_title = dict(text=f"E-Cores ({n_e})")
            fig.add_trace(go.Scatter(
                x=ts, y=df[col],
                mode="lines", name=name,
                line=dict(color=color, width=0.7),
                legendgroup=grp,
                legendgrouptitle=grp_title,
                showlegend=True,
                hovertemplate=f"Core {i}: " + "%{y}%<extra></extra>",
            ), row=row, col=1)
            if is_p:
                _p_core_indices.append(trace_idx)
            else:
                _e_core_indices.append(trace_idx)

        fig.update_yaxes(range=[0, 105], title_text="Core %", row=row, col=1)
        row += 1

    # ── Panel 3: Per-Core Frequency ──
    if freq_cols:
        for i, col in enumerate(freq_cols):
            trace_idx = len(fig.data)
            is_p = i in p_cores
            color = f"rgba(70,130,180,0.7)" if is_p else f"rgba(255,140,0,0.6)"
            name = f"P{i} freq" if is_p else f"E{i} freq"
            grp = "P-freq" if is_p else "E-freq"
            grp_title = None
            if is_p and p_cores and i == min(p_cores):
                grp_title = dict(text=f"P-Core Freq ({len(p_cores)})")
            elif not is_p and e_cores and i == min(e_cores):
                grp_title = dict(text=f"E-Core Freq ({len(e_cores)})")
            fig.add_trace(go.Scatter(
                x=ts, y=df[col],
                mode="lines", name=name,
                line=dict(color=color, width=0.7),
                legendgroup=grp,
                legendgrouptitle=grp_title,
                showlegend=True,
                hovertemplate=f"Core {i}: " + "%{y} MHz<extra></extra>",
            ), row=row, col=1)
            if is_p:
                _p_freq_indices.append(trace_idx)
            else:
                _e_freq_indices.append(trace_idx)

        if "cpu_freq_min_mhz" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["cpu_freq_max_mhz"],
                mode="lines", name="Freq Max",
                line=dict(width=0), showlegend=False,
            ), row=row, col=1)
            fig.add_trace(go.Scatter(
                x=ts, y=df["cpu_freq_min_mhz"],
                mode="lines", name="Freq range",
                fill="tonexty", fillcolor="rgba(128,128,128,0.1)",
                line=dict(width=0), showlegend=False,
            ), row=row, col=1)

        fig.update_yaxes(title_text="MHz", row=row, col=1)
        row += 1

    # ── Panel: iGPU Utilization & Power (PDH Engine Counters) ──
    if has_igpu:
        fig.add_trace(go.Scatter(
            x=ts, y=df["igpu_pct"],
            mode="lines", name="iGPU Engine % (PDH)",
            fill="tozeroy", fillcolor="rgba(34,139,34,0.15)",
            line=dict(color="forestgreen", width=2),
            hovertemplate="iGPU: %{y:.1f}%<extra></extra>",
        ), row=row, col=1)
        if "rapl_igpu_w" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["rapl_igpu_w"],
                mode="lines", name="iGPU Power (W)",
                line=dict(color="gray", width=1, dash="dash"),
                hovertemplate="iGPU Power: %{y:.1f}W<extra></extra>",
            ), row=row, col=1, secondary_y=True)
        if "igpu_shared_mb" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["igpu_shared_mb"],
                mode="lines", name="iGPU Shared Mem (MB)",
                line=dict(color="steelblue", width=1, dash="dot"),
                hovertemplate="Shared Mem: %{y:.0f} MB<extra></extra>",
            ), row=row, col=1, secondary_y=True)
        fig.update_yaxes(range=[0, 105], title_text="Utilization %", secondary_y=False, row=row, col=1)
        fig.update_yaxes(title_text="Power (W) / Mem (MB)", secondary_y=True, row=row, col=1)
        # Explain zero PDH data when L0 has real activity
        if has_l0 and df["igpu_pct"].sum() == 0:
            fig.add_annotation(
                x=0.5, y=0.5,
                text=("PDH \"GPU Engine\" counters read 0% — Level Zero workloads bypass PDH.<br>"
                      "<b>See iGPU EU Activity panel below</b> for real HW counters (GPU_BUSY, XVE metrics)."),
                showarrow=False, font=dict(size=12, color="rgba(85,85,85,0.8)"),
                xref="x domain", yref="y domain",
                row=row, col=1,
            )
        row += 1

    # ── Panel: iGPU EU Activity (Level Zero HW Counters) ──
    if has_l0:
        fig.add_trace(go.Scatter(
            x=ts, y=df["l0_gpu_busy"],
            mode="lines", name="GPU Busy %",
            line=dict(color="black", width=2),
            hovertemplate="GPU Busy: %{y:.1f}%<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["l0_xve_active"],
            mode="lines", name="XVE Active % (computing)",
            fill="tozeroy", fillcolor="rgba(34,139,34,0.2)",
            line=dict(color="forestgreen", width=1.5),
            hovertemplate="XVE Active: %{y:.1f}%<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["l0_xve_stall"],
            mode="lines", name="XVE Stall % (memory-bound)",
            fill="tozeroy", fillcolor="rgba(220,20,60,0.15)",
            line=dict(color="crimson", width=1.5),
            hovertemplate="XVE Stall: %{y:.1f}%<extra></extra>",
        ), row=row, col=1)
        if "l0_xve_occupancy" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["l0_xve_occupancy"],
                mode="lines", name="XVE Occupancy %",
                line=dict(color="mediumpurple", width=1, dash="dot"),
                hovertemplate="Occupancy: %{y:.1f}%<extra></extra>",
            ), row=row, col=1)
        if "l0_l3_stall" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["l0_l3_stall"],
                mode="lines", name="L3 Stall %",
                line=dict(color="darkorange", width=1, dash="dash"),
                hovertemplate="L3 Stall: %{y:.1f}%<extra></extra>",
            ), row=row, col=1)
            # L3 Cache Efficiency = 100 - L3_STALL (inverse of stall = effective hit/throughput)
            l3_eff = 100 - pd.to_numeric(df["l0_l3_stall"], errors="coerce").fillna(0)
            fig.add_trace(go.Scatter(
                x=ts, y=l3_eff,
                mode="lines", name="L3 Efficiency % (100-stall)",
                fill="tozeroy", fillcolor="rgba(46,139,87,0.1)",
                line=dict(color="seagreen", width=1.5),
                hovertemplate="L3 Efficiency: %{y:.1f}%<extra></extra>",
            ), row=row, col=1)

        # ── GPU Inference Session Detection & Annotations ──
        # Detect contiguous regions where GPU is busy (LLM inference sessions)
        gpu_busy = pd.to_numeric(df["l0_gpu_busy"], errors="coerce").fillna(0)
        xve_active = pd.to_numeric(df.get("l0_xve_active", pd.Series(dtype=float)), errors="coerce").fillna(0)
        xve_stall = pd.to_numeric(df.get("l0_xve_stall", pd.Series(dtype=float)), errors="coerce").fillna(0)
        busy_mask = gpu_busy > 30  # threshold for "active inference"
        # Find session boundaries (transitions)
        shifted = busy_mask.shift(1, fill_value=False)
        session_starts = busy_mask & ~shifted  # False→True
        session_ends = ~busy_mask & shifted     # True→False

        start_idxs = session_starts[session_starts].index.tolist()
        end_idxs = session_ends[session_ends].index.tolist()
        # Handle session that runs to end of data
        if len(start_idxs) > len(end_idxs):
            end_idxs.append(len(df) - 1)

        # Also get memory BW if available for prefill detection
        has_l0_mem = "l0_mem_read_gbs" in df.columns
        if has_l0_mem:
            mem_read = pd.to_numeric(df["l0_mem_read_gbs"], errors="coerce").fillna(0)
            mem_write = pd.to_numeric(df.get("l0_mem_write_gbs", pd.Series(dtype=float)), errors="coerce").fillna(0)
            mem_total = mem_read + mem_write
        else:
            mem_total = pd.Series(0, index=df.index)

        infer_count = 0
        for si, ei in zip(start_idxs, end_idxs):
            s_ts = ts.iloc[si]
            e_ts = ts.iloc[min(ei, len(ts) - 1)]
            dur_s = (e_ts - s_ts).total_seconds()
            if dur_s < 2:  # skip very short blips
                continue
            infer_count += 1
            n_samples_sess = ei - si

            # ── Sub-phase detection: Prefill vs Decode ──
            # Prefill (prompt processing): parallel computation, higher compute efficiency,
            #   higher memory BW burst (KV cache fill). Typically first 1-3 samples.
            # Decode (token generation): autoregressive, lower compute efficiency,
            #   sustained memory-bound (KV cache reads).
            per_sample_eff = []
            for k in range(si, min(ei, len(df))):
                a = xve_active.iloc[k]
                s = xve_stall.iloc[k]
                eff = a / (a + s) * 100 if (a + s) > 1 else 0
                per_sample_eff.append(eff)

            # Detect prefill boundary: samples where compute efficiency is notably
            # higher than the session average (prompt tokens processed in parallel)
            if len(per_sample_eff) >= 3:
                decode_eff = sum(per_sample_eff[1:]) / len(per_sample_eff[1:]) if len(per_sample_eff) > 1 else 0
                prefill_end = 0
                for k, eff in enumerate(per_sample_eff):
                    if k >= 3:  # prefill is at most first 3 samples
                        break
                    # Prefill sample: higher efficiency or higher mem BW than decode average
                    if eff > decode_eff + 3 or (has_l0_mem and k == 0 and mem_total.iloc[si] > mem_total.iloc[si:ei].median() * 1.2):
                        prefill_end = k + 1
                    else:
                        break
                if prefill_end == 0:
                    prefill_end = 1  # at minimum, first sample is prefill
            else:
                prefill_end = 1

            # Shade Prefill sub-phase (KV cache fill)
            pf_s = ts.iloc[si]
            pf_e = ts.iloc[min(si + prefill_end, len(ts) - 1)]
            pf_dur = (pf_e - pf_s).total_seconds()
            if pf_dur < 0.5:
                pf_e = pf_s + pd.Timedelta(seconds=1)  # min visible width
            pf_eff = sum(per_sample_eff[:prefill_end]) / max(prefill_end, 1)
            fig.add_vrect(
                x0=pf_s, x1=pf_e,
                fillcolor="rgba(65,105,225,0.12)",  # royal blue
                line=dict(width=1, color="rgba(65,105,225,0.5)"),
                layer="below",
                row=row, col=1,
            )
            fig.add_annotation(
                x=pf_s, y=108,
                text=f"<b>Prefill</b>",
                showarrow=False,
                font=dict(size=8, color="rgba(30,60,180,0.9)"),
                bgcolor="rgba(230,240,255,0.8)",
                bordercolor="rgba(65,105,225,0.4)",
                borderwidth=1,
                xref="x", yref="y",
                xanchor="left",
                row=row, col=1,
            )

            # Shade Decode sub-phase (autoregressive token generation)
            if si + prefill_end < ei:
                dc_s = ts.iloc[min(si + prefill_end, len(ts) - 1)]
                dc_e = e_ts
                dc_dur = (dc_e - dc_s).total_seconds()
                dc_eff = sum(per_sample_eff[prefill_end:]) / max(len(per_sample_eff) - prefill_end, 1)
                # Color by decode efficiency: green if compute-bound, red if memory-bound
                if dc_eff > 55:
                    dc_fill = "rgba(34,139,34,0.06)"
                else:
                    dc_fill = "rgba(220,20,60,0.05)"
                fig.add_vrect(
                    x0=dc_s, x1=dc_e,
                    fillcolor=dc_fill,
                    line=dict(width=0.5, color="rgba(100,100,100,0.2)", dash="dot"),
                    layer="below",
                    row=row, col=1,
                )
                dc_mid = dc_s + (dc_e - dc_s) / 2
                bound_label = "compute" if dc_eff > 55 else "mem-bound"
                fig.add_annotation(
                    x=dc_mid, y=108,
                    text=(f"<b>Decode #{infer_count}</b> {dc_dur:.0f}s<br>"
                          f"{bound_label} eff={dc_eff:.0f}%"),
                    showarrow=False,
                    font=dict(size=8, color="rgba(60,60,60,0.85)"),
                    bgcolor="rgba(255,255,255,0.7)",
                    bordercolor="rgba(150,150,150,0.3)",
                    borderwidth=1,
                    xref="x", yref="y",
                    row=row, col=1,
                )

        # Add Compute Efficiency % as a secondary-y trace
        denom = xve_active + xve_stall
        compute_eff = (xve_active / denom * 100).where(denom > 1, 0)
        fig.add_trace(go.Scatter(
            x=ts, y=compute_eff,
            mode="lines", name="Compute Efficiency %",
            line=dict(color="royalblue", width=1.5, dash="dashdot"),
            hovertemplate="Compute Eff: %{y:.1f}% (Active/(Active+Stall))<extra></extra>",
        ), row=row, col=1, secondary_y=True)
        fig.update_yaxes(range=[0, 105], title_text="Compute Eff %", secondary_y=True, row=row, col=1)

        fig.update_yaxes(range=[0, 105], title_text="EU / Cache %", row=row, col=1)
        row += 1

        # ── Panel: iGPU Memory BW & Frequency ──
        fig.add_trace(go.Scatter(
            x=ts, y=df["l0_mem_read_gbs"],
            mode="lines", name="GPU Mem Read (GB/s)",
            fill="tozeroy", fillcolor="rgba(70,130,180,0.15)",
            line=dict(color="steelblue", width=2),
            hovertemplate="Read: %{y:.1f} GB/s<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["l0_mem_write_gbs"],
            mode="lines", name="GPU Mem Write (GB/s)",
            line=dict(color="tomato", width=1.5),
            hovertemplate="Write: %{y:.2f} GB/s<extra></extra>",
        ), row=row, col=1)
        if "l0_gpu_freq_mhz" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["l0_gpu_freq_mhz"],
                mode="lines", name="GPU Freq (MHz)",
                line=dict(color="gray", width=1.5, dash="dot"),
                hovertemplate="GPU Freq: %{y:.0f} MHz<extra></extra>",
            ), row=row, col=1, secondary_y=True)
        fig.update_yaxes(title_text="Memory BW (GB/s)", secondary_y=False, row=row, col=1)
        if "l0_gpu_freq_mhz" in df.columns:
            fig.update_yaxes(title_text="Freq (MHz)", secondary_y=True, row=row, col=1)
        row += 1

    # ── Panel: NPU Inference Busy & Power ──
    if has_npu:
        npu_has_data = "npu_pct" in df.columns and df["npu_pct"].sum() > 0
        npu_all_zero = not npu_has_data
        if "npu_pct" in df.columns:
            # Label honestly: app-level busy % when data present, or "Engine %" when from PDH
            npu_label = "NPU Inference Busy % (app-level)" if npu_has_data else "NPU Engine % (PDH, no data)"
            fig.add_trace(go.Scatter(
                x=ts, y=df["npu_pct"],
                mode="lines", name=npu_label,
                fill="tozeroy", fillcolor="rgba(148,103,189,0.15)",
                line=dict(color="mediumpurple", width=2),
                hovertemplate="NPU Busy: %{y:.1f}%<extra></extra>",
            ), row=row, col=1)
        if "rapl_npu_w" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["rapl_npu_w"],
                mode="lines", name="NPU Power (RAPL, W)",
                fill="tozeroy" if npu_all_zero else None,
                fillcolor="rgba(255,165,0,0.15)" if npu_all_zero else None,
                line=dict(color="darkorange", width=2),
                hovertemplate="NPU Power: %{y:.2f}W<extra></extra>",
            ), row=row, col=1, secondary_y=True)
        if "npu_shared_mb" in df.columns and df["npu_shared_mb"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["npu_shared_mb"],
                mode="lines", name="NPU Shared Mem (MB)",
                line=dict(color="teal", width=1, dash="dot"),
                hovertemplate="NPU Shared: %{y:.0f} MB<extra></extra>",
            ), row=row, col=1, secondary_y=True)
        y_title = "Power (W)" if npu_all_zero else "Busy %"
        fig.update_yaxes(title_text=y_title, secondary_y=False, row=row, col=1)
        fig.update_yaxes(title_text="Power (W) / Mem (MB)", secondary_y=True, row=row, col=1)
        if npu_all_zero:
            fig.add_annotation(
                x=0.5, y=0.5,
                text="NPU Busy % requires npu_embedding_server /metrics — showing RAPL power only",
                showarrow=False, font=dict(size=12, color="rgba(148,103,189,0.6)"),
                xref="x domain", yref="y domain",
                row=row, col=1,
            )
        row += 1

    # ── Panel: NPU Inference Latency ──
    if has_npu_infer:
        fig.add_trace(go.Scatter(
            x=ts, y=df["npu_infer_ms"],
            mode="lines", name="NPU Infer Latency (ms)",
            fill="tozeroy", fillcolor="rgba(220,20,60,0.1)",
            line=dict(color="crimson", width=2),
            hovertemplate="Infer Latency: %{y:.1f} ms<extra></extra>",
        ), row=row, col=1)
        # Add p50/p95 reference lines
        infer_vals = pd.to_numeric(df["npu_infer_ms"], errors="coerce").dropna()
        if len(infer_vals) > 10:
            p50 = infer_vals.median()
            p95 = infer_vals.quantile(0.95)
            fig.add_hline(
                y=p50, line=dict(color="dodgerblue", width=1, dash="dash"),
                annotation_text=f"p50: {p50:.0f}ms",
                annotation_position="top right",
                annotation_font=dict(size=10, color="dodgerblue"),
                row=row, col=1,
            )
            fig.add_hline(
                y=p95, line=dict(color="darkorange", width=1, dash="dash"),
                annotation_text=f"p95: {p95:.0f}ms",
                annotation_position="top right",
                annotation_font=dict(size=10, color="darkorange"),
                row=row, col=1,
            )
        fig.update_yaxes(title_text="Latency (ms)", secondary_y=False, row=row, col=1)
        row += 1

    # ── Panel 4: NVIDIA GPU ──
    if has_nvidia:
        # --- Primary Y: percentages (0–100 scale) ---

        # SM Active % — "% time ≥1 kernel executing on SMs" (~Intel EU Active)
        fig.add_trace(go.Scatter(
            x=ts, y=df["nvidia_gpu_pct"],
            mode="lines", name="SM Active %",
            line=dict(color="limegreen", width=2),
            hovertemplate="SM Active: %{y}% (% time ≥1 kernel on SMs)<extra></extra>",
        ), row=row, col=1)

        # DRAM Active % — "% time GDDR7 R+W bus occupied" (combined, can't split via NVML)
        if "nvidia_mem_pct" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_mem_pct"],
                mode="lines", name="DRAM Active % (R+W)",
                line=dict(color="mediumpurple", width=1.5),
                hovertemplate="DRAM Active: %{y}% (R+W combined, NVML limit)<extra></extra>",
            ), row=row, col=1)

        # SM Clock Efficiency — current/max SM clock
        if "nvidia_sm_clk_eff_pct" in df.columns and df["nvidia_sm_clk_eff_pct"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_sm_clk_eff_pct"],
                mode="lines", name="SM Clk Eff %",
                line=dict(color="dodgerblue", width=1.5),
                hovertemplate="SM Clk Eff: %{y:.1f}% (cur/max)<extra></extra>",
            ), row=row, col=1)

        # Mem Clock Efficiency — current/max memory clock
        if "nvidia_mem_clk_eff_pct" in df.columns and df["nvidia_mem_clk_eff_pct"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_mem_clk_eff_pct"],
                mode="lines", name="Mem Clk Eff %",
                line=dict(color="teal", width=1.5),
                hovertemplate="Mem Clk Eff: %{y:.1f}% (cur/max)<extra></extra>",
            ), row=row, col=1)

        # Power Headroom — power_draw / power_limit
        if "nvidia_power_eff_pct" in df.columns and df["nvidia_power_eff_pct"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_power_eff_pct"],
                mode="lines", name="Power Draw %",
                line=dict(color="orange", width=1, dash="dot"),
                hovertemplate="Power: %{y:.1f}% of TDP<extra></extra>",
            ), row=row, col=1)

        # Temp (on primary Y — range ~30-90 fits 0-100 scale)
        if "nvidia_temp_c" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_temp_c"],
                mode="lines", name="Temp °C",
                line=dict(color="red", width=1, dash="dot"),
                hovertemplate="Temp: %{y}°C<extra></extra>",
                visible="legendonly",
            ), row=row, col=1)

        # P-State (0=max perf, 12=idle — on primary Y, legendonly)
        if "nvidia_pstate" in df.columns and df["nvidia_pstate"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_pstate"],
                mode="lines+markers", name="P-State (0=max)",
                line=dict(color="gray", width=1),
                marker=dict(size=3, color="gray"),
                hovertemplate="P-State: P%{y}<extra></extra>",
                visible="legendonly",
            ), row=row, col=1)

        # --- Secondary Y: absolute values ---

        # Power (W)
        fig.add_trace(go.Scatter(
            x=ts, y=df["nvidia_power_w"],
            mode="lines", name="Power (W)",
            line=dict(color="orange", width=1.5, dash="dashdot"),
            hovertemplate="Power: %{y:.1f}W<extra></extra>",
        ), row=row, col=1, secondary_y=True)

        # VRAM (MB)
        fig.add_trace(go.Scatter(
            x=ts, y=df["nvidia_mem_used_mb"],
            mode="lines", name="VRAM (MB)",
            line=dict(color="red", width=1, dash="dash"),
            hovertemplate="VRAM: %{y} MB<extra></extra>",
        ), row=row, col=1, secondary_y=True)

        # PCIe RX (MB/s) — host→GPU data flow
        if "nvidia_pcie_rx_kbs" in df.columns and df["nvidia_pcie_rx_kbs"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_pcie_rx_kbs"] / 1024,
                mode="lines", name="PCIe RX (MB/s)",
                line=dict(color="cyan", width=1.5),
                hovertemplate="PCIe RX: %{y:.1f} MB/s (host→GPU)<extra></extra>",
                visible="legendonly",
            ), row=row, col=1, secondary_y=True)

        # PCIe TX (MB/s) — GPU→host data flow
        if "nvidia_pcie_tx_kbs" in df.columns and df["nvidia_pcie_tx_kbs"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_pcie_tx_kbs"] / 1024,
                mode="lines", name="PCIe TX (MB/s)",
                line=dict(color="magenta", width=1.5),
                hovertemplate="PCIe TX: %{y:.1f} MB/s (GPU→host)<extra></extra>",
                visible="legendonly",
            ), row=row, col=1, secondary_y=True)

        # SM & Memory clocks (absolute)
        if "nvidia_sm_clk_mhz" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_sm_clk_mhz"],
                mode="lines", name="SM Clock (MHz)",
                line=dict(color="dodgerblue", width=1, dash="dash"),
                hovertemplate="SM: %{y} MHz<extra></extra>",
                visible="legendonly",
            ), row=row, col=1, secondary_y=True)
        if "nvidia_mem_clk_mhz" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_mem_clk_mhz"],
                mode="lines", name="Mem Clock (MHz)",
                line=dict(color="teal", width=1, dash="dash"),
                hovertemplate="Mem Clk: %{y} MHz<extra></extra>",
                visible="legendonly",
            ), row=row, col=1, secondary_y=True)

        # Throttle markers
        if "nvidia_throttle" in df.columns:
            throttled = df[df["nvidia_throttle"] > 0]
            if len(throttled) > 0:
                fig.add_trace(go.Scatter(
                    x=pd.to_datetime(throttled["timestamp"], unit="s"),
                    y=throttled["nvidia_gpu_pct"],
                    mode="markers", name="Throttle",
                    marker=dict(color="red", size=6, symbol="x"),
                    hovertemplate="Throttle: 0x%{customdata}<extra></extra>",
                    customdata=throttled["nvidia_throttle"].apply(lambda x: f"{int(x):08x}"),
                ), row=row, col=1)

        fig.update_yaxes(
            title_text="% (SM Active / DRAM Active / Clk Eff / Power)",
            secondary_y=False, row=row, col=1)
        fig.update_yaxes(
            title_text="Absolute (W / MB / MHz / MB·s⁻¹)",
            secondary_y=True, row=row, col=1)

        # Annotation explaining metric limitations
        fig.add_annotation(
            text=("SM Active = % time ≥1 kernel on SMs (~Intel EU). "
                  "DRAM Active = % time GDDR7 R+W bus occupied (combined; NVML cannot split R/W). "
                  "No L2 cache counters — requires DCGM or CUPTI (Linux-only)."),
            xref="x domain", yref="y domain",
            x=0.01, y=-0.15, xanchor="left", yanchor="top",
            font=dict(size=9, color="gray"),
            showarrow=False, row=row, col=1,
        )
        row += 1

    # ── Panel 5: Power (RAPL) ──
    if has_rapl:
        fig.add_trace(go.Scatter(
            x=ts, y=df["rapl_soc_w"],
            mode="lines", name="SoC Total (W)",
            line=dict(color="black", width=2),
            hovertemplate="SoC: %{y:.1f}W<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["rapl_cpu_w"],
            mode="lines", name="CPU IA (W)",
            line=dict(color="steelblue", width=1.5),
            hovertemplate="CPU: %{y:.1f}W<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["rapl_igpu_w"],
            mode="lines", name="iGPU GT (W)",
            line=dict(color="green", width=1.5),
            hovertemplate="iGPU: %{y:.1f}W<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["rapl_npu_w"],
            mode="lines", name="NPU (W)",
            line=dict(color="darkorange", width=1.5),
            hovertemplate="NPU: %{y:.2f}W<extra></extra>",
        ), row=row, col=1)
        if has_nvidia:
            fig.add_trace(go.Scatter(
                x=ts, y=df["nvidia_power_w"],
                mode="lines", name="NVIDIA GPU (W)",
                line=dict(color="limegreen", width=1.5, dash="dash"),
                hovertemplate="NVIDIA: %{y:.1f}W<extra></extra>",
            ), row=row, col=1)
        fig.update_yaxes(title_text="Power (W)", row=row, col=1)
        row += 1

    # ── Panel: DRAM Bandwidth ──
    if has_bw:
        fig.add_trace(go.Scatter(
            x=ts, y=df["dram_total_gbs"],
            mode="lines", name="DRAM Total BW",
            fill="tozeroy", fillcolor="rgba(148,103,189,0.15)",
            line=dict(color="mediumpurple", width=2),
            hovertemplate="Total: %{y:.2f} GB/s<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["dram_read_gbs"],
            mode="lines", name="DRAM Read",
            line=dict(color="dodgerblue", width=1.5),
            hovertemplate="Read: %{y:.2f} GB/s<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=df["dram_write_gbs"],
            mode="lines", name="DRAM Write",
            line=dict(color="tomato", width=1.5),
            hovertemplate="Write: %{y:.2f} GB/s<extra></extra>",
        ), row=row, col=1)
        # IA vs non-IA bandwidth split (from L3 miss estimation)
        if "ia_dram_bw_gbs" in df.columns and df["ia_dram_bw_gbs"].sum() > 0:
            fig.add_trace(go.Scatter(
                x=ts, y=df["ia_dram_bw_gbs"],
                mode="lines", name="IA Core BW (L3miss est.)",
                line=dict(color="forestgreen", width=1.5, dash="dash"),
                hovertemplate="IA Core: %{y:.2f} GB/s<extra></extra>",
            ), row=row, col=1)
            fig.add_trace(go.Scatter(
                x=ts, y=df["nonia_dram_bw_gbs"],
                mode="lines", name="Non-IA BW (iGPU+NPU+IO)",
                line=dict(color="darkorange", width=1.5, dash="dot"),
                hovertemplate="Non-IA: %{y:.2f} GB/s<extra></extra>",
            ), row=row, col=1)
        fig.update_yaxes(title_text="GB/s", row=row, col=1)
        row += 1

    # ── Panel: DRAM Read Latency ──
    if has_dram_latency:
        fig.add_trace(go.Scatter(
            x=ts, y=df["dram_rd_latency_ns"],
            mode="lines", name="DRAM Read Latency (ns)",
            fill="tozeroy", fillcolor="rgba(255,165,0,0.15)",
            line=dict(color="darkorange", width=2),
            hovertemplate="Latency: %{y:.1f} ns<extra></extra>",
        ), row=row, col=1)
        if "dram_page_hit_rate_rd" in df.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=df["dram_page_hit_rate_rd"] * 100,
                mode="lines", name="Page Hit Rate (%)",
                line=dict(color="seagreen", width=1.5, dash="dash"),
                hovertemplate="Page Hit: %{y:.1f}%<extra></extra>",
            ), row=row, col=1, secondary_y=True)
            fig.update_yaxes(title_text="Page Hit %", secondary_y=True, row=row, col=1)
        fig.update_yaxes(title_text="Latency (ns)", secondary_y=False, row=row, col=1)
        row += 1

    # ── Panel: KV-Cache Size (OVMS-measured and/or analytical estimate) ──
    if has_kv_cache:
        if has_kv_cache_ovms:
            fig.add_trace(go.Scatter(
                x=ts, y=df["ovms_kv_cache_pct"],
                mode="lines", name="KV-Cache Usage (%)",
                fill="tozeroy", fillcolor="rgba(147,112,219,0.2)",
                line=dict(color="mediumpurple", width=2),
                hovertemplate="KV-Cache: %{y:.1f}%<extra></extra>",
            ), row=row, col=1)
            fig.update_yaxes(range=[0, 100], title_text="Cache %", secondary_y=False, row=row, col=1)
            if "ovms_kv_cache_mb" in df.columns:
                fig.add_trace(go.Scatter(
                    x=ts, y=df["ovms_kv_cache_mb"],
                    mode="lines", name="KV-Cache (MB)",
                    line=dict(color="orchid", width=1.5, dash="dot"),
                    hovertemplate="KV-Cache: %{y:.1f} MB<extra></extra>",
                ), row=row, col=1, secondary_y=True)
        if kv_cache_est is not None:
            fig.add_trace(go.Scatter(
                x=ts, y=kv_cache_est,
                mode="lines", name="KV-Cache Size (Est., MB)",
                fill="tozeroy" if not has_kv_cache_ovms else None,
                fillcolor="rgba(255,165,79,0.2)" if not has_kv_cache_ovms else None,
                line=dict(color="darkorange", width=2 if not has_kv_cache_ovms else 1.5,
                           dash=None if not has_kv_cache_ovms else "dot"),
                hovertemplate="KV-Cache (est.): %{y:.1f} MB<extra></extra>",
            ), row=row, col=1, secondary_y=has_kv_cache_ovms)
            if has_kv_cache_ovms:
                fig.update_yaxes(title_text="MB", secondary_y=True, row=row, col=1)
            else:
                fig.update_yaxes(title_text="Estimated KV-Cache (MB)", secondary_y=False, row=row, col=1)
        row += 1

    # ── Panel: RAM ──
    ram_pct = df["ram_used_gb"] / df["ram_total_gb"] * 100
    fig.add_trace(go.Scatter(
        x=ts, y=ram_pct,
        mode="lines", name=f"RAM % (of {df['ram_total_gb'].iloc[0]} GB)",
        fill="tozeroy", fillcolor="rgba(255,127,80,0.2)",
        line=dict(color="coral", width=1.5),
        hovertemplate="RAM: %{y:.1f}% (" + df["ram_used_gb"].apply(lambda v: f"{v:.1f}") + " GB)<extra></extra>",
    ), row=row, col=1)
    fig.update_yaxes(range=[0, 100], title_text="RAM %", row=row, col=1)

    # ── Layout ──
    avg_interval_ms = (ts.diff().dt.total_seconds().median() * 1000) if len(ts) > 1 else 50

    # Build group toggle buttons for per-core panels
    _core_group_btns = []
    _all_pe_indices = _p_core_indices + _e_core_indices + _p_freq_indices + _e_freq_indices
    if _all_pe_indices:
        total_traces = len(fig.data)
        def _vis(show_indices):
            """Build visibility list: True for non-core traces, conditional for core traces."""
            vis = []
            for idx in range(total_traces):
                if idx in _all_pe_indices:
                    vis.append(idx in show_indices)
                else:
                    vis.append(True)
            return vis

        _btn_width = max(len("All Cores"), len("Hide Cores"), len("P-Cores Only"), len("E-Cores Only"))
        _pad = lambda s: s.center(_btn_width, "\u00a0")
        _core_group_btns = [
            dict(label=_pad("All Cores"), method="update",
                 args=[{"visible": _vis(set(_all_pe_indices))}]),
            dict(label=_pad("Hide Cores"), method="update",
                 args=[{"visible": _vis(set())}]),
        ]
        _core_group_btns2 = [
            dict(label=_pad("P-Cores Only"), method="update",
                 args=[{"visible": _vis(set(_p_core_indices + _p_freq_indices))}]),
            dict(label=_pad("E-Cores Only"), method="update",
                 args=[{"visible": _vis(set(_e_core_indices + _e_freq_indices))}]),
        ]

    # Build dynamic title based on which devices have data
    _active_devices = []
    if has_igpu or has_l0:
        _active_devices.append("iGPU")
    if has_nvidia:
        _active_devices.append("NVIDIA")
    if has_npu or has_npu_infer:
        _active_devices.append("NPU")
    _active_devices.append("CPU")  # always present
    if len(_active_devices) >= 3:
        _device_label = "Heterogeneous"
    else:
        _device_label = " + ".join(_active_devices)

    fig.update_layout(
        title=dict(
            text=f"{_device_label} Device Utilization — {duration_min:.1f} min, {n_samples:,} samples @ {avg_interval_ms:.0f}ms",
            font=dict(size=16),
        ),
        height=300 * n_panels,
        hovermode="x unified",
        legend=dict(
            orientation="v",
            yanchor="top", y=0.98,
            xanchor="left", x=1.01,
            font=dict(size=9),
            tracegroupgap=15,
            groupclick="toggleitem",
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ddd",
            borderwidth=1,
        ),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                buttons=_core_group_btns,
                showactive=True,
                x=1.01, xanchor="left",
                y=1.0, yanchor="bottom",
                font=dict(size=10),
                bgcolor="rgba(240,240,240,0.9)",
                bordercolor="#ccc",
                borderwidth=1,
                pad=dict(r=5, t=5),
            ),
            dict(
                type="buttons",
                direction="right",
                buttons=_core_group_btns2,
                showactive=True,
                x=1.01, xanchor="left",
                y=1.0, yanchor="top",
                font=dict(size=10),
                bgcolor="rgba(240,240,240,0.9)",
                bordercolor="#ccc",
                borderwidth=1,
                pad=dict(r=5, t=0),
            ),
        ] if _core_group_btns else [],
        xaxis=dict(
            rangeselector=dict(
                buttons=[
                    dict(count=10, label="10s", step="second", stepmode="backward"),
                    dict(count=30, label="30s", step="second", stepmode="backward"),
                    dict(count=1, label="1m", step="minute", stepmode="backward"),
                    dict(count=5, label="5m", step="minute", stepmode="backward"),
                    dict(step="all", label="All"),
                ]
            ),
            type="date",
        ),
        margin=dict(b=80),
    )

    # Add rangeslider to the TOP x-axis (xaxis1) so it appears above all other panels
    fig.update_layout(
        xaxis=dict(
            rangeslider=dict(visible=True, thickness=0.015),
        )
    )

    # Enable spike lines (crosshair) on all x-axes
    for i in range(1, n_panels + 1):
        fig.update_xaxes(
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikethickness=1, spikecolor="gray", spikedash="dot",
            row=i, col=1,
        )
        fig.update_yaxes(
            showspikes=True, spikethickness=1, spikecolor="gray", spikedash="dot",
            row=i, col=1,
        )

    # ── Phase annotations (agent timeline overlay) ──
    if phases:
        # Add colored vrect bands on ALL subplots for each phase
        # Skip task_agent (wraps everything) — show it only as thin border lines
        inner_phases = [p for p in phases if p[0] != "task_agent"]
        task_phase = [p for p in phases if p[0] == "task_agent"]

        for name, start_dt, end_dt, fill_color, line_color in inner_phases:
            for r in range(1, n_panels + 1):
                fig.add_vrect(
                    x0=start_dt, x1=end_dt,
                    fillcolor=fill_color,
                    line=dict(width=0.5, color=line_color, dash="dot"),
                    layer="below",
                    row=r, col=1,
                )
            # Add phase label at the top panel
            mid = start_dt + (end_dt - start_dt) / 2
            duration_s = (end_dt - start_dt).total_seconds()
            fig.add_annotation(
                x=mid, y=1.0,
                text=f"<b>{name}</b><br>{duration_s:.0f}s",
                showarrow=False,
                font=dict(size=14, color=line_color),
                xref="x", yref="y domain",
                row=1, col=1,
                textangle=-35 if duration_s < 30 else 0,
            )

        # task_agent: thin dashed boundary lines at start/end
        for name, start_dt, end_dt, _, line_color in task_phase:
            for r in range(1, n_panels + 1):
                fig.add_vline(
                    x=start_dt, line=dict(color=line_color, width=1.5, dash="dash"),
                    row=r, col=1,
                )
                fig.add_vline(
                    x=end_dt, line=dict(color=line_color, width=1.5, dash="dash"),
                    row=r, col=1,
                )
            fig.add_annotation(
                x=start_dt, y=1.05,
                text="▶ workflow start",
                showarrow=False, font=dict(size=13, color=line_color),
                xref="x", yref="y domain",
                xanchor="left",
                row=1, col=1,
            )
            fig.add_annotation(
                x=end_dt, y=1.05,
                text="workflow end ◀",
                showarrow=False, font=dict(size=13, color=line_color),
                xref="x", yref="y domain",
                xanchor="right",
                row=1, col=1,
            )

    fig.write_html(output_path, include_plotlyjs=True)

    # Inject interactive controls (selection stats, zoom stats, control panel)
    with open(output_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = inject_dashboard_controls(html, titles, n_panels)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Interactive dashboard saved to {output_path}")
    print(f"  Open in browser: file:///{Path(output_path).resolve().as_posix()}")
    print(f"  Controls: box-select for stats, zoom for range bar, [gear] button for panel toggles")


def main():
    parser = argparse.ArgumentParser(description="Interactive utilization dashboard")
    parser.add_argument("--input", "-i", default="outputs/utilization_samples.csv")
    parser.add_argument("--output", "-o", default="outputs/utilization_dashboard.html")
    parser.add_argument("--phases", "-p", default=None,
                        help="Path to workflow_kpi.json with phase timestamps for timeline overlay")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: {args.input} not found")
        sys.exit(1)

    phases = None
    kv_cache_phases = None
    if args.phases and Path(args.phases).exists():
        phases = load_phases(args.phases)
        if phases:
            print(f"Loaded {len(phases)} agent phases from {args.phases}")
        else:
            print(f"Warning: {args.phases} has no phase timestamps")
        kv_cache_phases = load_phase_tokens(args.phases)

    df = load_data(args.input)
    print(f"Loaded {len(df)} samples from {args.input}")
    build_dashboard(df, args.output, phases=phases, kv_cache_phases=kv_cache_phases)


if __name__ == "__main__":
    main()

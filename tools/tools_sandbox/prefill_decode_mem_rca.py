"""One-off RCA helper: split hw_samples.csv rows into prefill-phase vs decode-phase
using workflow_kpi.json's per-stage start_iso + prefill_ms_est, then compare DRAM/L0
memory-controller counters between phases and machines.

Usage: python prefill_decode_mem_rca.py <run_dir> [<run_dir> ...]
"""
import csv
import json
import statistics as st
import sys
from datetime import datetime, timedelta

METRICS = [
    "dram_rd_latency_ns", "dram_page_hit_rate_rd", "dram_page_hit_rate_wr",
    "dram_read_gbs", "dram_write_gbs", "dram_total_gbs",
    "ia_dram_bw_gbs", "nonia_dram_bw_gbs",
    "l0_mem_read_gbs", "l0_mem_write_gbs", "imc_freq_ghz",
]


def parse_ts(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S.%f")


def prefill_windows(workflow_kpi_path):
    with open(workflow_kpi_path) as f:
        data = json.load(f)
    windows = []
    for name, stage in data.get("stages", {}).items():
        start_iso = stage.get("start_iso")
        prefill_ms = stage.get("prefill_ms_est")
        if not start_iso or not prefill_ms:
            continue
        start = datetime.fromisoformat(start_iso)
        end = start + timedelta(milliseconds=prefill_ms)
        windows.append((name, start, end))
    return windows


def summarize(vals_by_metric):
    out = {}
    for m, vals in vals_by_metric.items():
        vals = [v for v in vals if v > 0]
        if vals:
            out[m] = (len(vals), st.mean(vals), st.median(vals))
    return out


def analyze(run_dir):
    wk_path = f"{run_dir}/workflow_kpi.json"
    hw_path = f"{run_dir}/hw_samples.csv"
    windows = prefill_windows(wk_path)

    prefill_vals = {m: [] for m in METRICS}
    decode_vals = {m: [] for m in METRICS}

    with open(hw_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = parse_ts(row["timestamp"])
            except (ValueError, KeyError):
                continue
            in_prefill = any(start <= ts <= end for (_, start, end) in windows)
            bucket = prefill_vals if in_prefill else decode_vals
            for m in METRICS:
                v = row.get(m, "")
                try:
                    bucket[m].append(float(v))
                except (ValueError, TypeError):
                    pass

    print(f"=== {run_dir} ===")
    print(f"  prefill windows found: {len(windows)}")
    print("  -- PREFILL-phase samples --")
    for m, (n, mean, med) in summarize(prefill_vals).items():
        print(f"    {m}: n={n} mean={mean:.2f} median={med:.2f}")
    print("  -- DECODE-phase samples --")
    for m, (n, mean, med) in summarize(decode_vals).items():
        print(f"    {m}: n={n} mean={mean:.2f} median={med:.2f}")
    print()


if __name__ == "__main__":
    for d in sys.argv[1:]:
        analyze(d)

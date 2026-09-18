"""MDAPI Counter Collector — standalone tool for any workload.

Streams all hardware counters from a Level Zero MDAPI metric group on the iGPU,
saves time-series to CSV, and generates an interactive Plotly HTML dashboard.

Use standalone (with or without built-in GPU load), or run alongside any workload
(OVMS, Ollama, custom inference, etc.) to capture iGPU hardware counter profiles.

Requirements:
    - Intel GPU with Level Zero driver
    - Environment: ZET_ENABLE_METRICS=1
    - ze_loader.dll loadable

Usage:
    # Collect while running your own workload (e.g. OVMS inference on iGPU):
    python mdapi_collector.py --duration 30 --output results/

    # Collect with built-in GPU memory-copy load:
    python mdapi_collector.py --duration 10 --synthetic-load

    # Choose a different metric group:
    python mdapi_collector.py --group VectorEngineProfile --duration 15

    # List available metric groups:
    python mdapi_collector.py --list-groups
"""
import ctypes
import struct
import os
import sys
import time
import csv
import argparse
from datetime import datetime
from pathlib import Path

os.environ.setdefault("ZET_ENABLE_METRICS", "1")

H = ctypes.c_void_p
U32 = ctypes.c_uint32
U64 = ctypes.c_uint64
SZ = ctypes.c_size_t


def _check(r, name=""):
    if r != 0:
        print(f"FATAL: {name} returned 0x{r:08X}")
        sys.exit(1)


def _load_ze():
    try:
        return ctypes.CDLL("ze_loader.dll")
    except OSError:
        print("ERROR: ze_loader.dll not found. Install Intel GPU driver with Level Zero support.")
        sys.exit(1)


def _init_device(ze):
    _check(ze.zeInit(U32(1)), "zeInit")
    dc = U32(0); ze.zeDriverGet(ctypes.byref(dc), None)
    drvs = (H * dc.value)(); ze.zeDriverGet(ctypes.byref(dc), drvs)
    nc = U32(0); ze.zeDeviceGet(H(drvs[0]), ctypes.byref(nc), None)
    devs = (H * nc.value)(); ze.zeDeviceGet(H(drvs[0]), ctypes.byref(nc), devs)
    return drvs[0], devs[0]


def _get_metric_groups(ze, dev):
    """Return list of (handle, name, sampling_type, metric_count) tuples."""
    mc = U32(0); ze.zetMetricGroupGet(H(dev), ctypes.byref(mc), None)
    groups = (H * mc.value)(); ze.zetMetricGroupGet(H(dev), ctypes.byref(mc), groups)
    result = []
    for g in range(mc.value):
        gp = (ctypes.c_byte * 4096)()
        struct.pack_into("I", gp, 0, 0x1000000B); struct.pack_into("Q", gp, 8, 0)
        ze.zetMetricGroupGetProperties(H(groups[g]), ctypes.byref(gp))
        gr = bytes(gp)
        name = gr[16:272].split(b"\x00")[0].decode()
        sampling = struct.unpack_from("I", gr, 528)[0]
        mcount = struct.unpack_from("I", gr, 536)[0]
        st = []
        if sampling & 1: st.append("EBS")
        if sampling & 2: st.append("TBS")
        result.append((groups[g], name, ",".join(st), mcount))
    return result


def _get_metric_names(ze, group_handle):
    """Return (names, units) lists for all metrics in the group."""
    m_count = U32(0); ze.zetMetricGet(H(group_handle), ctypes.byref(m_count), None)
    arr = (H * m_count.value)(); ze.zetMetricGet(H(group_handle), ctypes.byref(m_count), arr)
    names, units = [], []
    for m in range(m_count.value):
        mp = (ctypes.c_byte * 4096)()
        struct.pack_into("I", mp, 0, 0x1000000C); struct.pack_into("Q", mp, 8, 0)
        ze.zetMetricGetProperties(H(arr[m]), ctypes.byref(mp))
        raw = bytes(mp)
        names.append(raw[16:272].split(b"\x00")[0].decode())
        units.append(raw[796:1052].split(b"\x00")[0].decode())
    return names, units


def _run_synthetic_load(ze, ctx, dev, duration_s):
    """Generate GPU memory-copy load, return number of copies."""
    cq_desc = (ctypes.c_byte * 48)()
    struct.pack_into("I", cq_desc, 0, 0x04)
    cq = H(0)
    r = ze.zeCommandQueueCreate(ctx, H(dev), ctypes.byref(cq_desc), ctypes.byref(cq))
    if r != 0:
        return 0

    cl_desc = (ctypes.c_byte * 32)()
    struct.pack_into("I", cl_desc, 0, 0x05)
    cl = H(0)
    ze.zeCommandListCreate(ctx, H(dev), ctypes.byref(cl_desc), ctypes.byref(cl))

    BUF_SIZE = 4 * 1024 * 1024
    dev_alloc = (ctypes.c_byte * 32)()
    struct.pack_into("I", dev_alloc, 0, 0x15)
    src_ptr = H(0); dst_ptr = H(0)
    ze.zeMemAllocDevice(ctx, ctypes.byref(dev_alloc), SZ(BUF_SIZE), SZ(0), H(dev), ctypes.byref(src_ptr))
    ze.zeMemAllocDevice(ctx, ctypes.byref(dev_alloc), SZ(BUF_SIZE), SZ(0), H(dev), ctypes.byref(dst_ptr))

    copies = 0
    end_time = time.time() + duration_s
    while time.time() < end_time:
        ze.zeCommandListReset(cl)
        for _ in range(10):
            ze.zeCommandListAppendMemoryCopy(cl, dst_ptr, src_ptr, SZ(BUF_SIZE), None, U32(0), None)
        ze.zeCommandListClose(cl)
        cl_arr = (H * 1)(cl.value)
        ze.zeCommandQueueExecuteCommandLists(cq, U32(1), cl_arr, None)
        ze.zeCommandQueueSynchronize(cq, U64(2000000000))
        copies += 10

    ze.zeMemFree(ctx, src_ptr)
    ze.zeMemFree(ctx, dst_ptr)
    ze.zeCommandListDestroy(cl)
    ze.zeCommandQueueDestroy(cq)
    return copies


def collect(group_name="ComputeBasic", duration_s=10, sample_period_ns=100_000_000,
            synthetic_load=False, output_dir="."):
    """Collect MDAPI counters and return (csv_path, html_path, num_reports)."""
    ze = _load_ze()
    drv, dev = _init_device(ze)

    # Find requested TBS group
    all_groups = _get_metric_groups(ze, dev)
    target = None
    for handle, name, st, mcount in all_groups:
        if group_name in name and "TBS" in st:
            target = handle
            print(f"[mdapi] Using group: {name} ({mcount} metrics, {st})")
            break
    if target is None:
        print(f"ERROR: No TBS group matching '{group_name}' found.")
        print("Available groups:")
        for _, name, st, mc in all_groups:
            print(f"  {st:5s} {name} ({mc} metrics)")
        sys.exit(1)

    metric_names, metric_units = _get_metric_names(ze, target)

    # Context + activate
    ctx_desc = (ctypes.c_byte * 32)()
    struct.pack_into("I", ctx_desc, 0, 0xE); struct.pack_into("Q", ctx_desc, 8, 0)
    ctx = H(0)
    _check(ze.zeContextCreate(H(drv), ctypes.byref(ctx_desc), ctypes.byref(ctx)), "zeContextCreate")
    group_arr = (H * 1)(target)
    _check(ze.zetContextActivateMetricGroups(ctx, H(dev), U32(1), group_arr), "activate")

    # Open streamer
    st_desc = (ctypes.c_byte * 32)()
    struct.pack_into("I", st_desc, 0, 0x1000000E); struct.pack_into("Q", st_desc, 8, 0)
    struct.pack_into("I", st_desc, 16, 0); struct.pack_into("I", st_desc, 20, sample_period_ns)
    streamer = H(0)
    _check(ze.zetMetricStreamerOpen(ctx, H(dev), H(target),
           ctypes.byref(st_desc), None, ctypes.byref(streamer)), "streamerOpen")
    print(f"[mdapi] Streaming for {duration_s}s (period={sample_period_ns/1e6:.0f}ms)...")

    # Run load or wait
    copies = 0
    if synthetic_load:
        copies = _run_synthetic_load(ze, ctx, dev, duration_s)
        print(f"[mdapi] Synthetic load: {copies} x 4MB copies ({copies*4}MB)")
    else:
        print(f"[mdapi] Waiting {duration_s}s — run your workload now...")
        time.sleep(duration_s)

    # Read raw OA data
    raw_size = SZ(0)
    ze.zetMetricStreamerReadData(streamer, U32(0xFFFFFFFF), ctypes.byref(raw_size), None)
    all_reports = []

    if raw_size.value > 0:
        raw_buf = (ctypes.c_byte * raw_size.value)()
        _check(ze.zetMetricStreamerReadData(streamer, U32(0xFFFFFFFF),
               ctypes.byref(raw_size), raw_buf), "readData")

        sc = U32(0); tc = U32(0)
        ze.zetMetricGroupCalculateMultipleMetricValuesExp(
            H(target), U32(0), SZ(raw_size.value), raw_buf,
            ctypes.byref(sc), ctypes.byref(tc), None, None)

        if tc.value > 0:
            VALUE_SZ = 16
            vals = (ctypes.c_byte * (tc.value * VALUE_SZ))()
            mc_buf = (U32 * sc.value)()
            sc2 = U32(sc.value); tc2 = U32(tc.value)
            ze.zetMetricGroupCalculateMultipleMetricValuesExp(
                H(target), U32(0), SZ(raw_size.value), raw_buf,
                ctypes.byref(sc2), ctypes.byref(tc2), mc_buf, vals)

            if tc2.value > 0:
                vraw = bytes(vals)
                metrics_per = len(metric_names)
                num_reports = mc_buf[0] // metrics_per
                for rpt in range(num_reports):
                    row = {"report_idx": rpt}
                    for m in range(metrics_per):
                        off = (rpt * metrics_per + m) * VALUE_SZ
                        if off + VALUE_SZ > len(vraw):
                            break
                        vtype = struct.unpack_from("I", vraw, off)[0]
                        if vtype == 0:
                            val = struct.unpack_from("I", vraw, off + 8)[0]
                        elif vtype == 1:
                            val = struct.unpack_from("Q", vraw, off + 8)[0]
                        elif vtype == 2:
                            val = struct.unpack_from("f", vraw, off + 8)[0]
                        elif vtype == 3:
                            val = struct.unpack_from("d", vraw, off + 8)[0]
                        else:
                            val = 0
                        row[metric_names[m]] = val
                    all_reports.append(row)

    # Cleanup
    ze.zetMetricStreamerClose(streamer)
    ze.zetContextActivateMetricGroups(ctx, H(dev), U32(0), None)
    ze.zeContextDestroy(ctx)

    if not all_reports:
        print("ERROR: No reports collected. Ensure GPU activity during collection.")
        sys.exit(1)

    print(f"[mdapi] Collected {len(all_reports)} reports x {len(metric_names)} metrics")

    # Output
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # CSV
    csv_path = out / f"mdapi_{group_name}_{timestamp}.csv"
    columns = ["report_idx"] + metric_names
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in all_reports:
            w.writerow(row)

    # HTML
    html_path = out / f"mdapi_{group_name}_{timestamp}.html"
    _write_html(html_path, all_reports, metric_names, metric_units,
                group_name, duration_s, copies, timestamp)

    print(f"[mdapi] CSV:  {csv_path.resolve()}")
    print(f"[mdapi] HTML: {html_path.resolve()}")
    return str(csv_path), str(html_path), len(all_reports)


def _write_html(path, reports, metric_names, metric_units, group_name, duration, copies, timestamp):
    """Generate interactive Plotly HTML dashboard."""
    # Categorize metrics for separate plots
    pct_keys = [n for n in metric_names if any(k in n for k in
                ["BUSY", "ACTIVE", "STALL", "OCCUPANCY", "DISPATCH", "HOLD", "MULTIPLE_PIPE"])]
    rate_keys = [n for n in metric_names if "RATE" in n]
    freq_keys = [n for n in metric_names if "Frequency" in n or "FrequencyMHz" in n]
    count_keys = [n for n in metric_names if n not in pct_keys + rate_keys + freq_keys
                  and n not in ("GpuTime", "GpuCoreClocks", "ResultUncertainty",
                                "QueryBeginTime", "ReportReason", "ContextIdValid",
                                "ContextId", "SourceId", "StreamMarker", "report_idx")]

    def extract(keys):
        out = {}
        for n in keys:
            vals = [r.get(n, 0) for r in reports]
            if any(v != 0 for v in vals):
                out[n] = vals
        return out

    pct_s = extract(pct_keys)
    rate_s = extract(rate_keys)
    freq_s = extract(freq_keys)
    count_s = extract(count_keys)
    x = list(range(len(reports)))

    def traces(series):
        t = []
        for name, vals in series.items():
            t.append(f'{{x:{x},y:{vals},name:"{name}",mode:"lines"}}')
        return ",".join(t)

    # Summary table
    rows = ""
    for i, name in enumerate(metric_names):
        vals = [r.get(name, 0) for r in reports]
        avg = sum(vals) / len(vals)
        mn, mx = min(vals), max(vals)
        unit = metric_units[i] if i < len(metric_units) else ""
        if avg != 0 or mx != 0:
            rows += f"<tr><td>{name}</td><td>{unit}</td><td>{avg:.4f}</td><td>{mn:.4f}</td><td>{mx:.4f}</td></tr>\n"

    load_desc = f"{copies} x 4MB memory copies" if copies else "External workload"
    html = f"""<!DOCTYPE html>
<html><head>
<title>MDAPI {group_name} — {timestamp}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
body{{font-family:system-ui,sans-serif;margin:20px;background:#f8f9fa}}
h1{{color:#1a73e8}}h2{{color:#333;border-bottom:2px solid #1a73e8;padding-bottom:5px}}
.plot{{width:100%;height:380px;margin-bottom:25px;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
.info{{background:#fff;padding:15px;border-radius:8px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
th{{background:#1a73e8;color:#fff;padding:10px;text-align:left}}
td{{padding:8px 10px;border-bottom:1px solid #eee}}tr:hover{{background:#f0f7ff}}
</style></head><body>
<h1>MDAPI Hardware Counters — {group_name}</h1>
<div class="info">
<strong>Date:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
<strong>Group:</strong> {group_name} (TBS, {100}ms sampling)<br>
<strong>Duration:</strong> {duration}s | <strong>Reports:</strong> {len(reports)}<br>
<strong>Load:</strong> {load_desc}<br>
<strong>Platform:</strong> Intel Nova Lake iGPU (128 EUs, Xe2)
</div>
<h2>Utilization & Activity (%)</h2><div id="p1" class="plot"></div>
<h2>Bandwidth / Rates</h2><div id="p2" class="plot"></div>
<h2>Frequency</h2><div id="p3" class="plot"></div>
<h2>Counts (Instructions, Cache, Threads)</h2><div id="p4" class="plot"></div>
<h2>All Metrics Summary</h2>
<table><tr><th>Metric</th><th>Unit</th><th>Avg</th><th>Min</th><th>Max</th></tr>
{rows}</table>
<script>
var lo={{xaxis:{{title:'Sample #'}}}};
Plotly.newPlot('p1',[{traces(pct_s)}],{{...lo,title:'Utilization %',yaxis:{{title:'%',range:[0,105]}}}});
Plotly.newPlot('p2',[{traces(rate_s)}],{{...lo,title:'Bandwidth',yaxis:{{title:'Bytes/s'}}}});
Plotly.newPlot('p3',[{traces(freq_s)}],{{...lo,title:'Frequency',yaxis:{{title:'MHz'}}}});
Plotly.newPlot('p4',[{traces(count_s)}],{{...lo,title:'Counts',yaxis:{{title:'Count'}}}});
</script></body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def list_groups():
    """Print all available metric groups and exit."""
    ze = _load_ze()
    _, dev = _init_device(ze)
    groups = _get_metric_groups(ze, dev)
    print(f"{'#':>3}  {'Sampling':5}  {'Group Name':35}  Metrics")
    print("-" * 60)
    for i, (_, name, st, mc) in enumerate(groups):
        print(f"{i:3d}  {st:5s}  {name:35s}  {mc:3d}")


def main():
    parser = argparse.ArgumentParser(
        description="Collect iGPU MDAPI hardware counters to CSV + HTML dashboard")
    parser.add_argument("--group", default="ComputeBasic",
                        help="Metric group name to collect (default: ComputeBasic)")
    parser.add_argument("--duration", type=int, default=10,
                        help="Collection duration in seconds (default: 10)")
    parser.add_argument("--period", type=int, default=100,
                        help="OA sampling period in ms (default: 100)")
    parser.add_argument("--output", default=".",
                        help="Output directory for CSV and HTML files")
    parser.add_argument("--synthetic-load", action="store_true",
                        help="Generate built-in GPU memory-copy load during collection")
    parser.add_argument("--list-groups", action="store_true",
                        help="List all available metric groups and exit")
    args = parser.parse_args()

    if args.list_groups:
        list_groups()
        return

    collect(group_name=args.group, duration_s=args.duration,
            sample_period_ns=args.period * 1_000_000,
            synthetic_load=args.synthetic_load, output_dir=args.output)


if __name__ == "__main__":
    main()

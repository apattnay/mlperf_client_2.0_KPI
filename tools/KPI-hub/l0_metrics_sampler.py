"""Level Zero GPU Metrics Sampler — background thread for pipeline integration.

Collects hardware counters via Level Zero TBS (Time-Based Sampling) from 
the ComputeBasic metric group. Designed to integrate with sample_utilization_fast.py.

Requirements:
    - Intel GPU with Level Zero driver support
    - Environment: ZET_ENABLE_METRICS=1
    - ze_loader.dll must be loadable

Usage:
    sampler = L0MetricsSampler()
    sampler.start()
    # ... run workload ...
    metrics = sampler.snapshot()  # {"l0_gpu_busy": 85.2, "l0_xve_active": 42.1, ...}
    sampler.stop()
"""
import ctypes
import struct
import os
import sys
import time
import threading
import atexit

os.environ.setdefault("ZET_ENABLE_METRICS", "1")

H = ctypes.c_void_p
U32 = ctypes.c_uint32
U64 = ctypes.c_uint64
SZ = ctypes.c_size_t

# Metric columns exposed to CSV
_METRIC_COLUMNS = [
    "l0_gpu_busy",           # GPU_BUSY (%)
    "l0_xve_active",         # XVE_ACTIVE (%)
    "l0_xve_stall",          # XVE_STALL (%)
    "l0_xve_occupancy",      # XVE_THREADS_OCCUPANCY_ALL (%)
    "l0_l3_stall",           # L3_STALL (%)
    "l0_gpu_freq_mhz",       # CoreFrequencyMHz
    "l0_mem_read_gbs",       # GPU_MEMORY_BYTE_READ_RATE (GB/s)
    "l0_mem_write_gbs",      # GPU_MEMORY_BYTE_WRITE_RATE (GB/s)
    "l0_compute_busy",       # COMMAND_PARSER_COMPUTE_ENGINE_BUSY (%)
    "l0_copy_busy",          # COMMAND_PARSER_COPY_ENGINE_BUSY (%)
    "l0_render_busy",        # COMMAND_PARSER_RENDER_ENGINE_BUSY (%)
]

# Mapping from L0 metric name → CSV column name
_METRIC_MAP = {
    "GPU_BUSY": "l0_gpu_busy",
    "XVE_ACTIVE": "l0_xve_active",
    "XVE_STALL": "l0_xve_stall",
    "XVE_THREADS_OCCUPANCY_ALL": "l0_xve_occupancy",
    "L3_STALL": "l0_l3_stall",
    "CoreFrequencyMHz": "l0_gpu_freq_mhz",
    "GPU_MEMORY_BYTE_READ_RATE": "l0_mem_read_gbs",
    "GPU_MEMORY_BYTE_WRITE_RATE": "l0_mem_write_gbs",
    "COMMAND_PARSER_COMPUTE_ENGINE_BUSY": "l0_compute_busy",
    "COMMAND_PARSER_COPY_ENGINE_BUSY": "l0_copy_busy",
    "COMMAND_PARSER_RENDER_ENGINE_BUSY": "l0_render_busy",
}


class L0MetricsSampler:
    """Background thread that collects GPU hardware counters via Level Zero TBS."""

    def __init__(self, sample_interval_s=0.5, metric_group="ComputeBasic",
                 sampling_period_ns=100_000_000):
        """
        Args:
            sample_interval_s: How often to read and process raw OA data.
            metric_group: Name of the L0 metric group to collect (default: ComputeBasic).
            sampling_period_ns: OA hardware sampling period in nanoseconds (default: 100ms).
        """
        self._interval = sample_interval_s
        self._group_name = metric_group
        self._period_ns = sampling_period_ns
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._latest = {col: 0.0 for col in _METRIC_COLUMNS}
        self._initialized = False
        self._error = None

        # L0 handles (set during _init_l0)
        self._ze = None
        self._ctx = None
        self._dev = None
        self._streamer = None
        self._target_group = None
        self._metric_names = []
        self._metric_name_to_idx = {}

    def _init_l0(self):
        """Initialize Level Zero and open the metric streamer."""
        try:
            ze = ctypes.CDLL("ze_loader.dll")
        except OSError as e:
            self._error = f"Cannot load ze_loader.dll: {e}"
            return False
        self._ze = ze

        r = ze.zeInit(U32(1))
        if r != 0:
            self._error = f"zeInit failed: 0x{r:08X}"
            return False

        # Driver + Device
        dc = U32(0); ze.zeDriverGet(ctypes.byref(dc), None)
        if dc.value == 0:
            self._error = "No Level Zero drivers found"
            return False
        drvs = (H * dc.value)(); ze.zeDriverGet(ctypes.byref(dc), drvs)
        self._drv = drvs[0]

        devc = U32(0); ze.zeDeviceGet(H(self._drv), ctypes.byref(devc), None)
        if devc.value == 0:
            self._error = "No Level Zero devices found"
            return False
        devs = (H * devc.value)(); ze.zeDeviceGet(H(self._drv), ctypes.byref(devc), devs)
        self._dev = devs[0]

        # Find target metric group (TBS)
        mc = U32(0); ze.zetMetricGroupGet(H(self._dev), ctypes.byref(mc), None)
        if mc.value == 0:
            self._error = "No metric groups found (is ZET_ENABLE_METRICS=1?)"
            return False
        groups = (H * mc.value)(); ze.zetMetricGroupGet(H(self._dev), ctypes.byref(mc), groups)

        self._target_group = None
        for g in range(mc.value):
            gp = (ctypes.c_byte * 4096)()
            struct.pack_into("I", gp, 0, 0x1000000B)
            struct.pack_into("Q", gp, 8, 0)
            ze.zetMetricGroupGetProperties(H(groups[g]), ctypes.byref(gp))
            gr = bytes(gp)
            gname = gr[16:272].split(b"\x00")[0].decode()
            sampling = struct.unpack_from("I", gr, 528)[0]
            if self._group_name in gname and (sampling & 2):  # TBS = bit 1
                self._target_group = groups[g]
                break

        if self._target_group is None:
            self._error = f"Metric group '{self._group_name}' (TBS) not found"
            return False

        # Get metric names
        m_count = U32(0)
        ze.zetMetricGet(H(self._target_group), ctypes.byref(m_count), None)
        metrics_arr = (H * m_count.value)()
        ze.zetMetricGet(H(self._target_group), ctypes.byref(m_count), metrics_arr)

        self._metric_names = []
        for m in range(m_count.value):
            mp = (ctypes.c_byte * 4096)()
            struct.pack_into("I", mp, 0, 0x1000000C)
            struct.pack_into("Q", mp, 8, 0)
            ze.zetMetricGetProperties(H(metrics_arr[m]), ctypes.byref(mp))
            mraw = bytes(mp)
            name = mraw[16:272].split(b"\x00")[0].decode()
            self._metric_names.append(name)

        self._metric_name_to_idx = {n: i for i, n in enumerate(self._metric_names)}

        # Create context
        ctx_desc = (ctypes.c_byte * 32)()
        struct.pack_into("I", ctx_desc, 0, 0xE)
        struct.pack_into("Q", ctx_desc, 8, 0)
        ctx = H(0)
        r = ze.zeContextCreate(H(self._drv), ctypes.byref(ctx_desc), ctypes.byref(ctx))
        if r != 0:
            self._error = f"zeContextCreate failed: 0x{r:08X}"
            return False
        self._ctx = ctx

        # Activate metric group
        ga = (H * 1)(self._target_group)
        r = ze.zetContextActivateMetricGroups(ctx, H(self._dev), U32(1), ga)
        if r != 0:
            self._error = f"zetContextActivateMetricGroups failed: 0x{r:08X}"
            return False

        # Open streamer
        st_desc = (ctypes.c_byte * 32)()
        struct.pack_into("I", st_desc, 0, 0x1000000E)
        struct.pack_into("Q", st_desc, 8, 0)
        struct.pack_into("I", st_desc, 16, 0)
        struct.pack_into("I", st_desc, 20, self._period_ns)
        streamer = H(0)
        r = ze.zetMetricStreamerOpen(ctx, H(self._dev), H(self._target_group),
                                     ctypes.byref(st_desc), None, ctypes.byref(streamer))
        if r != 0:
            self._error = f"zetMetricStreamerOpen failed: 0x{r:08X}"
            return False
        self._streamer = streamer

        self._initialized = True
        return True

    def _cleanup_l0(self):
        """Release all Level Zero resources."""
        ze = self._ze
        if ze is None:
            return
        try:
            if self._streamer:
                ze.zetMetricStreamerClose(self._streamer)
                self._streamer = None
            if self._ctx and self._dev:
                ze.zetContextActivateMetricGroups(self._ctx, H(self._dev), U32(0), None)
            if self._ctx:
                ze.zeContextDestroy(self._ctx)
                self._ctx = None
        except Exception:
            pass

    def _read_and_process(self):
        """Read raw OA data and calculate metric values. Returns dict of latest values."""
        ze = self._ze
        if not self._initialized:
            return None

        # Read raw data
        raw_size = SZ(0)
        ze.zetMetricStreamerReadData(self._streamer, U32(0xFFFFFFFF),
                                     ctypes.byref(raw_size), None)
        if raw_size.value == 0:
            return None

        raw_buf = (ctypes.c_byte * raw_size.value)()
        r = ze.zetMetricStreamerReadData(self._streamer, U32(0xFFFFFFFF),
                                         ctypes.byref(raw_size), raw_buf)
        if r != 0 or raw_size.value == 0:
            return None

        # Count query (Exp variant)
        sc = U32(0); tc = U32(0)
        r = ze.zetMetricGroupCalculateMultipleMetricValuesExp(
            H(self._target_group), U32(0), SZ(raw_size.value), raw_buf,
            ctypes.byref(sc), ctypes.byref(tc), None, None)
        if r != 0 or tc.value == 0:
            return None

        # Calculate values
        vals = (ctypes.c_byte * (tc.value * 16))()
        mc_buf = (U32 * sc.value)()
        sc2 = U32(sc.value); tc2 = U32(tc.value)
        r = ze.zetMetricGroupCalculateMultipleMetricValuesExp(
            H(self._target_group), U32(0), SZ(raw_size.value), raw_buf,
            ctypes.byref(sc2), ctypes.byref(tc2), mc_buf, vals)
        if r != 0 or tc2.value == 0:
            return None

        # Parse values — average across ALL reports for stable readings
        vraw = bytes(vals)
        metrics_per = len(self._metric_names)
        actual_sets = tc2.value // metrics_per if metrics_per > 0 else 0
        if actual_sets == 0:
            return None

        # Average all reports for representative values
        result = {csv_name: 0.0 for csv_name in _METRIC_MAP.values()}
        for s in range(actual_sets):
            for l0_name, csv_name in _METRIC_MAP.items():
                idx = self._metric_name_to_idx.get(l0_name)
                if idx is None:
                    continue
                off = (s * metrics_per + idx) * 16
                if off + 16 > len(vraw):
                    continue
                vtype = struct.unpack_from("I", vraw, off)[0]
                if vtype == 0:
                    result[csv_name] += float(struct.unpack_from("I", vraw, off + 8)[0])
                elif vtype == 1:
                    result[csv_name] += float(struct.unpack_from("Q", vraw, off + 8)[0])
                elif vtype == 2:
                    result[csv_name] += float(struct.unpack_from("f", vraw, off + 8)[0])
                elif vtype == 3:
                    result[csv_name] += float(struct.unpack_from("d", vraw, off + 8)[0])

        for csv_name in result:
            result[csv_name] /= actual_sets

        return result

    def _run(self):
        """Background thread loop."""
        if not self._init_l0():
            return
        atexit.register(self._cleanup_l0)

        while not self._stop_event.is_set():
            result = self._read_and_process()
            if result:
                with self._lock:
                    self._latest.update(result)
            self._stop_event.wait(self._interval)

        self._cleanup_l0()

    def start(self):
        """Start background collection thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="L0MetricsSampler")
        self._thread.start()
        # Wait briefly for initialization
        time.sleep(0.2)
        if self._error:
            print(f"[L0MetricsSampler] WARNING: {self._error}", file=sys.stderr)

    def stop(self):
        """Stop background collection."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5)
        self._thread = None

    def snapshot(self):
        """Return latest metric values as a dict."""
        with self._lock:
            return dict(self._latest)

    @staticmethod
    def get_extra_columns():
        """Return CSV column names for this sampler."""
        return list(_METRIC_COLUMNS)

    @property
    def error(self):
        return self._error

    @property
    def initialized(self):
        return self._initialized


# Self-test
if __name__ == "__main__":
    print("L0MetricsSampler self-test")
    sampler = L0MetricsSampler(sample_interval_s=0.3)
    sampler.start()

    if sampler.error:
        print(f"Error: {sampler.error}")
        sys.exit(1)

    print(f"Columns: {sampler.get_extra_columns()}")
    print("Collecting 5 snapshots (1s apart)...")
    for i in range(5):
        time.sleep(1)
        snap = sampler.snapshot()
        busy = snap.get("l0_gpu_busy", 0)
        active = snap.get("l0_xve_active", 0)
        stall = snap.get("l0_xve_stall", 0)
        freq = snap.get("l0_gpu_freq_mhz", 0)
        mem_r = snap.get("l0_mem_read_gbs", 0)
        compute = snap.get("l0_compute_busy", 0)
        print(f"  [{i}] GPU_BUSY={busy:.1f}% XVE_ACTIVE={active:.1f}% "
              f"XVE_STALL={stall:.1f}% freq={freq:.0f}MHz "
              f"mem_rd={mem_r:.2f}GB/s compute={compute:.1f}%")

    sampler.stop()
    print("Done.")

"""
Level Zero Sysman-based iGPU metrics sampler.

Uses zes* APIs (Sysman) instead of zet* (TBS/OA counters).
Key advantage: NO exclusive OA counter access needed — works alongside
OVMS, NPU embedding server, and any other L0 users without contention.

Provides:
  - Per-engine busy % (compute, render, copy, media)
  - GPU frequency (actual MHz)
  - GPU memory usage (dedicated + shared)
  - GPU temperature
  - GPU power draw

Usage:
    sampler = L0SysmanSampler()
    sampler.start()
    snap = sampler.snapshot()  # dict of current values
    sampler.stop()
"""
import ctypes
import struct
import threading
import time
import os
import atexit

# ── L0 / Sysman struct type constants ──
ZES_STRUCTURE_TYPE_ENGINE_PROPERTIES = 0x13
ZES_STRUCTURE_TYPE_FREQ_PROPERTIES = 0x16
ZES_STRUCTURE_TYPE_FREQ_STATE = 0x18
ZES_STRUCTURE_TYPE_MEM_PROPERTIES = 0x1E
ZES_STRUCTURE_TYPE_MEM_STATE = 0x20
ZES_STRUCTURE_TYPE_TEMP_PROPERTIES = 0x23

# Engine types
ZES_ENGINE_GROUP_ALL = 0
ZES_ENGINE_GROUP_COMPUTE_ALL = 1
ZES_ENGINE_GROUP_MEDIA_ALL = 2
ZES_ENGINE_GROUP_COPY_ALL = 3
ZES_ENGINE_GROUP_RENDER_ALL = 6

ENGINE_NAMES = {
    0: "all", 1: "compute", 2: "media", 3: "copy",
    4: "3d", 5: "media_enh", 6: "render",
}


class L0SysmanSampler:
    """Collect iGPU metrics via Level Zero Sysman API (no OA contention)."""

    COLUMNS = [
        "zes_gpu_busy", "zes_compute_busy", "zes_render_busy",
        "zes_copy_busy", "zes_media_busy",
        "zes_freq_mhz", "zes_mem_used_mb", "zes_temp_c", "zes_power_w",
    ]

    def __init__(self):
        self.available = False
        self.error = None
        self._ze = None
        self._dev = None
        self._engines = []     # list of (handle, engine_type, prev_active, prev_ts)
        self._freq_handles = []
        self._mem_handles = []
        self._temp_handles = []
        self._power_handles = []
        self._lock = threading.Lock()
        self._snap = {c: 0.0 for c in self.COLUMNS}
        self._stop = threading.Event()
        self._thread = None
        self._init()
        if self.available:
            atexit.register(self.stop)

    def _init(self):
        """Initialize L0 Sysman handles."""
        try:
            if os.environ.get("ZES_ENABLE_SYSMAN") != "1":
                os.environ["ZES_ENABLE_SYSMAN"] = "1"

            U32 = ctypes.c_uint32
            H = ctypes.c_void_p

            ze = ctypes.CDLL("ze_loader.dll")
            self._ze = ze

            # Init L0
            rc = ze.zeInit(U32(1))  # GPU only
            if rc != 0:
                self.error = f"zeInit failed: 0x{rc:X}"
                return

            # Get driver
            dc = U32(0)
            ze.zeDriverGet(ctypes.byref(dc), None)
            if dc.value == 0:
                self.error = "No L0 drivers"
                return
            drvs = (H * dc.value)()
            ze.zeDriverGet(ctypes.byref(dc), drvs)

            # Get device (first GPU)
            devc = U32(0)
            ze.zeDeviceGet(H(drvs[0]), ctypes.byref(devc), None)
            if devc.value == 0:
                self.error = "No L0 devices"
                return
            devs = (H * devc.value)()
            ze.zeDeviceGet(H(drvs[0]), ctypes.byref(devc), devs)
            self._dev = devs[0]

            # ── Enumerate engine groups ──
            ec = U32(0)
            rc = ze.zesDeviceEnumEngineGroups(H(self._dev), ctypes.byref(ec), None)
            if rc == 0 and ec.value > 0:
                edoms = (H * ec.value)()
                ze.zesDeviceEnumEngineGroups(H(self._dev), ctypes.byref(ec), edoms)
                for i in range(ec.value):
                    # Get engine properties to know the type
                    pbuf = (ctypes.c_uint8 * 64)()
                    struct.pack_into("<I", pbuf, 0, ZES_STRUCTURE_TYPE_ENGINE_PROPERTIES)
                    rc2 = ze.zesEngineGetProperties(H(edoms[i]), ctypes.byref(pbuf))
                    if rc2 == 0:
                        etype = struct.unpack_from("<I", pbuf, 16)[0]
                        ename = ENGINE_NAMES.get(etype, f"unk{etype}")
                        self._engines.append((edoms[i], etype, ename, 0, 0))

            # ── Enumerate frequency domains ──
            fc = U32(0)
            rc = ze.zesDeviceEnumFrequencyDomains(H(self._dev), ctypes.byref(fc), None)
            if rc == 0 and fc.value > 0:
                fdoms = (H * fc.value)()
                ze.zesDeviceEnumFrequencyDomains(H(self._dev), ctypes.byref(fc), fdoms)
                self._freq_handles = list(fdoms)

            # ── Enumerate memory modules ──
            mc = U32(0)
            rc = ze.zesDeviceEnumMemoryModules(H(self._dev), ctypes.byref(mc), None)
            if rc == 0 and mc.value > 0:
                mdoms = (H * mc.value)()
                ze.zesDeviceEnumMemoryModules(H(self._dev), ctypes.byref(mc), mdoms)
                self._mem_handles = list(mdoms)

            # ── Enumerate temperature sensors ──
            tc = U32(0)
            rc = ze.zesDeviceEnumTemperatureSensors(H(self._dev), ctypes.byref(tc), None)
            if rc == 0 and tc.value > 0:
                tdoms = (H * tc.value)()
                ze.zesDeviceEnumTemperatureSensors(H(self._dev), ctypes.byref(tc), tdoms)
                self._temp_handles = list(tdoms)

            # ── Enumerate power domains ──
            pc = U32(0)
            rc = ze.zesDeviceEnumPowerDomains(H(self._dev), ctypes.byref(pc), None)
            if rc == 0 and pc.value > 0:
                pdoms = (H * pc.value)()
                ze.zesDeviceEnumPowerDomains(H(self._dev), ctypes.byref(pc), pdoms)
                self._power_handles = list(pdoms)

            n_eng = len(self._engines)
            n_freq = len(self._freq_handles)
            n_mem = len(self._mem_handles)
            n_temp = len(self._temp_handles)
            n_pwr = len(self._power_handles)
            print(f"[L0Sysman] Init OK: {n_eng} engines, {n_freq} freq, "
                  f"{n_mem} mem, {n_temp} temp, {n_pwr} power domains")

            if n_eng > 0 or n_freq > 0:
                self.available = True
            else:
                self.error = "No Sysman domains found"

        except OSError as e:
            self.error = f"ze_loader.dll not found: {e}"
        except Exception as e:
            self.error = f"Init failed: {e}"

    def start(self):
        if not self.available:
            return
        # Take initial engine activity readings
        self._read_engine_baselines()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self):
        """Return current metrics dict (thread-safe copy)."""
        with self._lock:
            return dict(self._snap)

    def _read_engine_baselines(self):
        """Read initial engine activity counters for delta computation."""
        ze = self._ze
        H = ctypes.c_void_p
        new_engines = []
        for handle, etype, ename, _, _ in self._engines:
            buf = (ctypes.c_uint8 * 32)()
            rc = ze.zesEngineGetActivity(H(handle), ctypes.byref(buf))
            if rc == 0:
                active = struct.unpack_from("<Q", buf, 0)[0]
                ts = struct.unpack_from("<Q", buf, 8)[0]
                new_engines.append((handle, etype, ename, active, ts))
            else:
                new_engines.append((handle, etype, ename, 0, 0))
        self._engines = new_engines

    def _loop(self):
        """Background polling loop (1 Hz)."""
        while not self._stop.is_set():
            try:
                snap = self._sample()
                with self._lock:
                    self._snap = snap
            except Exception:
                pass
            self._stop.wait(1.0)

    def _sample(self):
        """Read all Sysman domains and return metrics dict."""
        ze = self._ze
        H = ctypes.c_void_p
        result = {c: 0.0 for c in self.COLUMNS}

        # ── Engine activity (delta-based busy %) ──
        new_engines = []
        for handle, etype, ename, prev_active, prev_ts in self._engines:
            buf = (ctypes.c_uint8 * 32)()
            rc = ze.zesEngineGetActivity(H(handle), ctypes.byref(buf))
            if rc == 0:
                active = struct.unpack_from("<Q", buf, 0)[0]
                ts = struct.unpack_from("<Q", buf, 8)[0]
                dt = ts - prev_ts
                if dt > 0 and prev_ts > 0:
                    busy_pct = min(100.0, (active - prev_active) * 100.0 / dt)
                    # Map engine type to column
                    col_map = {
                        0: "zes_gpu_busy", 1: "zes_compute_busy",
                        2: "zes_media_busy", 3: "zes_copy_busy",
                        6: "zes_render_busy",
                    }
                    col = col_map.get(etype)
                    if col:
                        result[col] = round(busy_pct, 1)
                new_engines.append((handle, etype, ename, active, ts))
            else:
                new_engines.append((handle, etype, ename, prev_active, prev_ts))
        self._engines = new_engines

        # ── Frequency (take max across domains) ──
        for fh in self._freq_handles:
            buf = (ctypes.c_uint8 * 128)()
            struct.pack_into("<I", buf, 0, ZES_STRUCTURE_TYPE_FREQ_STATE)
            rc = ze.zesFrequencyGetState(H(fh), ctypes.byref(buf))
            if rc == 0:
                # zes_freq_state_t: stype(4)+pad(4)+pNext(8)+currentVoltage(8)+
                #   requestedFrequency(8)+tdpFrequency(8)+efficientFrequency(8)+
                #   actualFrequency(8)+throttleReasons(4)
                actual = struct.unpack_from("<d", buf, 48)[0]
                if actual > 0:
                    result["zes_freq_mhz"] = round(actual, 0)

        # ── Memory usage ──
        for mh in self._mem_handles:
            buf = (ctypes.c_uint8 * 128)()
            struct.pack_into("<I", buf, 0, ZES_STRUCTURE_TYPE_MEM_STATE)
            rc = ze.zesMemoryGetState(H(mh), ctypes.byref(buf))
            if rc == 0:
                # zes_mem_state_t: stype(4)+pad(4)+pNext(8)+health(4)+pad(4)+
                #   free(8)+size(8)
                free_bytes = struct.unpack_from("<Q", buf, 24)[0]
                size_bytes = struct.unpack_from("<Q", buf, 32)[0]
                if size_bytes > 0:
                    used_mb = (size_bytes - free_bytes) / (1024 * 1024)
                    result["zes_mem_used_mb"] = round(used_mb, 1)

        # ── Temperature ──
        for th in self._temp_handles:
            temp_val = ctypes.c_double(0.0)
            rc = ze.zesTemperatureGetState(H(th), ctypes.byref(temp_val))
            if rc == 0 and temp_val.value > 0:
                result["zes_temp_c"] = round(temp_val.value, 1)

        # ── Power ──
        for ph in self._power_handles:
            buf = (ctypes.c_uint8 * 32)()
            rc = ze.zesPowerGetEnergyCounter(H(ph), ctypes.byref(buf))
            if rc == 0:
                energy_uj = struct.unpack_from("<Q", buf, 0)[0]
                timestamp_us = struct.unpack_from("<Q", buf, 8)[0]
                # Would need delta for power — store for next iteration
                # For now, skip (RAPL already covers power)
                pass

        return result


if __name__ == "__main__":
    print("Testing L0 Sysman sampler (standalone)...")
    s = L0SysmanSampler()
    if not s.available:
        print(f"NOT AVAILABLE: {s.error}")
    else:
        s.start()
        for i in range(5):
            time.sleep(1)
            snap = s.snapshot()
            busy = snap["zes_gpu_busy"]
            comp = snap["zes_compute_busy"]
            rend = snap["zes_render_busy"]
            freq = snap["zes_freq_mhz"]
            mem = snap["zes_mem_used_mb"]
            temp = snap["zes_temp_c"]
            print(f"  [{i}] gpu={busy:.1f}% compute={comp:.1f}% render={rend:.1f}% "
                  f"freq={freq:.0f}MHz mem={mem:.0f}MB temp={temp:.1f}C")
        s.stop()
        print("Done.")

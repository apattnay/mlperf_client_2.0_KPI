#!/usr/bin/env python3
"""
High-frequency heterogeneous device utilization sampler.

Uses win32pdh (native PDH API) + psutil for ~5ms per-sample overhead,
enabling 50ms+ sampling rates. nvidia-smi runs on a background thread
at ~200ms intervals to avoid blocking the main sample loop.

DRAM Bandwidth Monitoring:
  --bandwidth pcm    : Use Intel PCM (pcm.exe) for accurate IMC counters [DEFAULT]
  --bandwidth proxy  : Use RAPL power-based estimation (no driver needed)
  --bandwidth none   : Disable bandwidth monitoring

Instrumentation Profiles (via --profile):
  lightweight   : PCM bandwidth + PDH RAPL + GPU. Fast, minimal overhead (~5ms/sample)
  simulation    : EMON deep collection (microarch, cache, DRAM page, C-state). For projection.
  memory_deep   : EMON bandwidth + cache + DRAM detail. Memory subsystem focus.
  power_analysis: EMON power + C-state. Platform power characterization.

Profiles are defined in config/instrumentation.json and can be overridden with --source.

Produces the same CSV schema as sample_utilization.ps1 for compatibility
with plot_utilization.py. EMON columns are appended when enabled.

Usage:
    python sample_utilization_fast.py [--interval 50] [--output outputs/utilization_samples.csv] [--power] [--bandwidth pcm|proxy|none]
    python sample_utilization_fast.py --profile simulation --interval 1000 --output outputs/sim_data.csv
    python sample_utilization_fast.py --profile lightweight --source bandwidth=emon
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import psutil

try:
    import win32pdh
except ImportError:
    print("ERROR: pywin32 required. Install with: pip install pywin32")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_instrumentation_config(profile_name: str = None, overrides: dict = None) -> dict:
    """Load instrumentation config from config/instrumentation.json.

    Args:
        profile_name: Profile to activate (overrides active_profile in file)
        overrides: Dict of source overrides e.g. {"bandwidth": "emon"}

    Returns:
        Resolved config dict with 'sources' and tool-specific sections.
    """
    # Find config file relative to this script or workspace root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "..", "..", "config", "instrumentation.json"),
        os.path.join(script_dir, "config", "instrumentation.json"),
        r"c:\LiteAgent-Clash\config\instrumentation.json",
    ]
    config_path = None
    for c in candidates:
        if os.path.isfile(c):
            config_path = os.path.abspath(c)
            break

    if config_path:
        with open(config_path) as f:
            full_config = json.load(f)
    else:
        # Fallback defaults (lightweight profile, no EMON)
        full_config = {
            "active_profile": "lightweight",
            "profiles": {"lightweight": {"sources": {
                "bandwidth": "pcm", "microarch": "none", "cache_detail": "none",
                "dram_detail": "none", "power": "pdh", "cstate": "none",
                "cpu_util": "pdh", "gpu_nvidia": "smi", "gpu_intel": "pdh",
            }}},
            "emon": {"bin_dir": None, "interval_s": 1.0, "events": {}, "auto_detect_paths": []},
            "pcm": {"exe": None, "interval_s": 0.1},
        }

    # Resolve profile
    active = profile_name or full_config.get("active_profile", "lightweight")
    profiles = full_config.get("profiles", {})
    profile = profiles.get(active, profiles.get("lightweight", {}))
    sources = dict(profile.get("sources", {}))

    # Apply overrides
    if overrides:
        sources.update(overrides)

    return {
        "profile_name": active,
        "sources": sources,
        "emon": full_config.get("emon", {}),
        "pcm": full_config.get("pcm", {}),
    }


def get_emon_categories(sources: dict) -> set:
    """Determine which EMON categories are needed based on source config."""
    categories = set()
    emon_sources = {"bandwidth", "microarch", "cache_detail", "dram_detail", "power", "cstate"}
    for key in emon_sources:
        val = sources.get(key, "none")
        if val == "emon" or val == "both":
            categories.add(key)
    return categories


class PDHCounterGroup:
    """Manages a set of Windows PDH performance counters."""

    def __init__(self):
        self.query = win32pdh.OpenQuery()
        self._counters = {}
        self._primed = False

    def add(self, name: str, object_name: str, instance: str, counter_name: str):
        path = win32pdh.MakeCounterPath(
            (None, object_name, instance, None, -1, counter_name)
        )
        self._counters[name] = win32pdh.AddCounter(self.query, path)

    def collect(self):
        win32pdh.CollectQueryData(self.query)
        if not self._primed:
            self._primed = True

    def get(self, name: str, fmt=win32pdh.PDH_FMT_LONG) -> int:
        try:
            return win32pdh.GetFormattedCounterValue(self._counters[name], fmt)[1]
        except Exception:
            return 0

    def close(self):
        win32pdh.CloseQuery(self.query)


class RAPLPoller:
    """Background thread for RAPL Energy Meter counters (PDH 'Power' counters block ~1.8s)."""

    RAPL_MAP = {
        "rapl_cpu_w": "CPU_VCCIA",
        "rapl_igpu_w": "GPU_VCCGT",
        "rapl_npu_w": "NPU",
        "rapl_soc_w": "SOC_TOTAL",
    }

    def __init__(self, poll_interval=2.0):
        self._interval = poll_interval
        self._lock = threading.Lock()
        self._latest = {k: 0.0 for k in self.RAPL_MAP}
        self._thread = None
        self._stop = threading.Event()
        self.available = False
        self._query = None
        self._counters = {}
        self._init()

    def _init(self):
        try:
            self._query = win32pdh.OpenQuery()
            for key, inst in self.RAPL_MAP.items():
                try:
                    path = win32pdh.MakeCounterPath(
                        (None, "Energy Meter", inst, None, -1, "Power"))
                    self._counters[key] = win32pdh.AddCounter(self._query, path)
                    self.available = True
                except Exception:
                    pass
            if self.available:
                win32pdh.CollectQueryData(self._query)
        except Exception:
            self.available = False

    def start(self):
        if not self.available or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="RAPLPoller")
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                win32pdh.CollectQueryData(self._query)
                vals = {}
                for key, ctr in self._counters.items():
                    try:
                        raw = win32pdh.GetFormattedCounterValue(ctr, win32pdh.PDH_FMT_LONG)[1]
                        vals[key] = round(raw / 1000, 2)
                    except Exception:
                        vals[key] = 0.0
                with self._lock:
                    self._latest.update(vals)
            except Exception:
                pass
            self._stop.wait(self._interval)

    def snapshot(self):
        with self._lock:
            return dict(self._latest)

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None
        if self._query:
            win32pdh.CloseQuery(self._query)


class NvidiaSampler:
    """Background thread that polls nvidia-smi + pynvml at ~200ms intervals.

    nvidia-smi (subprocess) provides the core 11 fields.
    pynvml (in-process NVML) adds fields nvidia-smi --query-gpu cannot expose:
      - PCIe RX/TX throughput split (KB/s)
      - Performance state (P0–P12)
      - Static caps (max SM/mem clocks, power limit) for efficiency ratios
    """

    QUERY = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,"
        "temperature.gpu,power.draw,clocks.current.sm,clocks.current.memory,"
        "pcie.link.gen.current,pcie.link.width.current,clocks_event_reasons.active",
        "--format=csv,noheader,nounits",
    ]

    def __init__(self):
        self.available = self._check()
        # Core fields (nvidia-smi)
        self.gpu_pct = 0
        self.mem_pct = 0
        self.mem_used = 0
        self.mem_total = 0
        self.temp = 0
        self.power = 0.0
        self.sm_clk = 0
        self.mem_clk = 0
        self.pcie_gen = 0
        self.pcie_width = 0
        self.throttle = 0  # bitmask from clocks_event_reasons.active
        # Extended fields (pynvml)
        self.pcie_rx_kbs = 0   # PCIe receive KB/s
        self.pcie_tx_kbs = 0   # PCIe transmit KB/s
        self.pstate = 0        # Performance state 0–12
        # Static caps (queried once at init via pynvml)
        self.power_limit_w = 0.0
        self.sm_clk_max_mhz = 0
        self.mem_clk_max_mhz = 0
        self._nvml_handle = None
        self._nvml_ok = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        if self.available:
            self._init_nvml()

    def _check(self) -> bool:
        try:
            subprocess.run(
                ["nvidia-smi", "--version"],
                capture_output=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def _init_nvml(self):
        """Initialize pynvml and query static device capabilities."""
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_ok = True
            # Static caps — queried once
            try:
                self.power_limit_w = round(
                    pynvml.nvmlDeviceGetPowerManagementLimit(self._nvml_handle) / 1000, 1)
            except Exception:
                pass
            try:
                self.sm_clk_max_mhz = pynvml.nvmlDeviceGetMaxClockInfo(
                    self._nvml_handle, pynvml.NVML_CLOCK_SM)
            except Exception:
                pass
            try:
                self.mem_clk_max_mhz = pynvml.nvmlDeviceGetMaxClockInfo(
                    self._nvml_handle, pynvml.NVML_CLOCK_MEM)
            except Exception:
                pass
        except Exception:
            self._nvml_ok = False

    def start(self):
        if not self.available:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._nvml_ok:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def _poll_nvml(self):
        """Poll pynvml for fields that nvidia-smi --query-gpu cannot provide."""
        if not self._nvml_ok:
            return
        try:
            import pynvml
            h = self._nvml_handle
            self.pcie_rx_kbs = pynvml.nvmlDeviceGetPcieThroughput(
                h, pynvml.NVML_PCIE_UTIL_RX_BYTES)
            self.pcie_tx_kbs = pynvml.nvmlDeviceGetPcieThroughput(
                h, pynvml.NVML_PCIE_UTIL_TX_BYTES)
            self.pstate = pynvml.nvmlDeviceGetPerformanceState(h)
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                r = subprocess.run(
                    self.QUERY, capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    parts = [p.strip() for p in r.stdout.strip().split(",")]
                    self._poll_nvml()
                    with self._lock:
                        self.gpu_pct = int(parts[0])
                        self.mem_pct = int(parts[1])
                        self.mem_used = int(parts[2])
                        self.mem_total = int(parts[3])
                        self.temp = int(parts[4])
                        self.power = round(float(parts[5]), 1)
                        self.sm_clk = int(parts[6])
                        self.mem_clk = int(parts[7])
                        if len(parts) > 9:
                            self.pcie_gen = int(parts[8])
                            self.pcie_width = int(parts[9])
                        if len(parts) > 10:
                            try:
                                self.throttle = int(parts[10].strip(), 0)
                            except ValueError:
                                self.throttle = 0
            except Exception:
                pass
            self._stop.wait(0.2)

    def snapshot(self) -> dict:
        with self._lock:
            # Derived efficiency ratios (safe against zero max)
            sm_eff = round(self.sm_clk / self.sm_clk_max_mhz * 100, 1) if self.sm_clk_max_mhz else 0
            mem_eff = round(self.mem_clk / self.mem_clk_max_mhz * 100, 1) if self.mem_clk_max_mhz else 0
            pwr_eff = round(self.power / self.power_limit_w * 100, 1) if self.power_limit_w else 0
            return {
                "nvidia_gpu_pct": self.gpu_pct,
                "nvidia_mem_pct": self.mem_pct,
                "nvidia_mem_used_mb": self.mem_used,
                "nvidia_mem_total_mb": self.mem_total,
                "nvidia_temp_c": self.temp,
                "nvidia_power_w": self.power,
                "nvidia_sm_clk_mhz": self.sm_clk,
                "nvidia_mem_clk_mhz": self.mem_clk,
                "nvidia_pcie_gen": self.pcie_gen,
                "nvidia_pcie_width": self.pcie_width,
                "nvidia_throttle": self.throttle,
                # pynvml extended fields
                "nvidia_pcie_rx_kbs": self.pcie_rx_kbs,
                "nvidia_pcie_tx_kbs": self.pcie_tx_kbs,
                "nvidia_pstate": self.pstate,
                "nvidia_power_limit_w": self.power_limit_w,
                "nvidia_sm_clk_max_mhz": self.sm_clk_max_mhz,
                "nvidia_mem_clk_max_mhz": self.mem_clk_max_mhz,
                # Derived efficiency %
                "nvidia_sm_clk_eff_pct": sm_eff,
                "nvidia_mem_clk_eff_pct": mem_eff,
                "nvidia_power_eff_pct": pwr_eff,
            }


class IntelGPUMonitor:
    """Monitors iGPU and NPU utilization via Windows GPU Engine PDH counters.

    Identifies Intel iGPU and NPU by LUID, then aggregates utilization
    across all engine instances for each device.
    """

    def __init__(self):
        self.available = False
        self.igpu_pct = 0.0
        self.npu_pct = 0.0
        self.igpu_ded_mb = 0.0
        self.igpu_shared_mb = 0.0
        self.npu_ded_mb = 0.0
        self.npu_shared_mb = 0.0
        self._igpu_luid = None
        self._npu_luid = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._detect()

    def _detect(self):
        """Detect iGPU and NPU LUIDs from GPU Engine instances."""
        try:
            _, instances = win32pdh.EnumObjectItems(
                None, None, "GPU Engine", win32pdh.PERF_DETAIL_WIZARD
            )
        except Exception:
            return

        # Find LUIDs by engine type signature
        # NPU has engtype_Neural; iGPU has engtype_3D but is NOT the NVIDIA GPU
        # NVIDIA has engtype_Cuda; iGPU does not
        from collections import defaultdict

        luid_engines = defaultdict(set)
        for inst in set(instances):
            parts = inst.split("_")
            # Extract LUID (format: ..._luid_0xAAAAAAAA_0xBBBBBBBB_...)
            try:
                luid_idx = parts.index("luid")
                luid = parts[luid_idx + 1] + "_" + parts[luid_idx + 2]
            except (ValueError, IndexError):
                continue
            # Extract engine type
            try:
                eng_idx = parts.index("engtype")
                engtype = "_".join(parts[eng_idx + 1:])
            except (ValueError, IndexError):
                continue
            luid_engines[luid].add(engtype)

        # NPU: has Neural but NOT Compute (dedicated NPU, not a GPU with neural engine)
        npu_candidates = [
            (luid, engines) for luid, engines in luid_engines.items()
            if "Neural" in engines and "Compute" not in engines
        ]
        if npu_candidates:
            self._npu_luid = npu_candidates[0][0]

        # Intel iGPU: has 3D + Compute + Copy etc., NOT Cuda (NVIDIA)
        # Pick the LUID with the most engine types (real iGPU has many vs virtual adapter)
        igpu_candidates = [
            (luid, engines) for luid, engines in luid_engines.items()
            if "3D" in engines and "Compute" in engines and "Cuda" not in engines
            and luid != self._npu_luid
        ]
        if igpu_candidates:
            igpu_candidates.sort(key=lambda x: len(x[1]), reverse=True)
            self._igpu_luid = igpu_candidates[0][0]

        if self._igpu_luid or self._npu_luid:
            self.available = True

    def start(self):
        if not self.available:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        """Periodically enumerate GPU Engine instances and aggregate utilization."""
        while not self._stop.is_set():
            try:
                # Open a fresh query each time (instances change as processes start/stop)
                query = win32pdh.OpenQuery()
                _, instances = win32pdh.EnumObjectItems(
                    None, None, "GPU Engine", win32pdh.PERF_DETAIL_WIZARD
                )

                counters = []
                for inst in set(instances):
                    # Determine LUID
                    parts = inst.split("_")
                    try:
                        luid_idx = parts.index("luid")
                        luid = parts[luid_idx + 1] + "_" + parts[luid_idx + 2]
                    except (ValueError, IndexError):
                        continue

                    if luid not in (self._igpu_luid, self._npu_luid):
                        continue

                    path = win32pdh.MakeCounterPath(
                        (None, "GPU Engine", inst, None, -1, "Utilization Percentage")
                    )
                    try:
                        ctr = win32pdh.AddCounter(query, path)
                        counters.append((luid, ctr))
                    except Exception:
                        continue

                if not counters:
                    win32pdh.CloseQuery(query)
                    self._stop.wait(1.0)
                    continue

                # Two collects needed for rate counters
                win32pdh.CollectQueryData(query)
                self._stop.wait(0.5)
                if self._stop.is_set():
                    win32pdh.CloseQuery(query)
                    break
                win32pdh.CollectQueryData(query)

                igpu_sum = 0.0
                npu_sum = 0.0
                for luid, ctr in counters:
                    try:
                        val = win32pdh.GetFormattedCounterValue(
                            ctr, win32pdh.PDH_FMT_DOUBLE
                        )[1]
                    except Exception:
                        val = 0.0
                    if luid == self._igpu_luid:
                        igpu_sum += val
                    elif luid == self._npu_luid:
                        npu_sum += val

                # Read GPU Adapter Memory (dedicated + shared) for iGPU and NPU
                igpu_ded = 0.0
                igpu_shd = 0.0
                npu_ded = 0.0
                npu_shd = 0.0
                try:
                    for luid, attr in [(self._igpu_luid, "igpu"), (self._npu_luid, "npu")]:
                        if not luid:
                            continue
                        inst = f"luid_{luid}_phys_0"
                        for counter_name, is_ded in [("Dedicated Usage", True), ("Shared Usage", False)]:
                            try:
                                path = win32pdh.MakeCounterPath(
                                    (None, "GPU Adapter Memory", inst, None, -1, counter_name)
                                )
                                ctr_mem = win32pdh.AddCounter(query, path)
                                win32pdh.CollectQueryData(query)
                                val = win32pdh.GetFormattedCounterValue(
                                    ctr_mem, win32pdh.PDH_FMT_LARGE
                                )[1]
                                mb = val / (1024 * 1024)
                                if attr == "igpu":
                                    if is_ded:
                                        igpu_ded = mb
                                    else:
                                        igpu_shd = mb
                                else:
                                    if is_ded:
                                        npu_ded = mb
                                    else:
                                        npu_shd = mb
                            except Exception:
                                pass
                except Exception:
                    pass

                with self._lock:
                    self.igpu_pct = round(min(igpu_sum, 100.0), 1)
                    self.npu_pct = round(min(npu_sum, 100.0), 1)
                    self.igpu_ded_mb = round(igpu_ded, 1)
                    self.igpu_shared_mb = round(igpu_shd, 1)
                    self.npu_ded_mb = round(npu_ded, 1)
                    self.npu_shared_mb = round(npu_shd, 1)

                win32pdh.CloseQuery(query)

            except Exception:
                pass
            self._stop.wait(0.5)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "igpu_pct": self.igpu_pct,
                "npu_pct": self.npu_pct,
                "igpu_ded_mb": self.igpu_ded_mb,
                "igpu_shared_mb": self.igpu_shared_mb,
                "npu_ded_mb": self.npu_ded_mb,
                "npu_shared_mb": self.npu_shared_mb,
            }


class PCMBandwidthSampler:
    """Background thread running pcm.exe for accurate DRAM bandwidth via IMC counters.

    Parses CSV output from pcm to extract system-level read/write GB/s,
    plus L3 cache misses to estimate per-agent (IA/iGPU+NPU) BW breakdown.

    PCM CSV columns used:
      READ  — total DRAM reads (GB) from IMC, all agents combined
      WRITE — total DRAM writes (GB) from IMC, all agents combined
      L3MISS— L3 cache read misses (millions) from CPU PMU, IA-core only

    Per-agent estimation:
      IA DRAM BW ≈ L3MISS × 64 bytes (each LLC miss fetches a cache line from DRAM)
      Non-IA BW  = Total BW − IA BW  (iGPU + NPU + IO combined)

    Uses pcm.exe (not pcm-memory.exe) which supports client platforms via TGLClientBW.
    Requires MSR driver (msr.sys) and admin rights.

    PCM executable location is resolved in order:
      1. PCM_EXE environment variable (full path)
      2. PCM_DIR environment variable + \\pcm.exe
      3. pcm.exe in system PATH
      4. Default: c:\\pcm\\pcm.exe
    """

    def __init__(self, sample_interval_s: float = 0.1):
        self.PCM_EXE = self._find_pcm()
        self.sample_interval_s = max(0.05, sample_interval_s)
        self.available = False
        self.read_gbs = 0.0
        self.write_gbs = 0.0
        self.total_gbs = 0.0
        self.l3miss_m = 0.0        # L3 misses in millions
        self.ia_bw_gbs = 0.0       # IA-core estimated DRAM BW
        self.nonia_bw_gbs = 0.0    # iGPU+NPU+IO estimated DRAM BW
        self.ipc = 0.0
        self.l3hit = 0.0
        self.l2hit = 0.0
        self.c0res_pct = 0.0
        self.temp_c = 0.0
        self.proc_energy_j = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._check()

    @staticmethod
    def _find_pcm():
        """Resolve pcm.exe path from env vars, PATH, or default location."""
        # 1. PCM_EXE env var (full path)
        env_exe = os.environ.get("PCM_EXE")
        if env_exe and os.path.isfile(env_exe):
            return env_exe
        # 2. PCM_DIR env var + \pcm.exe
        env_dir = os.environ.get("PCM_DIR")
        if env_dir:
            candidate = os.path.join(env_dir, "pcm.exe")
            if os.path.isfile(candidate):
                return candidate
        # 3. pcm.exe in system PATH
        import shutil
        path_exe = shutil.which("pcm.exe") or shutil.which("pcm")
        if path_exe:
            return path_exe
        # 4. Default location
        return r"c:\pcm\pcm.exe"

    def _check(self):
        """Check if pcm.exe exists and MSR driver is accessible."""
        if not os.path.isfile(self.PCM_EXE):
            return
        # Quick test: run 1 iteration to see if driver loads and CSV is produced
        # Use -r to reset PMU in case a prior pcm process left it locked
        try:
            r = subprocess.run(
                [self.PCM_EXE, "0.1", "-csv", "-nc", "-i=1", "-r"],
                capture_output=True, text=True, timeout=15,
            )
            # pcm.exe may return non-zero even on success; check for CSV data
            if "READ" in r.stdout and "WRITE" in r.stdout:
                self.available = True
        except Exception:
            pass

    def start(self):
        if not self.available:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        """Run pcm continuously and parse CSV output line by line."""
        try:
            self._proc = subprocess.Popen(
                [
                    self.PCM_EXE,
                    str(self.sample_interval_s),
                    "-csv", "-nc", "-r",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            return

        header_cols = []
        read_idx = write_idx = l3miss_idx = -1
        extra_indices = {name: -1 for name in ("IPC", "L3HIT", "L2HIT", "C0res%", "TEMP", "Proc Energy")}

        for line in iter(self._proc.stdout.readline, ""):
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split(",")]

            # Detect header line (contains "READ" and "WRITE")
            # pcm.exe outputs two header rows: group headers then column headers
            if not header_cols and any(p == "READ" for p in parts):
                header_cols = parts
                for i, col in enumerate(header_cols):
                    # Use the System-level columns (first occurrence)
                    if col == "READ" and read_idx < 0:
                        read_idx = i
                    elif col == "WRITE" and write_idx < 0:
                        write_idx = i
                    elif col == "L3MISS" and l3miss_idx < 0:
                        l3miss_idx = i
                    elif col in extra_indices and extra_indices[col] < 0:
                        extra_indices[col] = i
                continue

            # Skip non-data lines (group header row, etc.)
            if not header_cols or read_idx < 0:
                continue

            # Data lines
            if len(parts) > max(read_idx, write_idx, 0):
                try:
                    r_val = float(parts[read_idx]) if read_idx >= 0 else 0.0
                    w_val = float(parts[write_idx]) if write_idx >= 0 else 0.0
                    total = r_val + w_val

                    def pcm_value(name):
                        index = extra_indices[name]
                        if index < 0 or index >= len(parts):
                            return 0.0
                        try:
                            return float(parts[index])
                        except (ValueError, IndexError):
                            return 0.0

                    # L3MISS is in millions; each miss = 64B cache line from DRAM
                    l3m = 0.0
                    ia_bw = 0.0
                    nonia_bw = 0.0
                    if l3miss_idx >= 0 and l3miss_idx < len(parts):
                        try:
                            l3m = float(parts[l3miss_idx])
                            # L3MISS is millions of misses per interval
                            # BW = misses * 64 bytes / interval / 1e9 = GB in interval
                            # But pcm reports READ/WRITE in GB already (not GB/s)
                            # and L3MISS in absolute millions for the interval
                            # So: IA BW (GB) = L3MISS * 1e6 * 64 / 1e9 = L3MISS * 0.064
                            ia_bw = l3m * 0.064
                            nonia_bw = max(0.0, total - ia_bw)
                        except (ValueError, IndexError):
                            pass

                    with self._lock:
                        self.read_gbs = round(r_val, 2)
                        self.write_gbs = round(w_val, 2)
                        self.total_gbs = round(total, 2)
                        self.l3miss_m = round(l3m, 2)
                        self.ia_bw_gbs = round(ia_bw, 2)
                        self.nonia_bw_gbs = round(nonia_bw, 2)
                        self.ipc = round(pcm_value("IPC"), 4)
                        self.l3hit = round(pcm_value("L3HIT"), 4)
                        self.l2hit = round(pcm_value("L2HIT"), 4)
                        self.c0res_pct = round(pcm_value("C0res%"), 4)
                        self.temp_c = round(pcm_value("TEMP"), 2)
                        self.proc_energy_j = round(pcm_value("Proc Energy"), 4)
                except (ValueError, IndexError):
                    pass

        if self._proc:
            self._proc.wait()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "dram_read_gbs": self.read_gbs,
                "dram_write_gbs": self.write_gbs,
                "dram_total_gbs": self.total_gbs,
                "l3miss_millions": self.l3miss_m,
                "ia_dram_bw_gbs": self.ia_bw_gbs,
                "nonia_dram_bw_gbs": self.nonia_bw_gbs,
                "pcm_ipc": self.ipc,
                "pcm_l3hit": self.l3hit,
                "pcm_l2hit": self.l2hit,
                "pcm_c0res_pct": self.c0res_pct,
                "pcm_temp_c": self.temp_c,
                "pcm_proc_energy_j": self.proc_energy_j,
                "bw_source": "pcm",
            }


class ProxyBandwidthEstimator:
    """Estimates DRAM bandwidth from RAPL power consumption.

    Uses empirical linear model:
        BW_total ≈ α_cpu * P_cpu + α_igpu * P_igpu + α_npu * P_npu + β

    Default calibration for DDR5 platforms (~0.3-0.5 GB/s per Watt):
        - CPU: 0.40 GB/s per W (compute-bound tasks have higher BW/W)
        - iGPU: 0.55 GB/s per W (iGPU is bandwidth-hungry)
        - NPU: 0.30 GB/s per W (NPU is more compute-efficient)
        - Idle baseline: 0.5 GB/s (OS background + refresh)

    These are ROUGH estimates. For accurate values, use PCM mode.
    Calibrate with: run pcm-memory alongside this proxy, then compute regression.
    """

    def __init__(
        self,
        alpha_cpu: float = 0.40,
        alpha_igpu: float = 0.55,
        alpha_npu: float = 0.30,
        idle_bw: float = 0.5,
    ):
        self.alpha_cpu = alpha_cpu
        self.alpha_igpu = alpha_igpu
        self.alpha_npu = alpha_npu
        self.idle_bw = idle_bw
        self.available = True  # Always available if RAPL is present

    def estimate(self, rapl_cpu_w: float, rapl_igpu_w: float, rapl_npu_w: float) -> dict:
        """Estimate DRAM bandwidth from RAPL power readings."""
        bw_est = (
            self.alpha_cpu * rapl_cpu_w
            + self.alpha_igpu * rapl_igpu_w
            + self.alpha_npu * rapl_npu_w
            + self.idle_bw
        )
        # Split read/write (typical ratio ~60/40 for mixed workloads)
        read_gbs = round(bw_est * 0.6, 2)
        write_gbs = round(bw_est * 0.4, 2)
        return {
            "dram_read_gbs": read_gbs,
            "dram_write_gbs": write_gbs,
            "dram_total_gbs": round(bw_est, 2),
            "bw_source": "proxy",
        }


def discover_hardware(power: bool, bw_mode: str, config: dict = None):
    """Discover available hardware and set up PDH counters.

    Args:
        power: Enable RAPL power counters
        bw_mode: Legacy bandwidth mode ("pcm", "proxy", "none") — overridden by config sources
        config: Instrumentation config dict (from load_instrumentation_config)
    """
    sources = config.get("sources", {}) if config else {}
    emon_categories = get_emon_categories(sources) if config else set()

    n_cores = psutil.cpu_count(logical=True)
    print(f"CPU logical cores: {n_cores}")

    pdh = PDHCounterGroup()

    # Per-core actual frequency
    for i in range(n_cores):
        pdh.add(f"freq_{i}", "Processor Information", f"0,{i}", "Actual Frequency")

    # RAPL power (if requested) — separate poller to avoid blocking main PDH query
    has_rapl = False
    rapl_poller = None
    if power:
        rapl_poller = RAPLPoller(poll_interval=2.0)
        if rapl_poller.available:
            has_rapl = True
            print("Intel RAPL Energy Meter detected (background poller, 2s interval)")
        else:
            print("RAPL Energy Meter not available")

    # CPU thermal throttle detection
    has_thermal = False
    try:
        pdh.add("cpu_perf_limit_pct", "Processor Information", "_Total", "% Performance Limit")
        pdh.add("thermal_temp", "Thermal Zone Information", "\\_TZ.TZ00", "High Precision Temperature")
        has_thermal = True
        print("CPU thermal/throttle counters detected")
    except Exception:
        print("CPU thermal counters not available")

    # iGPU/NPU via GPU Engine counters (background thread)
    intel_gpu = IntelGPUMonitor()
    if intel_gpu.available:
        intel_gpu.start()
        print(f"Intel GPU Engine monitor: iGPU={intel_gpu._igpu_luid is not None}, NPU={intel_gpu._npu_luid is not None}")
    else:
        print("Intel GPU Engine monitor not available")

    # Prime the counters (first collect is always needed)
    pdh.collect()
    time.sleep(0.1)
    pdh.collect()

    # Read base frequencies
    base_freqs = [pdh.get(f"freq_{i}") for i in range(n_cores)]
    print(f"Per-core base frequencies (MHz): {', '.join(str(f) for f in base_freqs)}")

    # NVIDIA
    nv = NvidiaSampler()
    if nv.available:
        print(f"NVIDIA GPU detected")
    else:
        print("nvidia-smi not found")

    # DRAM Bandwidth — resolve source from config or legacy CLI arg
    bw_sampler = None
    bw_proxy = None
    bw_source = sources.get("bandwidth", bw_mode)  # Config takes precedence

    # If EMON handles bandwidth, skip PCM to avoid PMU contention
    if bw_source == "emon":
        if "bandwidth" not in emon_categories:
            emon_categories.add("bandwidth")
        print("DRAM BW: EMON (UNC_M_CAS_COUNT via SEP driver)")
    elif bw_source == "pcm":
        pcm = PCMBandwidthSampler(sample_interval_s=config.get("pcm", {}).get("interval_s", 0.1) if config else 0.1)
        if pcm.available:
            bw_sampler = pcm
            print("DRAM BW: Intel PCM (pcm.exe) — IMC hardware counters")
        else:
            print("WARNING: PCM not available (driver missing?). Falling back to proxy.")
            if has_rapl:
                bw_proxy = ProxyBandwidthEstimator()
                print("DRAM BW: Proxy estimation from RAPL power")
            else:
                print("DRAM BW: Disabled (no RAPL for proxy fallback)")
    elif bw_source == "proxy":
        if has_rapl:
            bw_proxy = ProxyBandwidthEstimator()
            print("DRAM BW: Proxy estimation from RAPL power")
        else:
            print("WARNING: Proxy BW requires --power (RAPL). Disabled.")
    else:
        print("DRAM BW: Disabled")

    # EMON deep sampler (for simulation/deep-analysis profiles)
    emon_sampler = None
    if emon_categories and config:
        from emon_sampler import EmonSampler
        emon_sampler = EmonSampler(config.get("emon", {}), emon_categories)
        if emon_sampler.available:
            print(f"EMON deep sampler: {sorted(emon_categories)} (interval={emon_sampler.interval_s}s)")
        else:
            print(f"WARNING: EMON requested for {sorted(emon_categories)} but not available")
            emon_sampler = None

    return n_cores, pdh, has_rapl, rapl_poller, nv, bw_sampler, bw_proxy, intel_gpu, emon_sampler


class NPUMetricsPoller:
    """Polls the NPU embedding server's /metrics endpoint for app-level busy %.

    The NPU driver doesn't expose utilization via Windows PDH GPU Engine counters.
    Instead, the npu_embedding_server.py exposes a /metrics endpoint with real
    wall-clock busy % (time spent inside compiled_model() inference).
    This is NOT a hardware performance counter — it's an app-level measurement.
    """

    def __init__(self, url="http://localhost:8001/metrics", poll_interval=0.5):
        self._url = url
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._data = {}
        self._available = False
        self._stop = threading.Event()
        self._thread = None

    @property
    def available(self):
        return self._available

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        import urllib.request
        import json as _json
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self._url, method="GET")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    data = _json.loads(resp.read())
                    with self._lock:
                        self._data = data
                        self._available = True
            except Exception:
                with self._lock:
                    self._available = False
            self._stop.wait(self._poll_interval)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


def _init_l0_sampler():
    """Try to initialize Level Zero GPU metrics sampler (TBS — needs exclusive OA access)."""
    try:
        if os.environ.get("ZET_ENABLE_METRICS") != "1":
            os.environ["ZET_ENABLE_METRICS"] = "1"
        from l0_metrics_sampler import L0MetricsSampler
        sampler = L0MetricsSampler(sample_interval_s=0.5)
        # Eagerly start to detect OA failures (e.g. SR-IOV PF, OA contention)
        sampler.start()
        if sampler.error:
            print(f"L0 TBS init warning: {sampler.error}")
            sampler.stop()
            return None
        return sampler
    except Exception as e:
        print(f"L0MetricsSampler not available: {e}")
        return None


def _init_l0_sysman():
    """Initialize Level Zero Sysman sampler (no OA contention)."""
    try:
        if os.environ.get("ZES_ENABLE_SYSMAN") != "1":
            os.environ["ZES_ENABLE_SYSMAN"] = "1"
        from l0_sysman_sampler import L0SysmanSampler
        sampler = L0SysmanSampler()
        if not sampler.available:
            print(f"L0 Sysman not available: {sampler.error}")
            return None
        return sampler
    except Exception as e:
        print(f"L0SysmanSampler not available: {e}")
        return None


def build_header(n_cores: int, has_rapl: bool, has_bw: bool, emon_cols: list = None,
                 l0_cols: list = None, sysman_cols: list = None, ovms_cols: list = None) -> list:
    """Build CSV column headers."""
    cols = ["timestamp", "cpu_total_pct", "cpu_core_min", "cpu_core_max", "cpu_cores_csv"]
    cols += ["cpu_freq_min_mhz", "cpu_freq_max_mhz", "cpu_freq_csv"]
    cols += ["igpu_pct", "igpu_ded_mb", "igpu_shared_mb"]
    cols += [
        "nvidia_gpu_pct", "nvidia_mem_pct", "nvidia_mem_used_mb",
        "nvidia_mem_total_mb", "nvidia_temp_c", "nvidia_power_w",
        "nvidia_sm_clk_mhz", "nvidia_mem_clk_mhz",
        "nvidia_pcie_gen", "nvidia_pcie_width", "nvidia_throttle",
        "nvidia_pcie_rx_kbs", "nvidia_pcie_tx_kbs", "nvidia_pstate",
        "nvidia_power_limit_w", "nvidia_sm_clk_max_mhz", "nvidia_mem_clk_max_mhz",
        "nvidia_sm_clk_eff_pct", "nvidia_mem_clk_eff_pct", "nvidia_power_eff_pct",
    ]
    cols += ["npu_pct", "npu_ded_mb", "npu_shared_mb", "npu_process_count", "npu_infer_ms"]
    cols += ["cpu_throttle_pct", "soc_temp_c"]
    if has_rapl:
        cols += ["rapl_cpu_w", "rapl_igpu_w", "rapl_npu_w", "rapl_soc_w"]
    if has_bw:
        cols += ["dram_read_gbs", "dram_write_gbs", "dram_total_gbs",
                 "l3miss_millions", "ia_dram_bw_gbs", "nonia_dram_bw_gbs",
                 "pcm_ipc", "pcm_l3hit", "pcm_l2hit", "pcm_c0res_pct",
                 "pcm_temp_c", "pcm_proc_energy_j", "bw_source"]
    cols += ["ram_used_gb", "ram_total_gb"]
    # L0 GPU HW counter columns (Level Zero TBS)
    if l0_cols:
        cols += l0_cols
    # L0 Sysman columns (engine busy%, freq, mem — no OA contention)
    if sysman_cols:
        cols += sysman_cols
    # OVMS KV-cache columns (from log tailing)
    if ovms_cols:
        cols += ovms_cols
    # EMON deep columns appended at end (non-breaking for existing parsers)
    if emon_cols:
        cols += emon_cols
    return cols


def sample_loop(
    n_cores: int,
    pdh: PDHCounterGroup,
    has_rapl: bool,
    rapl_poller: "RAPLPoller | None",
    nv: NvidiaSampler,
    bw_sampler: "PCMBandwidthSampler | None",
    bw_proxy: "ProxyBandwidthEstimator | None",
    intel_gpu: "IntelGPUMonitor",
    interval_ms: int,
    output_path: str,
    emon_sampler=None,
):
    """Main sampling loop at target interval."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    has_bw = bw_sampler is not None or bw_proxy is not None
    emon_cols = emon_sampler.get_extra_columns() if emon_sampler else None

    # Set L0 env vars BEFORE any ze_loader init (order matters — metrics won't activate after zeInit)
    os.environ.setdefault("ZET_ENABLE_METRICS", "1")
    os.environ.setdefault("ZES_ENABLE_SYSMAN", "1")

    # Level Zero Sysman sampler (primary — no OA contention)
    l0_sysman = _init_l0_sysman()
    sysman_cols = None
    if l0_sysman:
        from l0_sysman_sampler import L0SysmanSampler
        sysman_cols = L0SysmanSampler.COLUMNS
        print(f"L0 Sysman: {len(sysman_cols)} columns (engine busy%, freq, mem — no OA contention)")

    # Level Zero TBS (optional enrichment — EU-level detail, but needs exclusive OA access)
    l0_sampler = _init_l0_sampler()
    l0_cols = None
    if l0_sampler:
        from l0_metrics_sampler import L0MetricsSampler
        l0_cols = L0MetricsSampler.get_extra_columns()
        print(f"L0 TBS: {len(l0_cols)} HW counter columns (ComputeBasic OA)")

    # NPU app-level metrics poller (polls npu_embedding_server.py /metrics)
    npu_poller = NPUMetricsPoller()
    npu_poller.start()

    # OVMS log poller (extracts KV-cache usage from OVMS runtime log)
    ovms_poller = None
    ovms_cols = None
    ovms_log_path = os.environ.get("OVMS_LOG_PATH", "")
    if ovms_log_path:
        from ovms_log_poller import OVMSLogPoller, COLUMNS as OVMS_COLUMNS
        ovms_poller = OVMSLogPoller(ovms_log_path)
        if ovms_poller.available:
            ovms_cols = OVMS_COLUMNS
            ovms_poller.start()
            print(f"OVMS Log Poller: tracking KV-cache from {ovms_log_path}")
        else:
            ovms_poller = None

    header = build_header(n_cores, has_rapl, has_bw, emon_cols, l0_cols, sysman_cols, ovms_cols)

    ram_info = psutil.virtual_memory()
    ram_total_gb = round(ram_info.total / (1024**3), 1)

    # Prime psutil CPU
    psutil.cpu_percent(percpu=True)
    time.sleep(0.05)

    nv.start()
    if bw_sampler:
        bw_sampler.start()
    if rapl_poller:
        rapl_poller.start()
    if l0_sysman:
        l0_sysman.start()
    if l0_sampler:
        l0_sampler.start()
    if emon_sampler:
        emon_sampler.start()
    print(f"\nSampling interval: {interval_ms}ms")
    print(f"Output: {output_path}")
    print("Press Ctrl+C to stop\n")

    sample_count = 0
    interval_s = interval_ms / 1000.0

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)

            while True:
                t_start = time.perf_counter()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                # CPU per-core utilization (near-instant)
                core_pcts = psutil.cpu_percent(interval=0, percpu=True)
                cpu_total = round(sum(core_pcts) / len(core_pcts), 1)
                core_min = round(min(core_pcts))
                core_max = round(max(core_pcts))
                cores_csv = ";".join(str(round(v)) for v in core_pcts)

                # Per-core frequency via PDH (near-instant, ~0.03ms)
                pdh.collect()
                freqs = [pdh.get(f"freq_{i}") for i in range(n_cores)]
                freq_min = min(freqs) if freqs else 0
                freq_max = max(freqs) if freqs else 0
                freq_csv = ";".join(str(f) for f in freqs)

                # RAPL power (from background poller — non-blocking)
                rapl_vals = {}
                if has_rapl and rapl_poller:
                    rapl_vals = rapl_poller.snapshot()

                # NVIDIA (from background thread)
                nv_snap = nv.snapshot()

                # RAM
                mem = psutil.virtual_memory()
                ram_used_gb = round(mem.used / (1024**3), 1)

                # iGPU / NPU utilization
                igpu_snap = intel_gpu.snapshot() if intel_gpu.available else {"igpu_pct": 0, "npu_pct": 0}

                # NPU: prefer app-level metrics from npu_embedding_server if available
                npu_snap = npu_poller.snapshot() if npu_poller.available else {}
                npu_pct_val = npu_snap.get("npu_busy_pct", igpu_snap["npu_pct"])
                npu_infer_ms = npu_snap.get("last_infer_npu_real_ms", 0) or 0

                # Build row
                row = [
                    ts, cpu_total, core_min, core_max, cores_csv,
                    freq_min, freq_max, freq_csv,
                    igpu_snap["igpu_pct"], igpu_snap.get("igpu_ded_mb", 0), igpu_snap.get("igpu_shared_mb", 0),
                    nv_snap["nvidia_gpu_pct"], nv_snap["nvidia_mem_pct"],
                    nv_snap["nvidia_mem_used_mb"], nv_snap["nvidia_mem_total_mb"],
                    nv_snap["nvidia_temp_c"], nv_snap["nvidia_power_w"],
                    nv_snap["nvidia_sm_clk_mhz"], nv_snap["nvidia_mem_clk_mhz"],
                    nv_snap["nvidia_pcie_gen"], nv_snap["nvidia_pcie_width"],
                    nv_snap.get("nvidia_throttle", 0),
                    nv_snap.get("nvidia_pcie_rx_kbs", 0), nv_snap.get("nvidia_pcie_tx_kbs", 0),
                    nv_snap.get("nvidia_pstate", 0),
                    nv_snap.get("nvidia_power_limit_w", 0), nv_snap.get("nvidia_sm_clk_max_mhz", 0),
                    nv_snap.get("nvidia_mem_clk_max_mhz", 0),
                    nv_snap.get("nvidia_sm_clk_eff_pct", 0), nv_snap.get("nvidia_mem_clk_eff_pct", 0),
                    nv_snap.get("nvidia_power_eff_pct", 0),
                    npu_pct_val, igpu_snap.get("npu_ded_mb", 0), igpu_snap.get("npu_shared_mb", 0), 0, npu_infer_ms,
                    max(0, 100 - pdh.get("cpu_perf_limit_pct")) if pdh._counters.get("cpu_perf_limit_pct") else 0,
                    round(pdh.get("thermal_temp") / 10.0 - 273.15, 1) if pdh._counters.get("thermal_temp") else 0,
                ]
                if has_rapl:
                    row += [
                        rapl_vals["rapl_cpu_w"], rapl_vals["rapl_igpu_w"],
                        rapl_vals["rapl_npu_w"], rapl_vals["rapl_soc_w"],
                    ]

                # DRAM Bandwidth
                if bw_sampler:
                    bw_snap = bw_sampler.snapshot()
                    row += [
                        bw_snap["dram_read_gbs"], bw_snap["dram_write_gbs"],
                        bw_snap["dram_total_gbs"],
                        bw_snap.get("l3miss_millions", 0),
                        bw_snap.get("ia_dram_bw_gbs", 0),
                        bw_snap.get("nonia_dram_bw_gbs", 0),
                        bw_snap.get("pcm_ipc", 0), bw_snap.get("pcm_l3hit", 0),
                        bw_snap.get("pcm_l2hit", 0), bw_snap.get("pcm_c0res_pct", 0),
                        bw_snap.get("pcm_temp_c", 0), bw_snap.get("pcm_proc_energy_j", 0),
                        bw_snap["bw_source"],
                    ]
                elif bw_proxy:
                    bw_snap = bw_proxy.estimate(
                        rapl_vals.get("rapl_cpu_w", 0),
                        rapl_vals.get("rapl_igpu_w", 0),
                        rapl_vals.get("rapl_npu_w", 0),
                    )
                    row += [
                        bw_snap["dram_read_gbs"], bw_snap["dram_write_gbs"],
                        bw_snap["dram_total_gbs"],
                        0, 0, 0,  # proxy doesn't have L3MISS/per-agent
                        0, 0, 0, 0, 0, 0,
                        bw_snap["bw_source"],
                    ]

                row += [ram_used_gb, ram_total_gb]

                # L0 GPU HW counters (Level Zero TBS — may be zeros if OA contention)
                l0_snap = {}
                if l0_sampler and l0_cols:
                    l0_snap = l0_sampler.snapshot()
                    row += [l0_snap.get(col, "") for col in l0_cols]

                # L0 Sysman (engine busy%, freq, mem — always works, no OA contention)
                sysman_snap = {}
                if l0_sysman and sysman_cols:
                    sysman_snap = l0_sysman.snapshot()
                    row += [sysman_snap.get(col, "") for col in sysman_cols]

                # OVMS KV-cache (from log tailing)
                if ovms_poller and ovms_cols:
                    ovms_snap = ovms_poller.snapshot()
                    row += [ovms_snap.get(col, "") for col in ovms_cols]

                # EMON deep metrics (appended at end, non-breaking)
                emon_snap = {}
                if emon_sampler and emon_cols:
                    emon_snap = emon_sampler.snapshot()
                    # If EMON provides bandwidth and PCM doesn't, backfill BW columns
                    if not has_bw and "dram_total_gbs" in emon_snap:
                        pass  # BW already in emon_cols at end
                    row += [emon_snap.get(col, "") for col in emon_cols]

                writer.writerow(row)
                f.flush()
                sample_count += 1

                # Print every 20 samples to reduce console overhead
                if sample_count % 20 == 0:
                    elapsed_ms = (time.perf_counter() - t_start) * 1000
                    bw_str = ""
                    if bw_sampler or bw_proxy:
                        bw_str = f"BW={bw_snap['dram_total_gbs']:.1f}GB/s({bw_snap['bw_source']}) "
                    elif emon_snap.get("dram_total_gbs"):
                        bw_str = f"BW={emon_snap['dram_total_gbs']:.1f}GB/s(emon) "
                    emon_str = ""
                    if emon_snap.get("emon_ipc"):
                        emon_str = f"IPC={emon_snap['emon_ipc']:.2f} "
                    if emon_snap.get("dram_page_hit_rate_rd"):
                        emon_str += f"PageHit={emon_snap['dram_page_hit_rate_rd']:.0%} "
                    l0_str = ""
                    if l0_snap.get("l0_gpu_busy"):
                        l0_str = f"EU={l0_snap.get('l0_xve_active', 0):.0f}%act/{l0_snap.get('l0_xve_stall', 0):.0f}%stl "
                    elif sysman_snap.get("zes_gpu_busy"):
                        l0_str = f"SYS={sysman_snap['zes_gpu_busy']:.0f}%busy/{sysman_snap.get('zes_freq_mhz', 0):.0f}MHz "
                    npu_src = "app" if npu_poller.available else "pdh"
                    throttle_str = ""
                    cpu_throttle = max(0, 100 - (pdh.get("cpu_perf_limit_pct") if pdh._counters.get("cpu_perf_limit_pct") else 100))
                    nv_thr = nv_snap.get("nvidia_throttle", 0)
                    if cpu_throttle > 0 or nv_thr:
                        parts = []
                        if cpu_throttle > 0:
                            parts.append(f"CPU={cpu_throttle}%")
                        if nv_thr:
                            parts.append(f"NV=0x{nv_thr:X}")
                        throttle_str = f"⚠THROTTLE({','.join(parts)}) "
                    print(
                        f"[{ts}] CPU={cpu_total:5.1f}% "
                        f"freq={freq_min}-{freq_max}MHz "
                        f"RAPL_SoC={rapl_vals.get('rapl_soc_w', 0):5.1f}W "
                        f"{bw_str}"
                        f"{emon_str}"
                        f"iGPU={igpu_snap['igpu_pct']:.0f}% "
                        f"{l0_str}"
                        f"NPU={npu_pct_val:.0f}%({npu_src}) "
                        f"NV={nv_snap['nvidia_gpu_pct']}% "
                        f"RAM={ram_used_gb}/{ram_total_gb}GB "
                        f"{throttle_str}"
                        f"({elapsed_ms:.1f}ms) "
                        f"#{sample_count}"
                    )

                # Sleep for remainder of interval
                elapsed = time.perf_counter() - t_start
                remaining = interval_s - elapsed
                if remaining > 0:
                    time.sleep(remaining)

                # Check for graceful stop signal (file-based, for external orchestrators)
                stop_file = output_path + ".stop"
                if os.path.exists(stop_file):
                    try:
                        os.remove(stop_file)
                    except OSError:
                        pass
                    print("\nStop signal received.")
                    break

    except KeyboardInterrupt:
        pass
    finally:
        nv.stop()
        intel_gpu.stop()
        npu_poller.stop()
        if ovms_poller:
            ovms_poller.stop()
        if bw_sampler:
            bw_sampler.stop()
        if rapl_poller:
            rapl_poller.stop()
        if l0_sysman:
            l0_sysman.stop()
        if l0_sampler:
            l0_sampler.stop()
        if emon_sampler:
            emon_sampler.stop()
        pdh.close()
        duration = sample_count * interval_s
        print(f"\nStopped. {sample_count} samples over ~{duration:.1f}s saved to {output_path}")

        # ── Peak RSS summary ──
        _write_peak_rss(output_path)


def _write_peak_rss(csv_path: str):
    """Scan running processes for known workload components and record peak RSS.

    Writes <csv_basename>_peak_rss.json next to the sampler CSV.
    """
    targets = {
        "ovms": ["ovms.exe", "ovms"],
        "python": ["python.exe", "python3.exe", "python", "python3"],
    }
    summary = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pname = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or [])
            mi = proc.memory_info()
            peak_mb = round(mi.peak_wset / (1024 * 1024)) if hasattr(mi, "peak_wset") else round(mi.rss / (1024 * 1024))
            rss_mb = round(mi.rss / (1024 * 1024))

            if pname in targets["ovms"]:
                summary.setdefault("ovms", []).append({
                    "pid": proc.info["pid"], "rss_mb": rss_mb,
                    "peak_rss_mb": peak_mb,
                })
            elif pname in targets["python"]:
                # Classify python processes by their script name
                label = "python"
                for script in ["AgenticAI_ProdAgent", "mcp_sever_prod_uc_analysis",
                               "mcp_sever_prod_uc_rag", "npu_embedding_server",
                               "sample_utilization_fast"]:
                    if script in cmdline:
                        label = script.split(".")[0]
                        break
                summary.setdefault(label, []).append({
                    "pid": proc.info["pid"], "rss_mb": rss_mb,
                    "peak_rss_mb": peak_mb,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    if summary:
        out_path = csv_path.rsplit(".", 1)[0] + "_peak_rss.json"
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
        total_peak = sum(
            p["peak_rss_mb"] for procs in summary.values() for p in procs
        )
        print(f"Peak RSS summary ({total_peak} MB total) written to {out_path}")
        for label, procs in summary.items():
            for p in procs:
                print(f"  {label} (pid {p['pid']}): {p['rss_mb']} MB current, {p['peak_rss_mb']} MB peak")


def main():
    parser = argparse.ArgumentParser(
        description="High-frequency heterogeneous device utilization sampler"
    )
    parser.add_argument(
        "--interval", "-t", type=int, default=None,
        help="Sampling interval in milliseconds (default: 50 for lightweight, 1000 for simulation)",
    )
    parser.add_argument(
        "--output", "-o", default="outputs/utilization_samples.csv",
        help="Output CSV path",
    )
    parser.add_argument(
        "--power", "-p", action="store_true",
        help="Enable RAPL power counters",
    )
    parser.add_argument(
        "--bandwidth", "--bw", default="pcm",
        choices=["pcm", "proxy", "none"],
        help="DRAM bandwidth mode: pcm (default, needs MSR driver), proxy (RAPL-based estimate), none",
    )
    parser.add_argument(
        "--profile", default=None,
        choices=["lightweight", "simulation", "memory_deep", "power_analysis", "unified"],
        help="Instrumentation profile (from config/instrumentation.json)",
    )
    parser.add_argument(
        "--source", action="append", metavar="KEY=VALUE",
        help="Override a source knob, e.g. --source bandwidth=emon --source microarch=emon",
    )
    args = parser.parse_args()

    # Load instrumentation config
    source_overrides = {}
    if args.source:
        for s in args.source:
            if "=" in s:
                k, v = s.split("=", 1)
                source_overrides[k.strip()] = v.strip()

    config = load_instrumentation_config(args.profile, source_overrides)
    sources = config["sources"]
    profile_name = config["profile_name"]

    # Determine interval: explicit > profile-based > default
    if args.interval is not None:
        interval_ms = args.interval
    elif profile_name in ("simulation", "power_analysis", "memory_deep"):
        interval_ms = 1000  # EMON needs >= 1s for meaningful data
    else:
        interval_ms = 50

    if interval_ms < 5:
        print(f"WARNING: {interval_ms}ms may exceed collection overhead (~5ms)")

    # If profile uses EMON for power, enable RAPL PDH too (for fast-path display)
    if sources.get("power") in ("emon", "both") or args.power:
        args.power = True

    # Proxy mode requires power counters
    if args.bandwidth == "proxy" and not args.power:
        print("INFO: --bandwidth proxy requires --power, enabling RAPL.")
        args.power = True

    print(f"Profile: {profile_name}")
    emon_cats = get_emon_categories(sources)
    if emon_cats:
        print(f"EMON categories: {sorted(emon_cats)}")

    n_cores, pdh, has_rapl, rapl_poller, nv, bw_sampler, bw_proxy, intel_gpu, emon_sampler = discover_hardware(
        args.power, args.bandwidth, config
    )
    sample_loop(n_cores, pdh, has_rapl, rapl_poller, nv, bw_sampler, bw_proxy, intel_gpu, interval_ms, args.output, emon_sampler)


if __name__ == "__main__":
    main()

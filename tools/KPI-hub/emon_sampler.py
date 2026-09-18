"""
EMON-based deep instrumentation sampler.

Provides microarchitecture-level metrics (TopDown, cache detail, DRAM page hit/miss,
C-state residency, per-CBO/IMC uncore counters) via Intel SEP/EMON.

This complements (does NOT replace) the existing PCM-based flow:
  - PCM: Fast, simple, system-level BW + IPC + L3 hit. Good for high-frequency sampling.
  - EMON: Rich, per-unit detail, 1795+ core events + uncore. Good for simulation data.

Both CANNOT simultaneously own the PMU counters for the same event domain.
The config system ensures mutual exclusion where needed.

Requires:
  - SEP driver (sepintdrv5) running
  - emon.exe accessible (auto-detected or configured)
  - Admin/elevated privileges

Usage as standalone:
    python emon_sampler.py --interval 1 --output outputs/emon_deep.csv --duration 30

Usage integrated (via sample_utilization_fast.py --profile simulation):
    Activated automatically when config sources specify "emon" for any category.
"""
import csv
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime


class EmonSampler:
    """Background EMON collection with structured output parsing.

    Runs emon.exe continuously and provides periodic snapshots of:
      - DRAM bandwidth (CAS_COUNT_RD/WR → GB/s)
      - DRAM page hit/miss ratios
      - LLC (CBO) cache lookup read/write counts
      - LLC eviction counts by MESI state
      - Per-core IPC (INST_RETIRED / CLK_UNHALTED)
      - Package/DRAM/GPU energy (raw counter deltas)
      - C-state residency fractions

    The sampler uses EMON's standard output format (one line per event per interval),
    with intervals separated by '==========' lines.
    """

    # DDR5 cache line size = 64 bytes, CAS operates on 64B
    CACHELINE_BYTES = 64

    def __init__(self, config: dict, enabled_categories: set):
        """
        Args:
            config: The "emon" section of instrumentation.json
            enabled_categories: Set of category names to collect
                e.g. {"bandwidth", "microarch", "cache_detail", "dram_detail", "power", "cstate"}
        """
        self.config = config
        self.enabled = enabled_categories
        self.interval_s = config.get("interval_s", 1.0)
        self.available = False
        self.emon_exe = self._find_emon(config)

        # Snapshot state (protected by lock)
        self._lock = threading.Lock()
        self._data = {}
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

        # TSC frequency for time normalization (populated on first parse)
        self._tsc_freq_mhz = 3302.4  # Default NVL; updated from emon -v output

        if self.emon_exe:
            self._check()

    @staticmethod
    def _find_emon(config: dict) -> str | None:
        """Resolve emon.exe path from config, env vars, or auto-detect."""
        # 1. EMON_EXE env var
        env_exe = os.environ.get("EMON_EXE")
        if env_exe and os.path.isfile(env_exe):
            return env_exe

        # 2. Config bin_dir
        bin_dir = config.get("bin_dir")
        if bin_dir:
            candidate = os.path.join(bin_dir, "emon.exe")
            if os.path.isfile(candidate):
                return candidate

        # 3. SEP_INSTALL_PATH env var (set by sep_vars.cmd)
        sep_path = os.environ.get("SEP_INSTALL_PATH")
        if sep_path:
            candidate = os.path.join(sep_path, "emon.exe")
            if os.path.isfile(candidate):
                return candidate

        # 4. Auto-detect paths from config
        for path in config.get("auto_detect_paths", []):
            candidate = os.path.join(path, "emon.exe")
            if os.path.isfile(candidate):
                return candidate

        # 5. System PATH
        found = shutil.which("emon.exe") or shutil.which("emon")
        if found:
            return found

        return None

    def _check(self):
        """Verify emon.exe runs and SEP driver is accessible."""
        try:
            r = subprocess.run(
                [self.emon_exe, "-v"],
                capture_output=True, text=True, timeout=10,
            )
            if "EMON Version" in r.stdout and "Novalake" in r.stdout:
                self.available = True
                # Extract TSC frequency
                for line in r.stdout.splitlines():
                    if "tsc_freq" in line.lower():
                        m = re.search(r"([\d.]+)\s*MHz", line)
                        if m:
                            self._tsc_freq_mhz = float(m.group(1))
            elif "EMON Version" in r.stdout:
                # Platform recognized but not NVL — still usable
                self.available = True
        except Exception:
            pass

    def _build_event_list(self) -> str:
        """Build comma-separated event list from enabled categories."""
        events = []
        event_config = self.config.get("events", {})
        for cat in self.enabled:
            cat_events = event_config.get(cat, [])
            events.extend(cat_events)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for e in events:
            if e not in seen:
                seen.add(e)
                unique.append(e)
        return ",".join(unique)

    def start(self):
        """Start background EMON collection."""
        if not self.available:
            return
        event_list = self._build_event_list()
        if not event_list:
            return
        self._thread = threading.Thread(target=self._loop, args=(event_list,), daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background EMON collection."""
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self, event_list: str):
        """Run emon continuously and parse output blocks."""
        cmd = [
            self.emon_exe,
            "-C", event_list,
            "-t", str(self.interval_s),
            "-l", "0",   # infinite loops
            "-u",        # unformatted decimal (no commas in numbers)
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            return

        current_block = {}  # event_name -> [per_cpu_values]
        for line in iter(self._proc.stdout.readline, ""):
            if self._stop.is_set():
                break
            line = line.rstrip()
            if not line:
                continue

            # Block separator
            if line.startswith("=========="):
                if current_block:
                    self._process_block(current_block)
                current_block = {}
                continue

            # Parse event line: "EVENT_NAME  TSC  cpu0  cpu1  ..."
            # In -u mode, values are space-separated unformatted integers
            # Some values may be N/A (e.g. TOPDOWN.SLOTS on E-cores)
            parts = line.split()
            if len(parts) >= 2:
                event_name = parts[0]
                try:
                    values = []
                    for v in parts[1:]:
                        if v == "N/A":
                            values.append(0)
                        else:
                            values.append(int(v))
                    current_block[event_name] = values
                except ValueError:
                    # Skip non-data lines (timing info, etc.)
                    pass

        # Process last block if any
        if current_block:
            self._process_block(current_block)

        if self._proc:
            self._proc.wait()

    def _process_block(self, block: dict):
        """Convert raw event counts into meaningful metrics."""
        data = {}

        # --- Bandwidth (IMC CAS counts → GB/s) + L3 miss for IA/non-IA split ---
        if "bandwidth" in self.enabled:
            cas_rd = self._get_total(block, "UNC_M_CAS_COUNT_RD")
            cas_wr = self._get_total(block, "UNC_M_CAS_COUNT_WR")
            if cas_rd is not None:
                # CAS count × 64 bytes / interval / 1e9 = GB/s
                rd_gbs = (cas_rd * self.CACHELINE_BYTES) / (self.interval_s * 1e9)
                wr_gbs = ((cas_wr or 0) * self.CACHELINE_BYTES) / (self.interval_s * 1e9)
                data["dram_read_gbs"] = round(rd_gbs, 3)
                data["dram_write_gbs"] = round(wr_gbs, 3)
                data["dram_total_gbs"] = round(rd_gbs + wr_gbs, 3)
                data["bw_source"] = "emon"
            # L3 miss → IA-core DRAM BW estimate (each L3 miss = 64B from DRAM)
            llc_miss = self._get_total(block, "LONGEST_LAT_CACHE.MISS")
            if llc_miss is not None:
                data["l3miss_millions"] = round(llc_miss / 1e6, 3)
                ia_bw = (llc_miss * self.CACHELINE_BYTES) / (self.interval_s * 1e9)
                data["ia_dram_bw_gbs"] = round(ia_bw, 3)
                total_bw = data.get("dram_total_gbs", 0)
                data["nonia_dram_bw_gbs"] = round(max(0, total_bw - ia_bw), 3)

        # --- DRAM Page Hit/Miss + Latency ---
        if "dram_detail" in self.enabled:
            page_hit_rd = self._get_total(block, "UNC_M_DRAM_PAGE_HIT_RD")
            page_miss_rd = self._get_total(block, "UNC_M_DRAM_PAGE_MISS_RD")
            page_hit_wr = self._get_total(block, "UNC_M_DRAM_PAGE_HIT_WR")
            page_miss_wr = self._get_total(block, "UNC_M_DRAM_PAGE_MISS_WR")
            if page_hit_rd is not None and page_miss_rd is not None:
                total_rd = page_hit_rd + page_miss_rd
                data["dram_page_hit_rate_rd"] = round(page_hit_rd / total_rd, 3) if total_rd > 0 else 0.0
            if page_hit_wr is not None and page_miss_wr is not None:
                total_wr = page_hit_wr + page_miss_wr
                data["dram_page_hit_rate_wr"] = round(page_hit_wr / total_wr, 3) if total_wr > 0 else 0.0

            # --- DRAM Read Latency (IMC queue occupancy / read requests) ---
            rd_occ_ch0 = self._get_total(block, "UNC_M_RD_OCCUPANCY_CH0")
            rd_occ_ch1 = self._get_total(block, "UNC_M_RD_OCCUPANCY_CH1")
            rd_req = self._get_total(block, "UNC_M_VC0_REQUESTS_RD")
            imc_clks = self._get_total(block, "UNC_M_CLOCKTICKS")
            if rd_occ_ch0 is not None and rd_req and rd_req > 0:
                total_occ = rd_occ_ch0 + (rd_occ_ch1 or 0)
                avg_lat_imc_clks = total_occ / rd_req
                data["dram_rd_latency_imc_clks"] = round(avg_lat_imc_clks, 1)
                # Convert to nanoseconds using IMC clock frequency
                if imc_clks and imc_clks > 0:
                    imc_freq_ghz = imc_clks / (self.interval_s * 1e9)
                    if imc_freq_ghz > 0:
                        data["dram_rd_latency_ns"] = round(avg_lat_imc_clks / imc_freq_ghz, 1)
                        data["imc_freq_ghz"] = round(imc_freq_ghz, 3)

        # --- Microarchitecture (IPC, CPI) ---
        if "microarch" in self.enabled:
            inst = self._get_total(block, "INST_RETIRED.ANY")
            clk = self._get_total(block, "CPU_CLK_UNHALTED.THREAD")
            ref_tsc = self._get_total(block, "CPU_CLK_UNHALTED.REF_TSC")
            if inst and clk:
                data["emon_ipc"] = round(inst / clk, 3) if clk > 0 else 0.0
                data["emon_cpi"] = round(clk / inst, 3) if inst > 0 else 0.0
            if clk and ref_tsc:
                # Active frequency ratio = CLK_UNHALTED.THREAD / REF_TSC
                data["emon_freq_ratio"] = round(clk / ref_tsc, 3) if ref_tsc > 0 else 0.0
            # Per-core IPC (P-cores vs E-cores) from per-cpu values
            inst_per_cpu = block.get("INST_RETIRED.ANY")
            clk_per_cpu = block.get("CPU_CLK_UNHALTED.THREAD")
            if inst_per_cpu and clk_per_cpu and len(inst_per_cpu) > 9:
                # Skip TSC at index 0; per-CPU starts at index 1
                # P-cores: CPU0-3, CPU12-15 (indices 1-4 and 13-16 in values)
                # E-cores: CPU4-11, CPU16-23 (indices 5-12 and 17-24)
                p_inst = sum(inst_per_cpu[1:5]) + sum(inst_per_cpu[13:17])
                p_clk = sum(clk_per_cpu[1:5]) + sum(clk_per_cpu[13:17])
                e_inst = sum(inst_per_cpu[5:13]) + sum(inst_per_cpu[17:25])
                e_clk = sum(clk_per_cpu[5:13]) + sum(clk_per_cpu[17:25])
                if p_clk > 0:
                    data["emon_ipc_pcore"] = round(p_inst / p_clk, 3)
                if e_clk > 0:
                    data["emon_ipc_ecore"] = round(e_inst / e_clk, 3)

            # TopDown analysis (from fixed counters, available for free)
            # P-core: TOPDOWN.SLOTS = total pipeline slots
            # E-core: TOPDOWN_BAD_SPECULATION.ALL, TOPDOWN_FE_BOUND.ALL, TOPDOWN_RETIRING.ALL
            # (values are 0/N/A for the wrong core type, so we sum non-zero)
            td_slots = self._get_total(block, "TOPDOWN.SLOTS")
            td_bad_spec = self._get_total(block, "TOPDOWN_BAD_SPECULATION.ALL")
            td_fe_bound = self._get_total(block, "TOPDOWN_FE_BOUND.ALL")
            td_retiring = self._get_total(block, "TOPDOWN_RETIRING.ALL")
            # E-core TopDown is in fixed counter "perf_metrics" units (0-255 per sample)
            # Normalize: percentage of total slots
            if td_fe_bound and td_retiring:
                # E-core reports as scaled counts; total = bad_spec + fe_bound + be_bound + retiring
                td_total = (td_bad_spec or 0) + (td_fe_bound or 0) + (td_retiring or 0)
                if td_total > 0:
                    # be_bound = implicit remainder (not directly reported as separate fixed ctr)
                    data["topdown_retiring_pct"] = round((td_retiring / td_total) * 100, 1)
                    data["topdown_bad_spec_pct"] = round(((td_bad_spec or 0) / td_total) * 100, 1)
                    data["topdown_fe_bound_pct"] = round((td_fe_bound / td_total) * 100, 1)

        # --- Cache Detail (core-level L2/LLC hit/miss) ---
        # Note: CBO uncore counters are blocked by Hyper-V/VBS on this platform.
        # Using core PMU events instead: LONGEST_LAT_CACHE.MISS = LLC miss (works both P+E),
        # L2_RQSTS.DEMAND_DATA_RD_HIT/MISS = L2 hit/miss (P-cores, N/A on E-cores).
        if "cache_detail" in self.enabled:
            llc_miss = self._get_total(block, "LONGEST_LAT_CACHE.MISS")
            l2_hit = self._get_total(block, "L2_RQSTS.DEMAND_DATA_RD_HIT")
            l2_miss = self._get_total(block, "L2_RQSTS.DEMAND_DATA_RD_MISS")
            if llc_miss is not None:
                data["llc_miss_m"] = round(llc_miss / 1e6, 3)
                # Estimate LLC miss BW: each miss = 64B from DRAM
                data["llc_miss_bw_gbs"] = round((llc_miss * self.CACHELINE_BYTES) / (self.interval_s * 1e9), 3)
            if l2_hit is not None and l2_miss is not None:
                total_l2 = l2_hit + l2_miss
                data["l2_hit_rate"] = round(l2_hit / total_l2, 3) if total_l2 > 0 else 0.0

        # --- Power (energy counter deltas → Watts) ---
        if "power" in self.enabled:
            # EMON power events are raw energy counter deltas
            # UNC_PKG_ENERGY_STATUS unit = ~61 µJ (MSR energy unit for client)
            pkg_energy = self._get_total(block, "UNC_PKG_ENERGY_STATUS")
            dram_energy = self._get_total(block, "UNC_DRAM_ENERGY_STATUS")
            pp1_energy = self._get_total(block, "UNC_PP1_ENERGY_STATUS")
            energy_unit = 61e-6  # ~61 µJ per tick (platform-specific, NVL client)
            if pkg_energy is not None:
                data["emon_pkg_power_w"] = round((pkg_energy * energy_unit) / self.interval_s, 2)
            if dram_energy is not None:
                data["emon_dram_power_w"] = round((dram_energy * energy_unit) / self.interval_s, 2)
            if pp1_energy is not None:
                data["emon_gpu_power_w"] = round((pp1_energy * energy_unit) / self.interval_s, 2)

        # --- C-state Residency ---
        if "cstate" in self.enabled:
            c6 = self._get_total(block, "FREERUN_CORE_C6_RESIDENCY")
            pkg_c2 = self._get_total(block, "FREERUN_PKG_C2_RESIDENCY")
            pkg_c10 = self._get_total(block, "FREERUN_PKG_C10_RESIDENCY")
            # Freerun counters are TSC-normalized; value = cycles in state
            tsc_interval = self._tsc_freq_mhz * 1e6 * self.interval_s
            if c6 is not None and tsc_interval > 0:
                data["core_c6_residency_pct"] = round((c6 / tsc_interval) * 100, 1)
            if pkg_c2 is not None and tsc_interval > 0:
                data["pkg_c2_residency_pct"] = round((pkg_c2 / tsc_interval) * 100, 1)
            if pkg_c10 is not None and tsc_interval > 0:
                data["pkg_c10_residency_pct"] = round((pkg_c10 / tsc_interval) * 100, 1)

        # Store snapshot
        with self._lock:
            self._data = data

    @staticmethod
    def _get_total(block: dict, event_name: str) -> int | None:
        """Get the system total for an event from parsed block.

        EMON -u format: first value is TSC timestamp, remaining are per-CPU/per-unit.
        System total = sum of all per-CPU/per-unit values (skip TSC at index 0).
        """
        values = block.get(event_name)
        if values and len(values) >= 2:
            return sum(values[1:])  # Skip TSC timestamp at index 0
        return None

    def snapshot(self) -> dict:
        """Return latest computed metrics."""
        with self._lock:
            return dict(self._data)

    def get_extra_columns(self) -> list[str]:
        """Return CSV column names this sampler will produce."""
        cols = []
        if "bandwidth" in self.enabled:
            cols += ["dram_read_gbs", "dram_write_gbs", "dram_total_gbs",
                     "l3miss_millions", "ia_dram_bw_gbs", "nonia_dram_bw_gbs", "bw_source"]
        if "dram_detail" in self.enabled:
            cols += ["dram_page_hit_rate_rd", "dram_page_hit_rate_wr",
                     "dram_rd_latency_imc_clks", "dram_rd_latency_ns", "imc_freq_ghz"]
        if "microarch" in self.enabled:
            cols += ["emon_ipc", "emon_cpi", "emon_freq_ratio", "emon_ipc_pcore", "emon_ipc_ecore",
                     "topdown_retiring_pct", "topdown_bad_spec_pct", "topdown_fe_bound_pct"]
        if "cache_detail" in self.enabled:
            cols += ["llc_miss_m", "llc_miss_bw_gbs", "l2_hit_rate"]
        if "power" in self.enabled:
            cols += ["emon_pkg_power_w", "emon_dram_power_w", "emon_gpu_power_w"]
        if "cstate" in self.enabled:
            cols += ["core_c6_residency_pct", "pkg_c2_residency_pct", "pkg_c10_residency_pct"]
        return cols


def _test_standalone():
    """Quick standalone test of EMON sampler."""
    import json

    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "instrumentation.json")
    if not os.path.isfile(config_path):
        config_path = os.path.join(os.path.dirname(__file__), "config", "instrumentation.json")

    # Try workspace root config
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "instrumentation.json"),
        r"c:\LiteAgent-Clash\config\instrumentation.json",
    ]:
        if os.path.isfile(candidate):
            config_path = candidate
            break

    with open(config_path) as f:
        full_config = json.load(f)

    emon_config = full_config.get("emon", {})
    categories = {"bandwidth", "microarch", "cache_detail", "dram_detail", "power", "cstate"}

    sampler = EmonSampler(emon_config, categories)
    print(f"EMON exe: {sampler.emon_exe}")
    print(f"Available: {sampler.available}")
    print(f"Enabled categories: {categories}")
    print(f"Extra CSV columns: {sampler.get_extra_columns()}")

    if not sampler.available:
        print("EMON not available. Check SEP driver and emon.exe path.")
        return

    print(f"\nStarting collection (interval={sampler.interval_s}s)...")
    sampler.start()

    try:
        for i in range(5):
            time.sleep(sampler.interval_s + 0.2)
            snap = sampler.snapshot()
            if snap:
                print(f"\n[Sample {i+1}]")
                for k, v in sorted(snap.items()):
                    print(f"  {k}: {v}")
            else:
                print(f"[Sample {i+1}] No data yet...")
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        print("\nDone.")


if __name__ == "__main__":
    _test_standalone()

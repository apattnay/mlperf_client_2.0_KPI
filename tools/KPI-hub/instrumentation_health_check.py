#!/usr/bin/env python3
"""KPI Hub Instrumentation Health Check.

Verifies that all data collection pipelines are functional:
  - CPU counters (psutil)
  - PDH GPU Engine counters (Windows)
  - Level Zero Sysman (engine busy%, freq, memory)
  - Level Zero TBS/OA (EU activity, memory BW, GPU_BUSY)
  - NPU availability (OpenVINO)
  - RAPL energy (if available)

Run this BEFORE starting a benchmark to catch issues early.

Usage:
    python instrumentation_health_check.py
    python instrumentation_health_check.py --fix   # attempt auto-fixes
"""
import os
import sys
import time
import ctypes
import struct
import subprocess

# Ensure L0 env vars are set before any imports that might load ze_loader
os.environ.setdefault("ZET_ENABLE_METRICS", "1")
os.environ.setdefault("ZES_ENABLE_SYSMAN", "1")

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}


def check(label, ok, detail="", warn_only=False):
    if ok:
        print(f"  {PASS} {label}")
        results["pass"] += 1
    elif warn_only:
        print(f"  {WARN} {label} — {detail}")
        results["warn"] += 1
    else:
        print(f"  {FAIL} {label} — {detail}")
        results["fail"] += 1
    return ok


def check_cpu():
    print("\n── CPU Counters (psutil) ──")
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.2, percpu=True)
        check("psutil available", True)
        check("per-core data", len(cpu) > 0, f"got {len(cpu)} cores")
        freq = psutil.cpu_freq(percpu=True)
        check("frequency data", freq is not None and len(freq) > 0,
              "cpu_freq() returned None")
    except ImportError:
        check("psutil available", False, "pip install psutil")


def check_pdh():
    print("\n── PDH GPU Engine Counters (Windows) ──")
    if sys.platform != "win32":
        check("Windows platform", False, "PDH only on Windows", warn_only=True)
        return
    try:
        import win32pdh
        check("pywin32 (win32pdh)", True)
        # Try to read GPU engine counter
        query = win32pdh.OpenQuery()
        try:
            path = win32pdh.MakeCounterPath((None, "GPU Engine", "*",
                                             None, -1, "Utilization Percentage"))
            ctr = win32pdh.AddCounter(query, path)
            win32pdh.CollectQueryData(query)
            time.sleep(0.1)
            win32pdh.CollectQueryData(query)
            _, val = win32pdh.GetFormattedCounterValue(ctr, win32pdh.PDH_FMT_DOUBLE)
            check("GPU Engine counter readable", True)
            check("GPU Engine value > 0", val > 0,
                  "Level Zero workloads bypass PDH (expected if using L0 directly)",
                  warn_only=True)
        except Exception as e:
            check("GPU Engine counter", False, str(e)[:80], warn_only=True)
        finally:
            win32pdh.CloseQuery(query)
    except ImportError:
        check("pywin32 (win32pdh)", False, "pip install pywin32")


def check_l0_sysman():
    print("\n── Level Zero Sysman (engine busy%, freq) ──")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from l0_sysman_sampler import L0SysmanSampler
        s = L0SysmanSampler()
        check("L0SysmanSampler import", True)
        check("Sysman available", s.available, s.error or "unknown error")
        if s.available:
            s.start()
            time.sleep(0.5)
            snap = s.snapshot()
            s.stop()
            busy = snap.get("zes_gpu_busy", 0)
            freq = snap.get("zes_freq_mhz", 0)
            check("Sysman returns data", busy >= 0 and freq >= 0,
                  f"busy={busy}, freq={freq}")
    except ImportError as e:
        check("L0SysmanSampler import", False, str(e))


def check_l0_tbs():
    print("\n── Level Zero TBS/OA (EU Activity, Memory BW) ──")

    # Pre-check: env vars
    check("ZET_ENABLE_METRICS=1", os.environ.get("ZET_ENABLE_METRICS") == "1",
          f"got '{os.environ.get('ZET_ENABLE_METRICS')}'")

    try:
        ze = ctypes.CDLL("ze_loader.dll")
        check("ze_loader.dll loaded", True)
    except OSError as e:
        check("ze_loader.dll loaded", False, str(e))
        return

    H = ctypes.c_void_p; U32 = ctypes.c_uint32

    r = ze.zeInit(U32(0))
    check("zeInit", r == 0, f"0x{r:08X}")
    if r != 0:
        return

    # Find GPU
    dc = U32(0); ze.zeDriverGet(ctypes.byref(dc), None)
    drvs = (H * dc.value)(); ze.zeDriverGet(ctypes.byref(dc), drvs)
    gpu_drv = None; gpu_dev = None
    for d in range(dc.value):
        devc = U32(0); ze.zeDeviceGet(H(drvs[d]), ctypes.byref(devc), None)
        devs = (H * devc.value)(); ze.zeDeviceGet(H(drvs[d]), ctypes.byref(devc), devs)
        for i in range(devc.value):
            dp = (ctypes.c_byte * 512)()
            struct.pack_into('I', dp, 0, 4)
            ze.zeDeviceGetProperties(H(devs[i]), ctypes.byref(dp))
            if struct.unpack_from('I', bytes(dp), 16)[0] == 1:
                gpu_drv = drvs[d]; gpu_dev = devs[i]; break
        if gpu_dev: break

    check("GPU device found", gpu_dev is not None, "No GPU in L0 device list")
    if not gpu_dev:
        return

    # Find ComputeBasic TBS
    mc = U32(0); ze.zetMetricGroupGet(H(gpu_dev), ctypes.byref(mc), None)
    check("Metric groups available", mc.value > 0,
          "No metric groups (ZET_ENABLE_METRICS not set before zeInit?)")
    if mc.value == 0:
        return

    groups = (H * mc.value)(); ze.zetMetricGroupGet(H(gpu_dev), ctypes.byref(mc), groups)
    target = None
    for g in range(mc.value):
        gp = (ctypes.c_byte * 4096)()
        struct.pack_into("I", gp, 0, 0x1000000B)
        struct.pack_into("Q", gp, 8, 0)
        ze.zetMetricGroupGetProperties(H(groups[g]), ctypes.byref(gp))
        gr = bytes(gp)
        gname = gr[16:272].split(b"\x00")[0].decode()
        sampling = struct.unpack_from("I", gr, 528)[0]
        if "ComputeBasic" in gname and (sampling & 2):
            target = groups[g]; break

    check("ComputeBasic TBS group", target is not None, "Group not found")
    if not target:
        return

    # Try to open streamer
    ctx_desc = (ctypes.c_byte * 32)()
    struct.pack_into("I", ctx_desc, 0, 0xE)
    struct.pack_into("Q", ctx_desc, 8, 0)
    ctx = H(0)
    ze.zeContextCreate(H(gpu_drv), ctypes.byref(ctx_desc), ctypes.byref(ctx))

    ga = (H * 1)(target)
    r = ze.zetContextActivateMetricGroups(ctx, H(gpu_dev), U32(1), ga)
    check("zetContextActivateMetricGroups", r == 0, f"0x{r:08X}")

    st_desc = (ctypes.c_byte * 32)()
    struct.pack_into("I", st_desc, 0, 0x1000000E)
    struct.pack_into("Q", st_desc, 8, 0)
    struct.pack_into("I", st_desc, 16, 0)
    struct.pack_into("I", st_desc, 20, 100_000_000)
    streamer = H(0)
    r = ze.zetMetricStreamerOpen(ctx, H(gpu_dev), H(target),
                                 ctypes.byref(st_desc), None, ctypes.byref(streamer))

    if r == 0:
        check("zetMetricStreamerOpen (OA access)", True)
        # Verify data flows
        time.sleep(0.5)
        raw_size = ctypes.c_size_t(0)
        ze.zetMetricStreamerReadData(streamer, U32(0xFFFFFFFF),
                                     ctypes.byref(raw_size), None)
        check("OA data flowing", raw_size.value > 0,
              f"0 bytes after 500ms — driver may be stale (try reboot)")
        ze.zetMetricStreamerClose(streamer)
    else:
        detail = f"0x{r:08X}"
        if r == 0x7FFFFFFE:
            detail += (" — ZE_RESULT_ERROR_UNKNOWN. Common causes:\n"
                       "         1) GPU driver in stale state → reboot\n"
                       "         2) Another process holds OA (Intel Graphics Command Center)\n"
                       "            → Stop-Service IntelGraphicsSoftwareService\n"
                       "         3) SR-IOV PF GPU — OA not supported in virtualized mode\n"
                       "         4) ZET_ENABLE_METRICS=1 was not set before first zeInit()")
        check("zetMetricStreamerOpen (OA access)", False, detail)

    ze.zetContextActivateMetricGroups(ctx, H(gpu_dev), U32(0), None)
    ze.zeContextDestroy(ctx)


def check_npu():
    print("\n── NPU (OpenVINO) ──")
    try:
        import openvino as ov
        check("OpenVINO import", True)
        core = ov.Core()
        devices = core.available_devices
        has_npu = "NPU" in devices
        check("NPU device available", has_npu,
              f"devices={devices}" if not has_npu else "")
    except ImportError:
        check("OpenVINO import", False, "pip install openvino")


def check_contention():
    print("\n── Contention Checks ──")
    if sys.platform == "win32":
        # Check for services that hold OA
        try:
            r = subprocess.run(
                ["sc", "query", "IntelGraphicsSoftwareService"],
                capture_output=True, text=True)
            running = "RUNNING" in r.stdout
            check("Intel Graphics Software Service stopped", not running,
                  "Service holds OA stream — run: Stop-Service IntelGraphicsSoftwareService",
                  warn_only=True)
        except Exception:
            pass

    # Check if L0 env vars are set in calling environment
    check("ZET_ENABLE_METRICS in env", os.environ.get("ZET_ENABLE_METRICS") == "1",
          "Must be set BEFORE ze_loader.dll is loaded")
    check("ZES_ENABLE_SYSMAN in env", os.environ.get("ZES_ENABLE_SYSMAN") == "1",
          "Must be set BEFORE ze_loader.dll is loaded")


def check_init_order():
    """Verify the critical init order bug doesn't regress."""
    print("\n── Init Order (ZET_ENABLE_METRICS before zeInit) ──")
    # Spawn a subprocess that mimics the sampler's init sequence
    script = """
import os, sys, ctypes, struct
os.environ['ZET_ENABLE_METRICS'] = '1'
os.environ['ZES_ENABLE_SYSMAN'] = '1'
H = ctypes.c_void_p; U32 = ctypes.c_uint32
ze = ctypes.CDLL("ze_loader.dll")
ze.zeInit(U32(0))
dc = U32(0); ze.zeDriverGet(ctypes.byref(dc), None)
drvs = (H * dc.value)(); ze.zeDriverGet(ctypes.byref(dc), drvs)
for d in range(dc.value):
    devc = U32(0); ze.zeDeviceGet(H(drvs[d]), ctypes.byref(devc), None)
    devs = (H * devc.value)(); ze.zeDeviceGet(H(drvs[d]), ctypes.byref(devc), devs)
    for i in range(devc.value):
        dp = (ctypes.c_byte * 512)()
        struct.pack_into('I', dp, 0, 4)
        ze.zeDeviceGetProperties(H(devs[i]), ctypes.byref(dp))
        if struct.unpack_from('I', bytes(dp), 16)[0] == 1:
            mc = U32(0)
            ze.zetMetricGroupGet(H(devs[i]), ctypes.byref(mc), None)
            print(mc.value)
            sys.exit(0)
print(0)
"""
    try:
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True, timeout=10)
        n_groups = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
        check("Metric groups in fresh subprocess", n_groups > 0,
              f"got {n_groups} — ZET_ENABLE_METRICS not taking effect before zeInit")
    except Exception as e:
        check("Subprocess init order test", False, str(e)[:80])


def main():
    print("=" * 60)
    print(" KPI Hub — Instrumentation Health Check")
    print("=" * 60)

    check_cpu()
    check_pdh()
    check_l0_sysman()
    check_l0_tbs()
    check_npu()
    check_contention()
    check_init_order()

    print("\n" + "=" * 60)
    total = results["pass"] + results["fail"] + results["warn"]
    print(f" Results: {results['pass']}/{total} passed, "
          f"{results['fail']} failed, {results['warn']} warnings")
    if results["fail"] == 0:
        print(" Status: ALL INSTRUMENTATION HEALTHY")
    else:
        print(" Status: ISSUES DETECTED — see failures above")
    print("=" * 60)

    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

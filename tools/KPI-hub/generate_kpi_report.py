#!/usr/bin/env python3
"""Generate a self-contained HTML KPI report from workflow_kpi.json + rag_kpi.json.

Usage:
    python generate_kpi_report.py [--kpi-dir outputs/kpi_ovms_hetero] [--output outputs/kpi_report.html]

Reads:
  - <kpi-dir>/workflow_kpi.json   (or outputs/workflow_kpi.json as fallback)
  - <kpi-dir>/rag_kpi.json        (or outputs/rag_kpi.json as fallback)
  - <kpi-dir>/system_state.txt    (optional, for device assignment info)

Produces:
  - A single self-contained HTML file with:
    * Gantt-style timeline of agent phases (with absolute timestamps)
    * Per-agent KPI metrics table
    * RAG / Embedding KPI section
    * Device assignment summary
"""

import argparse
import ctypes
import json
import math
import os
import shutil
import struct
import sys
import html
import platform
import subprocess
from datetime import datetime, timedelta


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_text(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# --- Color palette for agent phases ---
PHASE_COLORS = {
    "task_agent":              "#4C72B0",
    "analysis_agent":          "#55A868",
    "summary_agent_1":         "#C44E52",
    "summary_agent_2":         "#8172B2",
    "executive_summary_agent": "#CCB974",
    "rag_setup":               "#607D8B",
}
DEFAULT_COLOR = "#64B5F6"

# Hardware responsible for each phase (used in timeline annotations)
PHASE_HW = {
    "task_agent":              "LLM",
    "analysis_agent":          "LLM + CPU",
    "summary_agent_1":         "LLM + EMBED_DEVICE (RAG)",
    "summary_agent_2":         "LLM + EMBED_DEVICE (RAG)",
    "executive_summary_agent": "LLM + CPU",
    "rag_setup":               "EMBED_DEVICE → CPU (ChromaDB)",
}


def _embed_device_label(rkpi: dict | None) -> str:
    """Derive the embedding device label from rag_kpi data."""
    if not rkpi:
        return "NPU"  # default for NPU embedding server
    api_base = rkpi.get("embedding_api_base", "")
    model_name = rkpi.get("embedding_model", "")
    if ":11435" in api_base or ":11434" in api_base:
        # Ollama CPU embedding
        return "CPU"
    if ":8001" in api_base:
        # NPU embedding server
        return "NPU"
    if "int8" in model_name.lower() or "openvino" in model_name.lower():
        return "NPU"
    return "CPU"


def _color_for(name):
    return PHASE_COLORS.get(name, DEFAULT_COLOR)


def _fmt_time(iso):
    """HH:MM:SS from ISO string."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso


def _collect_sut_info():
    """Collect System-Under-Test hardware and software info (portable, no hardcoded paths)."""
    hw = {}
    sw = {}

    # ── Hardware ──
    hw["hostname"] = platform.node()
    hw["os"] = f"{platform.system()} {platform.version()}"
    hw["arch"] = platform.machine()

    # CPU (basic)
    try:
        r = subprocess.run(
            ["wmic", "cpu", "get", "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                if k == "Name":
                    hw["cpu_model"] = v.strip()
                elif k == "NumberOfCores":
                    hw["cpu_cores"] = v.strip()
                elif k == "NumberOfLogicalProcessors":
                    hw["cpu_threads"] = v.strip()
                elif k == "MaxClockSpeed":
                    hw["cpu_max_mhz"] = v.strip()
    except Exception:
        pass

    # CPU P-core / E-core detection via registry EfficiencyClass
    try:
        import winreg
        total_cores = int(hw.get("cpu_threads", hw.get("cpu_cores", "0")))
        p_cores = e_cores = 0
        for i in range(total_cores):
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"HARDWARE\DESCRIPTION\System\CentralProcessor\{i}",
            )
            try:
                eff = winreg.QueryValueEx(key, "EfficiencyClass")[0]
                if eff == 0:
                    p_cores += 1
                else:
                    e_cores += 1
            except FileNotFoundError:
                pass  # EfficiencyClass not present (pre-production silicon)
            finally:
                key.Close()
        if p_cores or e_cores:
            hw["cpu_p_cores"] = str(p_cores)
            hw["cpu_e_cores"] = str(e_cores)
    except Exception:
        pass

    # RAM
    try:
        r = subprocess.run(
            ["wmic", "computersystem", "get", "TotalPhysicalMemory", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "TotalPhysicalMemory=" in line:
                mem_bytes = int(line.split("=")[1].strip())
                hw["ram_gb"] = f"{mem_bytes / (1024**3):.1f}"
    except Exception:
        pass

    # System/board
    try:
        r = subprocess.run(
            ["wmic", "computersystem", "get", "Manufacturer,Model", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                if k == "Model":
                    hw["system_model"] = v.strip()
                elif k == "Manufacturer":
                    hw["system_oem"] = v.strip()
    except Exception:
        pass

    # NVIDIA GPU (basic + extended)
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,memory.total,compute_cap,clocks.max.sm,clocks.max.mem,"
             "pcie.link.gen.max,pcie.link.width.max,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        if len(parts) >= 3:
            hw["nvidia_gpu"] = parts[0]
            sw["nvidia_driver"] = parts[1]
            hw["nvidia_vram"] = f"{parts[2]} MiB"
        if len(parts) >= 9:
            hw["nvidia_compute_cap"] = parts[3]
            hw["nvidia_sm_clock"] = f"{parts[4]} MHz"
            hw["nvidia_mem_clock"] = f"{parts[5]} MHz"
            hw["nvidia_pcie"] = f"Gen{parts[6]} x{parts[7]}"
            hw["nvidia_power_limit"] = f"{float(parts[8]):.0f} W"
    except Exception:
        pass

    # NVIDIA CUDA version
    try:
        r = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.split("\n"):
            if "CUDA Version" in line:
                import re
                m = re.search(r"CUDA Version:\s*([\d.]+)", line)
                if m:
                    hw["nvidia_cuda"] = m.group(1)
                break
    except Exception:
        pass

    # NVIDIA architecture from nvidia-smi -q
    try:
        r = subprocess.run(
            ["nvidia-smi", "-q"], capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.split("\n"):
            if "Product Architecture" in line and ":" in line:
                hw["nvidia_arch"] = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass

    # Intel iGPU (WMI) — wmic /format:list puts blank lines between each field,
    # so we collect all key-value pairs and group by repeated keys.
    try:
        r = subprocess.run(
            ["wmic", "path", "win32_videocontroller", "get",
             "Name,DriverVersion,AdapterRAM", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        records = []
        cur = {}
        for line in r.stdout.split("\n"):
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.lower()
                if k in cur:
                    records.append(cur)
                    cur = {}
                cur[k] = v.strip()
        if cur:
            records.append(cur)
        for rec in records:
            if "Intel" in rec.get("name", ""):
                hw["igpu_name"] = rec.get("name", "")
                sw["igpu_driver"] = rec.get("driverversion", "")
                try:
                    ram = int(rec.get("adapterram", "0"))
                    hw["igpu_dedicated_mb"] = str(ram // (1024 * 1024))
                except (ValueError, TypeError):
                    pass
                break
    except Exception:
        pass

    # Intel iGPU — Level Zero device properties (EU topology)
    try:
        ze = ctypes.CDLL("ze_loader.dll")
        U32 = ctypes.c_uint32
        H = ctypes.c_void_p

        if ze.zeInit(U32(1)) == 0:
            dc = U32(0)
            ze.zeDriverGet(ctypes.byref(dc), None)
            if dc.value > 0:
                drvs = (H * dc.value)()
                ze.zeDriverGet(ctypes.byref(dc), drvs)
                devc = U32(0)
                ze.zeDeviceGet(H(drvs[0]), ctypes.byref(devc), None)
                if devc.value > 0:
                    devs = (H * devc.value)()
                    ze.zeDeviceGet(H(drvs[0]), ctypes.byref(devc), devs)
                    buf = (ctypes.c_uint8 * 512)()
                    struct.pack_into("<I", buf, 0, 0x3)  # ZE_STRUCTURE_TYPE_DEVICE_PROPERTIES
                    if ze.zeDeviceGetProperties(H(devs[0]), ctypes.byref(buf)) == 0:
                        # ze_device_properties_t layout (x64, from ze_api.h):
                        # stype(4)+pad(4)+pNext(8)+type(4)+vendorId(4)+deviceId(4)+flags(4)
                        # +subdeviceId(4)+coreClockRate(4)+maxMemAllocSize(8)
                        # +maxHwCtx(4)+maxCmdQPri(4)+numThreadsPerEU(4)+physicalEUSimdWidth(4)
                        # +numEUsPerSubslice(4)+numSubslicesPerSlice(4)+numSlices(4)
                        vendor_id = struct.unpack_from("<I", buf, 20)[0]
                        device_id = struct.unpack_from("<I", buf, 24)[0]
                        core_clock = struct.unpack_from("<I", buf, 36)[0]
                        max_mem = struct.unpack_from("<Q", buf, 40)[0]
                        threads_per_eu = struct.unpack_from("<I", buf, 56)[0]
                        simd_width = struct.unpack_from("<I", buf, 60)[0]
                        eus_per_ss = struct.unpack_from("<I", buf, 64)[0]
                        ss_per_slice = struct.unpack_from("<I", buf, 68)[0]
                        num_slices = struct.unpack_from("<I", buf, 72)[0]
                        total_eus = num_slices * ss_per_slice * eus_per_ss
                        if vendor_id == 0x8086 and total_eus > 0:
                            hw["igpu_device_id"] = f"0x{device_id:04X}"
                            hw["igpu_max_clock"] = f"{core_clock} MHz"
                            hw["igpu_total_eus"] = str(total_eus)
                            hw["igpu_xe_cores"] = str(ss_per_slice * num_slices)
                            hw["igpu_slices"] = str(num_slices)
                            hw["igpu_max_mem_gb"] = f"{max_mem / (1024**3):.1f}"
                            hw["igpu_threads_per_eu"] = str(threads_per_eu)
                            hw["igpu_simd_width"] = str(simd_width)
    except (OSError, Exception):
        pass  # Level Zero not available — skip gracefully

    # NPU (PnP entity + driver version)
    try:
        r = subprocess.run(
            ["wmic", "path", "win32_pnpentity", "where", "Name like '%NPU%'",
             "get", "Name", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "Name=" in line:
                hw["npu_device"] = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass

    # NPU driver version
    try:
        r = subprocess.run(
            ["wmic", "path", "win32_pnpsigneddriver", "where", "DeviceName like '%NPU%'",
             "get", "DriverVersion", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "DriverVersion=" in line:
                sw["npu_driver"] = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass

    # ── Software / Middleware ──
    sw["python"] = platform.python_version()

    # OVMS — find executable portably (no hardcoded path)
    ovms_exe = shutil.which("ovms")
    if not ovms_exe:
        # Search common install locations
        for candidate in [
            os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "ovms", "ovms.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "ovms", "ovms.exe"),
            "c:\\ovms\\ovms.exe",
        ]:
            if candidate and os.path.isfile(candidate):
                ovms_exe = candidate
                break
    if ovms_exe:
        try:
            r = subprocess.run(
                [ovms_exe, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            ver_text = (r.stdout or r.stderr).strip()
            for line in ver_text.split("\n"):
                if line.startswith("OpenVINO Model Server"):
                    sw["ovms"] = line.replace("OpenVINO Model Server ", "").strip()
                elif line.startswith("OpenVINO backend"):
                    sw["openvino_backend"] = line.replace("OpenVINO backend ", "").strip()
        except Exception:
            pass

    # Key Python packages
    import importlib.metadata
    for pkg in ["openvino", "agent-framework", "agent-framework-core", "openai", "mcp", "fastapi", "plotly"]:
        try:
            sw[f"pkg_{pkg}"] = importlib.metadata.version(pkg)
        except Exception:
            pass

    return hw, sw


def _build_sut_html(hw, sw):
    """Render the SUT Hardware and Software sections as HTML."""
    # ── Hardware table ──
    # CPU cores line: include P/E breakdown if available
    cores_str = f"{hw.get('cpu_cores', '-')} / {hw.get('cpu_threads', '-')}"
    if hw.get("cpu_p_cores") or hw.get("cpu_e_cores"):
        cores_str += f" &nbsp;({hw.get('cpu_p_cores', '?')}P + {hw.get('cpu_e_cores', '?')}E)"

    # iGPU line: include EU topology if available
    igpu_detail = hw.get("igpu_name", "-")
    igpu_extras = []
    if hw.get("igpu_total_eus"):
        eu_str = f"{hw['igpu_total_eus']} EUs"
        if hw.get("igpu_threads_per_eu"):
            eu_str += f" ({hw['igpu_threads_per_eu']} threads/EU, SIMD{hw.get('igpu_simd_width', '?')})"
        igpu_extras.append(eu_str)
    if hw.get("igpu_xe_cores"):
        igpu_extras.append(f"{hw['igpu_xe_cores']} Xe Cores")
    if hw.get("igpu_max_clock"):
        igpu_extras.append(hw["igpu_max_clock"])
    if hw.get("igpu_device_id"):
        igpu_extras.append(f"DevID {hw['igpu_device_id']}")
    if igpu_extras:
        igpu_detail += f" ({', '.join(igpu_extras)})"

    igpu_vram = "-"
    if hw.get("igpu_dedicated_mb"):
        igpu_vram = f"{hw['igpu_dedicated_mb']} MB dedicated"
    if hw.get("igpu_max_mem_gb"):
        igpu_vram += f" / {hw['igpu_max_mem_gb']} GB max (shared USM)"

    # NVIDIA line
    nvidia_detail = hw.get("nvidia_gpu", "-")
    nvidia_extras = []
    if hw.get("nvidia_arch"):
        nvidia_extras.append(hw["nvidia_arch"])
    if hw.get("nvidia_compute_cap"):
        nvidia_extras.append(f"SM {hw['nvidia_compute_cap']}")
    if nvidia_extras:
        nvidia_detail += f" ({', '.join(nvidia_extras)})"

    nvidia_vram = hw.get("nvidia_vram", "-")
    nvidia_perf = []
    if hw.get("nvidia_sm_clock"):
        nvidia_perf.append(f"SM {hw['nvidia_sm_clock']}")
    if hw.get("nvidia_mem_clock"):
        nvidia_perf.append(f"Mem {hw['nvidia_mem_clock']}")
    if hw.get("nvidia_pcie"):
        nvidia_perf.append(f"PCIe {hw['nvidia_pcie']}")
    if hw.get("nvidia_power_limit"):
        nvidia_perf.append(f"TDP {hw['nvidia_power_limit']}")
    if hw.get("nvidia_cuda"):
        nvidia_perf.append(f"CUDA {hw['nvidia_cuda']}")

    hw_rows = [
        ("Platform", hw.get("system_model", "-")),
        ("OEM", hw.get("system_oem", "-")),
        ("CPU", hw.get("cpu_model", "-")),
        ("Cores / Threads", cores_str),
        ("Max Clock", f"{hw.get('cpu_max_mhz', '-')} MHz"),
        ("RAM", f"{hw.get('ram_gb', '-')} GB"),
        ("Intel iGPU", igpu_detail),
        ("iGPU VRAM", igpu_vram),
        ("NVIDIA GPU", nvidia_detail),
        ("NVIDIA VRAM", nvidia_vram),
    ]
    if nvidia_perf:
        hw_rows.append(("NVIDIA Perf", " · ".join(nvidia_perf)))
    hw_rows += [
        ("NPU", hw.get("npu_device", "-")),
        ("Hostname", hw.get("hostname", "-")),
        ("OS", hw.get("os", "-")),
    ]
    hw_html = '<div class="section"><h2>System Under Test — Hardware</h2><table>'
    hw_html += '<tr><th style="width:200px">Component</th><th>Detail</th></tr>'
    for label, val in hw_rows:
        hw_html += f'<tr><td>{html.escape(label)}</td><td>{str(val)}</td></tr>'
    hw_html += '</table></div>'

    # ── Software table ──
    sw_rows = [
        ("Python", sw.get("python", "-")),
        ("OVMS", sw.get("ovms", "-")),
        ("OpenVINO Backend", sw.get("openvino_backend", "-")),
        ("OpenVINO (pip)", sw.get("pkg_openvino", "-")),
        ("Agent Framework", sw.get("pkg_agent-framework", "-")),
        ("Agent Framework Core", sw.get("pkg_agent-framework-core", "-")),
        ("OpenAI SDK", sw.get("pkg_openai", "-")),
        ("MCP SDK", sw.get("pkg_mcp", "-")),
        ("FastAPI", sw.get("pkg_fastapi", "-")),
        ("Plotly", sw.get("pkg_plotly", "-")),
        ("NVIDIA Driver", sw.get("nvidia_driver", "-")),
        ("Intel iGPU Driver", sw.get("igpu_driver", "-")),
        ("NPU Driver", sw.get("npu_driver", "-")),
    ]
    sw_html = '<div class="section"><h2>System Under Test — Software / Middleware</h2><table>'
    sw_html += '<tr><th style="width:200px">Component</th><th>Version</th></tr>'
    for label, val in sw_rows:
        sw_html += f'<tr><td>{html.escape(label)}</td><td><code>{html.escape(str(val))}</code></td></tr>'
    sw_html += '</table></div>'

    return hw_html + sw_html


# Bytes per weight element, keyed by quantization tag found in the model name (rough heuristic
# covering the schemes this benchmark's presets actually use).
_QUANT_BYTES_PER_WEIGHT = [
    ("int4", 0.5),
    ("int8", 1.0),
    ("fp16", 2.0),
    ("bf16", 2.0),
    ("fp32", 4.0),
]


def _bytes_per_weight(model_name: str) -> float:
    name_lower = (model_name or "").lower()
    for tag, b in _QUANT_BYTES_PER_WEIGHT:
        if tag in name_lower:
            return b
    return 0.5  # default: every current preset is int4-quantized


def _estimate_params_b(model_weight_mb, bytes_per_weight) -> float:
    if model_weight_mb and model_weight_mb > 0:
        return (model_weight_mb * 1024 * 1024) / bytes_per_weight / 1e9
    return 4.0  # fallback used when weight size couldn't be measured (matches prior convention)


def _roofline_points(wkpi, params_b, bytes_per_weight):
    """Per-stage (prefill, decode) roofline points.

    Prefill processes the whole prompt as one batched pass - weight bytes are read once but
    reused across every input token, so its arithmetic intensity scales with input length and is
    typically deep in the compute-bound region. Decode processes one token at a time (batch=1) -
    the full weight has to be re-read from memory for every single token, so its intensity is a
    small constant (2 / bytes_per_weight) regardless of context length or model size - the classic
    memory-bandwidth-bound LLM decode signature.

    Returns a list of dicts: {stage, phase, ai_flops_per_byte, gflops_s, is_cold}.
    """
    params = params_b * 1e9
    points = []
    for name, s in wkpi.get("stages", {}).items():
        if name == "task_agent":
            continue
        input_tokens = s.get("input_tokens", 0)
        prefill_ms = s.get("prefill_ms_est")
        if input_tokens and prefill_ms:
            gflops_s = (2 * params * input_tokens) / (prefill_ms / 1000) / 1e9
            points.append({
                "stage": name, "phase": "prefill",
                "ai_flops_per_byte": (2 * input_tokens) / bytes_per_weight,
                "gflops_s": gflops_s, "is_cold": s.get("is_cold"),
            })
        avg_itl_ms = s.get("avg_itl_ms")
        if avg_itl_ms:
            gflops_s = (2 * params) / (avg_itl_ms / 1000) / 1e9
            points.append({
                "stage": name, "phase": "decode",
                "ai_flops_per_byte": 2 / bytes_per_weight,
                "gflops_s": gflops_s, "is_cold": s.get("is_cold"),
            })
    return points


def _build_roofline_html(wkpi, exp_meta):
    """Roofline analysis: plots each stage's prefill/decode phase as (arithmetic intensity,
    achieved GFLOPs/s) against this device's memory-bandwidth and compute roofs, on a log-log
    chart. See docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md section 9 for the full methodology."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return ""

    backend = exp_meta.get("backend", "ovms")
    is_nvidia = backend == "ollama"
    device_type = (exp_meta.get("device_type") or "").upper()
    is_npu = not is_nvidia and device_type == "NPU"
    model_name = exp_meta.get("model", "")
    model_weight_mb = exp_meta.get("model_weight_mb", 0)

    bytes_per_weight = _bytes_per_weight(model_name)
    params_b = _estimate_params_b(model_weight_mb, bytes_per_weight)
    points = _roofline_points(wkpi, params_b, bytes_per_weight)
    if not points:
        return ""

    # ---- Memory + compute roofs ----
    if is_nvidia:
        mem_bw_gbs = 672.0  # GDDR7, matches _build_efficiency_html's NVIDIA constant
        compute_roofs = [("NVIDIA FP16 Tensor Peak (theoretical)", 123400.0, True)]
        device_label = "NVIDIA GPU"
    elif is_npu:
        mem_bw_gbs = 89.0  # shared system DDR5
        # No vendor-published NPU FLOPS spec is detectable from this benchmark's HW probing, so
        # the compute roof is derived empirically from this run's own best prefill throughput
        # (prefill is deep in the compute-bound region, making it a reasonable practical ceiling).
        prefill_gflops = [p["gflops_s"] for p in points if p["phase"] == "prefill"]
        empirical_peak = max(prefill_gflops) if prefill_gflops else 0.0
        compute_roofs = [("NPU Empirically Observed Peak (this run)", empirical_peak, False)]
        device_label = "NPU"
    else:
        mem_bw_gbs = 89.0  # shared system DDR5
        compute_roofs = [
            ("iGPU Theoretical Peak (FP16 FMA)", 4096.0, True),
            ("iGPU Measured INT4 GEMM Ceiling", 78.0, False),
        ]
        device_label = "iGPU"

    mem_bw_bytes_s = mem_bw_gbs * 1e9
    ridge_ai = max((r[1] * 1e9 / mem_bw_bytes_s for r in compute_roofs if r[1] > 0), default=1.0)

    # ---- Roofline curves (log-log): memory-bound diagonal up to each roof's ridge point, then flat ----
    ai_values = [p["ai_flops_per_byte"] for p in points]
    ai_min = min(0.5, min(ai_values) / 2) if ai_values else 0.5
    ai_max = max(ridge_ai * 4, max(ai_values, default=1.0) * 2)

    fig = go.Figure()
    for label, peak_gflops_s, is_theoretical in compute_roofs:
        if peak_gflops_s <= 0:
            continue
        peak_bytes_s = peak_gflops_s * 1e9
        this_ridge_ai = peak_bytes_s / mem_bw_bytes_s
        xs = [ai_min, this_ridge_ai, ai_max]
        ys = [ai_min * mem_bw_bytes_s / 1e9, peak_gflops_s, peak_gflops_s]
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=label,
            line=dict(dash="dot" if is_theoretical else "solid", width=2),
        ))

    phase_style = {
        "prefill": dict(symbol="triangle-up", color="#58a6ff"),
        "decode": dict(symbol="circle", color="#ff9838"),
    }
    for phase in ("prefill", "decode"):
        subset = [p for p in points if p["phase"] == phase]
        if not subset:
            continue
        fig.add_trace(go.Scatter(
            x=[p["ai_flops_per_byte"] for p in subset],
            y=[p["gflops_s"] for p in subset],
            mode="markers", name=phase.capitalize(),
            marker=dict(size=10, **phase_style[phase]),
            text=[f"{p['stage']} ({'cold' if p['is_cold'] else 'warm' if p['is_cold'] is False else '?'})" for p in subset],
            hovertemplate="%{text}<br>AI=%{x:.2f} FLOPs/B<br>%{y:.1f} GFLOPs/s<extra></extra>",
        ))

    fig.update_xaxes(type="log", title="Arithmetic Intensity (FLOPs/Byte)")
    fig.update_yaxes(type="log", title="Achieved Performance (GFLOPs/s)")
    fig.update_layout(
        template="plotly_dark", height=480, margin=dict(l=60, r=20, t=20, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(22,27,34,1)",
    )
    chart_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    rows_html = "".join(
        f'<tr><td>{html.escape(p["stage"])}</td><td>{p["phase"]}</td>'
        f'<td class="num">{p["ai_flops_per_byte"]:.2f}</td><td class="num">{p["gflops_s"]:.1f}</td></tr>'
        for p in sorted(points, key=lambda p: (p["stage"], p["phase"]))
    )
    note = (
        f'Params estimated at ~{params_b:.1f}B from model weight size ({bytes_per_weight} bytes/weight). '
        f'Prefill (▲) reuses weights across the whole prompt in one batched pass - high arithmetic '
        f'intensity, typically compute-bound. Decode (●) re-reads the full weight per token (batch=1) - '
        f'a small constant intensity ({2/bytes_per_weight:.1f} FLOPs/Byte) regardless of context length, '
        f'the classic memory-bandwidth-bound signature. Points near/above a roof are running near that '
        f'ceiling for this device.'
    )

    return f"""<div class="section"><h2>{device_label} Roofline Projection</h2>
<p style="color:var(--text2);font-size:0.8rem;">{note}</p>
{chart_html}
<table><tr><th>Stage</th><th>Phase</th><th style="text-align:right">AI (FLOPs/Byte)</th><th style="text-align:right">GFLOPs/s</th></tr>
{rows_html}</table></div>"""


def _build_efficiency_html(wkpi, exp_meta):
    """Derive and render compute efficiency metrics — adapts to NPU, GPU (integrated), or NVIDIA (Ollama)."""
    backend = exp_meta.get("backend", "ovms")
    is_nvidia = backend == "ollama"
    device_type = (exp_meta.get("device_type") or "").upper()
    is_npu = not is_nvidia and device_type == "NPU"

    # Compute decode-only throughput: exclude task_agent orchestrator and subtract TTFT from each agent
    stages = wkpi.get("stages", {})
    decode_tokens = 0
    decode_seconds = 0.0
    for name, s in stages.items():
        if name == "task_agent":
            continue
        out_tok = s.get("output_tokens", 0)
        wall = s.get("wall_time_s", 0)
        ttft = s.get("ttft_s", 0)
        if out_tok > 0 and wall > ttft:
            decode_tokens += out_tok
            decode_seconds += wall - ttft

    # Wall-clock aggregate (for reference row)
    total_output_tokens = sum(s.get("output_tokens", 0) for s in stages.values())
    total_wall_s = wkpi.get("workflow_wall_time_s", 0)
    e2e_tok_per_s = total_output_tokens / total_wall_s if total_wall_s else 0

    if decode_tokens == 0 or decode_seconds == 0:
        return ""

    decode_tok_per_s = decode_tokens / decode_seconds

    model_weight_mb = exp_meta.get("model_weight_mb", 0)
    bytes_per_weight = _bytes_per_weight(exp_meta.get("model", ""))
    est_params_b = _estimate_params_b(model_weight_mb, bytes_per_weight)

    flops_per_token = 2 * est_params_b * 1e9
    theoretical_tops = decode_tok_per_s * flops_per_token / 1e12

    rows = [
        ("End-to-End Throughput", f"{e2e_tok_per_s:.1f} tok/s"),
        ("Decode Throughput (sum-of-agents)", f"{decode_tok_per_s:.1f} tok/s"),
        ("Model Weight", f"{model_weight_mb:,} MB" if model_weight_mb else "-"),
        ("Est. Parameters", f"~{est_params_b:.1f}B"),
        ("FLOPs/Token (2×params est.)", f"{flops_per_token/1e9:.1f} GFLOPs"),
        ("Theoretical Compute Demand", f"{theoretical_tops:.3f} TFLOPS"),
    ]

    if is_nvidia:
        NV_PEAK_TFLOPS_FP16 = 123.4
        NV_PEAK_TOPS_INT4 = 988.0
        NV_VRAM_BW_GBS = 672.0
        rows += [
            ("NVIDIA FP16 Tensor Peak", f"{NV_PEAK_TFLOPS_FP16:.1f} TFLOPS"),
            ("NVIDIA INT4 Tensor Peak", f"{NV_PEAK_TOPS_INT4:.0f} TOPS"),
            ("NVIDIA VRAM BW Peak (GDDR7)", f"{NV_VRAM_BW_GBS:.0f} GB/s"),
        ]
        if model_weight_mb > 0:
            mem_read_per_token_gb = model_weight_mb / 1024
            achieved_bw_gbs = decode_tok_per_s * mem_read_per_token_gb
            mem_bw_utilization_pct = (achieved_bw_gbs / NV_VRAM_BW_GBS) * 100
            rows += [
                ("VRAM BW Achieved", f"{achieved_bw_gbs:.1f} GB/s"),
                ("VRAM BW Utilization", f"{mem_bw_utilization_pct:.1f}%"),
            ]
        section_title = "NVIDIA GPU Efficiency Analysis"
        section_note = (
            'LLM decode is memory-bandwidth-bound: each token reads the full model weight from VRAM. '
            'NVIDIA RTX 5070 with 12 GB GDDR7 @ 672 GB/s. '
            'Compute peaks are theoretical tensor-core maximums.'
        )
    else:
        device_label = "NPU" if is_npu else "GPU"
        if not is_npu:
            # Measured/theoretical peaks below are specific to this platform's integrated GPU.
            IGPU_PEAK_TFLOPS_FP16 = 4.096
            IGPU_MEASURED_TOPS_INT4 = 0.078
            rows += [
                (f"Measured {device_label} INT4 GEMM Ceiling", f"{IGPU_MEASURED_TOPS_INT4:.3f} TOPS"),
                (f"{device_label} Theoretical Peak (FP16 FMA)", f"{IGPU_PEAK_TFLOPS_FP16:.2f} TFLOPS"),
            ]
        ddr5_peak_bw_gbs = 89.0
        if model_weight_mb > 0:
            mem_read_per_token_gb = model_weight_mb / 1024
            achieved_bw_gbs = decode_tok_per_s * mem_read_per_token_gb
            mem_bw_utilization_pct = (achieved_bw_gbs / ddr5_peak_bw_gbs) * 100
        else:
            achieved_bw_gbs = 0.0
            mem_bw_utilization_pct = 0.0
        rows += [
            ("DRAM BW Achieved", f"{achieved_bw_gbs:.1f} GB/s"),
            ("DRAM BW Peak (DDR5)", f"{ddr5_peak_bw_gbs:.0f} GB/s"),
            ("Memory BW Utilization", f"{mem_bw_utilization_pct:.1f}%"),
        ]
        section_title = f"{device_label} Efficiency Analysis"
        section_note = (
            f'LLM decode is memory-bandwidth-bound: each token reads the full model weight from DRAM. '
            f'Compute demand is theoretical (2×params)'
            + ('; measured GEMM ceiling confirms low actual INT4 throughput on this GPU. ' if not is_npu else '. ')
            + 'Memory BW utilization is the meaningful efficiency metric for decode.'
        )

    html = f'<div class="section"><h2>{section_title}</h2>'
    html += f'<p style="color:var(--text2);font-size:0.8rem;">{section_note}</p>'
    html += '<table><tr><th>Metric</th><th>Value</th></tr>'
    for label, val in rows:
        html += f'<tr><td>{label}</td><td class="num">{val}</td></tr>'
    html += '</table></div>'
    return html


def _build_peak_rss_html(peak_rss):
    """Render Peak RSS summary as HTML table.

    Shows only the highest-RSS entry per process category, and skips
    the sampler itself and tiny cmd.exe wrapper processes (<10 MB).
    """
    if not peak_rss:
        return ""

    # Skip the sampler itself and generic 'python' entries
    skip_categories = {"sample_utilization_fast", "python"}

    # For each category, pick the entry with the highest peak_rss_mb
    rows = []
    for category, entries in peak_rss.items():
        if not isinstance(entries, list):
            continue
        if category in skip_categories:
            continue
        # Find the entry with the highest peak RSS (skip tiny wrappers)
        best = None
        for entry in entries:
            rss = entry.get("peak_rss_mb", entry.get("rss_mb", 0))
            if rss < 10:
                continue  # Skip cmd.exe wrappers (~4 MB)
            if best is None or rss > best.get("peak_rss_mb", 0):
                best = entry
        if best:
            rows.append((category, best))

    if not rows:
        return ""

    # Sort by peak RSS descending
    rows.sort(key=lambda r: r[1].get("peak_rss_mb", 0), reverse=True)

    html = '<div class="section"><h2>Peak Memory (RSS) per Process</h2>'
    html += '<p style="color:var(--text2);font-size:0.8rem;">Peak working set (physical RAM) recorded at experiment shutdown.</p>'
    html += '<table><tr><th>Process</th><th style="text-align:right">PID</th>'
    html += '<th style="text-align:right">Peak RSS (MB)</th><th>Command</th></tr>'

    for category, entry in rows:
        pid = entry.get("pid", "-")
        rss_mb = entry.get("peak_rss_mb", entry.get("rss_mb", 0))
        cmdline = entry.get("cmdline", "")
        cmd_display = cmdline[:80] + "..." if len(cmdline) > 80 else cmdline
        html += (f'<tr><td><strong>{category}</strong></td>'
                 f'<td class="num">{pid}</td>'
                 f'<td class="num">{rss_mb:,}</td>'
                 f'<td><code style="font-size:0.75rem">{cmd_display}</code></td></tr>')

    html += '</table></div>'
    return html


def _build_model_weight_html(exp_meta):
    """Render model weight info from experiment metadata."""
    weight_mb = exp_meta.get("model_weight_mb", 0)
    if not weight_mb:
        return ""
    weight_gb = weight_mb / 1024
    path = exp_meta.get("model_weight_path", "-")
    html = '<div class="section"><h2>Model Weight</h2><table>'
    html += '<tr><th>Property</th><th>Value</th></tr>'
    html += f'<tr><td>Total Size</td><td class="num">{weight_mb:,} MB ({weight_gb:.2f} GB)</td></tr>'
    html += f'<tr><td>Path</td><td><code>{path}</code></td></tr>'
    html += '</table></div>'
    return html


def _build_startup_timing_html(exp_meta, rkpi):
    """Render server startup and model loading times from experiment metadata."""
    llm_t = exp_meta.get("llm_load_time_s")
    emb_t = exp_meta.get("embed_load_time_s")
    mcp_t = exp_meta.get("mcp_startup_time_s")
    total_t = exp_meta.get("total_startup_time_s")
    rag_t = rkpi.get("embedding_setup_time_s") if rkpi else None
    if not any([llm_t, emb_t, mcp_t]):
        return ""
    backend = exp_meta.get("backend", "ovms")
    embed_dev = exp_meta.get("embed_device", "")
    llm_label = "OVMS (iGPU)" if backend == "ovms" else "Ollama (NVIDIA)"
    emb_label = {"npu": "NPU (OpenVINO INT8)", "cpu": "CPU (Ollama)"}.get(embed_dev, embed_dev)

    html = '<div class="section"><h2>Startup &amp; Model Loading</h2>'
    html += '<p style="color:var(--text2);font-size:0.85rem">Time from cold start to server ready. '
    html += 'In a local workflow these costs are user-facing; in a server deployment they are amortized.</p>'
    html += '<table><tr><th>Component</th><th>Device</th><th style="text-align:right">Time (s)</th></tr>'
    if llm_t is not None:
        html += f'<tr><td>LLM Server</td><td>{llm_label}</td><td class="num">{llm_t:.1f}</td></tr>'
    if emb_t is not None:
        html += f'<tr><td>Embedding Server</td><td>{emb_label}</td><td class="num">{emb_t:.1f}</td></tr>'
    if mcp_t is not None:
        html += f'<tr><td>MCP Servers (Analysis + RAG)</td><td>CPU</td><td class="num">{mcp_t:.1f}</td></tr>'
    if rag_t is not None:
        docs = rkpi.get("docs_embedded", 0)
        tput = rkpi.get("embedding_throughput_docs_per_s", 0)
        html += f'<tr><td>RAG Index Build ({docs} docs)</td><td>{emb_label or "—"}</td><td class="num">{rag_t:.1f}</td></tr>'
    if total_t is not None:
        e2e = total_t + (rag_t or 0)
        html += f'<tr style="font-weight:600;border-top:2px solid var(--border)"><td>Total (servers + index)</td><td></td><td class="num">{e2e:.1f}</td></tr>'
    html += '</table></div>'
    return html


def build_html(wkpi, rkpi, sys_state_text, kpi_dir_name, exp_meta=None, peak_rss=None):
    if exp_meta is None:
        exp_meta = {}
    stages = wkpi.get("stages", {})
    totals = wkpi.get("totals", {})
    model = wkpi.get("model", "unknown")
    backend = wkpi.get("backend", "unknown")
    wall_time = wkpi.get("workflow_wall_time_s", 0)
    wf_start = wkpi.get("workflow_start_iso", "")
    wf_end = wkpi.get("workflow_end_iso", "")

    has_timestamps = any(s.get("start_epoch") for s in stages.values())

    # ---- Build timeline data (only if we have timestamps) ----
    timeline_rows = []
    rag_query_markers = []  # sub-event markers for RAG queries
    if has_timestamps:
        # Find earliest start for relative positioning
        all_starts = [s["start_epoch"] for s in stages.values() if s.get("start_epoch")]
        t0_epoch = min(all_starts) if all_starts else 0
        all_ends = [s["end_epoch"] for s in stages.values() if s.get("end_epoch")]
        t_max = max(all_ends) if all_ends else t0_epoch + wall_time

        # Extend range if RAG setup started before workflow agents
        rag_start = rkpi.get("embedding_start_epoch") if rkpi else None
        rag_end = rkpi.get("embedding_end_epoch") if rkpi else None
        if rag_start and rag_start < t0_epoch:
            t0_epoch = rag_start
        if rag_end and rag_end > t_max:
            t_max = rag_end

        total_span = t_max - t0_epoch if t_max > t0_epoch else 1

        # Inject RAG Setup (Phase 0) as a background bar if we have timing
        if rag_start and rag_end and rkpi.get("embedding_setup_time_s"):
            left_pct = (rag_start - t0_epoch) / total_span * 100
            width_pct = (rag_end - rag_start) / total_span * 100
            width_pct = max(width_pct, 0.5)
            docs = rkpi.get("docs_embedded", 0)
            tput = rkpi.get("embedding_throughput_docs_per_s", 0)
            timeline_rows.append({
                "name": "rag_setup",
                "left_pct": round(left_pct, 2),
                "width_pct": round(width_pct, 2),
                "start_time": _fmt_time(datetime.fromtimestamp(rag_start).isoformat(timespec='milliseconds')),
                "end_time": _fmt_time(datetime.fromtimestamp(rag_end).isoformat(timespec='milliseconds')),
                "duration_s": rkpi.get("embedding_setup_time_s", 0),
                "color": _color_for("rag_setup"),
                "tokens": 0,
                "tok_s": 0,
                "hw": PHASE_HW.get("rag_setup", "").replace("EMBED_DEVICE", _embed_device_label(rkpi)),
                "is_background": True,
                "extra_tooltip": f"{docs} docs @ {tput} docs/s",
            })

        # Build RAG query markers from rkpi epochs
        for qe in (rkpi.get("query_epochs", []) if rkpi else []):
            qs, qend = qe.get("start", 0), qe.get("end", 0)
            if qs and qend:
                rag_query_markers.append({
                    "left_pct": round((qs - t0_epoch) / total_span * 100, 2),
                    "width_pct": round(max((qend - qs) / total_span * 100, 0.3), 2),
                })

        # Agent phases — sort by start time
        ordered = sorted(
            [(name, data) for name, data in stages.items() if data.get("start_epoch")],
            key=lambda x: x[1]["start_epoch"]
        )

        # Determine LLM label based on backend
        llm_label = "iGPU (OVMS)" if backend == "ovms" else "NVIDIA (Ollama)"
        embed_label = _embed_device_label(rkpi)
        # Generic device label for stages not covered by the legacy PHASE_HW dict below
        # (e.g. SWE Agent / Data Agent stages, which aren't part of the fixed 5-agent RAG flow).
        device_type = (exp_meta.get("device_type") or "").upper()
        generic_llm_label = device_type or backend

        for i, (name, data) in enumerate(ordered):
            left_pct = (data["start_epoch"] - t0_epoch) / total_span * 100
            width_pct = (data["end_epoch"] - data["start_epoch"]) / total_span * 100
            width_pct = max(width_pct, 0.5)  # minimum visibility
            hw_raw = PHASE_HW.get(name, "")
            hw_label = hw_raw.replace("LLM", llm_label).replace("EMBED_DEVICE", embed_label) if hw_raw else generic_llm_label
            timeline_rows.append({
                "name": name,
                "left_pct": round(left_pct, 2),
                "width_pct": round(width_pct, 2),
                "start_time": _fmt_time(data.get("start_iso", "")),
                "end_time": _fmt_time(data.get("end_iso", "")),
                "duration_s": data.get("wall_time_s", 0),
                "color": _color_for(name),
                "tokens": data.get("output_tokens", 0),
                "tok_s": data.get("output_tokens_per_s", 0),
                "hw": hw_label,
                "is_background": False,
                "extra_tooltip": "",
            })

            # Tool execution (CPU-bound): the gap between this stage's inference ending and
            # the next stage's inference starting is where the harness runs the tool call(s)
            # this stage's output requested (read_file/write_file/apply_patch/execute_command).
            tool_calls = data.get("tool_calls")
            if tool_calls and i + 1 < len(ordered):
                next_start = ordered[i + 1][1]["start_epoch"]
                gap_s = next_start - data["end_epoch"]
                if gap_s > 0.2:
                    tool_left_pct = (data["end_epoch"] - t0_epoch) / total_span * 100
                    tool_width_pct = max(gap_s / total_span * 100, 0.5)
                    tool_summary = ", ".join(f"{tn}\u00d7{tc}" for tn, tc in sorted(tool_calls.items()))
                    timeline_rows.append({
                        "name": f"{name} (tools)",
                        "left_pct": round(tool_left_pct, 2),
                        "width_pct": round(tool_width_pct, 2),
                        "start_time": _fmt_time(data.get("end_iso", "")),
                        "end_time": _fmt_time(ordered[i + 1][1].get("start_iso", "")),
                        "duration_s": round(gap_s, 1),
                        "color": "rgba(139,148,158,0.55)",
                        "tokens": 0,
                        "tok_s": 0,
                        "hw": "CPU",
                        "is_background": True,
                        "extra_tooltip": tool_summary,
                    })

    # ---- Build agent table rows ----
    agent_order = ["task_agent", "analysis_agent", "summary_agent_1", "summary_agent_2", "executive_summary_agent"]
    table_rows = []
    for name in agent_order:
        if name not in stages:
            continue
        s = stages[name]
        row = {
            "name": name,
            "input_tokens": s.get("input_tokens", 0),
            "output_tokens": s.get("output_tokens", 0),
            "total_tokens": s.get("total_tokens", 0),
            "wall_time_s": s.get("wall_time_s", 0),
            "tok_s": s.get("output_tokens_per_s", 0),
            "start": _fmt_time(s.get("start_iso", "")) if s.get("start_iso") else "-",
            "end": _fmt_time(s.get("end_iso", "")) if s.get("end_iso") else "-",
            "color": _color_for(name),
            "ttft_s": s.get("ttft_s", ""),
            "prefill_ms_est": s.get("prefill_ms_est", ""),
            "avg_itl_ms": s.get("avg_itl_ms", ""),
            "itl_stddev_ms": s.get("itl_stddev_ms", ""),
            "p50_itl_ms": s.get("p50_itl_ms", ""),
            "p99_itl_ms": s.get("p99_itl_ms", ""),
            "tool_calls": s.get("tool_calls", {}),
            "is_cold": s.get("is_cold"),
            "history_tokens": s.get("history_tokens", 0),
        }
        table_rows.append(row)
    # Add any stages not in the predefined order
    for name in stages:
        if name not in agent_order:
            s = stages[name]
            table_rows.append({
                "name": name,
                "input_tokens": s.get("input_tokens", 0),
                "output_tokens": s.get("output_tokens", 0),
                "total_tokens": s.get("total_tokens", 0),
                "wall_time_s": s.get("wall_time_s", 0),
                "tok_s": s.get("output_tokens_per_s", 0),
                "start": _fmt_time(s.get("start_iso", "")) if s.get("start_iso") else "-",
                "end": _fmt_time(s.get("end_iso", "")) if s.get("end_iso") else "-",
                "color": _color_for(name),
                "ttft_s": s.get("ttft_s", ""),
                "prefill_ms_est": s.get("prefill_ms_est", ""),
                "avg_itl_ms": s.get("avg_itl_ms", ""),
                "itl_stddev_ms": s.get("itl_stddev_ms", ""),
                "p50_itl_ms": s.get("p50_itl_ms", ""),
                "p99_itl_ms": s.get("p99_itl_ms", ""),
                "tool_calls": s.get("tool_calls", {}),
                "is_cold": s.get("is_cold"),
                "history_tokens": s.get("history_tokens", 0),
            })

    # ---- RAG / Embedding section ----
    rag_html = ""
    if rkpi:
        rag_html = f"""
        <div class="section">
            <h2>RAG / Embedding KPIs</h2>
            <table>
                <tr><th>Metric</th><th>Value</th></tr>
                <tr><td>Embedding Model</td><td>{html.escape(str(rkpi.get('embedding_model', '-')))}</td></tr>
                <tr><td>Embedding Endpoint</td><td><code>{html.escape(str(rkpi.get('embedding_api_base', '-')))}</code></td></tr>
                <tr><td>Setup Time</td><td>{rkpi.get('embedding_setup_time_s', '-')}s</td></tr>
                <tr><td>Documents Indexed</td><td>{rkpi.get('docs_embedded', '-')}</td></tr>
                <tr><td>Index Throughput</td><td>{rkpi.get('embedding_throughput_docs_per_s', '-')} docs/s</td></tr>
                <tr><td>Queries Served</td><td>{rkpi.get('query_count', '-')}</td></tr>
                <tr><td>Avg Query Latency</td><td>{rkpi.get('avg_query_time_s', '-')}s</td></tr>
                <tr><td>Min / Max Query</td><td>{min(rkpi.get('query_times_s', [0])):.3f}s / {max(rkpi.get('query_times_s', [0])):.3f}s</td></tr>
            </table>
        </div>"""

    # ---- Device assignment from system_state.txt ----
    device_html = ""
    if sys_state_text:
        # Extract the DEVICE ASSIGNMENT section
        lines = sys_state_text.split("\n")
        in_device = False
        device_lines = []
        for line in lines:
            if "DEVICE ASSIGNMENT" in line:
                in_device = True
                continue
            if in_device:
                if line.startswith("---") and device_lines:
                    break
                if line.strip():
                    device_lines.append(line.strip())
        if device_lines:
            device_html = '<div class="section"><h2>Device Assignment</h2><div class="device-grid">'
            for dl in device_lines:
                parts = dl.split(":", 1)
                if len(parts) == 2:
                    role = parts[0].strip()
                    dev = parts[1].strip()
                    icon = "🔷"
                    if "iGPU" in dev:
                        icon = "🖥️"
                    elif "NPU" in dev:
                        icon = "🧠"
                    elif "CPU" in dev:
                        icon = "⚙️"
                    elif "NVIDIA" in dev:
                        icon = "🟢"
                    device_html += f'<div class="device-card"><div class="device-icon">{icon}</div><div class="device-role">{html.escape(role)}</div><div class="device-target">{html.escape(dev)}</div></div>'
            device_html += "</div></div>"

    # ---- Timeline HTML ----
    timeline_html = ""
    if timeline_rows:
        # Build time axis labels
        if has_timestamps:
            all_starts_sorted = sorted(timeline_rows, key=lambda r: r["left_pct"])
            first_time = all_starts_sorted[0]["start_time"] if all_starts_sorted else ""
            last_row = max(timeline_rows, key=lambda r: r["left_pct"] + r["width_pct"])
            last_time = last_row["end_time"]
        else:
            first_time = "0s"
            last_time = f"{wall_time:.0f}s"

        bars_html = ""
        for i, row in enumerate(timeline_rows):
            hw_badge = ""
            if row.get("hw"):
                hw_badge = f'<span class="gantt-hw">{html.escape(row["hw"])}</span>'
            bg_class = " gantt-bg-bar" if row.get("is_background") else ""
            extra_tip = f" | {row['extra_tooltip']}" if row.get("extra_tooltip") else ""
            label_suffix = ""
            if row["name"] == "rag_setup":
                label_suffix = ' <span style="font-size:0.7rem;opacity:0.6">(background)</span>'
            bars_html += f"""
            <div class="gantt-row">
                <div class="gantt-label">
                    <span class="gantt-dot" style="background:{row['color']}"></span>
                    {html.escape(row['name'])}{label_suffix}
                </div>
                <div class="gantt-track">
                    <div class="gantt-bar{bg_class}" style="left:{row['left_pct']}%;width:{row['width_pct']}%;background:{row['color']}"
                         title="{row['name']}: {row['start_time']} - {row['end_time']} ({row['duration_s']}s, {row['tokens']} tokens, {row['tok_s']} tok/s{extra_tip})">
                        <span class="gantt-bar-text">{row['duration_s']}s</span>
                        {hw_badge}
                    </div>
                </div>
            </div>"""

        # RAG query markers overlay (small triangles on the timeline)
        rag_markers_html = ""
        _rag_embed_label = _embed_device_label(rkpi)
        if rag_query_markers:
            markers = ""
            for m in rag_query_markers:
                markers += f'<div class="rag-marker" style="left:{m["left_pct"]}%;width:{m["width_pct"]}%" title="RAG query ({_rag_embed_label} embed + HNSW search)"></div>'
            rag_markers_html = f"""
            <div class="gantt-row">
                <div class="gantt-label" style="font-size:0.75rem;color:var(--text2)">
                    ★ rag_query
                </div>
                <div class="gantt-track" style="height:16px">
                    {markers}
                </div>
            </div>"""

        timeline_html = f"""
        <div class="section">
            <h2>Workflow Timeline</h2>
            <div class="timeline-axis">
                <span>{first_time}</span>
                <span>← {wall_time:.0f}s total →</span>
                <span>{last_time}</span>
            </div>
            <div class="gantt-chart">
                {bars_html}
                {rag_markers_html}
            </div>
            <div class="timeline-legend">
                <em>Hover bars for detail. Concurrent bars (summary_agent_1/2) show parallel execution.</em>
                <br><em style="font-size:0.72rem">HW badges show responsible hardware. ★ rag_query marks {_rag_embed_label} embedding + HNSW vector search events.</em>
            </div>
        </div>"""
    else:
        timeline_html = """
        <div class="section">
            <h2>Workflow Timeline</h2>
            <p class="no-data">No phase timestamps available. Re-run the workflow with <code>--kpi</code> to generate timeline data.</p>
        </div>"""

    # ---- Agent table ----
    rows_html = ""
    has_tool_calls = any(r.get("tool_calls") for r in table_rows)
    # Tools that actually run code/commands vs. tools that just read/inspect state.
    _EXECUTION_TOOLS = {"execute_command", "execute", "run_command", "apply_patch"}
    for r in table_rows:
        ttft_cell = f"{r['ttft_s']}" if r.get('ttft_s') != '' else "-"
        prefill_cell = f"{r['prefill_ms_est']:,.0f}" if r.get('prefill_ms_est') != '' else "-"
        itl_cell = f"{r['avg_itl_ms']}" if r.get('avg_itl_ms') != '' else "-"
        if r.get('avg_itl_ms') != '' and r.get('itl_stddev_ms') != '':
            itl_cell = f"{r['avg_itl_ms']} \u00b1{r['itl_stddev_ms']}"
        p50_cell = f"{r['p50_itl_ms']}" if r.get('p50_itl_ms') != '' else "-"
        p99_cell = f"{r['p99_itl_ms']}" if r.get('p99_itl_ms') != '' else "-"
        is_orchestrator = r['name'] == 'task_agent'
        row_style = ' style="color:var(--text2); font-style:italic"' if is_orchestrator else ''
        orch_suffix = ' <span style="font-size:0.7rem; opacity:0.6">(orchestrator)</span>' if is_orchestrator else ''
        phase_badge = ""
        if r.get('is_cold') is True:
            phase_badge = ' <span class="phase-badge phase-badge-cold" title="Fresh context - no prior turn KV cache reused">cold</span>'
        elif r.get('is_cold') is False:
            phase_badge = (f' <span class="phase-badge phase-badge-warm" '
                           f'title="Continues prior turn - {r["history_tokens"]} history tokens carried in KV cache">'
                           f'warm +{r["history_tokens"]}</span>')
        # For orchestrator, dim the misleading ITL/tok_s (they include sub-agent wait time)
        if is_orchestrator:
            itl_cell = f'<span title="Includes sub-agent wait time">{itl_cell}</span>'
            tok_s_cell = f'<span title="Includes sub-agent wait time">{r["tok_s"]}</span>'
        else:
            tok_s_cell = str(r['tok_s'])
        tools_cell = "-"
        if has_tool_calls:
            tool_calls = r.get("tool_calls") or {}
            if tool_calls:
                badges = "".join(
                    f'<span class="tool-badge{" tool-badge-exec" if name in _EXECUTION_TOOLS else ""}" '
                    f'title="{name} called {count}x">{html.escape(name)}\u00d7{count}</span>'
                    for name, count in sorted(tool_calls.items())
                )
                tools_cell = badges
        rows_html += f"""
            <tr{row_style}>
                <td><span class="gantt-dot" style="background:{r['color']}"></span> {html.escape(r['name'])}{orch_suffix}{phase_badge}</td>
                <td class="num">{r['start']}</td>
                <td class="num">{r['end']}</td>
                <td class="num">{r['input_tokens']:,}</td>
                <td class="num">{r['output_tokens']:,}</td>
                <td class="num">{r['total_tokens']:,}</td>
                <td class="num">{r['wall_time_s']:.1f}</td>
                <td class="num">{tok_s_cell}</td>
                <td class="num">{ttft_cell}</td>
                <td class="num">{prefill_cell}</td>
                <td class="num">{itl_cell}</td>
                <td class="num" title="p50 / p99">{p50_cell} / {p99_cell}</td>{f'<td>{tools_cell}</td>' if has_tool_calls else ''}
            </tr>"""

    total_out_tps = totals.get("output_tokens_per_s", 0)

    # ---- Collect SUT info ----
    hw_info, sw_info = _collect_sut_info()
    sut_html = _build_sut_html(hw_info, sw_info)

    # ---- New KPI sections ----
    efficiency_html = _build_efficiency_html(wkpi, exp_meta)
    roofline_html = _build_roofline_html(wkpi, exp_meta)
    peak_rss_html = _build_peak_rss_html(peak_rss)
    model_weight_html = _build_model_weight_html(exp_meta)
    startup_html = _build_startup_timing_html(exp_meta, rkpi)

    # ---- Assemble full HTML ----
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wf_start_display = _fmt_time(wf_start) if wf_start else "-"
    wf_end_display = _fmt_time(wf_end) if wf_end else "-"

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-Agent KPI Report — {report_time}</title>
<style>
:root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text2: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
       background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5; }}
h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
h2 {{ font-size: 1.15rem; color: var(--accent); margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
.header {{ margin-bottom: 24px; }}
.header .subtitle {{ color: var(--text2); font-size: 0.9rem; }}
.header .meta {{ display: flex; gap: 24px; margin-top: 8px; flex-wrap: wrap; }}
.header .meta-item {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px; }}
.header .meta-label {{ color: var(--text2); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }}
.header .meta-value {{ font-size: 1.3rem; font-weight: 600; }}
.header .meta-value.green {{ color: var(--green); }}
.section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
th {{ text-align: left; color: var(--text2); font-weight: 500; padding: 8px 10px; border-bottom: 2px solid var(--border); }}
td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: 'SF Mono', Consolas, monospace; }}
tr:hover td {{ background: rgba(88,166,255,0.06); }}
code {{ background: rgba(110,118,129,0.2); padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}

/* Gantt chart */
.gantt-chart {{ margin: 8px 0; }}
.gantt-row {{ display: flex; align-items: center; margin-bottom: 6px; }}
.gantt-label {{ width: 200px; flex-shrink: 0; font-size: 0.82rem; padding-right: 12px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.gantt-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
.gantt-track {{ flex: 1; height: 28px; background: rgba(48,54,61,0.5); border-radius: 4px; position: relative; overflow: hidden; }}
.gantt-bar {{ position: absolute; top: 2px; height: 24px; border-radius: 3px; opacity: 0.85; display: flex; align-items: center;
              justify-content: center; cursor: default; transition: opacity 0.15s; min-width: 2px; }}
.gantt-bar:hover {{ opacity: 1; box-shadow: 0 0 8px rgba(88,166,255,0.3); }}
.gantt-bar-text {{ color: #fff; font-size: 0.7rem; font-weight: 600; text-shadow: 0 1px 2px rgba(0,0,0,0.5);
                   white-space: nowrap; overflow: hidden; padding: 0 4px; }}
.gantt-hw {{ position: absolute; right: 4px; top: 50%; transform: translateY(-50%); font-size: 0.6rem;
             background: rgba(0,0,0,0.45); color: #ddd; padding: 1px 5px; border-radius: 3px;
             white-space: nowrap; line-height: 1.2; pointer-events: none; }}
.gantt-bg-bar {{ opacity: 0.45; border: 1px dashed rgba(255,255,255,0.3); }}
.gantt-bg-bar:hover {{ opacity: 0.7; }}
.rag-marker {{ position: absolute; top: 2px; height: 12px; background: #FF9800; border-radius: 2px;
               opacity: 0.9; min-width: 4px; cursor: default; }}
.rag-marker:hover {{ opacity: 1; box-shadow: 0 0 6px rgba(255,152,0,0.5); }}
.tool-badge {{ display: inline-block; font-size: 0.68rem; padding: 1px 6px; margin: 1px 2px; border-radius: 3px;
               background: rgba(88,166,255,0.15); color: #79b8ff; white-space: nowrap; }}
.tool-badge-exec {{ background: rgba(63,185,80,0.18); color: #56d364; }}
.phase-badge {{ display: inline-block; font-size: 0.65rem; padding: 1px 6px; margin-left: 4px; border-radius: 3px;
               white-space: nowrap; vertical-align: middle; }}
.phase-badge-cold {{ background: rgba(121,192,255,0.15); color: #79c0ff; }}
.phase-badge-warm {{ background: rgba(255,152,56,0.15); color: #ff9838; }}
.timeline-axis {{ display: flex; justify-content: space-between; color: var(--text2); font-size: 0.78rem;
                  margin-bottom: 4px; padding: 0 200px 0 0; margin-left: 200px; }}
.timeline-legend {{ color: var(--text2); font-size: 0.78rem; margin-top: 8px; }}
.no-data {{ color: var(--text2); font-style: italic; }}

/* Device cards */
.device-grid {{ display: flex; gap: 14px; flex-wrap: wrap; }}
.device-card {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px;
                min-width: 180px; flex: 1; }}
.device-icon {{ font-size: 1.5rem; margin-bottom: 4px; }}
.device-role {{ font-weight: 600; font-size: 0.9rem; }}
.device-target {{ color: var(--text2); font-size: 0.8rem; margin-top: 2px; }}

/* Responsive */
@media (max-width: 800px) {{
    .gantt-label {{ width: 120px; font-size: 0.72rem; }}
    .timeline-axis {{ margin-left: 120px; padding: 0 0; }}
    .header .meta {{ gap: 10px; }}
    .device-grid {{ flex-direction: column; }}
}}
</style>
</head>
<body>

<div class="header">
    <h1>Multi-Agent KPI Report</h1>
    <div class="subtitle">Generated {report_time} &nbsp;|&nbsp; Source: <code>{html.escape(kpi_dir_name)}</code></div>
    <div class="meta">
        <div class="meta-item">
            <div class="meta-label">Model</div>
            <div class="meta-value">{html.escape(model)}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Backend</div>
            <div class="meta-value">{html.escape(backend)}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Wall Time</div>
            <div class="meta-value">{wall_time:.1f}s</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Output Tokens</div>
            <div class="meta-value green">{totals.get('output_tokens', 0):,}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Throughput</div>
            <div class="meta-value green">{total_out_tps} tok/s</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Run Window</div>
            <div class="meta-value" style="font-size:1rem">{wf_start_display} → {wf_end_display}</div>
        </div>
    </div>
</div>

{device_html}

{timeline_html}

<div class="section">
    <h2>Per-Agent KPIs</h2>
    <table>
        <thead>
            <tr>
                <th>Agent</th>
                <th style="text-align:right">Start</th>
                <th style="text-align:right">End</th>
                <th style="text-align:right">In Tok</th>
                <th style="text-align:right">Out Tok</th>
                <th style="text-align:right">Total</th>
                <th style="text-align:right">Time (s)</th>
                <th style="text-align:right">Tok/s</th>
                <th style="text-align:right">TTFT (s)</th>
                <th style="text-align:right">Prefill (ms)</th>
                <th style="text-align:right">Avg ITL (ms)</th>
                <th style="text-align:right">p50/p99 ITL (ms)</th>
                {'<th>Tools</th>' if has_tool_calls else ''}
            </tr>
        </thead>
        <tbody>
            {rows_html}
            <tr style="font-weight:600; border-top: 2px solid var(--border);">
                <td>TOTAL</td>
                <td></td><td></td>
                <td class="num">{totals.get('input_tokens', 0):,}</td>
                <td class="num">{totals.get('output_tokens', 0):,}</td>
                <td class="num">{totals.get('total_tokens', 0):,}</td>
                <td class="num">{wall_time:.1f}</td>
                <td class="num">{total_out_tps}</td>
                <td></td><td></td><td></td><td></td>{'<td></td>' if has_tool_calls else ''}
            </tr>
        </tbody>
    </table>
</div>

{rag_html}

{efficiency_html}

{roofline_html}

{model_weight_html}

{startup_html}

{peak_rss_html}

{sut_html}

<div style="color:var(--text2); font-size:0.75rem; text-align:center; margin-top:20px;">
    LiteAgent-Clash &mdash; Heterogeneous AI Workload Orchestration
</div>

</body>
</html>"""
    return html_doc


def main():
    parser = argparse.ArgumentParser(description="Generate HTML KPI report")
    parser.add_argument("--kpi-dir", default=None,
                        help="Directory containing workflow_kpi.json, rag_kpi.json, system_state.txt")
    parser.add_argument("--exp-dir", default=None,
                        help="Experiment output directory (contains experiment.json, *_peak_rss.json)")
    parser.add_argument("--output", default=None,
                        help="Output HTML file path")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    outputs_dir = os.path.join(script_dir, "outputs")

    # Find KPI files
    kpi_dir = args.kpi_dir
    if kpi_dir and not os.path.isabs(kpi_dir):
        kpi_dir = os.path.join(script_dir, kpi_dir)

    # Try kpi_dir first, fall back to outputs/
    wkpi_path = None
    for candidate_dir in ([kpi_dir] if kpi_dir else []) + [outputs_dir]:
        p = os.path.join(candidate_dir, "workflow_kpi.json")
        if os.path.exists(p):
            wkpi_path = p
            break

    if not wkpi_path:
        print("ERROR: workflow_kpi.json not found. Run the workflow with --kpi first.", file=sys.stderr)
        sys.exit(1)

    wkpi = load_json(wkpi_path)
    actual_dir = os.path.dirname(wkpi_path)
    kpi_dir_name = os.path.basename(actual_dir)

    rkpi = load_json(os.path.join(actual_dir, "rag_kpi.json"))
    if not rkpi:
        rkpi = load_json(os.path.join(outputs_dir, "rag_kpi.json"))

    sys_state = load_text(os.path.join(actual_dir, "system_state.txt"))

    # Resolve experiment directory (for experiment.json, peak_rss, etc.)
    exp_dir = args.exp_dir
    if exp_dir and not os.path.isabs(exp_dir):
        exp_dir = os.path.join(script_dir, exp_dir)
    # Search order: --exp-dir, then --output dir, then actual_dir (kpi-dir)
    search_dirs = []
    if exp_dir:
        search_dirs.append(exp_dir)
    if args.output:
        out_abs = args.output if os.path.isabs(args.output) else os.path.join(script_dir, args.output)
        search_dirs.append(os.path.dirname(out_abs))
    search_dirs.append(actual_dir)

    # Load experiment metadata (model weight, platform info)
    exp_meta = {}
    for d in search_dirs:
        exp_meta = load_json(os.path.join(d, "experiment.json")) or {}
        if exp_meta:
            break

    # Load peak RSS summary
    peak_rss = None
    for d in search_dirs:
        for rss_name in ["hw_samples_peak_rss.json", "peak_rss.json"]:
            peak_rss = load_json(os.path.join(d, rss_name))
            if peak_rss:
                break
        if not peak_rss:
            import glob
            rss_files = glob.glob(os.path.join(d, "*_peak_rss.json"))
            if rss_files:
                peak_rss = load_json(rss_files[0])
        if peak_rss:
            break

    html_content = build_html(wkpi, rkpi, sys_state, kpi_dir_name, exp_meta, peak_rss)

    # Output path
    if args.output:
        out_path = args.output if os.path.isabs(args.output) else os.path.join(script_dir, args.output)
    else:
        out_path = os.path.join(outputs_dir, "kpi_report.html")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"KPI report written to {out_path}")


if __name__ == "__main__":
    main()

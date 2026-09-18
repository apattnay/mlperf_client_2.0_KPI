#!/usr/bin/env python3
"""Preset launcher for KPI-hub benchmark runs (see docs/run_benchmark_prompt.md).

Preset 1: Full base prompt set (all agents), NPU            - Workflow KPI + HW KPI
Preset 2: Full base prompt set (all agents), iGPU           - Workflow KPI + HW KPI
Preset 3: Code-analysis prompts only, NPU                   - Workflow KPI + HW KPI
Preset 4: Code-analysis prompts only, iGPU                  - Workflow KPI + HW KPI
Preset 5: SWE Agent agentic workflow (data/configs/vendors_default/agentic), NPU  - Workflow KPI + HW KPI
Preset 6: SWE Agent agentic workflow (data/configs/vendors_default/agentic), iGPU - Workflow KPI + HW KPI

All presets run against the mlperf 2.0 install (see DEFAULT_MLPERF_DIR) and need network access
on first run to fetch models/prompts (not cached locally) - see docs/run_benchmark_prompt.md,
including the corporate proxy prerequisite (tools/set_proxy_env.ps1). Presets 5/6 additionally run
Python via the agentic 'execute' tool.

Usage:
    .venv\\Scripts\\python.exe tools\\run_kpi_preset.py --preset 4
"""
import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_KPI_WORKFLOW = Path(__file__).resolve().parent / "run_kpi_workflow.py"
DEFAULT_MLPERF_DIR = r"C:\Applications\mlperf_client\mlperf_v2p0"

PRESETS = {
    1: {"name": "preset1_full_npu", "config": "llm/Llama3.1/Intel_NativeOpenVINO_NPU_Default.json", "download_behaviour": "normal"},
    2: {"name": "preset2_full_gpu", "config": "llm/Llama3.1/Intel_NativeOpenVINO_GPU_Default.json", "download_behaviour": "normal"},
    3: {"name": "preset3_swe_npu", "config": str(REPO_ROOT / "data" / "configs" / "kpi_presets" / "Llama3.1_NativeOpenVINO_NPU_SWEAgent.json"), "download_behaviour": "normal"},
    4: {"name": "preset4_swe_gpu", "config": str(REPO_ROOT / "data" / "configs" / "kpi_presets" / "Llama3.1_NativeOpenVINO_GPU_SWEAgent.json"), "download_behaviour": "normal"},
    5: {
        "name": "preset5_sweagent_npu",
        "config": str(REPO_ROOT / "data" / "configs" / "vendors_default" / "agentic" / "Llama3.1" / "Intel_NativeOpenVINO_NPU.json"),
        "download_behaviour": "normal",
        "default_extra_args": ["--python-path", "system"],
    },
    6: {
        "name": "preset6_sweagent_gpu",
        "config": str(REPO_ROOT / "data" / "configs" / "vendors_default" / "agentic" / "Llama3.1" / "Intel_NativeOpenVINO_GPU.json"),
        "download_behaviour": "normal",
        "default_extra_args": ["--python-path", "system"],
    },
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", type=int, required=True, choices=sorted(PRESETS))
    ap.add_argument("--mlperf-dir", default=DEFAULT_MLPERF_DIR, help="Override the mlperf install dir (default: mlperf 2.0 install)")
    ap.add_argument("--output-root", default=None)
    # parse_known_args (not a REMAINDER positional) so passthrough flags work regardless of position.
    args, extra_args = ap.parse_known_args()
    args.extra_args = extra_args

    preset = PRESETS[args.preset]
    cmd = [
        sys.executable, str(RUN_KPI_WORKFLOW),
        "--mlperf-dir", args.mlperf_dir,
        "--config", preset["config"],
        "--name", preset["name"],
    ]
    if args.output_root:
        cmd += ["--output-root", args.output_root]
    if "download_behaviour" in preset:
        cmd += ["--download-behaviour", preset["download_behaviour"]]
    cmd += preset.get("default_extra_args", [])
    cmd += args.extra_args

    print(f"Preset {args.preset}: {preset['name']}")
    print("Command:", " ".join(cmd))
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()

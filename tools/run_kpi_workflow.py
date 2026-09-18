#!/usr/bin/env python3
"""Run an MLPerf Client benchmark scenario while collecting KPI-hub telemetry.

Wraps the mlperf-windows.exe run with:
  - HW telemetry sampling (tools/KPI-hub/sample_utilization_fast.py) -> hw_samples.csv
  - Per-prompt workflow KPIs parsed from the scenario's executor log -> workflow_kpi.json
  - experiment.json (exit code, duration, model weight size)

Then generates the post-run reports:
  - dashboard.html   (tools/KPI-hub/plot_utilization_interactive.py)
  - kpi_report.html  (tools/KPI-hub/generate_kpi_report.py)

Usage (from repo root, using the project .venv):
    .venv\\Scripts\\python.exe tools\\run_kpi_workflow.py ^
        --mlperf-dir "C:\\Applications\\mlperf_client\\mlperf_v2p0" ^
        --config llm\\Llama3.1\\Intel_NativeOpenVINO_NPU_Default.json ^
        --name llama3_npu
"""
import argparse
import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

KPI_HUB_DIR = Path(__file__).resolve().parent / "KPI-hub"

LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}\.\d{3})\]\s+\S+\s+"
    r"(?P<level>INFO|DEBUG|WARN|ERROR)\s*-\s*(?P<msg>.*)$"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "prompt"


# Ordered (first match wins) keyword -> category label, used when the executor log has no
# "Category:" line (e.g. the agentic SWE Agent / Data Agent scenarios), based on distinctive
# phrases from the actual prompt bodies in data/prompts/llama_3_1_8b_instruct/.
_PROMPT_CATEGORY_HINTS = [
    ("Warmup.", "warmup"),
    ("SWE-Agent", "swe_agent"),
    ("Software Engineer Agent", "swe_agent"),
    ("DATA ANALYST", "data_agent"),
    ("Data Analyst Agent", "data_agent"),
]


def _classify_prompt(text: str) -> str:
    for hint, label in _PROMPT_CATEGORY_HINTS:
        if hint in text:
            return label
    return "unknown"


def _parse_ts(ts: str) -> float:
    return datetime.strptime(ts, "%m-%d-%Y %H:%M:%S.%f").timestamp()


def parse_executor_log(path: Path, start_offset: int) -> dict:
    """Extract one KPI stage per inference (power_begin..Tokens Per Second block).

    Consecutive prompts (Delay=0) are logged in a pipelined order: power_end / "Ran inference" /
    "TTFT" for the CURRENT stage are printed, then the NEXT stage's power_begin fires, and only
    then is the current stage's trailing "Input tokens:"/"Tokens Per Second:" block printed. So
    two stages can be "in flight" at once: `open_stage` (accumulating start_epoch, then end_epoch/
    output_tokens/ttft_s as they arrive) and `closing_stage` (already has those, just waiting on
    its trailing input/throughput stats before being emitted).
    """
    stages = {}
    category = "unknown"
    open_stage = None
    closing_stage = None
    idx = 0
    capturing_prompt = False
    prompt_buf = []

    def _emit(stage):
        nonlocal idx
        idx += 1
        name = f"{idx:02d}_{_slug(stage.get('category', 'unknown'))}"
        stages[name] = {
            "input_tokens": stage.get("input_tokens", 0),
            "output_tokens": stage.get("output_tokens", 0),
            "total_tokens": stage.get("input_tokens", 0) + stage.get("output_tokens", 0),
            "wall_time_s": round(stage["end_epoch"] - stage["start_epoch"], 3),
            "output_tokens_per_s": stage.get("output_tokens_per_s", 0),
            "ttft_s": stage.get("ttft_s", 0),
            "start_epoch": stage["start_epoch"],
            "end_epoch": stage["end_epoch"],
            "start_iso": datetime.fromtimestamp(stage["start_epoch"]).isoformat(timespec="milliseconds"),
            "end_iso": datetime.fromtimestamp(stage["end_epoch"]).isoformat(timespec="milliseconds"),
        }

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        for line in f:
            m = LOG_LINE_RE.match(line)
            if not m:
                if capturing_prompt:
                    prompt_buf.append(line)
                continue
            ts = _parse_ts(m.group("ts"))
            msg = m.group("msg")
            if capturing_prompt:
                # Any bracketed log line ends the (possibly multi-line, unbracketed) prompt body.
                # Later turns in a multi-turn agentic conversation may not repeat the system
                # prompt's distinguishing text, so keep the last successfully detected category.
                detected = _classify_prompt("".join(prompt_buf))
                if detected != "unknown":
                    category = detected
                capturing_prompt = False
                prompt_buf = []
            if msg.startswith("Category:"):
                category = msg.split(":", 1)[1].strip()
            elif msg.startswith("User prompt:"):
                capturing_prompt = True
                prompt_buf = [msg.split("User prompt:", 1)[1]]
            elif msg == "power_begin":
                if open_stage is not None and "end_epoch" in open_stage and closing_stage is None:
                    closing_stage = open_stage
                open_stage = {"start_epoch": ts, "category": category}
            elif msg == "power_end":
                if open_stage is not None:
                    open_stage["end_epoch"] = ts
            elif msg.startswith("Ran inference and got"):
                target = open_stage if open_stage is not None else closing_stage
                if target is not None:
                    mm = re.search(r"got (\d+) tokens", msg)
                    if mm:
                        target["output_tokens"] = int(mm.group(1))
                    target.setdefault("end_epoch", ts)
            elif msg.startswith("TTFT"):
                target = open_stage if open_stage is not None else closing_stage
                if target is not None:
                    mm = re.search(r"TTFT ([\d.]+)ms", msg)
                    if mm:
                        target["ttft_s"] = round(float(mm.group(1)) / 1000, 4)
            elif msg.startswith("Input tokens:"):
                target = closing_stage if closing_stage is not None else open_stage
                if target is not None:
                    mm = re.search(r"Input tokens:\s*(\d+)", msg)
                    if mm:
                        target["input_tokens"] = int(mm.group(1))
            elif msg.startswith("Tokens Per Second:"):
                target = closing_stage if closing_stage is not None else open_stage
                if target is not None:
                    mm = re.search(r"\(tokens/sec\)\s*([\d.]+)", msg)
                    if mm:
                        target["output_tokens_per_s"] = float(mm.group(1))
                    # Last field of the per-inference block: emit the stage now.
                    if "start_epoch" in target and "end_epoch" in target:
                        _emit(target)
                    if target is closing_stage:
                        closing_stage = None
                    else:
                        open_stage = None
    return stages


def build_workflow_kpi(name: str, stages: dict, run_start: float, run_end: float, model: str, backend: str) -> dict:
    out_tok = sum(s["output_tokens"] for s in stages.values())
    in_tok = sum(s["input_tokens"] for s in stages.values())
    tot_tok = sum(s["total_tokens"] for s in stages.values())
    wall = run_end - run_start
    totals = {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": tot_tok}
    if out_tok and wall:
        totals["output_tokens_per_s"] = round(out_tok / wall, 2)
    return {
        "model": model,
        "backend": backend,
        "stages": stages,
        "totals": totals,
        "workflow_wall_time_s": round(wall, 2),
        "workflow_start_iso": datetime.fromtimestamp(run_start).isoformat(timespec="milliseconds"),
        "workflow_end_iso": datetime.fromtimestamp(run_end).isoformat(timespec="milliseconds"),
        "timeline": {"workflow_name": name, "start_epoch": run_start, "end_epoch": run_end, "phases": []},
    }


def resolve_model(mlperf_dir: Path, config_path: Path):
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    try:
        model = cfg["Scenarios"][0]["Models"][0]
        ep = cfg["Scenarios"][0]["ExecutionProviders"][0]
        ep_name = ep["Name"]
        device_type = ep.get("Config", {}).get("device_type", "")
    except (KeyError, IndexError):
        return "", "", None, ""
    file_path = model.get("FilePath", "")
    model_dir = None
    if file_path.startswith("file://"):
        rel = file_path.replace("file://", "").lstrip("./")
        model_dir = (mlperf_dir / rel).resolve().parent
    elif file_path.startswith(("http://", "https://")):
        # Remotely-fetched model: actual local cache path is an opaque hash directory
        # managed by mlperf-windows.exe, not derivable from the config alone.
        model_dir = None
    return model.get("ModelName", ""), ep_name, model_dir, device_type, file_path


def build_experiment_meta(model_name: str, ep_name: str, device_type: str, model_dir, exit_code: int, duration_s: float, model_source: str = "") -> dict:
    model_weight_mb = 0
    if model_dir and model_dir.exists():
        model_weight_mb = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) // (1024 * 1024)
    return {
        "backend": ep_name,
        "device_type": device_type,
        "model": model_name,
        "kpi_mode": "full",
        "workflow_exit_code": exit_code,
        "workflow_duration_s": round(duration_s, 2),
        "model_weight_mb": model_weight_mb,
        "model_weight_path": str(model_dir) if model_dir else model_source,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mlperf-dir", required=True, help="Directory containing mlperf-windows.exe (used as cwd)")
    ap.add_argument("--mlperf-exe", default="mlperf-windows.exe")
    ap.add_argument("--config", required=True, help="Config path, relative to --mlperf-dir or absolute")
    ap.add_argument("--executor-log", default=None, help="Executor log filename under <mlperf-dir>/Logs (auto-detected from the scenario name if omitted)")
    ap.add_argument("--name", default="mlperf_run", help="Experiment name used in the output directory")
    ap.add_argument("--output-root", default=str(Path(__file__).resolve().parent.parent / "kpi_runs"))
    ap.add_argument("--hw-profile", default="lightweight", choices=["lightweight", "simulation", "memory_deep", "power_analysis", "unified"])
    ap.add_argument("--hw-interval-ms", type=int, default=1000)
    ap.add_argument("--sampler-warmup-s", type=float, default=3.0, help="Time to let the HW sampler discover hardware before starting the workload")
    ap.add_argument("--download-behaviour", default="skip_all", choices=["forced", "prompt", "skip_all", "deps_only", "normal"], help="Passed to mlperf-windows.exe -b flag (agentic scenarios need 'normal'/'forced' to fetch remote assets on first run)")
    # parse_known_args (not a REMAINDER positional) so passthrough flags like --python-path work regardless of position.
    args, extra_args = ap.parse_known_args()
    args.extra_args = extra_args

    python_exe = sys.executable
    mlperf_dir = Path(args.mlperf_dir).resolve()
    mlperf_exe = mlperf_dir / args.mlperf_exe
    config_path = Path(args.config)
    config_abs = config_path if config_path.is_absolute() else (mlperf_dir / config_path)

    model_name, ep_name, model_dir, device_type, model_source = resolve_model(mlperf_dir, config_abs)
    scenario_name = json.loads(config_abs.read_text(encoding="utf-8-sig"))["Scenarios"][0]["Name"]
    executor_log_name = args.executor_log or f"{_slug(scenario_name)}_executor.log"
    executor_log_path = mlperf_dir / "Logs" / executor_log_name

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_root) / f"{args.name}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    # 1. Start HW telemetry sampler in the background.
    hw_csv = out_dir / "hw_samples.csv"
    sampler_log = open(out_dir / "hw_sampler.log", "w", encoding="utf-8")
    sampler_cmd = [
        python_exe, str(KPI_HUB_DIR / "sample_utilization_fast.py"),
        "--profile", args.hw_profile, "--interval", str(args.hw_interval_ms), "--output", str(hw_csv),
        "--power",
    ]
    sampler_proc = subprocess.Popen(
        sampler_cmd, stdout=sampler_log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    time.sleep(args.sampler_warmup_s)

    # 2. Run the benchmark, only capturing executor-log lines appended during this run.
    start_offset = executor_log_path.stat().st_size if executor_log_path.exists() else 0
    exe_cmd = [str(mlperf_exe), "-c", str(config_path), "-p", "false", "-n", "false", "-b", args.download_behaviour] + args.extra_args
    print(f"Running: {' '.join(exe_cmd)} (cwd={mlperf_dir})")
    run_start_epoch = time.time()
    proc = subprocess.run(exe_cmd, cwd=str(mlperf_dir), capture_output=True, text=True)
    run_end_epoch = time.time()
    (out_dir / "mlperf_stdout.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    print(f"mlperf-windows.exe exit code: {proc.returncode}")

    # 3. Stop the sampler gracefully (CTRL_BREAK triggers its KeyboardInterrupt cleanup path).
    try:
        sampler_proc.send_signal(signal.CTRL_BREAK_EVENT)
        sampler_proc.wait(timeout=15)
    except Exception:
        sampler_proc.terminate()
    sampler_log.close()

    # 4. Build workflow_kpi.json + experiment.json from the executor log.
    stages = parse_executor_log(executor_log_path, start_offset) if executor_log_path.exists() else {}
    print(f"Parsed {len(stages)} inference stage(s) from {executor_log_path.name}")
    workflow_kpi = build_workflow_kpi(args.name, stages, run_start_epoch, run_end_epoch, model_name, ep_name)
    (out_dir / "workflow_kpi.json").write_text(json.dumps(workflow_kpi, indent=2), encoding="utf-8")

    experiment = build_experiment_meta(model_name, ep_name, device_type, model_dir, proc.returncode, run_end_epoch - run_start_epoch, model_source)
    (out_dir / "experiment.json").write_text(json.dumps(experiment, indent=2), encoding="utf-8")

    # 5. Generate the post-run HTML reports.
    subprocess.run([
        python_exe, str(KPI_HUB_DIR / "plot_utilization_interactive.py"),
        "--input", str(hw_csv), "--output", str(out_dir / "dashboard.html"),
        "--phases", str(out_dir / "workflow_kpi.json"),
    ], check=False)

    subprocess.run([
        python_exe, str(KPI_HUB_DIR / "generate_kpi_report.py"),
        "--kpi-dir", str(out_dir), "--exp-dir", str(out_dir), "--output", str(out_dir / "kpi_report.html"),
    ], check=False)

    print(f"kpi_report.html : {out_dir / 'kpi_report.html'}")
    print(f"dashboard.html  : {out_dir / 'dashboard.html'}")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()

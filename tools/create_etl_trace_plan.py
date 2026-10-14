#!/usr/bin/env python3
"""Create a stage-selective WPR/GPUView trace plan from a completed workflow KPI file."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GPUVIEW_PROFILE = (
    r"C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit"
    r"\gpuview\log.wprp!GPUView"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-kpi", required=True, help="Completed workflow_kpi.json from a baseline run")
    parser.add_argument("--output", required=True, help="Destination ETL trace-plan JSON")
    parser.add_argument("--capture-stage", action="append", default=[], metavar="STAGE",
                        help="Stage name to trace; repeat this option. Unselected stages default to capture=false.")
    parser.add_argument("--profile", default=DEFAULT_GPUVIEW_PROFILE,
                        help="WPR profile path!ProfileName (default: installed GPUView profile)")
    parser.add_argument("--max-capture-seconds", type=float, default=20.0,
                        help="Maximum ETL duration per selected stage (default: 20s)")
    parser.add_argument("--no-ptat", action="store_true",
                        help="Do not start PTAT monitor logging during the replay")
    args = parser.parse_args()

    if args.max_capture_seconds <= 0:
        parser.error("--max-capture-seconds must be positive")

    source = Path(args.workflow_kpi)
    data = json.loads(source.read_text(encoding="utf-8"))
    stages = data.get("stages")
    if not isinstance(stages, dict) or not stages:
        parser.error("workflow KPI must contain a non-empty stages object")

    selected_names = set(args.capture_stage)
    unknown = sorted(selected_names - set(stages))
    if unknown:
        parser.error("unknown stage name(s): " + ", ".join(unknown))
    if not selected_names:
        parser.error("select at least one stage with --capture-stage")

    workflow_start = data.get("workflow_start_iso")
    if workflow_start:
        workflow_start_epoch = datetime.fromisoformat(workflow_start).timestamp()
    else:
        workflow_start_epoch = min(
            float(stage["start_epoch"])
            for stage in stages.values()
            if "start_epoch" in stage
        )

    plan_stages = []
    for ordinal, (name, stage) in enumerate(stages.items(), start=1):
        start_epoch = stage.get("start_epoch")
        end_epoch = stage.get("end_epoch")
        plan_stages.append({
            "name": name,
            "ordinal": ordinal,
            "capture": name in selected_names,
            "baseline_offset_s": round(float(start_epoch) - workflow_start_epoch, 3) if start_epoch is not None else None,
            "baseline_duration_s": round(float(end_epoch) - float(start_epoch), 3)
            if start_epoch is not None and end_epoch is not None else None,
        })

    plan = {
        "schema_version": 1,
        "workflow_name": data.get("timeline", {}).get("workflow_name", "workflow"),
        "baseline_workflow_kpi": str(source.resolve()),
        "baseline_workflow_start_iso": workflow_start,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wpr_profile": args.profile,
        "max_capture_seconds_per_stage": args.max_capture_seconds,
        "circular_buffer": True,
        "ptat_enabled": not args.no_ptat,
        "marker_source": "wpr -marker",
        "stage_trigger_source": "live executor log power_begin/power_end events on replay",
        "stages": plan_stages,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    selected = [stage["name"] for stage in plan_stages if stage["capture"]]
    print(f"Created {output} with {len(plan_stages)} stages; selected: {', '.join(selected)}")
    print("Baseline offsets are reference metadata only; captures are triggered by live stage log events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
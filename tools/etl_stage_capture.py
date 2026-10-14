#!/usr/bin/env python3
"""Trigger bounded WPR/GPUView captures on live MLPerf executor stage boundaries."""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}\.\d{3})\]\s+\S+\s+"
    r"(?P<level>INFO|DEBUG|WARN|ERROR)\s*-\s*(?P<msg>.*)$"
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "stage"


def export_ptat_capture_csvs(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ptat_csv = manifest.get("ptat_monitor", {}).get("copied_csv")
    if not ptat_csv or not Path(ptat_csv).is_file():
        return manifest

    with Path(ptat_csv).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames
        samples = []
        for row in reader:
            try:
                timestamp = datetime.strptime(
                    f"{row['Date']} {row['Time']}", "%d/%m/%Y %H:%M:%S:%f",
                ).astimezone()
            except (KeyError, ValueError):
                continue
            samples.append((timestamp, row))

    for capture in manifest.get("captures", []):
        if capture.get("status") != "saved" or not capture.get("stop_requested_utc"):
            continue
        start = datetime.fromisoformat(capture["started_utc"])
        end = datetime.fromisoformat(capture["stop_requested_utc"])
        rows = [row for timestamp, row in samples if start <= timestamp <= end]
        capture["ptat_sample_count"] = len(rows)
        if not rows:
            capture["ptat_status"] = "no_samples_in_capture_window"
            continue
        output_path = manifest_path.parent / "etw" / f"{Path(capture['etl_path']).stem}.ptat.csv"
        with output_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        capture["ptat_csv_path"] = str(output_path)

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


class ETWStageCaptureController:
    def __init__(self, plan_path: Path, executor_log: Path, start_offset: int, output_dir: Path):
        self.plan_path = Path(plan_path)
        self.executor_log = Path(executor_log)
        self.start_offset = start_offset
        self.output_dir = Path(output_dir)
        self.plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        if self.plan.get("schema_version") != 1:
            raise ValueError("Unsupported ETL trace-plan schema_version")
        self.profile = self.plan["wpr_profile"]
        self.max_capture_s = float(self.plan.get("max_capture_seconds_per_stage", 20.0))
        self.stages = {int(s["ordinal"]): s for s in self.plan.get("stages", [])}
        self.selected_count = sum(bool(s.get("capture")) for s in self.stages.values())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._ordinal = 0
        self._current_stage = None
        self._active_capture = None
        self._captures = []
        self._status = "disabled" if self.selected_count == 0 else "armed"
        self._wpr = shutil.which("wpr.exe") or r"C:\Windows\System32\wpr.exe"
        self._ptat_exe = Path(self.plan.get(
            "ptat_exe", r"C:\Program Files\Intel Corporation\Intel(R)PTAT\PTAT.exe",
        ))
        self._ptat_log_dir = Path(self.plan.get(
            "ptat_log_dir", str(Path.home() / "Documents" / "iPTAT" / "log"),
        ))
        self._ptat_enabled = bool(self.plan.get("ptat_enabled", True))
        self._ptat_started_by_controller = False
        self._ptat_started_local = None
        self._ptat_start_process = None
        self._ptat_manifest = {"enabled": self._ptat_enabled, "status": "disabled"}

    def start(self) -> None:
        if self.selected_count == 0:
            print("ETW trace plan has no selected stages; capture disabled")
            return
        if self._ptat_enabled:
            self._start_ptat()
        if not Path(self._wpr).exists():
            self._status = "unavailable"
            self._captures.append({"error": f"wpr.exe not found: {self._wpr}"})
            return
        profile_path = self.profile.split("!", 1)[0]
        if "!" in self.profile and not Path(profile_path).exists():
            self._status = "unavailable"
            self._captures.append({"error": f"WPR profile not found: {profile_path}"})
            return
        self._status = "armed"
        self._thread = threading.Thread(target=self._tail_executor_log, daemon=True, name="ETWStageCapture")
        self._thread.start()
        print(f"ETW trace plan armed for {self.selected_count} selected stage(s), profile={self.profile}")

    def _ptat_is_running(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception as exc:
            return False, f"tasklist probe failed: {exc}"
        output = result.stdout.lower()
        return ("ptat.exe" in output or "ptatservice.exe" in output), output

    def _start_ptat(self) -> None:
        self._ptat_started_local = datetime.now().astimezone()
        self._ptat_manifest.update({
            "exe": str(self._ptat_exe),
            "log_dir": str(self._ptat_log_dir),
            "start_local": self._ptat_started_local.isoformat(timespec="milliseconds"),
        })
        if not self._ptat_exe.exists():
            self._ptat_manifest.update({"status": "unavailable", "error": "PTAT.exe not found"})
            print("[PTAT] not installed; PDH RAPL/EMON fallback remains available")
            return
        already_running, process_output = self._ptat_is_running()
        if already_running:
            self._ptat_manifest.update({"status": "already_running", "started_by_controller": False})
            print("[PTAT] already running; leaving the existing session untouched")
            return

        try:
            self._ptat_start_process = subprocess.Popen(
                [str(self._ptat_exe), "-start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._ptat_manifest.update({"status": "start_failed", "started_by_controller": False,
                                        "error": str(exc)})
            print(f"[PTAT] monitor launch failed; continuing with PDH RAPL/EMON fallback: {exc}")
            return

        self._ptat_started_by_controller = True
        ready_deadline = time.monotonic() + 20.0
        while time.monotonic() < ready_deadline:
            if self._ptat_is_running()[0]:
                self._ptat_manifest.update({"status": "started", "started_by_controller": True})
                print("[PTAT] monitor started for workflow duration")
                return
            if self._ptat_start_process.poll() is not None:
                break
            time.sleep(0.25)

        already_running, probe = self._ptat_is_running()
        if already_running:
            self._ptat_manifest.update({"status": "started", "started_by_controller": True})
            print("[PTAT] monitor process detected for workflow duration")
        else:
            self._ptat_manifest.update({
                "status": "start_unconfirmed",
                "started_by_controller": True,
                "start_process_exit_code": self._ptat_start_process.poll(),
                "process_probe": probe[-2000:],
            })
            print("[PTAT] monitor start unconfirmed; continuing with PDH RAPL/EMON fallback")

    def _stop_ptat(self) -> dict:
        if not self._ptat_enabled:
            return self._ptat_manifest
        if self._ptat_started_by_controller:
            try:
                stop_process = subprocess.Popen(
                    [str(self._ptat_exe), "-stop"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    stop_process.wait(timeout=30.0)
                    self._ptat_manifest["stop_status"] = "stopped"
                except subprocess.TimeoutExpired:
                    stop_process.kill()
                    self._ptat_manifest["stop_status"] = "stop_command_timeout"
            except Exception as exc:
                self._ptat_manifest["stop_status"] = "stop_failed"
                self._ptat_manifest["stop_error"] = str(exc)
            self._ptat_started_by_controller = False
        if self._ptat_log_dir.exists() and self._ptat_started_local is not None:
            candidates = []
            cutoff = self._ptat_started_local.timestamp() - 5.0
            for path in self._ptat_log_dir.glob("PTATMonitor*.csv"):
                try:
                    stat = path.stat()
                    if stat.st_mtime >= cutoff and stat.st_size > 0:
                        candidates.append(path)
                except OSError:
                    continue
            if candidates:
                latest = max(candidates, key=lambda path: path.stat().st_mtime)
                output_path = self.output_dir / "ptat_power.csv"
                try:
                    shutil.copy2(latest, output_path)
                    self._ptat_manifest["source_csv"] = str(latest)
                    self._ptat_manifest["copied_csv"] = str(output_path)
                    self._ptat_manifest["status"] = "captured"
                    print(f"[PTAT] copied monitor log to {output_path}")
                except OSError as exc:
                    self._ptat_manifest["copy_error"] = str(exc)
            elif self._ptat_manifest.get("status") not in ("start_failed", "unavailable"):
                self._ptat_manifest["status"] = "no_fresh_log"
                print("[PTAT] no fresh monitor CSV; PDH RAPL/EMON fallback remains available")
        return self._ptat_manifest

    def _run(self, command: list[str], timeout: float = 30.0) -> tuple[int, str]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            return result.returncode, (result.stdout + result.stderr).strip()
        except Exception as exc:
            return 1, str(exc)

    def _marker(self, text: str) -> str | None:
        code, output = self._run([self._wpr, "-marker", text], timeout=10.0)
        return None if code == 0 else f"WPR marker failed ({code}): {output}"

    def _begin_capture(self, stage: dict, log_timestamp: str) -> None:
        etl_dir = self.output_dir / "etw"
        etl_dir.mkdir(parents=True, exist_ok=True)
        etl_path = etl_dir / f"stage_{int(stage['ordinal']):02d}_{_slug(stage['name'])}.etl"
        record = {
            "stage_name": stage["name"],
            "stage_ordinal": int(stage["ordinal"]),
            "baseline_offset_s": stage.get("baseline_offset_s"),
            "baseline_duration_s": stage.get("baseline_duration_s"),
            "max_capture_seconds": self.max_capture_s,
            "etl_path": str(etl_path),
            "trigger_log_timestamp": log_timestamp,
            "started_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        code, output = self._run([self._wpr, "-start", self.profile])
        if code != 0:
            record["start_error"] = output
            record["status"] = "start_failed"
            self._captures.append(record)
            print(f"[ETW] failed to start for {stage['name']}: {output}")
            return

        record["status"] = "started"
        marker_error = self._marker(f"KPIHUB_ETW_BEGIN stage={stage['name']} ordinal={stage['ordinal']}")
        if marker_error:
            record["begin_marker_warning"] = marker_error
        self._active_capture = {
            "record": record,
            "etl_path": etl_path,
            "monotonic_start": time.monotonic(),
        }
        print(f"[ETW] started capture for {stage['name']}")

    def _end_capture(self, reason: str, log_timestamp: str | None = None) -> None:
        capture = self._active_capture
        if capture is None:
            return
        record = capture["record"]
        marker_error = self._marker(
            f"KPIHUB_ETW_END stage={record['stage_name']} ordinal={record['stage_ordinal']} reason={_slug(reason)}"
        )
        if marker_error:
            record["end_marker_warning"] = marker_error
        stop_requested_monotonic = time.monotonic()
        record["stop_requested_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        record["capture_active_seconds"] = round(
            stop_requested_monotonic - capture["monotonic_start"], 3,
        )
        code, output = self._run([self._wpr, "-stop", str(capture["etl_path"])])
        stop_completed_monotonic = time.monotonic()
        record["stop_completed_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        record["end_log_timestamp"] = log_timestamp
        record["captured_seconds"] = record["capture_active_seconds"]
        record["stop_flush_seconds"] = round(stop_completed_monotonic - stop_requested_monotonic, 3)
        record["stop_reason"] = reason
        if code == 0:
            record["status"] = "saved"
        else:
            record["status"] = "stop_failed"
            record["stop_error"] = output
        self._captures.append(record)
        self._active_capture = None
        print(f"[ETW] {record['status']} {capture['etl_path']}")

    def _handle_log_line(self, line: str) -> None:
        match = LOG_LINE_RE.match(line.rstrip())
        if not match:
            return
        message = match.group("msg")
        timestamp = match.group("ts")
        if message == "power_begin":
            if self._active_capture is not None:
                self._end_capture("next_stage_begin", timestamp)
            self._ordinal += 1
            stage = self.stages.get(self._ordinal)
            self._current_stage = stage
            if stage and stage.get("capture"):
                self._begin_capture(stage, timestamp)
        elif message == "power_end":
            if self._current_stage and self._current_stage.get("capture"):
                self._end_capture("stage_end", timestamp)
            self._current_stage = None

    def _tail_executor_log(self) -> None:
        while not self._stop.is_set() and not self.executor_log.exists():
            self._stop.wait(0.1)
        if self._stop.is_set():
            return
        try:
            with self.executor_log.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.start_offset)
                while not self._stop.is_set():
                    line = f.readline()
                    if line:
                        if (self._active_capture
                                and time.monotonic() - self._active_capture["monotonic_start"] >= self.max_capture_s):
                            self._end_capture("max_capture_seconds")
                        self._handle_log_line(line)
                    else:
                        if self._active_capture and time.monotonic() - self._active_capture["monotonic_start"] >= self.max_capture_s:
                            self._end_capture("max_capture_seconds")
                        self._stop.wait(0.05)
        except Exception as exc:
            self._status = "monitor_error"
            self._captures.append({"error": f"executor-log watcher failed: {exc}"})

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._active_capture:
            self._end_capture("workflow_end")
        ptat_manifest = self._stop_ptat()
        if self._status == "armed":
            self._status = "completed"
        manifest = {
            "schema_version": 1,
            "status": self._status,
            "profile": self.profile,
            "trigger_source": "live executor log power_begin/power_end events",
            "max_capture_seconds_per_stage": self.max_capture_s,
            "ptat_monitor": ptat_manifest,
            "selected_stages": [s["name"] for s in self.stages.values() if s.get("capture")],
            "captures": self._captures,
            "note": "Baseline offsets are references only; ETL windows are triggered by live stage log events.",
        }
        manifest_path = self.output_dir / "etl_trace_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return export_ptat_capture_csvs(manifest_path)
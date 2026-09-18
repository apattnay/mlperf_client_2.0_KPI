"""
KPI Writer — Structured output for workflow metrics.

Merges PhaseTracker timeline data with token/throughput KPIs into a single
JSON file that the dashboard can consume.

Output format is backward-compatible with existing workflow_kpi.json
(the dashboard's load_phases() still works) while adding rich sub-phase data.
"""

import json
import os
import time
from datetime import datetime
from typing import Optional

from .phase_tracker import PhaseTracker


class KPIWriter:
    """Writes structured KPI JSON combining phase timeline + token metrics.

    Usage:
        writer = KPIWriter(tracker, model="Qwen3-4B", backend="ovms")
        writer.record_stage("analysis_agent", response, wall_time)
        writer.save("outputs/workflow_kpi.json")

    Output JSON structure (backward-compatible + enhanced):
    {
        "model": "...",
        "backend": "...",
        "stages": {
            "analysis_agent": {
                "input_tokens": ...,
                "output_tokens": ...,
                "wall_time_s": ...,
                "output_tokens_per_s": ...,
                "start_iso": "...",
                "end_iso": "...",
                "start_epoch": ...,
                "end_epoch": ...,
                "sub_phases": [...]   // NEW: nested sub-phase timeline
            }
        },
        "totals": {...},
        "workflow_wall_time_s": ...,
        "timeline": {...}   // NEW: full PhaseTracker hierarchy
    }
    """

    def __init__(self, tracker: PhaseTracker, model: str = "", backend: str = ""):
        self.tracker = tracker
        self.model = model
        self.backend = backend
        self._stages: dict = {}
        self._totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def record_stage(self, name: str, response=None, wall_time_s: float = 0.0,
                     t_start: float = 0.0, t_end: float = 0.0, **extra):
        """Record a stage's KPI metrics (backward-compatible with _record_kpi).

        Args:
            name: Stage identifier (e.g., "analysis_agent")
            response: Agent response with .usage_details
            wall_time_s: Wall clock duration
            t_start: Unix timestamp of stage start
            t_end: Unix timestamp of stage end
            **extra: Additional metadata
        """
        # Extract token usage
        usage = {}
        if response and hasattr(response, "usage_details"):
            ud = response.usage_details or {}
            usage = {
                "input_tokens": ud.get("input_token_count", 0),
                "output_tokens": ud.get("output_token_count", 0),
                "total_tokens": ud.get("total_token_count", 0),
            }

        # Accumulate
        if name not in self._stages:
            self._stages[name] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "wall_time_s": 0.0,
                "calls": 0,
            }

        stage = self._stages[name]
        stage["input_tokens"] += usage.get("input_tokens", 0)
        stage["output_tokens"] += usage.get("output_tokens", 0)
        stage["total_tokens"] += usage.get("total_tokens", 0)
        stage["wall_time_s"] += wall_time_s
        stage["calls"] += 1

        if stage["output_tokens"] > 0 and stage["wall_time_s"] > 0:
            stage["output_tokens_per_s"] = round(
                stage["output_tokens"] / stage["wall_time_s"], 1
            )

        # Timeline correlation
        if t_start:
            stage["start_epoch"] = t_start
            stage["start_iso"] = datetime.fromtimestamp(t_start).isoformat(timespec="milliseconds")
        if t_end:
            stage["end_epoch"] = t_end
            stage["end_iso"] = datetime.fromtimestamp(t_end).isoformat(timespec="milliseconds")
        elif t_start and wall_time_s:
            stage["end_epoch"] = t_start + wall_time_s
            stage["end_iso"] = datetime.fromtimestamp(t_start + wall_time_s).isoformat(timespec="milliseconds")

        # Extra metadata
        stage.update(extra)

        # Update totals
        self._totals["input_tokens"] += usage.get("input_tokens", 0)
        self._totals["output_tokens"] += usage.get("output_tokens", 0)
        self._totals["total_tokens"] += usage.get("total_tokens", 0)

    def _enrich_stages_with_subphases(self):
        """Match PhaseTracker phases to stages and attach sub-phase data."""
        for phase in self.tracker.phases:
            stage_name = phase.name
            if stage_name in self._stages and phase.children:
                self._stages[stage_name]["sub_phases"] = [
                    c.to_dict() for c in phase.children
                ]

    def to_dict(self) -> dict:
        """Generate the full KPI dict."""
        self._enrich_stages_with_subphases()

        return {
            "model": self.model,
            "backend": self.backend,
            "stages": self._stages,
            "totals": self._totals,
            "workflow_wall_time_s": self.tracker.workflow_duration_s,
            "workflow_start_iso": datetime.fromtimestamp(self.tracker.start_epoch).isoformat(timespec="milliseconds") if self.tracker.start_epoch else "",
            "workflow_end_iso": datetime.fromtimestamp(self.tracker.end_epoch).isoformat(timespec="milliseconds") if self.tracker.end_epoch else "",
            "timeline": self.tracker.to_dict(),
        }

    def save(self, path: str):
        """Write KPI JSON to disk."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = self.to_dict()
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

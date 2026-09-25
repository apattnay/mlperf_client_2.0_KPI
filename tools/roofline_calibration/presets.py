"""Metadata for the 4 roofline-calibration presets - each isolates exactly ONE of the 4 macro-
component scaling equations in scaling_engine.py, so its measured behavior can be cross-checked
against that equation's assumption independent of the noisy, workload-mixed agentic timeline.

See docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md Sec 4.3 for the full design rationale, the real
2026-09-25 NPU dry-run results, and the bugs found+fixed to get presets 3/4 working (odd-prompt-
count requirement, --python-path system, "execute" vs "execute_command" tool dispatch, and the
real tools_sandbox asset staging path).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationPreset:
    key: str
    equation: str            # which scaling_engine.py macro-component/knob this isolates
    is_agentic: bool
    description: str
    confidence: str          # "high" (non-agentic, proven config pattern) or "validated" (agentic, confirmed working on a real 2026-09-25 NPU dry-run after fixing --python-path/odd-prompt-count/tool-name/asset-path issues - see docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md §4.3)


CALIBRATION_PRESETS = {
    "prefill_sweep": CalibrationPreset(
        key="prefill_sweep",
        equation="compute_efficiency_retention (prefill_s, compute-bound)",
        is_agentic=False,
        description=(
            "Fixed short output (max_length=32), sweeping input context length "
            "(128/512/2048/8192 tokens) across 4 stages - isolates the prefill/compute scaling "
            "curve from a real, controlled, non-agentic shape instead of one noisy agentic point."
        ),
        confidence="high",
    ),
    "thin_serving": CalibrationPreset(
        key="thin_serving",
        equation="stage_overhead_s / fixed_overhead_s (assumed constant, non-resource-bound)",
        is_agentic=False,
        description=(
            "Minimal 1-token-ish round trips repeated many times (Iterations) - isolates the "
            "fixed per-request software/IPC overhead this model treats as a constant, from real "
            "compute/memory work (which is near-zero here by design)."
        ),
        confidence="high",
    ),
    "kv_cache_growth": CalibrationPreset(
        key="kv_cache_growth",
        equation="memory_efficiency_retention (decode_s/ITL scaling with KV-cache depth)",
        is_agentic=True,
        description=(
            "Agentic multi-turn conversation (prompt_files scripted 'agent' replies, matching "
            "swe-agent-prompts.json's own schema, including always ending on a trailing 'agent' "
            "entry - an odd total prompt count, required by the harness) where each scripted "
            "agent turn adds a KNOWN, roughly-equal token increment to history - isolates how ITL "
            "changes as KV-cache depth grows in controlled steps, instead of the agentic "
            "workload's own uneven growth."
        ),
        confidence="validated",
    ),
    "tool_exec_only": CalibrationPreset(
        key="tool_exec_only",
        equation="cpu_efficiency_retention + tool_parallel_fraction (tool_exec_gap_s, Amdahl's law)",
        is_agentic=True,
        description=(
            "Agentic scenario prompting the model to invoke tools_sandbox/ scripts directly with "
            "minimal reasoning text in between, via scripted tool_use \"execute\" blocks (NOT "
            "\"execute_command\" - that name is only ever documented, never actually dispatched by "
            "the real harness) at the confirmed real staging path data/<ScenarioName>/"
            "tools_sandbox/ - isolates tool_exec_gap_s (CPU-bound) from real LLM decode time."
        ),
        confidence="validated",
    ),
}

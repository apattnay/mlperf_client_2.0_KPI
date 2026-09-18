"""
Agentic Instrumentation — Modular profiling for any agentic workflow.

Plug-and-play components:
    PhaseTracker     : Context-manager-based phase/sub-phase timeline tracking
    LLMProfiler      : TTFT/decode detection middleware for streaming LLM responses
    ToolProfiler     : MCP/function-call timing middleware
    KPIWriter        : Structured JSON output with nested phase hierarchy

Usage (minimal — 3 lines to instrument any workflow):

    from agentic_instrumentation import PhaseTracker, WorkflowProfilerMiddleware

    tracker = PhaseTracker("my_workflow")
    agent = Agent(client=client, middleware=[WorkflowProfilerMiddleware(tracker)])

    with tracker.phase("agent_1"):
        result = await agent.run(task)

    tracker.save("outputs/workflow_kpi.json")
"""

from .phase_tracker import PhaseTracker, Phase
from .middleware import WorkflowProfilerMiddleware, LLMTimingMiddleware, ToolTimingMiddleware
from .kpi_writer import KPIWriter
from .dashboard_controls import inject_dashboard_controls

__all__ = [
    "PhaseTracker",
    "Phase",
    "WorkflowProfilerMiddleware",
    "LLMTimingMiddleware",
    "ToolTimingMiddleware",
    "KPIWriter",
    "inject_dashboard_controls",
]

"""
Agent Framework Middleware — Automatic profiling hooks.

Three middleware classes that plug into agent_framework's middleware system:
    WorkflowProfilerMiddleware : Combines LLM + Tool timing in one middleware
    LLMTimingMiddleware        : Captures TTFT, decode time, token throughput
    ToolTimingMiddleware       : Captures MCP/function call durations

These integrate with PhaseTracker to auto-record sub-phases without
modifying agent code.

Usage:
    from agentic_instrumentation import PhaseTracker, WorkflowProfilerMiddleware

    tracker = PhaseTracker("my_workflow")
    profiler = WorkflowProfilerMiddleware(tracker)

    agent = Agent(
        client=client,
        tools=[mcp_server],
        middleware=[profiler],
    )

    with tracker.phase("my_agent"):
        result = await agent.run(task)
    # Sub-phases automatically captured: llm_call, tool_call, etc.
"""

import time
from typing import Any

from .phase_tracker import PhaseTracker


class LLMTimingMiddleware:
    """ChatMiddleware that captures LLM call timing (TTFT + decode).

    For non-streaming: records total wall time and token counts.
    For streaming: records time-to-first-token (TTFT) and decode throughput.

    Integrates with PhaseTracker to create sub-phases automatically.
    """

    def __init__(self, tracker: PhaseTracker):
        self.tracker = tracker
        self._call_count = 0

    async def process(self, context: Any, call_next):
        """ChatMiddleware.process — wraps each LLM API call."""
        self._call_count += 1
        call_id = self._call_count

        t_start = time.time()
        phase = self.tracker.begin_phase(
            f"llm_call_{call_id}",
            category="llm_call",
            call_id=call_id,
        )

        try:
            await call_next()
        finally:
            t_end = time.time()
            wall_time = t_end - t_start

            # Extract token usage from result
            result = getattr(context, "result", None)
            usage = {}
            if result and hasattr(result, "usage_details"):
                ud = result.usage_details or {}
                usage = {
                    "input_tokens": ud.get("input_token_count", 0),
                    "output_tokens": ud.get("output_token_count", 0),
                    "total_tokens": ud.get("total_token_count", 0),
                }
                out_tok = usage["output_tokens"]
                if out_tok > 0 and wall_time > 0:
                    usage["output_tokens_per_s"] = round(out_tok / wall_time, 1)

                # Estimate TTFT vs decode split
                # TTFT ≈ (input_tokens / throughput_prefill) — heuristic
                # For non-streaming, we approximate: prefill dominates first ~30% of time
                # For proper TTFT, streaming mode with chunk detection is needed
                in_tok = usage["input_tokens"]
                if in_tok > 0 and out_tok > 0:
                    # Rough model: prefill time ∝ input_tokens, decode time ∝ output_tokens
                    # Typical ratio: prefill ~2-5x faster per token than decode
                    prefill_weight = in_tok * 0.3  # prefill is ~3x faster per token
                    decode_weight = out_tok * 1.0
                    total_weight = prefill_weight + decode_weight
                    if total_weight > 0:
                        usage["est_prefill_s"] = round(wall_time * (prefill_weight / total_weight), 3)
                        usage["est_decode_s"] = round(wall_time * (decode_weight / total_weight), 3)
                        usage["est_ttft_ms"] = round(usage["est_prefill_s"] * 1000, 1)

            self.tracker.end_phase(phase, wall_time_s=round(wall_time, 3), **usage)


class ToolTimingMiddleware:
    """FunctionMiddleware that captures MCP/tool call timing.

    Records each function invocation with name, duration, and result size.
    Creates sub-phases like: rag_retrieve, analyze_data, generate_charts, etc.
    """

    # Map function names to semantic categories
    CATEGORY_MAP = {
        "read_reports": "rag_retrieve",
        "search_documents": "rag_retrieve",
        "query_knowledge": "rag_retrieve",
        "embed_documents": "rag_embed",
        "analyze_data": "tool_call",
        "generate_charts": "tool_call",
        "generate_presentation": "tool_call",
    }

    def __init__(self, tracker: PhaseTracker):
        self.tracker = tracker
        self._call_count = 0

    async def process(self, context: Any, call_next):
        """FunctionMiddleware.process — wraps each tool/function call."""
        self._call_count += 1

        # Get function name from context
        func_name = "unknown"
        if hasattr(context, "function"):
            func_name = getattr(context.function, "name", "unknown")
        elif hasattr(context, "name"):
            func_name = context.name

        category = self.CATEGORY_MAP.get(func_name, "tool_call")

        phase = self.tracker.begin_phase(
            func_name,
            category=category,
            call_id=self._call_count,
        )

        try:
            await call_next()
        finally:
            t_end = time.time()
            wall_time = t_end - phase.start_epoch

            # Capture result metadata
            result_meta = {}
            result = getattr(context, "result", None)
            if result is not None:
                result_str = str(result)
                result_meta["result_chars"] = len(result_str)
                if len(result_str) > 200:
                    result_meta["result_preview"] = result_str[:100] + "..."

            self.tracker.end_phase(phase, wall_time_s=round(wall_time, 3), **result_meta)


class WorkflowProfilerMiddleware:
    """Combined profiler that acts as both Chat and Function middleware.

    This is the recommended single-middleware solution. Attach to any agent:

        agent = Agent(client=client, middleware=[WorkflowProfilerMiddleware(tracker)])

    It automatically detects whether it's being called as a ChatMiddleware
    or FunctionMiddleware based on the context type.
    """

    def __init__(self, tracker: PhaseTracker):
        self.tracker = tracker
        self._llm = LLMTimingMiddleware(tracker)
        self._tool = ToolTimingMiddleware(tracker)

    async def process(self, context: Any, call_next):
        """Auto-dispatch to LLM or Tool middleware based on context type."""
        # Detect context type by checking for distinctive attributes
        ctx_type = type(context).__name__

        if "Chat" in ctx_type or "chat" in ctx_type.lower():
            await self._llm.process(context, call_next)
        elif "Function" in ctx_type or "Invocation" in ctx_type:
            await self._tool.process(context, call_next)
        else:
            # Agent-level middleware — just pass through
            await call_next()

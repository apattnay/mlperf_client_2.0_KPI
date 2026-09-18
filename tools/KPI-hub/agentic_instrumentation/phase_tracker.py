"""
Phase Tracker — Hierarchical timeline recording for agentic workflows.

Supports nested phases (agent > tool_call > LLM_call) with precise timestamps.
Thread-safe. Works as context manager or explicit start/end.

Design principles:
    - Zero dependencies beyond stdlib
    - Thread-safe (multiple agents can run in parallel)
    - Nestable (phases within phases)
    - Serializable to JSON for dashboard consumption
    - Reusable across any workflow (not tied to agent_framework)
"""

import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Phase:
    """A single phase or sub-phase in the workflow timeline."""

    name: str
    category: str = "agent"  # agent, llm_call, tool_call, rag_embed, rag_retrieve, prefill, decode
    start_epoch: float = 0.0
    end_epoch: float = 0.0
    metadata: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Optional["Phase"] = field(default=None, repr=False)

    @property
    def duration_s(self) -> float:
        if self.end_epoch and self.start_epoch:
            return round(self.end_epoch - self.start_epoch, 4)
        return 0.0

    @property
    def start_iso(self) -> str:
        return datetime.fromtimestamp(self.start_epoch).isoformat(timespec="milliseconds") if self.start_epoch else ""

    @property
    def end_iso(self) -> str:
        return datetime.fromtimestamp(self.end_epoch).isoformat(timespec="milliseconds") if self.end_epoch else ""

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict (recursive for children)."""
        d = {
            "name": self.name,
            "category": self.category,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "start_iso": self.start_iso,
            "end_iso": self.end_iso,
            "duration_s": self.duration_s,
            "metadata": self.metadata,
        }
        if self.children:
            d["sub_phases"] = [c.to_dict() for c in self.children]
        return d


class PhaseTracker:
    """Hierarchical phase timeline for any agentic workflow.

    Usage:
        tracker = PhaseTracker("productivity_workflow")

        with tracker.phase("analysis_agent", category="agent"):
            # Agent runs here — sub-phases auto-captured by middleware
            result = await agent.run()

        with tracker.phase("summary_agent", category="agent"):
            with tracker.phase("rag_retrieve", category="rag_retrieve"):
                docs = await rag.search(query)
            with tracker.phase("llm_generate", category="llm_call"):
                response = await agent.run()

        tracker.save("outputs/workflow_phases.json")
    """

    def __init__(self, workflow_name: str = "workflow"):
        self.workflow_name = workflow_name
        self.start_epoch: float = 0.0
        self.end_epoch: float = 0.0
        self.phases: list[Phase] = []  # Top-level phases
        self._lock = threading.Lock()
        self._stack_var: ContextVar[tuple[Phase, ...]] = ContextVar(
            f"phase_stack_{id(self)}", default=()
        )
        self._all_phases: list[Phase] = []  # Flat list for quick lookup

    def start(self):
        """Mark workflow start."""
        self.start_epoch = time.time()

    def stop(self):
        """Mark workflow end."""
        self.end_epoch = time.time()

    @property
    def workflow_duration_s(self) -> float:
        if self.end_epoch and self.start_epoch:
            return round(self.end_epoch - self.start_epoch, 2)
        return round(time.time() - self.start_epoch, 2) if self.start_epoch else 0.0

    def phase(self, name: str, category: str = "agent", **metadata) -> "_PhaseContext":
        """Context manager for a phase. Supports nesting."""
        return _PhaseContext(self, name, category, metadata)

    def begin_phase(self, name: str, category: str = "agent", **metadata) -> Phase:
        """Explicitly start a phase (for cases where context manager isn't suitable)."""
        p = Phase(name=name, category=category, start_epoch=time.time(), metadata=metadata)
        with self._lock:
            stack = list(self._stack_var.get())
            if stack:
                # Nested: attach to parent
                parent = stack[-1]
                p.parent = parent
                parent.children.append(p)
            else:
                # Top-level phase
                self.phases.append(p)

            stack.append(p)
            self._all_phases.append(p)
            self._stack_var.set(tuple(stack))

        return p

    def end_phase(self, phase: Phase, **extra_metadata):
        """Explicitly end a phase."""
        phase.end_epoch = time.time()
        if extra_metadata:
            phase.metadata.update(extra_metadata)

        with self._lock:
            stack = list(self._stack_var.get())
            if stack and stack[-1] is phase:
                self._stack_var.set(tuple(stack[:-1]))

    def add_event(self, name: str, category: str = "event", **metadata):
        """Add a point-in-time event (zero-duration marker)."""
        t = time.time()
        p = Phase(name=name, category=category, start_epoch=t, end_epoch=t, metadata=metadata)

        with self._lock:
            stack = self._stack_var.get()
            if stack:
                stack[-1].children.append(p)
            else:
                self.phases.append(p)
            self._all_phases.append(p)

    def to_dict(self) -> dict:
        """Serialize full timeline to JSON-friendly dict."""
        return {
            "workflow_name": self.workflow_name,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch or time.time(),
            "start_iso": datetime.fromtimestamp(self.start_epoch).isoformat(timespec="milliseconds") if self.start_epoch else "",
            "end_iso": datetime.fromtimestamp(self.end_epoch).isoformat(timespec="milliseconds") if self.end_epoch else "",
            "workflow_duration_s": self.workflow_duration_s,
            "phases": [p.to_dict() for p in self.phases],
            "total_phases": len(self._all_phases),
        }

    def save(self, path: str):
        """Write timeline JSON to disk."""
        import json
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def get_flat_phases(self, category: str = None) -> list[Phase]:
        """Get all phases (optionally filtered by category)."""
        if category:
            return [p for p in self._all_phases if p.category == category]
        return list(self._all_phases)


class _PhaseContext:
    """Context manager for phase tracking."""

    def __init__(self, tracker: PhaseTracker, name: str, category: str, metadata: dict):
        self._tracker = tracker
        self._name = name
        self._category = category
        self._metadata = metadata
        self._phase: Optional[Phase] = None

    def __enter__(self) -> Phase:
        self._phase = self._tracker.begin_phase(self._name, self._category, **self._metadata)
        return self._phase

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._phase.metadata["error"] = str(exc_val)
            self._phase.metadata["error_type"] = exc_type.__name__
        self._tracker.end_phase(self._phase)
        return False  # Don't suppress exceptions

    async def __aenter__(self) -> Phase:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)

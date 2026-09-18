"""Quick self-test for the agentic_instrumentation package."""
import asyncio
import time
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic_instrumentation import PhaseTracker, KPIWriter

tracker = PhaseTracker("e2e_test")
tracker.start()

# Simulate nested phases
with tracker.phase("analysis_agent", category="agent") as p1:
    time.sleep(0.05)
    with tracker.phase("rag_retrieve", category="rag_retrieve") as p2:
        time.sleep(0.02)
    with tracker.phase("llm_call_1", category="llm_call") as p3:
        time.sleep(0.03)

with tracker.phase("summary_agent", category="agent") as p4:
    time.sleep(0.04)
    tracker.add_event("checkpoint", category="event", note="halfway")

tracker.stop()

# Validate structure
data = tracker.to_dict()
assert data["total_phases"] == 5, f"Expected 5 phases, got {data['total_phases']}"
assert len(data["phases"]) == 2, f"Expected 2 top-level, got {len(data['phases'])}"
assert len(data["phases"][0]["sub_phases"]) == 2, "analysis_agent should have 2 sub-phases"
assert data["phases"][1]["sub_phases"][0]["name"] == "checkpoint"
assert data["workflow_duration_s"] > 0.1

# Test KPIWriter integration
writer = KPIWriter(tracker, model="Qwen3-4B", backend="ovms")
writer.record_stage("analysis_agent", wall_time_s=0.1, t_start=tracker.start_epoch)
writer.record_stage("summary_agent", wall_time_s=0.04, t_start=tracker.start_epoch + 0.1)
kpi = writer.to_dict()

assert "sub_phases" in kpi["stages"]["analysis_agent"], "Missing sub_phases in KPI"
assert kpi["model"] == "Qwen3-4B"
assert kpi["workflow_wall_time_s"] > 0

# Test flat phase query
llm_phases = tracker.get_flat_phases(category="llm_call")
assert len(llm_phases) == 1
assert llm_phases[0].name == "llm_call_1"


async def test_async_phase_isolation():
    async_tracker = PhaseTracker("async_test")
    async_tracker.start()

    async def child(index):
        with async_tracker.phase(f"child_{index}", category="agent"):
            await asyncio.sleep(0.005)

    fanout = async_tracker.begin_phase("fanout", category="orchestration")
    await asyncio.gather(*(child(index) for index in range(4)))
    async_tracker.end_phase(fanout)

    fanout_children = [phase.name for phase in fanout.children]
    assert fanout_children == ["child_0", "child_1", "child_2", "child_3"]


asyncio.run(test_async_phase_isolation())

print("ALL TESTS PASSED")
print(f"  Workflow duration: {data['workflow_duration_s']}s")
print(f"  Total phases: {data['total_phases']}")
print(f"  Top-level: {[p['name'] for p in data['phases']]}")
print(f"  Sub-phases: {[sp['name'] for sp in data['phases'][0]['sub_phases']]}")

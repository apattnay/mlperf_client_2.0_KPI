# Custom MLPerf Client Build Plan

## Purpose

Build a custom MLPerf client executable that emits detailed orchestrator telemetry, while preserving the existing MLPerf benchmark behavior and report compatibility.

The desired workflow is:

```text
clone LiteAgent-Clash / mlperf client repository
  -> initialize custom MLPerf source submodule
  -> create or reuse the repository virtual environment
  -> optionally run --build-custom
  -> fetch or update the pinned MLPerf source revision
  -> apply maintained instrumentation patches or shims
  -> build mlperf-windows.exe
  -> run preset 5 using the custom executable
  -> parse detailed orchestrator events into workflow_kpi.json
  -> render the enhanced KPI report
```

The custom build should be opt-in. Existing users should continue to be able to run the stock executable without building native code.

## Clarification: `--build-custom` Is Not a Venv Option

A Python virtual environment activation command cannot build a native executable. The proposed option should belong to a setup or benchmark command, for example:

```powershell
python tools/setup_mlperf.py --build-custom
```

or:

```powershell
python tools/run_kpi_preset.py --preset 5 --build-custom
```

The option may ensure that the venv exists and then invoke the native build, but it should not alter the semantics of venv activation itself.

Recommended separation:

```powershell
.\.venv\Scripts\Activate.ps1
python tools/setup_mlperf.py --build-custom
python tools/run_kpi_preset.py --preset 5 --mlperf-build custom
```

A convenience command may combine those steps later, but the underlying responsibilities should remain separate.

## Source Repository and Fork Strategy

The exact upstream source must be confirmed before implementation by inspecting the provenance of the currently installed `mlperf-windows.exe`, its version, and its build metadata. The source should be the MLPerf Client repository that produces this Windows executable, not merely the MLPerf Inference model/configuration repository.

The plan should identify and record:

- Upstream repository URL
- Upstream branch or release tag
- Commit corresponding to the installed stock executable
- Native build toolchain and dependency versions
- License and contribution requirements
- Whether the executable is built from MLPerf Client, MLPerf Inference, or an Intel-specific client fork

Do not guess the source repository from the executable name. The correct source is the one whose build system produces the same `mlperf-windows.exe` and supports the same scenario/configuration format.

### Recommended fork layout

Create a dedicated fork under the project owner's Git hosting account, for example:

```text
<organization>/mlperf-client-custom
```

Keep the fork focused on:

- Minimal instrumentation changes
- Build reproducibility
- Upstream synchronization
- Clearly separated patches
- No benchmark behavior changes unrelated to telemetry

The LiteAgent/MLPerf KPI repository should reference that fork as a pinned submodule:

```text
external/mlperf-client-custom/
```

The parent repository should pin an exact commit, not a moving branch. This ensures that a KPI run can always be reproduced against the same custom client source.

A submodule is preferable to copying the source into this repository because it keeps source history, upstream synchronization, and native build ownership separate. The tradeoff is that cloning requires:

```powershell
git clone --recurse-submodules <parent-repository>
```

or:

```powershell
git submodule update --init --recursive
```

## What the Custom Instrumentation Should Capture

The current wrapper can measure stage boundaries, token counts, TTFT, tool duration, and timestamp gaps. The custom executable should explain those gaps with structured events.

### Workflow lifecycle

Capture:

- Process start and end
- Configuration loading
- Scenario initialization
- Provider/model initialization
- Warmup setup
- Main workload start
- Shutdown and cleanup
- Exit code and termination reason

### Task lifecycle

Capture a stable task identifier and transitions:

```text
task_created
task_queued
task_dequeued
task_submitted
stage_started
first_token
stage_completed
stage_failed
stage_cancelled
```

Each event should include:

```json
{
  "event": "stage_started",
  "task_id": "03_swe_agent_1",
  "iteration": 1,
  "stage": "swe_agent_1",
  "timestamp_monotonic_ns": 0,
  "timestamp_epoch": 0,
  "thread_id": 0,
  "process_id": 0
}
```

Use a monotonic timestamp for duration calculations and wall-clock time only for report display. This avoids errors when the system clock changes during a run.

### Scheduler and queue behavior

Capture:

- Queue depth before and after each dispatch
- Queue wait duration
- Dispatch duration
- Number of active tasks
- Sequential versus concurrent execution
- Dependency that blocked a task
- Configured delay versus unexpected delay
- Scheduling reason

Example event names:

```text
scheduler_enqueue
scheduler_dequeue
scheduler_dispatch_begin
scheduler_dispatch_end
scheduler_wait_begin
scheduler_wait_end
```

### Prompt and context preparation

Capture:

- Prompt file read time
- Prompt template expansion
- System prompt construction
- Conversation history assembly
- Tokenization time
- Input-token counting
- Warm/cold context decision
- KV-cache reuse or reset
- Context truncation
- Serialization/deserialization

Useful fields include:

```json
{
  "input_tokens": 10251,
  "history_tokens": 1212,
  "new_prompt_tokens": 9039,
  "context_reused": true,
  "cache_reset": false,
  "prompt_prepare_ms": 0,
  "tokenize_ms": 0
}
```

### LLM request lifecycle

Capture:

- Request creation
- Request enqueue
- Provider submission
- Network or IPC wait
- TTFT
- Prefill duration
- Decode duration
- Stream assembly
- Response parsing
- Usage metadata extraction
- Retry and backoff
- Timeout or cancellation

Existing MLPerf log lines should continue to be emitted for backward compatibility. The new structured events should supplement, not replace, them.

### Tool lifecycle

Capture:

- Tool-call detection
- Argument parsing
- Tool dispatch
- Tool process startup
- Tool execution
- stdout/stderr collection
- Result parsing
- Result insertion into the next prompt
- Timeout, retry, or failure

Use a correlation ID linking the tool event to its parent task and model response.

### Iteration bookkeeping

Capture the work that currently appears as unexplained gaps:

```text
iteration_begin
iteration_end
result_validation
agent_output_collection
next_prompt_selection
conversation_state_update
metrics_update
log_flush
next_task_scheduled
```

This is especially important for the gap between the final SWE-agent stage and the next warmup stage.

## Event Transport and Compatibility

The safest first implementation is structured line logging to the existing executor log or a separate orchestrator trace file.

Preferred format:

```text
[2026-10-08 07:22:38.123] ... ORCH_EVENT {"event":"scheduler_wait_begin", ...}
```

A separate file is preferable if the existing executor log format is difficult to extend:

```text
Logs/<scenario>_orchestrator.jsonl
```

JSON Lines is recommended because it is:

- Append-only
- Easy to parse while a run is active
- Friendly to partial or interrupted runs
- Extensible without breaking older parsers
- Suitable for nested correlation IDs

The wrapper should tolerate missing trace files and fall back to the current timestamp-gap behavior.

## Changes in the Parent KPI Repository

### 1. Source and build management

Add:

```text
external/mlperf-client-custom/       # pinned submodule
build/                                # ignored native build output
config/custom_build.json              # optional build metadata
```

Do not commit generated binaries or build directories unless there is a deliberate release policy.

### 2. Build command

Add a dedicated setup/build script with options such as:

```text
--build-custom
--custom-source <path>
--custom-ref <commit>
--clean-build
--skip-fetch
--skip-patch
--configuration Release
```

The script should:

1. Validate prerequisites.
2. Initialize/update the submodule.
3. Check out the pinned commit.
4. Apply instrumentation patches or build the fork directly.
5. Configure the native build.
6. Build the Release executable.
7. Verify that the resulting executable exists.
8. Record source commit, compiler, configuration, and build timestamp.
9. Return a nonzero exit code on failure.

### 3. Executable selection

Make the benchmark runner explicit about which executable it uses:

```text
stock
custom
explicit path
```

The generated `experiment.json` should record:

```json
{
  "mlperf_executable": "...",
  "mlperf_build": "custom",
  "mlperf_source_commit": "...",
  "instrumentation_schema_version": "1"
}
```

Never silently replace the stock executable. A report must make it obvious whether the run used the custom build.

### 4. Trace parser

Extend `run_kpi_workflow.py` to parse the JSONL trace and merge it into:

```json
{
  "orchestrator": {
    "events": [],
    "gaps": [],
    "metrics": {}
  }
}
```

The existing executor-log parser remains the source of truth for leaf-stage KPIs. The orchestrator trace becomes the source of truth for sub-stage attribution.

### 5. Report changes

Add:

- Nested orchestrator timeline
- Task lifecycle table
- Gap attribution table
- Scheduler and queue metrics
- Prompt/context preparation metrics
- Tool lifecycle metrics
- Orchestrator p50/p95/p99 distributions
- Accounted versus unaccounted workflow time
- Trace completeness and confidence indicators

Do not present inferred timestamp gaps as measured internal operations. Every value should include a source or confidence label.

## Build Size and Time Estimates

Exact values depend on the identified upstream repository, dependency cache, compiler, and whether third-party dependencies are vendored.

Reasonable planning estimates are:

| Component | Approximate size | Typical time |
|---|---:|---:|
| MLPerf client source checkout | 100 MB to 500 MB | 1 to 10 minutes |
| Full dependency checkout/cache | 500 MB to several GB | 5 to 30 minutes |
| First clean Windows Release build | 10 to 40 minutes | 10 to 60 minutes |
| Incremental instrumentation rebuild | Same build tree | 30 seconds to 10 minutes |
| Reconfigure after dependency/toolchain change | Same build tree | 5 to 30 minutes |

These are planning ranges, not guarantees. The largest variables are:

- Whether dependencies are already cached
- Whether the build uses CMake, vcpkg, Conan, or custom scripts
- Whether OpenVINO/ONNX/runtime components are rebuilt
- Whether the build is CPU-only or includes accelerator integrations
- Antivirus scanning and Windows filesystem performance
- Number of parallel compiler jobs
- Network access to dependency repositories

The initial implementation should measure and record actual checkout and build durations rather than relying on estimates.

## Is Source-Level Instrumentation Necessary?

For the desired detail, yes. The existing prebuilt binary exposes enough information for inferred gap accounting but not enough to identify internal operations reliably.

The custom source is required to distinguish:

```text
prompt preparation
context assembly
scheduler wait
queue wait
result parsing
iteration bookkeeping
logging overhead
```

A wrapper-only solution can add process and OS-level measurements, but it cannot see internal executor function boundaries inside the compiled binary.

## Risks and Mitigations

### Source mismatch

The fork may not correspond to the installed executable.

Mitigation:

- Identify the stock executable's version and source commit first.
- Reproduce a stock build before adding instrumentation.
- Compare benchmark outputs between stock and custom builds.

### Benchmark behavior changes

Instrumentation can accidentally change scheduling or timing.

Mitigation:

- Use low-overhead JSONL logging.
- Buffer events in memory and flush asynchronously where safe.
- Provide an instrumentation-off build flag.
- Keep the benchmark logic unchanged.

### Log-volume overhead

Detailed event logging may affect short stages.

Mitigation:

- Use monotonic timestamps and compact JSON.
- Avoid logging full prompts, model responses, or tool payloads by default.
- Record sizes, hashes, and IDs instead of sensitive content.
- Measure instrumentation overhead with the same workload.

### Upstream drift

The fork can become difficult to update.

Mitigation:

- Keep changes small and isolated.
- Maintain one instrumentation commit or a small patch series.
- Periodically rebase from upstream.
- Document the exact upstream base commit.

### Privacy and sensitive data

Prompts, tool arguments, and model responses may contain sensitive content.

Mitigation:

- Do not emit raw prompt or response contents by default.
- Use token counts, lengths, hashes, and stable IDs.
- Add an explicit opt-in debug mode for payload capture.

### Partial or failed runs

The executable may terminate before the trace is complete.

Mitigation:

- Flush lifecycle events at important boundaries.
- Mark incomplete tasks as `unknown` or `aborted`.
- Include trace completeness metrics in the report.

## Proposed Implementation Order

### Phase 0: Identify and reproduce the stock client

- Locate the exact MLPerf client source repository.
- Record executable version and build metadata.
- Pin the upstream commit.
- Reproduce a stock build if practical.
- Compare stock output with the currently installed executable.

### Phase 1: Fork and submodule integration

- Create the custom fork.
- Add it as a pinned submodule.
- Add documentation for cloning and updating it.
- Add a source/build manifest.

### Phase 2: Minimal structured tracing

Add only:

- Workflow start/end
- Task created/queued/dequeued
- Stage start/end
- Iteration start/end
- Tool start/end
- Error and retry events

Build and compare stock versus custom benchmark results.

### Phase 3: Context and scheduler tracing

Add:

- Prompt/context preparation spans
- Queue depth
- Scheduling decisions
- Warm/cold cache decisions
- Result parsing and bookkeeping spans

Update the parser and report.

### Phase 4: Process and resource tracing

Add:

- PID and thread metadata
- Child process lifecycle
- CPU time and utilization
- Memory high-water mark
- IPC/network wait indicators where available

Keep this separate from accelerator KPI calculations.

### Phase 5: Build automation

Implement `--build-custom` with:

- Prerequisite checks
- Submodule initialization
- Pinned checkout
- Native configuration
- Release build
- Artifact verification
- Build manifest generation
- Custom executable selection

### Phase 6: Regression and performance validation

For identical preset 5 runs, compare:

- Output correctness
- Stage ordering
- Token totals
- Wall time
- Tool behavior
- Exit status
- Trace completeness
- Instrumentation overhead

The custom build should not be considered production-ready until its benchmark results are equivalent to the stock build within an agreed tolerance.

## Acceptance Criteria

The implementation is complete when:

- A clean clone can initialize the custom source submodule.
- `--build-custom` produces a verified Release executable.
- Preset 5 can explicitly select the custom executable.
- The report identifies the custom source commit and build.
- Every stage has lifecycle events.
- Inter-stage gaps are attributed to measured events where possible.
- Remaining unclassified time is visible.
- No negative gaps or synthetic-parent iteration artifacts appear.
- Leaf token and throughput totals remain unchanged.
- Custom and stock runs produce equivalent benchmark behavior.
- Instrumentation overhead is measured and documented.

## Immediate Next Step

Before writing build automation, identify the actual source repository and commit corresponding to the installed `mlperf-windows.exe`. Once that provenance is confirmed, the fork, submodule path, build commands, dependency strategy, and realistic size/time estimates can be finalized safely.

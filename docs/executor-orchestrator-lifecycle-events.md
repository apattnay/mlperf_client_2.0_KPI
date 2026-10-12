# Executor and Orchestrator Lifecycle Events

The MLPerf KPI workflow can record lifecycle events emitted by the native executor. These events describe high-level orchestration around inference and tool execution. They are not individual CPU, GPU, or NPU kernel calls.

## Collection Requirement

Native lifecycle events require an MLPerf Client built from source with the native instrumentation described in this repository. The precompiled/default runtime does not emit these events. Follow the [Windows source-build guide](../README_BUILD.md), then use [`tools/setup_mlperf_v2_from_source.ps1`](../tools/setup_mlperf_v2_from_source.ps1) to build and stage the instrumented runtime. Pass that staged directory to the KPI preset runner with `--mlperf-dir`.

## Events

| Event | Meaning |
| --- | --- |
| `model_init` | The model and inference runtime are initialized for an execution group. The duration covers the initialization work. |
| `model_deinit` | The model and inference runtime are released. This is a lifecycle event for the end of an execution group or run. |
| `turn_start` | A logical inference task begins. Details identify the execution group, task index, warmup status, history-token count, and user-token count. |
| `prepare` | The inference object prepares the next request. This corresponds to the executor's high-level prepare operation, not the kernels launched by inference. |
| `inference_delay` | The configured delay before inference. Its duration is the time spent waiting in the executor. |
| `tool_start` | An agent tool call begins. Details identify the tool and call ID. |
| `tool_end` | An agent tool call finishes. Details include the tool, call ID, duration, and success status. |
| `reset` | The inference object is reset after a task. The duration covers the executor's reset operation. |
| `turn_end` | A logical inference task finishes. Details include the task index, generated-token count, and success status. |

## Reading the Timeline

Events are shown alongside inference stages in the KPI report. Rows are ordered by event start time from left to right and top to bottom. Native events are placed before other rows when they start at the same timestamp. Separate rows with aligned bars indicate overlapping activity.

Most events are instantaneous markers with a near-zero duration. Events such as `model_init`, `inference_delay`, `tool_end`, and `reset` can have measurable durations. The duration is measured around the executor operation that emitted the event.

The event timestamp is the native executor log timestamp. For duration-bearing events, log emission occurs after the measured operation, so consumers that reconstruct intervals should treat the timestamp as the operation endpoint and subtract the duration. The report timeline preserves the event ordering and displays the measured duration for readability.

## What These Events Do Not Show

These events do not provide a kernel trace. Model inference between `prepare` and `reset` may launch many runtime, CPU, GPU, or NPU operations that are not represented individually here. Use platform profilers or hardware telemetry when kernel-level attribution is required.

## Data Locations

- Per-stage events: `workflow_kpi.json` under each run directory, in `stages.*.orchestrator_events`.
- Aggregated events: `workflow_kpi.json`, in `orchestrator.native_events` and `orchestrator.native_event_summary`.
- Visual report: `kpi_report.html` under the same run directory.

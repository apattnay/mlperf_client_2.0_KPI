# LiteAgent-Instrumentation

Plug-and-play hardware telemetry sampler and workflow profiling toolkit for heterogeneous compute platforms (CPU P/E-cores, iGPU, dGPU, NPU, DRAM bandwidth).

## Components

| Component | Purpose |
|-----------|---------|
| `sample_utilization_fast.py` | High-frequency HW sampler (CPU, GPU, NPU, DRAM BW via PCM/EMON) |
| `plot_utilization_interactive.py` | Plotly dashboard generator from sampler CSV |
| `mdapi_collector.py` | Standalone MDAPI counter collector — any metric group, CSV + HTML |
| `l0_metrics_sampler.py` | Background thread L0 TBS sampler (ComputeBasic subset) |
| `agentic_instrumentation/` | Workflow phase tracker, LLM/tool profiling middleware, KPI writer |
| `config/instrumentation.json` | Profiles (lightweight, simulation, memory_deep, power_analysis, unified) |

## Quick Start — Standalone HW Sampling

```powershell
# Set environment for Level Zero GPU metrics
$env:ZES_ENABLE_SYSMAN = "1"
$env:ZET_ENABLE_METRICS = "1"

# Run sampler (works with ANY workload running in parallel)
python sample_utilization_fast.py --profile lightweight --interval 1000 --output my_samples.csv

# Generate interactive dashboard
python plot_utilization_interactive.py --input my_samples.csv --output dashboard.html
```

## Quick Start — MDAPI Counter Collection

```powershell
$env:ZET_ENABLE_METRICS = "1"

# List all available iGPU metric groups
python mdapi_collector.py --list-groups

# Collect while running your own workload (OVMS, Ollama, etc.)
python mdapi_collector.py --duration 30 --output results/

# Collect with built-in GPU load (for testing)
python mdapi_collector.py --duration 10 --synthetic-load --output results/

# Different metric group (VectorEngineProfile, MemoryProfile, etc.)
python mdapi_collector.py --group VectorEngineProfile --duration 15 --output results/
```

Outputs: timestamped CSV (all counters) + interactive Plotly HTML dashboard.

## Quick Start — Workflow Profiling

```python
from agentic_instrumentation import PhaseTracker, WorkflowProfilerMiddleware, KPIWriter

tracker = PhaseTracker("my_workflow")
tracker.start()

with tracker.phase("step_1", category="agent"):
    # ... your workload ...
    pass

tracker.stop()
tracker.save("outputs/workflow_kpi.json")
```

## Profiles

Configure via `--profile <name>` or override individual sources with `--source key=value`:

- **lightweight** — Minimal overhead (~5ms/sample), PCM/PDH + GPU
- **simulation** — Full EMON collection for HW simulation projection (~1s intervals)
- **memory_deep** — DRAM bandwidth + cache + page hit/miss analysis
- **power_analysis** — Platform power + C-state residency
- **unified** — All sources combined for Roofline analysis

## Integration as Git Submodule

```bash
# In your project repo:
git submodule add <repo-url> instrumentation

# Use the sampler:
python instrumentation/sample_utilization_fast.py --profile lightweight --output data.csv
```

## Platform Support

Designed for Intel heterogeneous platforms (Nova Lake, Meteor Lake, etc.) with:
- Intel PCM / EMON for memory bandwidth and microarchitecture counters
- Windows PDH for CPU/iGPU/power counters
- nvidia-smi for discrete GPU telemetry
- Level Zero Sysman for Intel GPU metrics

## Dependencies

- Python ≥ 3.11
- `pywin32` — Windows PDH performance counters
- `psutil` — CPU/memory utilization
- `pandas` — CSV processing for plotter
- `plotly` — Interactive dashboard generation

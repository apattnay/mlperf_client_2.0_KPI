# AGENTS.md

## Project Contents

The primary content is the MLPerf benchmark suite which includes several
personas. Also included are tools for telemetry collection and profiling that
are used when these benchmarks are executed.

## Project Goal

Create roofline models of the benchmarks that can be used to quickly calculate
key performance indicators (KPIs) when key input variables are changed.

## Key input variables

1st-pass analysis
1. CPU frequency
2. Bus bandwidth
3. iGPU frequency or FLOPS

2nd-pass analysis
4. L3 cache size
5. L2 cache size
6. Amount of host RAM
7. RAM read bandwidth
8. RAM read latency
9. RAM write bandwidth
10. RAM write latency

3rd-pass analysis
11. TDP

## KPIs output by the roofline model

1st-pass analysis
1. Wall-clock time

2nd-pass analysis
1. Power (Watts)

3rd-pass analysis
1. L3 cache miss rate
2. L2 cache miss rate

## Constraints

- `kpi_runs/` is generated output. Never edit any files within this subdirectory
or any subdirectory.

## Running benchmarks/experiments

- `.venv\Scripts\python.exe tools\run_kpi_preset.py --preset <1-8>`. See
[docs/run_benchmark_prompt.md](docs/run_benchmark_prompt.md) for full setup and
[docs/BENCHMARK_DESCRIPTIONS.md](docs/BENCHMARK_DESCRIPTIONS.md) for what each
preset runs.
- Experiment output ends up in `kpi_runs/{scenario_name}_{timestamp}/`.
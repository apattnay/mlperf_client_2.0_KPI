# KPI-Hub: Data Sources & Aggregation Reference

**Purpose**: Document all data sources aggregated by the instrumentation pipeline for workload characterization and performance analysis of multi-agent AI workflows on Intel heterogeneous compute (Nova Lake).

---

## 1. Unified CSV (`hw_samples.csv`) — Time-Series Telemetry

Collected by `sample_utilization_fast.py` at 1000ms intervals. One row per sample.

### CPU Metrics

| Column | Source | Description |
|--------|--------|-------------|
| `cpu_total_pct` | psutil | Aggregate CPU utilization % |
| `cpu_core_min/max` | psutil | Min/max per-core utilization |
| `cpu_cores_csv` | psutil | Per-core %, semicolon-separated (24 values) |
| `cpu_freq_min/max_mhz` | PDH `Processor Information\Actual Frequency` | Per-core frequency range |
| `cpu_freq_csv` | PDH | Per-core MHz, semicolon-separated |
| `cpu_throttle_pct` | PDH `% Performance Limit` | CPU thermal throttle % (0=none) |
| `soc_temp_c` | PDH Thermal Zone | SoC temperature (°C) |

### iGPU Metrics (Intel Xe, 128 EUs @ 2.0 GHz)

| Column | Source | Description |
|--------|--------|-------------|
| `igpu_pct` | PDH GPU Engine Counters | Overall iGPU utilization % |
| `igpu_ded_mb` | PDH GPU Adapter Memory | Dedicated VRAM usage (MB) |
| `igpu_shared_mb` | PDH GPU Adapter Memory | Shared system memory usage (MB) |
| `l0_gpu_busy` | Level Zero TBS (ComputeBasic) | GPU Busy % — OA hardware counter |
| `l0_xve_active` | Level Zero TBS | XVE (EU) Active % — actual compute |
| `l0_xve_stall` | Level Zero TBS | XVE Stall % — waiting on memory/deps |
| `l0_xve_occupancy` | Level Zero TBS | Thread Occupancy % — EU slot utilization |
| `l0_l3_stall` | Level Zero TBS | L3 Cache Stall % |
| `l0_gpu_freq_mhz` | Level Zero TBS | Actual GPU core frequency |
| `l0_mem_read_gbs` | Level Zero TBS | GPU memory read bandwidth (GB/s) |
| `l0_mem_write_gbs` | Level Zero TBS | GPU memory write bandwidth (GB/s) |
| `l0_compute_busy` | Level Zero TBS | Compute engine busy % |
| `l0_copy_busy` | Level Zero TBS | Copy engine busy % |
| `l0_render_busy` | Level Zero TBS | Render engine busy % |

### NPU Metrics (Intel NPU, 35 TOPS)

| Column | Source | Description |
|--------|--------|-------------|
| `npu_pct` | PDH GPU Engine / Application polling | NPU utilization % |
| `npu_ded_mb` | PDH GPU Adapter Memory | NPU dedicated memory |
| `npu_shared_mb` | PDH GPU Adapter Memory | NPU shared memory |
| `npu_infer_ms` | NPU Embedding Server `/metrics` | Last inference latency (ms) |

### NVIDIA dGPU Metrics (RTX 5070)

| Column | Source | Description |
|--------|--------|-------------|
| `nvidia_gpu_pct` | nvidia-smi (NVML) | GPU utilization % |
| `nvidia_mem_pct` | nvidia-smi | VRAM utilization % |
| `nvidia_mem_used/total_mb` | nvidia-smi | VRAM usage |
| `nvidia_temp_c` | nvidia-smi | GPU temperature |
| `nvidia_power_w` | nvidia-smi | GPU power draw (W) |
| `nvidia_sm_clk_mhz` | nvidia-smi | SM clock frequency |
| `nvidia_mem_clk_mhz` | nvidia-smi | Memory clock frequency |
| `nvidia_pcie_gen` | nvidia-smi | PCIe link generation |
| `nvidia_pcie_width` | nvidia-smi | PCIe link width |
| `nvidia_throttle` | nvidia-smi | Clock throttle bitmask |

### Power Metrics (RAPL)

| Column | Source | Description |
|--------|--------|-------------|
| `rapl_cpu_w` | PDH Energy Meter (VCCIA) | CPU IA domain power (W) |
| `rapl_igpu_w` | PDH Energy Meter (VCCGT) | iGPU power (W) |
| `rapl_npu_w` | PDH Energy Meter (NPU) | NPU power (W) |
| `rapl_soc_w` | PDH Energy Meter (SoC) | Total SoC package power (W) |

### DRAM Bandwidth & Latency (EMON/SEP)

| Column | Source | Description |
|--------|--------|-------------|
| `dram_read_gbs` | EMON (UNC_M_CAS_COUNT.RD × 64B) | DRAM read bandwidth (GB/s) |
| `dram_write_gbs` | EMON (UNC_M_CAS_COUNT.WR × 64B) | DRAM write bandwidth (GB/s) |
| `dram_total_gbs` | EMON (derived) | Total DRAM bandwidth |
| `ia_dram_bw_gbs` | EMON (L3MISS × 64B) | CPU-initiated DRAM BW |
| `nonia_dram_bw_gbs` | EMON (total - IA) | iGPU+NPU-initiated DRAM BW |
| `dram_page_hit_rate_rd` | EMON (PAGE_HIT/MISS_RD) | DRAM page hit rate (reads) |
| `dram_page_hit_rate_wr` | EMON (PAGE_HIT/MISS_WR) | DRAM page hit rate (writes) |
| `dram_rd_latency_imc_clks` | EMON (IMC occupancy/requests) | Read latency in IMC clocks |
| `dram_rd_latency_ns` | EMON (derived) | Read latency in nanoseconds |

### OVMS KV-Cache (via `ovms_log_poller.py`)

| Column | Source | Description |
|--------|--------|-------------|
| `ovms_kv_cache_pct` | OVMS runtime log regex | KV-cache usage % |
| `ovms_kv_cache_mb` | OVMS runtime log regex | KV-cache usage (MB) |

Requires `OVMS_LOG_PATH` env var pointing to the OVMS stdout log file. Parsed via regex: `cache usage: X% of Y MB`.

### KV-Cache Size Estimate, Native EP (via `plot_utilization_interactive.py`)

For scenarios that don't run against an OVMS server (e.g. the in-process NativeOpenVINO,
OrtGenAI, WindowsML execution providers), there is no runtime log exposing live KV-cache usage.
`load_phase_tokens()` / `estimate_kv_cache_series()` instead derive an **analytical estimate**
(MB over time) directly at plot time from the `--phases` workflow_kpi.json already passed to
`plot_utilization_interactive.py` — no new sampler/log source is needed.

| Formula | Description |
|---------|-------------|
| `bytes/token = 2 (K+V) × num_hidden_layers × num_key_value_heads × head_dim × dtype_bytes` | Standard transformer KV-cache formula |
| `KV_CACHE_MB_PER_TOKEN` (model-name prefix keyed) | e.g. Llama-3.1-8B: 32 layers × 8 KV heads (GQA) × 128 head_dim × 2 bytes (fp16 cache) = 0.125 MB/token |
| Per-stage curve | Jumps to `input_tokens` right after `ttft_s` (prefill), then ramps linearly to `input_tokens + output_tokens` over the remaining decode window |

Add new model families to `KV_CACHE_MB_PER_TOKEN` in `plot_utilization_interactive.py` as needed.
Both mechanisms are additive: if OVMS-measured data is present it's shown, and/or the estimate is
shown when its model is recognized and stage token data is available (see `has_kv_cache_ovms` /
`kv_cache_est` in `build_dashboard()`).

### System Memory

| Column | Source | Description |
|--------|--------|-------------|
| `ram_used_gb` | psutil | System RAM usage (GB) |
| `ram_total_gb` | psutil | Total system RAM (GB) |

---

## 2. Workflow KPI (`workflow_kpi.json`) — Per-Agent Performance

Collected by the agentic workflow orchestrator via streaming token measurement.

| Field | Source | Description | Status |
|-------|--------|-------------|--------|
| `input_tokens` | LLM response `usage_details` | Prompt tokens consumed | ✅ |
| `output_tokens` | LLM response `usage_details` | Generated tokens | ✅ |
| `total_tokens` | LLM response `usage_details` | Total token count | ✅ |
| `wall_time_s` | `time.time()` | Stage wall-clock time | ✅ |
| `output_tokens_per_s` | Derived | Decode throughput | ✅ |
| `ttft_s` | Streaming: time to first `text` chunk | Time To First Token | ✅ |
| `decode_time_s` | last_chunk - first_chunk | Active decode duration | ✅ |
| `avg_itl_ms` | Streaming: mean inter-chunk delta | Average Inter-Token Latency | ✅ |
| `p50_itl_ms` | Streaming: 50th percentile | Median ITL | ✅ |
| `p99_itl_ms` | Streaming: 99th percentile | Tail ITL | ✅ |
| `text_chunks` | Streaming: count of non-empty deltas | Number of decode steps | ✅ |

**Per-stage fields** (analysis_agent, summary_agent_1, summary_agent_2, executive_summary_agent, task_agent).

---

## 3. RAG KPI (`rag_kpi.json`) — Embedding Performance

Collected by `mcp_sever_prod_uc_rag.py`.

| Field | Source | Description |
|-------|--------|-------------|
| `embedding_model` | Config | Embedding model identifier |
| `embedding_api_base` | Config | Embedding endpoint URL |
| `embedding_setup_time_s` | `time.time()` | Time to embed all documents |
| `docs_embedded` | Counter | Total documents embedded |
| `embedding_throughput_docs_per_s` | Derived | Embedding throughput |
| `query_count` | Counter | Total RAG queries processed |
| `query_times_s` | List | Per-query latency |
| `avg_query_time_s` | Derived | Average query latency |

---

## 4. Peak RSS (`hw_samples_peak_rss.json`) — Process Memory Footprint

Collected at sampler shutdown by scanning running processes.

| Field | Source | Description |
|-------|--------|-------------|
| `ovms.[].peak_rss_mb` | `psutil.memory_info().peak_wset` | OVMS peak working set |
| `AgenticAI_ProdAgent.[].peak_rss_mb` | psutil | Workflow agent peak RSS |
| `mcp_sever_prod_uc_rag.[].peak_rss_mb` | psutil | RAG MCP server peak RSS |
| `npu_embedding_server.[].peak_rss_mb` | psutil | NPU embedding server peak RSS |

---

## 5. Experiment Metadata (`experiment.json`)

| Field | Source | Description |
|-------|--------|-------------|
| `backend` | Config | `ovms` or `ollama` |
| `model` | Config | Model identifier |
| `hetero` | Config | Heterogeneous mode flag |
| `kpi_mode` | Config | `full`, `workflow`, `none` |
| `workflow_exit_code` | Process exit | 0=success |
| `workflow_duration_s` | `time.time()` | Total wall-clock time |
| `model_weight_mb` | `os.stat()` sum | Model directory size in MB |
| `model_weight_path` | Config | Path to model directory |

---

## 6. Implementation Status & Remaining Gaps

### ✅ Implemented

| Metric | Where It Lives | Notes |
|--------|---------------|-------|
| TTFT (s) | `workflow_kpi.json` → per-stage `ttft_s` | Via streaming chunk measurement |
| p50/p99 ITL (ms) | `workflow_kpi.json` → `p50_itl_ms`, `p99_itl_ms` | Same as above |
| Peak RSS/Process | `hw_samples_peak_rss.json` | Written at sampler shutdown; filtered <10MB wrappers |
| DRAM Latency (ns) | `hw_samples.csv` → `dram_rd_latency_ns` | Via EMON IMC occupancy counters |
| KV-Cache Size (dynamic, OVMS) | `hw_samples.csv` → `ovms_kv_cache_pct`, `ovms_kv_cache_mb` | `ovms_log_poller.py` parses OVMS runtime log in real-time |
| KV-Cache Size (estimated, native EP) | `dashboard.html` panel (derived at plot time) | `estimate_kv_cache_series()` — analytical, from stage token counts × model architecture; no OVMS server needed |
| Model Weight Size (MB) | `experiment.json` → `model_weight_mb` | `os.stat()` sum of model directory at experiment start |
| iGPU Efficiency Analysis | `kpi_report.html` | Theoretical compute demand (2×params), measured GEMM reference, DRAM BW utilization |
| DRAM BW Utilization % | `kpi_report.html` | `decode_tok_per_s × model_weight_GB / 89 GB/s` — primary efficiency metric for memory-bound decode |

### ⚠️ Partial / Proxy Only

| Metric | Status | Notes |
|--------|--------|-------|
| **NPU TOPS %** | Proxy only | We have `npu_pct` (utilization) and `npu_infer_ms` (latency). True TOPS% requires op-count not exposed via Windows APIs |
| **iGPU INT4 Throughput** | Measured on 16 EUs only | 0.078 TOPS INT4 measured; extrapolated to 128 EUs: ~0.63 TOPS. Need full-EU benchmark to confirm scaling |

### ❌ Not Feasible (Hardware/Driver Limitation)

| Metric | Blocker |
|--------|---------|
| True NPU op-count TOPS | Intel NPU driver doesn't expose arithmetic operation counters on Windows. Only utilization % and latency available. |
| Per-EU instruction mix | L0 ComputeBasic doesn't break down INT4 vs FP16 vs FP32 ops. Would need `ComputeExtended` group (not available on NVL). |
| True INT4 peak TOPS | Theoretical 32.8 TOPS assumes native packed-INT4 dot-product. Measured (16 EUs) = 0.078 TOPS; extrapolated (128 EUs) = ~0.63 TOPS — still ~52× below theoretical. INT4 only 1.17× faster than FP32 confirms no native INT4 hardware. |

---

## 7. Data Flow Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         EXPERIMENT RUNNER                              │
│                    (run_experiment.py --preset N)                      │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           │                      │                      │
           ▼                      ▼                      ▼
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────────┐
│  HW Sampler     │   │  Workflow Agent  │   │  MCP Servers        │
│  (1s intervals) │   │  (streaming)    │   │  (RAG + Analysis)   │
├─────────────────┤   ├─────────────────┤   ├─────────────────────┤
│ • psutil CPU    │   │ • Token counts  │   │ • Embedding time    │
│ • PDH Freq/RAPL│   │ • TTFT (stream) │   │ • Query latency     │
│ • EMON BW/Lat  │   │ • ITL p50/p99   │   │ • Docs embedded     │
│ • L0 TBS EU    │   │ • Wall time     │   │                     │
│ • nvidia-smi   │   │ • Phase timing  │   │                     │
│ • PDH GPU/NPU  │   │                 │   │                     │
└────────┬────────┘   └────────┬────────┘   └──────────┬──────────┘
         │                     │                        │
         ▼                     ▼                        ▼
   hw_samples.csv      workflow_kpi.json          rag_kpi.json
   peak_rss.json       workflow_timeline.json
         │                     │                        │
         └─────────────────────┼────────────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │  Report Generator   │
                    │  (plot + KPI HTML)  │
                    ├─────────────────────┤
                    │ • dashboard.html    │
                    │ • kpi_report.html   │
                    └─────────────────────┘
```

---

## 8. Configuration Profiles (`config/instrumentation.json`)

| Profile | EMON Events | Interval | Use Case |
|---------|-------------|----------|----------|
| `lightweight` | BW + latency + page hit | 1000ms | Default experiment runs |
| `simulation` | Full TopDown + cache + power + C-state | 1000ms | Performance modeling |
| `memory_deep` | BW + cache + latency detailed | 1000ms | Memory bottleneck analysis |
| `power_analysis` | Power + C-state + frequency | 1000ms | Power characterization |

---

## 9. Key Derived Metrics (Computed in Reports)

| Metric | Formula | Inputs |
|--------|---------|--------|
| Tokens/Joule | `total_tokens / (rapl_soc_w × wall_time_s / 3600)` | workflow_kpi + hw_samples |
| Memory-bound % | `l0_xve_stall / (l0_xve_active + l0_xve_stall) × 100` | hw_samples L0 columns |
| DRAM BW Utilization (EMON) | `dram_total_gbs / 89.0 × 100` | EMON BW / DDR5 peak (89 GB/s) |
| DRAM BW Utilization (decode) | `decode_tok_per_s × model_weight_MB/1024 / 89.0 × 100` | workflow_kpi + experiment.json |
| IA vs Device BW | `ia_dram_bw_gbs / dram_total_gbs` | EMON split |
| Theoretical Compute Demand | `decode_tok_per_s × 2 × est_params / 1e12` | workflow_kpi + experiment.json |
| iGPU Arithmetic Intensity | `output_tokens_per_s × flops_per_token / (l0_mem_read_gbs × 1e9)` | L0 + KPI |

---

## 10. Hardware Platform Reference

| Component | Spec | Peak Throughput |
|-----------|------|-----------------|
| CPU | 8 P-cores (Coyote Cove) + 16 E-cores (Arctic Wolf) | ~120 GOPS INT8 |
| iGPU | 128 XVE (EUs) @ 2.0 GHz, USM shared DDR5 | ~4.1 TFLOPS FP16 (theoretical); measured on 16 EUs below |
| NPU | Intel NPU, dedicated silicon | 35 TOPS INT8 |
| dGPU | NVIDIA RTX 5070, 12 GB GDDR7 | ~504 TOPS INT4 |
| DDR5 | 2ch × 64-bit @ 6400 MT/s (2×48 GB = 96 GB) | 102.4 GB/s theoretical, ~89 GB/s measured peak (shared CPU+iGPU+NPU) |
| PCIe | Gen5 x16 (dGPU) | ~63 GB/s per direction, ~126 GB/s bidirectional |

### iGPU Measured GEMM Throughput (2048×2048, 16 EUs, no-copy)

| Precision | Kernel | Avg Time (ms) | Avg TOPS | Peak TOPS | vs FP32 |
|-----------|--------|--------------|----------|-----------|--------|
| FP16 | oneMKL HGEMM | 154.3 | 0.1113 | 0.1116 | 1.66× |
| INT4 | Custom SYCL | 218.9 | 0.0785 | 0.0786 | 1.17× |
| FP32 | oneMKL SGEMM | 255.8 | 0.0672 | 0.0672 | 1.00× |
| TF32 | oneMKL | 255.8 | 0.0672 | 0.0672 | 1.00× |
| BF16 | oneMKL | 261.7 | 0.0657 | 0.0659 | 0.98× |
| FP64 | oneMKL DGEMM | 747.8 | 0.0230 | 0.0230 | 0.34× |
| INT8 | oneMKL | FAILED | — | — | — |

**Key findings** (16 EUs):
- FP16 is fastest (0.111 TOPS), INT4 only 1.17× over FP32 — no native INT4 datapath
- TF32 = FP32 (no dedicated TF32 acceleration)
- INT8 not supported by oneMKL on this iGPU
- Extrapolated to 128 EUs (linear, compute-bound assumption): FP16 ~0.89 TOPS, INT4 ~0.63 TOPS
- Theoretical 32.8 TOPS INT4 is unreachable (~52× above measured extrapolation)

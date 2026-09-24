"""Roofline-based hardware projection pipeline.

Projects a MEASURED kpi_runs/ experiment (this machine) onto a HYPOTHETICAL target
system (more CPU cores, iGPU XeCores, NPU MACs, memory bandwidth, ...) using a
bottom-up macro-component decomposition of workflow wall time. See
docs/ROOFLINE_HW_PROJECTION_METHODOLOGY.md for the full methodology and equations.

Modules:
  hw_spec           - SystemSpec (CPU/iGPU/NPU/memory) + derived peak-capability math
  baseline_extractor - build a per-stage macro-component profile from a kpi_runs run dir
  scaling_engine    - project a BaselineProfile onto a target SystemSpec
  report            - render an HTML + JSON projection report (baseline vs projected)
  what_if_calculator - generate a standalone interactive HTML/JS dropdown calculator
"""

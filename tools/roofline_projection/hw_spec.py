"""System hardware specification + bottom-up peak-capability / scaling-ratio math.

A SystemSpec describes the macro hardware knobs this pipeline can project across:
CPU (cores x frequency), iGPU (XeCores x frequency), NPU (MAC units x frequency),
and memory (channels x per-channel width x transfer rate). These are exactly the
knobs the user's what-if dropdown UI exposes.

None of these numbers are treated as absolute FLOPS/GB-s truth (vendor peak specs
for the exact silicon here are largely unverifiable from this benchmark - see
docs/KPI_HUB_INTEGRATION_NOTES.md). Instead everything downstream uses RATIOS
between a baseline SystemSpec and a target SystemSpec, applied to this machine's
own EMPIRICALLY MEASURED per-phase performance (see baseline_extractor.py). That
keeps every projection self-consistent and calibrated, even though the absolute
"peak capability" numbers are only relative units, not physical FLOPs/s.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path


@dataclass
class SystemSpec:
    name: str = "unnamed_system"

    cpu_cores: float = 8
    cpu_freq_ghz: float = 4.0

    igpu_xecores: float = 96
    igpu_freq_ghz: float = 2.0

    npu_macs: float = 2048
    npu_freq_ghz: float = 1.4

    mem_channels: float = 8
    mem_width_bits: float = 16
    mem_freq_mts: float = 8533

    notes: str = ""

    # ---- derived "relative capability" numbers (linear in the underlying count x freq) ----
    @property
    def cpu_capability(self) -> float:
        return self.cpu_cores * self.cpu_freq_ghz

    @property
    def igpu_capability(self) -> float:
        return self.igpu_xecores * self.igpu_freq_ghz

    @property
    def npu_capability(self) -> float:
        return self.npu_macs * self.npu_freq_ghz

    @property
    def mem_bw_peak_gbs(self) -> float:
        """Peak theoretical memory bandwidth: channels x (bus width bytes) x transfer-rate (GT/s)."""
        return self.mem_channels * (self.mem_width_bits / 8.0) * self.mem_freq_mts / 1000.0

    def compute_capability(self, device_type: str) -> float:
        """Pick the accelerator capability relevant to a stage's device_type (NPU vs GPU/iGPU)."""
        dt = (device_type or "").upper()
        if dt == "NPU":
            return self.npu_capability
        return self.igpu_capability

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SystemSpec":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)

    @classmethod
    def from_file(cls, path: str) -> "SystemSpec":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def raw_speedup(target_value: float, baseline_value: float) -> float:
    """target/baseline capability ratio, guarded against a zero/missing baseline."""
    if not baseline_value:
        return 1.0
    return target_value / baseline_value


def amdahl_speedup(cores_ratio: float, freq_ratio: float, parallel_fraction: float) -> float:
    """Amdahl's law speedup for a CPU-bound component (e.g. tool execution: git apply, pytest, ...).

    `parallel_fraction` (0-1) is the fraction of that component's time assumed to scale with
    extra cores; the rest is treated as effectively serial and only benefits from clock speed.
    Per-core throughput scales linearly with frequency in this simplified model.
    """
    per_core_speedup = max(freq_ratio, 1e-9)
    serial_fraction = 1.0 - parallel_fraction
    denom = serial_fraction + parallel_fraction / (cores_ratio * per_core_speedup)
    if denom <= 0:
        return cores_ratio * per_core_speedup
    return 1.0 / denom


def effective_speedup(raw: float, efficiency_retention: float) -> float:
    """Damp a raw (ideal) HW speedup toward reality.

    efficiency_retention in [0, 1]: 1.0 = the raw ratio is fully realized (best case, "iso
    efficiency" scaling - utilization % stays constant as the resource scales up). Lower values
    model the well-known fact that bigger systems rarely scale perfectly linearly (NUMA effects,
    contention, Amdahl-ish serial fractions elsewhere in the stack, etc). 0.0 = no benefit at all.
    """
    efficiency_retention = min(max(efficiency_retention, 0.0), 1.0)
    return 1.0 + (raw - 1.0) * efficiency_retention


def load_preset(path: str) -> SystemSpec:
    return SystemSpec.from_file(path)

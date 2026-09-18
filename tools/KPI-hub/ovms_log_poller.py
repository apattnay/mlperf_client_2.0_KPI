"""OVMS Log Poller — Extracts KV-cache usage from OVMS runtime logs.

Tails the OVMS log file (set via OVMS_LOG_PATH env var) and parses lines like:
  [llm_executor][info][llm_executor.hpp:105] All requests: 2; Scheduled requests: 2; Cache type: dynamic, cache usage: 92.7% of 139.9 MB;

Exposes latest KV-cache percentage and absolute MB as CSV columns.
"""

import os
import re
import threading
import time

# Regex to extract cache usage from OVMS log lines
_CACHE_RE = re.compile(
    r"cache usage:\s*([\d.]+)%\s*of\s*([\d.]+)\s*MB",
    re.IGNORECASE,
)

COLUMNS = ["ovms_kv_cache_pct", "ovms_kv_cache_mb"]


class OVMSLogPoller:
    """Tail OVMS log file and extract KV-cache usage metrics."""

    def __init__(self, log_path: str | None = None):
        self.log_path = log_path or os.environ.get("OVMS_LOG_PATH", "")
        self.available = bool(self.log_path) and os.path.exists(self.log_path)
        self._cache_pct = 0.0
        self._cache_mb = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._file_pos = 0

    def start(self):
        if not self.available:
            return
        # Start from end of file (only read new content)
        try:
            self._file_pos = os.path.getsize(self.log_path)
        except OSError:
            self._file_pos = 0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ovms_kv_cache_pct": round(self._cache_pct, 1),
                "ovms_kv_cache_mb": round(self._cache_mb, 1),
            }

    def _poll_loop(self):
        """Read new lines from OVMS log every 500ms."""
        while not self._stop_event.is_set():
            try:
                size = os.path.getsize(self.log_path)
                if size > self._file_pos:
                    with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self._file_pos)
                        new_data = f.read(size - self._file_pos)
                        self._file_pos = size
                    self._parse_lines(new_data)
                elif size < self._file_pos:
                    # File was truncated (rotated), reset
                    self._file_pos = 0
            except OSError:
                pass
            self._stop_event.wait(0.5)

    def _parse_lines(self, data: str):
        """Extract latest cache usage from new log data."""
        last_pct = None
        last_mb = None
        for line in data.split("\n"):
            m = _CACHE_RE.search(line)
            if m:
                last_pct = float(m.group(1))
                last_mb = float(m.group(2)) * last_pct / 100.0
        if last_pct is not None:
            with self._lock:
                self._cache_pct = last_pct
                self._cache_mb = last_mb

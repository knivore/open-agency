"""Secret-safe structured event and metric collection for the browser runtime."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_LOGGER = logging.getLogger("agency.browser_runtime")
_SENSITIVE_KEY = re.compile(r"(authorization|cookie|password|secret|token|storage_state|proxy)", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(https?://)([^/@:]+):([^/@]+)@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)(bearer|basic)\s+[a-z0-9._~+/=-]+", r"\1 [REDACTED]", value)
    return value


@dataclass(slots=True)
class BrowserTelemetry:
    counters: Counter[str] = field(default_factory=Counter)
    duration_totals_ms: dict[str, float] = field(default_factory=dict)
    duration_max_ms: dict[str, float] = field(default_factory=dict)

    def emit(self, event: str, **attributes: Any) -> None:
        self.counters[event] += 1
        payload = {"event": event, "timestamp": time.time(), **redact(attributes)}
        _LOGGER.info(json.dumps(payload, sort_keys=True, default=str))

    def observe(self, stage: str, duration_ms: float, **attributes: Any) -> None:
        self.duration_totals_ms[stage] = self.duration_totals_ms.get(stage, 0.0) + duration_ms
        self.duration_max_ms[stage] = max(duration_ms, self.duration_max_ms.get(stage, 0.0))
        self.emit(f"{stage}_observed", duration_ms=round(duration_ms, 3), **attributes)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "duration_total_ms": dict(self.duration_totals_ms),
            "duration_max_ms": dict(self.duration_max_ms),
        }


def process_tree_resources(root_pid: int | None = None) -> dict[str, int | None]:
    """Measure the runtime and Chromium descendants without adding a host metrics dependency."""
    proc_root = Path("/proc")
    root = root_pid or os.getpid()
    if not proc_root.is_dir():
        return {"process_tree_pids": 1, "process_tree_rss_bytes": None}

    parents: dict[int, int] = {}
    rss_by_pid: dict[int, int] = {}
    page_size = os.sysconf("SC_PAGE_SIZE")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The command name may contain spaces or parentheses; split after
            # its final ')' before reading the stable state/ppid fields.
            stat_tail = entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            pid = int(entry.name)
            parents[pid] = int(stat_tail[1])
            resident_pages = int(entry.joinpath("statm").read_text().split()[1])
            rss_by_pid[pid] = resident_pages * page_size
        except (IndexError, OSError, ValueError):
            # Processes can disappear between directory enumeration and read.
            continue

    descendants = {root}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    measured = descendants.intersection(rss_by_pid)
    return {
        "process_tree_pids": len(measured),
        "process_tree_rss_bytes": sum(rss_by_pid[pid] for pid in measured),
    }


def cgroup_resources() -> dict[str, int | None]:
    """Read container-level usage when cgroup v2 or legacy v1 counters are mounted."""
    candidates = {
        "cgroup_memory_current_bytes": [
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ],
        "cgroup_pids_current": [
            Path("/sys/fs/cgroup/pids.current"),
            Path("/sys/fs/cgroup/pids/pids.current"),
        ],
    }
    result: dict[str, int | None] = {}
    for name, paths in candidates.items():
        value = None
        for path in paths:
            try:
                value = int(path.read_text().strip())
                break
            except (OSError, ValueError):
                continue
        result[name] = value
    return result


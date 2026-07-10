"""Lightweight in-process runtime operations counters and recent-action log."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeOperationsSnapshot:
    counters: dict[str, int]
    recent_actions: list[dict[str, Any]]


class RuntimeOperationsRecorder:
    """Record runtime counters without coupling callers to a metrics backend."""

    def __init__(self, *, max_recent_actions: int = 50):
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._recent_actions: deque[dict[str, Any]] = deque(maxlen=max_recent_actions)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def record_action(self, action: str, **payload: Any) -> None:
        with self._lock:
            self._counters[f"action.{action}"] += 1
            self._recent_actions.append({"action": action, **payload})

    def snapshot(self) -> RuntimeOperationsSnapshot:
        with self._lock:
            return RuntimeOperationsSnapshot(
                counters=dict(self._counters),
                recent_actions=list(self._recent_actions),
            )

    def snapshot_dict(self) -> dict[str, Any]:
        return asdict(self.snapshot())

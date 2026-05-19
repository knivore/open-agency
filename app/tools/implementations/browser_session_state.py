from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BrowserActionResult:
    signals: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserSessionState:
    signals: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def record_signal(self, name: str, value: Any) -> None:
        self.signals[name] = value
        self.events.append({"type": "signal", "name": name, "value": value})

    def record_artifact(self, name: str, value: Any) -> None:
        self.artifacts[name] = value
        self.events.append({"type": "artifact", "name": name, "value": value})

    def merge_result(self, result: BrowserActionResult | None) -> None:
        if result is None:
            return
        for name, value in result.signals.items():
            self.record_signal(name, value)
        for name, value in result.artifacts.items():
            self.record_artifact(name, value)

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["BrowserActionResult", "BrowserSessionState"]

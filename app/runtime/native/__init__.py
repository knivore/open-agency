from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["ExecutionEngine"]


def __getattr__(name: str) -> Any:
    if name == "ExecutionEngine":
        return import_module("app.runtime.native.engine").ExecutionEngine
    raise AttributeError(name)

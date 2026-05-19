from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["create_a2a_router"]


def __getattr__(name: str) -> Any:
    if name == "create_a2a_router":
        return import_module("app.protocols.a2a.routes").create_a2a_router
    raise AttributeError(name)

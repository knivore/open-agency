from __future__ import annotations

from typing import Any


class ExternalExampleRuntimeToolHandler:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def run(self, tool_name: str, payload: dict[str, Any], *, actor: str | None) -> dict[str, Any]:
        return {
            "status": "ok",
            "tool": tool_name,
            "payload": payload,
            "actor": actor,
        }


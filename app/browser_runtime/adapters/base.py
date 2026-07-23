"""Async browser engine boundary; engine objects never cross this interface."""

from __future__ import annotations

from typing import Any, Protocol

from ..contracts import ActionRequest, BrowserOptions, ChallengeResult, ExtractMode, ExtractionResult
from ..proxy import ResolvedProxy


class EngineUnavailableError(RuntimeError):
    pass


class EngineNavigationError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class EngineSession(Protocol):
    engine: str
    page: Any

    async def close(self) -> None: ...


class BrowserEngineAdapter(Protocol):
    name: str
    interactive: bool

    async def start(
            self,
            *,
            options: BrowserOptions,
            proxy: ResolvedProxy | None,
            allowed_hosts: list[str],
    ) -> EngineSession: ...

    async def navigate(self, session: Any, url: str, *, timeout_ms: int) -> dict[str, Any]: ...

    async def extract(
            self,
            session: Any,
            *,
            mode: ExtractMode,
            max_chars: int,
    ) -> ExtractionResult: ...

    async def challenge(self, session: Any, *, http_status: int | None = None) -> ChallengeResult: ...

    async def action(self, session: Any, request: ActionRequest) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...

    async def shutdown(self) -> None: ...


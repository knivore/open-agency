"""Private FastAPI process hosting durable browser sessions."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .contracts import ActionRequest, ExtractRequest, OpenRequest
from .security import (
    BrowserCapability,
    BrowserCapabilityError,
    derive_execution_secret,
    peek_capability_owner,
    verify_capability,
)
from .service import BrowserRuntimeService
from .session_registry import SessionAccessError, SessionLimitError, SessionNotFoundError


class CapabilityVerifier:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self._used_nonces: dict[str, int] = {}

    def verify(self, authorization: str | None, *, operation: str) -> BrowserCapability:
        if len(self.secret) < 32:
            raise HTTPException(status_code=503, detail="Browser runtime signing secret is not configured")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Browser runtime capability is required")
        try:
            token = authorization[7:]
            unverified_owner = peek_capability_owner(token)
            verification_secret = (
                derive_execution_secret(self.secret, unverified_owner.execution_id)
                if unverified_owner.execution_id else self.secret
            )
            capability = verify_capability(verification_secret, token, operation=operation)
        except BrowserCapabilityError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        now = int(time.time())
        self._used_nonces = {nonce: expiry for nonce, expiry in self._used_nonces.items() if expiry > now}
        if capability.nonce in self._used_nonces:
            raise HTTPException(status_code=409, detail="Browser runtime capability was already used")
        self._used_nonces[capability.nonce] = capability.expires_at
        return capability


def create_app(
        *,
        service: BrowserRuntimeService | None = None,
        signing_secret: str | None = None,
) -> FastAPI:
    runtime = service or BrowserRuntimeService()
    verifier = CapabilityVerifier(signing_secret or os.getenv("BROWSER_RUNTIME_SIGNING_SECRET", ""))
    expiry_interval = max(5, int(os.getenv("BROWSER_SESSION_EXPIRY_INTERVAL_SECONDS", "30")))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()

        async def expire_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=expiry_interval)
                except TimeoutError:
                    await runtime.expire()

        task = asyncio.create_task(expire_loop())
        try:
            yield
        finally:
            stop.set()
            await task
            await runtime.shutdown()

    app = FastAPI(title="Agency Browser Runtime", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)

    def capability(operation: str):
        def resolve(authorization: str | None = Header(default=None)) -> BrowserCapability:
            return verifier.verify(authorization, operation=operation)
        return resolve

    @app.exception_handler(SessionNotFoundError)
    async def not_found_handler(_: Request, exc: SessionNotFoundError) -> Response:
        return _json_error(404, str(exc))

    @app.exception_handler(SessionAccessError)
    async def access_handler(_: Request, exc: SessionAccessError) -> Response:
        return _json_error(403, str(exc))

    @app.exception_handler(SessionLimitError)
    async def limit_handler(_: Request, exc: SessionLimitError) -> Response:
        return _json_error(429, str(exc))

    @app.get("/health")
    async def health() -> Response:
        from fastapi.responses import JSONResponse

        result = await runtime.health()
        return JSONResponse(
            status_code=503 if result.status == "unhealthy" else 200,
            content=result.model_dump(mode="json"),
        )

    @app.post("/v1/open")
    async def open_browser(
            request: OpenRequest,
            auth: BrowserCapability = Depends(capability("open")),
    ) -> dict:
        if set(request.allowed_hosts) - set(auth.allowed_hosts):
            raise HTTPException(status_code=403, detail="Requested browser hosts exceed the capability grant")
        return (await runtime.open(request, owner=auth.owner)).model_dump(mode="json")

    @app.post("/v1/sessions/{session_id}/extract")
    async def extract(
            session_id: str,
            request: ExtractRequest,
            auth: BrowserCapability = Depends(capability("extract")),
    ) -> dict:
        return (await runtime.extract(session_id, request, owner=auth.owner)).model_dump(mode="json")

    @app.post("/v1/sessions/{session_id}/actions")
    async def action(
            session_id: str,
            request: ActionRequest,
            auth: BrowserCapability = Depends(capability("action")),
    ) -> dict:
        return await runtime.action(session_id, request, owner=auth.owner)

    @app.delete("/v1/sessions/{session_id}")
    async def close(
            session_id: str,
            auth: BrowserCapability = Depends(capability("close")),
    ) -> dict:
        return await runtime.close(session_id, owner=auth.owner)

    @app.get("/v1/sessions")
    async def sessions(auth: BrowserCapability = Depends(capability("status"))) -> dict:
        return {"sessions": await runtime.status(owner=auth.owner)}

    @app.get("/v1/artifacts/{artifact_id}")
    async def artifact(
            artifact_id: str,
            auth: BrowserCapability = Depends(capability("artifact")),
    ) -> FileResponse:
        try:
            record = runtime.artifacts.get(artifact_id, owner=auth.owner)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(record.path, media_type=record.media_type, filename=record.path.name)

    @app.delete("/v1/executions/{execution_id}/sessions")
    async def close_execution(
            execution_id: str,
            auth: BrowserCapability = Depends(capability("close_execution")),
    ) -> dict:
        if auth.owner.execution_id != execution_id:
            raise HTTPException(status_code=403, detail="Capability execution does not match requested execution")
        return {"closed": await runtime.close_all_for_execution(execution_id)}

    return app


def _json_error(status_code: int, detail: str) -> Response:
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status_code, content={"detail": detail})


app = create_app()


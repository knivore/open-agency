"""FastAPI application factory and startup orchestration.

This module owns process-level concerns: middleware, route registration, seed
data, background loops, and graceful task cancellation. Business behavior is
delegated to services hanging off `ApiContext`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.api.routes import create_api_router
from app.core.config import get_settings
from app.core.time import utc_now
from app.modules.registry import validate_expected_optional_modules
from app.services.connector_installations import ConnectorInstallationService
from app.services.connector_retention import ConnectorRetentionService
from app.services.conversation_daily_summary import (
    ConversationDailySummaryService,
    DailySummaryScheduleCoordinator,
)
from app.services.conversations.discord_gateway import DiscordGatewayListenerService
from app.services.goals import GoalStartupReconciler
from app.services.execution_waits import ExecutionWaitService
from app.services.main_agent_setup.prompt_doc import extract_prompt_from_doc
from app.services.main_agent_setup.service import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.main_agent_workflow_monitor import MainAgentWorkflowMonitorService

logger = logging.getLogger(__name__)

PUBLIC_API_PATHS = {
    "/auth/bootstrap",
    "/auth/login",
    "/setup/status",
}
DOCUMENTATION_PATHS = {"/docs", "/openapi.json", "/redoc"}


def _is_public_http_request(request: Request) -> bool:
    """Keep only bootstrap, diagnostics, docs, and signed webhooks outside the API auth boundary."""

    path = request.url.path.rstrip("/") or "/"
    if request.method.upper() == "OPTIONS" or path in PUBLIC_API_PATHS:
        return True
    if path in DOCUMENTATION_PATHS and get_settings().app_env != "production":
        return True
    if path == "/health" or path.startswith("/health/"):
        return True
    return (
        request.method.upper() == "POST"
        and path.startswith("/integrations/conversations/adapters/")
        and path.endswith("/webhook")
    )


def create_app(context: ApiContext | None = None) -> FastAPI:
    """Build the API app and attach lifecycle tasks for the supplied context."""
    settings = get_settings()
    explicit_context_supplied = context is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        settings.ensure_runtime_requirements()
        _ensure_expected_optional_modules(settings.parsed_agency_expected_optional_modules)
        logger.info("OneCLI credential gateway diagnostics: %s", settings.sanitized_onecli_diagnostics)
        runtime_context = context
        workflow_scheduler_task: asyncio.Task | None = None
        execution_wait_task: asyncio.Task | None = None
        reconcile_task: asyncio.Task | None = None
        connector_retention_task: asyncio.Task | None = None
        daily_summary_task: asyncio.Task | None = None
        main_agent_monitor_task: asyncio.Task | None = None
        discord_gateway_listener_task: asyncio.Task | None = None
        runtime_context = runtime_context or getattr(app.state, "api_context", None)
        if runtime_context is not None:
            app.state.api_context = runtime_context
            await runtime_context.ensure_builtin_tool_seed_data()
            await runtime_context.ensure_builtin_mcp_servers_seed_data()
            if not explicit_context_supplied:
                interactive_startup = sys.stdin.isatty() and sys.stdout.isatty()
                setup_service = MainAgentSetupService(runtime_context)
                try:
                    await setup_service.ensure_startup_ready(
                        interactive=interactive_startup,
                        settings=settings,
                        default_agent_instructions=extract_prompt_from_doc(),
                    )
                except (
                        MainAgentModelProfileRequiredError,
                        MainAgentSetupRequiredError,
                        MainAgentSetupInvalidError,
                ) as exc:
                    raise RuntimeError(setup_service.startup_guidance(exc)) from exc
            for server_id in runtime_context.builtin_mcp_server_ids_for_startup_discovery():
                try:
                    await runtime_context.sync_mcp_catalog(server_id=server_id)
                except Exception:
                    pass
            try:
                await MainAgentSetupService(runtime_context).sync_main_agent_tool_access()
            except Exception:
                pass
            try:
                await ConnectorInstallationService(runtime_context).reconcile_startup_integrations()
            except Exception:
                logger.exception("Startup connector reconciliation failed")
            try:
                await GoalStartupReconciler(runtime_context).reconcile_once()
            except Exception:
                logger.exception("Startup goal reconciliation failed")
        runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
        app.state.api_context = runtime_context
        try:
            await ExecutionWaitService(runtime_context).wake_due_waits()
        except Exception:
            logger.exception("Startup execution wait reconciliation failed")

        execution_wait_service = ExecutionWaitService(runtime_context)

        async def execution_wait_loop() -> None:
            # Durable continuations are required for local and isolated runs, so
            # their wake cadence must not depend on the optional container reconciler.
            while True:
                try:
                    await execution_wait_service.wake_due_waits()
                except Exception:
                    logger.exception("Execution wait loop failed")
                await asyncio.sleep(settings.execution_wait_poll_interval_seconds)

        execution_wait_task = asyncio.create_task(execution_wait_loop())
        if settings.workflow_scheduler_enabled:
            runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
            app.state.api_context = runtime_context
            if not runtime_context.scheduler.fire_claim_support_available():
                logger.warning(
                    "Workflow scheduler fire-claim support is unavailable; due schedules may duplicate across workers"
                )
                runtime_context.runtime_operations.increment("scheduler.fire_claim_support_missing")

            async def workflow_scheduler_loop() -> None:
                while True:
                    try:
                        await runtime_context.scheduler.run_due_schedules()
                    except Exception:
                        logger.exception("Workflow scheduler loop failed")
                    await asyncio.sleep(settings.workflow_scheduler_interval_seconds)

            workflow_scheduler_task = asyncio.create_task(workflow_scheduler_loop())
        if settings.main_agent_workflow_monitor_enabled:
            runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
            app.state.api_context = runtime_context
            monitor_service = MainAgentWorkflowMonitorService(runtime_context, settings=settings)

            async def main_agent_monitor_loop() -> None:
                while True:
                    try:
                        result = await monitor_service.run_once()
                        runtime_context.runtime_operations.record_action(
                            "main_agent_monitor.tick",
                            occurred_at=utc_now().isoformat(),
                            finding_count=result.get("finding_count", 0),
                            proposal_count=result.get("proposal_count", 0),
                            approval_request_count=result.get("approval_request_count", 0),
                            steering_request_count=result.get("steering_request_count", 0),
                        )
                    except Exception as exc:
                        runtime_context.runtime_operations.record_action(
                            "main_agent_monitor.tick_failed",
                            occurred_at=utc_now().isoformat(),
                            error=str(exc),
                        )
                        logger.exception("Main-agent workflow monitor loop failed")
                    await asyncio.sleep(settings.main_agent_workflow_monitor_interval_seconds)

            main_agent_monitor_task = asyncio.create_task(main_agent_monitor_loop())
        if settings.runtime_reconciler_enabled:
            runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
            app.state.api_context = runtime_context

            async def reconcile_loop() -> None:
                while True:
                    try:
                        await runtime_context.runtime_reconciler.reconcile_once()
                    except Exception:
                        logger.exception("Runtime reconciliation loop failed")
                    await asyncio.sleep(settings.runtime_reconciler_interval_seconds)

            reconcile_task = asyncio.create_task(reconcile_loop())
        if settings.connector_health_history_retention_enabled:
            runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
            app.state.api_context = runtime_context
            connector_retention_service = ConnectorRetentionService(runtime_context)

            async def connector_retention_loop() -> None:
                while True:
                    try:
                        await connector_retention_service.run_once(settings)
                    except Exception:
                        pass
                    await asyncio.sleep(settings.connector_health_history_retention_interval_seconds)

            connector_retention_task = asyncio.create_task(connector_retention_loop())
        if settings.memory_daily_summary_enabled:
            runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
            app.state.api_context = runtime_context
            summary_service = ConversationDailySummaryService(runtime_context)
            coordinator = DailySummaryScheduleCoordinator(
                timezone_name=settings.memory_daily_summary_timezone,
                target_hour=settings.memory_daily_summary_target_hour,
                target_minute=settings.memory_daily_summary_target_minute,
            )

            async def daily_summary_loop() -> None:
                last_completed_target_date = None
                while True:
                    try:
                        target_date = coordinator.due_target_date(
                            last_completed_target_date=last_completed_target_date,
                        )
                        if target_date is not None:
                            result = await summary_service.summarize_day(
                                target_date=target_date,
                                timezone_name=settings.memory_daily_summary_timezone,
                            )
                            if result.get("status") in {"ok", "partial"}:
                                last_completed_target_date = target_date
                    except Exception:
                        pass
                    await asyncio.sleep(settings.memory_daily_summary_interval_seconds)

            daily_summary_task = asyncio.create_task(daily_summary_loop())
        runtime_context = runtime_context or getattr(app.state, "api_context", None) or get_default_api_context()
        app.state.api_context = runtime_context
        # Discord ordinary chat is backend-owned transport behavior, so the
        # listener should follow active integrations instead of a separate env
        # toggle. The service self-discovers credentials and idles when none are
        # installed.
        discord_gateway_listener = DiscordGatewayListenerService(runtime_context, settings=settings)
        discord_gateway_listener_task = asyncio.create_task(discord_gateway_listener.run_forever())
        try:
            yield
        finally:
            if workflow_scheduler_task is not None:
                workflow_scheduler_task.cancel()
                try:
                    await workflow_scheduler_task
                except asyncio.CancelledError:
                    pass
            if execution_wait_task is not None:
                execution_wait_task.cancel()
                try:
                    await execution_wait_task
                except asyncio.CancelledError:
                    pass
            if reconcile_task is not None:
                reconcile_task.cancel()
                try:
                    await reconcile_task
                except asyncio.CancelledError:
                    pass
            if connector_retention_task is not None:
                connector_retention_task.cancel()
                try:
                    await connector_retention_task
                except asyncio.CancelledError:
                    pass
            if daily_summary_task is not None:
                daily_summary_task.cancel()
                try:
                    await daily_summary_task
                except asyncio.CancelledError:
                    pass
            if main_agent_monitor_task is not None:
                main_agent_monitor_task.cancel()
                try:
                    await main_agent_monitor_task
                except asyncio.CancelledError:
                    pass
            if discord_gateway_listener_task is not None:
                discord_gateway_listener_task.cancel()
                try:
                    await discord_gateway_listener_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="Agency API",
        description="Backend API for defining and running agents, tools, workflows, schedules, and executions.",
        version="0.1.0",
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None if settings.app_env == "production" else "/redoc",
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
        swagger_ui_parameters={
            "displayRequestDuration": True,
            "persistAuthorization": True,
        },
        lifespan=lifespan,
    )
    if context is not None:
        app.state.api_context = context

    if settings.app_env == "production":
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.parsed_agency_allowed_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=settings.agency_cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_api_identity(request: Request, call_next):  # noqa: ANN001
        # Tests exercise route behavior with lightweight in-memory contexts. Real
        # development and production deployments fail closed at one shared edge.
        if settings.app_env == "test" or _is_public_http_request(request):
            return await call_next(request)

        runtime_context = context or getattr(request.app.state, "api_context", None) or get_default_api_context()
        try:
            request.state.authenticated_user = await resolve_current_user(request, runtime_context)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):  # noqa: ANN001
        logger.exception("Unhandled API exception", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.include_router(create_api_router(context))
    return app


def _ensure_expected_optional_modules(expected_modules: list[str]) -> None:
    if not expected_modules:
        return
    errors = validate_expected_optional_modules(expected_modules)
    if errors:
        raise RuntimeError("Optional module expectation check failed: " + "; ".join(errors))

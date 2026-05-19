from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.context import ApiContext, get_default_api_context
from app.api.routes import create_api_router
from app.core.config import get_settings
from app.services.conversation_daily_summary import (
    ConversationDailySummaryService,
    DailySummaryScheduleCoordinator,
)
from app.services.main_agent_setup import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.main_agent_setup.prompt_doc import extract_prompt_from_doc
from app.services.main_agent_workflow_monitor import MainAgentWorkflowMonitorService


logger = logging.getLogger(__name__)


def create_app(context: ApiContext | None = None) -> FastAPI:
    settings = get_settings()
    explicit_context_supplied = context is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        settings.ensure_runtime_requirements()
        runtime_context = context
        workflow_scheduler_task: asyncio.Task | None = None
        reconcile_task: asyncio.Task | None = None
        daily_summary_task: asyncio.Task | None = None
        main_agent_monitor_task: asyncio.Task | None = None
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
            for server_id in runtime_context.builtin_computer_use_server_ids_for_host():
                try:
                    await runtime_context.sync_mcp_catalog(server_id=server_id)
                except Exception:
                    pass
            try:
                await MainAgentSetupService(runtime_context).sync_main_agent_tool_access()
            except Exception:
                pass
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
                        await monitor_service.run_once()
                    except Exception:
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
                        pass
                    await asyncio.sleep(settings.runtime_reconciler_interval_seconds)

            reconcile_task = asyncio.create_task(reconcile_loop())
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
        try:
            yield
        finally:
            if workflow_scheduler_task is not None:
                workflow_scheduler_task.cancel()
                try:
                    await workflow_scheduler_task
                except asyncio.CancelledError:
                    pass
            if reconcile_task is not None:
                reconcile_task.cancel()
                try:
                    await reconcile_task
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

    app = FastAPI(
        title="Agency API",
        description="Backend API for defining and running agents, tools, workflows, schedules, and executions.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        swagger_ui_parameters={
            "displayRequestDuration": True,
            "persistAuthorization": True,
        },
        lifespan=lifespan,
    )
    if context is not None:
        app.state.api_context = context

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):  # noqa: ANN001
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    app.include_router(create_api_router(context))
    return app

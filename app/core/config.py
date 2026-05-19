from __future__ import annotations

import json
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = Field(default="development", alias="APP_ENV")
    agency_backend_run_mode: str = Field(default="docker", alias="AGENCY_BACKEND_RUN_MODE")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    execution_runtime_database_url: str | None = Field(default=None, alias="EXECUTION_RUNTIME_DATABASE_URL")
    database_echo: bool = Field(default=False, alias="DATABASE_ECHO")
    database_pool_size: int = Field(default=5, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=10, alias="DATABASE_MAX_OVERFLOW")
    require_database: bool = Field(default=False, alias="REQUIRE_DATABASE")
    integrations_runtime_enabled: bool = Field(default=False, alias="INTEGRATIONS_RUNTIME_ENABLED")
    execution_isolation_enabled: bool = Field(default=False, alias="EXECUTION_ISOLATION_ENABLED")
    runtime_revision_shadow_mode: bool = Field(default=False, alias="RUNTIME_REVISION_SHADOW_MODE")
    cancel_outdated_executions: bool = Field(default=False, alias="CANCEL_OUTDATED_EXECUTIONS")
    workflow_scheduler_enabled: bool = Field(default=False, alias="WORKFLOW_SCHEDULER_ENABLED")
    workflow_scheduler_interval_seconds: int = Field(default=30, alias="WORKFLOW_SCHEDULER_INTERVAL_SECONDS")
    workflow_restart_active_executions_on_revision_change: bool = Field(
        default=False,
        alias="WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE",
    )
    runtime_reconciler_enabled: bool = Field(default=False, alias="RUNTIME_RECONCILER_ENABLED")
    runtime_reconciler_interval_seconds: int = Field(default=30, alias="RUNTIME_RECONCILER_INTERVAL_SECONDS")
    runtime_max_concurrent_builds: int = Field(default=1, alias="RUNTIME_MAX_CONCURRENT_BUILDS")
    runtime_image_retention_count: int = Field(default=10, alias="RUNTIME_IMAGE_RETENTION_COUNT")
    runtime_container_ttl_seconds: int = Field(default=86400, alias="RUNTIME_CONTAINER_TTL_SECONDS")
    execution_runtime_base_image: str = Field(default="agency-runtime-base:latest",
                                              alias="EXECUTION_RUNTIME_BASE_IMAGE")
    execution_container_network: str = Field(default="bridge", alias="EXECUTION_CONTAINER_NETWORK")
    execution_container_workdir: str = Field(default="/app", alias="EXECUTION_CONTAINER_WORKDIR")
    execution_container_memory_limit_mb: int = Field(default=512, alias="EXECUTION_CONTAINER_MEMORY_LIMIT_MB")
    execution_container_cpu_limit: float = Field(default=1.0, alias="EXECUTION_CONTAINER_CPU_LIMIT")
    execution_container_auto_remove: bool = Field(default=False, alias="EXECUTION_CONTAINER_AUTO_REMOVE")
    execution_container_bind_integrations_read_only: bool = Field(
        default=True,
        alias="EXECUTION_CONTAINER_BIND_INTEGRATIONS_READ_ONLY",
    )
    execution_container_extra_mounts: str | None = Field(default=None, alias="EXECUTION_CONTAINER_EXTRA_MOUNTS")
    main_agent_bootstrap_enabled: bool = Field(default=False, alias="MAIN_AGENT_BOOTSTRAP_ENABLED")
    main_agent_bootstrap_existing_model_profile_id: str | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_EXISTING_MODEL_PROFILE_ID",
    )
    main_agent_bootstrap_provider_family: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY")
    main_agent_bootstrap_provider_name: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_PROVIDER_NAME")
    main_agent_bootstrap_base_url: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_BASE_URL")
    main_agent_bootstrap_api_key: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_API_KEY")
    main_agent_bootstrap_profile_name: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_PROFILE_NAME")
    main_agent_bootstrap_model_name: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_MODEL_NAME")
    main_agent_bootstrap_temperature: float | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_TEMPERATURE")
    main_agent_bootstrap_max_tokens: int | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_MAX_TOKENS")
    main_agent_bootstrap_agent_name: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_AGENT_NAME")
    main_agent_bootstrap_agent_description: str | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_AGENT_DESCRIPTION",
    )
    main_agent_bootstrap_agent_instructions: str | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_AGENT_INSTRUCTIONS",
    )
    main_agent_bootstrap_workflow_name: str | None = Field(default=None, alias="MAIN_AGENT_BOOTSTRAP_WORKFLOW_NAME")
    main_agent_bootstrap_workflow_description: str | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_WORKFLOW_DESCRIPTION",
    )
    main_agent_bootstrap_can_trigger_workflows: bool | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_CAN_TRIGGER_WORKFLOWS",
    )
    main_agent_bootstrap_can_create_workflows: bool | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_CAN_CREATE_WORKFLOWS",
    )
    main_agent_bootstrap_can_update_workflows: bool | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_CAN_UPDATE_WORKFLOWS",
    )
    main_agent_bootstrap_require_approval_for_mutations: bool | None = Field(
        default=None,
        alias="MAIN_AGENT_BOOTSTRAP_REQUIRE_APPROVAL_FOR_MUTATIONS",
    )
    main_agent_workflow_mutation_enabled: bool = Field(
        default=True,
        alias="MAIN_AGENT_WORKFLOW_MUTATION_ENABLED",
    )
    main_agent_tool_mutation_enabled: bool = Field(default=True, alias="MAIN_AGENT_TOOL_MUTATION_ENABLED")
    main_agent_external_channel_daily_message_budget: int = Field(
        default=100,
        alias="MAIN_AGENT_EXTERNAL_CHANNEL_DAILY_MESSAGE_BUDGET",
    )
    main_agent_workflow_monitor_enabled: bool = Field(
        default=False,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_ENABLED",
    )
    main_agent_workflow_monitor_default_enabled: bool = Field(
        default=True,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_DEFAULT_ENABLED",
    )
    main_agent_workflow_monitor_interval_seconds: int = Field(
        default=60,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_INTERVAL_SECONDS",
    )
    main_agent_workflow_monitor_stale_after_seconds: int = Field(
        default=300,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_STALE_AFTER_SECONDS",
    )
    main_agent_workflow_monitor_terminal_lookback_seconds: int = Field(
        default=86400,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_TERMINAL_LOOKBACK_SECONDS",
    )
    main_agent_workflow_monitor_finding_retention_days: int = Field(
        default=60,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_FINDING_RETENTION_DAYS",
    )
    llm_request_timeout_seconds: float = Field(default=15.0, alias="LLM_REQUEST_TIMEOUT_SECONDS")
    memory_vector_retrieval_enabled: bool = Field(default=True, alias="MEMORY_VECTOR_RETRIEVAL_ENABLED")
    memory_embedding_model_profile_id: str | None = Field(default=None, alias="MEMORY_EMBEDDING_MODEL_PROFILE_ID")
    memory_embedding_write_errors_strict: bool = Field(
        default=False,
        alias="MEMORY_EMBEDDING_WRITE_ERRORS_STRICT",
    )
    memory_retrieval_v2_enabled: bool = Field(default=False, alias="MEMORY_RETRIEVAL_V2_ENABLED")
    memory_daily_summary_enabled: bool = Field(default=False, alias="MEMORY_DAILY_SUMMARY_ENABLED")
    memory_daily_summary_timezone: str = Field(default="UTC", alias="MEMORY_DAILY_SUMMARY_TIMEZONE")
    memory_daily_summary_interval_seconds: int = Field(
        default=3600,
        alias="MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS",
    )
    memory_daily_summary_target_hour: int = Field(default=0, alias="MEMORY_DAILY_SUMMARY_TARGET_HOUR")
    memory_daily_summary_target_minute: int = Field(default=15, alias="MEMORY_DAILY_SUMMARY_TARGET_MINUTE")
    agent_persistent_run_summary_enabled: bool = Field(default=False, alias="AGENT_PERSISTENT_RUN_SUMMARY_ENABLED")
    agency_internal_api_key: str | None = Field(default=None, alias="AGENCY_INTERNAL_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_api_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_API_BASE_URL")
    openai_audio_transcription_model: str = Field(default="whisper-1", alias="OPENAI_AUDIO_TRANSCRIPTION_MODEL")
    openai_realtime_transcription_model: str = Field(
        default="whisper-1",
        alias="OPENAI_REALTIME_TRANSCRIPTION_MODEL",
    )
    sandbox_edit_allowed_repos: str = Field(
        default=(
            "/Users/kehchinleong/Documents/Personal/Agency/agency,"
            "/Users/kehchinleong/Documents/Personal/Agency/agency-fe"
        ),
        alias="SANDBOX_EDIT_ALLOWED_REPOS",
    )
    tool_file_write_allowed_dirs: str = Field(
        default=(
            "/Users/kehchinleong/Documents/Personal/Agency/agency,"
            "/Users/kehchinleong/Documents/Personal/Agency/agency-fe"
        ),
        alias="TOOL_FILE_WRITE_ALLOWED_DIRS",
    )
    tool_http_allowed_hosts: str = Field(default="*", alias="TOOL_HTTP_ALLOWED_HOSTS")
    tool_run_store_path: str = Field(default=".data/executions/tool_runs.jsonl", alias="TOOL_RUN_STORE_PATH")

    @property
    def database_enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def database_required(self) -> bool:
        return self.require_database or self.app_env == "production"

    @property
    def sqlalchemy_database_url(self) -> str | None:
        if not self.database_url:
            return None
        if self.database_url.startswith("postgresql+"):
            return self.database_url
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+asyncpg://", 1)
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if self.database_url.startswith("sqlite+"):
            return self.database_url
        if self.database_url.startswith("sqlite:///"):
            return self.database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if self.database_url.startswith("sqlite://"):
            return self.database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return self.database_url

    @property
    def container_database_url(self) -> str | None:
        return self.execution_runtime_database_url or self.database_url

    @property
    def parsed_execution_container_extra_mounts(self) -> list[dict[str, object]]:
        if not self.execution_container_extra_mounts:
            return []
        try:
            payload = json.loads(self.execution_container_extra_mounts)
        except json.JSONDecodeError as exc:  # pragma: no cover - validated by tests/settings users
            raise RuntimeError("EXECUTION_CONTAINER_EXTRA_MOUNTS must be valid JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("EXECUTION_CONTAINER_EXTRA_MOUNTS must decode to a list")
        mounts: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("Each EXECUTION_CONTAINER_EXTRA_MOUNTS entry must be an object")
            source = item.get("source")
            target = item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                raise RuntimeError("Each EXECUTION_CONTAINER_EXTRA_MOUNTS entry requires string source and target")
            mounts.append(
                {
                    "source": source,
                    "target": target,
                    "read_only": bool(item.get("read_only", True)),
                }
            )
        return mounts

    @property
    def parsed_sandbox_edit_allowed_repos(self) -> list[str]:
        return [item.strip() for item in self.sandbox_edit_allowed_repos.split(",") if item.strip()]

    @property
    def parsed_tool_file_write_allowed_dirs(self) -> list[str]:
        return [item.strip() for item in self.tool_file_write_allowed_dirs.split(",") if item.strip()]

    @property
    def parsed_tool_http_allowed_hosts(self) -> list[str]:
        return [item.strip().lower() for item in self.tool_http_allowed_hosts.split(",") if item.strip()]

    def ensure_runtime_requirements(self) -> None:
        if self.database_required and not self.database_enabled:
            raise RuntimeError("DATABASE_URL must be configured when the database is required")
        if self.runtime_reconciler_interval_seconds <= 0:
            raise RuntimeError("RUNTIME_RECONCILER_INTERVAL_SECONDS must be greater than zero")
        if self.memory_daily_summary_interval_seconds <= 0:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS must be greater than zero")
        if self.workflow_scheduler_interval_seconds <= 0:
            raise RuntimeError("WORKFLOW_SCHEDULER_INTERVAL_SECONDS must be greater than zero")
        if self.memory_daily_summary_target_hour < 0 or self.memory_daily_summary_target_hour > 23:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_TARGET_HOUR must be between 0 and 23")
        if self.memory_daily_summary_target_minute < 0 or self.memory_daily_summary_target_minute > 59:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_TARGET_MINUTE must be between 0 and 59")
        if self.runtime_max_concurrent_builds <= 0:
            raise RuntimeError("RUNTIME_MAX_CONCURRENT_BUILDS must be greater than zero")
        if self.runtime_image_retention_count <= 0:
            raise RuntimeError("RUNTIME_IMAGE_RETENTION_COUNT must be greater than zero")
        if self.runtime_container_ttl_seconds <= 0:
            raise RuntimeError("RUNTIME_CONTAINER_TTL_SECONDS must be greater than zero")
        if self.main_agent_external_channel_daily_message_budget < 0:
            raise RuntimeError("MAIN_AGENT_EXTERNAL_CHANNEL_DAILY_MESSAGE_BUDGET must be zero or greater")
        if self.main_agent_workflow_monitor_interval_seconds <= 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_INTERVAL_SECONDS must be greater than zero")
        if self.main_agent_workflow_monitor_stale_after_seconds <= 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_STALE_AFTER_SECONDS must be greater than zero")
        if self.main_agent_workflow_monitor_terminal_lookback_seconds < 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_TERMINAL_LOOKBACK_SECONDS must be zero or greater")
        if self.main_agent_workflow_monitor_finding_retention_days <= 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_FINDING_RETENTION_DAYS must be greater than zero")
        if self.execution_container_memory_limit_mb <= 0:
            raise RuntimeError("EXECUTION_CONTAINER_MEMORY_LIMIT_MB must be greater than zero")
        if self.execution_container_cpu_limit <= 0:
            raise RuntimeError("EXECUTION_CONTAINER_CPU_LIMIT must be greater than zero")
        _ = self.parsed_execution_container_extra_mounts
        if self.execution_isolation_enabled and not self.integrations_runtime_enabled:
            raise RuntimeError(
                "INTEGRATIONS_RUNTIME_ENABLED must be enabled when EXECUTION_ISOLATION_ENABLED is enabled")
        if self.runtime_revision_shadow_mode and not self.integrations_runtime_enabled:
            raise RuntimeError(
                "INTEGRATIONS_RUNTIME_ENABLED must be enabled when RUNTIME_REVISION_SHADOW_MODE is enabled")
        if self.cancel_outdated_executions and not self.execution_isolation_enabled:
            raise RuntimeError("CANCEL_OUTDATED_EXECUTIONS requires EXECUTION_ISOLATION_ENABLED")
        if self.runtime_reconciler_enabled and not self.execution_isolation_enabled:
            raise RuntimeError("RUNTIME_RECONCILER_ENABLED requires EXECUTION_ISOLATION_ENABLED")
        if self.app_env == "production":
            if self.execution_isolation_enabled and self.execution_container_network == "host":
                raise RuntimeError("Host networking is not allowed when execution isolation is enabled in production")
            if self.execution_isolation_enabled and not self.execution_container_bind_integrations_read_only:
                raise RuntimeError(
                    "Integrations mounts must be read-only when execution isolation is enabled in production")
            if self.cancel_outdated_executions and not self.runtime_reconciler_enabled:
                raise RuntimeError(
                    "RUNTIME_RECONCILER_ENABLED must be enabled before CANCEL_OUTDATED_EXECUTIONS in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

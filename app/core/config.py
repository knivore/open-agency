"""Environment-backed backend settings.

`Settings` centralizes runtime flags, database connectivity, model/provider
configuration, scheduler intervals, memory behavior, OneCLI safety policy, and
execution isolation options. Keep new environment variables documented here and
in the maintained docs checked by `tests.test_documentation_consistency`.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal
from urllib.parse import urlsplit

LOCAL_DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
POSTGRES_DATABASE_SCHEMES = {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}


class Settings(BaseSettings):
    """Typed view of supported backend environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = Field(default="development", alias="APP_ENV")
    agency_backend_run_mode: str = Field(default="docker", alias="AGENCY_BACKEND_RUN_MODE")
    agency_allowed_origins: str = Field(default="", alias="AGENCY_ALLOWED_ORIGINS")
    agency_cors_allow_credentials: bool = Field(default=True, alias="AGENCY_CORS_ALLOW_CREDENTIALS")
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
    agency_builtin_optional_modules: str = Field(
        default="",
        alias="AGENCY_BUILTIN_OPTIONAL_MODULES",
    )
    agency_optional_module_spec_refs: str = Field(default="", alias="AGENCY_OPTIONAL_MODULE_SPEC_REFS")
    agency_optional_module_entry_points_enabled: bool = Field(
        default=False,
        alias="AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED",
    )
    agency_expected_optional_modules: str = Field(default="", alias="AGENCY_EXPECTED_OPTIONAL_MODULES")
    workflow_scheduler_enabled: bool = Field(default=False, alias="WORKFLOW_SCHEDULER_ENABLED")
    workflow_scheduler_interval_seconds: int = Field(default=30, alias="WORKFLOW_SCHEDULER_INTERVAL_SECONDS")
    # Durable waits are checked independently of the workflow scheduler so a
    # resumed execution does not need to wait for the scheduler's cadence.
    execution_wait_poll_interval_seconds: float = Field(
        default=1.0,
        alias="EXECUTION_WAIT_POLL_INTERVAL_SECONDS",
    )
    workflow_restart_active_executions_on_revision_change: bool = Field(
        default=False,
        alias="WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE",
    )
    graph_projection_enabled: bool = Field(default=True, alias="GRAPH_PROJECTION_ENABLED")
    graph_entity_extraction_enabled: bool = Field(default=False, alias="GRAPH_ENTITY_EXTRACTION_ENABLED")
    graph_entity_extraction_min_confidence: float = Field(
        default=0.7,
        alias="GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE",
    )
    graph_document_projection_max_chunks: int = Field(
        default=500,
        alias="GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS",
    )
    agency_graph_context_tools_enabled: bool = Field(default=True, alias="AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED")
    agency_graph_context_query_timeout_seconds: float = Field(
        default=5.0,
        alias="AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS",
    )
    agency_graph_context_rate_limit_window_seconds: float = Field(
        default=60.0,
        alias="AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS",
    )
    agency_graph_context_rate_limit_max_units: int = Field(
        default=5000,
        alias="AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS",
    )
    graph_context_auto_retrieval_enabled: bool = Field(
        default=False,
        alias="GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
    )
    graph_context_subagent_steering_enabled: bool = Field(
        default=False,
        alias="GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED",
    )
    graph_context_coding_agent_resume_enabled: bool = Field(
        default=False,
        alias="GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED",
    )
    graph_context_loop_guard_enabled: bool = Field(
        default=True,
        alias="GRAPH_CONTEXT_LOOP_GUARD_ENABLED",
    )
    neo4j_enabled: bool = Field(default=False, alias="NEO4J_ENABLED")
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="agency-neo4j-password", alias="NEO4J_PASSWORD")
    neo4j_database: str | None = Field(default=None, alias="NEO4J_DATABASE")
    runtime_reconciler_enabled: bool = Field(default=False, alias="RUNTIME_RECONCILER_ENABLED")
    runtime_reconciler_interval_seconds: int = Field(default=30, alias="RUNTIME_RECONCILER_INTERVAL_SECONDS")
    connector_health_history_retention_enabled: bool = Field(
        default=False,
        alias="CONNECTOR_HEALTH_HISTORY_RETENTION_ENABLED",
    )
    connector_health_history_retention_interval_seconds: int = Field(
        default=3600,
        alias="CONNECTOR_HEALTH_HISTORY_RETENTION_INTERVAL_SECONDS",
    )
    connector_health_history_retention_days: int = Field(
        default=30,
        alias="CONNECTOR_HEALTH_HISTORY_RETENTION_DAYS",
    )
    connector_health_history_retention_max_per_credential: int = Field(
        default=20,
        alias="CONNECTOR_HEALTH_HISTORY_RETENTION_MAX_PER_CREDENTIAL",
    )
    agency_public_webhook_base_url: str | None = Field(
        default=None,
        alias="AGENCY_PUBLIC_WEBHOOK_BASE_URL",
    )
    agency_runtime_secret_key: str | None = Field(
        default=None,
        alias="AGENCY_RUNTIME_SECRET_KEY",
    )
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
    discord_gateway_listener_enabled: bool = Field(
        default=False,
        alias="DISCORD_GATEWAY_LISTENER_ENABLED",
    )
    discord_gateway_bot_token: str | None = Field(
        default=None,
        alias="DISCORD_GATEWAY_BOT_TOKEN",
    )
    discord_gateway_credential_id: str | None = Field(
        default=None,
        alias="DISCORD_GATEWAY_CREDENTIAL_ID",
    )
    discord_gateway_mention_only: bool = Field(
        default=True,
        alias="DISCORD_GATEWAY_MENTION_ONLY",
    )
    discord_gateway_reconnect_delay_seconds: float = Field(
        default=5.0,
        alias="DISCORD_GATEWAY_RECONNECT_DELAY_SECONDS",
    )
    main_agent_workflow_monitor_enabled: bool = Field(
        default=True,
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
    agent_activity_idle_timeout_seconds: int = Field(default=600, alias="AGENT_ACTIVITY_IDLE_TIMEOUT_SECONDS")
    agent_run_timeout_seconds: int = Field(default=7200, alias="AGENT_RUN_TIMEOUT_SECONDS")
    main_agent_workflow_monitor_terminal_lookback_seconds: int = Field(
        default=86400,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_TERMINAL_LOOKBACK_SECONDS",
    )
    main_agent_workflow_monitor_finding_retention_days: int = Field(
        default=60,
        alias="MAIN_AGENT_WORKFLOW_MONITOR_FINDING_RETENTION_DAYS",
    )
    agent_token_budget_warn_ratio: float = Field(default=0.8, alias="AGENT_TOKEN_BUDGET_WARN_RATIO")
    agent_token_budget_hard_ratio: float = Field(default=1.0, alias="AGENT_TOKEN_BUDGET_HARD_RATIO")
    agent_run_total_token_budget: int | None = Field(default=None, alias="AGENT_RUN_TOTAL_TOKEN_BUDGET")
    agent_token_budget_action: Literal[
        "warn_only",
        "compact_context",
        "pause_execution",
        "fail_execution",
    ] = Field(default="warn_only", alias="AGENT_TOKEN_BUDGET_ACTION")
    agent_context_compaction_persist_context_pack_default: bool = Field(
        default=False,
        alias="AGENT_CONTEXT_COMPACTION_PERSIST_CONTEXT_PACK_DEFAULT",
    )
    llm_request_timeout_seconds: float = Field(default=15.0, alias="LLM_REQUEST_TIMEOUT_SECONDS")
    # Local Ollama/OpenAI-compatible endpoints are intentionally opt-in by
    # host, keeping provider URL validation fail-closed for arbitrary hosts.
    model_provider_allowed_hosts: str = Field(
        default="host.docker.internal",
        alias="MODEL_PROVIDER_ALLOWED_HOSTS",
    )
    codex_cli_timeout_seconds: int = Field(default=1800, alias="CODEX_CLI_TIMEOUT_SECONDS")
    memory_vector_retrieval_enabled: bool = Field(default=True, alias="MEMORY_VECTOR_RETRIEVAL_ENABLED")
    memory_embedding_model_profile_id: str | None = Field(default=None, alias="MEMORY_EMBEDDING_MODEL_PROFILE_ID")
    memory_embedding_write_errors_strict: bool = Field(
        default=False,
        alias="MEMORY_EMBEDDING_WRITE_ERRORS_STRICT",
    )
    memory_retrieval_v2_enabled: bool = Field(default=False, alias="MEMORY_RETRIEVAL_V2_ENABLED")
    memory_context_pack_enabled: bool = Field(default=True, alias="MEMORY_CONTEXT_PACK_ENABLED")
    memory_context_pack_auto_create_enabled: bool = Field(
        default=False,
        alias="MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED",
    )
    memory_context_pack_prompt_injection_enabled: bool = Field(
        default=False,
        alias="MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED",
    )
    memory_context_pack_prompt_limit: int = Field(default=1, alias="MEMORY_CONTEXT_PACK_PROMPT_LIMIT")
    memory_context_pack_history_compaction_enabled: bool = Field(
        default=False,
        alias="MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED",
    )
    memory_context_pack_history_recent_messages: int = Field(
        default=12,
        alias="MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES",
    )
    memory_context_pack_history_min_messages: int = Field(
        default=40,
        alias="MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES",
    )
    memory_context_pack_history_max_raw_tokens: int = Field(
        default=8000,
        alias="MEMORY_CONTEXT_PACK_HISTORY_MAX_RAW_TOKENS",
    )
    persona_factory_max_documents_per_run: int = Field(
        default=25,
        alias="PERSONA_FACTORY_MAX_DOCUMENTS_PER_RUN",
    )
    persona_factory_max_source_memories_per_run: int = Field(
        default=250,
        alias="PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN",
    )
    persona_factory_max_source_characters_per_run: int = Field(
        default=300_000,
        alias="PERSONA_FACTORY_MAX_SOURCE_CHARACTERS_PER_RUN",
    )
    persona_factory_default_distillation_mode: str = Field(
        default="llm",
        alias="PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE",
    )
    persona_factory_default_llm_model_source: str = Field(
        default="main_agent",
        alias="PERSONA_FACTORY_DEFAULT_LLM_MODEL_SOURCE",
    )
    persona_factory_llm_distillation_enabled: bool = Field(
        default=True,
        alias="PERSONA_FACTORY_LLM_DISTILLATION_ENABLED",
    )
    persona_factory_hybrid_distillation_enabled: bool = Field(
        default=True,
        alias="PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED",
    )
    persona_factory_llm_max_source_memories_per_run: int = Field(
        default=100,
        alias="PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN",
    )
    persona_factory_llm_max_source_characters_per_run: int = Field(
        default=120_000,
        alias="PERSONA_FACTORY_LLM_MAX_SOURCE_CHARACTERS_PER_RUN",
    )
    persona_factory_llm_max_source_tokens_per_run: int = Field(
        default=30_000,
        alias="PERSONA_FACTORY_LLM_MAX_SOURCE_TOKENS_PER_RUN",
    )
    persona_factory_llm_max_calls_per_run: int = Field(
        default=100,
        alias="PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN",
    )
    persona_factory_llm_timeout_seconds: float = Field(
        default=15.0,
        alias="PERSONA_FACTORY_LLM_TIMEOUT_SECONDS",
    )
    persona_factory_llm_retry_attempts: int = Field(
        default=0,
        alias="PERSONA_FACTORY_LLM_RETRY_ATTEMPTS",
    )
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
    agency_vision_provider: str = Field(default="local", alias="AGENCY_VISION_PROVIDER")
    agency_vision_model: str | None = Field(default=None, alias="AGENCY_VISION_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_api_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_API_BASE_URL")
    openai_audio_transcription_model: str = Field(default="whisper-1", alias="OPENAI_AUDIO_TRANSCRIPTION_MODEL")
    openai_realtime_transcription_model: str = Field(
        default="whisper-1",
        alias="OPENAI_REALTIME_TRANSCRIPTION_MODEL",
    )
    baidu_ocr_api_key: str | None = Field(default=None, alias="BAIDU_OCR_API_KEY", repr=False)
    baidu_ocr_secret_key: str | None = Field(default=None, alias="BAIDU_OCR_SECRET_KEY", repr=False)
    sandbox_edit_allowed_repos: str = Field(
        default="",
        alias="SANDBOX_EDIT_ALLOWED_REPOS",
    )
    tool_file_write_allowed_dirs: str = Field(
        default="",
        alias="TOOL_FILE_WRITE_ALLOWED_DIRS",
    )
    tool_http_allowed_hosts: str = Field(default="*", alias="TOOL_HTTP_ALLOWED_HOSTS")
    tool_run_store_path: str = Field(default=".data/executions/tool_runs.jsonl", alias="TOOL_RUN_STORE_PATH")
    mcp_server_extra_paths: str = Field(default="", alias="MCP_SERVER_EXTRA_PATHS")
    firecrawl_api_key: str | None = Field(default=None, alias="FIRECRAWL_API_KEY")
    firecrawl_mcp_enabled: str | None = Field(default=None, alias="FIRECRAWL_MCP_ENABLED")
    firecrawl_mcp_command: str = Field(default="npx", alias="FIRECRAWL_MCP_COMMAND")
    firecrawl_mcp_args: str = Field(default="-y firecrawl-mcp", alias="FIRECRAWL_MCP_ARGS")
    firecrawl_mcp_api_key_ref: str = Field(default="env://FIRECRAWL_API_KEY", alias="FIRECRAWL_MCP_API_KEY_REF")
    context7_api_key: str | None = Field(default=None, alias="CONTEXT7_API_KEY")
    context7_mcp_enabled: str | None = Field(default=None, alias="CONTEXT7_MCP_ENABLED")
    context7_mcp_command: str = Field(default="npx", alias="CONTEXT7_MCP_COMMAND")
    context7_mcp_args: str = Field(default="-y @upstash/context7-mcp", alias="CONTEXT7_MCP_ARGS")
    context7_mcp_api_key_ref: str = Field(default="env://CONTEXT7_API_KEY", alias="CONTEXT7_MCP_API_KEY_REF")
    onecli_enabled: bool = Field(default=False, alias="ONECLI_ENABLED")
    onecli_api_url: str = Field(default="http://localhost:10254", alias="ONECLI_API_URL")
    onecli_gateway_url: str = Field(default="http://localhost:10255", alias="ONECLI_GATEWAY_URL")
    onecli_gateway_ca_bundle_path: str | None = Field(default=None, alias="ONECLI_GATEWAY_CA_BUNDLE_PATH")
    onecli_gateway_ca_bundle_container_path: str = Field(
        default="/etc/agency/onecli/ca.pem",
        alias="ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH",
    )
    onecli_agent_token_secret_ref: str | None = Field(default=None, alias="ONECLI_AGENT_TOKEN_SECRET_REF")
    # Keep setup-session expiry configurable so connector setup can calculate a
    # bounded verification window without relying on an undeclared setting.
    onecli_setup_session_ttl_seconds: int = Field(
        default=1800,
        alias="ONECLI_SETUP_SESSION_TTL_SECONDS",
    )
    onecli_control_api_key_secret_ref: str | None = Field(
        default=None,
        alias="ONECLI_CONTROL_API_KEY_SECRET_REF",
    )
    onecli_force_for_http_tools: bool = Field(default=False, alias="ONECLI_FORCE_FOR_HTTP_TOOLS")
    onecli_force_for_isolated_workers: bool = Field(default=False, alias="ONECLI_FORCE_FOR_ISOLATED_WORKERS")
    onecli_allow_global_agent_token_fallback: bool = Field(
        default=False,
        alias="ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK",
    )
    onecli_multi_user_mode: bool = Field(default=False, alias="ONECLI_MULTI_USER_MODE")
    onecli_external_calls_disabled: bool = Field(default=False, alias="ONECLI_EXTERNAL_CALLS_DISABLED")
    onecli_worker_egress_mode: Literal["proxy_env_only", "docker_internal_network"] = Field(
        default="proxy_env_only",
        alias="ONECLI_WORKER_EGRESS_MODE",
    )
    onecli_worker_egress_network: str = Field(
        default="agency_onecli_worker_egress",
        alias="ONECLI_WORKER_EGRESS_NETWORK",
    )
    onecli_node_proxy_bootstrap_path: str = Field(
        default="/app/app/runtime/node_onecli_proxy.cjs",
        alias="ONECLI_NODE_PROXY_BOOTSTRAP_PATH",
    )
    onecli_worker_no_proxy: str = Field(
        default="localhost,127.0.0.1,::1,postgres,redis,backend,agency-backend,onecli,onecli-postgres,host.docker.internal",
        alias="ONECLI_WORKER_NO_PROXY",
    )

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

    @property
    def parsed_model_provider_allowed_hosts(self) -> list[str]:
        return [item.strip().lower() for item in self.model_provider_allowed_hosts.split(",") if item.strip()]

    @property
    def parsed_agency_allowed_origins(self) -> list[str]:
        return [_normalize_origin(item) for item in self.agency_allowed_origins.split(",") if item.strip()]

    @property
    def parsed_agency_optional_module_spec_refs(self) -> list[str]:
        return [item.strip() for item in self.agency_optional_module_spec_refs.split(",") if item.strip()]

    @property
    def parsed_agency_builtin_optional_modules(self) -> list[str]:
        return [item.strip() for item in self.agency_builtin_optional_modules.split(",") if item.strip()]

    @property
    def parsed_agency_expected_optional_modules(self) -> list[str]:
        return [item.strip() for item in self.agency_expected_optional_modules.split(",") if item.strip()]

    @property
    def cors_allowed_origins(self) -> list[str]:
        origins = list(self.parsed_agency_allowed_origins)
        if self.app_env != "production":
            origins.extend(LOCAL_DEVELOPMENT_CORS_ORIGINS)
        return list(dict.fromkeys(origins))

    @property
    def sanitized_onecli_diagnostics(self) -> dict[str, object]:
        return {
            "enabled": self.onecli_enabled,
            "api_url": self.onecli_api_url,
            "gateway_url": self.onecli_gateway_url,
            "gateway_ca_bundle_configured": bool(self.onecli_gateway_ca_bundle_path),
            "gateway_ca_bundle_container_path": self.onecli_gateway_ca_bundle_container_path,
            "agent_token_secret_ref_configured": bool(self.onecli_agent_token_secret_ref),
            "control_api_key_secret_ref_configured": bool(self.onecli_control_api_key_secret_ref),
            "force_for_http_tools": self.onecli_force_for_http_tools,
            "force_for_isolated_workers": self.onecli_force_for_isolated_workers,
            "allow_global_agent_token_fallback": self.onecli_allow_global_agent_token_fallback,
            "multi_user_mode": self.onecli_multi_user_mode,
            "external_calls_disabled": self.onecli_external_calls_disabled,
            "worker_egress_mode": self.onecli_worker_egress_mode,
            "worker_egress_network": self.onecli_worker_egress_network,
            "node_proxy_bootstrap_configured": bool(self.onecli_node_proxy_bootstrap_path.strip()),
        }

    def ensure_runtime_requirements(self) -> None:
        if self.agency_cors_allow_credentials and "*" in self.parsed_agency_allowed_origins:
            raise RuntimeError("AGENCY_ALLOWED_ORIGINS cannot include '*' when CORS credentials are enabled")
        if self.database_required and not self.database_enabled:
            raise RuntimeError("DATABASE_URL must be configured when the database is required")
        if self.onecli_enabled and not self.onecli_gateway_url.strip():
            raise RuntimeError("ONECLI_GATEWAY_URL must be configured when ONECLI_ENABLED is true")
        if self.onecli_force_for_http_tools and not self.onecli_enabled:
            raise RuntimeError("ONECLI_ENABLED must be true when ONECLI_FORCE_FOR_HTTP_TOOLS is true")
        if self.onecli_force_for_isolated_workers and not self.onecli_enabled:
            raise RuntimeError("ONECLI_ENABLED must be true when ONECLI_FORCE_FOR_ISOLATED_WORKERS is true")
        if self.onecli_force_for_isolated_workers and not self.execution_isolation_enabled:
            raise RuntimeError(
                "EXECUTION_ISOLATION_ENABLED must be true when ONECLI_FORCE_FOR_ISOLATED_WORKERS is true"
            )
        if self.onecli_multi_user_mode and self.onecli_allow_global_agent_token_fallback:
            raise RuntimeError(
                "ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK must be false when ONECLI_MULTI_USER_MODE is true"
            )
        if self.onecli_worker_egress_mode == "docker_internal_network":
            if not self.onecli_force_for_isolated_workers:
                raise RuntimeError(
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS must be true when "
                    "ONECLI_WORKER_EGRESS_MODE=docker_internal_network"
                )
            if not self.onecli_worker_egress_network.strip():
                raise RuntimeError(
                    "ONECLI_WORKER_EGRESS_NETWORK must be configured when "
                    "ONECLI_WORKER_EGRESS_MODE=docker_internal_network"
                )
        if (
                self.onecli_enabled
                and self.onecli_gateway_url.startswith("https://")
                and not self.onecli_gateway_ca_bundle_path
                and self.app_env == "production"
        ):
            raise RuntimeError(
                "ONECLI_GATEWAY_CA_BUNDLE_PATH must be configured for HTTPS OneCLI gateway use in production"
            )
        if self.runtime_reconciler_interval_seconds <= 0:
            raise RuntimeError("RUNTIME_RECONCILER_INTERVAL_SECONDS must be greater than zero")
        if self.connector_health_history_retention_interval_seconds <= 0:
            raise RuntimeError("CONNECTOR_HEALTH_HISTORY_RETENTION_INTERVAL_SECONDS must be greater than zero")
        if self.memory_daily_summary_interval_seconds <= 0:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS must be greater than zero")
        if not self.agency_vision_provider.strip():
            raise RuntimeError("AGENCY_VISION_PROVIDER must be configured")
        if self.memory_context_pack_prompt_limit < 0:
            raise RuntimeError("MEMORY_CONTEXT_PACK_PROMPT_LIMIT must be zero or greater")
        if self.memory_context_pack_history_recent_messages <= 0:
            raise RuntimeError("MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES must be greater than zero")
        if self.memory_context_pack_history_min_messages < 0:
            raise RuntimeError("MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES must be zero or greater")
        if self.memory_context_pack_history_max_raw_tokens < 0:
            raise RuntimeError("MEMORY_CONTEXT_PACK_HISTORY_MAX_RAW_TOKENS must be zero or greater")
        if self.persona_factory_max_documents_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_MAX_DOCUMENTS_PER_RUN must be greater than zero")
        if self.persona_factory_max_source_memories_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN must be greater than zero")
        if self.persona_factory_max_source_characters_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_MAX_SOURCE_CHARACTERS_PER_RUN must be greater than zero")
        if self.persona_factory_default_distillation_mode not in {"deterministic", "llm", "hybrid"}:
            raise RuntimeError("PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE must be deterministic, llm, or hybrid")
        if self.persona_factory_default_llm_model_source not in {"main_agent", "model_profile", "model"}:
            raise RuntimeError("PERSONA_FACTORY_DEFAULT_LLM_MODEL_SOURCE must be main_agent, model_profile, or model")
        if self.persona_factory_llm_max_source_memories_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN must be greater than zero")
        if self.persona_factory_llm_max_source_characters_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_MAX_SOURCE_CHARACTERS_PER_RUN must be greater than zero")
        if self.persona_factory_llm_max_source_tokens_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_MAX_SOURCE_TOKENS_PER_RUN must be greater than zero")
        if self.persona_factory_llm_max_calls_per_run <= 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN must be greater than zero")
        if self.persona_factory_llm_timeout_seconds <= 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_TIMEOUT_SECONDS must be greater than zero")
        if self.persona_factory_llm_retry_attempts < 0:
            raise RuntimeError("PERSONA_FACTORY_LLM_RETRY_ATTEMPTS must be zero or greater")
        if self.workflow_scheduler_interval_seconds <= 0:
            raise RuntimeError("WORKFLOW_SCHEDULER_INTERVAL_SECONDS must be greater than zero")
        if self.execution_wait_poll_interval_seconds <= 0:
            raise RuntimeError("EXECUTION_WAIT_POLL_INTERVAL_SECONDS must be greater than zero")
        if self.memory_daily_summary_target_hour < 0 or self.memory_daily_summary_target_hour > 23:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_TARGET_HOUR must be between 0 and 23")
        if self.memory_daily_summary_target_minute < 0 or self.memory_daily_summary_target_minute > 59:
            raise RuntimeError("MEMORY_DAILY_SUMMARY_TARGET_MINUTE must be between 0 and 59")
        if self.connector_health_history_retention_days <= 0:
            raise RuntimeError("CONNECTOR_HEALTH_HISTORY_RETENTION_DAYS must be greater than zero")
        if self.connector_health_history_retention_max_per_credential < 0:
            raise RuntimeError("CONNECTOR_HEALTH_HISTORY_RETENTION_MAX_PER_CREDENTIAL must be zero or greater")
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
        if self.agent_activity_idle_timeout_seconds <= 0:
            raise RuntimeError("AGENT_ACTIVITY_IDLE_TIMEOUT_SECONDS must be greater than zero")
        if self.agent_run_timeout_seconds <= 0:
            raise RuntimeError("AGENT_RUN_TIMEOUT_SECONDS must be greater than zero")
        if self.main_agent_workflow_monitor_terminal_lookback_seconds < 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_TERMINAL_LOOKBACK_SECONDS must be zero or greater")
        if self.main_agent_workflow_monitor_finding_retention_days <= 0:
            raise RuntimeError("MAIN_AGENT_WORKFLOW_MONITOR_FINDING_RETENTION_DAYS must be greater than zero")
        if self.agent_token_budget_warn_ratio < 0 or self.agent_token_budget_warn_ratio > 1:
            raise RuntimeError("AGENT_TOKEN_BUDGET_WARN_RATIO must be between 0 and 1")
        if self.agent_token_budget_hard_ratio < self.agent_token_budget_warn_ratio:
            raise RuntimeError("AGENT_TOKEN_BUDGET_HARD_RATIO must be greater than or equal to warning ratio")
        if self.agent_run_total_token_budget is not None and self.agent_run_total_token_budget < 0:
            raise RuntimeError("AGENT_RUN_TOTAL_TOKEN_BUDGET must be zero or greater")
        if self.execution_container_memory_limit_mb <= 0:
            raise RuntimeError("EXECUTION_CONTAINER_MEMORY_LIMIT_MB must be greater than zero")
        if self.execution_container_cpu_limit <= 0:
            raise RuntimeError("EXECUTION_CONTAINER_CPU_LIMIT must be greater than zero")
        _ = self.parsed_execution_container_extra_mounts
        if self.execution_isolation_enabled and not self.integrations_runtime_enabled:
            raise RuntimeError(
                "INTEGRATIONS_RUNTIME_ENABLED must be enabled when EXECUTION_ISOLATION_ENABLED is enabled")
        if (
                self.execution_isolation_enabled
                and not self.execution_runtime_database_url
                and self.execution_container_network != "host"
                and _database_url_uses_local_postgres(self.database_url)
        ):
            raise RuntimeError(
                "EXECUTION_RUNTIME_DATABASE_URL must be configured for isolated Docker workers when "
                "DATABASE_URL points at localhost. Use the container-visible Postgres address, for example "
                "postgresql://postgres:postgres@postgres:5432/agency."
            )
        if self.runtime_revision_shadow_mode and not self.integrations_runtime_enabled:
            raise RuntimeError(
                "INTEGRATIONS_RUNTIME_ENABLED must be enabled when RUNTIME_REVISION_SHADOW_MODE is enabled")
        if self.cancel_outdated_executions and not self.execution_isolation_enabled:
            raise RuntimeError("CANCEL_OUTDATED_EXECUTIONS requires EXECUTION_ISOLATION_ENABLED")
        if self.runtime_reconciler_enabled and not self.execution_isolation_enabled:
            raise RuntimeError("RUNTIME_RECONCILER_ENABLED requires EXECUTION_ISOLATION_ENABLED")
        if self.app_env == "production":
            if not self.agency_internal_api_key:
                raise RuntimeError("AGENCY_INTERNAL_API_KEY must be configured in production")
            if self.onecli_force_for_http_tools or self.onecli_force_for_isolated_workers:
                direct_external_credentials = {
                    key
                    for key, value in {
                        "OPENAI_API_KEY": self.openai_api_key,
                        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
                        "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY"),
                        "AZURE_API_KEY": os.getenv("AZURE_API_KEY"),
                        "AZURE_OPENAI_API_KEY": os.getenv("AZURE_OPENAI_API_KEY"),
                        "LOCAL_OPENAI_API_KEY": os.getenv("LOCAL_OPENAI_API_KEY"),
                    }.items()
                    if value
                }
                if direct_external_credentials:
                    raise RuntimeError(
                        "Direct external credential environment variables are not allowed in production "
                        "when OneCLI enforcement is enabled: "
                        + ", ".join(sorted(direct_external_credentials))
                    )
            if self.onecli_force_for_isolated_workers and self.onecli_worker_egress_mode != "docker_internal_network":
                raise RuntimeError(
                    "ONECLI_WORKER_EGRESS_MODE=docker_internal_network is required in production "
                    "when ONECLI_FORCE_FOR_ISOLATED_WORKERS is true"
                )
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


def _database_url_uses_local_postgres(database_url: str | None) -> bool:
    if not database_url:
        return False
    parsed = urlsplit(database_url)
    return parsed.scheme in POSTGRES_DATABASE_SCHEMES and (parsed.hostname or "").lower() in LOCAL_DATABASE_HOSTS


def _normalize_origin(value: str) -> str:
    origin = value.strip()
    if origin == "*":
        return origin
    return origin.rstrip("/")

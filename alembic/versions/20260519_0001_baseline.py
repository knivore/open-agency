"""baseline schema

Revision ID: 20260519_0001_baseline
Revises:
Create Date: 2026-05-18

This baseline is the canonical bootstrap schema for early disposable
environments. Keep it readable by grouping tables by domain, then add
future schema changes as separate chronological migrations once shared
databases depend on this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from collections.abc import Sequence
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

from alembic import op

revision: str = "20260519_0001_baseline"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def json_type() -> sa.types.TypeEngine:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


class _PGVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "vector"


def vector_type() -> sa.types.TypeEngine:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        return _PGVector()
    return sa.Text()


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Alembic creates this bookkeeping column as VARCHAR(32), but Agency's
        # descriptive revision IDs exceed that limit starting with migration 0002.
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Model and tool catalog
    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider_type", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("provider_id", sa.String(length=64), sa.ForeignKey("model_providers.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supports_structured_output", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_streaming", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "tools",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tool_type", sa.String(length=64), nullable=False),
        sa.Column("input_schema_json", json_type(), nullable=False),
        sa.Column("output_schema_json", json_type(), nullable=False),
        sa.Column("implementation_json", json_type(), nullable=False),
        sa.Column("security_json", json_type(), nullable=False),
        sa.Column("mcp_json", json_type(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tools_tool_type", "tools", ["tool_type"])

    # Workflow and agent definitions
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflows_enabled", "workflows", ["enabled"])

    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_id", sa.String(length=64), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("definition_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_id_version"),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=255), nullable=True),
        sa.Column("backstory", sa.Text(), nullable=True),
        sa.Column("model_profile_id", sa.String(length=64), sa.ForeignKey("model_profiles.id"), nullable=True),
        sa.Column("tool_ids_json", json_type(), nullable=False),
        sa.Column("handoff_agent_ids_json", json_type(), nullable=False),
        sa.Column("guardrails_json", json_type(), nullable=False),
        sa.Column("memory_json", json_type(), nullable=False),
        sa.Column("framework_hints_json", json_type(), nullable=False),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agents_enabled", "agents", ["enabled"])

    # Credentials and external integrations
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=True),
        sa.Column("secret_ref", sa.String(length=255), nullable=False, unique=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="active"),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secret_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rotation_policy_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_credentials_owner_user_id", "credentials", ["owner_user_id"])

    op.create_table(
        "connector_installations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("onecli_credential_ref", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="setup_pending"),
        sa.Column("setup_session_id", sa.String(length=64), nullable=True),
        sa.Column("runtime_secret_encrypted", sa.String(length=4096), nullable=True),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "owner_user_id",
            "provider",
            "onecli_credential_ref",
            name="uq_connector_installations_owner_provider_onecli_ref",
        ),
    )
    op.create_index("ix_connector_installations_owner_user_id", "connector_installations", ["owner_user_id"])
    op.create_index("ix_connector_installations_provider", "connector_installations", ["provider"])

    op.create_table(
        "public_endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("endpoint_type", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="launcher"),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_endpoints_endpoint_type", "public_endpoints", ["endpoint_type"])
    op.create_index("ix_public_endpoints_provider", "public_endpoints", ["provider"])
    op.create_index("ix_public_endpoints_status", "public_endpoints", ["status"])

    # Runtime adapters and agent protocols
    op.create_table(
        "runtime_adapters",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("config_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("transport", sa.String(length=64), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("env_refs_json", json_type(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("security_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "a2a_agents",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("agent_card_url", sa.Text(), nullable=True),
        sa.Column("agent_card_json", json_type(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("security_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Prompt and memory catalog
    op.create_table(
        "memory_sources",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("config_json", json_type(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("template_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("variables_json", json_type(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Runtime execution state
    op.create_table(
        "runtime_revisions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False, server_default="integrations/"),
        sa.Column("build_status", sa.String(length=64), nullable=False),
        sa.Column("image_name", sa.String(length=255), nullable=True),
        sa.Column("image_tag", sa.String(length=255), nullable=True),
        sa.Column("base_image", sa.String(length=255), nullable=True),
        sa.Column("build_log_ref", sa.String(), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(), nullable=True),
    )
    op.create_index("ix_runtime_revisions_fingerprint", "runtime_revisions", ["fingerprint"], unique=True)
    op.create_index("ix_runtime_revisions_build_status", "runtime_revisions", ["build_status"])
    op.create_index("ix_runtime_revisions_created_at", "runtime_revisions", ["created_at"])
    op.create_index("ix_runtime_revisions_invalidated_at", "runtime_revisions", ["invalidated_at"])

    op.create_table(
        "goals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("objective", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=64), nullable=False, server_default="normal"),
        sa.Column("owner_actor", sa.String(length=255), nullable=True),
        sa.Column("parent_goal_id", sa.String(length=64), sa.ForeignKey("goals.id"), nullable=True),
        sa.Column("success_criteria_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("constraints_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("execution_ids_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("evidence_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("evaluation_json", json_type(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_goals_status", "goals", ["status"])
    op.create_index("ix_goals_parent_goal_id", "goals", ["parent_goal_id"])
    op.create_index("ix_goals_created_at", "goals", ["created_at"])
    op.create_index("ix_goals_deadline_at", "goals", ["deadline_at"])

    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_id", sa.String(length=64), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("goal_id", sa.String(length=64), sa.ForeignKey("goals.id"), nullable=True),
        sa.Column("workflow_version_id", sa.String(length=64), sa.ForeignKey("workflow_versions.id"), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("runtime_adapter", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_payload_json", json_type(), nullable=False),
        sa.Column("input_json", json_type(), nullable=False),
        sa.Column("output_json", json_type(), nullable=True),
        sa.Column("error_json", json_type(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_revision_id", sa.String(length=64), sa.ForeignKey("runtime_revisions.id"), nullable=True),
        sa.Column("runtime_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("container_id", sa.String(length=255), nullable=True),
        sa.Column("container_name", sa.String(length=255), nullable=True),
        sa.Column("container_image", sa.String(length=255), nullable=True),
        sa.Column("container_status", sa.String(length=64), nullable=True),
        sa.Column("container_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("container_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("container_exit_code", sa.Integer(), nullable=True),
        sa.Column("replacement_of_execution_id", sa.String(length=64), sa.ForeignKey("executions.id"), nullable=True),
        sa.Column("restart_reason", sa.String(), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_executions_status", "executions", ["status"])
    op.create_index("ix_executions_workflow_id", "executions", ["workflow_id"])
    op.create_index("ix_executions_created_at", "executions", ["created_at"])
    op.create_index("ix_executions_runtime_revision_id", "executions", ["runtime_revision_id"])
    op.create_index("ix_executions_container_id", "executions", ["container_id"])
    op.create_index("ix_executions_container_status", "executions", ["container_status"])
    op.create_index("ix_executions_replacement_of_execution_id", "executions", ["replacement_of_execution_id"])
    op.create_index("ix_executions_goal_id", "executions", ["goal_id"])

    op.create_table(
        "execution_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("payload_json", json_type(), nullable=False),
        sa.Column("parent_event_id", sa.String(length=64), sa.ForeignKey("execution_events.id"), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("span_id", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_execution_events_execution_id_sequence"),
    )
    op.create_index("ix_execution_events_execution_id", "execution_events", ["execution_id"])
    op.create_index("ix_execution_events_event_type", "execution_events", ["event_type"])

    op.create_table(
        "outbound_webhook_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=64),
            sa.ForeignKey("execution_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body_preview", sa.Text(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_outbound_webhook_attempts_event_id", "outbound_webhook_attempts", ["event_id"])
    op.create_index("ix_outbound_webhook_attempts_target", "outbound_webhook_attempts", ["target"])
    op.create_index("ix_outbound_webhook_attempts_status", "outbound_webhook_attempts", ["status"])
    op.create_index("ix_outbound_webhook_attempts_created_at", "outbound_webhook_attempts", ["created_at"])

    op.create_table(
        "execution_artifacts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(length=64), sa.ForeignKey("execution_events.id"), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_json", json_type(), nullable=True),
        sa.Column("content_text", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_execution_artifacts_execution_id", "execution_artifacts", ["execution_id"])

    # Scheduling
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_id", sa.String(length=64), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_config_json", json_type(), nullable=False),
        sa.Column("input_template_json", json_type(), nullable=False),
        sa.Column("runtime_adapter", sa.String(length=64), nullable=True),
        sa.Column("max_concurrent_executions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"])
    op.create_index("ix_schedules_next_fire_at", "schedules", ["next_fire_at"])

    op.create_table(
        "schedule_fire_claims",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("schedule_id", sa.String(length=64), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(length=255), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("executions.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("schedule_id", "scheduled_fire_at", name="uq_schedule_fire_claims_schedule_fire_at"),
    )
    op.create_index("ix_schedule_fire_claims_schedule_id", "schedule_fire_claims", ["schedule_id"])
    op.create_index("ix_schedule_fire_claims_status", "schedule_fire_claims", ["status"])
    op.create_index("ix_schedule_fire_claims_lease_expires_at", "schedule_fire_claims", ["lease_expires_at"])
    op.create_index("ix_schedule_fire_claims_execution_id", "schedule_fire_claims", ["execution_id"])

    # Runtime approvals and tool calls
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(length=64), sa.ForeignKey("execution_events.id"), nullable=True),
        # Tool definitions may be deleted independently of historical approval records.
        sa.Column("tool_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("request_payload_json", json_type(), nullable=False),
        sa.Column("response_payload_json", json_type(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_by", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        # Runtime tool history needs to survive catalog edits, so this stores only the tool identifier.
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), sa.ForeignKey("execution_events.id"), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("input_json", json_type(), nullable=False),
        sa.Column("output_json", json_type(), nullable=True),
        sa.Column("error_json", json_type(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )

    # Conversations and primary agent profiles
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="open"),
        sa.Column("created_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("main_agent_profile_id", sa.String(length=64), nullable=True),
        sa.Column("channel_type", sa.String(length=64), nullable=False, server_default="api"),
        sa.Column("channel_thread_id", sa.String(length=255), nullable=True),
        sa.Column("channel_user_id", sa.String(length=255), nullable=True),
        sa.Column("channel_display_name", sa.String(length=255), nullable=True),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_created_by_user_id", "conversations", ["created_by_user_id"])
    op.create_index("ix_conversations_channel_type", "conversations", ["channel_type"])
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("conversation_id", sa.String(length=64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("content_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("plain_text", sa.Text(), nullable=True),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("approval_request_id", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_index("ix_conversation_messages_created_at", "conversation_messages", ["created_at"])
    op.create_index("ix_conversation_messages_message_type", "conversation_messages", ["message_type"])
    op.create_index(
        "ix_conversation_messages_external_message",
        "conversation_messages",
        ["conversation_id", "external_message_id"],
    )

    op.create_table(
        "main_agent_profiles",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("default_workflow_id", sa.String(length=64), nullable=False),
        sa.Column("default_model_profile_id", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("policy_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_main_agent_profiles_enabled", "main_agent_profiles", ["enabled"])

    # Persona factory
    op.create_table(
        "personas",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("published_agent_id", sa.String(length=64), nullable=True),
        sa.Column("published_workflow_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_personas_slug"),
    )
    op.create_index("ix_personas_status", "personas", ["status"])
    op.create_index("ix_personas_created_by_user_id", "personas", ["created_by_user_id"])
    op.create_index("ix_personas_workspace_id", "personas", ["workspace_id"])

    op.create_table(
        "persona_versions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("persona_id", sa.String(length=64), sa.ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("package_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("generated_from_run_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("persona_id", "version", name="uq_persona_versions_persona_id_version"),
    )
    op.create_index("ix_persona_versions_persona_id", "persona_versions", ["persona_id"])
    op.create_index("ix_persona_versions_status", "persona_versions", ["status"])

    op.create_table(
        "persona_sources",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("persona_id", sa.String(length=64), sa.ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("content_sha256", sa.String(length=128), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_persona_sources_persona_id", "persona_sources", ["persona_id"])
    op.create_index("ix_persona_sources_source", "persona_sources", ["source_type", "source_id"])

    op.create_table(
        "persona_distillation_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("persona_id", sa.String(length=64), sa.ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("distillation_mode", sa.String(length=32), nullable=False, server_default="deterministic"),
        sa.Column("llm_model_source", sa.String(length=32), nullable=True),
        sa.Column("model_profile_id", sa.String(length=64), nullable=True),
        sa.Column("llm_model_provider", sa.String(length=128), nullable=True),
        sa.Column("llm_model", sa.String(length=255), nullable=True),
        sa.Column("resolved_model_provider", sa.String(length=128), nullable=True),
        sa.Column("resolved_model", sa.String(length=255), nullable=True),
        sa.Column("resolved_model_profile_id", sa.String(length=64), nullable=True),
        sa.Column("input_source_ids_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("output_package_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("distillation_metrics_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("warnings_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("errors_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_persona_distillation_runs_persona_id", "persona_distillation_runs", ["persona_id"])
    op.create_index("ix_persona_distillation_runs_status", "persona_distillation_runs", ["status"])

    op.create_table(
        "persona_distillation_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("persona_distillation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("persona_id", sa.String(length=64), sa.ForeignKey("personas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_memory_id", sa.String(length=128), nullable=True),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("memory_layer", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_payload_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("review_status", sa.String(length=32), nullable=False, server_default="needs_review"),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_persona_distillation_items_run_id", "persona_distillation_items", ["run_id"])
    op.create_index("ix_persona_distillation_items_persona_id", "persona_distillation_items", ["persona_id"])
    op.create_index(
        "ix_persona_distillation_items_source_memory_id",
        "persona_distillation_items",
        ["source_memory_id"],
    )
    op.create_index(
        "ix_persona_distillation_items_type_layer",
        "persona_distillation_items",
        ["item_type", "memory_layer"],
    )
    op.create_index(
        "ix_persona_distillation_items_review_status",
        "persona_distillation_items",
        ["review_status"],
    )

    op.create_table(
        "conversation_approval_requests",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("approval_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("requested_by_agent_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by_profile_id", sa.String(length=64), nullable=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("origin_message_id", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("diff_summary", sa.Text(), nullable=True),
        sa.Column("proposed_payload_json", json_type(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("approved_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversation_approval_requests_conversation_id",
        "conversation_approval_requests",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_approval_requests_status",
        "conversation_approval_requests",
        ["status"],
    )
    op.create_index(
        "ix_conversation_approval_requests_origin_message_id",
        "conversation_approval_requests",
        ["origin_message_id"],
    )

    # Users and identity mappings
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="active"),
        sa.Column("roles_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("provider_subject", sa.String(length=255), nullable=True),
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_subject", name="uq_users_provider_subject"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_status", "users", ["status"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("last4", sa.String(length=8), nullable=False),
        sa.Column("scopes_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_tokens_owner_user_id", "api_tokens", ["owner_user_id"])
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)

    op.create_table(
        "channel_identity_mappings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("channel_type", sa.String(length=64), nullable=False),
        sa.Column("channel_user_id", sa.String(length=255), nullable=False),
        sa.Column("internal_user_id", sa.String(length=255), nullable=False),
        sa.Column("channel_display_name", sa.String(length=255), nullable=True),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_channel_identity_mappings_channel",
        "channel_identity_mappings",
        ["channel_type", "channel_user_id"],
        unique=True,
    )
    op.create_index(
        "ix_channel_identity_mappings_internal_user_id",
        "channel_identity_mappings",
        ["internal_user_id"],
    )

    op.create_table(
        "onecli_identity_mappings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("onecli_agent_id", sa.String(length=255), nullable=False),
        sa.Column("agent_token_secret_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("onecli_agent_id", name="uq_onecli_identity_mappings_onecli_agent_id"),
    )
    op.create_index(
        "ix_onecli_identity_mappings_owner_user_id",
        "onecli_identity_mappings",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_onecli_identity_mappings_workflow_id",
        "onecli_identity_mappings",
        ["workflow_id"],
    )

    # Smart-home semantic context and guarded ambient actions ship in the
    # baseline for disposable environments, but older databases received them
    # as follow-up migrations before this collapse.
    op.create_table(
        "home_context_rooms",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("home_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("aliases_json", json_type(), nullable=False),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_home_context_rooms_home_id", "home_context_rooms", ["home_id"])
    op.create_index("ix_home_context_rooms_name", "home_context_rooms", ["name"])

    op.create_table(
        "home_context_entity_mappings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=True),
        sa.Column("aliases_json", json_type(), nullable=False),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["home_context_rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_home_context_entity_mappings_room_id", "home_context_entity_mappings", ["room_id"])
    op.create_index("ix_home_context_entity_mappings_entity_id", "home_context_entity_mappings", ["entity_id"])
    op.create_index("ix_home_context_entity_mappings_kind", "home_context_entity_mappings", ["kind"])

    op.create_table(
        "ambient_pending_actions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("audit_category", sa.String(length=64), nullable=False),
        sa.Column("confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_payload_json", json_type(), nullable=False),
        sa.Column("result_payload_json", json_type(), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ambient_pending_actions_status", "ambient_pending_actions", ["status"])
    op.create_index("ix_ambient_pending_actions_expires_at", "ambient_pending_actions", ["expires_at"])
    op.create_index("ix_ambient_pending_actions_audit_category", "ambient_pending_actions", ["audit_category"])

    op.create_table(
        "ambient_action_audit_log",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("audit_category", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ambient_action_audit_log_action_id", "ambient_action_audit_log", ["action_id"])
    op.create_index("ix_ambient_action_audit_log_event_type", "ambient_action_audit_log", ["event_type"])
    op.create_index("ix_ambient_action_audit_log_created_at", "ambient_action_audit_log", ["created_at"])

    # Uploaded documents, long-term memory, and graph projection
    op.create_table(
        "uploaded_documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("text_characters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upload_mode", sa.String(length=32), nullable=False, server_default="vector"),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_uploaded_documents_agent_id", "uploaded_documents", ["agent_id"])
    op.create_index("ix_uploaded_documents_content_sha256", "uploaded_documents", ["content_sha256"])
    op.create_index("ix_uploaded_documents_conversation_id", "uploaded_documents", ["conversation_id"])
    op.create_index(
        "ix_uploaded_documents_created_by_user_scope",
        "uploaded_documents",
        ["created_by_user_id", "scope"],
    )
    op.create_index("ix_uploaded_documents_status", "uploaded_documents", ["status"])
    op.create_index("ix_uploaded_documents_upload_mode", "uploaded_documents", ["upload_mode"])
    op.create_index("ix_uploaded_documents_workspace_scope", "uploaded_documents", ["workspace_id", "scope"])
    op.create_index("ix_uploaded_documents_workflow_id", "uploaded_documents", ["workflow_id"])

    op.create_table(
        "memory_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags_json", json_type(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("memory_type", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("summary_date", sa.Date(), nullable=True),
        sa.Column("archived_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_conversation_id", sa.String(length=64), nullable=True),
        sa.Column("source_execution_id", sa.String(length=64), nullable=True),
        sa.Column("supersedes_memory_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False),
        sa.Column("embedding_json", json_type(), nullable=True),
        sa.Column("embedding_vector", vector_type(), nullable=True),
        sa.Column("embedding_model_profile_id", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_records_scope", "memory_records", ["scope"])
    op.create_index("ix_memory_records_user_scope", "memory_records", ["created_by_user_id", "scope"])
    op.create_index("ix_memory_records_workspace_scope", "memory_records", ["workspace_id", "scope"])
    op.create_index("ix_memory_records_conversation_scope", "memory_records", ["conversation_id", "scope"])
    op.create_index("ix_memory_records_workflow_scope", "memory_records", ["workflow_id", "scope"])
    op.create_index("ix_memory_records_agent_id", "memory_records", ["agent_id"])
    op.create_index("ix_memory_records_type_status", "memory_records", ["memory_type", "status"])
    op.create_index(
        "ix_memory_records_source_conversation_summary_date",
        "memory_records",
        ["source_conversation_id", "summary_date"],
    )
    op.create_index("ix_memory_records_agent_type", "memory_records", ["agent_id", "memory_type"])
    op.create_index("ix_memory_records_workflow_type", "memory_records", ["workflow_id", "memory_type"])
    op.create_index("ix_memory_records_workspace_type", "memory_records", ["workspace_id", "memory_type"])
    op.create_index("ix_memory_records_user_type", "memory_records", ["created_by_user_id", "memory_type"])
    op.create_index("ix_memory_records_summary_date_type", "memory_records", ["summary_date", "memory_type"])

    op.create_table(
        "graph_projection_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column("payload_json", json_type(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="application"),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "source_event_id", name="uq_graph_projection_events_source_event_id"),
    )
    op.create_index("ix_graph_projection_events_event_type", "graph_projection_events", ["event_type"])
    op.create_index("ix_graph_projection_events_aggregate", "graph_projection_events", ["aggregate_type", "aggregate_id"])
    op.create_index(
        "ix_graph_projection_events_status_occurred_at",
        "graph_projection_events",
        ["status", "occurred_at"],
    )
    op.create_index("ix_graph_projection_events_source_event_id", "graph_projection_events", ["source_event_id"])


def downgrade() -> None:
    for table_name in [
        "graph_projection_events",
        "memory_records",
        "uploaded_documents",
        "ambient_action_audit_log",
        "ambient_pending_actions",
        "home_context_entity_mappings",
        "home_context_rooms",
        "onecli_identity_mappings",
        "channel_identity_mappings",
        "api_tokens",
        "users",
        "persona_distillation_items",
        "persona_distillation_runs",
        "persona_sources",
        "persona_versions",
        "personas",
        "conversation_approval_requests",
        "main_agent_profiles",
        "conversation_messages",
        "conversations",
        "tool_invocations",
        "approval_requests",
        "schedule_fire_claims",
        "schedules",
        "execution_artifacts",
        "outbound_webhook_attempts",
        "execution_events",
        "executions",
        "goals",
        "runtime_revisions",
        "prompt_templates",
        "memory_sources",
        "a2a_agents",
        "mcp_servers",
        "runtime_adapters",
        "public_endpoints",
        "connector_installations",
        "credentials",
        "agents",
        "workflow_versions",
        "workflows",
        "tools",
        "model_profiles",
        "model_providers",
    ]:
        op.drop_table(table_name)

from __future__ import annotations

import os
import tempfile
import unittest
from alembic.config import Config
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from unittest.mock import patch

from alembic import command
from app.core.config import reset_settings_cache
from app.db.models import (
    A2AAgentORM,
    AgentORM,
    Base,
    ConversationApprovalRequestORM,
    ConversationMessageORM,
    ConversationORM,
    CredentialORM,
    ExecutionArtifactORM,
    ExecutionEventORM,
    ExecutionORM,
    MainAgentProfileORM,
    MCPServerORM,
    MemoryRecordORM,
    ModelProfileORM,
    ModelProviderORM,
    RuntimeAdapterORM,
    RuntimeRevisionORM,
    ScheduleFireClaimORM,
    ScheduleORM,
    ToolORM,
    WorkflowORM,
    WorkflowVersionORM,
)
from app.db.repositories import (
    A2AAgentRepository,
    AgentRepository,
    ConversationMessageRepository,
    ConversationRepository,
    CredentialRepository,
    MCPServerRepository,
    ModelProfileRepository,
    ModelProviderRepository,
    RuntimeAdapterRepository,
    SQLExecutionArtifactRepository,
    SQLExecutionEventRepository,
    SQLExecutionRepository,
    SQLMemoryRepository,
    ScheduleRepository,
    ToolRepository,
    WorkflowRepository,
    WorkflowVersionRepository,
)
from app.db.session import get_async_engine, get_session_maker, reset_session_state
from app.domain import Execution, ExecutionEvent, ExecutionEventType, MemoryRecord
from app.runtime.native.state import SQLExecutionStore


class PostgresSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "schema.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": self.db_url,
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def asyncSetUp(self) -> None:
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()

    async def test_all_orm_models_import(self) -> None:
        expected_tables = {
            "model_providers",
            "model_profiles",
            "agents",
            "tools",
            "workflows",
            "workflow_versions",
            "runtime_revisions",
            "executions",
            "execution_events",
            "execution_artifacts",
            "schedules",
            "schedule_fire_claims",
            "credentials",
            "runtime_adapters",
            "mcp_servers",
            "a2a_agents",
            "approval_requests",
            "conversations",
            "conversation_messages",
            "conversation_approval_requests",
            "main_agent_profiles",
            "tool_invocations",
            "memory_sources",
            "prompt_templates",
        }
        self.assertTrue(expected_tables.issubset(Base.metadata.tables.keys()))

    async def test_repositories_can_create_list_get_update_records(self) -> None:
        async with self.session_factory() as session:
            provider_repo = ModelProviderRepository(session)
            profile_repo = ModelProfileRepository(session)
            agent_repo = AgentRepository(session)
            conversation_repo = ConversationRepository(session)
            conversation_message_repo = ConversationMessageRepository(session)
            tool_repo = ToolRepository(session)
            workflow_repo = WorkflowRepository(session)
            workflow_version_repo = WorkflowVersionRepository(session)
            execution_repo = SQLExecutionRepository(session)
            event_repo = SQLExecutionEventRepository(session)
            artifact_repo = SQLExecutionArtifactRepository(session)
            schedule_repo = ScheduleRepository(session)
            credential_repo = CredentialRepository(session)
            runtime_repo = RuntimeAdapterRepository(session)
            mcp_repo = MCPServerRepository(session)
            a2a_repo = A2AAgentRepository(session)

            provider = await provider_repo.create(
                ModelProviderORM(
                    id="provider-1",
                    name="OpenAI",
                    provider_type="openai",
                    base_url="https://api.openai.com/v1",
                    config_json={"tier": "default"},
                )
            )
            profile = await profile_repo.create(
                ModelProfileORM(
                    id="profile-1",
                    provider_id=provider.id,
                    name="GPT-4.1",
                    model="gpt-4.1",
                    supports_tools=True,
                    config_json={"max_retries": 2},
                )
            )
            agent = await agent_repo.create(
                AgentORM(
                    id="agent-1",
                    name="Planner",
                    instructions="Plan tasks",
                    model_profile_id=profile.id,
                    tool_ids_json=["tool-1"],
                    handoff_agent_ids_json=[],
                    guardrails_json=[{"name": "safe"}],
                    memory_json={"enabled": True},
                    framework_hints_json={"preferred_adapter": "native"},
                    enabled=True,
                )
            )
            tool = await tool_repo.create(
                ToolORM(
                    id="tool-1",
                    name="Search",
                    description="Search the web",
                    tool_type="http_request",
                    input_schema_json={"type": "object"},
                    output_schema_json={"type": "object"},
                    implementation_json={"module": "app.tools.impl", "function": "search"},
                    security_json={"requires_approval": False},
                    mcp_json={"exposed": True},
                    enabled=True,
                )
            )
            workflow = await workflow_repo.create(
                WorkflowORM(
                    id="workflow-1",
                    name="Workflow",
                    description="Runs an agent",
                    current_version=1,
                    enabled=True,
                )
            )
            conversation = await conversation_repo.create(
                ConversationORM(
                    id="conversation-1",
                    title=None,
                    status="open",
                    created_by_user_id="user-1",
                    channel_type="api",
                    metadata_json={"source": "test"},
                )
            )
            main_agent_profile = MainAgentProfileORM(
                id="main-agent-profile-1",
                name="Main",
                agent_id="agent-1",
                default_workflow_id="workflow-1",
                default_model_profile_id=profile.id,
                enabled=True,
                policy_json={"can_answer_directly": True},
                metadata_json={"source": "test"},
            )
            session.add(main_agent_profile)
            await session.flush()
            await conversation_message_repo.create(
                ConversationMessageORM(
                    id="conversation-message-1",
                    conversation_id=conversation.id,
                    role="user",
                    message_type="user_text",
                    plain_text="hello",
                    content_json={"text": "hello"},
                    metadata_json={},
                )
            )
            session.add(
                ConversationApprovalRequestORM(
                    id="conversation-approval-1",
                    approval_type="workflow_execution",
                    status="pending",
                    target_type="workflow",
                    target_id="workflow-1",
                    requested_by_agent_id="agent-1",
                    requested_by_profile_id="main-agent-profile-1",
                    conversation_id=conversation.id,
                    origin_message_id="conversation-message-1",
                    summary="Run workflow-1",
                    metadata_json={},
                )
            )
            await session.flush()
            workflow_version = await workflow_version_repo.create(
                WorkflowVersionORM(
                    id="workflow-version-1",
                    workflow_id=workflow.id,
                    version=1,
                    status="published",
                    definition_json={"entrypoint": "agent-1"},
                )
            )
            runtime_revision = RuntimeRevisionORM(
                id="runtime-revision-1",
                fingerprint="fingerprint-1",
                source_path="integrations/",
                build_status="ready",
                image_name="agency-runtime",
                image_tag="rev-1",
                metadata_json={"source": "test"},
            )
            session.add(runtime_revision)
            await session.flush()
            execution = await execution_repo.create(
                ExecutionORM(
                    id="execution-1",
                    workflow_id=workflow.id,
                    workflow_version_id=workflow_version.id,
                    runtime_revision_id=runtime_revision.id,
                    runtime_fingerprint=runtime_revision.fingerprint,
                    status="created",
                    runtime_adapter="native",
                    trigger_type="manual",
                    trigger_payload_json={"source": "test"},
                    input_json={"topic": "schema"},
                    container_id="container-1",
                    container_name="agency-execution-1",
                    container_image="agency-runtime:rev-1",
                    container_status="created",
                )
            )
            event = await event_repo.create(
                ExecutionEventORM(
                    id="event-1",
                    execution_id=execution.id,
                    sequence=1,
                    event_type="execution.created",
                    actor_type="system",
                    payload_json={"state": "created"},
                )
            )
            await artifact_repo.create(
                ExecutionArtifactORM(
                    id="artifact-1",
                    execution_id=execution.id,
                    event_id=event.id,
                    artifact_type="text",
                    name="summary",
                    content_json={"summary": "done"},
                    metadata_json={"format": "json"},
                )
            )
            await schedule_repo.create(
                ScheduleORM(
                    id="schedule-1",
                    workflow_id=workflow.id,
                    enabled=True,
                    trigger_type="cron",
                    trigger_config_json={"cron": "0 * * * *"},
                    input_template_json={"topic": "schema"},
                    runtime_adapter="native",
                    max_concurrent_executions=1,
                    timezone="UTC",
                )
            )
            await credential_repo.create(
                CredentialORM(
                    id="credential-1",
                    name="OpenAI Key",
                    provider="openai",
                    secret_ref="secret/openai",
                    metadata_json={"scope": "default"},
                )
            )
            await runtime_repo.create(
                RuntimeAdapterORM(
                    id="native",
                    name="Native",
                    adapter_type="native",
                    enabled=True,
                    available=True,
                    config_json={"streaming": True},
                )
            )
            await mcp_repo.create(
                MCPServerORM(
                    id="mcp-1",
                    name="Filesystem",
                    transport="stdio",
                    command="filesystem",
                    env_refs_json=["ENV_ONE"],
                    enabled=True,
                    security_json={"allowed_paths": ["/tmp"]},
                )
            )
            await a2a_repo.create(
                A2AAgentORM(
                    id="a2a-1",
                    name="Remote Agent",
                    agent_card_url="https://example.test/agent-card.json",
                    agent_card_json={"name": "Remote Agent"},
                    enabled=True,
                    security_json={"allowlisted_domains": ["example.test"]},
                )
            )
            await session.commit()

            listed_tools = await tool_repo.list()
            fetched_execution = await execution_repo.get("execution-1")
            updated_provider = await provider_repo.update("provider-1", {"base_url": "https://example.test/v1"})
            await session.commit()

        self.assertEqual(len(listed_tools), 1)
        self.assertEqual(fetched_execution.id, "execution-1")
        self.assertEqual(updated_provider.base_url, "https://example.test/v1")
        self.assertEqual(agent.tool_ids_json, ["tool-1"])
        self.assertEqual(tool.security_json["requires_approval"], False)
        self.assertEqual(fetched_execution.runtime_revision_id, "runtime-revision-1")
        self.assertEqual(fetched_execution.container_id, "container-1")

    async def test_json_fields_round_trip(self) -> None:
        async with self.session_factory() as session:
            tool_repo = ToolRepository(session)
            tool = await tool_repo.create(
                ToolORM(
                    id="tool-json",
                    name="JSON Tool",
                    description="JSON round trip",
                    tool_type="python_function",
                    input_schema_json={"type": "object", "properties": {"value": {"type": "string"}}},
                    output_schema_json={"type": "object"},
                    implementation_json={"module": "demo", "function": "run"},
                    security_json={"allowed_paths": ["/tmp"], "dangerous": False},
                    mcp_json={"tags": ["demo"]},
                    enabled=True,
                )
            )
            await session.commit()
            fetched = await tool_repo.get(tool.id)

        self.assertEqual(fetched.input_schema_json["properties"]["value"]["type"], "string")
        self.assertEqual(fetched.security_json["allowed_paths"], ["/tmp"])
        self.assertEqual(fetched.mcp_json["tags"], ["demo"])

    async def test_memory_embedding_vector_round_trip(self) -> None:
        async with self.session_factory() as session:
            repo = SQLMemoryRepository(lambda: self.session_factory())
            created = await repo.create(
                MemoryRecord(
                    id="memory-vector",
                    scope="global",
                    content="Durable semantic memory.",
                    embedding=[0.1, 0.2, 0.3],
                    embedding_model_profile_id="embedding-profile",
                    embedding_model="embedding-model",
                    embedding_dimensions=3,
                )
            )
            fetched_orm = await session.get(MemoryRecordORM, created.id)

        self.assertEqual(created.embedding, [0.1, 0.2, 0.3])
        self.assertEqual(fetched_orm.embedding_vector, [0.1, 0.2, 0.3])
        self.assertEqual(fetched_orm.embedding_json, [0.1, 0.2, 0.3])

    async def test_sql_execution_store_assigns_event_sequence_from_database(self) -> None:
        async with self.session_factory() as session:
            session.add(WorkflowORM(id="workflow-store-seq", name="Store Sequence", current_version=1, enabled=True))
            await session.commit()

        store = SQLExecutionStore(self.session_factory)
        execution = await store.save_execution(
            Execution(
                id="execution-store-seq",
                workflow_id="workflow-store-seq",
                runtime_adapter="native",
                status="created",
            )
        )
        first = await store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                event_type=ExecutionEventType.EXECUTION_CREATED,
                sequence=1,
            )
        )
        second = await store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                event_type=ExecutionEventType.EXECUTION_CANCELLED,
                sequence=1,
            )
        )

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)

    async def test_execution_event_sequence_constraint(self) -> None:
        async with self.session_factory() as session:
            workflow = WorkflowORM(id="workflow-seq", name="Sequence", current_version=1, enabled=True)
            workflow_version = WorkflowVersionORM(
                id="workflow-version-seq",
                workflow_id=workflow.id,
                version=1,
                status="draft",
                definition_json={"entrypoint": "node-1"},
            )
            execution = ExecutionORM(
                id="execution-seq",
                workflow_id=workflow.id,
                workflow_version_id=workflow_version.id,
                status="running",
                runtime_adapter="native",
                trigger_type="manual",
                trigger_payload_json={},
                input_json={},
            )
            session.add_all(
                [
                    workflow,
                    workflow_version,
                    execution,
                    ExecutionEventORM(
                        id="event-seq-1",
                        execution_id=execution.id,
                        sequence=1,
                        event_type="execution.started",
                        actor_type="system",
                        payload_json={},
                    ),
                    ExecutionEventORM(
                        id="event-seq-2",
                        execution_id=execution.id,
                        sequence=1,
                        event_type="execution.started",
                        actor_type="system",
                        payload_json={},
                    ),
                ]
            )

            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()


class AlembicUpgradeTests(unittest.TestCase):
    def test_alembic_upgrade_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "alembic.db"
            db_url = f"sqlite+aiosqlite:///{db_path}"
            ini_path = Path("/Users/kehchinleong/Documents/Personal/Agency/agency/alembic.ini")
            alembic_path = Path("/Users/kehchinleong/Documents/Personal/Agency/agency/alembic")
            with patch.dict(
                    os.environ,
                    {
                        "APP_ENV": "test",
                        "DATABASE_URL": db_url,
                    },
                    clear=False,
            ):
                reset_settings_cache()
                cfg = Config(str(ini_path))
                cfg.set_main_option("script_location", str(alembic_path))
                cfg.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(cfg, "head")

            engine = create_engine(f"sqlite:///{db_path}")
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            execution_indexes = {index["name"] for index in inspector.get_indexes("executions")}
            runtime_revision_indexes = {index["name"] for index in inspector.get_indexes("runtime_revisions")}
            channel_identity_indexes = {
                index["name"] for index in inspector.get_indexes("channel_identity_mappings")
            }
            conversation_message_indexes = {
                index["name"] for index in inspector.get_indexes("conversation_messages")
            }
            engine.dispose()

        self.assertIn("executions", tables)
        self.assertIn("workflow_versions", tables)
        self.assertIn("runtime_revisions", tables)
        self.assertIn("approval_requests", tables)
        self.assertIn("channel_identity_mappings", tables)
        self.assertIn("ix_executions_runtime_revision_id", execution_indexes)
        self.assertIn("ix_runtime_revisions_fingerprint", runtime_revision_indexes)
        self.assertIn("ix_channel_identity_mappings_channel", channel_identity_indexes)
        self.assertIn("ix_channel_identity_mappings_internal_user_id", channel_identity_indexes)
        self.assertIn("ix_conversation_messages_external_message", conversation_message_indexes)

    def test_alembic_upgrade_normalizes_retired_agency_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy-alembic.db"
            db_url = f"sqlite+aiosqlite:///{db_path}"
            ini_path = Path("/Users/kehchinleong/Documents/Personal/Agency/agency/alembic.ini")
            alembic_path = Path("/Users/kehchinleong/Documents/Personal/Agency/agency/alembic")

            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": "20260517_0002"},
                )
            engine.dispose()

            with patch.dict(
                    os.environ,
                    {
                        "APP_ENV": "test",
                        "DATABASE_URL": db_url,
                    },
                    clear=False,
            ):
                reset_settings_cache()
                cfg = Config(str(ini_path))
                cfg.set_main_option("script_location", str(alembic_path))
                cfg.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(cfg, "head")

            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as connection:
                version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            engine.dispose()

        self.assertEqual(version, "0001")


if __name__ == "__main__":
    unittest.main()

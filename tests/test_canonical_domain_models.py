from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.api.schemas.execution import ExecutionRecord
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    MemoryType,
    MemoryRecord,
    MemoryStatus,
    ModelProfileDefinition,
    ModelProviderDefinition,
    RuntimeAdapterDefinition,
    ScheduleDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.domain.models import (
    ConnectorBindingDefinition,
    CredentialReference,
    EdgeType,
    FrameworkHints,
    GraphContextSettings,
    MCPExposureSettings,
    MemorySettings,
    ModelProviderType,
    ProviderEndpointDefinition,
    RuntimeAdapterType,
    ScheduleType,
    SecretReference,
    SecuritySettings,
    ToolImplementationReference,
    ToolType,
    VersionDefinition,
)


class CanonicalDomainModelTests(unittest.TestCase):
    def test_privileged_security_capabilities_always_require_sandboxing(self):
        for capability in ("allow_shell", "allow_browser", "allow_filesystem", "allow_network"):
            with self.subTest(capability=capability):
                security = SecuritySettings.model_validate(
                    {"sandbox_required": False, capability: True}
                )
                self.assertTrue(security.has_privileged_capabilities)
                self.assertTrue(security.sandbox_required)

        unprivileged = SecuritySettings(sandbox_required=False)
        self.assertFalse(unprivileged.has_privileged_capabilities)
        self.assertFalse(unprivileged.sandbox_required)

    def test_model_provider_and_profile_round_trip(self):
        provider = ModelProviderDefinition(
            name="Local vLLM",
            provider_type=ModelProviderType.VLLM,
            endpoint=ProviderEndpointDefinition(
                base_url="http://localhost:8000/v1",
                region="local",
            ),
            capabilities=["chat", "tools"],
            secret_references=[SecretReference(secret_name="vllm-api-key", source="vault")],
        )
        profile = ModelProfileDefinition(
            name="Fast Tool Model",
            provider=provider.id,
            model="gpt-4o-mini-compatible",
            base_url="http://localhost:1234/v1",
            api_key_ref="secret/local-openai",
            temperature=0.1,
            context_window=128000,
            supports_tools=True,
            supports_structured_output=True,
            supports_streaming=True,
            fallback_strategy="manual",
            fallback_policy={
                "retry_on": ["rate_limit", "timeout"],
                "same_provider_only": True,
                "require_capability_match": True,
            },
            fallback_models=[
                {
                    "provider": provider.id,
                    "model": "gpt-4o-mini-compatible-backup",
                    "context_window": 64000,
                }
            ],
        )

        provider_round_trip = ModelProviderDefinition.model_validate(provider.model_dump(mode="json"))
        profile_round_trip = ModelProfileDefinition.model_validate(profile.model_dump(mode="json"))

        self.assertEqual(provider_round_trip.provider_type, ModelProviderType.VLLM)
        self.assertEqual(profile_round_trip.provider, provider.id)
        self.assertEqual(profile_round_trip.provider_id, provider.id)
        self.assertEqual(profile_round_trip.model_name, "gpt-4o-mini-compatible")
        self.assertTrue(profile_round_trip.supports_structured_output)
        self.assertEqual(profile_round_trip.fallback_strategy, "manual")
        self.assertEqual(profile_round_trip.fallback_policy.retry_on, ["rate_limit", "timeout"])
        self.assertTrue(profile_round_trip.fallback_policy.same_provider_only)
        self.assertEqual(profile_round_trip.fallback_models[0].model, "gpt-4o-mini-compatible-backup")

    def test_agent_tool_task_workflow_round_trip(self):
        tool = ToolDefinition(
            name="Browser",
            description="Controlled browser tool",
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"title": {"type": "string"}}},
            implementation=ToolImplementationReference(
                implementation_type="python_function",
                target="tools.browser",
                callable_name="create_tool",
            ),
            security=SecuritySettings(
                requires_approval=True,
                sandbox_required=True,
                allow_browser=True,
                allow_network=True,
                allowlisted_mcp_servers=["browser-use"],
                module_allowlist=["tools.browser"],
                function_allowlist=["create_tool"],
                credential_references=[CredentialReference(ref="secret/browser-token", source="vault")],
                connector_bindings=[
                    ConnectorBindingDefinition(
                        provider="github",
                        credential_id="credential-github-api",
                        purpose="release_automation",
                        target_scope={"owner": "acme", "repo": "api"},
                    )
                ],
            ),
            mcp_exposure=MCPExposureSettings(expose_as_mcp_tool=True, tags=["browser"]),
        )
        agent = AgentDefinition(
            name="Research Agent",
            description="Looks things up and summarizes them.",
            instructions="Use tools carefully and cite sources.",
            system_prompt="Use tools carefully and cite sources.",
            role="Researcher",
            backstory="A careful analyst.",
            model_profile_id="profile-1",
            tool_ids=[tool.id],
            handoff_agent_ids=["agent-reviewer"],
            memory=MemorySettings(enabled=True, strategy="buffer", scope="execution"),
            graph_context=GraphContextSettings(
                enabled=True,
                auto_retrieval_enabled=False,
                default_intent="resume",
                default_budget="brief",
                max_records=25,
            ),
            framework_hints=FrameworkHints(preferred_adapter="crewai", adapter_config={"verbose": True}),
        )
        task = TaskDefinition(
            name="Research task",
            description="Research the requested topic",
            instructions="Find three relevant points.",
            expected_output="A short bulleted summary",
            agent_id=agent.id,
            tool_ids=[tool.id],
            human_approval_required=True,
        )
        start_node = WorkflowNodeDefinition(
            name="Research node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        review_node = WorkflowNodeDefinition(
            name="Approval node",
            node_type="approval",
            config={"policy": "human"},
        )
        edge = WorkflowEdgeDefinition(
            source_node_id=start_node.id,
            target_node_id=review_node.id,
            edge_type=EdgeType.APPROVAL,
        )
        workflow = WorkflowDefinition(
            name="Research workflow",
            description="Research then request approval.",
            nodes=[start_node, review_node],
            edges=[edge],
            entrypoint=start_node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            allowed_runtime_adapter_ids=["native", "crewai"],
            default_runtime_adapter_id="crewai",
            versioning=VersionDefinition(version="1.2.0", revision=3, labels=["stable"]),
        )

        round_trip = WorkflowDefinition.model_validate(workflow.model_dump(mode="json"))
        serialized_agent = round_trip.agent_definitions[0].model_dump(mode="json")

        self.assertEqual(round_trip.entrypoint, start_node.id)
        self.assertEqual(round_trip.default_runtime_adapter_id, "crewai")
        self.assertEqual(round_trip.default_runtime_adapter, "crewai")
        self.assertEqual(round_trip.allowed_runtime_adapters, ["native", "crewai"])
        self.assertEqual(round_trip.agent_definitions[0].tool_ids, [tool.id])
        self.assertTrue(round_trip.agent_definitions[0].graph_context.enabled)
        self.assertEqual(round_trip.agent_definitions[0].graph_context.default_intent, "resume")
        self.assertEqual(round_trip.agent_definitions[0].graph_context.default_budget, "brief")
        self.assertEqual(round_trip.agent_definitions[0].instructions, "Use tools carefully and cite sources.")
        self.assertEqual(round_trip.agent_definitions[0].system_prompt, "Use tools carefully and cite sources.")
        self.assertNotIn("objective", serialized_agent)
        self.assertEqual(round_trip.edges[0].edge_type, EdgeType.APPROVAL)
        self.assertEqual(
            round_trip.tool_definitions[0].security.connector_bindings[0].target_scope["repo"],
            "api",
        )

    def test_memory_record_legacy_and_summary_validation(self):
        legacy = MemoryRecord(
            scope="user",
            created_by_user_id="user-1",
            content="User prefers concise summaries.",
        )
        legacy_round_trip = MemoryRecord.model_validate(legacy.model_dump(mode="json"))
        self.assertIsNone(legacy_round_trip.memory_type)
        self.assertEqual(legacy_round_trip.status, MemoryStatus.ACTIVE)
        self.assertEqual(legacy_round_trip.importance, 50)

        summary = MemoryRecord(
            scope="conversation",
            conversation_id="conversation-1",
            source_conversation_id="conversation-1",
            memory_type=MemoryType.DAILY_SUMMARY,
            summary_date=date(2026, 5, 7),
            archived_window_start=datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc),
            archived_window_end=datetime(2026, 5, 7, 23, 59, tzinfo=timezone.utc),
            content="The day focused on memory architecture decisions.",
            summary="Locked the DB-first memory approach.",
        )
        summary_round_trip = MemoryRecord.model_validate(summary.model_dump(mode="json"))
        self.assertEqual(summary_round_trip.memory_type, MemoryType.DAILY_SUMMARY)
        self.assertEqual(summary_round_trip.summary_date, date(2026, 5, 7))

        with self.assertRaises(ValueError):
            MemoryRecord(
                scope="conversation",
                conversation_id="conversation-1",
                memory_type=MemoryType.DAILY_SUMMARY,
                content="Missing required summary fields.",
            )

        with self.assertRaises(ValueError):
            MemoryRecord(
                scope="user",
                created_by_user_id="user-1",
                content="Out of bounds importance.",
                importance=101,
            )

    def test_execution_models_round_trip(self):
        created_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        execution = Execution(
            workflow_id="workflow-123",
            runtime_adapter_id="crewai",
            status="running",
            input_payload={"topic": "agentic workflows"},
            created_by="user-1",
            started_at=created_at,
        )
        event = ExecutionEvent(
            execution_id=execution.id,
            event_type=ExecutionEventType.TOOL_CALL_STARTED,
            timestamp=created_at,
            sequence=4,
            actor="Research Agent",
            payload={"tool_id": "browser", "input": {"url": "https://example.com"}},
            redacted_fields=["payload.input.headers.authorization"],
        )
        artifact = ExecutionArtifact(
            execution_id=execution.id,
            name="report.docx",
            artifact_type="document",
            uri="s3://bucket/reports/report.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=2048,
        )

        execution_round_trip = Execution.model_validate(execution.model_dump(mode="json"))
        event_round_trip = ExecutionEvent.model_validate(event.model_dump(mode="json"))
        artifact_round_trip = ExecutionArtifact.model_validate(artifact.model_dump(mode="json"))

        self.assertEqual(execution_round_trip.status.value, "running")
        self.assertEqual(event_round_trip.event_type, ExecutionEventType.TOOL_CALL_STARTED)
        self.assertEqual(artifact_round_trip.uri, "s3://bucket/reports/report.docx")

    def test_runtime_adapter_and_schedule_round_trip(self):
        adapter = RuntimeAdapterDefinition(
            name="CrewAI Adapter",
            adapter_type=RuntimeAdapterType.CREWAI,
            version="1.10.1",
            capabilities=["multi-agent", "planning", "tools"],
            config_schema={"type": "object"},
        )
        schedule = ScheduleDefinition(
            name="Nightly digest",
            workflow_id="workflow-123",
            runtime_adapter_id=adapter.id,
            schedule_type=ScheduleType.CRON,
            cron="0 2 * * *",
            timezone="Asia/Singapore",
            input_payload={"mode": "nightly"},
        )

        adapter_round_trip = RuntimeAdapterDefinition.model_validate(adapter.model_dump(mode="json"))
        schedule_round_trip = ScheduleDefinition.model_validate(schedule.model_dump(mode="json"))

        self.assertEqual(adapter_round_trip.adapter_type, RuntimeAdapterType.CREWAI)
        self.assertEqual(schedule_round_trip.cron, "0 2 * * *")
        self.assertTrue(schedule_round_trip.enabled)

    def test_agent_definition_uses_instructions_and_system_prompt(self):
        agent = AgentDefinition.model_validate(
            {
                "name": "Alias Agent",
                "role": "planner",
                "instructions": "Plan effectively",
                "system_prompt": "Think before acting.",
                "model_profile_id": "profile-1",
            }
        )

        self.assertEqual(agent.instructions, "Plan effectively")
        self.assertEqual(agent.system_prompt, "Think before acting.")
        self.assertNotIn("objective", agent.model_dump(mode="json"))

    def test_agent_definition_accepts_legacy_objective_field(self):
        agent = AgentDefinition.model_validate(
            {
                "name": "Legacy Agent",
                "role": "planner",
                "objective": "Produce a migration plan",
            }
        )

        self.assertEqual(agent.instructions, "Produce a migration plan")
        self.assertEqual(agent.system_prompt, "Produce a migration plan")
        self.assertNotIn("objective", agent.model_dump(mode="json"))

    def test_execution_record_has_canonical_conversion_wrapper(self):
        execution_record = ExecutionRecord(
            processId="proc-1",
            workflowId="workflow-1",
            userId="user-1",
            startTime=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
            endTime=datetime(2026, 4, 28, 12, 5, tzinfo=timezone.utc),
            status="completed",
            finalOutput="done",
            logFileS3Path="s3://logs/proc-1.json",
            artifactDirectoryS3Path="s3://artifacts/proc-1/",
        )

        canonical_execution = execution_record.to_canonical_execution()
        round_trip_execution = ExecutionRecord.from_canonical_execution(canonical_execution)

        self.assertEqual(canonical_execution.id, "proc-1")
        self.assertEqual(canonical_execution.workflow_id, "workflow-1")
        self.assertEqual(canonical_execution.status.value, "completed")
        self.assertEqual(round_trip_execution.processId, "proc-1")
        self.assertEqual(round_trip_execution.status, "completed")


if __name__ == "__main__":
    unittest.main()

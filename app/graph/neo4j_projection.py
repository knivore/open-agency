"""Neo4j graph projection adapter for Agency graph outbox events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.domain import GraphProjectionEvent
from app.graph.projection import ProjectionBatchResult
from app.modules.registry import optional_module_neo4j_projection_handlers

NEO4J_CONSTRAINTS = (
    "CREATE CONSTRAINT agency_workflow_id IF NOT EXISTS FOR (n:Workflow) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_workflow_version_id IF NOT EXISTS FOR (n:WorkflowVersion) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_workflow_run_id IF NOT EXISTS FOR (n:WorkflowRun) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_step_run_id IF NOT EXISTS FOR (n:StepRun) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_memory_id IF NOT EXISTS FOR (n:Memory) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_document_id IF NOT EXISTS FOR (n:Document) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_document_chunk_id IF NOT EXISTS FOR (n:DocumentChunk) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_tool_id IF NOT EXISTS FOR (n:Tool) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_agent_id IF NOT EXISTS FOR (n:Agent) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_task_id IF NOT EXISTS FOR (n:Task) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_user_id IF NOT EXISTS FOR (n:User) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_conversation_id IF NOT EXISTS FOR (n:Conversation) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_context_pack_id IF NOT EXISTS FOR (n:ContextPack) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_decision_id IF NOT EXISTS FOR (n:Decision) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_constraint_id IF NOT EXISTS FOR (n:Constraint) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_open_question_id IF NOT EXISTS FOR (n:OpenQuestion) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_next_action_id IF NOT EXISTS FOR (n:NextAction) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_schedule_id IF NOT EXISTS FOR (n:Schedule) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_runtime_revision_id IF NOT EXISTS FOR (n:RuntimeRevision) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_runtime_container_id IF NOT EXISTS FOR (n:RuntimeContainer) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_execution_event_id IF NOT EXISTS FOR (n:ExecutionEvent) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_container_event_id IF NOT EXISTS FOR (n:ContainerEvent) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_tool_call_id IF NOT EXISTS FOR (n:ToolCall) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_model_id IF NOT EXISTS FOR (n:Model) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_model_provider_id IF NOT EXISTS FOR (n:ModelProvider) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_model_request_id IF NOT EXISTS FOR (n:ModelRequest) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_artifact_id IF NOT EXISTS FOR (n:Artifact) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_error_id IF NOT EXISTS FOR (n:Error) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_context_health_id IF NOT EXISTS FOR (n:ContextHealth) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_token_usage_id IF NOT EXISTS FOR (n:TokenUsage) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_token_budget_id IF NOT EXISTS FOR (n:TokenBudget) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_context_compaction_id IF NOT EXISTS FOR (n:ContextCompaction) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_monitor_finding_id IF NOT EXISTS FOR (n:MonitorFinding) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_persona_id IF NOT EXISTS FOR (n:Persona) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_persona_version_id IF NOT EXISTS FOR (n:PersonaVersion) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_distillation_run_id IF NOT EXISTS FOR (n:DistillationRun) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_distillation_item_id IF NOT EXISTS FOR (n:DistillationItem) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_source_memory_id IF NOT EXISTS FOR (n:SourceMemory) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_person_id IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_knowledge_id IF NOT EXISTS FOR (n:Knowledge) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_organization_id IF NOT EXISTS FOR (n:Organization) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_event_id IF NOT EXISTS FOR (n:Event) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_device_id IF NOT EXISTS FOR (n:Device) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_device_event_id IF NOT EXISTS FOR (n:DeviceEvent) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_device_command_id IF NOT EXISTS FOR (n:DeviceCommand) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_room_id IF NOT EXISTS FOR (n:Room) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_adapter_id IF NOT EXISTS FOR (n:Adapter) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT agency_location_id IF NOT EXISTS FOR (n:Location) REQUIRE n.id IS UNIQUE",
)

NEO4J_PROJECTED_LABELS = (
    "Workflow",
    "WorkflowVersion",
    "WorkflowRun",
    "StepRun",
    "Memory",
    "Document",
    "DocumentChunk",
    "Tool",
    "Agent",
    "Task",
    "User",
    "Conversation",
    "Entity",
    "ContextPack",
    "Decision",
    "Constraint",
    "OpenQuestion",
    "NextAction",
    "Schedule",
    "RuntimeRevision",
    "RuntimeContainer",
    "ExecutionEvent",
    "ContainerEvent",
    "ToolCall",
    "Model",
    "ModelProvider",
    "ModelRequest",
    "Artifact",
    "Error",
    "ContextHealth",
    "TokenUsage",
    "TokenBudget",
    "ContextCompaction",
    "MonitorFinding",
    "Persona",
    "PersonaVersion",
    "DistillationRun",
    "DistillationItem",
    "SourceMemory",
    "Person",
    "Knowledge",
    "Organization",
    "Event",
    "Device",
    "DeviceEvent",
    "DeviceCommand",
    "Room",
    "Adapter",
    "Location",
)

SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS = {
    "Person",
    "Knowledge",
    "Tool",
    "Workflow",
    "Artifact",
    "Decision",
    "Event",
    "Organization",
    "Persona",
}

SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES = {
    "KNOWS",
    "USES",
    "FOLLOWS",
    "PRODUCES",
    "REVIEWS",
    "APPROVES",
    "ESCALATES_TO",
    "PARTICIPATES_IN",
    "DERIVED_FROM",
    "RELATES_TO",
}


def create_neo4j_driver(settings: Settings):
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


@dataclass(slots=True)
class Neo4jProjectionConfig:
    database: str | None = None


class Neo4jGraphProjector:
    def __init__(self, driver, *, config: Neo4jProjectionConfig | None = None):
        self.driver = driver
        self.config = config or Neo4jProjectionConfig()

    async def close(self) -> None:
        close = getattr(self.driver, "close", None)
        if close is not None:
            await close()

    async def ensure_schema(self) -> None:
        for cypher in NEO4J_CONSTRAINTS:
            await self._run(cypher, {})

    async def clear_projection(self, *, labels: list[str] | None = None) -> None:
        projected_labels = labels or list(NEO4J_PROJECTED_LABELS)
        if not projected_labels:
            return
        await self._run(
            """
            MATCH (n)
            WHERE any(label IN labels(n) WHERE label IN $labels)
            DETACH DELETE n
            """,
            {"labels": projected_labels},
        )

    async def project(self, event: GraphProjectionEvent) -> None:
        projection = self._projection_for_event(event)
        if projection is None:
            return
        cypher, params = projection
        await self._run(cypher, params)

    def _projection_for_event(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]] | None:
        handler = self._handler_for(event.event_type)
        if event.aggregate_type == "step_run" and event.event_type in {
            "execution.started",
            "execution.completed",
            "execution.failed",
        }:
            handler = self._step_run_cypher
        if handler is None:
            return None
        return handler(event)

    async def project_pending(self, event_repository, *, limit: int = 100) -> ProjectionBatchResult:
        events = await event_repository.list_events(status="pending", limit=limit)
        result = ProjectionBatchResult()
        session_kwargs = {}
        if self.config.database:
            session_kwargs["database"] = self.config.database
        async with self.driver.session(**session_kwargs) as session:
            for event in events:
                try:
                    projection = self._projection_for_event(event)
                    if projection is not None:
                        cypher, params = projection
                        await session.run(cypher, **params)
                    await event_repository.mark_projected(event.event_id)
                    result.processed += 1
                    result.checkpoint_event_id = event.event_id
                except Exception as exc:
                    message = str(exc)
                    await event_repository.mark_failed(event.event_id, message)
                    result.failed += 1
                    result.errors.append(f"{event.event_id}: {message}")
        return result

    async def _run(self, cypher: str, params: dict[str, Any]) -> None:
        session_kwargs = {}
        if self.config.database:
            session_kwargs["database"] = self.config.database
        async with self.driver.session(**session_kwargs) as session:
            await session.run(cypher, **params)

    def _handler_for(self, event_type: str):
        return {
            "workflow.created": self._workflow_cypher,
            "workflow.updated": self._workflow_cypher,
            "workflow.deleted": self._workflow_deleted_cypher,
            "execution.started": self._workflow_run_cypher,
            "execution.completed": self._workflow_run_cypher,
            "execution.deleted": self._workflow_run_deleted_cypher,
            "execution.failed": self._workflow_run_cypher,
            "approval.granted": self._execution_detail_event_cypher,
            "approval.rejected": self._execution_detail_event_cypher,
            "approval.requested": self._execution_detail_event_cypher,
            "artifact.created": self._execution_detail_event_cypher,
            "container.created": self._container_event_cypher,
            "container.failed": self._container_event_cypher,
            "container.replaced": self._container_event_cypher,
            "container.started": self._container_event_cypher,
            "container.stopped": self._container_event_cypher,
            "context.compaction.completed": self._execution_detail_event_cypher,
            "context.compaction.failed": self._execution_detail_event_cypher,
            "context.compaction.started": self._execution_detail_event_cypher,
            "context.health.recorded": self._execution_detail_event_cypher,
            "llm.request.created": self._execution_detail_event_cypher,
            "llm.response.created": self._execution_detail_event_cypher,
            "monitor.finding.created": self._execution_detail_event_cypher,
            "runtime.revision.resolved": self._execution_detail_event_cypher,
            "task.started": self._step_run_cypher,
            "token.budget.exceeded": self._execution_detail_event_cypher,
            "token.budget.warning": self._execution_detail_event_cypher,
            "token.usage.recorded": self._execution_detail_event_cypher,
            "agent.step.completed": self._step_run_cypher,
            "agent.step.failed": self._step_run_cypher,
            "tool.call.completed": self._execution_detail_event_cypher,
            "tool.call.failed": self._execution_detail_event_cypher,
            "tool.call.started": self._execution_detail_event_cypher,
            "memory.created": self._memory_cypher,
            "memory.updated": self._memory_cypher,
            "memory.deleted": self._memory_deleted_cypher,
            "memory.entities.extracted": self._memory_entities_cypher,
            "memory.source_intelligence.graph_hints.approved": self._memory_graph_hints_cypher,
            "document_memory_collection.created": self._document_cypher,
            "document_memory_collection.deleted": self._document_deleted_cypher,
            "workflow_memory_link.created": self._workflow_memory_link_cypher,
            "workflow_memory_link.updated": self._workflow_memory_link_cypher,
            "workflow_memory_link.deleted": self._workflow_memory_link_deleted_cypher,
            "persona.factory.distilled": self._persona_cypher,
            "persona.factory.item.updated": self._persona_cypher,
            "persona.factory.item.approved": self._persona_cypher,
            "persona.factory.item.rejected": self._persona_cypher,
            "persona.factory.items.normalized": self._persona_cypher,
            "persona.factory.package.synthesized": self._persona_cypher,
            "persona.factory.package.updated": self._persona_cypher,
            "persona.factory.run.approved": self._persona_cypher,
            "persona.factory.version.published": self._persona_cypher,
            "persona.runtime.invoked": self._persona_cypher,
            **optional_module_neo4j_projection_handlers(),
        }.get(event_type)

    def _persona_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        return (
            """
            MERGE (p:Persona {id: $persona_id})
            SET p.slug = $persona_slug,
                p.name = $persona_name,
                p.status = $persona_status,
                p.workspace_id = $workspace_id,
                p.last_event_type = $event_type,
                p.source_record_type = 'persona',
                p.source_record_id = $persona_id,
                p.updated_at = datetime($occurred_at),
                p.deleted = false
            FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
                MERGE (run:DistillationRun {id: $run_id})
                SET run.persona_id = $persona_id,
                    run.status = $run_status,
                    run.item_count = $item_count,
                    run.active_item_count = $active_item_count,
                    run.needs_review_count = $needs_review_count,
                    run.source_record_type = 'persona_distillation_run',
                    run.source_record_id = $run_id,
                    run.updated_at = datetime($occurred_at),
                    run.deleted = false
                MERGE (p)-[hasRun:PERSONA_HAS_DISTILLATION_RUN]->(run)
                SET hasRun.updated_at = datetime($occurred_at),
                    hasRun.deleted = false
            )
            FOREACH (_ IN CASE WHEN $persona_version_id IS NULL THEN [] ELSE [1] END |
                MERGE (version:PersonaVersion {id: $persona_version_id})
                SET version.persona_id = $persona_id,
                    version.version = $version,
                    version.status = $version_status,
                    version.source_record_type = 'persona_version',
                    version.source_record_id = $persona_version_id,
                    version.updated_at = datetime($occurred_at),
                    version.deleted = false
                MERGE (p)-[hasVersion:PERSONA_HAS_VERSION]->(version)
                SET hasVersion.updated_at = datetime($occurred_at),
                    hasVersion.deleted = false
                FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
                    MERGE (run:DistillationRun {id: $run_id})
                    MERGE (run)-[produced:RUN_PRODUCED_VERSION]->(version)
                    SET produced.updated_at = datetime($occurred_at),
                        produced.deleted = false
                )
            )
            FOREACH (_ IN CASE WHEN $item_id IS NULL THEN [] ELSE [1] END |
                MERGE (item:DistillationItem {id: $item_id})
                SET item.persona_id = $persona_id,
                    item.run_id = $run_id,
                    item.source_memory_id = $source_memory_id,
                    item.item_type = $item_type,
                    item.memory_layer = $memory_layer,
                    item.title = $title,
                    item.review_status = $review_status,
                    item.needs_review = $needs_review,
                    item.source_record_type = 'persona_distillation_item',
                    item.source_record_id = $item_id,
                    item.updated_at = datetime($occurred_at),
                    item.deleted = false
                FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
                    MERGE (run:DistillationRun {id: $run_id})
                    MERGE (run)-[extracted:RUN_EXTRACTED_ITEM]->(item)
                    SET extracted.updated_at = datetime($occurred_at),
                        extracted.deleted = false
                )
                FOREACH (_ IN CASE WHEN $source_memory_id IS NULL THEN [] ELSE [1] END |
                    MERGE (sourceMemory:SourceMemory {id: $source_memory_id})
                    SET sourceMemory.source_record_type = 'memory',
                        sourceMemory.source_record_id = $source_memory_id,
                        sourceMemory.updated_at = datetime($occurred_at),
                        sourceMemory.deleted = false
                    MERGE (item)-[derived:ITEM_DERIVED_FROM_MEMORY]->(sourceMemory)
                    SET derived.updated_at = datetime($occurred_at),
                        derived.deleted = false
                )
            )
            FOREACH (sourceMemoryId IN $source_memory_ids |
                MERGE (sourceMemory:SourceMemory {id: sourceMemoryId})
                SET sourceMemory.source_record_type = 'memory',
                    sourceMemory.source_record_id = sourceMemoryId,
                    sourceMemory.updated_at = datetime($occurred_at),
                    sourceMemory.deleted = false
                FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
                    MERGE (run:DistillationRun {id: $run_id})
                    MERGE (run)-[usedSource:RUN_USED_SOURCE_MEMORY]->(sourceMemory)
                    SET usedSource.updated_at = datetime($occurred_at),
                        usedSource.deleted = false
                )
            )
            FOREACH (memoryId IN $memory_ids |
                MERGE (memory:Memory {id: memoryId})
                MERGE (p)-[publishedMemory:PERSONA_PUBLISHED_MEMORY]->(memory)
                SET publishedMemory.updated_at = datetime($occurred_at),
                    publishedMemory.deleted = false
            )
            FOREACH (tool IN $tools |
                MERGE (toolNode:Tool {id: tool.id})
                SET toolNode.name = tool.name,
                    toolNode.granted = tool.granted,
                    toolNode.confidence = tool.confidence,
                    toolNode.source_record_type = 'persona_tool',
                    toolNode.source_record_id = tool.id,
                    toolNode.updated_at = datetime($occurred_at),
                    toolNode.deleted = false
                MERGE (p)-[usesTool:PERSONA_USES_TOOL]->(toolNode)
                SET usesTool.distillation_item_id = tool.distillation_item_id,
                    usesTool.updated_at = datetime($occurred_at),
                    usesTool.deleted = false
            )
            FOREACH (workflow IN $workflows |
                MERGE (workflowNode:Workflow {id: workflow.id})
                SET workflowNode.name = workflow.name,
                    workflowNode.confidence = workflow.confidence,
                    workflowNode.source_record_type = 'persona_workflow',
                    workflowNode.source_record_id = workflow.id,
                    workflowNode.updated_at = datetime($occurred_at),
                    workflowNode.deleted = false
                MERGE (p)-[followsWorkflow:PERSONA_FOLLOWS_WORKFLOW]->(workflowNode)
                SET followsWorkflow.distillation_item_id = workflow.distillation_item_id,
                    followsWorkflow.updated_at = datetime($occurred_at),
                    followsWorkflow.deleted = false
            )
            FOREACH (artifact IN $artifacts |
                MERGE (artifactNode:Artifact {id: artifact.id})
                SET artifactNode.name = artifact.name,
                    artifactNode.artifact_type = artifact.artifact_type,
                    artifactNode.confidence = artifact.confidence,
                    artifactNode.source_record_type = 'persona_artifact',
                    artifactNode.source_record_id = artifact.id,
                    artifactNode.updated_at = datetime($occurred_at),
                    artifactNode.deleted = false
                MERGE (p)-[producesArtifact:PERSONA_PRODUCES_ARTIFACT]->(artifactNode)
                SET producesArtifact.distillation_item_id = artifact.distillation_item_id,
                    producesArtifact.updated_at = datetime($occurred_at),
                    producesArtifact.deleted = false
            )
            FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                MERGE (agent:Agent {id: $agent_id})
                SET agent.source_record_type = 'agent',
                    agent.source_record_id = $agent_id,
                    agent.updated_at = datetime($occurred_at),
                    agent.deleted = false
                MERGE (p)-[materialized:PERSONA_MATERIALIZED_AS_AGENT]->(agent)
                SET materialized.updated_at = datetime($occurred_at),
                    materialized.deleted = false
            )
            FOREACH (_ IN CASE WHEN $conversation_id IS NULL THEN [] ELSE [1] END |
                MERGE (conversation:Conversation {id: $conversation_id})
                MERGE (p)-[invoked:PERSONA_INVOKED_IN_CONVERSATION]->(conversation)
                SET invoked.message_id = $message_id,
                    invoked.updated_at = datetime($occurred_at),
                    invoked.deleted = false
            )
            """,
            _persona_projection_params(event),
        )

    def _workflow_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        return (
            """
            MERGE (w:Workflow {id: $workflow_id})
            SET w.name = $name,
                w.description = $description,
                w.entrypoint = $entrypoint,
                w.revision = $revision,
                w.version = $version,
                w.labels = $labels,
                w.is_published = $is_published,
                w.created_by_user_id = $created_by_user_id,
                w.workspace_id = $workspace_id,
                w.tenant_id = $tenant_id,
                w.default_runtime_adapter_id = $default_runtime_adapter_id,
                w.allowed_runtime_adapter_ids = $allowed_runtime_adapter_ids,
                w.source_record_type = 'workflow',
                w.source_record_id = $workflow_id,
                w.deleted = false,
                w.updated_at = datetime($occurred_at)
            WITH w
            OPTIONAL MATCH (w)-[oldDefinition:DEFINES_AGENT|DEFINES_TASK|DEFINES_TOOL]->()
            DELETE oldDefinition
            WITH w
            OPTIONAL MATCH ()-[oldScoped:CAN_USE|CAN_HANDOFF_TO|ASSIGNED_TO|USES_TOOL|DEPENDS_ON|USED_MODEL|USES_MODEL_PROFILE]->()
            WHERE oldScoped.workflow_id = $workflow_id
            DELETE oldScoped
            WITH w
            FOREACH (tool IN $tools |
                MERGE (t:Tool {id: tool.id})
                SET t.name = tool.name,
                    t.display_name = tool.display_name,
                    t.description = tool.description,
                    t.tool_type = tool.tool_type,
                    t.tags = tool.tags,
                    t.requires_approval = tool.requires_approval,
                    t.sandbox_required = tool.sandbox_required,
                    t.allow_shell = tool.allow_shell,
                    t.allow_browser = tool.allow_browser,
                    t.allow_filesystem = tool.allow_filesystem,
                    t.allow_network = tool.allow_network,
                    t.read_only = tool.read_only,
                    t.read_only_sql = tool.read_only_sql,
                    t.dangerous = tool.dangerous,
                    t.has_input_schema = tool.has_input_schema,
                    t.has_output_schema = tool.has_output_schema,
                    t.source_record_type = 'tool_definition',
                    t.source_record_id = tool.id,
                    t.defined_in_workflow_id = $workflow_id,
                    t.workspace_id = $workspace_id,
                    t.tenant_id = $tenant_id,
                    t.deleted = false,
                    t.updated_at = datetime($occurred_at)
                MERGE (w)-[:DEFINES_TOOL]->(t)
            )
            FOREACH (agent IN $agents |
                MERGE (a:Agent {id: agent.id})
                SET a.name = agent.name,
                    a.display_name = agent.display_name,
                    a.description = agent.description,
                    a.role = agent.role,
                    a.model_profile_id = agent.model_profile_id,
                    a.memory_enabled = agent.memory_enabled,
                    a.memory_scope = agent.memory_scope,
                    a.memory_strategy = agent.memory_strategy,
                    a.memory_backend_ref = agent.memory_backend_ref,
                    a.source_record_type = 'agent_definition',
                    a.source_record_id = agent.id,
                    a.defined_in_workflow_id = $workflow_id,
                    a.created_by_user_id = $created_by_user_id,
                    a.workspace_id = $workspace_id,
                    a.tenant_id = $tenant_id,
                    a.deleted = false,
                    a.updated_at = datetime($occurred_at)
                MERGE (w)-[:DEFINES_AGENT]->(a)
                FOREACH (tool_id IN agent.tool_ids |
                    MERGE (tool:Tool {id: tool_id})
                    SET tool.source_record_type = coalesce(tool.source_record_type, 'tool_definition'),
                        tool.source_record_id = tool_id,
                        tool.deleted = false,
                        tool.updated_at = datetime($occurred_at)
                    MERGE (a)-[canUse:CAN_USE {workflow_id: $workflow_id}]->(tool)
                    SET canUse.updated_at = datetime($occurred_at),
                        canUse.deleted = false
                )
                FOREACH (handoff_agent_id IN agent.handoff_agent_ids |
                    MERGE (target:Agent {id: handoff_agent_id})
                    SET target.source_record_type = coalesce(target.source_record_type, 'agent_definition'),
                        target.source_record_id = handoff_agent_id,
                        target.created_by_user_id = coalesce(target.created_by_user_id, $created_by_user_id),
                        target.workspace_id = coalesce(target.workspace_id, $workspace_id),
                        target.tenant_id = coalesce(target.tenant_id, $tenant_id),
                        target.deleted = false,
                        target.updated_at = datetime($occurred_at)
                    MERGE (a)-[handoff:CAN_HANDOFF_TO {workflow_id: $workflow_id}]->(target)
                    SET handoff.updated_at = datetime($occurred_at),
                        handoff.deleted = false
                )
                FOREACH (_ IN CASE WHEN agent.model_profile_id IS NULL THEN [] ELSE [1] END |
                    MERGE (model:Model {id: agent.model_profile_id})
                    SET model.name = coalesce(model.name, agent.model_profile_id),
                        model.source_record_type = 'model_profile',
                        model.source_record_id = agent.model_profile_id,
                        model.deleted = false,
                        model.updated_at = datetime($occurred_at)
                    MERGE (a)-[usedModel:USED_MODEL {workflow_id: $workflow_id}]->(model)
                    SET usedModel.updated_at = datetime($occurred_at),
                        usedModel.deleted = false
                    MERGE (a)-[usesModelProfile:USES_MODEL_PROFILE {workflow_id: $workflow_id}]->(model)
                    SET usesModelProfile.updated_at = datetime($occurred_at),
                        usesModelProfile.deleted = false
                )
            )
            FOREACH (task IN $tasks |
                MERGE (t:Task {id: task.id})
                SET t.name = task.name,
                    t.description = task.description,
                    t.agent_id = task.agent_id,
                    t.human_approval_required = task.human_approval_required,
                    t.has_input_schema = task.has_input_schema,
                    t.has_output_schema = task.has_output_schema,
                    t.source_record_type = 'task_definition',
                    t.source_record_id = task.id,
                    t.defined_in_workflow_id = $workflow_id,
                    t.created_by_user_id = $created_by_user_id,
                    t.workspace_id = $workspace_id,
                    t.tenant_id = $tenant_id,
                    t.deleted = false,
                    t.updated_at = datetime($occurred_at)
                MERGE (w)-[:DEFINES_TASK]->(t)
                FOREACH (_ IN CASE WHEN task.agent_id IS NULL THEN [] ELSE [1] END |
                    MERGE (a:Agent {id: task.agent_id})
                    SET a.source_record_type = coalesce(a.source_record_type, 'agent_definition'),
                        a.source_record_id = task.agent_id,
                        a.created_by_user_id = coalesce(a.created_by_user_id, $created_by_user_id),
                        a.workspace_id = coalesce(a.workspace_id, $workspace_id),
                        a.tenant_id = coalesce(a.tenant_id, $tenant_id),
                        a.deleted = false,
                        a.updated_at = datetime($occurred_at)
                    MERGE (t)-[assigned:ASSIGNED_TO {workflow_id: $workflow_id}]->(a)
                    SET assigned.updated_at = datetime($occurred_at),
                        assigned.deleted = false
                )
                FOREACH (tool_id IN task.tool_ids |
                    MERGE (tool:Tool {id: tool_id})
                    SET tool.source_record_type = coalesce(tool.source_record_type, 'tool_definition'),
                        tool.source_record_id = tool_id,
                        tool.workspace_id = coalesce(tool.workspace_id, $workspace_id),
                        tool.tenant_id = coalesce(tool.tenant_id, $tenant_id),
                        tool.deleted = false,
                        tool.updated_at = datetime($occurred_at)
                    MERGE (t)-[usesTool:USES_TOOL {workflow_id: $workflow_id}]->(tool)
                    SET usesTool.updated_at = datetime($occurred_at),
                        usesTool.deleted = false
                )
                FOREACH (dependency_id IN task.depends_on_task_ids |
                    MERGE (dependency:Task {id: dependency_id})
                    SET dependency.source_record_type = coalesce(dependency.source_record_type, 'task_definition'),
                        dependency.source_record_id = dependency_id,
                        dependency.created_by_user_id = coalesce(dependency.created_by_user_id, $created_by_user_id),
                        dependency.workspace_id = coalesce(dependency.workspace_id, $workspace_id),
                        dependency.tenant_id = coalesce(dependency.tenant_id, $tenant_id),
                        dependency.deleted = false,
                        dependency.updated_at = datetime($occurred_at)
                    MERGE (t)-[dependsOn:DEPENDS_ON {workflow_id: $workflow_id}]->(dependency)
                    SET dependsOn.updated_at = datetime($occurred_at),
                        dependsOn.deleted = false
                )
            )
            FOREACH (_ IN CASE WHEN $workflow_version_id IS NULL THEN [] ELSE [1] END |
                MERGE (version:WorkflowVersion {id: $workflow_version_id})
                SET version.workflow_id = $workflow_id,
                    version.revision = $revision,
                    version.version = $version,
                    version.labels = $labels,
                    version.status = CASE WHEN $is_published THEN 'published' ELSE 'draft' END,
                    version.is_current = true,
                    version.source_record_type = 'workflow_version',
                    version.source_record_id = $workflow_version_id,
                    version.created_by_user_id = $created_by_user_id,
                    version.workspace_id = $workspace_id,
                    version.tenant_id = $tenant_id,
                    version.updated_at = datetime($occurred_at),
                    version.deleted = false
                MERGE (w)-[hasVersion:HAS_VERSION]->(version)
                SET hasVersion.current = true,
                    hasVersion.updated_at = datetime($occurred_at),
                    hasVersion.deleted = false
            )
            """,
            {
                "workflow_id": payload.get("workflow_id") or event.aggregate_id,
                "name": payload.get("name"),
                "description": payload.get("description"),
                "entrypoint": payload.get("entrypoint"),
                "revision": payload.get("revision"),
                "version": payload.get("version"),
                "labels": payload.get("labels") or [],
                "is_published": payload.get("is_published"),
                "workflow_version_id": _workflow_version_id(payload, payload.get("workflow_id") or event.aggregate_id),
                **_boundary_params(event, payload),
                "default_runtime_adapter_id": payload.get("default_runtime_adapter_id"),
                "allowed_runtime_adapter_ids": payload.get("allowed_runtime_adapter_ids") or [],
                "agents": _dict_items(
                    payload.get("agents"),
                    required_key="id",
                    list_keys=("tool_ids", "handoff_agent_ids"),
                ),
                "tasks": _dict_items(
                    payload.get("tasks"),
                    required_key="id",
                    list_keys=("tool_ids", "depends_on_task_ids"),
                ),
                "tools": _dict_items(payload.get("tools"), required_key="id", list_keys=("tags",)),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _workflow_deleted_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        return (
            """
            MERGE (w:Workflow {id: $workflow_id})
            SET w.deleted = true,
                w.deleted_at = datetime($occurred_at),
                w.updated_at = datetime($occurred_at)
            WITH w
            OPTIONAL MATCH (w)-[definitionRel:DEFINES_AGENT|DEFINES_TASK|DEFINES_TOOL|LINKS_MEMORY|HAS_MEMORY_LINK|HAS_RUN|STARTED]->()
            FOREACH (_ IN CASE WHEN definitionRel IS NULL THEN [] ELSE [1] END |
                SET definitionRel.deleted = true,
                    definitionRel.deleted_at = datetime($occurred_at),
                    definitionRel.updated_at = datetime($occurred_at)
            )
            WITH w
            OPTIONAL MATCH ()-[scopedRel:CAN_USE|CAN_HANDOFF_TO|ASSIGNED_TO|USES_TOOL|DEPENDS_ON|USED_MODEL|USES_MODEL_PROFILE|HAS_MEMORY_LINK|LINKS_MEMORY]->()
            WHERE scopedRel.workflow_id = $workflow_id
            SET scopedRel.deleted = true,
                scopedRel.deleted_at = datetime($occurred_at),
                scopedRel.updated_at = datetime($occurred_at)
            """,
            {"workflow_id": event.payload.get("workflow_id") or event.aggregate_id,
             "occurred_at": event.occurred_at.isoformat()},
        )

    def _workflow_run_deleted_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        execution_id = event.payload.get("execution_id") or event.aggregate_id
        return (
            """
            MERGE (r:WorkflowRun {id: $execution_id})
            SET r.status = 'deleted',
                r.deleted = true,
                r.deleted_at = datetime($occurred_at),
                r.updated_at = datetime($occurred_at)
            WITH r
            OPTIONAL MATCH (r)-[outRel]->()
            WHERE outRel IS NULL OR type(outRel) IN $run_relationship_types
            FOREACH (_ IN CASE WHEN outRel IS NULL THEN [] ELSE [1] END |
                SET outRel.deleted = true,
                    outRel.deleted_at = datetime($occurred_at),
                    outRel.updated_at = datetime($occurred_at)
            )
            WITH r
            OPTIONAL MATCH ()-[inRel]->(r)
            WHERE inRel IS NULL OR type(inRel) IN $run_relationship_types
            FOREACH (_ IN CASE WHEN inRel IS NULL THEN [] ELSE [1] END |
                SET inRel.deleted = true,
                    inRel.deleted_at = datetime($occurred_at),
                    inRel.updated_at = datetime($occurred_at)
            )
            WITH r
            OPTIONAL MATCH (r)-[:HAS_STEP_RUN]->(step:StepRun)
            FOREACH (_ IN CASE WHEN step IS NULL THEN [] ELSE [1] END |
                SET step.deleted = true,
                    step.deleted_at = datetime($occurred_at),
                    step.updated_at = datetime($occurred_at)
            )
            WITH r
            OPTIONAL MATCH (r)-[:EMITTED_EVENT]->(eventNode)
            WHERE eventNode IS NULL OR eventNode:ExecutionEvent OR eventNode:ContainerEvent
            FOREACH (_ IN CASE WHEN eventNode IS NULL THEN [] ELSE [1] END |
                SET eventNode.deleted = true,
                    eventNode.deleted_at = datetime($occurred_at),
                    eventNode.updated_at = datetime($occurred_at)
            )
            """,
            {
                "execution_id": execution_id,
                "run_relationship_types": [
                    "CREATED_CONTAINER",
                    "EMITTED_EVENT",
                    "FAILED_WITH",
                    "HAS_BUDGET_SIGNAL",
                    "HAS_COMPACTION",
                    "HAS_CONTEXT_HEALTH",
                    "HAS_RUN",
                    "HAS_STEP_RUN",
                    "OCCURRED_IN",
                    "PARTICIPATED_IN",
                    "PRODUCED_ARTIFACT",
                    "RAISED_FINDING",
                    "RECORDED_USAGE",
                    "SOURCE_EXECUTION",
                    "STARTED",
                    "TRIGGERED",
                    "USED_WORKFLOW_VERSION",
                    "USED_MODEL",
                    "USED_RUNTIME",
                ],
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _workflow_run_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        params = _execution_event_params(event)
        return (
            """
            MERGE (r:WorkflowRun {id: $execution_id})
            SET r.status = $status,
                r.trace_id = $trace_id,
                r.workflow_version_id = $workflow_version_id,
                r.runtime_adapter_id = $runtime_adapter_id,
                r.runtime_revision_id = $runtime_revision_id,
                r.runtime_fingerprint = $runtime_fingerprint,
                r.created_by_user_id = $created_by_user_id,
                r.workspace_id = $workspace_id,
                r.tenant_id = $tenant_id,
                r.trigger_type = $trigger_type,
                r.started_at = $started_at,
                r.completed_at = $completed_at,
                r.error = $error_message,
                r.canonical_type = 'Run',
                r.source_record_type = 'execution',
                r.source_record_id = $execution_id,
                r.source_event_id = $source_event_id,
                r.updated_at = datetime($occurred_at)
            WITH r
            MERGE (w:Workflow {id: $workflow_id})
            SET w.created_by_user_id = coalesce(w.created_by_user_id, $created_by_user_id),
                w.workspace_id = coalesce(w.workspace_id, $workspace_id),
                w.tenant_id = coalesce(w.tenant_id, $tenant_id)
            MERGE (w)-[hasRun:HAS_RUN]->(r)
            SET hasRun.updated_at = datetime($occurred_at),
                hasRun.deleted = false
            MERGE (w)-[started:STARTED]->(r)
            SET started.updated_at = datetime($occurred_at),
                started.deleted = false
            WITH r, w
            FOREACH (_ IN CASE WHEN $workflow_version_id IS NULL THEN [] ELSE [1] END |
                MERGE (version:WorkflowVersion {id: $workflow_version_id})
                SET version.workflow_id = $workflow_id,
                    version.source_record_type = 'workflow_version',
                    version.source_record_id = $workflow_version_id,
                    version.created_by_user_id = $created_by_user_id,
                    version.workspace_id = $workspace_id,
                    version.tenant_id = $tenant_id,
                    version.updated_at = datetime($occurred_at),
                    version.deleted = false
                MERGE (w)-[hasVersion:HAS_VERSION]->(version)
                SET hasVersion.updated_at = datetime($occurred_at),
                    hasVersion.deleted = false
                MERGE (r)-[usedWorkflowVersion:USED_WORKFLOW_VERSION]->(version)
                SET usedWorkflowVersion.updated_at = datetime($occurred_at),
                    usedWorkflowVersion.deleted = false
            )
            WITH r
            FOREACH (_ IN CASE WHEN $schedule_id IS NULL THEN [] ELSE [1] END |
                MERGE (s:Schedule {id: $schedule_id})
                SET s.trigger_type = $trigger_type,
                    s.source_record_type = 'schedule',
                    s.source_record_id = $schedule_id,
                    s.updated_at = datetime($occurred_at),
                    s.deleted = false
                MERGE (s)-[:TRIGGERED]->(r)
            )
            FOREACH (_ IN CASE WHEN $runtime_revision_id IS NULL THEN [] ELSE [1] END |
                MERGE (rev:RuntimeRevision {id: $runtime_revision_id})
                SET rev.runtime_adapter_id = $runtime_adapter_id,
                    rev.fingerprint = $runtime_fingerprint,
                    rev.source_record_type = 'runtime_revision',
                    rev.source_record_id = $runtime_revision_id,
                    rev.updated_at = datetime($occurred_at),
                    rev.deleted = false
                MERGE (r)-[:USED_RUNTIME]->(rev)
            )
            FOREACH (_ IN CASE WHEN $container_source_id IS NULL THEN [] ELSE [1] END |
                MERGE (container:RuntimeContainer {id: $container_source_id})
                SET container.container_id = $container_id,
                    container.name = $container_name,
                    container.image = $container_image,
                    container.status = $container_status,
                    container.started_at = $container_started_at,
                    container.ended_at = $container_ended_at,
                    container.exit_code = $container_exit_code,
                    container.source_record_type = 'runtime_container',
                    container.source_record_id = $container_source_id,
                    container.updated_at = datetime($occurred_at),
                    container.deleted = false
                MERGE (r)-[:CREATED_CONTAINER]->(container)
            )
            FOREACH (_ IN CASE WHEN $error_message IS NULL THEN [] ELSE [1] END |
                MERGE (err:Error {id: $error_id})
                SET err.message = $error_message,
                    err.status = $status,
                    err.source_record_type = 'execution',
                    err.source_record_id = $execution_id,
                    err.updated_at = datetime($occurred_at),
                    err.deleted = false
                MERGE (r)-[:FAILED_WITH]->(err)
            )
            """,
            {
                **params,
                "execution_id": payload.get("execution_id") or event.aggregate_id,
                "workflow_id": payload.get("workflow_id"),
                "status": _status_from_event(event),
                "trace_id": payload.get("trace_id"),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _step_run_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        step_id = payload.get("task_id") or event.aggregate_id
        run_id = payload.get("execution_id")
        params = _execution_event_params(event)
        return (
            """
            MERGE (s:StepRun {id: $step_run_id})
            SET s.task_id = $task_id,
                s.agent_id = $agent_id,
                s.status = $status,
                s.created_by_user_id = $created_by_user_id,
                s.workspace_id = $workspace_id,
                s.tenant_id = $tenant_id,
                s.source_record_type = 'step_run',
                s.source_record_id = $step_run_id,
                s.source_event_id = $source_event_id,
                s.updated_at = datetime($occurred_at)
            WITH s
            MERGE (r:WorkflowRun {id: $execution_id})
            MERGE (r)-[:HAS_STEP_RUN]->(s)
            WITH s, r
            FOREACH (_ IN CASE WHEN $task_id IS NULL THEN [] ELSE [1] END |
                MERGE (t:Task {id: $task_id})
                SET t.source_record_type = 'task',
                    t.source_record_id = $task_id,
                    t.created_by_user_id = coalesce(t.created_by_user_id, $created_by_user_id),
                    t.workspace_id = coalesce(t.workspace_id, $workspace_id),
                    t.tenant_id = coalesce(t.tenant_id, $tenant_id),
                    t.updated_at = datetime($occurred_at),
                    t.deleted = false
                MERGE (t)-[:OCCURRED_IN]->(r)
            )
            FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                MERGE (a:Agent {id: $agent_id})
                SET a.source_record_type = 'agent',
                    a.source_record_id = $agent_id,
                    a.created_by_user_id = coalesce(a.created_by_user_id, $created_by_user_id),
                    a.workspace_id = coalesce(a.workspace_id, $workspace_id),
                    a.tenant_id = coalesce(a.tenant_id, $tenant_id),
                    a.updated_at = datetime($occurred_at),
                    a.deleted = false
                MERGE (a)-[:PARTICIPATED_IN]->(r)
                MERGE (s)-[:ASSIGNED_TO]->(a)
            )
            FOREACH (_ IN CASE WHEN $error_message IS NULL THEN [] ELSE [1] END |
                MERGE (err:Error {id: $error_id})
                SET err.message = $error_message,
                    err.status = $status,
                    err.source_record_type = 'step_run',
                    err.source_record_id = $step_run_id,
                    err.updated_at = datetime($occurred_at),
                    err.deleted = false
                MERGE (s)-[:FAILED_WITH]->(err)
            )
            """,
            {
                **params,
                "step_run_id": event.aggregate_id,
                "execution_id": run_id,
                "task_id": step_id,
                "agent_id": payload.get("agent_id"),
                "status": _status_from_event(event),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _execution_detail_event_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        return self._event_detail_cypher(event, label="ExecutionEvent")

    def _container_event_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        return self._event_detail_cypher(event, label="ContainerEvent")

    def _event_detail_cypher(self, event: GraphProjectionEvent, *, label: str) -> tuple[str, dict[str, Any]]:
        params = _execution_event_params(event)
        event_node = "containerEvent" if label == "ContainerEvent" else "executionEvent"
        cypher = f"""
            MERGE (r:WorkflowRun {{id: $execution_id}})
            SET r.canonical_type = 'Run',
                r.source_record_type = 'execution',
                r.source_record_id = $execution_id,
                r.created_by_user_id = coalesce(r.created_by_user_id, $created_by_user_id),
                r.workspace_id = coalesce(r.workspace_id, $workspace_id),
                r.tenant_id = coalesce(r.tenant_id, $tenant_id),
                r.updated_at = datetime($occurred_at)
            MERGE ({event_node}:{label} {{id: $event_id}})
            SET {event_node}.event_type = $event_type,
                {event_node}.execution_id = $execution_id,
                {event_node}.sequence = $sequence,
                {event_node}.status = $status,
                {event_node}.agent_id = $agent_id,
                {event_node}.task_id = $task_id,
                {event_node}.created_by_user_id = $created_by_user_id,
                {event_node}.workspace_id = $workspace_id,
                {event_node}.tenant_id = $tenant_id,
                {event_node}.tool_call_id = $tool_call_id,
                {event_node}.model_request_id = $model_request_id,
                {event_node}.payload_keys = $payload_keys,
                {event_node}.metric_keys = $metric_keys,
                {event_node}.source_record_type = 'execution_event',
                {event_node}.source_record_id = $event_id,
                {event_node}.source_event_id = $source_event_id,
                {event_node}.updated_at = datetime($occurred_at),
                {event_node}.deleted = false
            MERGE (r)-[:EMITTED_EVENT]->({event_node})
            WITH r, {event_node}
            OPTIONAL MATCH (previousExecutionEvent:ExecutionEvent {{execution_id: $execution_id, sequence: $previous_sequence}})
            WHERE coalesce(previousExecutionEvent.deleted, false) = false
            OPTIONAL MATCH (previousContainerEvent:ContainerEvent {{execution_id: $execution_id, sequence: $previous_sequence}})
            WHERE coalesce(previousContainerEvent.deleted, false) = false
            WITH r, {event_node}, coalesce(previousExecutionEvent, previousContainerEvent) AS previousEvent
            FOREACH (_ IN CASE WHEN previousEvent IS NULL THEN [] ELSE [1] END |
                MERGE (previousEvent)-[followed:FOLLOWED_BY]->({event_node})
                SET followed.execution_id = $execution_id,
                    followed.sequence = $sequence,
                    followed.updated_at = datetime($occurred_at),
                    followed.deleted = false
            )
            WITH r, {event_node}
            OPTIONAL MATCH (nextExecutionEvent:ExecutionEvent {{execution_id: $execution_id, sequence: $next_sequence}})
            WHERE coalesce(nextExecutionEvent.deleted, false) = false
            OPTIONAL MATCH (nextContainerEvent:ContainerEvent {{execution_id: $execution_id, sequence: $next_sequence}})
            WHERE coalesce(nextContainerEvent.deleted, false) = false
            WITH r, {event_node}, coalesce(nextExecutionEvent, nextContainerEvent) AS nextEvent
            FOREACH (_ IN CASE WHEN nextEvent IS NULL THEN [] ELSE [1] END |
                MERGE ({event_node})-[followed:FOLLOWED_BY]->(nextEvent)
                SET followed.execution_id = $execution_id,
                    followed.sequence = $next_sequence,
                    followed.updated_at = datetime($occurred_at),
                    followed.deleted = false
            )
            WITH r, {event_node}
            FOREACH (_ IN CASE WHEN $parent_event_id IS NULL THEN [] ELSE [1] END |
                MERGE (parent:ExecutionEvent {{id: $parent_event_id}})
                MERGE (parent)-[:PARENT_OF]->({event_node})
            )
            FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                MERGE (a:Agent {{id: $agent_id}})
                SET a.source_record_type = 'agent',
                    a.source_record_id = $agent_id,
                    a.updated_at = datetime($occurred_at),
                    a.deleted = false
                MERGE (a)-[:PARTICIPATED_IN]->(r)
                MERGE (a)-[:EMITTED_EVENT]->({event_node})
            )
            FOREACH (_ IN CASE WHEN $task_id IS NULL THEN [] ELSE [1] END |
                MERGE (t:Task {{id: $task_id}})
                SET t.source_record_type = 'task',
                    t.source_record_id = $task_id,
                    t.updated_at = datetime($occurred_at),
                    t.deleted = false
                MERGE (t)-[:OCCURRED_IN]->(r)
            )
            FOREACH (_ IN CASE WHEN $tool_call_id IS NULL THEN [] ELSE [1] END |
                MERGE (toolCall:ToolCall {{id: $tool_call_id}})
                SET toolCall.name = $tool_name,
                    toolCall.status = $status,
                    toolCall.source_record_type = 'tool_call',
                    toolCall.source_record_id = $tool_call_id,
                    toolCall.updated_at = datetime($occurred_at),
                    toolCall.deleted = false
                MERGE (toolCall)-[:OCCURRED_IN]->(r)
                MERGE ({event_node})-[:CALLED_TOOL]->(toolCall)
            )
            FOREACH (_ IN CASE WHEN $model_request_id IS NULL THEN [] ELSE [1] END |
                MERGE (modelRequest:ModelRequest {{id: $model_request_id}})
                SET modelRequest.provider = $model_provider,
                    modelRequest.model = $model_name,
                    modelRequest.status = $status,
                    modelRequest.source_record_type = 'model_request',
                    modelRequest.source_record_id = $model_request_id,
                    modelRequest.updated_at = datetime($occurred_at),
                    modelRequest.deleted = false
                MERGE (modelRequest)-[:OCCURRED_IN]->(r)
            )
            FOREACH (_ IN CASE WHEN $model_id IS NULL THEN [] ELSE [1] END |
                MERGE (model:Model {{id: $model_id}})
                SET model.name = $model_name,
                    model.provider = $model_provider,
                    model.source_record_type = 'model',
                    model.source_record_id = $model_id,
                    model.updated_at = datetime($occurred_at),
                    model.deleted = false
                MERGE (r)-[:USED_MODEL]->(model)
                FOREACH (_ IN CASE WHEN $model_request_id IS NULL THEN [] ELSE [1] END |
                    MERGE (modelRequest:ModelRequest {{id: $model_request_id}})
                    MERGE (modelRequest)-[:USED_MODEL]->(model)
                )
            )
            FOREACH (_ IN CASE WHEN $model_provider_id IS NULL THEN [] ELSE [1] END |
                MERGE (provider:ModelProvider {{id: $model_provider_id}})
                SET provider.name = $model_provider,
                    provider.source_record_type = 'model_provider',
                    provider.source_record_id = $model_provider_id,
                    provider.updated_at = datetime($occurred_at),
                    provider.deleted = false
                FOREACH (_ IN CASE WHEN $model_id IS NULL THEN [] ELSE [1] END |
                    MERGE (model:Model {{id: $model_id}})
                    MERGE (model)-[:USED_PROVIDER]->(provider)
                )
            )
            FOREACH (_ IN CASE WHEN $artifact_id IS NULL THEN [] ELSE [1] END |
                MERGE (artifact:Artifact {{id: $artifact_id}})
                SET artifact.name = $artifact_name,
                    artifact.path = $artifact_path,
                    artifact.uri = $artifact_uri,
                    artifact.source_record_type = 'artifact',
                    artifact.source_record_id = $artifact_id,
                    artifact.updated_at = datetime($occurred_at),
                    artifact.deleted = false
                MERGE (r)-[:PRODUCED_ARTIFACT]->(artifact)
            )
            FOREACH (_ IN CASE WHEN $runtime_revision_id IS NULL THEN [] ELSE [1] END |
                MERGE (rev:RuntimeRevision {{id: $runtime_revision_id}})
                SET rev.runtime_adapter_id = $runtime_adapter_id,
                    rev.fingerprint = $runtime_fingerprint,
                    rev.source_record_type = 'runtime_revision',
                    rev.source_record_id = $runtime_revision_id,
                    rev.updated_at = datetime($occurred_at),
                    rev.deleted = false
                MERGE (r)-[:USED_RUNTIME]->(rev)
            )
            FOREACH (_ IN CASE WHEN $container_source_id IS NULL THEN [] ELSE [1] END |
                MERGE (container:RuntimeContainer {{id: $container_source_id}})
                SET container.container_id = $container_id,
                    container.name = $container_name,
                    container.image = $container_image,
                    container.status = $container_status,
                    container.started_at = $container_started_at,
                    container.ended_at = $container_ended_at,
                    container.exit_code = $container_exit_code,
                    container.source_record_type = 'runtime_container',
                    container.source_record_id = $container_source_id,
                    container.updated_at = datetime($occurred_at),
                    container.deleted = false
                MERGE (r)-[:CREATED_CONTAINER]->(container)
            )
            FOREACH (_ IN CASE WHEN $error_message IS NULL THEN [] ELSE [1] END |
                MERGE (err:Error {{id: $error_id}})
                SET err.message = $error_message,
                    err.status = $status,
                    err.source_record_type = 'execution_event',
                    err.source_record_id = $event_id,
                    err.updated_at = datetime($occurred_at),
                    err.deleted = false
                MERGE ({event_node})-[:FAILED_WITH]->(err)
                MERGE (r)-[:FAILED_WITH]->(err)
            )
            FOREACH (_ IN CASE WHEN $context_health_id IS NULL THEN [] ELSE [1] END |
                MERGE (contextHealth:ContextHealth {{id: $context_health_id}})
                SET contextHealth.status = $context_health_status,
                    contextHealth.estimated_prompt_tokens = $context_estimated_prompt_tokens,
                    contextHealth.reserved_completion_tokens = $context_reserved_completion_tokens,
                    contextHealth.estimated_total_context_tokens = $context_estimated_total_context_tokens,
                    contextHealth.context_window = $context_window,
                    contextHealth.remaining_context_tokens = $context_remaining_context_tokens,
                    contextHealth.usage_ratio = $context_usage_ratio,
                    contextHealth.after_compaction = $context_after_compaction,
                    contextHealth.compaction_reason = $context_compaction_reason,
                    contextHealth.source_record_type = 'context_health',
                    contextHealth.source_record_id = $context_health_id,
                    contextHealth.source_event_id = $source_event_id,
                    contextHealth.updated_at = datetime($occurred_at),
                    contextHealth.deleted = false
                MERGE (r)-[:HAS_CONTEXT_HEALTH]->(contextHealth)
                MERGE ({event_node})-[:RECORDED_CONTEXT_HEALTH]->(contextHealth)
                SET r.context_health_status = $context_health_status,
                    r.context_usage_ratio = $context_usage_ratio,
                    r.remaining_context_tokens = $context_remaining_context_tokens
                FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                    MERGE (a:Agent {{id: $agent_id}})
                    SET a.context_health_status = $context_health_status,
                        a.context_usage_ratio = $context_usage_ratio,
                        a.updated_at = datetime($occurred_at),
                        a.deleted = false
                )
                FOREACH (_ IN CASE WHEN $model_request_id IS NULL THEN [] ELSE [1] END |
                    MERGE (modelRequest:ModelRequest {{id: $model_request_id}})
                    SET modelRequest.context_health_status = $context_health_status,
                        modelRequest.context_usage_ratio = $context_usage_ratio,
                        modelRequest.updated_at = datetime($occurred_at),
                        modelRequest.deleted = false
                )
            )
            FOREACH (_ IN CASE WHEN $token_usage_id IS NULL THEN [] ELSE [1] END |
                MERGE (tokenUsage:TokenUsage {{id: $token_usage_id}})
                SET tokenUsage.provider = $token_usage_provider,
                    tokenUsage.model = $token_usage_model,
                    tokenUsage.prompt_tokens = $token_usage_prompt_tokens,
                    tokenUsage.completion_tokens = $token_usage_completion_tokens,
                    tokenUsage.total_tokens = $token_usage_total_tokens,
                    tokenUsage.cached_tokens = $token_usage_cached_tokens,
                    tokenUsage.reasoning_tokens = $token_usage_reasoning_tokens,
                    tokenUsage.estimated_cost = $token_usage_estimated_cost,
                    tokenUsage.currency = $token_usage_currency,
                    tokenUsage.estimated = $token_usage_estimated,
                    tokenUsage.source_record_type = 'token_usage',
                    tokenUsage.source_record_id = $token_usage_id,
                    tokenUsage.source_event_id = $source_event_id,
                    tokenUsage.updated_at = datetime($occurred_at),
                    tokenUsage.deleted = false
                MERGE (r)-[:RECORDED_USAGE]->(tokenUsage)
                MERGE ({event_node})-[:RECORDED_USAGE]->(tokenUsage)
                SET r.last_token_usage_total_tokens = $token_usage_total_tokens,
                    r.last_token_usage_prompt_tokens = $token_usage_prompt_tokens,
                    r.last_token_usage_completion_tokens = $token_usage_completion_tokens,
                    r.last_estimated_cost = $token_usage_estimated_cost,
                    r.last_token_usage_currency = $token_usage_currency
                FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                    MERGE (a:Agent {{id: $agent_id}})
                    SET a.last_token_usage_total_tokens = $token_usage_total_tokens,
                        a.last_estimated_cost = $token_usage_estimated_cost,
                        a.last_token_usage_currency = $token_usage_currency,
                        a.updated_at = datetime($occurred_at),
                        a.deleted = false
                )
                FOREACH (_ IN CASE WHEN $model_id IS NULL THEN [] ELSE [1] END |
                    MERGE (model:Model {{id: $model_id}})
                    SET model.last_token_usage_total_tokens = $token_usage_total_tokens,
                        model.last_estimated_cost = $token_usage_estimated_cost,
                        model.last_token_usage_currency = $token_usage_currency,
                        model.updated_at = datetime($occurred_at),
                        model.deleted = false
                )
                FOREACH (_ IN CASE WHEN $model_request_id IS NULL THEN [] ELSE [1] END |
                    MERGE (modelRequest:ModelRequest {{id: $model_request_id}})
                    SET modelRequest.last_token_usage_total_tokens = $token_usage_total_tokens,
                        modelRequest.last_token_usage_prompt_tokens = $token_usage_prompt_tokens,
                        modelRequest.last_token_usage_completion_tokens = $token_usage_completion_tokens,
                        modelRequest.last_estimated_cost = $token_usage_estimated_cost,
                        modelRequest.last_token_usage_currency = $token_usage_currency,
                        modelRequest.updated_at = datetime($occurred_at),
                        modelRequest.deleted = false
                    MERGE (modelRequest)-[:RECORDED_USAGE]->(tokenUsage)
                )
            )
            FOREACH (_ IN CASE WHEN $token_budget_id IS NULL THEN [] ELSE [1] END |
                MERGE (tokenBudget:TokenBudget {{id: $token_budget_id}})
                SET tokenBudget.scope = $token_budget_scope,
                    tokenBudget.status = $token_budget_status,
                    tokenBudget.action = $token_budget_action,
                    tokenBudget.used_tokens = $token_budget_used_tokens,
                    tokenBudget.budget_tokens = $token_budget_budget_tokens,
                    tokenBudget.usage_ratio = $token_budget_usage_ratio,
                    tokenBudget.source_record_type = 'token_budget',
                    tokenBudget.source_record_id = $token_budget_id,
                    tokenBudget.source_event_id = $source_event_id,
                    tokenBudget.updated_at = datetime($occurred_at),
                    tokenBudget.deleted = false
                MERGE (r)-[:HAS_BUDGET_SIGNAL]->(tokenBudget)
                MERGE ({event_node})-[:HAS_BUDGET_SIGNAL]->(tokenBudget)
                SET r.token_budget_status = $token_budget_status,
                    r.token_budget_action = $token_budget_action,
                    r.token_budget_usage_ratio = $token_budget_usage_ratio
            )
            FOREACH (_ IN CASE WHEN $context_compaction_id IS NULL THEN [] ELSE [1] END |
                MERGE (contextCompaction:ContextCompaction {{id: $context_compaction_id}})
                SET contextCompaction.status = $context_compaction_status,
                    contextCompaction.reason = $context_compaction_reason,
                    contextCompaction.compacted = $context_compacted,
                    contextCompaction.memory_id = $context_compaction_memory_id,
                    contextCompaction.source_model_request_id = $context_compaction_source_model_request_id,
                    contextCompaction.estimated_tokens_saved = $context_estimated_tokens_saved,
                    contextCompaction.context_status_before = $context_status_before,
                    contextCompaction.context_status_after = $context_status_after,
                    contextCompaction.context_usage_ratio_before = $context_usage_ratio_before,
                    contextCompaction.context_usage_ratio_after = $context_usage_ratio_after,
                    contextCompaction.source_record_type = 'context_compaction',
                    contextCompaction.source_record_id = $context_compaction_id,
                    contextCompaction.source_event_id = $source_event_id,
                    contextCompaction.updated_at = datetime($occurred_at),
                    contextCompaction.deleted = false
                MERGE (r)-[:HAS_COMPACTION]->(contextCompaction)
                MERGE ({event_node})-[:HAS_COMPACTION]->(contextCompaction)
                SET r.context_compaction_status = $context_compaction_status,
                    r.context_compaction_reason = $context_compaction_reason,
                    r.context_estimated_tokens_saved = $context_estimated_tokens_saved
                FOREACH (_ IN CASE WHEN $context_compaction_memory_id IS NULL THEN [] ELSE [1] END |
                    MERGE (memory:Memory {{id: $context_compaction_memory_id}})
                    MERGE (contextCompaction)-[:CREATED_MEMORY]->(memory)
                )
            )
            FOREACH (_ IN CASE WHEN $monitor_finding_id IS NULL THEN [] ELSE [1] END |
                MERGE (monitorFinding:MonitorFinding {{id: $monitor_finding_id}})
                SET monitorFinding.finding_type = $monitor_finding_type,
                    monitorFinding.severity = $monitor_finding_severity,
                    monitorFinding.status = $monitor_finding_status,
                    monitorFinding.summary = $monitor_finding_summary,
                    monitorFinding.source_record_type = 'monitor_finding',
                    monitorFinding.source_record_id = $monitor_finding_id,
                    monitorFinding.source_event_id = $source_event_id,
                    monitorFinding.updated_at = datetime($occurred_at),
                    monitorFinding.deleted = false
                MERGE (r)-[:RAISED_FINDING]->(monitorFinding)
                MERGE ({event_node})-[:RAISED_FINDING]->(monitorFinding)
            )
        """
        return cypher, params

    def _memory_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        return (
            """
            MERGE (m:Memory {id: $memory_id})
            SET m.scope = $scope,
                m.summary = $summary,
                m.tags = $tags,
                m.sensitive = $sensitive,
                m.created_by_user_id = $created_by_user_id,
                m.workspace_id = $workspace_id,
                m.missing_embedding = $missing_embedding,
                m.source = $source,
                m.memory_type = $memory_type,
                m.status = $status,
                m.importance = $importance,
                m.summary_date = $summary_date,
                m.archived_window_start = $archived_window_start,
                m.archived_window_end = $archived_window_end,
                m.started_at = $started_at,
                m.ended_at = $ended_at,
                m.document_id = $document_id,
                m.filename = $filename,
                m.content_type = $content_type,
                m.content_sha256 = $content_sha256,
                m.chunk_index = $chunk_index,
                m.chunk_count = $chunk_count,
                m.start_char = $start_char,
                m.end_char = $end_char,
                m.semantic_hint = $semantic_hint,
                m.mode = $mode,
                m.source_range = $source_range,
                m.source_message_start_id = $source_message_start_id,
                m.source_message_end_id = $source_message_end_id,
                m.source_message_start_at = $source_message_start_at,
                m.source_message_end_at = $source_message_end_at,
                m.source_message_count = $source_message_count,
                m.updated_at = datetime($updated_at),
                m.deleted = false
            WITH m
            FOREACH (_ IN CASE WHEN $created_by_user_id IS NULL THEN [] ELSE [1] END |
                MERGE (u:User {id: $created_by_user_id})
                MERGE (u)-[:CREATED_MEMORY]->(m)
            )
            FOREACH (_ IN CASE WHEN $workflow_id IS NULL THEN [] ELSE [1] END |
                MERGE (w:Workflow {id: $workflow_id})
                MERGE (m)-[:AVAILABLE_TO {scope: 'workflow'}]->(w)
            )
            FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                MERGE (a:Agent {id: $agent_id})
                MERGE (m)-[:AVAILABLE_TO {scope: 'agent'}]->(a)
            )
            FOREACH (_ IN CASE WHEN $conversation_id IS NULL THEN [] ELSE [1] END |
                MERGE (c:Conversation {id: $conversation_id})
                MERGE (m)-[:AVAILABLE_TO {scope: 'conversation'}]->(c)
            )
            FOREACH (_ IN CASE WHEN $source_conversation_id IS NULL THEN [] ELSE [1] END |
                MERGE (sourceConversation:Conversation {id: $source_conversation_id})
                MERGE (m)-[:SOURCE_CONVERSATION]->(sourceConversation)
            )
            FOREACH (_ IN CASE WHEN $source_execution_id IS NULL THEN [] ELSE [1] END |
                MERGE (r:WorkflowRun {id: $source_execution_id})
                MERGE (m)-[:SOURCE_EXECUTION]->(r)
            )
            FOREACH (_ IN CASE WHEN $supersedes_memory_id IS NULL THEN [] ELSE [1] END |
                MERGE (superseded:Memory {id: $supersedes_memory_id})
                MERGE (m)-[:SUPERSEDES]->(superseded)
            )
            FOREACH (_ IN CASE WHEN $context_pack_id IS NULL THEN [] ELSE [1] END |
                MERGE (contextPack:ContextPack {id: $context_pack_id})
                SET contextPack.summary = $summary,
                    contextPack.mode = $context_pack_mode,
                    contextPack.graph_context_source = $graph_context_source,
                    contextPack.working_set_id = $working_set_id,
                    contextPack.source_execution_id = $source_execution_id,
                    contextPack.source_conversation_id = $source_conversation_id,
                    contextPack.created_by_user_id = $created_by_user_id,
                    contextPack.workspace_id = $workspace_id,
                    contextPack.tenant_id = $tenant_id,
                    contextPack.source_record_type = 'context_pack',
                    contextPack.source_record_id = $context_pack_id,
                    contextPack.updated_at = datetime($updated_at),
                    contextPack.deleted = false
                MERGE (m)-[:HAS_CONTEXT_PACK]->(contextPack)
            )
            FOREACH (decision IN CASE WHEN $context_pack_id IS NULL THEN [] ELSE $context_pack_decisions END |
                MERGE (decisionNode:Decision {id: decision.id})
                SET decisionNode.summary = decision.summary,
                    decisionNode.source_record_type = 'context_pack_item',
                    decisionNode.source_record_id = decision.id,
                    decisionNode.updated_at = datetime($updated_at),
                    decisionNode.deleted = false
                MERGE (contextPack:ContextPack {id: $context_pack_id})
                MERGE (contextPack)-[:SUMMARIZES]->(decisionNode)
                MERGE (m)-[:MENTIONS]->(decisionNode)
            )
            FOREACH (constraint IN CASE WHEN $context_pack_id IS NULL THEN [] ELSE $context_pack_constraints END |
                MERGE (constraintNode:Constraint {id: constraint.id})
                SET constraintNode.summary = constraint.summary,
                    constraintNode.source_record_type = 'context_pack_item',
                    constraintNode.source_record_id = constraint.id,
                    constraintNode.updated_at = datetime($updated_at),
                    constraintNode.deleted = false
                MERGE (contextPack:ContextPack {id: $context_pack_id})
                MERGE (contextPack)-[:SUMMARIZES]->(constraintNode)
                MERGE (m)-[:MENTIONS]->(constraintNode)
            )
            FOREACH (question IN CASE WHEN $context_pack_id IS NULL THEN [] ELSE $context_pack_open_questions END |
                MERGE (questionNode:OpenQuestion {id: question.id})
                SET questionNode.summary = question.summary,
                    questionNode.source_record_type = 'context_pack_item',
                    questionNode.source_record_id = question.id,
                    questionNode.updated_at = datetime($updated_at),
                    questionNode.deleted = false
                MERGE (contextPack:ContextPack {id: $context_pack_id})
                MERGE (contextPack)-[:SUMMARIZES]->(questionNode)
                MERGE (m)-[:MENTIONS]->(questionNode)
            )
            FOREACH (action IN CASE WHEN $context_pack_id IS NULL THEN [] ELSE $context_pack_next_actions END |
                MERGE (actionNode:NextAction {id: action.id})
                SET actionNode.summary = action.summary,
                    actionNode.source_record_type = 'context_pack_item',
                    actionNode.source_record_id = action.id,
                    actionNode.updated_at = datetime($updated_at),
                    actionNode.deleted = false
                MERGE (contextPack:ContextPack {id: $context_pack_id})
                MERGE (contextPack)-[:SUMMARIZES]->(actionNode)
                MERGE (m)-[:MENTIONS]->(actionNode)
            )
            """,
            {
                "memory_id": payload.get("memory_id") or event.aggregate_id,
                "tenant_id": payload.get("tenant_id") or event.tenant_id,
                "scope": payload.get("scope"),
                "summary": payload.get("summary"),
                "tags": payload.get("tags") or [],
                "sensitive": payload.get("sensitive"),
                "created_by_user_id": payload.get("created_by_user_id"),
                "workspace_id": payload.get("workspace_id"),
                "missing_embedding": _memory_missing_embedding(payload),
                "conversation_id": payload.get("conversation_id"),
                "workflow_id": payload.get("workflow_id"),
                "agent_id": payload.get("agent_id"),
                "source": payload.get("source"),
                "memory_type": payload.get("memory_type"),
                "status": payload.get("status"),
                "importance": payload.get("importance"),
                "summary_date": payload.get("summary_date"),
                "archived_window_start": payload.get("archived_window_start"),
                "archived_window_end": payload.get("archived_window_end"),
                "source_conversation_id": payload.get("source_conversation_id"),
                "source_execution_id": payload.get("source_execution_id"),
                "supersedes_memory_id": payload.get("supersedes_memory_id"),
                "started_at": _memory_started_at(payload),
                "ended_at": _memory_ended_at(payload),
                "document_id": _metadata_get(payload, "document_id"),
                "filename": _metadata_get(payload, "filename"),
                "content_type": _metadata_get(payload, "content_type"),
                "content_sha256": _metadata_get(payload, "content_sha256"),
                "chunk_index": _metadata_get(payload, "chunk_index"),
                "chunk_count": _metadata_get(payload, "chunk_count"),
                "start_char": _metadata_get(payload, "start_char"),
                "end_char": _metadata_get(payload, "end_char"),
                "semantic_hint": _metadata_get(payload, "semantic_hint"),
                "mode": _metadata_get(payload, "mode"),
                "source_range": _metadata_get(payload, "source_range"),
                "source_message_start_id": _metadata_get(payload, "source_message_start_id"),
                "source_message_end_id": _metadata_get(payload, "source_message_end_id"),
                "source_message_start_at": _metadata_get(payload, "source_message_start_at"),
                "source_message_end_at": _metadata_get(payload, "source_message_end_at"),
                "source_message_count": _metadata_get(payload, "source_message_count"),
                "context_pack_id": _context_pack_id(payload, event.aggregate_id),
                "context_pack_mode": _metadata_get(payload, "mode"),
                "graph_context_source": _metadata_get(payload, "graph_context_source"),
                "working_set_id": _metadata_get(payload, "working_set_id") or _metadata_get(payload,
                                                                                            "graph_working_set_id"),
                "context_pack_decisions": _context_pack_items(payload, "decisions", prefix="decision"),
                "context_pack_constraints": _context_pack_items(payload, "constraints", prefix="constraint"),
                "context_pack_open_questions": _context_pack_items(payload, "open_questions", prefix="open-question"),
                "context_pack_next_actions": _context_pack_items(payload, "next_actions", prefix="next-action"),
                "updated_at": payload.get("updated_at") or event.occurred_at.isoformat(),
            },
        )

    def _memory_deleted_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        return (
            """
            MERGE (m:Memory {id: $memory_id})
            SET m.status = 'deleted',
                m.deleted = true,
                m.deleted_at = datetime($occurred_at),
                m.updated_at = datetime($occurred_at)
            WITH m
            OPTIONAL MATCH (m)-[outRel]->()
            FOREACH (_ IN CASE WHEN outRel IS NULL THEN [] ELSE [1] END |
                SET outRel.deleted = true,
                    outRel.deleted_at = datetime($occurred_at),
                    outRel.updated_at = datetime($occurred_at)
            )
            WITH m
            OPTIONAL MATCH ()-[inRel]->(m)
            FOREACH (_ IN CASE WHEN inRel IS NULL THEN [] ELSE [1] END |
                SET inRel.deleted = true,
                    inRel.deleted_at = datetime($occurred_at),
                    inRel.updated_at = datetime($occurred_at)
            )
            """,
            {"memory_id": event.payload.get("memory_id") or event.aggregate_id,
             "occurred_at": event.occurred_at.isoformat()},
        )

    def _memory_entities_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        entities = [
            entity
            for entity in payload.get("entities", [])
            if isinstance(entity, dict) and entity.get("id") and entity.get("name")
        ]
        return (
            """
            MERGE (m:Memory {id: $memory_id})
            WITH m
            UNWIND $entities AS entity
            MERGE (e:Entity {id: entity.id})
            SET e.name = entity.name,
                e.normalized_name = entity.normalized_name,
                e.entity_type = entity.entity_type,
                e.updated_at = datetime($occurred_at),
                e.deleted = false
            MERGE (m)-[rel:MENTIONS {extractor_version: entity.extractor_version, entity_id: entity.id}]->(e)
            SET rel.confidence = entity.confidence,
                rel.source_fields = entity.source_fields,
                rel.updated_at = datetime($occurred_at),
                rel.deleted = false
            FOREACH (_ IN CASE WHEN $document_id IS NULL THEN [] ELSE [1] END |
                MERGE (d:Document {id: $document_id})
                MERGE (d)-[docRel:MENTIONS {extractor_version: entity.extractor_version, entity_id: entity.id}]->(e)
                SET docRel.confidence = entity.confidence,
                    docRel.source_fields = entity.source_fields,
                    docRel.updated_at = datetime($occurred_at),
                    docRel.deleted = false
            )
            """,
            {
                "memory_id": payload.get("memory_id") or event.aggregate_id,
                "document_id": payload.get("document_id"),
                "entities": entities,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _memory_graph_hints_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        relationship_groups = _source_graph_relationship_groups(payload, event)
        return (
            """
            MERGE (m:Memory {id: $memory_id})
            SET m.source_intelligence_graph_hints_review_status = 'approved',
                m.source_intelligence_graph_hints_reviewed_at = $reviewed_at,
                m.source_intelligence_graph_hints_reviewed_by_user_id = $reviewed_by_user_id,
                m.updated_at = datetime($occurred_at),
                m.deleted = false
            FOREACH (_ IN CASE WHEN $document_id IS NULL THEN [] ELSE [1] END |
                MERGE (d:Document {id: $document_id})
                SET d.filename = coalesce(d.filename, $filename),
                    d.updated_at = datetime($occurred_at),
                    d.deleted = false
                MERGE (m)-[:SOURCE_DOCUMENT]->(d)
            )
            FOREACH (_ IN CASE WHEN $persona_id IS NULL THEN [] ELSE [1] END |
                MERGE (p:Persona {id: $persona_id})
                SET p.source_record_type = coalesce(p.source_record_type, 'persona'),
                    p.source_record_id = $persona_id,
                    p.updated_at = datetime($occurred_at),
                    p.deleted = false
            )
            FOREACH (_ IN CASE WHEN $distillation_item_id IS NULL THEN [] ELSE [1] END |
                MERGE (item:DistillationItem {id: $distillation_item_id})
                SET item.persona_id = $persona_id,
                    item.run_id = $run_id,
                    item.source_memory_id = $memory_id,
                    item.item_type = $item_type,
                    item.memory_layer = $memory_layer,
                    item.review_status = 'approved',
                    item.source_record_type = 'persona_distillation_item',
                    item.source_record_id = $distillation_item_id,
                    item.updated_at = datetime($occurred_at),
                    item.deleted = false
                MERGE (sourceMemory:SourceMemory {id: $memory_id})
                SET sourceMemory.source_record_type = 'memory',
                    sourceMemory.source_record_id = $memory_id,
                    sourceMemory.updated_at = datetime($occurred_at),
                    sourceMemory.deleted = false
                MERGE (item)-[derived:ITEM_DERIVED_FROM_MEMORY]->(sourceMemory)
                SET derived.updated_at = datetime($occurred_at),
                    derived.deleted = false
                FOREACH (_ IN CASE WHEN $persona_id IS NULL THEN [] ELSE [1] END |
                    MERGE (p:Persona {id: $persona_id})
                    MERGE (p)-[mentionsItem:MENTIONS {source: $graph_hint_source, distillation_item_id: $distillation_item_id}]->(item)
                    SET mentionsItem.review_status = 'approved',
                        mentionsItem.updated_at = datetime($occurred_at),
                        mentionsItem.deleted = false
                )
            )
            WITH m
            UNWIND $entities AS entity
            MERGE (hint:Entity {id: entity.id})
            SET hint.name = entity.name,
                hint.normalized_name = entity.normalized_name,
                hint.entity_type = entity.label,
                hint.source_label = entity.label,
                hint.confidence = entity.confidence,
                hint.evidence = entity.evidence,
                hint.source_record_type = 'memory_source_intelligence_graph_hint',
                hint.source_record_id = entity.id,
                hint.source_memory_id = $memory_id,
                hint.document_id = $document_id,
                hint.filename = $filename,
                hint.chunk_index = $chunk_index,
                hint.updated_at = datetime($occurred_at),
                hint.deleted = false
            FOREACH (_ IN CASE WHEN entity.label = 'Person' THEN [1] ELSE [] END | SET hint:Person)
            FOREACH (_ IN CASE WHEN entity.label = 'Knowledge' THEN [1] ELSE [] END | SET hint:Knowledge)
            FOREACH (_ IN CASE WHEN entity.label = 'Tool' THEN [1] ELSE [] END | SET hint:Tool)
            FOREACH (_ IN CASE WHEN entity.label = 'Workflow' THEN [1] ELSE [] END | SET hint:Workflow)
            FOREACH (_ IN CASE WHEN entity.label = 'Artifact' THEN [1] ELSE [] END | SET hint:Artifact)
            FOREACH (_ IN CASE WHEN entity.label = 'Decision' THEN [1] ELSE [] END | SET hint:Decision)
            FOREACH (_ IN CASE WHEN entity.label = 'Event' THEN [1] ELSE [] END | SET hint:Event)
            FOREACH (_ IN CASE WHEN entity.label = 'Organization' THEN [1] ELSE [] END | SET hint:Organization)
            FOREACH (_ IN CASE WHEN entity.label = 'Persona' THEN [1] ELSE [] END | SET hint:Persona)
            MERGE (m)-[mentions:MENTIONS {source: 'source_intelligence', entity_id: entity.id}]->(hint)
            SET mentions.confidence = entity.confidence,
                mentions.evidence = entity.evidence,
                mentions.review_status = 'approved',
                mentions.updated_at = datetime($occurred_at),
                mentions.deleted = false
            FOREACH (_ IN CASE WHEN $persona_id IS NULL THEN [] ELSE [1] END |
                MERGE (p:Persona {id: $persona_id})
                MERGE (p)-[personaMentions:MENTIONS {source: $graph_hint_source, entity_id: entity.id}]->(hint)
                SET personaMentions.confidence = entity.confidence,
                    personaMentions.evidence = entity.evidence,
                    personaMentions.review_status = 'approved',
                    personaMentions.distillation_item_id = $distillation_item_id,
                    personaMentions.source_memory_id = $memory_id,
                    personaMentions.updated_at = datetime($occurred_at),
                    personaMentions.deleted = false
            )
            FOREACH (_ IN CASE WHEN $distillation_item_id IS NULL THEN [] ELSE [1] END |
                MERGE (item:DistillationItem {id: $distillation_item_id})
                MERGE (item)-[itemMentions:MENTIONS {source: $graph_hint_source, entity_id: entity.id}]->(hint)
                SET itemMentions.confidence = entity.confidence,
                    itemMentions.evidence = entity.evidence,
                    itemMentions.review_status = 'approved',
                    itemMentions.source_memory_id = $memory_id,
                    itemMentions.updated_at = datetime($occurred_at),
                    itemMentions.deleted = false
            )
            FOREACH (_ IN CASE WHEN $document_id IS NULL THEN [] ELSE [1] END |
                MERGE (d:Document {id: $document_id})
                MERGE (d)-[docMentions:MENTIONS {source: 'source_intelligence', entity_id: entity.id}]->(hint)
                SET docMentions.confidence = entity.confidence,
                    docMentions.evidence = entity.evidence,
                    docMentions.review_status = 'approved',
                    docMentions.updated_at = datetime($occurred_at),
                    docMentions.deleted = false
            )
            WITH m
            FOREACH (rel IN $knows_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:KNOWS {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $uses_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:USES {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $follows_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:FOLLOWS {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $produces_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:PRODUCES {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $reviews_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:REVIEWS {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $approves_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:APPROVES {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $escalates_to_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:ESCALATES_TO {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $participates_in_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:PARTICIPATES_IN {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $derived_from_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:DERIVED_FROM {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            FOREACH (rel IN $relates_to_relationships |
                MERGE (source:Entity {id: rel.source_id})
                MERGE (target:Entity {id: rel.target_id})
                MERGE (source)-[edge:RELATES_TO {memory_id: $memory_id, target_id: rel.target_id}]->(target)
                SET edge.confidence = rel.confidence, edge.evidence = rel.evidence, edge.review_status = 'approved', edge.updated_at = datetime($occurred_at), edge.deleted = false
            )
            """,
            {
                "memory_id": payload.get("memory_id") or event.aggregate_id,
                "document_id": payload.get("document_id"),
                "filename": payload.get("filename"),
                "chunk_index": payload.get("chunk_index"),
                "persona_id": payload.get("persona_id"),
                "run_id": payload.get("run_id"),
                "distillation_item_id": payload.get("distillation_item_id"),
                "item_type": payload.get("item_type"),
                "memory_layer": payload.get("memory_layer"),
                "graph_hint_source": payload.get("graph_hint_source") or "source_intelligence",
                "entities": _source_graph_entities(payload, event),
                **relationship_groups,
                "reviewed_at": _dict_value(payload.get("review")).get("reviewed_at"),
                "reviewed_by_user_id": _dict_value(payload.get("review")).get("reviewed_by_user_id"),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _document_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        return (
            """
            MERGE (d:Document {id: $document_id})
            SET d.filename = $filename,
                d.content_type = $content_type,
                d.content_sha256 = $content_sha256,
                d.chunk_count = $chunk_count,
                d.projected_chunk_count = $projected_chunk_count,
                d.omitted_chunk_count = $omitted_chunk_count,
                d.projection_capped = $projection_capped,
                d.scope = $scope,
                d.created_by_user_id = $created_by_user_id,
                d.workspace_id = $workspace_id,
                d.missing_embedding = $missing_embedding,
                d.deleted = false,
                d.updated_at = datetime($occurred_at)
            WITH d
            FOREACH (_ IN CASE WHEN $created_by_user_id IS NULL THEN [] ELSE [1] END |
                MERGE (u:User {id: $created_by_user_id})
                MERGE (u)-[:OWNS_DOCUMENT]->(d)
            )
            FOREACH (_ IN CASE WHEN $workflow_id IS NULL THEN [] ELSE [1] END |
                MERGE (w:Workflow {id: $workflow_id})
                MERGE (d)-[:AVAILABLE_TO {scope: 'workflow'}]->(w)
            )
            FOREACH (_ IN CASE WHEN $agent_id IS NULL THEN [] ELSE [1] END |
                MERGE (a:Agent {id: $agent_id})
                MERGE (d)-[:AVAILABLE_TO {scope: 'agent'}]->(a)
            )
            FOREACH (_ IN CASE WHEN $conversation_id IS NULL THEN [] ELSE [1] END |
                MERGE (c:Conversation {id: $conversation_id})
                MERGE (d)-[:AVAILABLE_TO {scope: 'conversation'}]->(c)
            )
            WITH d
            UNWIND $chunks AS chunk
            MERGE (chunkNode:DocumentChunk {id: chunk.id})
            SET chunkNode.document_id = $document_id,
                chunkNode.memory_id = chunk.memory_id,
                chunkNode.chunk_index = chunk.chunk_index,
                chunkNode.source_record_type = 'document_chunk',
                chunkNode.source_record_id = chunk.id,
                chunkNode.updated_at = datetime($occurred_at),
                chunkNode.deleted = false
            MERGE (m:Memory {id: chunk.memory_id})
            MERGE (d)-[:HAS_CHUNK]->(chunkNode)
            MERGE (chunkNode)-[:PART_OF_DOCUMENT]->(d)
            MERGE (m)-[:PART_OF_DOCUMENT]->(d)
            MERGE (m)-[:SOURCE_DOCUMENT]->(d)
            """,
            {
                "document_id": payload.get("document_id") or event.aggregate_id,
                "scope": payload.get("scope"),
                "created_by_user_id": payload.get("created_by_user_id"),
                "workspace_id": payload.get("workspace_id"),
                "conversation_id": payload.get("conversation_id"),
                "workflow_id": payload.get("workflow_id"),
                "agent_id": payload.get("agent_id"),
                "filename": payload.get("filename"),
                "content_type": payload.get("content_type"),
                "content_sha256": payload.get("content_sha256"),
                "chunk_count": payload.get("chunk_count"),
                "projected_chunk_count": payload.get("projected_chunk_count") or len(payload.get("memory_ids") or []),
                "omitted_chunk_count": payload.get("omitted_chunk_count") or 0,
                "projection_capped": bool(payload.get("projection_capped")),
                "memory_ids": payload.get("memory_ids") or [],
                "chunks": _document_chunks(payload),
                "missing_embedding": bool(payload.get("missing_embedding")),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _document_deleted_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        payload = event.payload
        return (
            """
            MERGE (d:Document {id: $document_id})
            SET d.deleted = true,
                d.deleted_at = datetime($occurred_at),
                d.updated_at = datetime($occurred_at)
            WITH d
            OPTIONAL MATCH (d)-[docOutRel]->()
            FOREACH (_ IN CASE WHEN docOutRel IS NULL THEN [] ELSE [1] END |
                SET docOutRel.deleted = true,
                    docOutRel.deleted_at = datetime($occurred_at),
                    docOutRel.updated_at = datetime($occurred_at)
            )
            WITH d
            OPTIONAL MATCH ()-[docInRel]->(d)
            FOREACH (_ IN CASE WHEN docInRel IS NULL THEN [] ELSE [1] END |
                SET docInRel.deleted = true,
                    docInRel.deleted_at = datetime($occurred_at),
                    docInRel.updated_at = datetime($occurred_at)
            )
            WITH d
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(chunk:DocumentChunk)
            FOREACH (_ IN CASE WHEN chunk IS NULL THEN [] ELSE [1] END |
                SET chunk.deleted = true,
                    chunk.deleted_at = datetime($occurred_at),
                    chunk.updated_at = datetime($occurred_at)
            )
            WITH d
            UNWIND $memory_ids AS memory_id
            MERGE (m:Memory {id: memory_id})
            SET m.deleted = true,
                m.status = 'deleted',
                m.updated_at = datetime($occurred_at)
            WITH d, m
            OPTIONAL MATCH (m)-[memoryRel]-()
            FOREACH (_ IN CASE WHEN memoryRel IS NULL THEN [] ELSE [1] END |
                SET memoryRel.deleted = true,
                    memoryRel.deleted_at = datetime($occurred_at),
                    memoryRel.updated_at = datetime($occurred_at)
            )
            """,
            {
                "document_id": payload.get("document_id") or event.aggregate_id,
                "memory_ids": payload.get("memory_ids") or [],
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _workflow_memory_link_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        link = event.payload.get("link") if isinstance(event.payload.get("link"), dict) else {}
        return (
            """
            MERGE (w:Workflow {id: $workflow_id})
            WITH w
            UNWIND $memory_ids AS memory_id
            MERGE (m:Memory {id: memory_id})
            MERGE (w)-[rel:LINKS_MEMORY {link_id: $link_id, target_type: $target_type, target_id: $target_id}]->(m)
            SET rel.ref_type = $ref_type,
                rel.ref_id = $ref_id,
                rel.access_mode = $access_mode,
                rel.label = $label,
                rel.workflow_id = $workflow_id,
                rel.deleted = false,
                rel.updated_at = datetime($occurred_at)
            WITH w, m
            FOREACH (_ IN CASE WHEN $target_type = 'workflow' THEN [1] ELSE [] END |
                MERGE (w)-[workflowMemoryRel:HAS_MEMORY_LINK {link_id: $link_id, workflow_id: $workflow_id}]->(m)
                SET workflowMemoryRel.ref_type = $ref_type,
                    workflowMemoryRel.ref_id = $ref_id,
                    workflowMemoryRel.access_mode = $access_mode,
                    workflowMemoryRel.label = $label,
                    workflowMemoryRel.deleted = false,
                    workflowMemoryRel.updated_at = datetime($occurred_at)
            )
            FOREACH (_ IN CASE WHEN $target_type = 'agent' AND $target_id IS NOT NULL THEN [1] ELSE [] END |
                MERGE (a:Agent {id: $target_id})
                MERGE (w)-[:DEFINES_AGENT]->(a)
                MERGE (a)-[agentMemoryRel:HAS_MEMORY_LINK {link_id: $link_id, workflow_id: $workflow_id}]->(m)
                SET agentMemoryRel.ref_type = $ref_type,
                    agentMemoryRel.ref_id = $ref_id,
                    agentMemoryRel.access_mode = $access_mode,
                    agentMemoryRel.label = $label,
                    agentMemoryRel.deleted = false,
                    agentMemoryRel.updated_at = datetime($occurred_at)
            )
            FOREACH (_ IN CASE WHEN $target_type = 'task' AND $target_id IS NOT NULL THEN [1] ELSE [] END |
                MERGE (t:Task {id: $target_id})
                MERGE (w)-[:DEFINES_TASK]->(t)
                MERGE (t)-[taskMemoryRel:HAS_MEMORY_LINK {link_id: $link_id, workflow_id: $workflow_id}]->(m)
                SET taskMemoryRel.ref_type = $ref_type,
                    taskMemoryRel.ref_id = $ref_id,
                    taskMemoryRel.access_mode = $access_mode,
                    taskMemoryRel.label = $label,
                    taskMemoryRel.deleted = false,
                    taskMemoryRel.updated_at = datetime($occurred_at)
            )
            """,
            {
                "workflow_id": event.payload.get("workflow_id"),
                "link_id": link.get("id") or event.aggregate_id,
                "target_type": link.get("targetType"),
                "target_id": link.get("targetId"),
                "ref_type": link.get("refType"),
                "ref_id": link.get("refId"),
                "memory_ids": link.get("memoryIds") or [],
                "access_mode": link.get("accessMode"),
                "label": link.get("label"),
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    def _workflow_memory_link_deleted_cypher(self, event: GraphProjectionEvent) -> tuple[str, dict[str, Any]]:
        link = event.payload.get("link") if isinstance(event.payload.get("link"), dict) else {}
        return (
            """
            MATCH (:Workflow {id: $workflow_id})-[rel:LINKS_MEMORY {link_id: $link_id}]->(:Memory)
            SET rel.deleted = true,
                rel.deleted_at = datetime($occurred_at),
                rel.updated_at = datetime($occurred_at)
            WITH count(rel) AS deleted_link_count
            OPTIONAL MATCH ()-[targetRel:HAS_MEMORY_LINK {link_id: $link_id, workflow_id: $workflow_id}]->(:Memory)
            SET targetRel.deleted = true,
                targetRel.deleted_at = datetime($occurred_at),
                targetRel.updated_at = datetime($occurred_at)
            """,
            {
                "workflow_id": event.payload.get("workflow_id"),
                "link_id": link.get("id") or event.aggregate_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )


def _status_from_event(event: GraphProjectionEvent) -> str:
    explicit = event.payload.get("status")
    if isinstance(explicit, str) and explicit:
        return explicit
    suffix = event.event_type.rsplit(".", 1)[-1]
    return suffix if suffix else "unknown"


def _metadata_get(payload: dict[str, Any], key: str) -> Any:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get(key)


def _boundary_params(event: GraphProjectionEvent, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": payload.get("tenant_id") or event.tenant_id,
        "created_by_user_id": payload.get("created_by_user_id") or payload.get("user_id") or event.user_id,
        "workspace_id": payload.get("workspace_id"),
    }


def _workflow_version_id(payload: dict[str, Any], workflow_id: Any) -> str | None:
    explicit = _first_string(payload.get("workflow_version_id"), payload.get("workflow_version"))
    if explicit:
        return explicit
    clean_workflow_id = _first_string(workflow_id)
    revision = payload.get("revision")
    if clean_workflow_id and revision is not None:
        return f"{clean_workflow_id}:v{revision}"
    return None


def _dict_items(
        value: Any,
        *,
        required_key: str | None = None,
        list_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if required_key is not None and not item.get(required_key):
            continue
        normalized = dict(item)
        for key in list_keys:
            if not isinstance(normalized.get(key), list):
                normalized[key] = []
        items.append(normalized)
    return items


def _document_chunks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    document_id = str(payload.get("document_id") or "").strip()
    chunks: list[dict[str, Any]] = []
    for index, memory_id in enumerate(payload.get("memory_ids") or []):
        clean_memory_id = str(memory_id or "").strip()
        if not clean_memory_id:
            continue
        chunks.append(
            {
                "id": f"{document_id}:chunk:{index}" if document_id else f"document_chunk:{clean_memory_id}",
                "memory_id": clean_memory_id,
                "chunk_index": index,
            }
        )
    return chunks


def _memory_started_at(payload: dict[str, Any]) -> Any:
    return payload.get("archived_window_start") or _metadata_get(payload, "source_message_start_at")


def _memory_ended_at(payload: dict[str, Any]) -> Any:
    return payload.get("archived_window_end") or _metadata_get(payload, "source_message_end_at")


def _memory_missing_embedding(payload: dict[str, Any]) -> bool:
    if "missing_embedding" in payload:
        return bool(payload.get("missing_embedding"))
    return not bool(payload.get("embedding_model_profile_id"))


def _context_pack_id(payload: dict[str, Any], aggregate_id: str) -> str | None:
    if payload.get("memory_type") != "context_pack":
        return None
    return _first_string(payload.get("memory_id"), aggregate_id)


def _context_pack_items(payload: dict[str, Any], key: str, *, prefix: str) -> list[dict[str, str]]:
    context_pack_id = _context_pack_id(payload, str(payload.get("memory_id") or ""))
    if context_pack_id is None:
        return []
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    raw_items = metadata.get(key)
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, str]] = []
    for index, item in enumerate(raw_items):
        if isinstance(item, str):
            summary = item.strip()
            item_id = f"{context_pack_id}:{prefix}:{index}"
        elif isinstance(item, dict):
            summary = _first_string(item.get("summary"), item.get("text"), item.get("title"), item.get("name")) or ""
            item_id = _first_string(item.get("id"), item.get("node_id")) or f"{context_pack_id}:{prefix}:{index}"
        else:
            continue
        if not summary:
            continue
        items.append({"id": item_id, "summary": summary[:500]})
    return items[:50]


def _execution_event_params(event: GraphProjectionEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    trigger_payload = payload.get("trigger_payload") if isinstance(payload.get("trigger_payload"), dict) else {}
    usage_payload = event_payload.get("usage") if isinstance(event_payload.get("usage"), dict) else {}
    budget_payload = event_payload.get("budget") if isinstance(event_payload.get("budget"), dict) else {}
    record_payload = event_payload.get("record") if isinstance(event_payload.get("record"), dict) else {}
    context_health_payload = (
        event_payload.get("context_health")
        if isinstance(event_payload.get("context_health"), dict)
        else event_payload
        if event.event_type == "context.health.recorded"
        else {}
    )
    context_health_before = (
        event_payload.get("context_health_before") if isinstance(event_payload.get("context_health_before"),
                                                                 dict) else {}
    )
    context_health_after = (
        event_payload.get("context_health_after") if isinstance(event_payload.get("context_health_after"), dict) else {}
    )
    execution_id = payload.get("execution_id") or event.aggregate_id
    event_id = event.source_event_id or f"{execution_id}:{event.event_type}:{payload.get('sequence') or event.event_id}"
    tool_call_id = _first_string(
        payload.get("tool_call_id"),
        event_payload.get("tool_call_id"),
        event_payload.get("toolCallId"),
    )
    model_request_id = _first_string(
        payload.get("model_request_id"),
        event_payload.get("model_request_id"),
        event_payload.get("modelRequestId"),
    )
    artifact_id = _first_string(
        event_payload.get("artifact_id"),
        event_payload.get("artifactId"),
        event_payload.get("id") if "artifact" in event.event_type else None,
    )
    artifact_name = _first_string(
        event_payload.get("artifact_name"),
        event_payload.get("artifactName"),
        event_payload.get("name") if "artifact" in event.event_type else None,
        _basename(_first_string(event_payload.get("path"), event_payload.get("uri"))),
    )
    container_id = _first_string(
        payload.get("container_id"),
        event_payload.get("container_id"),
        event_payload.get("containerId"),
        event_payload.get("id") if event.event_type.startswith("container.") else None,
    )
    container_name = _first_string(
        payload.get("container_name"),
        event_payload.get("container_name"),
        event_payload.get("containerName"),
        event_payload.get("name") if event.event_type.startswith("container.") else None,
    )
    container_source_id = _first_string(container_id, container_name)
    model_provider = _first_string(event_payload.get("provider"), event_payload.get("model_provider"))
    model_name = _first_string(event_payload.get("model"), event_payload.get("model_name"))
    status = _status_from_event(event)
    error_message = _error_message(event, payload=payload, event_payload=event_payload, status=status)
    sequence = _number(payload.get("sequence"))
    if isinstance(sequence, float) and sequence.is_integer():
        sequence = int(sequence)
    previous_sequence = int(sequence) - 1 if isinstance(sequence, int) and sequence > 1 else None
    next_sequence = int(sequence) + 1 if isinstance(sequence, int) else None
    boundary = _boundary_params(event, payload)
    return {
        "event_id": event_id,
        "event_type": event.event_type,
        "source_event_id": event.source_event_id,
        "execution_id": execution_id,
        "workflow_id": payload.get("workflow_id"),
        "workflow_version_id": payload.get("workflow_version_id"),
        **boundary,
        "agent_id": payload.get("agent_id"),
        "task_id": payload.get("task_id"),
        "sequence": sequence,
        "previous_sequence": previous_sequence,
        "next_sequence": next_sequence,
        "trace_id": payload.get("trace_id"),
        "span_id": payload.get("span_id"),
        "status": status,
        "parent_event_id": payload.get("parent_event_id"),
        "payload_keys": sorted(str(key) for key in event_payload.keys()),
        "metric_keys": sorted(str(key) for key in metrics.keys()),
        "runtime_adapter_id": _first_string(payload.get("runtime_adapter_id"), event_payload.get("runtime_adapter_id")),
        "runtime_revision_id": _first_string(
            payload.get("runtime_revision_id"),
            event_payload.get("runtime_revision_id"),
            event_payload.get("runtimeRevisionId"),
        ),
        "runtime_fingerprint": _first_string(payload.get("runtime_fingerprint"),
                                             event_payload.get("runtime_fingerprint")),
        "trigger_type": payload.get("trigger_type"),
        "trigger_payload": trigger_payload,
        "schedule_id": _first_string(trigger_payload.get("schedule_id"), trigger_payload.get("scheduleId")),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "tool_call_id": tool_call_id,
        "tool_name": _first_string(
            event_payload.get("tool_name"),
            event_payload.get("toolName"),
            event_payload.get("tool"),
            event_payload.get("name"),
        ),
        "model_request_id": model_request_id,
        "model_provider": model_provider,
        "model_name": model_name,
        "model_provider_id": model_provider,
        "model_id": _model_id(model_provider, model_name),
        "artifact_id": artifact_id,
        "artifact_name": artifact_name,
        "artifact_path": _first_string(event_payload.get("path")),
        "artifact_uri": _first_string(event_payload.get("uri")),
        "container_source_id": container_source_id,
        "container_id": container_id,
        "container_name": container_name,
        "container_image": _first_string(payload.get("container_image"), event_payload.get("image")),
        "container_status": _first_string(payload.get("container_status"), event_payload.get("status")),
        "container_started_at": payload.get("container_started_at") or event_payload.get("started_at"),
        "container_ended_at": payload.get("container_ended_at") or event_payload.get(
            "finished_at") or event_payload.get("ended_at"),
        "container_exit_code": payload.get("container_exit_code") or event_payload.get("exit_code"),
        "error_id": f"error:{event_id}",
        "error_message": error_message,
        "context_health_id": f"context_health:{event_id}" if _is_context_health_event(event.event_type,
                                                                                      context_health_payload) else None,
        "context_health_status": _first_string(context_health_payload.get("status"), metrics.get("context_status")),
        "context_estimated_prompt_tokens": _number(
            context_health_payload.get("estimated_prompt_tokens"),
            metrics.get("estimated_prompt_tokens"),
        ),
        "context_reserved_completion_tokens": _number(
            context_health_payload.get("reserved_completion_tokens"),
            metrics.get("reserved_completion_tokens"),
        ),
        "context_estimated_total_context_tokens": _number(
            context_health_payload.get("estimated_total_context_tokens"),
            metrics.get("estimated_total_context_tokens"),
        ),
        "context_window": _number(context_health_payload.get("context_window"), metrics.get("context_window")),
        "context_remaining_context_tokens": _number(context_health_payload.get("remaining_context_tokens")),
        "context_usage_ratio": _number(context_health_payload.get("usage_ratio"), metrics.get("context_usage_ratio")),
        "context_after_compaction": bool(event_payload.get("after_compaction")),
        "context_compaction_reason": _first_string(event_payload.get("compaction_reason"), event_payload.get("reason"),
                                                   record_payload.get("reason")),
        "token_usage_id": f"token_usage:{event_id}" if event.event_type == "token.usage.recorded" else None,
        "token_usage_provider": _first_string(usage_payload.get("provider"), metrics.get("model_provider")),
        "token_usage_model": _first_string(usage_payload.get("model"), metrics.get("model_name")),
        "token_usage_prompt_tokens": _number(usage_payload.get("prompt_tokens"), metrics.get("prompt_tokens"),
                                             metrics.get("input_tokens")),
        "token_usage_completion_tokens": _number(
            usage_payload.get("completion_tokens"),
            metrics.get("completion_tokens"),
            metrics.get("output_tokens"),
        ),
        "token_usage_total_tokens": _number(usage_payload.get("total_tokens"), metrics.get("total_tokens")),
        "token_usage_cached_tokens": _number(usage_payload.get("cached_tokens")),
        "token_usage_reasoning_tokens": _number(usage_payload.get("reasoning_tokens")),
        "token_usage_estimated_cost": _number(usage_payload.get("estimated_cost"), metrics.get("estimated_cost")),
        "token_usage_currency": _first_string(usage_payload.get("currency")),
        "token_usage_estimated": bool(usage_payload.get("estimated") or metrics.get("token_usage_estimated")),
        "token_budget_id": f"token_budget:{event_id}" if event.event_type.startswith("token.budget.") else None,
        "token_budget_scope": _first_string(budget_payload.get("scope")),
        "token_budget_status": _first_string(budget_payload.get("status"), event.event_type.rsplit(".", 1)[-1]),
        "token_budget_action": _first_string(budget_payload.get("action")),
        "token_budget_used_tokens": _number(budget_payload.get("used_tokens"), metrics.get("used_tokens")),
        "token_budget_budget_tokens": _number(budget_payload.get("budget_tokens"), metrics.get("budget_tokens")),
        "token_budget_usage_ratio": _number(budget_payload.get("usage_ratio"), metrics.get("usage_ratio")),
        "context_compaction_id": (
            f"context_compaction:{event_id}" if event.event_type.startswith("context.compaction.") else None
        ),
        "context_compaction_status": event.event_type.rsplit(".", 1)[-1] if event.event_type.startswith(
            "context.compaction.") else None,
        "context_compacted": bool(record_payload.get("compacted")),
        "context_compaction_memory_id": _first_string(record_payload.get("memory_id")),
        "context_compaction_source_model_request_id": _first_string(
            record_payload.get("source_model_request_id"),
            event_payload.get("model_request_id"),
        ),
        "context_estimated_tokens_saved": _number(record_payload.get("estimated_tokens_saved"),
                                                  metrics.get("estimated_tokens_saved")),
        "context_status_before": _first_string(context_health_before.get("status"),
                                               metrics.get("context_status_before")),
        "context_status_after": _first_string(
            context_health_after.get("status"),
            metrics.get("context_status_after"),
            record_payload.get("metadata", {}).get("context_status_after") if isinstance(record_payload.get("metadata"),
                                                                                         dict) else None,
        ),
        "context_usage_ratio_before": _number(context_health_before.get("usage_ratio"),
                                              metrics.get("context_usage_ratio_before")),
        "context_usage_ratio_after": _number(
            context_health_after.get("usage_ratio"),
            metrics.get("context_usage_ratio_after"),
            record_payload.get("metadata", {}).get("context_usage_ratio_after") if isinstance(
                record_payload.get("metadata"), dict) else None,
        ),
        "monitor_finding_id": f"monitor_finding:{event_id}" if event.event_type == "monitor.finding.created" else None,
        "monitor_finding_type": _first_string(
            event_payload.get("finding_type"),
            event_payload.get("type"),
            event_payload.get("category"),
        ),
        "monitor_finding_severity": _first_string(event_payload.get("severity")),
        "monitor_finding_status": _first_string(event_payload.get("status"), status),
        "monitor_finding_summary": _first_string(event_payload.get("summary"), event_payload.get("message"),
                                                 event_payload.get("title")),
        "occurred_at": event.occurred_at.isoformat(),
    }


def _persona_projection_params(event: GraphProjectionEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    persona_id = _first_string(payload.get("persona_id"), event.aggregate_id)
    return {
        "persona_id": persona_id,
        "persona_slug": _first_string(payload.get("persona_slug")),
        "persona_name": _first_string(payload.get("persona_name"), payload.get("persona_slug"), persona_id),
        "persona_status": _first_string(payload.get("persona_status"), _status_from_event(event)),
        "workspace_id": _first_string(payload.get("workspace_id")),
        "event_type": event.event_type,
        "run_id": _first_string(payload.get("run_id")),
        "run_status": _first_string(payload.get("run_status"), _status_from_event(event)),
        "item_count": _number(payload.get("item_count")),
        "active_item_count": _number(payload.get("active_item_count")),
        "needs_review_count": _number(payload.get("needs_review_count")),
        "persona_version_id": _first_string(payload.get("persona_version_id")),
        "version": _first_string(payload.get("version")),
        "version_status": _first_string(payload.get("version_status"), _status_from_event(event)),
        "item_id": _first_string(payload.get("item_id")),
        "source_memory_id": _first_string(payload.get("source_memory_id")),
        "item_type": _first_string(payload.get("item_type")),
        "memory_layer": _first_string(payload.get("memory_layer")),
        "title": _first_string(payload.get("title")),
        "review_status": _first_string(payload.get("review_status"), _status_from_event(event)),
        "needs_review": bool(payload.get("needs_review")) if payload.get("needs_review") is not None else None,
        "source_memory_ids": _string_list(payload.get("source_memory_ids")),
        "memory_ids": _string_list(payload.get("memory_ids")),
        "tools": _dict_projection_items(payload.get("tools")),
        "workflows": _dict_projection_items(payload.get("workflows")),
        "artifacts": _dict_projection_items(payload.get("artifacts")),
        "agent_id": _first_string(payload.get("agent_id")),
        "conversation_id": _first_string(payload.get("conversation_id")),
        "message_id": _first_string(payload.get("message_id")),
        "occurred_at": event.occurred_at.isoformat(),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _dict_projection_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_id = _first_string(item.get("id"), item.get("tool_id"), item.get("workflow_id"), item.get("artifact_id"),
                                item.get("name"))
        if not item_id:
            continue
        items.append(
            {
                "id": item_id,
                "name": _first_string(item.get("name"), item.get("title"), item_id),
                "granted": item.get("granted"),
                "confidence": _number(item.get("confidence")),
                "artifact_type": _first_string(item.get("artifact_type")),
                "distillation_item_id": _first_string(item.get("distillation_item_id")),
            }
        )
    return items


def _source_graph_entities(payload: dict[str, Any], event: GraphProjectionEvent) -> list[dict[str, Any]]:
    graph_hints = payload.get("graph_hints") if isinstance(payload.get("graph_hints"), dict) else {}
    raw_entities = payload.get("entities")
    if raw_entities is None:
        raw_entities = graph_hints.get("entities")
    entities_by_name: dict[str, dict[str, Any]] = {}
    for item in _dict_projection_items_for_graph(raw_entities):
        name = _first_string(item.get("name"))
        if not name:
            continue
        label = _source_graph_label(item.get("label"))
        node = _source_graph_node(label=label, name=name, event=event, source=item)
        entities_by_name[node["normalized_name"]] = node
    for relationship in _dict_projection_items_for_graph(
            payload.get("relationships") or graph_hints.get("relationships")):
        for key in ("source_name", "target_name"):
            name = _first_string(relationship.get(key))
            if not name:
                continue
            normalized_name = _normalized_source_graph_name(name)
            if normalized_name not in entities_by_name:
                entities_by_name[normalized_name] = _source_graph_node(
                    label="Entity",
                    name=name,
                    event=event,
                    source={"confidence": relationship.get("confidence"), "evidence": relationship.get("evidence")},
                )
    return list(entities_by_name.values())


def _source_graph_relationship_groups(payload: dict[str, Any], event: GraphProjectionEvent) -> dict[
    str, list[dict[str, Any]]]:
    groups = {
        "knows_relationships": [],
        "uses_relationships": [],
        "follows_relationships": [],
        "produces_relationships": [],
        "reviews_relationships": [],
        "approves_relationships": [],
        "escalates_to_relationships": [],
        "participates_in_relationships": [],
        "derived_from_relationships": [],
        "relates_to_relationships": [],
    }
    graph_hints = payload.get("graph_hints") if isinstance(payload.get("graph_hints"), dict) else {}
    entities = _source_graph_entities(payload, event)
    labels_by_name = {item["normalized_name"]: item["label"] for item in entities}
    raw_relationships = payload.get("relationships")
    if raw_relationships is None:
        raw_relationships = graph_hints.get("relationships")
    for item in _dict_projection_items_for_graph(raw_relationships):
        relationship_type = _source_graph_relationship_type(item.get("relationship_type"))
        source_name = _first_string(item.get("source_name"))
        target_name = _first_string(item.get("target_name"))
        if not relationship_type or not source_name or not target_name:
            continue
        source_label = labels_by_name.get(_normalized_source_graph_name(source_name), "Entity")
        target_label = labels_by_name.get(_normalized_source_graph_name(target_name), "Entity")
        group_key = f"{relationship_type.lower()}_relationships"
        groups[group_key].append(
            {
                "source_id": _source_graph_node_id(source_label, source_name),
                "target_id": _source_graph_node_id(target_label, target_name),
                "relationship_type": relationship_type,
                "confidence": _number(item.get("confidence")) or 0.7,
                "evidence": _first_string(item.get("evidence")),
            }
        )
    return groups


def _dict_projection_items_for_graph(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _source_graph_node(
        *,
        label: str,
        name: str,
        event: GraphProjectionEvent,
        source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": _source_graph_node_id(label, name),
        "label": label,
        "name": name,
        "normalized_name": _normalized_source_graph_name(name),
        "confidence": _number(source.get("confidence")) or 0.7,
        "evidence": _first_string(source.get("evidence")),
        "event_id": event.event_id,
    }


def _source_graph_node_id(label: str, name: str) -> str:
    normalized_label = _source_graph_label(label).lower()
    normalized_name = _normalized_source_graph_name(name)
    digest = hashlib.sha1(f"{normalized_label}:{normalized_name}".encode("utf-8")).hexdigest()[:16]
    return f"source-intelligence:{normalized_label}:{digest}"


def _source_graph_label(value: Any) -> str:
    label = _first_string(value)
    if label in SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS:
        return label
    return "Entity"


def _source_graph_relationship_type(value: Any) -> str | None:
    relationship_type = _first_string(value)
    if relationship_type in SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES:
        return relationship_type
    return None


def _normalized_source_graph_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _number(*values: Any) -> int | float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                number = float(value)
            except ValueError:
                continue
            return int(number) if number.is_integer() else number
    return None


def _is_context_health_event(event_type: str, context_health_payload: dict[str, Any]) -> bool:
    return event_type == "context.health.recorded" or bool(context_health_payload.get("status"))


def _error_message(
        event: GraphProjectionEvent,
        *,
        payload: dict[str, Any],
        event_payload: dict[str, Any],
        status: str,
) -> str | None:
    explicit = _first_string(
        payload.get("execution_error"),
        payload.get("error"),
        event_payload.get("error"),
        event_payload.get("message"),
        event_payload.get("reason"),
    )
    if explicit:
        return explicit
    if status == "failed" or "failed" in event.event_type or "error" in event.event_type:
        return event.event_type
    return None


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    clean = value.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in clean.split("/") if part]
    return parts[-1] if parts else clean


def _model_id(provider: str | None, model: str | None) -> str | None:
    if not model:
        return None
    return f"{provider}:{model}" if provider else model

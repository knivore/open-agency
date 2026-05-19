from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pydantic import BaseModel
from typing import Any, Literal
from uuid import uuid4

from app.api.context import ApiContext
from app.domain import (
    AgentDefinition,
    ModelProfileDefinition,
    NodeType,
    SecuritySettings,
    TaskDefinition,
    ToolDefinition,
    ToolImplementationReference,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.llm.base import ModelMessage


class WorkflowBuilderTaskDraft(BaseModel):
    name: str
    description: str
    expected_output: str


class WorkflowBuilderAgentDraft(BaseModel):
    name: str
    role: str
    instructions: str
    backstory: str


class WorkflowBuilderWorkflowDraft(BaseModel):
    name: str
    description: str


class WorkflowBuilderTasksResponse(BaseModel):
    assistant_message: str
    tasks: list[WorkflowBuilderTaskDraft]


class WorkflowBuilderAgentsResponse(BaseModel):
    agents: list[WorkflowBuilderAgentDraft]


class WorkflowBuilderWorkflowResponse(BaseModel):
    workflow: WorkflowBuilderWorkflowDraft


class WorkflowBuilderRepairResponse(BaseModel):
    workflow: WorkflowDefinition


@dataclass(slots=True)
class WorkflowBuilderService:
    context: ApiContext
    _COMMAND_TOOL_ID = "agency.command.run"
    _COMMAND_TOOL_NAME = "run_command"

    async def build_workflow_definition(
            self,
            *,
            goal: str,
            conversation_history: str | None = None,
            latest_tasks: str | None = None,
            model_profile_id: str | None = None,
            default_agent_model_profile_id: str | None = None,
            workflow_id: str | None = None,
    ) -> WorkflowDefinition:
        tasks_response = await self.generate_draft(
            "tasks",
            conversation_history=conversation_history,
            latest_instruction=goal,
            latest_tasks=latest_tasks,
            model_profile_id=model_profile_id,
        )
        task_drafts = tasks_response.get("tasks") or []
        agents_response = await self.generate_draft(
            "agents",
            tasks=task_drafts,
            model_profile_id=model_profile_id,
        )
        agent_drafts = agents_response.get("agents") or []
        workflow_response = await self.generate_draft(
            "workflow",
            tasks=task_drafts,
            agents=agent_drafts,
            model_profile_id=model_profile_id,
        )
        workflow_draft = workflow_response.get("workflow") or {}
        workflow_definition = self.assemble_workflow_definition(
            workflow=workflow_draft,
            tasks=task_drafts,
            agents=agent_drafts,
            default_agent_model_profile_id=default_agent_model_profile_id or model_profile_id,
            workflow_id=workflow_id,
        )
        return self._ensure_recommendation_to_code_pipeline(
            workflow=workflow_definition,
            goal=goal,
        )

    def assemble_workflow_definition(
            self,
            *,
            workflow: dict[str, Any],
            tasks: list[dict[str, Any]],
            agents: list[dict[str, Any]],
            default_agent_model_profile_id: str | None = None,
            workflow_id: str | None = None,
    ) -> WorkflowDefinition:
        if not tasks:
            raise ValueError("Workflow builder did not produce any tasks")
        if not agents:
            raise ValueError("Workflow builder did not produce any agents")

        resolved_workflow_id = workflow_id or f"workflow-{uuid4()}"
        agent_definitions: list[AgentDefinition] = []
        for index, agent in enumerate(agents, start=1):
            agent_definitions.append(
                AgentDefinition(
                    id=f"{resolved_workflow_id}-agent-{index}",
                    name=str(agent.get("name") or f"Agent {index}"),
                    role=str(agent.get("role") or "Workflow agent"),
                    instructions=str(agent.get("instructions") or "Execute assigned workflow tasks carefully."),
                    backstory=str(agent.get("backstory") or "Created by the workflow builder."),
                    model_profile_id=default_agent_model_profile_id,
                )
            )

        task_definitions: list[TaskDefinition] = []
        nodes: list[WorkflowNodeDefinition] = []
        primary_agent = agent_definitions[0]
        for index, task in enumerate(tasks, start=1):
            task_id = f"{resolved_workflow_id}-task-{index}"
            node_id = f"{resolved_workflow_id}-node-{index}"
            task_definitions.append(
                TaskDefinition(
                    id=task_id,
                    name=str(task.get("name") or f"Task {index}"),
                    description=str(task.get("description") or task.get("name") or f"Task {index}"),
                    instructions=str(task.get("description") or ""),
                    expected_output=str(task.get("expected_output") or "Completed task output."),
                    agent_id=primary_agent.id,
                    depends_on_task_ids=[] if index == 1 else [f"{resolved_workflow_id}-task-{index - 1}"],
                )
            )
            nodes.append(
                WorkflowNodeDefinition(
                    id=node_id,
                    name=str(task.get("name") or f"Task {index}"),
                    node_type=NodeType.TASK,
                    task_id=task_id,
                )
            )

        return WorkflowDefinition(
            id=resolved_workflow_id,
            name=str(workflow.get("name") or self._title_from_goal(resolved_workflow_id)),
            description=str(workflow.get("description") or "Generated workflow draft."),
            entrypoint=nodes[0].id,
            nodes=nodes,
            task_definitions=task_definitions,
            agent_definitions=agent_definitions,
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "generated_by": "workflow_builder",
            },
        )

    async def rewrite_agent(
            self,
            agent: dict[str, Any],
            *,
            model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._generate_structured(
            schema_name="workflow_builder_rewrite_agent",
            schema=WorkflowBuilderAgentDraft.model_json_schema(),
            system=(
                "You improve agent definitions for an agentic workflow builder. "
                "Keep the original intent, but make the role, instructions, and backstory "
                "clearer, more concrete, and more execution-ready."
            ),
            prompt=(
                "Rewrite the following agent fields.\n\n"
                f"Name: {agent.get('name', '')}\n"
                f"Role: {agent.get('role', '')}\n"
                f"Instructions: {agent.get('instructions', '')}\n"
                f"Backstory: {agent.get('backstory', '')}\n"
            ),
            model_profile_id=model_profile_id,
        )
        return WorkflowBuilderAgentDraft.model_validate(response).model_dump(mode="json")

    async def rewrite_task(
            self,
            task: dict[str, Any],
            *,
            model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._generate_structured(
            schema_name="workflow_builder_rewrite_task",
            schema=WorkflowBuilderTaskDraft.model_json_schema(),
            system=(
                "You improve workflow task definitions. Keep the original meaning, "
                "but make each task name, description, and expected output more "
                "specific, professional, and directly executable."
            ),
            prompt=(
                "Rewrite the following task fields.\n\n"
                f"Name: {task.get('name', '')}\n"
                f"Description: {task.get('description', '')}\n"
                f"Expected Output: {task.get('expected_output', '')}\n"
            ),
            model_profile_id=model_profile_id,
        )
        return WorkflowBuilderTaskDraft.model_validate(response).model_dump(mode="json")

    async def repair_workflow_definition(
            self,
            *,
            workflow: WorkflowDefinition,
            validation_errors: list[str],
            goal: str | None = None,
            model_profile_id: str | None = None,
    ) -> WorkflowDefinition:
        response = await self._generate_structured(
            schema_name="workflow_builder_repair_workflow",
            schema=WorkflowBuilderRepairResponse.model_json_schema(),
            system=(
                "You repair invalid workflow definitions for an agentic workflow builder. "
                "Return a complete workflow definition that preserves the user's intent, "
                "keeps existing IDs where possible, avoids unsafe new tool definitions, "
                "and fixes only the validation problems."
            ),
            prompt=(
                "Repair the workflow below so it passes validation.\n\n"
                f"User goal or edit request: {goal or ''}\n\n"
                f"Validation errors: {validation_errors}\n\n"
                "Workflow JSON:\n"
                f"{json.dumps(workflow.model_dump(mode='json'), indent=2, sort_keys=True)}\n"
            ),
            model_profile_id=model_profile_id,
        )
        return WorkflowBuilderRepairResponse.model_validate(response).workflow

    async def update_workflow_definition(
            self,
            *,
            workflow: WorkflowDefinition,
            goal: str,
            conversation_history: str | None = None,
            model_profile_id: str | None = None,
    ) -> WorkflowDefinition:
        response = await self._generate_structured(
            schema_name="workflow_builder_update_workflow",
            schema=WorkflowBuilderRepairResponse.model_json_schema(),
            system=(
                "You update existing workflow definitions for an agentic workflow builder. "
                "Return one complete updated WorkflowDefinition. Preserve the workflow id, "
                "existing IDs, visibility/mutability metadata, and unrelated fields unless "
                "the requested edit requires a change. Do not create unsafe executable tools."
            ),
            prompt=(
                "Apply the requested update to the workflow below.\n\n"
                f"Update request: {goal}\n\n"
                f"Conversation history: {conversation_history or ''}\n\n"
                "Current workflow JSON:\n"
                f"{json.dumps(workflow.model_dump(mode='json'), indent=2, sort_keys=True)}\n"
            ),
            model_profile_id=model_profile_id,
        )
        updated = WorkflowBuilderRepairResponse.model_validate(response).workflow
        if not updated.agent_definitions and workflow.agent_definitions:
            updated = updated.model_copy(update={"agent_definitions": workflow.agent_definitions})
        if not updated.task_definitions and workflow.task_definitions:
            updated = updated.model_copy(update={"task_definitions": workflow.task_definitions})
        if not updated.nodes and workflow.nodes:
            updated = updated.model_copy(update={"nodes": workflow.nodes})
        return self._ensure_recommendation_to_code_pipeline(
            workflow=updated,
            goal=goal,
        )

    async def generate_draft(
            self,
            draft_type: Literal["tasks", "agents", "workflow"],
            *,
            conversation_history: str | None = None,
            latest_instruction: str | None = None,
            latest_tasks: str | None = None,
            tasks: list[dict[str, Any]] | None = None,
            agents: list[dict[str, Any]] | None = None,
            model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        if draft_type == "tasks":
            response = await self._generate_structured(
                schema_name="workflow_builder_task_list",
                schema=WorkflowBuilderTasksResponse.model_json_schema(),
                system=(
                    "You are an agentic workflow planner. Break the user's goal into a "
                    "clear, ordered list of text-generation-friendly tasks. Avoid tasks "
                    "that require external research or human intervention. Return a short "
                    "assistant_message acknowledging the drafted workflow, then the tasks."
                ),
                prompt=(
                    "Create a workflow task list from the request below.\n\n"
                    f"Latest instruction: {latest_instruction or ''}\n\n"
                    f"Previous tasks: {latest_tasks or ''}\n\n"
                    f"Conversation history: {conversation_history or ''}\n"
                ),
                model_profile_id=model_profile_id,
            )
            return WorkflowBuilderTasksResponse.model_validate(response).model_dump(mode="json")

        if draft_type == "agents":
            task_names = ", ".join(str(task.get("name", "")) for task in tasks or [])
            response = await self._generate_structured(
                schema_name="workflow_builder_agent_list",
                schema=WorkflowBuilderAgentsResponse.model_json_schema(),
                system=(
                    "You design agent teams for workflows. Generate focused, non-overlapping "
                    "agents that can execute the provided tasks using only text-based work."
                ),
                prompt=(
                    "Generate agents for the following tasks.\n\n"
                    f"Task names: {task_names}\n\n"
                    f"Task details: {tasks or []}\n"
                ),
                model_profile_id=model_profile_id,
            )
            return WorkflowBuilderAgentsResponse.model_validate(response).model_dump(mode="json")

        if draft_type == "workflow":
            response = await self._generate_structured(
                schema_name="workflow_builder_workflow_summary",
                schema=WorkflowBuilderWorkflowResponse.model_json_schema(),
                system=(
                    "You summarize a workflow into a single team or workflow identity. "
                    "Return one concise workflow name and one clear description."
                ),
                prompt=(
                    "Create one workflow identity from the following tasks and agents.\n\n"
                    f"Tasks: {tasks or []}\n\n"
                    f"Agents: {agents or []}\n"
                ),
                model_profile_id=model_profile_id,
            )
            return WorkflowBuilderWorkflowResponse.model_validate(response).model_dump(mode="json")

        raise ValueError(f"Unsupported draft_type '{draft_type}'")

    async def _generate_structured(
            self,
            *,
            schema_name: str,
            schema: dict[str, Any],
            system: str,
            prompt: str,
            model_profile_id: str | None,
    ) -> dict[str, Any]:
        profile = await self._resolve_model_profile(model_profile_id=model_profile_id)
        client = self.context.llm_provider_registry.resolve(profile)
        messages = [
            ModelMessage(role="system", content=system),
            ModelMessage(role="user", content=prompt),
        ]
        if hasattr(client, "agenerate_structured"):
            response = await client.agenerate_structured(
                messages,
                schema=schema,
                schema_name=schema_name,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
            )
        else:
            response = await asyncio.to_thread(
                client.generate_structured,
                messages,
                schema=schema,
                schema_name=schema_name,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
            )
        if not isinstance(response.content, dict):
            raise ValueError(f"Structured builder response '{schema_name}' was not an object")
        return response.content

    async def _resolve_model_profile(self, *, model_profile_id: str | None) -> ModelProfileDefinition:
        if model_profile_id:
            profile = await self.context.model_profile_repo.get_profile(model_profile_id)
            if profile is None:
                raise ValueError(f"Model profile '{model_profile_id}' was not found")
            return profile

        profiles = await self.context.model_profile_repo.list()
        structured_profile = next((item for item in profiles if item.supports_structured_output), None)
        if structured_profile is not None:
            return structured_profile
        if profiles:
            return profiles[0]
        raise ValueError("No model profiles are configured for workflow builder operations")

    def _title_from_goal(self, goal: str) -> str:
        words = re.sub(r"[^a-zA-Z0-9]+", " ", goal).strip().split()
        title = " ".join(words[:5]) or "Generated Workflow"
        return f"{title.title()} Workflow" if not title.lower().endswith("workflow") else title.title()

    def _ensure_recommendation_to_code_pipeline(
            self,
            *,
            workflow: WorkflowDefinition,
            goal: str,
    ) -> WorkflowDefinition:
        if not self._goal_requests_recommendation_to_code(goal=goal, workflow=workflow):
            return workflow
        if not workflow.task_definitions:
            return workflow
        if not workflow.agent_definitions:
            return workflow

        tasks = list(workflow.task_definitions)
        nodes = list(workflow.nodes)
        edges = list(workflow.edges)
        agents = list(workflow.agent_definitions)
        tools = list(workflow.tool_definitions)
        qa_collaboration_requested = self._goal_requests_coder_qa_collaboration(goal=goal, workflow=workflow)

        command_tool = next(
            (
                tool
                for tool in tools
                if tool.id == self._COMMAND_TOOL_ID
                or tool.implementation.implementation_type == "shell_command"
            ),
            None,
        )
        if command_tool is None:
            command_tool = self._build_command_tool_definition()
            tools.append(command_tool)

        implementer = self._find_or_create_implementer_agent(workflow=workflow, agents=agents)
        implementer, agents = self._ensure_agent_has_tool(
            agents=agents,
            agent=implementer,
            required_tool_id=command_tool.id,
        )

        qa_reviewer: AgentDefinition | None = None
        if qa_collaboration_requested:
            qa_reviewer = self._find_or_create_qa_agent(workflow=workflow, agents=agents)
            qa_reviewer, agents = self._ensure_agent_has_tool(
                agents=agents,
                agent=qa_reviewer,
                required_tool_id=command_tool.id,
            )
            agents = self._ensure_bidirectional_handoff(
                agents=agents,
                left_agent_id=implementer.id,
                right_agent_id=qa_reviewer.id,
            )

        implementation_keywords = ("implement", "code", "patch", "fix", "apply", "todo", "to-do")
        verification_keywords = ("verify", "validate", "test", "lint")
        verification_agent_id = qa_reviewer.id if qa_reviewer is not None else implementer.id
        tasks = self._assign_tasks_to_agent(
            tasks=tasks,
            keywords=implementation_keywords,
            agent_id=implementer.id,
            required_tool_id=command_tool.id,
        )
        tasks = self._assign_tasks_to_agent(
            tasks=tasks,
            keywords=verification_keywords,
            agent_id=verification_agent_id,
            required_tool_id=command_tool.id,
        )

        task_ids = {task.id for task in tasks}
        node_ids = {node.id for node in nodes}
        last_node_id = self._last_node_id_for_tasks(workflow=workflow, task_ids=task_ids) or workflow.entrypoint
        only_add_fourth_task = self._goal_requests_fourth_task_from_third_output(goal)

        if not self._has_task_with_keywords(tasks, implementation_keywords):
            repos = self._repos_from_goal(goal)
            implement_task = TaskDefinition(
                id=self._unique_id(f"{workflow.id}-task-implement-improvements", task_ids),
                name="Implement selected TODO improvements",
                description="Apply selected recommendation TODOs directly in repository code.",
                instructions=(
                    f"Convert selected recommendations into concrete TODO items, then implement those TODOs in {repos}. "
                    "Make the smallest reliable patch that resolves the issue and keeps behavior stable."
                ),
                expected_output=(
                    "Completed TODO items, applied code changes, affected files, rationale, and exact commands used."
                ),
                agent_id=implementer.id,
                tool_ids=[command_tool.id],
                depends_on_task_ids=[tasks[-1].id],
            )
            tasks.append(implement_task)
            task_ids.add(implement_task.id)
            implement_node_id = self._unique_id(f"{implement_task.id}-node", node_ids)
            nodes.append(
                WorkflowNodeDefinition(
                    id=implement_node_id,
                    name=implement_task.name,
                    node_type=NodeType.TASK,
                    task_id=implement_task.id,
                )
            )
            node_ids.add(implement_node_id)
            if last_node_id and last_node_id in node_ids:
                edges.append(
                    WorkflowEdgeDefinition(
                        source_node_id=last_node_id,
                        target_node_id=implement_node_id,
                    )
                )
            last_node_id = implement_node_id

        if not only_add_fourth_task and not self._has_task_with_keywords(tasks, verification_keywords):
            verification_dependency = tasks[-1].id
            verify_task = TaskDefinition(
                id=self._unique_id(f"{workflow.id}-task-verify-improvements", task_ids),
                name="QA verify implemented changes" if qa_collaboration_requested else "Verify implemented changes",
                description=(
                    "Run focused QA checks to confirm the patch and catch regressions."
                    if qa_collaboration_requested
                    else "Run focused checks to confirm the patch and catch regressions."
                ),
                instructions=(
                    "Run targeted test/lint/build commands relevant to changed files. Report failures explicitly "
                    "with command outputs and likely fix direction."
                ),
                expected_output="Verification summary with pass/fail outcomes and command evidence.",
                agent_id=verification_agent_id,
                tool_ids=[command_tool.id],
                depends_on_task_ids=[verification_dependency],
            )
            tasks.append(verify_task)
            task_ids.add(verify_task.id)
            verify_node_id = self._unique_id(f"{verify_task.id}-node", node_ids)
            nodes.append(
                WorkflowNodeDefinition(
                    id=verify_node_id,
                    name=verify_task.name,
                    node_type=NodeType.TASK,
                    task_id=verify_task.id,
                )
            )
            node_ids.add(verify_node_id)
            if last_node_id and last_node_id in node_ids:
                edges.append(
                    WorkflowEdgeDefinition(
                        source_node_id=last_node_id,
                        target_node_id=verify_node_id,
                    )
                )
            last_node_id = verify_node_id

        if qa_collaboration_requested and not only_add_fourth_task:
            latest_verification_task_id = self._latest_task_id_with_keywords(tasks, verification_keywords)
            if latest_verification_task_id and not self._has_qa_fix_task(tasks):
                fix_task = TaskDefinition(
                    id=self._unique_id(f"{workflow.id}-task-fix-qa-findings", task_ids),
                    name="Fix QA findings",
                    description="Coder fixes any errors or regressions reported by QA verification.",
                    instructions=(
                        "Review QA verification output. If QA reported failures, fix root causes and rerun focused "
                        "checks locally. If QA reported no failures, return a no-op note with rationale."
                    ),
                    expected_output=(
                        "Patch set resolving QA findings (or explicit no-op rationale), changed files, and command evidence."
                    ),
                    agent_id=implementer.id,
                    tool_ids=[command_tool.id],
                    depends_on_task_ids=[latest_verification_task_id],
                )
                tasks.append(fix_task)
                task_ids.add(fix_task.id)
                fix_node_id = self._unique_id(f"{fix_task.id}-node", node_ids)
                nodes.append(
                    WorkflowNodeDefinition(
                        id=fix_node_id,
                        name=fix_task.name,
                        node_type=NodeType.TASK,
                        task_id=fix_task.id,
                    )
                )
                node_ids.add(fix_node_id)
                if last_node_id and last_node_id in node_ids:
                    edges.append(
                        WorkflowEdgeDefinition(
                            source_node_id=last_node_id,
                            target_node_id=fix_node_id,
                        )
                    )
                last_node_id = fix_node_id

            if self._has_qa_fix_task(tasks) and not self._has_qa_recheck_task(tasks):
                latest_fix_task_id = self._latest_qa_fix_task_id(tasks)
                if latest_fix_task_id:
                    qa_recheck_task = TaskDefinition(
                        id=self._unique_id(f"{workflow.id}-task-qa-recheck-fixes", task_ids),
                        name="QA recheck coder fixes",
                        description="QA re-runs focused verification after coder addresses findings.",
                        instructions=(
                            "Re-run focused tests/lint/build for changed scope. Confirm whether QA failures are "
                            "resolved and highlight any remaining blockers."
                        ),
                        expected_output="Final QA verdict with pass/fail status and command evidence.",
                        agent_id=verification_agent_id,
                        tool_ids=[command_tool.id],
                        depends_on_task_ids=[latest_fix_task_id],
                    )
                    tasks.append(qa_recheck_task)
                    task_ids.add(qa_recheck_task.id)
                    recheck_node_id = self._unique_id(f"{qa_recheck_task.id}-node", node_ids)
                    nodes.append(
                        WorkflowNodeDefinition(
                            id=recheck_node_id,
                            name=qa_recheck_task.name,
                            node_type=NodeType.TASK,
                            task_id=qa_recheck_task.id,
                        )
                    )
                    node_ids.add(recheck_node_id)
                    if last_node_id and last_node_id in node_ids:
                        edges.append(
                            WorkflowEdgeDefinition(
                                source_node_id=last_node_id,
                                target_node_id=recheck_node_id,
                            )
                        )

        metadata = dict(workflow.metadata)
        metadata["workflow_builder_enhancement"] = "recommendation_to_code_pipeline"
        if qa_collaboration_requested:
            metadata["workflow_builder_collaboration"] = "coder_qa"
        return workflow.model_copy(
            update={
                "task_definitions": tasks,
                "agent_definitions": agents,
                "tool_definitions": tools,
                "nodes": nodes,
                "edges": edges,
                "metadata": metadata,
            }
        )

    def _goal_requests_recommendation_to_code(
            self,
            *,
            goal: str,
            workflow: WorkflowDefinition | None = None,
    ) -> bool:
        lowered = goal.lower()
        workflow_text = ""
        if workflow is not None:
            parts: list[str] = [workflow.name or "", workflow.description or ""]
            for task in workflow.task_definitions:
                parts.extend([task.name or "", task.description or "", task.instructions or ""])
            for agent in workflow.agent_definitions:
                parts.extend([agent.name or "", agent.role or "", agent.instructions or "", agent.description or ""])
            workflow_text = " ".join(parts).lower()

        haystacks = [lowered, workflow_text]
        recommendation_signals = ("recommend", "idea", "improvement", "suggestion")
        coding_signals = (
            "implement",
            "apply",
            "coding",
            "code",
            "patch",
            "fix",
            "todo",
            "to-do",
            "action item",
            "direct",
            "direct8000",
            "llmfirst",
        )
        repo_signals = ("repo", "repository", "agency", "agency-fe")
        has_recommendation = any(any(signal in text for signal in recommendation_signals) for text in haystacks)
        has_coding = any(any(signal in text for signal in coding_signals) for text in haystacks)
        has_repo = any(any(signal in text for signal in repo_signals) for text in haystacks)
        if has_recommendation and has_coding and has_repo:
            return True

        # Support direct phrasing like:
        # "i want to have the coder agent to work on the workflow to perform the todo"
        direct_coder_todo_request = (
            "coder" in lowered
            and "workflow" in lowered
            and any(token in lowered for token in ("todo", "to-do", "action item"))
        )
        if direct_coder_todo_request:
            return True
        return False

    def _goal_requests_coder_qa_collaboration(
            self,
            *,
            goal: str,
            workflow: WorkflowDefinition | None = None,
    ) -> bool:
        lowered = goal.lower()
        workflow_text = ""
        if workflow is not None:
            parts: list[str] = [workflow.name or "", workflow.description or ""]
            for task in workflow.task_definitions:
                parts.extend([task.name or "", task.description or "", task.instructions or "", task.expected_output or ""])
            for agent in workflow.agent_definitions:
                parts.extend([agent.name or "", agent.role or "", agent.instructions or "", agent.description or ""])
            workflow_text = " ".join(parts).lower()
        haystacks = [lowered, workflow_text]
        has_qa_signal = any(
            any(token in text for token in ("qa", "quality assurance", "verify", "validation", "test"))
            for text in haystacks
        )
        has_coder_signal = any(any(token in text for token in ("coder", "coding agent", "code")) for text in haystacks)
        has_fix_signal = any(any(token in text for token in ("fix", "error", "bug", "failure")) for text in haystacks)
        has_collaboration_signal = any(
            any(token in text for token in ("work together", "together", "handoff", "collaborat"))
            for text in haystacks
        )
        if has_qa_signal and has_coder_signal and (has_fix_signal or has_collaboration_signal):
            return True
        return False

    def _goal_requests_fourth_task_from_third_output(self, goal: str) -> bool:
        lowered = goal.lower()
        third_output_signals = (
            "output of the 3rd task",
            "output of the third task",
            "from the 3rd task",
            "from the third task",
        )
        asks_for_fourth_task = any(token in lowered for token in ("4th task", "fourth task"))
        asks_for_coding = any(token in lowered for token in ("coding", "code", "implement", "todo", "to-do"))
        return asks_for_fourth_task and asks_for_coding and any(signal in lowered for signal in third_output_signals)

    def _build_command_tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=self._COMMAND_TOOL_ID,
            name=self._COMMAND_TOOL_NAME,
            display_name="Run Command",
            description=(
                "Run one approved shell command step. Use for local repository edits and verification commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "mode": {
                        "type": ["string", "null"],
                        "enum": ["auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd", None],
                    },
                    "cwd": {"type": ["string", "null"]},
                    "timeout_seconds": {"type": ["integer", "null"], "minimum": 1, "maximum": 7200},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                    "output_text": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "overflow_path": {"type": ["string", "null"]},
                },
                "required": [
                    "status",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "duration_ms",
                    "output_text",
                    "truncated",
                ],
                "additionalProperties": True,
            },
            implementation=ToolImplementationReference(
                implementation_type="shell_command",
                target="agency.system.command",
                callable_name=self._COMMAND_TOOL_NAME,
                config={"timeout": 30, "max_timeout": 7200},
            ),
            security=SecuritySettings(
                requires_approval=True,
                sandbox_required=True,
                allow_shell=True,
                allow_filesystem=True,
                allow_network=False,
                read_only=False,
                dangerous=True,
            ),
            tags=["workflow_builder", "command", "repo_mutation"],
        )

    def _find_or_create_implementer_agent(
            self,
            *,
            workflow: WorkflowDefinition,
            agents: list[AgentDefinition],
    ) -> AgentDefinition:
        # Prefer dedicated coder agents before generic implementation-capable agents.
        for agent in agents:
            text = " ".join(
                [
                    agent.id or "",
                    agent.name or "",
                    agent.role or "",
                    agent.instructions or "",
                    agent.description or "",
                ]
            ).lower()
            if "coder" in text:
                return agent

        for agent in agents:
            text = " ".join(
                [
                    agent.id or "",
                    agent.name or "",
                    agent.role or "",
                    agent.instructions or "",
                    agent.description or "",
                ]
            ).lower()
            if any(token in text for token in ("implement", "coding", "code", "patch", "fix")):
                return agent

        base_agent = agents[0]
        implementer = AgentDefinition(
            id=f"{workflow.id}-agent-coder",
            name="Coder Agent",
            role="Repository coding specialist",
            description="Implements repository TODO items from approved recommendations and verifies outcomes.",
            instructions=(
                "Implement repository TODO items safely, keep diffs small and reviewable, and run focused validation "
                "commands before final output. Prioritize high-impact TODOs first."
            ),
            backstory="Experienced at turning recommendations and TODO items into tested, minimal patches.",
            model_profile_id=base_agent.model_profile_id,
            tool_ids=[],
        )
        agents.append(implementer)
        return implementer

    def _find_or_create_qa_agent(
            self,
            *,
            workflow: WorkflowDefinition,
            agents: list[AgentDefinition],
    ) -> AgentDefinition:
        for agent in agents:
            text = " ".join(
                [
                    agent.id or "",
                    agent.name or "",
                    agent.role or "",
                    agent.instructions or "",
                    agent.description or "",
                ]
            ).lower()
            if "qa" in text or any(token in text for token in ("quality assurance", "tester", "test engineer")):
                return agent

        base_agent = agents[0]
        qa_reviewer = AgentDefinition(
            id=f"{workflow.id}-agent-qa",
            name="QA Agent",
            role="Quality assurance and regression specialist",
            description="Verifies coder patches and reports concrete failures for remediation.",
            instructions=(
                "Run focused validation on coder changes, capture failing commands and actionable repro details, "
                "and provide a clear pass/fail verdict."
            ),
            backstory="Experienced at catching regressions early and guiding fast fixes with precise evidence.",
            model_profile_id=base_agent.model_profile_id,
            tool_ids=[],
        )
        agents.append(qa_reviewer)
        return qa_reviewer

    def _ensure_agent_has_tool(
            self,
            *,
            agents: list[AgentDefinition],
            agent: AgentDefinition,
            required_tool_id: str,
    ) -> tuple[AgentDefinition, list[AgentDefinition]]:
        if required_tool_id in agent.tool_ids:
            return agent, agents
        updated = agent.model_copy(update={"tool_ids": [*agent.tool_ids, required_tool_id]})
        refreshed_agents = [updated if item.id == agent.id else item for item in agents]
        return updated, refreshed_agents

    def _ensure_bidirectional_handoff(
            self,
            *,
            agents: list[AgentDefinition],
            left_agent_id: str,
            right_agent_id: str,
    ) -> list[AgentDefinition]:
        refreshed_agents: list[AgentDefinition] = []
        for agent in agents:
            handoff_ids = list(agent.handoff_agent_ids or [])
            if agent.id == left_agent_id and right_agent_id not in handoff_ids:
                handoff_ids.append(right_agent_id)
            if agent.id == right_agent_id and left_agent_id not in handoff_ids:
                handoff_ids.append(left_agent_id)
            if handoff_ids != list(agent.handoff_agent_ids or []):
                refreshed_agents.append(agent.model_copy(update={"handoff_agent_ids": handoff_ids}))
            else:
                refreshed_agents.append(agent)
        return refreshed_agents

    def _has_task_with_keywords(self, tasks: list[TaskDefinition], keywords: tuple[str, ...]) -> bool:
        for task in tasks:
            if self._is_non_execution_brief_task(task):
                continue
            haystack = " ".join(
                [
                    task.name or "",
                    task.description or "",
                    task.instructions or "",
                    task.expected_output or "",
                ]
            ).lower()
            if any(keyword in haystack for keyword in keywords):
                return True
        return False

    def _is_non_execution_brief_task(self, task: TaskDefinition) -> bool:
        name_text = (task.name or "").lower()
        primary_text = " ".join([task.name or "", task.description or ""]).lower()
        execution_name_verbs = ("implement", "apply", "patch", "fix", "code", "verify", "validate", "test", "lint")
        if any(token in name_text for token in execution_name_verbs):
            return False
        non_execution_signals = (
            "brief",
            "report",
            "summary",
            "recommendation",
            "review",
            "inspect",
            "identify",
            "choose",
            "prepare",
            "produce",
            "gather",
        )
        return any(token in primary_text for token in non_execution_signals)

    def _assign_tasks_to_agent(
            self,
            *,
            tasks: list[TaskDefinition],
            keywords: tuple[str, ...],
            agent_id: str,
            required_tool_id: str,
    ) -> list[TaskDefinition]:
        updated_tasks: list[TaskDefinition] = []
        for task in tasks:
            if self._is_non_execution_brief_task(task):
                updated_tasks.append(task)
                continue
            haystack = " ".join(
                [
                    task.name or "",
                    task.description or "",
                    task.instructions or "",
                    task.expected_output or "",
                ]
            ).lower()
            if any(keyword in haystack for keyword in keywords):
                tool_ids = list(task.tool_ids or [])
                if required_tool_id not in tool_ids:
                    tool_ids.append(required_tool_id)
                updated_tasks.append(task.model_copy(update={"agent_id": agent_id, "tool_ids": tool_ids}))
                continue
            updated_tasks.append(task)
        return updated_tasks

    def _latest_task_id_with_keywords(self, tasks: list[TaskDefinition], keywords: tuple[str, ...]) -> str | None:
        for task in reversed(tasks):
            haystack = " ".join(
                [
                    task.name or "",
                    task.description or "",
                    task.instructions or "",
                    task.expected_output or "",
                ]
            ).lower()
            if any(keyword in haystack for keyword in keywords):
                return task.id
        return None

    def _has_qa_fix_task(self, tasks: list[TaskDefinition]) -> bool:
        for task in tasks:
            name_text = (task.name or "").lower()
            if "qa" in name_text and any(token in name_text for token in ("fix", "resolve", "remediate", "address")):
                return True
        return False

    def _latest_qa_fix_task_id(self, tasks: list[TaskDefinition]) -> str | None:
        for task in reversed(tasks):
            name_text = (task.name or "").lower()
            if "qa" in name_text and any(token in name_text for token in ("fix", "resolve", "remediate", "address")):
                return task.id
        return None

    def _has_qa_recheck_task(self, tasks: list[TaskDefinition]) -> bool:
        for task in tasks:
            haystack = " ".join(
                [
                    task.name or "",
                    task.description or "",
                    task.instructions or "",
                    task.expected_output or "",
                ]
            ).lower()
            if ("qa" in haystack or "verify" in haystack) and any(
                token in haystack for token in ("recheck", "re-verify", "reverify", "after fix")
            ):
                return True
        return False

    def _last_node_id_for_tasks(self, *, workflow: WorkflowDefinition, task_ids: set[str]) -> str | None:
        for node in reversed(workflow.nodes):
            if node.task_id and node.task_id in task_ids:
                return node.id
        return None

    def _unique_id(self, base: str, existing_ids: set[str]) -> str:
        if base not in existing_ids:
            return base
        index = 2
        while f"{base}-{index}" in existing_ids:
            index += 1
        return f"{base}-{index}"

    def _repos_from_goal(self, goal: str) -> str:
        lowered = goal.lower()
        repos: list[str] = []
        if "agency-fe" in lowered or "agency fe" in lowered:
            repos.append("agency-fe")
        if re.search(r"\bagency\b", lowered):
            repos.append("agency")
        if not repos:
            return "the target repositories from the request"
        unique = list(dict.fromkeys(repos))
        if len(unique) == 1:
            return unique[0]
        return ", ".join(unique)

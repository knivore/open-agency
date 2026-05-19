from __future__ import annotations

import asyncio
import os
import traceback
from dotenv import load_dotenv
from enum import Enum
from typing import Any, Dict, List, Optional

from app.domain import AgentDefinition, ModelProfileDefinition, TaskDefinition, WorkflowDefinition, \
    WorkflowNodeDefinition
from app.llm.registry import ModelProviderRegistry
from .availability import ensure_crewai_available
from .events import print_agent_output_to_json
from .llm_bridge import AgencyModelClientLLM
from .tools import create_crewai_tool

load_dotenv()


class LLMmodel(Enum):
    GPT4oMini = "gpt-4o-mini"
    GPT4o = "gpt-4o"


def create_llm_model(
        llm_string: Optional[str] = "gpt-4o-mini",
        temperature: Optional[float] = None,
        *,
        profile: ModelProfileDefinition | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
):
    ensure_crewai_available()
    from crewai import LLM

    if profile is not None and model_provider_registry is not None:
        return AgencyModelClientLLM(
            profile=profile,
            model_client=model_provider_registry.resolve(profile),
            model_event_loop=model_event_loop,
        )

    model_name = llm_string or (profile.model if profile is not None else None) or LLMmodel.GPT4oMini.value
    provider = profile.provider.lower() if profile is not None else ""
    explicit_base_url = profile.base_url if profile is not None else None
    explicit_api_key = profile.api_key_ref if profile is not None else None
    azure_endpoint = os.getenv("AZURE_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_api_key = os.getenv("AZURE_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
    azure_api_version = os.getenv("AZURE_API_VERSION") or "2024-06-01"

    llm_kwargs: dict[str, Any] = {"model": model_name}
    if provider == "azure_openai" and "/" not in model_name:
        llm_kwargs.update(
            {
                "model": f"azure/{model_name}",
                "endpoint": explicit_base_url or azure_endpoint,
                "api_key": explicit_api_key or azure_api_key,
                "api_version": azure_api_version,
            }
        )
    else:
        if provider in {"ollama", "anthropic", "google", "aws_bedrock"}:
            llm_kwargs["provider"] = provider
        if explicit_base_url:
            llm_kwargs["base_url"] = explicit_base_url
        if explicit_api_key:
            llm_kwargs["api_key"] = explicit_api_key
        elif azure_endpoint and azure_api_key and "/" not in model_name:
            llm_kwargs.update(
                {
                    "model": f"azure/{model_name}",
                    "endpoint": azure_endpoint,
                    "api_key": azure_api_key,
                    "api_version": azure_api_version,
                }
            )

    llm = LLM(**llm_kwargs)
    llm.temperature = temperature or 0.1
    return llm


def select_llm_model(
        llm_string: Optional[str] = "gpt-4o-mini",
        temperature: Optional[float] = None,
        *,
        profile: ModelProfileDefinition | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
):
    selected_model = llm_string or (profile.model if profile is not None else None) or LLMmodel.GPT4oMini.value
    return create_llm_model(
        selected_model,
        temperature,
        profile=profile,
        model_provider_registry=model_provider_registry,
        model_event_loop=model_event_loop,
    )


def get_tools(agent_tools: Optional[List[Dict[str, Any]]], process_id, run_by) -> List[Any]:
    tool_configs = agent_tools or []
    return [convert_tool(tool, process_id, run_by) for tool in tool_configs]


def convert_tool(tool_config: Dict[str, Any], process_id, run_by) -> Any:
    tool_id = tool_config["id"]
    params = dict(tool_config.get("parameters") or {})
    params["process_id"] = process_id
    params["run_by"] = run_by
    return create_crewai_tool(tool_id, **params)


def resolve_task_tools(task_tools: Optional[List[Any]], process_id: str, run_by: str) -> List[Any]:
    if not task_tools:
        return []
    if all(isinstance(tool, dict) and "id" in tool for tool in task_tools):
        return get_tools(task_tools, process_id, run_by)
    return task_tools


def resolve_llm_config(llm_config: Any, *, profile: ModelProfileDefinition | None = None) -> Any:
    if isinstance(llm_config, str):
        return select_llm_model(llm_config, profile=profile)
    return llm_config


def agent_definition_to_crewai_config(agent: AgentDefinition, *, default_model: str = "gpt-4o-mini") -> Dict[str, Any]:
    config = agent.framework_hints.adapter_config
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "role": agent.role or agent.name,
        "instructions": agent.instructions or agent.description or "",
        "backstory": agent.backstory or "",
        "allow_delegation": bool(agent.handoff_agent_ids),
        "verbose": bool(config.get("verbose", False)),
        "cache": bool(config.get("cache", True)),
        "llm": str(config.get("llm", default_model)),
        "temperature": config.get("temperature", 0.1),
        "tool_ids": list(agent.tool_ids),
        "max_iter": int(config.get("max_iterations", config.get("max_iter", 13))),
    }


def task_definition_to_crewai_config(task: TaskDefinition, node: WorkflowNodeDefinition,
                                     nodes_by_id: Dict[str, WorkflowNodeDefinition]) -> Dict[str, Any]:
    context_task_ids: List[str] = []
    for dependency_id in task.depends_on_task_ids:
        dependency_node = nodes_by_id.get(dependency_id)
        if dependency_node and dependency_node.task_id:
            context_task_ids.append(dependency_node.task_id)
        else:
            context_task_ids.append(dependency_id)
    return {
        "task_id": task.id,
        "name": task.name,
        "description": task.description,
        "expected_output": task.expected_output or "Complete the assigned task.",
        "agent_id": node.agent_id or task.agent_id,
        "context": context_task_ids,
        "tool_ids": list(task.tool_ids),
        "human_input": task.human_approval_required,
    }


def workflow_to_crewai_config(workflow: WorkflowDefinition, *, default_model: str = "gpt-4o-mini") -> Dict[str, Any]:
    tasks_by_id = {task.id: task for task in workflow.task_definitions}
    nodes_by_id = {node.id: node for node in workflow.nodes}
    ordered_nodes = [node for node in workflow.nodes if node.node_type == "task" and node.task_id in tasks_by_id]
    return {
        "name": workflow.name,
        "description": workflow.description,
        "agents": [agent_definition_to_crewai_config(agent, default_model=default_model) for agent in
                   workflow.agent_definitions],
        "tasks": [task_definition_to_crewai_config(tasks_by_id[node.task_id], node, nodes_by_id) for node in
                  ordered_nodes],
        "inputs": list(workflow.metadata.get("inputs", [])),
    }


def get_agents(
        agents: List[AgentDefinition],
        process_id: str,
        run_by: str,
        *,
        default_model: str,
        model_profiles: dict[str, ModelProfileDefinition] | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
) -> list[Any]:
    return [
        convert_agent(
            agent,
            process_id,
            run_by,
            default_model=default_model,
            model_profile=(model_profiles or {}).get(agent.id),
            model_provider_registry=model_provider_registry,
            model_event_loop=model_event_loop,
        )
        for agent in agents
    ]


def convert_agent(
        agent: AgentDefinition,
        process_id: str,
        run_by: str,
        *,
        default_model: str,
        model_profile: ModelProfileDefinition | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
):
    ensure_crewai_available()
    from crewai import Agent

    if agent is None:
        raise ValueError("Agent cannot be None")
    if not agent.role or not agent.instructions:
        raise ValueError(f"Agent {agent.id} must have both role and instructions defined")
    agent_config = agent_definition_to_crewai_config(agent, default_model=default_model)
    explicit_llm_hint = agent.framework_hints.adapter_config.get("llm")
    try:
        converted_agent_tools = get_tools([{"id": tool_id, "parameters": {}} for tool_id in agent_config["tool_ids"]],
                                          process_id, run_by)
    except Exception as exc:
        raise RuntimeError(f"Failed to create tools for agent {agent.id}: {str(exc)}") from exc

    llm_hint = explicit_llm_hint if isinstance(explicit_llm_hint, str) and explicit_llm_hint.strip() else None
    llm_model = select_llm_model(
        llm_hint or None,
        agent_config["temperature"],
        profile=model_profile,
        model_provider_registry=model_provider_registry,
        model_event_loop=model_event_loop,
    )
    return Agent(
        role=agent_config["role"],
        goal=agent_config["instructions"],
        backstory=agent_config["backstory"],
        cache=agent_config["cache"],
        verbose=agent_config["verbose"],
        allow_delegation=agent_config["allow_delegation"],
        tools=converted_agent_tools,
        max_iter=agent_config["max_iter"],
        llm=llm_model,
        step_callback=lambda output: print_agent_output_to_json(process_id, output, agent.name),
        function_calling_llm=llm_model,
    )


def get_tasks(
        workflow: WorkflowDefinition,
        agent_lookup: dict[str, Any],
        process_id: str,
        run_by: str,
) -> list[Any]:
    tasks_by_id = {task.id: task for task in workflow.task_definitions}
    nodes_by_id = {node.id: node for node in workflow.nodes}
    ordered_nodes = [node for node in workflow.nodes if node.node_type == "task" and node.task_id in tasks_by_id]
    task_lookup: dict[str, Any] = {}
    converted_tasks: list[Any] = []
    for node in ordered_nodes:
        task = tasks_by_id[node.task_id]
        converted_task = convert_task(task, node, nodes_by_id, agent_lookup, process_id, run_by, context=[])
        task_lookup[task.id] = converted_task
        converted_tasks.append(converted_task)

    for index, node in enumerate(ordered_nodes):
        task = tasks_by_id[node.task_id]
        task_config = task_definition_to_crewai_config(task, node, nodes_by_id)
        if task_config["context"]:
            context_tasks = [task_lookup[task_id] for task_id in task_config["context"] if task_id in task_lookup]
            converted_tasks[index].context = context_tasks
    return converted_tasks


def convert_task(
        task: TaskDefinition,
        node: WorkflowNodeDefinition,
        nodes_by_id: Dict[str, WorkflowNodeDefinition],
        agent_lookup: dict[str, Any],
        process_id: str,
        run_by: str,
        context: Optional[list[Any]] = None,
):
    ensure_crewai_available()
    from crewai import Task

    task_config = task_definition_to_crewai_config(task, node, nodes_by_id)
    agent = agent_lookup.get(task_config["agent_id"])
    if not agent:
        raise ValueError(f"Agent {task_config['agent_id']} not found in agent lookup")

    return Task(
        name=task_config["name"],
        description=task_config["description"],
        expected_output=task_config["expected_output"],
        agent=agent,
        callback=None,
        async_execution=False,
        output_json=None,
        output_pydantic=None,
        output_file=None,
        tools=resolve_task_tools([{"id": tool_id, "parameters": {}} for tool_id in task_config["tool_ids"]], process_id,
                                 run_by),
        human_input=task_config["human_input"],
        context=context or [],
    )


def get_agent_lookup(agents: List[AgentDefinition], crewai_agents: list[Any]) -> dict[str, Any]:
    agent_lookup: dict[str, Any] = {}
    for agent_definition, crewai_agent in zip(agents, crewai_agents):
        agent_lookup[agent_definition.id] = crewai_agent
    return agent_lookup


def convert_workflow(
        workflow: WorkflowDefinition,
        process_id: str,
        run_by: str,
        *,
        default_model: str,
        model_profiles: dict[str, ModelProfileDefinition] | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
):
    ensure_crewai_available()
    from crewai import Crew
    from crewai.process import Process

    if not workflow.agent_definitions:
        raise ValueError("Workflow must have at least one agent")
    if not workflow.task_definitions:
        raise ValueError("Workflow must have at least one task")

    workflow_config = workflow_to_crewai_config(workflow, default_model=default_model)
    crewai_agents = get_agents(
        workflow.agent_definitions,
        process_id,
        run_by,
        default_model=default_model,
        model_profiles=model_profiles,
        model_provider_registry=model_provider_registry,
        model_event_loop=model_event_loop,
    )
    agent_lookup = get_agent_lookup(workflow.agent_definitions, crewai_agents)
    crewai_tasks = get_tasks(workflow, agent_lookup, process_id, run_by)
    process_value = Process.sequential

    return Crew(
        name=workflow_config["name"] or "workflow",
        tasks=crewai_tasks,
        agents=crewai_agents,
        process=process_value,
        verbose=True,
        manager_llm=None,
        manager_agent=None,
        function_calling_llm=None,
        cache=False,
        max_rpm=None,
        share_crew=False,
        step_callback=None,
        task_callback=None,
        prompt_file=None,
        output_log_file=False,
        memory=False,
        embedder={"provider": "openai"},
        planning=False,
        planning_llm=None,
        knowledge_sources=None,
        chat_llm=None,
    )


def run_workflow(
        workflow: WorkflowDefinition,
        inputs: Dict[str, str],
        queue,
        process_id: str,
        run_by: str,
        *,
        default_model: str,
        model_profiles: dict[str, ModelProfileDefinition] | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
) -> str:
    raw_output = ""
    try:
        crew = convert_workflow(
            workflow,
            process_id,
            run_by,
            default_model=default_model,
            model_profiles=model_profiles,
            model_provider_registry=model_provider_registry,
            model_event_loop=model_event_loop,
        )
        crew_output = crew.kickoff(inputs=inputs)
        raw_output = getattr(crew_output, "raw", str(crew_output))
    except Exception as exc:
        error_msg = f"Failed to execute crew: {str(exc)}"
        print(f"{error_msg}\n{traceback.format_exc()}")
        raw_output = f"Error Encountered: {error_msg}"
    finally:
        queue.put(raw_output)

    return raw_output

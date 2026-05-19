from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.domain import AgentDefinition, SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType

if TYPE_CHECKING:
    from app.api.context import ApiContext

SYSTEM_WORKFLOW_LIST_TOOL_ID = "agency.workflow.list"
SYSTEM_WORKFLOW_GET_TOOL_ID = "agency.workflow.get"
SYSTEM_WORKFLOW_RUN_TOOL_ID = "agency.workflow.run"
SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID = "agency.workflow.propose-create"
SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID = "agency.workflow.propose-update"
SYSTEM_WORKFLOW_TOOL_TARGET = "agency.system.workflow"
SYSTEM_TOOL_LIST_TOOL_ID = "agency.tool.list"
SYSTEM_TOOL_GET_TOOL_ID = "agency.tool.get"
SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID = "agency.tool.propose-create"
SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID = "agency.tool.propose-update"
SYSTEM_TOOL_MANAGEMENT_TARGET = "agency.system.tool"
SYSTEM_MEMORY_LIST_TOOL_ID = "agency.memory.list"
SYSTEM_MEMORY_REMEMBER_TOOL_ID = "agency.memory.remember"
SYSTEM_MEMORY_UPDATE_TOOL_ID = "agency.memory.update"
SYSTEM_MEMORY_DELETE_TOOL_ID = "agency.memory.delete"
SYSTEM_MEMORY_TOOL_TARGET = "agency.system.memory"
SYSTEM_EXECUTION_GET_TOOL_ID = "agency.execution.get"
SYSTEM_EXECUTION_EVENTS_TOOL_ID = "agency.execution.events"
SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID = "agency.execution.artifacts"
SYSTEM_EXECUTION_TOOL_TARGET = "agency.system.execution"
SYSTEM_COMMAND_RUN_TOOL_ID = "agency.command.run"
SYSTEM_COMMAND_TOOL_TARGET = "agency.system.command"
LEGACY_MAIN_AGENT_INTERNAL_TOOL_PREFIX = "__main_agent__:"


ITEMS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "List of matching records.",
        },
    },
    "required": ["items"],
    "additionalProperties": True,
}

RESULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"description": "Tool-specific result payload."},
    },
    "required": ["result"],
    "additionalProperties": True,
}

PROPOSAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "approval_request": {
            "type": "object",
            "description": "Approval request created for the proposed mutation.",
        },
        "preview": {
            "type": "object",
            "description": "Preview of the proposed workflow or tool mutation.",
        },
        "error": {
            "type": "string",
            "description": "Validation or policy error if the proposal was rejected before approval.",
        },
    },
    "additionalProperties": True,
}

COMMAND_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "ok for exit code 0, error for non-zero exit."},
        "stdout": {"type": "string", "description": "Captured standard output, possibly truncated."},
        "stderr": {"type": "string", "description": "Captured standard error, possibly truncated."},
        "exit_code": {"type": "integer", "description": "Process exit code."},
        "duration_ms": {"type": "integer", "description": "Command runtime in milliseconds."},
        "output_text": {"type": "string", "description": "LLM-facing combined command output."},
        "truncated": {"type": "boolean", "description": "Whether output was truncated."},
        "overflow_path": {
            "type": ["string", "null"],
            "description": "Path to full captured output when truncation occurred.",
        },
    },
    "required": ["status", "stdout", "stderr", "exit_code", "duration_ms", "output_text", "truncated"],
    "additionalProperties": True,
}


def workflow_system_tool_ids(*, can_trigger_workflows: bool = True) -> list[str]:
    if not can_trigger_workflows:
        return []
    return [
        SYSTEM_WORKFLOW_LIST_TOOL_ID,
        SYSTEM_WORKFLOW_GET_TOOL_ID,
        SYSTEM_WORKFLOW_RUN_TOOL_ID,
        SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
        SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
    ]


def tool_management_system_tool_ids(*, can_manage_tools: bool = True) -> list[str]:
    if not can_manage_tools:
        return []
    return [
        SYSTEM_TOOL_LIST_TOOL_ID,
        SYSTEM_TOOL_GET_TOOL_ID,
        SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID,
        SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID,
    ]


def memory_system_tool_ids(*, can_manage_memory: bool = True) -> list[str]:
    if not can_manage_memory:
        return []
    return [
        SYSTEM_MEMORY_LIST_TOOL_ID,
        SYSTEM_MEMORY_REMEMBER_TOOL_ID,
        SYSTEM_MEMORY_UPDATE_TOOL_ID,
        SYSTEM_MEMORY_DELETE_TOOL_ID,
    ]


def command_system_tool_ids(*, can_run_commands: bool = True) -> list[str]:
    if not can_run_commands:
        return []
    return [SYSTEM_COMMAND_RUN_TOOL_ID]


def execution_system_tool_ids(*, can_inspect_executions: bool = True) -> list[str]:
    if not can_inspect_executions:
        return []
    return [
        SYSTEM_EXECUTION_GET_TOOL_ID,
        SYSTEM_EXECUTION_EVENTS_TOOL_ID,
        SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    ]


def workflow_system_tool_definitions(*, can_trigger_workflows: bool = True) -> list[ToolDefinition]:
    if not can_trigger_workflows:
        return []
    return [
        ToolDefinition(
            id=SYSTEM_WORKFLOW_LIST_TOOL_ID,
            name="list_workflows",
            display_name="List Workflows",
            description=(
                "List workflows assigned to the deployment that are visible to this agent. "
                "Use this before choosing a workflow unless the user gave an exact workflow id. "
                "Output includes workflow ids, names, descriptions, runtime adapters, and metadata needed to choose safely."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=ITEMS_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_WORKFLOW_TOOL_TARGET,
                callable_name="list_workflows",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "workflow", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_WORKFLOW_GET_TOOL_ID,
            name="get_workflow",
            display_name="Get Workflow",
            description=(
                "Get a visible workflow definition by `workflow_id` before planning a run or update. "
                "Output includes agents, tasks, tool definitions, graph structure, runtime settings, and metadata."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "ID of a visible workflow.",
                    },
                },
                "required": ["workflow_id"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_WORKFLOW_TOOL_TARGET,
                callable_name="get_workflow",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "workflow", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_WORKFLOW_RUN_TOOL_ID,
            name="run_workflow",
            display_name="Run Workflow",
            description=(
                "Run a visible workflow by workflow_id. Call Get Workflow first when inputs are unclear. "
                "Protected workflows request human approval before every execution attempt. "
                "Output returns execution status, id, and runtime payload for follow-up inspection."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "ID of a visible workflow.",
                    },
                    "input_payload": {
                        "type": "object",
                        "description": "Workflow input payload keyed by the workflow's input_keys or task input_schema properties.",
                        "examples": [{"topic": "quarterly launch plan", "audience": "internal operators"}],
                        "additionalProperties": True,
                    },
                    "runtime_adapter_id": {
                        "type": ["string", "null"],
                        "description": "Optional runtime adapter override.",
                    },
                    "conversation_id": {
                        "type": ["string", "null"],
                        "description": "Optional conversation id for protected workflow approval requests.",
                    },
                    "origin_message_id": {
                        "type": ["string", "null"],
                        "description": "Optional existing origin message id for protected workflow approval requests.",
                    },
                },
                "required": ["workflow_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string", "description": "Created execution id."},
                    "status": {"type": "string", "description": "Execution status."},
                    "output": {"description": "Execution output payload."},
                    "error": {"type": ["string", "null"], "description": "Execution error when failed."},
                },
                "required": ["execution_id", "status"],
                "additionalProperties": True,
            },
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_WORKFLOW_TOOL_TARGET,
                callable_name="run_workflow",
            ),
            security=SecuritySettings(read_only=False),
            tags=["system", "workflow", "agent_assignable"],
        ),
        ToolDefinition(
            id=SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
            name="propose_workflow_create",
            display_name="Propose New Workflow",
            description=(
                "Propose creating a workflow. Provide either a complete workflow payload or a natural-language goal. "
                "Prefer goal for smaller context windows; the backend workflow builder can draft and repair the payload. "
                "This always creates a human approval request before the workflow is persisted."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Human-readable summary of the workflow proposal."},
                    "diff_summary": {
                        "type": ["string", "null"],
                        "description": "Optional concise explanation of the proposed workflow contents.",
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "Natural-language workflow goal to draft using the existing workflow builder. "
                            "Use this instead of a full workflow when the required JSON is too large or uncertain."
                        ),
                        "examples": [
                            "Create a workflow that researches a prospect, drafts an outreach email, and asks for approval before sending."
                        ],
                    },
                    "conversation_history": {
                        "type": ["string", "null"],
                        "description": "Optional context for the workflow builder.",
                    },
                    "model_profile_id": {
                        "type": ["string", "null"],
                        "description": "Optional model profile for workflow builder structured generation.",
                    },
                    "restart_active_executions": {
                        "type": "boolean",
                        "description": (
                            "Whether approving this workflow revision should create replacement executions for active "
                            "runs of the same workflow and cancel/remove their existing containers. Use true only when "
                            "the update makes active runs stale or unsafe to continue; default false."
                        ),
                    },
                    "workflow": {
                        "type": "object",
                        "description": (
                            "Complete WorkflowDefinition payload. If validation fails, retry with a smaller goal-based request "
                            "or include only the corrected sections."
                        ),
                        "additionalProperties": True,
                    },
                },
                "additionalProperties": False,
            },
            output_schema=PROPOSAL_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_WORKFLOW_TOOL_TARGET,
                callable_name="propose_workflow_create",
            ),
            security=SecuritySettings(requires_approval=False),
            tags=["system", "workflow", "agent_assignable", "mutation"],
        ),
        ToolDefinition(
            id=SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
            name="propose_workflow_update",
            display_name="Propose Workflow Update",
            description=(
                "Propose updating a visible, mutable workflow. Provide either a complete updated workflow payload "
                "or a natural-language goal. Prefer goal for smaller context windows. This always creates a human "
                "approval request before changes are applied."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "ID of the workflow to update."},
                    "summary": {"type": "string", "description": "Human-readable summary of the proposed update."},
                    "diff_summary": {
                        "type": ["string", "null"],
                        "description": "Optional concise explanation of the changed workflow fields.",
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "Natural-language edit request to apply to the current workflow. Use this when patching a large "
                            "workflow or when validation repair is likely."
                        ),
                        "examples": [
                            "Add a human approval task before any external email tool is called, and keep the existing agents."
                        ],
                    },
                    "conversation_history": {
                        "type": ["string", "null"],
                        "description": "Optional context for the workflow builder.",
                    },
                    "model_profile_id": {
                        "type": ["string", "null"],
                        "description": "Optional model profile for workflow builder structured generation.",
                    },
                    "workflow": {
                        "type": "object",
                        "description": (
                            "Complete updated WorkflowDefinition payload. If omitted, goal is required. If validation fails, "
                            "repair only the fields called out by validation rather than regenerating unrelated sections."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["workflow_id"],
                "additionalProperties": False,
            },
            output_schema=PROPOSAL_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_WORKFLOW_TOOL_TARGET,
                callable_name="propose_workflow_update",
            ),
            security=SecuritySettings(requires_approval=False),
            tags=["system", "workflow", "agent_assignable", "mutation"],
        ),
    ]


def tool_management_system_tool_definitions(*, can_manage_tools: bool = True) -> list[ToolDefinition]:
    if not can_manage_tools:
        return []
    return [
        ToolDefinition(
            id=SYSTEM_TOOL_LIST_TOOL_ID,
            name="list_tools",
            display_name="List Tools",
            description=(
                "List tool definitions available in the deployment. Use this before choosing, assigning, or proposing "
                "tool changes when the exact tool id is unknown. Output includes tool ids, names, descriptions, schemas, "
                "security settings, tags, and implementation references. This is read-only and does not execute tools."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=ITEMS_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_TOOL_MANAGEMENT_TARGET,
                callable_name="list_tools",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "tool_management", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_TOOL_GET_TOOL_ID,
            name="get_tool",
            display_name="Get Tool",
            description=(
                "Get one tool definition by `tool_id`. Use this before planning an update, assigning the tool to an "
                "agent, or explaining a tool's inputs. Output returns the full ToolDefinition including input schema, "
                "output schema, implementation target, security settings, MCP exposure, and tags."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "ID of the tool to inspect.",
                    },
                },
                "required": ["tool_id"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_TOOL_MANAGEMENT_TARGET,
                callable_name="get_tool",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "tool_management", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID,
            name="propose_tool_create",
            display_name="Propose New Tool",
            description=(
                "Propose creating a new tool definition. Use this only when the user asks for a new capability and "
                "you can provide a complete ToolDefinition payload. The backend validates the payload and creates a "
                "human approval request before persistence. Do not use this to execute a tool."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Human-readable summary of the proposed tool."},
                    "diff_summary": {
                        "type": ["string", "null"],
                        "description": "Optional concise explanation of what will be added.",
                    },
                    "tool": {
                        "type": "object",
                        "description": "Complete ToolDefinition payload.",
                        "additionalProperties": True,
                    },
                },
                "required": ["tool"],
                "additionalProperties": False,
            },
            output_schema=PROPOSAL_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_TOOL_MANAGEMENT_TARGET,
                callable_name="propose_tool_create",
            ),
            security=SecuritySettings(requires_approval=False),
            tags=["system", "tool_management", "agent_assignable", "mutation"],
        ),
        ToolDefinition(
            id=SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID,
            name="propose_tool_update",
            display_name="Propose Tool Update",
            description=(
                "Propose updating an existing tool definition. Use Get Tool first when the current definition is not "
                "already in context. The backend validates the full updated ToolDefinition and creates a human approval "
                "request before any change is applied. Do not use this for runtime execution."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {"type": "string", "description": "ID of the existing tool to update."},
                    "summary": {"type": "string", "description": "Human-readable summary of the proposed update."},
                    "diff_summary": {
                        "type": ["string", "null"],
                        "description": "Optional concise explanation of the changed fields.",
                    },
                    "tool": {
                        "type": "object",
                        "description": "Complete updated ToolDefinition payload.",
                        "additionalProperties": True,
                    },
                },
                "required": ["tool_id", "tool"],
                "additionalProperties": False,
            },
            output_schema=PROPOSAL_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_TOOL_MANAGEMENT_TARGET,
                callable_name="propose_tool_update",
            ),
            security=SecuritySettings(requires_approval=False),
            tags=["system", "tool_management", "agent_assignable", "mutation"],
        ),
    ]


def memory_system_tool_definitions(*, can_manage_memory: bool = True) -> list[ToolDefinition]:
    if not can_manage_memory:
        return []
    scope_schema = {"type": "string", "enum": ["user", "workspace", "conversation", "workflow", "global"]}
    return [
        ToolDefinition(
            id=SYSTEM_MEMORY_LIST_TOOL_ID,
            name="list_memories",
            display_name="List Memories",
            description=(
                "List durable memories visible to this conversation. Use this when the user asks what is remembered "
                "or before updating/deleting a memory."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {**scope_schema, "description": "Optional memory scope filter."},
                    "query": {"type": ["string", "null"], "description": "Optional text filter."},
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50,
                        "default": 20,
                        "description": "Maximum number of memories to return.",
                    },
                },
                "additionalProperties": False,
            },
            output_schema=ITEMS_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_MEMORY_TOOL_TARGET,
                callable_name="list_memories",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "memory", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_MEMORY_REMEMBER_TOOL_ID,
            name="remember_memory",
            display_name="Remember Memory",
            description=(
                "Store a durable memory only when the user explicitly asks you to remember something or confirms it. "
                "Sensitive content requires confirmed=true."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {**scope_schema, "description": "Memory scope. Prefer user for personal preferences."},
                    "content": {"type": "string", "description": "The exact fact or preference to remember."},
                    "summary": {"type": ["string", "null"], "description": "Short display summary."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for retrieval and grouping.",
                    },
                    "sensitive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether the memory contains sensitive personal or business information.",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set true only after explicit user confirmation, required for sensitive memory.",
                    },
                    "workspace_id": {"type": ["string", "null"], "description": "Optional workspace scope id."},
                    "conversation_id": {"type": ["string", "null"], "description": "Optional conversation scope id."},
                    "workflow_id": {"type": ["string", "null"], "description": "Optional workflow scope id."},
                },
                "required": ["scope", "content"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_MEMORY_TOOL_TARGET,
                callable_name="remember_memory",
            ),
            security=SecuritySettings(read_only=False),
            tags=["system", "memory", "agent_assignable", "mutation"],
        ),
        ToolDefinition(
            id=SYSTEM_MEMORY_UPDATE_TOOL_ID,
            name="update_memory",
            display_name="Update Memory",
            description=(
                "Update a durable memory after the user asks to correct, refine, or change something that was remembered. "
                "Use List Memories first if the memory id is unknown. Sensitive updates require confirmed=true. "
                "Output returns the updated memory record or a validation error."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "ID of the durable memory to update."},
                    "content": {"type": ["string", "null"], "description": "Replacement memory content, if changing."},
                    "summary": {"type": ["string", "null"], "description": "Replacement short display summary."},
                    "tags": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Replacement tag list, if changing tags.",
                    },
                    "sensitive": {"type": ["boolean", "null"], "description": "Whether the memory is sensitive."},
                    "confirmed": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set true only after explicit user confirmation for sensitive updates.",
                    },
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_MEMORY_TOOL_TARGET,
                callable_name="update_memory",
            ),
            security=SecuritySettings(read_only=False),
            tags=["system", "memory", "agent_assignable", "mutation"],
        ),
        ToolDefinition(
            id=SYSTEM_MEMORY_DELETE_TOOL_ID,
            name="delete_memory",
            display_name="Delete Memory",
            description=(
                "Delete a durable memory after the user explicitly asks Agency to forget it. Use List Memories first "
                "if the memory id is unknown. Output confirms whether the memory was deleted. Side effect: removes "
                "the memory from future retrieval."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "ID of the durable memory to delete."},
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_MEMORY_TOOL_TARGET,
                callable_name="delete_memory",
            ),
            security=SecuritySettings(read_only=False),
            tags=["system", "memory", "agent_assignable", "mutation"],
        ),
    ]


def command_system_tool_definitions(*, can_run_commands: bool = True) -> list[ToolDefinition]:
    if not can_run_commands:
        return []
    return [
        ToolDefinition(
            id=SYSTEM_COMMAND_RUN_TOOL_ID,
            name="run_command",
            display_name="Run Command",
            description=(
                "Run one approved shell command workflow. Prefer Unix-style composition when it is concise: "
                "pipes, &&, ||, and ; are supported by the shell. Use command --help for discovery. "
                "Modes: auto, bash, sh, zsh, powershell, pwsh, cmd. Results include stdout, stderr, exit_code, "
                "duration_ms, and output_text with an [exit:N | duration] footer. Large output is truncated with "
                "an overflow file path for follow-up grep/head/tail exploration."
            ),
            tool_type=ToolType.SHELL_COMMAND,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command or shell chain to run after approval.",
                    },
                    "mode": {
                        "type": ["string", "null"],
                        "enum": ["auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd", None],
                        "description": "Shell mode. Use auto unless the user or target platform requires a specific shell.",
                    },
                    "cwd": {
                        "type": ["string", "null"],
                        "description": "Optional working directory for the command.",
                    },
                    "timeout_seconds": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 7200,
                        "description": (
                            "Optional timeout override. Use longer values for Codex jobs, builds, and test suites."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            output_schema=COMMAND_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="shell_command",
                target=SYSTEM_COMMAND_TOOL_TARGET,
                callable_name="run_command",
                config={"timeout": 30, "max_timeout": 7200},
            ),
            security=SecuritySettings(
                requires_approval=True,
                sandbox=True,
                allow_shell=True,
                allow_filesystem=True,
                allow_network=False,
                read_only=False,
                dangerous=True,
            ),
            tags=["system", "command", "cli", "shell", "agent_assignable", "mutation"],
        )
    ]


def execution_system_tool_definitions(*, can_inspect_executions: bool = True) -> list[ToolDefinition]:
    if not can_inspect_executions:
        return []
    return [
        ToolDefinition(
            id=SYSTEM_EXECUTION_GET_TOOL_ID,
            name="get_execution",
            display_name="Get Execution",
            description=(
                "Read one Agency execution by execution_id. Use this when evaluating a run's status, input, output, "
                "error, runtime metadata, or workflow id. This tool is read-only and never starts, mutates, cancels, "
                "or retries executions."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "ID of the execution to inspect.",
                    },
                },
                "required": ["execution_id"],
                "additionalProperties": False,
            },
            output_schema=RESULT_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_EXECUTION_TOOL_TARGET,
                callable_name="get_execution",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "execution", "evaluation", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_EXECUTION_EVENTS_TOOL_ID,
            name="list_execution_events",
            display_name="List Execution Events",
            description=(
                "Read canonical execution events by execution_id. Use this to evaluate tool choice, event ordering, "
                "model calls, approvals, failures, and task progress. Supports optional event_type, agent_id, task_id, "
                "sequence, and limit filters. This tool is read-only."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "ID of the execution whose events should be inspected.",
                    },
                    "after_sequence": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Only return events after this sequence number.",
                    },
                    "event_types": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Optional event type filter, for example execution.completed or tool.call.completed.",
                    },
                    "agent_id": {
                        "type": ["string", "null"],
                        "description": "Optional agent id filter.",
                    },
                    "task_id": {
                        "type": ["string", "null"],
                        "description": "Optional task id filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 200,
                        "description": "Maximum number of matching events to return.",
                    },
                },
                "required": ["execution_id"],
                "additionalProperties": False,
            },
            output_schema=ITEMS_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_EXECUTION_TOOL_TARGET,
                callable_name="list_execution_events",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "execution", "events", "evaluation", "agent_assignable", "read_only"],
        ),
        ToolDefinition(
            id=SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
            name="list_execution_artifacts",
            display_name="List Execution Artifacts",
            description=(
                "Read execution artifacts by execution_id. Use this when the run produced files, JSON payloads, "
                "screenshots, or text artifacts needed for evaluation. Content is returned only when include_content "
                "is true and large text is truncated. This tool is read-only."
            ),
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "ID of the execution whose artifacts should be inspected.",
                    },
                    "include_content": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to include artifact content_json and content_text fields.",
                    },
                    "max_content_chars": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 20000,
                        "default": 4000,
                        "description": "Maximum characters of content_text to return for each artifact.",
                    },
                },
                "required": ["execution_id"],
                "additionalProperties": False,
            },
            output_schema=ITEMS_OUTPUT_SCHEMA,
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target=SYSTEM_EXECUTION_TOOL_TARGET,
                callable_name="list_execution_artifacts",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "execution", "artifacts", "evaluation", "agent_assignable", "read_only"],
        ),
    ]


def is_system_workflow_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_WORKFLOW_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_WORKFLOW_LIST_TOOL_ID,
                SYSTEM_WORKFLOW_GET_TOOL_ID,
                SYSTEM_WORKFLOW_RUN_TOOL_ID,
                SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
                SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
            }
    )


def is_system_tool_management_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_TOOL_MANAGEMENT_TARGET
            or tool.id
            in {
                SYSTEM_TOOL_LIST_TOOL_ID,
                SYSTEM_TOOL_GET_TOOL_ID,
                SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID,
                SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID,
            }
    )


def is_system_memory_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_MEMORY_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_MEMORY_LIST_TOOL_ID,
                SYSTEM_MEMORY_REMEMBER_TOOL_ID,
                SYSTEM_MEMORY_UPDATE_TOOL_ID,
                SYSTEM_MEMORY_DELETE_TOOL_ID,
            }
    )


def is_system_command_tool(tool: ToolDefinition) -> bool:
    return tool.implementation.target == SYSTEM_COMMAND_TOOL_TARGET or tool.id == SYSTEM_COMMAND_RUN_TOOL_ID


def is_system_execution_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_EXECUTION_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_EXECUTION_GET_TOOL_ID,
                SYSTEM_EXECUTION_EVENTS_TOOL_ID,
                SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
            }
    )


@dataclass(slots=True)
class AgentToolResolver:
    context: ApiContext

    async def ensure_workflow_system_tools(self, *, can_trigger_workflows: bool = True) -> list[ToolDefinition]:
        tools = workflow_system_tool_definitions(can_trigger_workflows=can_trigger_workflows)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_tool_management_system_tools(self, *, can_manage_tools: bool = True) -> list[ToolDefinition]:
        tools = tool_management_system_tool_definitions(can_manage_tools=can_manage_tools)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_memory_system_tools(self, *, can_manage_memory: bool = True) -> list[ToolDefinition]:
        tools = memory_system_tool_definitions(can_manage_memory=can_manage_memory)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_command_system_tools(self, *, can_run_commands: bool = True) -> list[ToolDefinition]:
        tools = command_system_tool_definitions(can_run_commands=can_run_commands)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_execution_system_tools(self, *, can_inspect_executions: bool = True) -> list[ToolDefinition]:
        tools = execution_system_tool_definitions(can_inspect_executions=can_inspect_executions)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def resolve_agent_tools(self, agent: AgentDefinition | None) -> list[ToolDefinition]:
        if agent is None or not agent.tool_ids:
            return []
        tools: list[ToolDefinition] = []
        seen: set[str] = set()
        for tool_id in agent.tool_ids:
            if tool_id in seen:
                continue
            seen.add(tool_id)
            tool = await self.context.tool_repo.get(tool_id)
            if tool is not None:
                tools.append(tool)
        return tools

    def main_agent_default_tool_ids(self, policy: dict[str, Any]) -> list[str]:
        return [
            *workflow_system_tool_ids(can_trigger_workflows=policy.get("can_trigger_workflows", True)),
            *tool_management_system_tool_ids(can_manage_tools=policy.get("can_manage_tools", True)),
            *memory_system_tool_ids(can_manage_memory=policy.get("can_manage_memory", True)),
            *command_system_tool_ids(can_run_commands=policy.get("can_run_commands", True)),
        ]

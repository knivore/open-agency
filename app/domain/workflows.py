from __future__ import annotations

from enum import Enum
from pydantic import AliasChoices, Field
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .agents import AgentDefinition, FrameworkHints
from .credentials import DomainModel
from .tools import ToolDefinition


class RuntimeAdapterType(str, Enum):
    NATIVE = "native"
    CREWAI = "crewai"
    OPENAI_AGENTS = "openai_agents"
    NEMO_AGENT_TOOLKIT = "nemo_agent_toolkit"
    PYDANTIC_AGENTS = "pydantic_agents"
    OTHER = "other"


class NodeType(str, Enum):
    AGENT = "agent"
    TASK = "task"
    TOOL = "tool"
    APPROVAL = "approval"
    SUBWORKFLOW = "subworkflow"
    TRIGGER = "trigger"


class EdgeType(str, Enum):
    DEFAULT = "default"
    SUCCESS = "success"
    FAILURE = "failure"
    APPROVAL = "approval"
    HANDOFF = "handoff"


class VersionDefinition(DomainModel):
    version: str = "1.0.0"
    revision: int = 1
    parent_version: Optional[str] = None
    is_published: bool = False
    labels: List[str] = Field(default_factory=list)


class TaskDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str
    instructions: Optional[str] = None
    expected_output: Optional[str] = None
    agent_id: Optional[str] = None
    tool_ids: List[str] = Field(default_factory=list)
    depends_on_task_ids: List[str] = Field(default_factory=list)
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    human_approval_required: bool = False
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowNodeDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    node_type: NodeType
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    tool_id: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowEdgeDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_node_id: str
    target_node_id: str
    edge_type: EdgeType = EdgeType.DEFAULT
    condition: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: Optional[str] = None
    nodes: List[WorkflowNodeDefinition] = Field(default_factory=list)
    edges: List[WorkflowEdgeDefinition] = Field(default_factory=list)
    entrypoint: str
    task_definitions: List[TaskDefinition] = Field(default_factory=list)
    agent_definitions: List[AgentDefinition] = Field(default_factory=list)
    tool_definitions: List[ToolDefinition] = Field(default_factory=list)
    allowed_runtime_adapter_ids: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("allowed_runtime_adapter_ids", "allowed_runtime_adapters"),
        serialization_alias="allowed_runtime_adapter_ids",
    )
    default_runtime_adapter_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("default_runtime_adapter_id", "default_runtime_adapter"),
        serialization_alias="default_runtime_adapter_id",
    )
    versioning: VersionDefinition = Field(default_factory=VersionDefinition)
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed_runtime_adapters(self) -> List[str]:
        return self.allowed_runtime_adapter_ids

    @property
    def default_runtime_adapter(self) -> Optional[str]:
        return self.default_runtime_adapter_id


class RuntimeAdapterDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    adapter_type: RuntimeAdapterType
    description: Optional[str] = None
    version: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    config_schema: Dict[str, Any] = Field(default_factory=dict)
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)

from __future__ import annotations

from app.domain import ToolDefinition
from app.domain.tools import SecuritySettings, ToolImplementationReference, ToolType


EXTERNAL_EXAMPLE_READ_TOOL_ID = "agency.external-example.read"


def external_example_system_tool_definitions(*, enabled: bool = True) -> list[ToolDefinition]:
    if not enabled:
        return []
    return [
        ToolDefinition(
            id=EXTERNAL_EXAMPLE_READ_TOOL_ID,
            name="external_example_read",
            display_name="External Example Read",
            description="Read a tiny status payload from the external example module pack.",
            tool_type=ToolType.WORKFLOW_TOOL,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                },
                "required": ["status"],
                "additionalProperties": True,
            },
            implementation=ToolImplementationReference(
                implementation_type="workflow_tool",
                target="tests.fixtures.external_module_pack.runtime_tools",
                callable_name="ExternalExampleRuntimeToolHandler",
            ),
            security=SecuritySettings(read_only=True),
            tags=["system", "optional-module", "external-example"],
        )
    ]


def external_example_system_tool_ids(*, enabled: bool = True) -> list[str]:
    if not enabled:
        return []
    return [EXTERNAL_EXAMPLE_READ_TOOL_ID]


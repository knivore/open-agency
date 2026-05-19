from __future__ import annotations

from typing import Any

from app.domain import AgentDefinition


def agent_definition_to_card(agent: AgentDefinition, *, base_url: str = "", endpoint_path: str = "/a2a/tasks") -> dict[
    str, Any]:
    capabilities = []
    if agent.tool_ids:
        capabilities.append("tool-use")
    if agent.handoff_agent_ids:
        capabilities.append("handoff")
    if agent.memory.enabled:
        capabilities.append("memory")

    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.system_prompt or "",
        "capabilities": capabilities,
        "input_modes": ["text", "json"],
        "output_modes": ["text", "artifact", "json"],
        "skills": agent.tool_ids,
        "endpoint": f"{base_url}{endpoint_path}",
        "metadata": {
            "role": agent.role,
            "instructions": agent.instructions,
            "backstory": agent.backstory,
        },
    }

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain import ExecutionEventType

LLM_PARSE_FAILURE = "Failed to parse LLM response"


def _is_llm_parse_failure(value: Any) -> bool:
    return isinstance(value, str) and value.strip() == LLM_PARSE_FAILURE


def _add_normalized_field(entry: dict[str, Any], field_name: str, value: Any) -> None:
    if field_name == "thought" and _is_llm_parse_failure(value):
        entry["thought"] = None
        entry["thought_parse_error"] = True
        return
    entry[field_name] = value


def _thought_payload(item: dict[str, Any]) -> dict[str, Any]:
    thought = item.get("thought")
    parse_error = bool(item.get("thought_parse_error")) or _is_llm_parse_failure(thought)
    payload = {"thought": None if parse_error else thought}
    if parse_error:
        payload["thought_parse_error"] = True
    return payload


class CallbackLog:
    def __init__(self, log_filepath: str):
        self.log_filepath = log_filepath

    def _format_agent_output(self, agent_output: Any, agent_name: str) -> dict[str, Any] | None:
        base_entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_name": agent_name,
            "type": type(agent_output).__name__,
        }

        output_type = type(agent_output).__name__
        if output_type == "ToolResult":
            return None

        if output_type == "AgentFinish":
            entry = {**base_entry}
            for field_name in ("thought", "output", "text"):
                value = getattr(agent_output, field_name, None)
                if value is not None:
                    _add_normalized_field(entry, field_name, value)
            return entry

        if output_type == "AgentAction":
            entry = {**base_entry}
            for field_name in ("thought", "tool", "tool_input", "result", "text"):
                value = getattr(agent_output, field_name, None)
                if value not in (None, "{}"):
                    _add_normalized_field(entry, field_name, value)
            return entry

        return {
            **base_entry,
            "output": str(agent_output),
            "format": "unknown",
        }

    def print_agent_output(self, agent_output: Any, agent_name: str = "Generic call") -> None:
        entry = self._format_agent_output(agent_output, agent_name)
        try:
            try:
                with open(self.log_filepath, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, list):
                    data = []
            except (FileNotFoundError, json.JSONDecodeError):
                data = []

            if entry is not None:
                data.append(entry)

            with open(self.log_filepath, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except Exception as exc:  # pragma: no cover
            print(f"Error writing to log file: {str(exc)}")


def print_agent_output_to_json(process_id: str, agent_output: Any, agent_name: str = "Generic call") -> None:
    file_path = f"logs/{process_id}.json" if process_id else "logs/task_result.json"
    CallbackLog(file_path).print_agent_output(agent_output, agent_name)


def _log_entries(log_path: str) -> list[dict[str, Any]]:
    path = Path(log_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


async def emit_callback_events(emitter, state, items: list[dict[str, Any]]) -> None:
    for item in items:
        actor = item.get("agent_name")
        if item.get("tool"):
            await emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_STARTED,
                actor=actor,
                payload={"tool": item.get("tool"), "tool_input": item.get("tool_input")},
                metadata={"source": "crewai_log"},
            )
            event_type = ExecutionEventType.TOOL_CALL_FAILED if str(item.get("result", "")).lower().startswith(
                "error") else ExecutionEventType.TOOL_CALL_COMPLETED
            await emitter.emit(
                state,
                event_type,
                actor=actor,
                payload={"tool": item.get("tool"), "tool_input": item.get("tool_input"), "result": item.get("result"),
                         "text": item.get("text")},
                metadata={"source": "crewai_log"},
            )
            continue

        if item.get("output") or item.get("text") or item.get("thought") or item.get("thought_parse_error"):
            await emitter.emit(
                state,
                ExecutionEventType.LLM_REQUEST_CREATED,
                actor=actor,
                payload=_thought_payload(item),
                metadata={"source": "crewai_log"},
            )
            await emitter.emit(
                state,
                ExecutionEventType.LLM_RESPONSE_CREATED,
                actor=actor,
                payload={"output": item.get("output"), "text": item.get("text"), **_thought_payload(item)},
                metadata={"source": "crewai_log"},
            )
            continue

        await emitter.emit(
            state,
            ExecutionEventType.AGENT_MESSAGE_CREATED,
            actor=actor,
            payload={"raw": item},
            metadata={"source": "crewai_log"},
        )


async def replay_callback_events(emitter, state, log_path: str, *, start_index: int = 0) -> int:
    items = _log_entries(log_path)
    if start_index >= len(items):
        return len(items)
    await emit_callback_events(emitter, state, items[start_index:])
    return len(items)

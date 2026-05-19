from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain import ToolDefinition


def _remote_properties(tool: ToolDefinition) -> set[str]:
    schema = tool.implementation.config.get("remote_input_schema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return set()
    return {str(key) for key in properties}


def _choose_field(remote_properties: set[str], *candidates: str) -> str:
    for candidate in candidates:
        if candidate in remote_properties:
            return candidate
    return candidates[0]


def _set_if_present(target: dict[str, Any], field_name: str, value: Any) -> None:
    if value is not None:
        target[field_name] = value


def _copy_matching_passthrough(arguments: dict[str, Any], remote_properties: set[str]) -> dict[str, Any]:
    if not remote_properties:
        return dict(arguments)
    return {key: value for key, value in arguments.items() if key in remote_properties}


def adapt_computer_use_arguments(tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    canonical_name = tool.implementation.config.get("canonical_tool_name") or tool.name
    remote_properties = _remote_properties(tool)
    adapted = _copy_matching_passthrough(arguments, remote_properties)

    if canonical_name == "press_key":
        value = arguments.get("keys", arguments.get("shortcut", arguments.get("key")))
        field = _choose_field(remote_properties, "keys", "shortcut", "key")
        _set_if_present(adapted, field, value)
        return adapted

    if canonical_name == "type":
        text_field = _choose_field(remote_properties, "text", "value", "input")
        _set_if_present(adapted, text_field, arguments.get("text", arguments.get(text_field)))
        _set_if_present(adapted, _choose_field(remote_properties, "x", "target_x"), arguments.get("x"))
        _set_if_present(adapted, _choose_field(remote_properties, "y", "target_y"), arguments.get("y"))
        _set_if_present(adapted, _choose_field(remote_properties, "clear", "clear_existing"), arguments.get("clear"))
        return adapted

    if canonical_name == "click":
        _set_if_present(adapted, _choose_field(remote_properties, "x", "target_x"), arguments.get("x"))
        _set_if_present(adapted, _choose_field(remote_properties, "y", "target_y"), arguments.get("y"))
        button_value = arguments.get("button", arguments.get("click_type"))
        _set_if_present(adapted, _choose_field(remote_properties, "button", "click_type"), button_value)
        if arguments.get("double_click"):
            if "double_click" in remote_properties:
                adapted["double_click"] = True
            elif "click_type" in remote_properties:
                adapted["click_type"] = "double"
            elif "clicks" in remote_properties:
                adapted["clicks"] = 2
        return adapted

    if canonical_name == "move":
        _set_if_present(adapted, _choose_field(remote_properties, "x", "target_x", "to_x"),
                        arguments.get("x", arguments.get("to_x")))
        _set_if_present(adapted, _choose_field(remote_properties, "y", "target_y", "to_y"),
                        arguments.get("y", arguments.get("to_y")))
        _set_if_present(adapted, _choose_field(remote_properties, "drag", "is_drag"), arguments.get("drag"))
        _set_if_present(adapted, "from_x", arguments.get("from_x"))
        _set_if_present(adapted, "from_y", arguments.get("from_y"))
        _set_if_present(adapted, _choose_field(remote_properties, "duration_ms", "duration"),
                        arguments.get("duration_ms"))
        return adapted

    if canonical_name == "scroll":
        _set_if_present(adapted, "x", arguments.get("x"))
        _set_if_present(adapted, "y", arguments.get("y"))
        _set_if_present(adapted, _choose_field(remote_properties, "direction", "axis"), arguments.get("direction"))
        _set_if_present(adapted, _choose_field(remote_properties, "amount", "delta", "clicks"), arguments.get("amount"))
        _set_if_present(adapted, "dx", arguments.get("dx"))
        _set_if_present(adapted, "dy", arguments.get("dy"))
        return adapted

    if canonical_name in {"snapshot", "screenshot"}:
        _set_if_present(adapted, "display", arguments.get("display"))
        _set_if_present(adapted, _choose_field(remote_properties, "use_vision", "vision"), arguments.get("use_vision"))
        _set_if_present(adapted, "use_dom", arguments.get("use_dom"))
        _set_if_present(adapted, "annotate", arguments.get("annotate"))
        return adapted

    if canonical_name == "wait":
        seconds_value = arguments.get("seconds", arguments.get("duration"))
        field = _choose_field(remote_properties, "seconds", "duration", "duration_seconds")
        _set_if_present(adapted, field, seconds_value)
        return adapted

    if canonical_name == "app":
        _set_if_present(adapted, _choose_field(remote_properties, "action", "operation"), arguments.get("action"))
        _set_if_present(adapted, _choose_field(remote_properties, "name", "app_name", "application"),
                        arguments.get("name", arguments.get("app_name")))
        _set_if_present(adapted, "bundle_id", arguments.get("bundle_id"))
        _set_if_present(adapted, "window_title", arguments.get("window_title"))
        _set_if_present(adapted, "x", arguments.get("x"))
        _set_if_present(adapted, "y", arguments.get("y"))
        _set_if_present(adapted, "width", arguments.get("width"))
        _set_if_present(adapted, "height", arguments.get("height"))
        return adapted

    if canonical_name == "shell":
        _set_if_present(adapted, _choose_field(remote_properties, "command", "cmd"), arguments.get("command"))
        _set_if_present(adapted, "mode", arguments.get("mode"))
        return adapted

    if canonical_name == "scrape":
        _set_if_present(adapted, "url", arguments.get("url"))
        _set_if_present(adapted, "use_dom", arguments.get("use_dom"))
        _set_if_present(adapted, "selector", arguments.get("selector"))
        return adapted

    if canonical_name == "multi_select":
        _set_if_present(adapted, "labels", arguments.get("labels"))
        _set_if_present(adapted, "coordinates", arguments.get("coordinates"))
        _set_if_present(adapted, "ctrl", arguments.get("ctrl"))
        return adapted

    if canonical_name == "multi_edit":
        _set_if_present(adapted, "edits", arguments.get("edits"))
        _set_if_present(adapted, "clear", arguments.get("clear"))
        return adapted

    if canonical_name == "clipboard":
        _set_if_present(adapted, _choose_field(remote_properties, "action", "operation"), arguments.get("action"))
        _set_if_present(adapted, "text", arguments.get("text"))
        return adapted

    if canonical_name == "process":
        _set_if_present(adapted, _choose_field(remote_properties, "action", "operation"), arguments.get("action"))
        _set_if_present(adapted, "pid", arguments.get("pid"))
        _set_if_present(adapted, "name", arguments.get("name"))
        return adapted

    if canonical_name == "notification":
        _set_if_present(adapted, "title", arguments.get("title"))
        _set_if_present(adapted, "message", arguments.get("message"))
        return adapted

    if canonical_name == "registry":
        _set_if_present(adapted, _choose_field(remote_properties, "action", "operation"), arguments.get("action"))
        _set_if_present(adapted, "path", arguments.get("path"))
        _set_if_present(adapted, "name", arguments.get("name"))
        _set_if_present(adapted, "value", arguments.get("value"))
        return adapted

    return adapted


def canonical_computer_use_schema(canonical_name: str, remote_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    remote_schema = remote_schema or {"type": "object"}
    base: dict[str, Any] = {"type": "object", "additionalProperties": True}
    schemas: dict[str, dict[str, Any]] = {
        "click": {
            **base,
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "button": {"type": "string"},
                "double_click": {"type": "boolean"},
            },
            "required": ["x", "y"],
        },
        "type": {
            **base,
            "properties": {
                "text": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "clear": {"type": "boolean"},
            },
            "required": ["text"],
        },
        "scroll": {
            **base,
            "properties": {
                "direction": {"type": "string"},
                "amount": {"type": "number"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "dx": {"type": "number"},
                "dy": {"type": "number"},
            },
        },
        "move": {
            **base,
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "drag": {"type": "boolean"},
                "from_x": {"type": "number"},
                "from_y": {"type": "number"},
                "to_x": {"type": "number"},
                "to_y": {"type": "number"},
                "duration_ms": {"type": "number"},
            },
        },
        "press_key": {
            **base,
            "properties": {
                "keys": {"type": "string"},
            },
            "required": ["keys"],
        },
        "wait": {
            **base,
            "properties": {
                "seconds": {"type": "number"},
            },
            "required": ["seconds"],
        },
        "snapshot": {
            **base,
            "properties": {
                "display": {"type": "array", "items": {"type": "integer"}},
                "use_vision": {"type": "boolean"},
                "use_dom": {"type": "boolean"},
                "annotate": {"type": "boolean"},
            },
        },
        "screenshot": {
            **base,
            "properties": {
                "display": {"type": "array", "items": {"type": "integer"}},
            },
        },
        "app": {
            **base,
            "properties": {
                "action": {"type": "string"},
                "name": {"type": "string"},
                "bundle_id": {"type": "string"},
                "window_title": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
            },
        },
        "shell": {
            **base,
            "properties": {
                "command": {"type": "string"},
                "mode": {"type": "string"},
            },
            "required": ["command"],
        },
        "scrape": {
            **base,
            "properties": {
                "url": {"type": "string"},
                "use_dom": {"type": "boolean"},
                "selector": {"type": "string"},
            },
        },
    }
    return schemas.get(canonical_name, remote_schema)


def _extract_text_content(raw_result: dict[str, Any]) -> str | None:
    content = raw_result.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    if not parts:
        return None
    return "\n".join(parts)


def _extract_payload(raw_result: dict[str, Any]) -> Any:
    if isinstance(raw_result.get("result"), dict):
        return raw_result["result"]
    text_content = _extract_text_content(raw_result)
    if text_content:
        try:
            return json.loads(text_content)
        except json.JSONDecodeError:
            return {"text": text_content}
    return raw_result


def _status_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("status", "state", "result"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        if payload.get("error"):
            return "error"
        if payload.get("success") is True:
            return "ok"
    return "ok"


def _normalize_snapshot_data(payload: Any, raw_result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"text": _extract_text_content(raw_result)}
    return {
        "image": payload.get("image") or payload.get("screenshot") or payload.get("image_base64") or payload.get(
            "screenshot_path"),
        "elements": payload.get("elements") or payload.get("accessibility_tree") or payload.get("ui_elements"),
        "windows": payload.get("windows") or payload.get("window_titles"),
        "displays": payload.get("displays"),
        "cursor": payload.get("cursor"),
        "text": payload.get("text") or _extract_text_content(raw_result),
    }


def _snapshot_artifact_fields(canonical_name: str, data: dict[str, Any]) -> dict[str, Any]:
    image_value = data.get("image")
    if not isinstance(image_value, str) or not image_value:
        return {}

    artifact_name = f"{canonical_name}.png"
    artifact_fields: dict[str, Any] = {
        "artifact_name": artifact_name,
        "artifact_type": "image",
        "artifact_media_type": "image/png",
    }

    if image_value.startswith("data:image/"):
        artifact_fields["artifact_uri"] = image_value
        return artifact_fields

    if image_value.startswith("/") or image_value.startswith("./") or image_value.startswith("../"):
        artifact_fields["artifact_uri"] = image_value
        artifact_fields["artifact_name"] = Path(image_value).name or artifact_name
        suffix = Path(image_value).suffix.lower()
        if suffix == ".jpg" or suffix == ".jpeg":
            artifact_fields["artifact_media_type"] = "image/jpeg"
        elif suffix == ".webp":
            artifact_fields["artifact_media_type"] = "image/webp"
        elif suffix == ".gif":
            artifact_fields["artifact_media_type"] = "image/gif"
        return artifact_fields

    artifact_fields["artifact_uri"] = f"data:image/png;base64,{image_value}"
    return artifact_fields


def _normalize_shell_data(payload: Any, raw_result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"stdout": _extract_text_content(raw_result)}
    exit_code = payload.get("exit_code")
    if exit_code is None:
        exit_code = payload.get("returncode")
    if exit_code is None:
        exit_code = payload.get("code")
    return {
        "stdout": payload.get("stdout") or payload.get("output") or payload.get("text"),
        "stderr": payload.get("stderr") or payload.get("error"),
        "exit_code": exit_code,
    }


def normalize_computer_use_response(
        tool: ToolDefinition,
        canonical_arguments: dict[str, Any],
        remote_arguments: dict[str, Any],
        raw_result: dict[str, Any],
) -> dict[str, Any]:
    canonical_name = tool.implementation.config.get("canonical_tool_name") or tool.name
    payload = _extract_payload(raw_result)
    base = {
        "status": _status_from_payload(payload),
        "tool_family": "computer_use",
        "tool": canonical_name,
        "platform": tool.implementation.config.get("tool_platform"),
        "remote_tool_name": tool.implementation.config.get(
            "mcp_tool_name") or tool.implementation.callable_name or tool.name,
        "request": canonical_arguments,
        "remote_request": remote_arguments,
        "raw_result": raw_result,
    }

    if canonical_name == "click":
        data = {
            "x": canonical_arguments.get("x"),
            "y": canonical_arguments.get("y"),
            "button": canonical_arguments.get("button"),
            "double_click": canonical_arguments.get("double_click", False),
        }
    elif canonical_name == "type":
        data = {
            "text": canonical_arguments.get("text"),
            "x": canonical_arguments.get("x"),
            "y": canonical_arguments.get("y"),
            "clear": canonical_arguments.get("clear"),
        }
    elif canonical_name == "press_key":
        data = {"keys": canonical_arguments.get("keys")}
    elif canonical_name == "scroll":
        data = {
            "direction": canonical_arguments.get("direction"),
            "amount": canonical_arguments.get("amount"),
            "x": canonical_arguments.get("x"),
            "y": canonical_arguments.get("y"),
            "dx": canonical_arguments.get("dx"),
            "dy": canonical_arguments.get("dy"),
        }
    elif canonical_name == "move":
        data = {
            "x": canonical_arguments.get("x"),
            "y": canonical_arguments.get("y"),
            "drag": canonical_arguments.get("drag"),
            "from_x": canonical_arguments.get("from_x"),
            "from_y": canonical_arguments.get("from_y"),
            "to_x": canonical_arguments.get("to_x"),
            "to_y": canonical_arguments.get("to_y"),
            "duration_ms": canonical_arguments.get("duration_ms"),
        }
    elif canonical_name in {"snapshot", "screenshot"}:
        data = _normalize_snapshot_data(payload, raw_result)
        base.update(_snapshot_artifact_fields(canonical_name, data))
    elif canonical_name == "wait":
        data = {"seconds": canonical_arguments.get("seconds")}
    elif canonical_name == "app":
        data = {
            "action": canonical_arguments.get("action"),
            "name": canonical_arguments.get("name"),
            "bundle_id": canonical_arguments.get("bundle_id"),
            "window_title": canonical_arguments.get("window_title"),
            "x": canonical_arguments.get("x"),
            "y": canonical_arguments.get("y"),
            "width": canonical_arguments.get("width"),
            "height": canonical_arguments.get("height"),
        }
    elif canonical_name == "shell":
        data = _normalize_shell_data(payload, raw_result)
    elif canonical_name == "scrape":
        if isinstance(payload, dict):
            data = {
                "url": payload.get("url") or canonical_arguments.get("url"),
                "title": payload.get("title"),
                "text": payload.get("text") or payload.get("content") or _extract_text_content(raw_result),
                "markdown": payload.get("markdown"),
                "html": payload.get("html"),
            }
        else:
            data = {"url": canonical_arguments.get("url"), "text": _extract_text_content(raw_result)}
    else:
        data = payload if isinstance(payload, dict) else {"value": payload}

    return {
        **base,
        "data": data,
    }

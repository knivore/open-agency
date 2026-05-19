from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain import MCPExposureSettings, SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.tools.implementations.browser import (
    BrowserAnalyseInput,
    BrowserClickInput,
    BrowserExtractInput,
    BrowserInputText,
    BrowserOpenInput,
    BrowserScrollInput,
    BrowserSelectInput,
    BrowserVerifyInput,
)
from app.tools.implementations.audio import TranscribeAudioInput
from app.tools.implementations.documents import SaveMarkdownToWordToolSchema
from app.tools.implementations.http_integrations import CustomAPIInput
from app.tools.implementations.human_input import HumanInputRequest
from app.tools.implementations.spreadsheets import ExcelImageInput, ExcelJSONInput, ExcelTextInput


def load_tool_catalog_config() -> dict[str, Any]:
    config_path = Path(__file__).resolve().parent / "config" / "agency_tools.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _metadata_to_schema(parameters_metadata: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, metadata in (parameters_metadata or {}).items():
        details = metadata if isinstance(metadata, dict) else {}
        property_schema: dict[str, Any] = {"type": details.get("type", "string")}
        if details.get("description"):
            property_schema["description"] = details["description"]
        if "default" in details:
            property_schema["default"] = details["default"]
        if "examples" in details:
            property_schema["examples"] = details["examples"]
        properties[name] = property_schema
        if details.get("mandatory") or details.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _input_schema_for(tool_id: str, parameters_metadata: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, dict[str, Any]] = {
        "agency.human.ask": HumanInputRequest.model_json_schema(),
        "agency.audio.transcribe": TranscribeAudioInput.model_json_schema(),
        "agency.http.request": CustomAPIInput.model_json_schema(),
        "agency.document.markdown-to-word": SaveMarkdownToWordToolSchema.model_json_schema(),
        "agency.excel.write-image": ExcelImageInput.model_json_schema(),
        "agency.excel.write-json": ExcelJSONInput.model_json_schema(),
        "agency.excel.write-text": ExcelTextInput.model_json_schema(),
        "agency.browser.open": BrowserOpenInput.model_json_schema(),
        "agency.browser.analyze-screenshot": BrowserAnalyseInput.model_json_schema(),
        "agency.browser.extract-screenshot": BrowserExtractInput.model_json_schema(),
        "agency.browser.scroll": BrowserScrollInput.model_json_schema(),
        "agency.browser.click": BrowserClickInput.model_json_schema(),
        "agency.browser.select-option": BrowserSelectInput.model_json_schema(),
        "agency.browser.type-text": BrowserInputText.model_json_schema(),
        "agency.browser.verify-content": BrowserVerifyInput.model_json_schema(),
    }
    if tool_id == "agency.browser.screenshot":
        return {
            "type": "object",
            "properties": {
                "full_page_screenshot": {
                    "type": "boolean",
                    "description": "Whether to capture the full scrollable page instead of only the viewport.",
                    "default": False,
                },
                "img_directory": {
                    "type": "string",
                    "description": "Artifact directory for the screenshot.",
                },
            },
        }
    if tool_id == "agency.file.write-text":
        return {
            "type": "object",
            "properties": {
                "base_folder": {"type": "string", "description": "Allowed directory where the file will be written."},
                "filename": {"type": "string", "description": "Optional file name under base_folder."},
                "content": {"type": "string", "description": "Text content to write or append."},
                "mode": {
                    "type": "string",
                    "enum": ["write", "append"],
                    "description": "Write mode: write replaces the file; append adds to the end.",
                },
            },
            "required": ["base_folder", "content", "mode"],
            "additionalProperties": False,
        }
    if tool_id == "agency.browser.close":
        return {"type": "object", "properties": {}}
    return schemas.get(tool_id) or _metadata_to_schema(parameters_metadata)


def _string_or_error_schema(*, description: str) -> dict[str, Any]:
    return {
        "oneOf": [
            {"type": "string", "description": description},
            {
                "type": "object",
                "properties": {
                    "Error Message": {
                        "type": "string",
                        "description": "Human-readable error explaining why the tool failed.",
                    },
                },
                "required": ["Error Message"],
                "additionalProperties": True,
            },
        ],
    }


def _output_schema_for(tool_id: str) -> dict[str, Any]:
    action_message_schema = _string_or_error_schema(description="Human-readable success or status message.")
    spreadsheet_schema = _string_or_error_schema(
        description="Stringified success message when the workbook was updated successfully.",
    )
    mapping: dict[str, dict[str, Any]] = {
        "agency.human.ask": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["received", "timeout"],
                    "description": "Whether the human operator replied before timeout.",
                },
                "response": {
                    "type": "string",
                    "description": "Human reply text, or an empty string on timeout.",
                },
            },
            "required": ["status", "response"],
            "additionalProperties": False,
        },
        "agency.audio.transcribe": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success"]},
                "text": {"type": "string", "description": "Transcribed speech text."},
                "model": {"type": "string", "description": "Transcription model used."},
                "language": {"type": ["string", "null"], "description": "Language hint or detected language."},
                "duration": {"type": ["number", "null"], "description": "Audio duration when returned by the model."},
                "segments": {
                    "type": ["array", "null"],
                    "items": {"type": "object"},
                    "description": "Verbose segment metadata when requested and supported.",
                },
                "response_format": {"type": "string", "description": "Requested response format."},
                "raw_response": {
                    "type": "object",
                    "description": "Raw OpenAI response payload when available.",
                    "additionalProperties": True,
                },
            },
            "required": ["status", "text", "model", "response_format"],
            "additionalProperties": True,
        },
        "agency.http.request": {
            "type": "object",
            "properties": {
                "status_code": {"type": "integer", "description": "HTTP response status code."},
                "response": {"description": "Parsed JSON response body or raw text response body."},
            },
            "required": ["status_code", "response"],
            "additionalProperties": False,
        },
        "agency.document.markdown-to-word": {
            "type": "string",
            "description": "Storage success message or document conversion/upload error message.",
        },
        "agency.file.write-text": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success"], "description": "File write status."},
                "message": {"type": "string", "description": "Human-readable file write result."},
                "path": {"type": "string", "description": "Absolute path to the written file."},
            },
            "required": ["status", "message", "path"],
            "additionalProperties": False,
        },
        "agency.excel.write-image": spreadsheet_schema,
        "agency.excel.write-json": spreadsheet_schema,
        "agency.excel.write-text": spreadsheet_schema,
        "agency.browser.open": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Current browser page URL."},
                        "title": {"type": "string", "description": "Current page title."},
                        "user_agent": {"type": "string", "description": "Browser user agent used for the session."},
                        "runtime_root": {"type": "string", "description": "Browser runtime artifact root."},
                        "trace_path": {
                            "type": ["string", "null"],
                            "description": "Trace artifact path when tracing is enabled.",
                        },
                        "overlay_dismissed": {"type": ["boolean", "null"]},
                        "challenge_detected": {"type": ["boolean", "null"]},
                        "session_state": {"type": "object", "description": "Browser session state and signals."},
                        "message": {"type": "string", "description": "Human-readable navigation result."},
                    },
                    "required": ["url", "title", "user_agent", "runtime_root", "session_state", "message"],
                    "additionalProperties": True,
                },
                {
                    "type": "object",
                    "properties": {
                        "Error Message": {"type": "string", "description": "Browser startup or navigation error."},
                    },
                    "required": ["Error Message"],
                    "additionalProperties": True,
                },
            ],
        },
        "agency.browser.screenshot": action_message_schema,
        "agency.browser.analyze-screenshot": {
            "type": "string",
            "description": "Vision-model analysis or fallback page-state summary.",
        },
        "agency.browser.extract-screenshot": {
            "type": "object",
            "properties": {
                "page_type": {"type": "string", "description": "Detected page type."},
                "page_url": {"type": "string", "description": "Current page URL."},
                "content": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Extracted visual summary."},
                        "text": {"type": "string", "description": "Extracted page text excerpt."},
                    },
                    "required": ["summary", "text"],
                    "additionalProperties": True,
                },
            },
            "required": ["page_type", "page_url", "content"],
            "additionalProperties": True,
        },
        "agency.browser.scroll": action_message_schema,
        "agency.browser.click": action_message_schema,
        "agency.browser.select-option": action_message_schema,
        "agency.browser.type-text": action_message_schema,
        "agency.browser.verify-content": {
            "type": "object",
            "properties": {
                "Verification Reasoning": {"type": "string", "description": "Why the score was assigned."},
                "Verification Score": {"type": "integer", "minimum": 0, "maximum": 100},
                "Challenge Detected": {"type": "boolean", "description": "Whether a page challenge was detected."},
            },
            "required": ["Verification Reasoning", "Verification Score", "Challenge Detected"],
            "additionalProperties": True,
        },
        "agency.browser.close": {
            "type": "object",
            "properties": {
                "Success Message": {"type": "string", "description": "Browser shutdown status."},
            },
            "required": ["Success Message"],
            "additionalProperties": False,
        },
    }
    return mapping.get(tool_id, {"type": "object", "description": "Generic object output."})


def _implementation_for(tool_id: str) -> ToolImplementationReference:
    mapping = {
        "agency.human.ask": ("app.tools.implementations.human_input", "request_human_input"),
        "agency.audio.transcribe": ("app.tools.implementations.audio", "transcribe_audio"),
        "agency.http.request": ("app.tools.implementations.http_integrations", "execute_custom_api"),
        "agency.document.markdown-to-word": ("app.tools.implementations.documents", "save_markdown_to_word"),
        "agency.file.write-text": ("app.tools.implementations.custom.files", "write_text_file"),
        "agency.excel.write-image": ("app.tools.implementations.spreadsheets", "write_excel_image"),
        "agency.excel.write-json": ("app.tools.implementations.spreadsheets", "write_excel_json"),
        "agency.excel.write-text": ("app.tools.implementations.spreadsheets", "write_excel_text"),
        "agency.browser.open": ("app.tools.implementations.browser", "open_browser"),
        "agency.browser.screenshot": ("app.tools.implementations.browser", "screenshot"),
        "agency.browser.analyze-screenshot": ("app.tools.implementations.browser", "screenshot_and_analyse"),
        "agency.browser.extract-screenshot": ("app.tools.implementations.browser", "screenshot_and_extract"),
        "agency.browser.scroll": ("app.tools.implementations.browser", "scroll_page"),
        "agency.browser.click": ("app.tools.implementations.browser", "click_element"),
        "agency.browser.select-option": ("app.tools.implementations.browser", "select_dropdown"),
        "agency.browser.type-text": ("app.tools.implementations.browser", "send_keys"),
        "agency.browser.verify-content": ("app.tools.implementations.browser", "verify_content"),
        "agency.browser.close": ("app.tools.implementations.browser", "terminate_browser"),
    }
    module_name, function_name = mapping[tool_id]
    return ToolImplementationReference(
        implementation_type="python_function",
        module=module_name,
        function=function_name,
    )


def _security_for(tool_id: str) -> SecuritySettings:
    network_tools = {
        "agency.audio.transcribe",
        "agency.http.request",
        "agency.browser.open",
        "agency.browser.screenshot",
        "agency.browser.analyze-screenshot",
        "agency.browser.extract-screenshot",
        "agency.browser.scroll",
        "agency.browser.click",
        "agency.browser.select-option",
        "agency.browser.type-text",
        "agency.browser.verify-content",
        "agency.browser.close",
    }
    browser_tools = {
        "agency.browser.open",
        "agency.browser.screenshot",
        "agency.browser.analyze-screenshot",
        "agency.browser.extract-screenshot",
        "agency.browser.scroll",
        "agency.browser.click",
        "agency.browser.select-option",
        "agency.browser.type-text",
        "agency.browser.verify-content",
        "agency.browser.close",
    }
    filesystem_write_tools = {
        "agency.document.markdown-to-word",
        "agency.file.write-text",
        "agency.excel.write-image",
        "agency.excel.write-json",
        "agency.excel.write-text",
    }
    dangerous_tools = filesystem_write_tools | {
        "agency.browser.click",
        "agency.browser.select-option",
        "agency.browser.type-text",
    }
    read_only_tools = {
        "agency.audio.transcribe",
        "agency.browser.open",
        "agency.browser.screenshot",
        "agency.browser.analyze-screenshot",
        "agency.browser.extract-screenshot",
        "agency.browser.scroll",
        "agency.browser.verify-content",
        "agency.browser.close",
    }

    return SecuritySettings(
        requires_approval=tool_id in dangerous_tools or tool_id == "agency.human.ask",
        sandbox=True if tool_id in dangerous_tools or tool_id in browser_tools else False,
        allow_browser=tool_id in browser_tools,
        allow_filesystem=tool_id in filesystem_write_tools,
        allow_network=tool_id in network_tools,
        allowed_domains=[],
        allowed_paths=["local_storage", "logs", "."] if tool_id in filesystem_write_tools else [],
        module_allowlist=[_implementation_for(tool_id).module],
        function_allowlist=[_implementation_for(tool_id).function] if _implementation_for(tool_id).function else [],
        read_only=tool_id in read_only_tools,
        dangerous=tool_id in dangerous_tools,
        redaction_enabled=tool_id in {"agency.http.request"},
    )


@dataclass(frozen=True)
class ToolCatalogSpec:
    id: str
    name: str
    display_name: str
    description: str
    created_by: str
    owned_by: str
    parameters_metadata: dict[str, Any]
    tool_definition: ToolDefinition


def build_tool_catalog_specs() -> dict[str, ToolCatalogSpec]:
    specs: dict[str, ToolCatalogSpec] = {}
    for tool_id, config in load_tool_catalog_config().items():
        parameters_metadata = config.get("parameters", {}) or {}
        tool_definition = ToolDefinition(
            id=config["id"],
            name=config["name"],
            display_name=config.get("display_name") or config["name"],
            description=config["description"],
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema=_input_schema_for(tool_id, parameters_metadata),
            output_schema=_output_schema_for(tool_id),
            implementation=_implementation_for(tool_id),
            security=_security_for(tool_id),
            mcp_exposure=MCPExposureSettings(),
            tags=["catalog", "crewai"],
            framework_hints={"preferred_adapter": "crewai"},
        )
        specs[tool_id] = ToolCatalogSpec(
            id=config["id"],
            name=config["name"],
            display_name=config.get("display_name") or config["name"],
            description=config["description"],
            created_by=config["created_by"],
            owned_by=config["owned_by"],
            parameters_metadata=parameters_metadata,
            tool_definition=tool_definition,
        )
    return specs


def get_tool_catalog_specs() -> dict[str, ToolCatalogSpec]:
    return build_tool_catalog_specs()


def get_tool_catalog_definitions() -> list[ToolDefinition]:
    return [spec.tool_definition for spec in get_tool_catalog_specs().values()]

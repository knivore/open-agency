"""Built-in app tool normalization.

This module reads the app-owned tool catalog directly from the shared YAML
registry, then attaches runtime schemas, implementations, and security metadata
to produce canonical ToolDefinition objects for the builtin registry.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from app.tools.implementations.documents import SaveMarkdownToWordToolSchema
from app.tools.implementations.http_integrations import CustomAPIInput
from app.tools.implementations.human_input import HumanInputRequest
from app.tools.implementations.media import MediaPublishInput, MediaSendInput
from app.tools.implementations.speech import SpeechContinueInput, SpeechListenInput, SpeechSpeakInput
from app.tools.implementations.spreadsheets import ExcelImageInput, ExcelJSONInput, ExcelTextInput
from app.tools.implementations.voice import VoiceGenerateInput
from app.tools.registry_config import load_agency_tool_registry_config

SCHEMA_FILLED_BY_KEY = "x-agency-filled-by"
SCHEMA_USER_VISIBLE_KEY = "x-agency-user-visible"


def load_tool_catalog_config() -> dict[str, Any]:
    """Load app-owned builtin tool metadata from the shared YAML registry."""
    registry = load_agency_tool_registry_config()
    app_tools = registry.get("app_tools") or {}
    return dict(app_tools if isinstance(app_tools, dict) else {})


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


def _schema_filled_by(details: dict[str, Any]) -> str | None:
    raw_value = details.get("filled_by")
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"user", "agent", "user_or_agent"}:
            return normalized
    if details.get("input_type") == "hidden":
        return "agent"
    return None


def _apply_parameter_contract_metadata(
        schema: dict[str, Any],
        parameters_metadata: dict[str, Any],
) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema

    next_properties: dict[str, Any] = {}
    for name, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            next_properties[name] = property_schema
            continue

        details = parameters_metadata.get(name) if isinstance(parameters_metadata, dict) else None
        details = details if isinstance(details, dict) else {}
        next_property_schema = dict(property_schema)
        filled_by = _schema_filled_by(details)
        if filled_by:
            next_property_schema[SCHEMA_FILLED_BY_KEY] = filled_by
        if details.get("input_type") == "hidden":
            next_property_schema[SCHEMA_USER_VISIBLE_KEY] = False
        next_properties[name] = next_property_schema

    return {
        **schema,
        "properties": next_properties,
    }


def _input_schema_for(tool_id: str, parameters_metadata: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, dict[str, Any]] = {
        "agency.human.ask": HumanInputRequest.model_json_schema(),
        "agency.speech.listen": SpeechListenInput.model_json_schema(),
        "agency.speech.speak": SpeechSpeakInput.model_json_schema(),
        "agency.speech.continue": SpeechContinueInput.model_json_schema(),
        "agency.voice.generate": VoiceGenerateInput.model_json_schema(),
        "agency.media.publish": MediaPublishInput.model_json_schema(),
        "agency.media.send": MediaSendInput.model_json_schema(),
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
    schema: dict[str, Any]
    if tool_id == "agency.browser.screenshot":
        schema = {
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
    elif tool_id == "agency.file.write-text":
        schema = {
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
    elif tool_id == "agency.repo.inspect":
        schema = {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository alias or allowlisted absolute path."},
                "query": {"type": ["string", "null"], "description": "Optional case-insensitive text search."},
                "focus_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repo-relative files or glob patterns to prioritize.",
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repo-relative glob patterns to include.",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repo-relative glob patterns to exclude.",
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 24,
                    "description": "Maximum number of files to scan for matches.",
                },
                "max_hits": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 40,
                    "description": "Maximum number of TODO or query hits to return.",
                },
                "excerpt_line_limit": {
                    "type": "integer",
                    "minimum": 4,
                    "maximum": 120,
                    "default": 24,
                    "description": "Maximum number of lines to include per file excerpt.",
                },
            },
            "required": ["repo"],
            "additionalProperties": False,
        }
    elif tool_id == "agency.browser.close":
        schema = {"type": "object", "properties": {}}
    else:
        schema = schemas.get(tool_id) or _metadata_to_schema(parameters_metadata)
    return _apply_parameter_contract_metadata(schema, parameters_metadata)


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
        "agency.speech.listen": {
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
        "agency.speech.speak": {
            "type": "object",
            "properties": {
                "announcementId": {"type": "string", "description": "Generated announcement id."},
                "status": {"type": "string", "enum": ["accepted"]},
                "text": {"type": "string", "description": "Normalized announcement text."},
                "targetKind": {"type": ["string", "null"], "description": "Target scope for delivery."},
                "targetRef": {"type": ["string", "null"], "description": "Target reference for delivery."},
                "channel": {"type": ["string", "null"], "description": "Requested delivery channel."},
                "ssml": {"type": ["string", "null"], "description": "SSML payload when provided."},
                "voice": {"type": ["string", "null"], "description": "Selected voice preset."},
                "metadata": {"type": "object", "description": "Arbitrary delivery metadata."},
            },
            "required": ["announcementId", "status", "text", "metadata"],
            "additionalProperties": True,
        },
        "agency.speech.continue": {
            "type": "object",
            "properties": {
                "continuationId": {"type": "string", "description": "Generated continuation id."},
                "status": {"type": "string", "enum": ["completed"]},
                "replyText": {"type": "string", "description": "Agent reply generated for the follow-up."},
                "replySsml": {"type": ["string", "null"], "description": "Optional SSML reply payload."},
                "actionsTaken": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Actions triggered while handling the follow-up.",
                },
                "sessionId": {"type": ["string", "null"], "description": "Conversation session id."},
                "priorAnnouncementId": {
                    "type": ["string", "null"],
                    "description": "Announcement id that prompted the response.",
                },
                "channel": {"type": ["string", "null"], "description": "Requested continuation channel."},
                "metadata": {"type": "object", "description": "Arbitrary continuation metadata."},
            },
            "required": ["continuationId", "status", "replyText", "actionsTaken", "metadata"],
            "additionalProperties": True,
        },
        "agency.voice.generate": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["preview", "generated", "setup_required"]},
                "provider": {"type": "string", "description": "Resolved voice provider."},
                "text": {"type": "string", "description": "Normalized text used for synthesis."},
                "voice": {"type": ["string", "null"], "description": "Voice preset or local voice name."},
                "reference_voice_path": {
                    "type": ["string", "null"],
                    "description": "Reference voice path when local OpenVoice generation is used.",
                },
                "file_path": {"type": ["string", "null"], "description": "Resolved local generated file path."},
                "storage_key": {"type": "string", "description": "Agency storage key for the generated audio."},
                "storage_uri": {"type": ["string", "null"], "description": "Internal Agency storage URI."},
                "media_url": {
                    "type": ["string", "null"],
                    "description": "Download URL for downstream tools, workflows, or tied-application delivery.",
                },
                "content_type": {"type": ["string", "null"], "description": "Generated audio MIME type."},
                "provider_fetchable": {
                    "type": ["boolean", "null"],
                    "description": "Whether external providers can fetch media_url directly.",
                },
                "ai_disclosure": {"type": "boolean", "description": "Whether AI speech disclosure is confirmed."},
                "consent_confirmed": {
                    "type": "boolean",
                    "description": "Whether consent is confirmed for cloned/reference voice generation.",
                },
                "purpose": {"type": ["string", "null"], "description": "Audit purpose."},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "setup": {"type": "object", "additionalProperties": True},
                "metadata": {"type": "object", "additionalProperties": True},
                "next_step": {"type": "string", "description": "Recommended follow-up action."},
            },
            "required": ["status", "provider", "text", "storage_key", "ai_disclosure", "consent_confirmed"],
            "additionalProperties": True,
        },
        "agency.media.publish": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["preview", "published"],
                    "description": "Whether the file copy was previewed or completed.",
                },
                "file_path": {"type": "string", "description": "Resolved local source file path."},
                "storage_key": {"type": "string", "description": "Agency storage key for the media artifact."},
                "storage_uri": {"type": "string", "description": "Internal storage URI for the artifact."},
                "media_url": {
                    "type": "string",
                    "description": "Download URL for downstream tools, workflows, or tied-application delivery.",
                },
                "filename": {"type": "string", "description": "Stored filename used for the artifact."},
                "content_type": {"type": "string", "description": "Resolved media MIME type."},
                "provider_fetchable": {
                    "type": "boolean",
                    "description": "Whether the URL is expected to be directly fetchable by external providers.",
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object", "additionalProperties": True},
            },
            "required": [
                "status",
                "file_path",
                "storage_key",
                "storage_uri",
                "media_url",
                "filename",
                "content_type",
                "provider_fetchable",
                "warnings",
                "metadata",
            ],
            "additionalProperties": True,
        },
        "agency.media.send": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["preview", "sent", "failed", "requires_context"],
                    "description": "Whether the tool previewed, sent, failed delivery, or needs API context.",
                },
                "provider": {"type": "string", "description": "Normalized connector provider key."},
                "media": {"type": "object", "additionalProperties": True},
                "destination": {"type": "object", "additionalProperties": True},
                "provider_message": {"type": "object", "additionalProperties": True},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object", "additionalProperties": True},
                "delivery": {"type": ["object", "null"], "additionalProperties": True},
                "error": {"type": ["string", "null"]},
            },
            "required": ["status", "provider", "media", "destination", "provider_message", "warnings", "metadata"],
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
        "agency.repo.inspect": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok"]},
                "repo_id": {"type": "string"},
                "repo_path": {"type": "string"},
                "branch": {"type": "string"},
                "head_commit": {"type": "string"},
                "status_short": {"type": "array", "items": {"type": "string"}},
                "recent_commits": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "tracked_file_count": {"type": "integer"},
                "untracked_file_count": {"type": "integer"},
                "scanned_files": {"type": "array", "items": {"type": "string"}},
                "todo_hits": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "query_hits": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "file_excerpts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
            "required": ["status", "repo_id", "repo_path", "branch", "head_commit"],
            "additionalProperties": True,
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


def _default_implementation_for(tool_id: str) -> ToolImplementationReference:
    mapping = {
        "agency.human.ask": ("app.tools.implementations.human_input", "request_human_input"),
        "agency.speech.listen": ("app.tools.implementations.speech", "listen_speech"),
        "agency.speech.speak": ("app.tools.implementations.speech", "speak_speech"),
        "agency.speech.continue": ("app.tools.implementations.speech", "continue_speech"),
        "agency.voice.generate": ("app.tools.implementations.voice", "generate_voice"),
        "agency.media.publish": ("app.tools.implementations.media", "publish_media"),
        "agency.media.send": ("app.tools.implementations.media", "send_media"),
        "agency.http.request": ("app.tools.implementations.http_integrations", "execute_custom_api"),
        "agency.document.markdown-to-word": ("app.tools.implementations.documents", "save_markdown_to_word"),
        "agency.file.write-text": ("app.tools.implementations.custom.files", "write_text_file"),
        "agency.repo.inspect": ("app.tools.implementations.custom.repo_inspect", "inspect_repo"),
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


def _implementation_for(tool_id: str, config: dict[str, Any]) -> ToolImplementationReference:
    implementation_config = config.get("implementation")
    if isinstance(implementation_config, dict) and implementation_config:
        return ToolImplementationReference.model_validate(implementation_config)
    return _default_implementation_for(tool_id)


def _default_security_for(tool_id: str, implementation: ToolImplementationReference) -> SecuritySettings:
    network_tools = {
        "agency.speech.listen",
        "agency.media.publish",
        "agency.media.send",
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
    external_mutation_tools = {
        "agency.media.publish",
        "agency.media.send",
        "agency.http.request",
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
    filesystem_tools = {
        "agency.document.markdown-to-word",
        "agency.media.publish",
        "agency.media.send",
        "agency.voice.generate",
        "agency.file.write-text",
        "agency.excel.write-image",
        "agency.excel.write-json",
        "agency.excel.write-text",
    }
    dangerous_tools = filesystem_tools | {
        "agency.browser.click",
        "agency.browser.select-option",
        "agency.browser.type-text",
    } | external_mutation_tools
    read_only_tools = {
        "agency.speech.listen",
        "agency.speech.speak",
        "agency.browser.open",
        "agency.browser.screenshot",
        "agency.browser.analyze-screenshot",
        "agency.browser.extract-screenshot",
        "agency.browser.scroll",
        "agency.browser.verify-content",
        "agency.browser.close",
        "agency.repo.inspect",
    }

    return SecuritySettings(
        requires_approval=tool_id in dangerous_tools or tool_id == "agency.human.ask",
        sandbox=tool_id in dangerous_tools or tool_id in browser_tools or tool_id in network_tools,
        allow_browser=tool_id in browser_tools,
        allow_filesystem=tool_id in filesystem_tools or tool_id == "agency.repo.inspect",
        allow_network=tool_id in network_tools,
        allowed_domains=[],
        allowed_paths=["local_storage", "logs", "."] if tool_id in filesystem_tools else [],
        module_allowlist=[implementation.module],
        function_allowlist=[implementation.function] if implementation.function else [],
        read_only=tool_id in read_only_tools,
        dangerous=tool_id in dangerous_tools,
        redaction_enabled=tool_id in {"agency.http.request"},
    )


def _security_for(
        tool_id: str,
        config: dict[str, Any],
        implementation: ToolImplementationReference,
) -> SecuritySettings:
    default_security = _default_security_for(tool_id, implementation).model_dump(mode="json")
    override = config.get("security")
    if not isinstance(override, dict) or not override:
        return SecuritySettings.model_validate(default_security)
    merged = {**default_security, **override}
    if not merged.get("module_allowlist"):
        merged["module_allowlist"] = [implementation.module]
    if implementation.function and not merged.get("function_allowlist"):
        merged["function_allowlist"] = [implementation.function]
    return SecuritySettings.model_validate(merged)


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
        implementation = _implementation_for(tool_id, config)
        tool_definition = ToolDefinition(
            id=config["id"],
            name=config["name"],
            display_name=config.get("display_name") or config["name"],
            description=config["description"],
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema=_input_schema_for(tool_id, parameters_metadata),
            output_schema=_output_schema_for(tool_id),
            implementation=implementation,
            security=_security_for(tool_id, config, implementation),
            mcp_exposure=MCPExposureSettings(),
            tags=list(config.get("tags") or ["catalog", "crewai"]),
            framework_hints=config.get("framework_hints") or {"preferred_adapter": "crewai"},
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

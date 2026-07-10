from __future__ import annotations

import re

from app.domain import ToolDefinition

TOOL_CALL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
ACRONYM_WORDS = {
    "a2a",
    "api",
    "cli",
    "csv",
    "docx",
    "html",
    "http",
    "json",
    "llm",
    "mcp",
    "pdf",
    "sql",
    "txt",
    "ui",
    "url",
    "xml",
    "yaml",
}
LOWERCASE_DISPLAY_WORDS = {"a", "an", "and", "as", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}


def _split_name_words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name.strip())
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [part for part in re.split(r"[^A-Za-z0-9]+", spaced) if part]


def _format_display_word(word: str) -> str:
    if word.lower() in ACRONYM_WORDS:
        return word.upper()
    return f"{word[:1].upper()}{word[1:]}"


def make_tool_call_name(tool_name: str) -> str:
    """Return an LLM/function-call safe name for a tool identifier."""
    raw = tool_name.strip()
    if TOOL_CALL_NAME_PATTERN.fullmatch(raw):
        return raw
    parts = _split_name_words(raw)
    candidate = "".join(part if part.isupper() else _format_display_word(part) for part in parts)
    if not candidate:
        candidate = "Tool"
    if not re.match(r"^[A-Za-z_]", candidate):
        candidate = f"Tool{candidate}"
    return candidate[:64]


def make_tool_display_name(name: str) -> str:
    """Return a human-facing spaced Pascal Case label for a tool name/id segment."""
    parts = _split_name_words(name)
    if not parts:
        return "Tool"
    formatted = []
    for index, part in enumerate(parts):
        lower = part.lower()
        if index > 0 and lower in LOWERCASE_DISPLAY_WORDS:
            formatted.append(lower)
        else:
            formatted.append(part if part.isupper() else _format_display_word(part))
    return " ".join(formatted)


def tool_call_name(tool: ToolDefinition) -> str:
    configured = tool.framework_hints.metadata.get("tool_call_name")
    if isinstance(configured, str) and TOOL_CALL_NAME_PATTERN.fullmatch(configured.strip()):
        return configured.strip()
    return make_tool_call_name(tool.name)


def tool_display_name(tool: ToolDefinition) -> str:
    if isinstance(tool.display_name, str) and tool.display_name.strip():
        return tool.display_name.strip()
    return make_tool_display_name(tool.name)


def tool_matches_call_name(tool: ToolDefinition, name: str | None) -> bool:
    if not name:
        return False
    normalized = name.strip()
    return normalized in {
        tool.id,
        tool.name,
        tool_display_name(tool),
        make_tool_call_name(tool_display_name(tool)),
        tool_call_name(tool),
        tool.implementation.callable_name or "",
        tool.implementation.entrypoint or "",
    }

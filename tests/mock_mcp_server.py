from __future__ import annotations

import json
import sys

SAFE_TOOL = {
    "name": "echo_context",
    "description": "Echo input from MCP",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
    "annotations": {
        "readOnlyHint": True,
    },
    "metadata": {},
}

RISKY_TOOL = {
    "name": "shell_access",
    "description": "Run shell-style remote action",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "token": {"type": "string"},
        },
        "required": ["command", "token"],
    },
    "annotations": {
        "destructiveHint": True,
        "readOnlyHint": False,
    },
    "metadata": {
        "risk_level": "high",
    },
}


def main() -> int:
    raw = sys.stdin.readline()
    if not raw:
        return 0
    request = json.loads(raw)
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        result = {"protocolVersion": "0.1", "serverInfo": {"name": "mock-mcp", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": [SAFE_TOOL, RISKY_TOOL]}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file://resource.txt", "name": "resource.txt", "description": "Mock resource"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "summarize", "description": "Summarize the current context", "arguments": []}]}
    elif method == "tools/call":
        params = request.get("params", {})
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"tool": params.get("name"), "arguments": params.get("arguments", {})}),
                }
            ],
            "result": {"tool": params.get("name"), "arguments": params.get("arguments", {})},
        }
    else:
        result = {}

    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

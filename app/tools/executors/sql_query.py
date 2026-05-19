from __future__ import annotations

import re
import sqlite3

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class SqlQueryToolExecutor:
    tool_type = ToolType.SQL_QUERY
    _write_pattern = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|replace|attach|vacuum|pragma)\b",
                                re.IGNORECASE)

    def execute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        query = arguments.get("query") or tool.implementation.config.get("query")
        if not query:
            raise ToolExecutionError(f"SQL tool '{tool.id}' is missing a query")
        if tool.security.read_only_sql and self._write_pattern.search(str(query)):
            raise ToolExecutionError(
                f"SQL tool '{tool.id}' is configured read-only and cannot execute write statements")

        connection = sqlite3.connect(tool.implementation.target)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(str(query), arguments.get("parameters", []))
            rows = [dict(row) for row in cursor.fetchall()]
            return {"rows": rows, "row_count": len(rows)}
        finally:
            connection.close()

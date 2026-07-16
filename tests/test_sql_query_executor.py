from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.domain import SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.runtime.native.errors import ToolExecutionError
from app.tools.executors.base import ToolExecutionContext
from app.tools.executors.sql_query import SqlQueryToolExecutor


class SqlQueryToolExecutorSecurityTests(unittest.TestCase):
    def test_read_only_mode_rejects_analyze_and_reindex_before_opening_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "read-only.db"
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
                connection.execute("CREATE INDEX idx_items_name ON items(name)")
                connection.execute("INSERT INTO items(name) VALUES ('example')")

            tool = ToolDefinition(
                id="read-only-sql",
                name="read_only_sql",
                description="Read-only SQLite query tool",
                tool_type=ToolType.SQL_QUERY,
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                implementation=ToolImplementationReference(
                    implementation_type="sql_query",
                    target=str(database_path),
                ),
                security=SecuritySettings(read_only_sql=True),
            )
            executor = SqlQueryToolExecutor()
            context = ToolExecutionContext(execution_id="sql-security-regression")
            original_digest = hashlib.sha256(database_path.read_bytes()).digest()

            # These SQLite maintenance commands can write even though they are not
            # conventional DML, so policy must reject them before opening the sink.
            for query in ("ANALYZE", "REINDEX idx_items_name"):
                with self.subTest(query=query):
                    with patch("app.tools.executors.sql_query.sqlite3.connect", wraps=sqlite3.connect) as connect:
                        with self.assertRaisesRegex(ToolExecutionError, "configured read-only"):
                            executor.execute(tool, {"query": query}, context)
                        connect.assert_not_called()

                    self.assertEqual(hashlib.sha256(database_path.read_bytes()).digest(), original_digest)

            with sqlite3.connect(database_path) as connection:
                statistics_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'"
                ).fetchone()
                index_definition = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'idx_items_name'"
                ).fetchone()

            self.assertIsNone(statistics_table)
            self.assertEqual(index_definition, ("CREATE INDEX idx_items_name ON items(name)",))


if __name__ == "__main__":
    unittest.main()

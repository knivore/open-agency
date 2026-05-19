from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.api.schemas import PreSignedUrlRequest
from app.db.models import (
    AgentORM,
    CredentialORM,
    EXECUTION_ARTIFACTS_COLLECTION,
    EXECUTION_EVENTS_COLLECTION,
    EXECUTIONS_COLLECTION,
    ExecutionArtifactORM,
    ExecutionEventORM,
    ExecutionORM,
    MCPServerORM,
    ScheduleORM,
    ToolORM,
    WorkflowORM,
)
from app.domain import AgentDefinition, Execution, ScheduleDefinition, ToolDefinition, WorkflowDefinition


class ModelMigrationBoundaryTests(unittest.TestCase):
    def test_domain_models_still_serialize(self):
        agent = AgentDefinition(name="Boundary Agent")
        tool = ToolDefinition(
            name="Boundary Tool",
            description="Test tool",
            input_schema={"type": "object", "properties": {}},
            implementation={"implementation_type": "python_function", "target": "tests.native_test_tools",
                            "callable_name": "echo_tool"},
        )
        workflow = WorkflowDefinition(name="Boundary Workflow", entrypoint="node-1")
        execution = Execution(workflow_id="wf-1", runtime_adapter_id="native")
        schedule = ScheduleDefinition(name="Boundary Schedule", workflow_id="wf-1")

        self.assertEqual(agent.model_dump(mode="json")["name"], "Boundary Agent")
        self.assertEqual(tool.model_dump(mode="json")["tool_type"], "python_function")
        self.assertEqual(workflow.model_dump(mode="json")["entrypoint"], "node-1")
        self.assertEqual(execution.model_dump(mode="json")["runtime_adapter_id"], "native")
        self.assertEqual(schedule.model_dump(mode="json")["workflow_id"], "wf-1")

    def test_api_schemas_are_importable(self):
        request = PreSignedUrlRequest(filename="demo.txt", operation="upload")

        self.assertEqual(request.filename, "demo.txt")

    def test_orm_model_modules_are_importable(self):
        orm_classes = [
            AgentORM,
            CredentialORM,
            ExecutionORM,
            ExecutionArtifactORM,
            ExecutionEventORM,
            MCPServerORM,
            ScheduleORM,
            ToolORM,
            WorkflowORM,
        ]
        for cls in orm_classes:
            self.assertTrue(cls.__tablename__)

        self.assertEqual(EXECUTIONS_COLLECTION.name, "executions")
        self.assertEqual(EXECUTION_EVENTS_COLLECTION.name, "execution_events")
        self.assertEqual(EXECUTION_ARTIFACTS_COLLECTION.name, "execution_artifacts")

    def test_app_package_has_no_direct_root_models_imports(self):
        app_root = Path(__file__).resolve().parents[1] / "app"
        offending: list[str] = []

        for path in app_root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (
                        isinstance(node, ast.ImportFrom)
                        and node.level == 0
                        and node.module
                        and node.module.startswith("models")
                ):
                    offending.append(f"{path}:{node.lineno}:{node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "models" or alias.name.startswith("models."):
                            offending.append(f"{path}:{node.lineno}:{alias.name}")

        self.assertEqual(offending, [])

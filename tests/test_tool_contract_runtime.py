from __future__ import annotations

import os
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    Conversation,
    ConversationChannelType,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    ModelProfileDefinition,
    UserDefinition,
    WorkflowDefinition,
)
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService
from app.tools.cli_discovery import list_builtin_tool_definitions
from app.tools.contracts import ToolContractRegistry, get_default_contract_registry, load_contracts
from app.tools.contracts.validator import ToolContractValidationError, validate_tool_input, validate_tool_output
from app.tools.policies import PolicyEngine
from app.tools.runtime import JsonlToolRunStore, ToolRuntimeExecutor, build_dry_run_pr_payload
from app.runtime.streaming import RuntimeEventBus, set_default_runtime_event_bus


README_PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-hello
+hello world
"""


def _create_workbook(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"
    workbook.save(path)
    return path


def _workflow_definition(workflow_id: str = "workflow-contract") -> WorkflowDefinition:
    return WorkflowDefinition(
        id=workflow_id,
        name="Contract Workflow",
        description="Workflow visible to contract runtime tests.",
        entrypoint="node-1",
        metadata={"inputs": ["topic"]},
    )


async def _prepare_conversation_context(conversation_id: str = "conversation-contract"):
    context = create_test_api_context()
    await context.model_profile_repo.save(
        ModelProfileDefinition(id="profile-contract", name="Contract Model", provider="fake", model="fake-model")
    )
    await MainAgentSetupService(context).create_main_agent(
        MainAgentSetupConfig(
            agent_name="Main Agent",
            agent_description="Configured for contract runtime tests.",
            agent_instructions="Answer briefly.",
            model_profile_id="profile-contract",
            profile_id="main-agent-profile",
            agent_id="main-agent",
            workflow_id="main-workflow",
        )
    )
    await context.conversation_repo.create(
        Conversation(
            id=conversation_id,
            created_by_user_id="user-contract",
            channel_type=ConversationChannelType.API,
        )
    )
    return context


def _workflow_payload(workflow_id: str = "workflow-contract-proposal") -> dict:
    return {
        "id": workflow_id,
        "name": "Contract Proposal Workflow",
        "description": "Workflow proposed through the contract runtime.",
        "entrypoint": "node-1",
        "nodes": [
            {
                "id": "node-1",
                "name": "Entry",
                "node_type": "task",
                "task_id": "task-1",
                "config": {},
                "metadata": {},
            }
        ],
        "task_definitions": [
            {
                "id": "task-1",
                "name": "Task One",
                "description": "Do the work",
                "tool_ids": [],
                "depends_on_task_ids": [],
                "input_schema": {},
                "output_schema": {},
                "human_approval_required": False,
                "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                "metadata": {},
            }
        ],
        "versioning": {
            "version": "1.0.0",
            "revision": 1,
            "parent_version": None,
            "is_published": False,
            "labels": [],
        },
        "metadata": {
            "visible_to_main_agent": True,
            "mutable_by_main_agent": True,
        },
    }


class ToolContractRuntimeTests(unittest.TestCase):
    def test_sandbox_edit_contract_loads_and_validates_payloads(self):
        contracts = load_contracts()
        registry = ToolContractRegistry(contracts)
        contract = registry.get_contract("sandbox-edit")
        http_contract = registry.get_contract("agency.http.request")
        workflow_list_contract = registry.get_contract("agency.workflow.list")
        workflow_get_contract = registry.get_contract("agency.workflow.get")
        execution_get_contract = registry.get_contract("agency.execution.get")
        execution_events_contract = registry.get_contract("agency.execution.events")
        execution_artifacts_contract = registry.get_contract("agency.execution.artifacts")
        tool_get_contract = registry.get_contract("agency.tool.get")
        workflow_propose_create_contract = registry.get_contract("agency.workflow.propose-create")
        workflow_propose_update_contract = registry.get_contract("agency.workflow.propose-update")
        tool_propose_create_contract = registry.get_contract("agency.tool.propose-create")
        tool_propose_update_contract = registry.get_contract("agency.tool.propose-update")
        memory_list_contract = registry.get_contract("agency.memory.list")
        memory_remember_contract = registry.get_contract("agency.memory.remember")
        memory_update_contract = registry.get_contract("agency.memory.update")
        memory_delete_contract = registry.get_contract("agency.memory.delete")
        tool_list_contract = registry.get_contract("agency.tool.list")
        command_run_contract = registry.get_contract("agency.command.run")
        file_write_contract = registry.get_contract("agency.file.write-text")
        document_contract = registry.get_contract("agency.document.markdown-to-word")
        excel_text_contract = registry.get_contract("agency.excel.write-text")
        excel_json_contract = registry.get_contract("agency.excel.write-json")
        excel_image_contract = registry.get_contract("agency.excel.write-image")

        self.assertIsNotNone(contract)
        self.assertEqual(contract.name, "sandbox-edit")
        self.assertIsNotNone(http_contract)
        self.assertEqual(http_contract.name, "agency.http.request")
        self.assertIsNotNone(workflow_list_contract)
        self.assertEqual(workflow_list_contract.name, "agency.workflow.list")
        self.assertIsNotNone(workflow_get_contract)
        self.assertEqual(workflow_get_contract.name, "agency.workflow.get")
        self.assertIsNotNone(execution_get_contract)
        self.assertEqual(execution_get_contract.name, "agency.execution.get")
        self.assertIsNotNone(execution_events_contract)
        self.assertEqual(execution_events_contract.name, "agency.execution.events")
        self.assertIsNotNone(execution_artifacts_contract)
        self.assertEqual(execution_artifacts_contract.name, "agency.execution.artifacts")
        self.assertIsNotNone(tool_get_contract)
        self.assertEqual(tool_get_contract.name, "agency.tool.get")
        self.assertIsNotNone(workflow_propose_create_contract)
        self.assertEqual(workflow_propose_create_contract.name, "agency.workflow.propose-create")
        self.assertIsNotNone(workflow_propose_update_contract)
        self.assertEqual(workflow_propose_update_contract.name, "agency.workflow.propose-update")
        self.assertIsNotNone(tool_propose_create_contract)
        self.assertEqual(tool_propose_create_contract.name, "agency.tool.propose-create")
        self.assertIsNotNone(tool_propose_update_contract)
        self.assertEqual(tool_propose_update_contract.name, "agency.tool.propose-update")
        self.assertIsNotNone(memory_list_contract)
        self.assertEqual(memory_list_contract.name, "agency.memory.list")
        self.assertIsNotNone(memory_remember_contract)
        self.assertEqual(memory_remember_contract.name, "agency.memory.remember")
        self.assertIsNotNone(memory_update_contract)
        self.assertEqual(memory_update_contract.name, "agency.memory.update")
        self.assertIsNotNone(memory_delete_contract)
        self.assertEqual(memory_delete_contract.name, "agency.memory.delete")
        self.assertIsNotNone(tool_list_contract)
        self.assertEqual(tool_list_contract.name, "agency.tool.list")
        self.assertIsNotNone(command_run_contract)
        self.assertEqual(command_run_contract.name, "agency.command.run")
        self.assertIsNotNone(file_write_contract)
        self.assertEqual(file_write_contract.name, "agency.file.write-text")
        self.assertIsNotNone(document_contract)
        self.assertEqual(document_contract.name, "agency.document.markdown-to-word")
        self.assertIsNotNone(excel_text_contract)
        self.assertEqual(excel_text_contract.name, "agency.excel.write-text")
        self.assertIsNotNone(excel_json_contract)
        self.assertEqual(excel_json_contract.name, "agency.excel.write-json")
        self.assertIsNotNone(excel_image_contract)
        self.assertEqual(excel_image_contract.name, "agency.excel.write-image")
        static_contract_names = {item.name for item in contracts}
        self.assertIn("agency.workflow.run", static_contract_names)
        self.assertIn("agency.human.ask", static_contract_names)
        self.assertIn("agency.browser.open", static_contract_names)
        self.assertIn("agency.browser.click", static_contract_names)
        validate_tool_input(
            contract,
            {
                "repo": "/tmp/example",
                "ref": "main",
                "changes": [{"path": "README.md", "patch": README_PATCH}],
                "dryRun": True,
            },
        )
        with self.assertRaises(ToolContractValidationError):
            validate_tool_input(contract, {"repo": "/tmp/example"})
        with self.assertRaises(ToolContractValidationError):
            validate_tool_output(contract, {"verdict": "maybe", "dryRun": True, "timestamp": "now"})

    def test_default_contract_registry_covers_every_builtin_tool(self):
        contracts = {contract.name for contract in get_default_contract_registry().list_contracts()}
        builtins = list_builtin_tool_definitions()
        missing = [tool.id for tool in builtins if tool.id not in contracts]

        self.assertEqual(len(builtins), 35)
        self.assertEqual(missing, [])
        self.assertIn("agency.audio.transcribe", contracts)
        self.assertIn("sandbox-edit", contracts)

    def test_policy_denies_unallowlisted_repos_dangerous_paths_and_high_risk_secrets(self):
        verdict = PolicyEngine(allowed_repos=[]).evaluate(
            "sandbox-edit",
            {
                "repo": "/tmp/not-allowed",
                "ref": "main",
                "changes": [
                    {"path": "src/.env.local", "patch": "+OPENAI_API_KEY=sk-test"},
                    {"path": ".git/config", "patch": "+[remote]\n"},
                ],
            },
        )

        self.assertEqual(verdict.outcome, "deny")
        rule_ids = {rule.id for rule in verdict.rules if rule.outcome == "deny"}
        self.assertIn("repo-allowlist", rule_ids)
        self.assertIn("no-dangerous-paths", rule_ids)
        self.assertIn("no-secrets", rule_ids)

    def test_runtime_returns_signed_structured_dry_run_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")

            store = JsonlToolRunStore(repo / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(policy_engine=PolicyEngine(allowed_repos=[str(repo)]), run_store=store)
            response = executor.run(
                "sandbox-edit",
                {
                    "repo": str(repo),
                    "ref": "main",
                    "changes": [{"path": "README.md", "patch": README_PATCH}],
                    "dryRun": True,
                },
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.errors, [])
            self.assertTrue(response.signature.startswith("sha256:"))
            self.assertEqual(response.filesChanged[0].path, "README.md")
            self.assertEqual(response.patch, README_PATCH)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "hello\n")
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].tool_name, "sandbox-edit")
            self.assertEqual(records[0].tool_version, "1.0")
            self.assertEqual(records[0].actor, "user-runtime")
            self.assertEqual(records[0].verdict, "ok")
            self.assertTrue(records[0].input_hash.startswith("sha256:"))
            self.assertTrue(records[0].output_hash.startswith("sha256:"))

    def test_runtime_runs_contract_backed_tool_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(run_store=store)
            response = executor.run("agency.tool.list", {}, actor="user-runtime")

            self.assertEqual(response.verdict, "ok")
            self.assertIsNotNone(response.policyVerdict)
            self.assertIsNotNone(response.result)
            assert response.result is not None
            self.assertEqual(response.result["count"], 35)
            tool_ids = {item["id"] for item in response.result["items"]}
            self.assertIn("agency.audio.transcribe", tool_ids)
            self.assertIn("agency.tool.list", tool_ids)
            self.assertIn("agency.command.run", tool_ids)
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].tool_name, "agency.tool.list")

    @patch("app.tools.runtime.executor.execute_custom_api")
    def test_runtime_runs_contract_backed_http_request(self, mock_execute):
        mock_execute.return_value = {"status_code": 200, "response": {"ok": True}}
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_http_hosts=["api.example.test"]),
                run_store=store,
            )
            response = executor.run(
                "agency.http.request",
                {"url": "https://api.example.test/items", "method": "GET", "verify_ssl": True},
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status_code"], 200)
            self.assertEqual(response.result["response"], {"ok": True})
            self.assertEqual(response.result["method"], "GET")
            self.assertTrue(response.signature.startswith("sha256:"))
            self.assertEqual(store.list_records()[0].tool_name, "agency.http.request")
            mock_execute.assert_called_once()

    def test_runtime_denies_contract_backed_http_request_disallowed_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_http_hosts=["api.example.test"]),
                run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"),
            )
            response = executor.run(
                "agency.http.request",
                {"url": "https://blocked.example.test/items", "method": "GET"},
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "deny")
            self.assertIsNone(response.result)
            self.assertIn("host is not allowlisted", response.errors[0])

    def test_runtime_runs_contract_backed_tool_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            response = executor.run("agency.tool.get", {"tool_id": "agency.http.request"}, actor="user-runtime")

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "ok")
            self.assertEqual(response.result["tool"]["id"], "agency.http.request")

    def test_runtime_runs_context_backed_workflow_list_and_get(self):
        async def run_assertions():
            with tempfile.TemporaryDirectory() as tmp:
                context = create_test_api_context()
                workflow = await context.workflow_repo.create(_workflow_definition())
                executor = ToolRuntimeExecutor(
                    context=context,
                    run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"),
                )

                list_response = await executor.run_async("agency.workflow.list", {}, actor="user-runtime")
                get_response = await executor.run_async(
                    "agency.workflow.get",
                    {"workflow_id": workflow.id},
                    actor="user-runtime",
                )

                self.assertEqual(list_response.verdict, "ok")
                self.assertEqual(list_response.result["status"], "ok")
                self.assertEqual(list_response.result["workflows"][0]["id"], workflow.id)
                self.assertEqual(get_response.verdict, "ok")
                self.assertEqual(get_response.result["workflow"]["id"], workflow.id)
                self.assertEqual(get_response.result["summary"]["input_keys"], ["topic"])

        asyncio.run(run_assertions())

    def test_runtime_runs_context_backed_memory_crud(self):
        async def run_assertions():
            with tempfile.TemporaryDirectory() as tmp:
                context = create_test_api_context()
                await context.user_repo.create(UserDefinition(id="user-memory", email="memory@example.com"))
                executor = ToolRuntimeExecutor(
                    context=context,
                    run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"),
                )

                remember = await executor.run_async(
                    "agency.memory.remember",
                    {
                        "scope": "user",
                        "content": "Use concise contract updates.",
                        "summary": "Prefers concise updates.",
                        "tags": ["preference"],
                    },
                    actor="user-memory",
                )
                memory_id = remember.result["memory"]["id"]
                listed = await executor.run_async("agency.memory.list", {"scope": "user", "query": "concise"}, actor="user-memory")
                updated = await executor.run_async(
                    "agency.memory.update",
                    {"memory_id": memory_id, "summary": "Prefers concise engineering updates."},
                    actor="user-memory",
                )
                deleted = await executor.run_async("agency.memory.delete", {"memory_id": memory_id}, actor="user-memory")

                self.assertEqual(remember.verdict, "ok")
                self.assertEqual(remember.result["memory"]["created_by_user_id"], "user-memory")
                self.assertEqual(listed.result["memories"][0]["id"], memory_id)
                self.assertEqual(updated.result["memory"]["summary"], "Prefers concise engineering updates.")
                self.assertTrue(deleted.result["deleted"])

        asyncio.run(run_assertions())

    def test_runtime_marks_direct_proposal_tools_as_conversation_context_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            response = executor.run(
                "agency.workflow.propose-create",
                {"goal": "Create a workflow that drafts a report."},
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "warn")
            self.assertEqual(response.result["status"], "requires_conversation_context")
            self.assertIn("requires conversation/profile/origin-message approval context", response.errors[0])

    @patch("app.tools.runtime.executor.open_browser")
    def test_runtime_runs_contract_backed_browser_open(self, mock_open_browser):
        mock_open_browser.return_value = {
            "url": "https://example.test",
            "title": "Example",
            "runtime_root": "/tmp/browser-runtime",
            "message": "Browser started.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(run_store=store)
            response = executor.run(
                "agency.browser.open",
                {"url": "https://example.test"},
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "ok")
            self.assertEqual(response.result["output"]["title"], "Example")
            self.assertTrue(response.signature.startswith("sha256:"))
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].tool_name, "agency.browser.open")
            mock_open_browser.assert_called_once()

    @patch("app.tools.runtime.executor.click_element")
    def test_runtime_runs_contract_backed_browser_mutation_with_policy_warning(self, mock_click):
        mock_click.return_value = "Clicked element matching instruction: Submit"
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            response = executor.run("agency.browser.click", {"instruction": "Submit"}, actor="user-runtime")

            self.assertEqual(response.verdict, "warn")
            self.assertEqual(response.result["status"], "ok")
            self.assertIn("browser mutation", response.policyVerdict.rules[1].reason)

    def test_runtime_routes_all_browser_tools_through_contract_executor(self):
        browser_cases = [
            ("agency.browser.screenshot", {}, "screenshot", "screenshot ok"),
            ("agency.browser.analyze-screenshot", {"text": "Analyze the page"}, "screenshot_and_analyse", "analysis ok"),
            (
                "agency.browser.extract-screenshot",
                {"text": "Extract content"},
                "screenshot_and_extract",
                {"page_type": "generic", "page_url": "https://example.test", "content": {"summary": "ok", "text": "ok"}},
            ),
            ("agency.browser.scroll", {"scroll_direction": "down 1"}, "scroll_page", "scroll ok"),
            ("agency.browser.select-option", {"instruction": "Select 'A'"}, "select_dropdown", "select ok"),
            ("agency.browser.type-text", {"instruction": "Type 'hello'"}, "send_keys", "type ok"),
            (
                "agency.browser.verify-content",
                {"text": "hello"},
                "verify_content",
                {"Verification Reasoning": "found", "Verification Score": 100, "Challenge Detected": False},
            ),
            ("agency.browser.close", {}, "terminate_browser", {"Success Message": "closed"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            for tool_name, payload, handler_name, raw_result in browser_cases:
                with self.subTest(tool_name=tool_name), patch(f"app.tools.runtime.executor.{handler_name}") as mock_handler:
                    mock_handler.return_value = raw_result
                    response = executor.run(tool_name, payload, actor="approved/user-runtime")

                    self.assertEqual(response.verdict, "ok")
                    self.assertEqual(response.result["status"], "ok")
                    self.assertEqual(response.result["output"], raw_result)

    @patch("app.tools.runtime.executor.request_human_input")
    def test_runtime_runs_contract_backed_human_ask(self, mock_request_human_input):
        mock_request_human_input.return_value = {"status": "received", "response": "Proceed"}
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            response = executor.run(
                "agency.human.ask",
                {"query": "Should I proceed?", "timeout_seconds": 1},
                actor="user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["response"], "Proceed")
            mock_request_human_input.assert_called_once_with("Should I proceed?", process_id=None, timeout=1)

    def test_runtime_requires_confirmation_for_sensitive_memory(self):
        async def run_assertions():
            with tempfile.TemporaryDirectory() as tmp:
                context = create_test_api_context()
                await context.user_repo.create(UserDefinition(id="user-memory", email="memory@example.com"))
                executor = ToolRuntimeExecutor(
                    context=context,
                    run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"),
                )

                response = await executor.run_async(
                    "agency.memory.remember",
                    {
                        "scope": "user",
                        "content": "My password is example",
                        "sensitive": True,
                        "confirmed": False,
                    },
                    actor="user-memory",
                )

                self.assertEqual(response.verdict, "warn")
                self.assertEqual(response.result["status"], "error")
                self.assertIn("Sensitive memory writes require explicit user confirmation", response.errors[0])

        asyncio.run(run_assertions())

    def test_runtime_runs_contract_backed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(run_store=store)
            response = executor.run(
                "agency.command.run",
                {"command": "printf 'contract-command\\n'", "mode": "bash", "timeout_seconds": 2},
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertIsNotNone(response.result)
            assert response.result is not None
            self.assertEqual(response.result["status"], "ok")
            self.assertEqual(response.result["exit_code"], 0)
            self.assertEqual(response.result["stdout"], "contract-command")
            self.assertTrue(response.signature.startswith("sha256:"))
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].tool_name, "agency.command.run")

    def test_runtime_denies_blocked_contract_backed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(run_store=store)
            response = executor.run(
                "agency.command.run",
                {"command": "git push origin main", "mode": "bash"},
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "deny")
            self.assertIsNone(response.result)
            self.assertTrue(response.errors)
            self.assertIn("git push is blocked", response.errors[0])
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].verdict, "deny")

    def test_runtime_runs_contract_backed_file_write_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonlToolRunStore(root / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root)]),
                run_store=store,
            )
            response = executor.run(
                "agency.file.write-text",
                {
                    "base_folder": str(root),
                    "filename": "notes/out.txt",
                    "content": "contract file\n",
                    "mode": "write",
                },
                actor="approved/user-runtime",
            )

            written = root / "notes" / "out.txt"
            self.assertEqual(response.verdict, "ok")
            self.assertEqual(written.read_text(encoding="utf-8"), "contract file\n")
            self.assertIsNotNone(response.result)
            assert response.result is not None
            self.assertEqual(response.result["status"], "success")
            self.assertEqual(response.filesChanged[0].path, str(written.resolve()))
            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].tool_name, "agency.file.write-text")

    def test_runtime_denies_contract_backed_file_write_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonlToolRunStore(root / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root / "allowed")]),
                run_store=store,
            )
            response = executor.run(
                "agency.file.write-text",
                {
                    "base_folder": str(root / "blocked"),
                    "filename": "out.txt",
                    "content": "blocked",
                    "mode": "write",
                },
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "deny")
            self.assertIsNone(response.result)
            self.assertFalse((root / "blocked" / "out.txt").exists())
            self.assertIn("not under an allowlisted directory", response.errors[0])

    @patch("app.tools.implementations.documents.upload_to_s3")
    def test_runtime_runs_contract_backed_markdown_to_word(self, mock_upload):
        mock_upload.return_value = {"uploaded_files": ["user_approved-user/workflow_reports/run_proc-1/report.docx"]}
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(run_store=store)
            response = executor.run(
                "agency.document.markdown-to-word",
                {
                    "markdown_text": "# Report\n\nBody",
                    "filename": "report.docx",
                    "img_directory": "reports",
                    "process_id": "proc-1",
                    "run_by": "approved-user",
                },
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertIsNotNone(response.result)
            assert response.result is not None
            self.assertEqual(response.result["status"], "success")
            self.assertEqual(response.result["storage_uri"], "s3://mybucket/user_approved-user/workflow_reports/run_proc-1/report.docx")
            self.assertTrue(response.signature.startswith("sha256:"))
            self.assertEqual(store.list_records()[0].tool_name, "agency.document.markdown-to-word")

    def test_runtime_denies_contract_backed_markdown_to_word_unsafe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"))
            response = executor.run(
                "agency.document.markdown-to-word",
                {
                    "markdown_text": "# Report",
                    "filename": "../report.docx",
                    "img_directory": "reports",
                },
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "deny")
            self.assertIsNone(response.result)
            self.assertIn("filename must be a safe document name", response.errors[0])

    def test_runtime_runs_contract_backed_excel_text_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = _create_workbook(root / "results.xlsx")
            text_path = root / "result.txt"
            text_path.write_text("contract spreadsheet", encoding="utf-8")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root)]),
                run_store=JsonlToolRunStore(root / "tool_runs.jsonl"),
            )

            response = executor.run(
                "agency.excel.write-text",
                {
                    "sheet_name": "Sheet1",
                    "excel_file_path": str(workbook_path),
                    "text_file_path": str(text_path),
                    "serial_number": 1,
                    "header_title": "Notes",
                },
                actor="approved/user-runtime",
            )

            worksheet = load_workbook(workbook_path).active
            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "success")
            self.assertEqual(worksheet["A1"].value, "Notes")
            self.assertEqual(worksheet["A2"].value, "contract spreadsheet")
            self.assertEqual(response.filesChanged[0].path, str(workbook_path))

    def test_runtime_runs_contract_backed_excel_json_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = _create_workbook(root / "results.xlsx")
            json_path = root / "result.json"
            json_path.write_text(json.dumps({"Summary": "OK"}), encoding="utf-8")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root)]),
                run_store=JsonlToolRunStore(root / "tool_runs.jsonl"),
            )

            response = executor.run(
                "agency.excel.write-json",
                {
                    "sheet_name": "Sheet1",
                    "excel_file_path": str(workbook_path),
                    "json_file_path": str(json_path),
                    "serial_number": 1,
                },
                actor="approved/user-runtime",
            )

            worksheet = load_workbook(workbook_path).active
            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "success")
            self.assertEqual(worksheet["A1"].value, "Summary")
            self.assertEqual(worksheet["A2"].value, "OK")

    def test_runtime_runs_contract_backed_excel_image_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = _create_workbook(root / "results.xlsx")
            image_path = root / "sample.png"
            Image.new("RGB", (20, 20), color="red").save(image_path)
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root)]),
                run_store=JsonlToolRunStore(root / "tool_runs.jsonl"),
            )

            response = executor.run(
                "agency.excel.write-image",
                {
                    "sheet_name": "Sheet1",
                    "excel_file_path": str(workbook_path),
                    "image_path": str(image_path),
                    "serial_number": 1,
                    "header_title": "Evidence",
                },
                actor="approved/user-runtime",
            )

            worksheet = load_workbook(workbook_path).active
            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "success")
            self.assertEqual(worksheet["A1"].value, "Evidence")

    def test_runtime_denies_contract_backed_excel_writer_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = _create_workbook(root / "blocked" / "results.xlsx")
            text_path = root / "result.txt"
            text_path.write_text("blocked", encoding="utf-8")
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_file_write_dirs=[str(root / "allowed")]),
                run_store=JsonlToolRunStore(root / "tool_runs.jsonl"),
            )

            response = executor.run(
                "agency.excel.write-text",
                {
                    "sheet_name": "Sheet1",
                    "excel_file_path": str(workbook_path),
                    "text_file_path": str(text_path),
                    "serial_number": 1,
                },
                actor="approved/user-runtime",
            )

            self.assertEqual(response.verdict, "deny")
            self.assertIsNone(response.result)
            self.assertIn("not under an allowlisted directory", response.errors[0])

    def test_runtime_denies_before_patch_validation_when_policy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolRuntimeExecutor(
                policy_engine=PolicyEngine(allowed_repos=[]),
                run_store=JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl"),
            )
            response = executor.run(
                "sandbox-edit",
                {
                    "repo": "/tmp/not-allowed",
                    "ref": "main",
                    "changes": [{"path": "README.md", "patch": "not a patch"}],
                    "dryRun": True,
                },
            )

        self.assertEqual(response.verdict, "deny")
        self.assertIsNone(response.patch)
        self.assertTrue(response.errors)

    def test_api_exposes_contracts_and_runtime_validation(self):
        client = TestClient(create_app(context=create_test_api_context()))

        list_response = client.get("/tools/contracts")
        self.assertEqual(list_response.status_code, 200)
        self.assertIn("sandbox-edit", {item["name"] for item in list_response.json()["items"]})
        self.assertIn("agency.tool.list", {item["name"] for item in list_response.json()["items"]})

        get_response = client.get("/tools/contracts/sandbox-edit")
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["name"], "sandbox-edit")

        invalid_run = client.post("/tools/sandbox-edit/run", json={"repo": "/tmp/example"})
        self.assertEqual(invalid_run.status_code, 400)
        self.assertIn("validation failed", invalid_run.json()["detail"])

        capabilities = client.get("/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        body = capabilities.json()
        self.assertEqual(body["name"], "agency-runtime")
        self.assertEqual(
            [tool.id for tool in list_builtin_tool_definitions() if tool.id not in {item["name"] for item in body["tools"]}],
            [],
        )
        self.assertIn("sandbox-edit", {item["name"] for item in body["tools"]})
        self.assertIn("agency.http.request", {item["name"] for item in body["tools"]})
        self.assertIn("agency.workflow.list", {item["name"] for item in body["tools"]})
        self.assertIn("agency.workflow.get", {item["name"] for item in body["tools"]})
        self.assertIn("agency.workflow.propose-create", {item["name"] for item in body["tools"]})
        self.assertIn("agency.workflow.propose-update", {item["name"] for item in body["tools"]})
        self.assertIn("agency.tool.get", {item["name"] for item in body["tools"]})
        self.assertIn("agency.tool.propose-create", {item["name"] for item in body["tools"]})
        self.assertIn("agency.tool.propose-update", {item["name"] for item in body["tools"]})
        self.assertIn("agency.memory.list", {item["name"] for item in body["tools"]})
        self.assertIn("agency.memory.remember", {item["name"] for item in body["tools"]})
        self.assertIn("agency.memory.update", {item["name"] for item in body["tools"]})
        self.assertIn("agency.memory.delete", {item["name"] for item in body["tools"]})
        self.assertIn("agency.tool.list", {item["name"] for item in body["tools"]})
        self.assertIn("agency.command.run", {item["name"] for item in body["tools"]})
        self.assertIn("agency.file.write-text", {item["name"] for item in body["tools"]})
        self.assertIn("agency.document.markdown-to-word", {item["name"] for item in body["tools"]})
        self.assertIn("agency.excel.write-text", {item["name"] for item in body["tools"]})
        self.assertIn("agency.excel.write-json", {item["name"] for item in body["tools"]})
        self.assertIn("agency.excel.write-image", {item["name"] for item in body["tools"]})
        capability_by_name = {item["name"]: item for item in body["tools"]}
        workflow_run_execution = capability_by_name["agency.workflow.run"]["execution"]
        self.assertEqual(workflow_run_execution["executionMode"], "approval_context")
        self.assertTrue(workflow_run_execution["supportsApprovalRequest"])
        self.assertIn("conversation_id", workflow_run_execution["inputContextFields"])
        self.assertIn("workflow_execution", workflow_run_execution["sideEffects"])
        proposal_execution = capability_by_name["agency.workflow.propose-create"]["execution"]
        self.assertEqual(proposal_execution["executionMode"], "conversation_context")
        self.assertTrue(proposal_execution["requiresConversation"])
        self.assertIn("approval_request", proposal_execution["sideEffects"])
        browser_execution = capability_by_name["agency.browser.click"]["execution"]
        self.assertEqual(browser_execution["executionMode"], "direct")
        self.assertIn("browser_mutation", browser_execution["sideEffects"])
        self.assertTrue(browser_execution["policyNotes"])
        memory_execution = capability_by_name["agency.memory.remember"]["execution"]
        self.assertEqual(memory_execution["executionMode"], "api_context")
        self.assertIn("memory", memory_execution["sideEffects"])
        self.assertEqual(body["events"]["streamUrl"], "/api/runtime/events/stream")

    def test_api_runs_contract_backed_tool_list(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post("/tools/agency.tool.list/run", json={})

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertEqual(body["policyVerdict"]["score"], 0)
                self.assertEqual(body["result"]["count"], 35)
                self.assertIn("agency.audio.transcribe", {item["id"] for item in body["result"]["items"]})
                self.assertIn("agency.tool.list", {item["id"] for item in body["result"]["items"]})
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    @patch("app.tools.runtime.executor.open_browser")
    def test_api_runs_contract_backed_browser_open(self, mock_open_browser):
        mock_open_browser.return_value = {
            "url": "https://example.test",
            "title": "Example",
            "runtime_root": "/tmp/browser-runtime",
            "message": "Browser started.",
        }
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post("/tools/agency.browser.open/run", json={"url": "https://example.test"})

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertEqual(body["result"]["status"], "ok")
                self.assertEqual(body["result"]["output"]["title"], "Example")
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_runs_contract_backed_workflow_run(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = create_test_api_context()
                asyncio.run(context.workflow_repo.create(_workflow_definition("workflow-contract-run")))
                client = TestClient(create_app(context=context))
                response = client.post(
                    "/tools/agency.workflow.run/run",
                    json={"workflow_id": "workflow-contract-run", "input_payload": {"topic": "launch"}},
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertEqual(body["result"]["workflow_id"], "workflow-contract-run")
                self.assertEqual(body["result"]["execution_status"], "queued")
                self.assertTrue(body["result"]["execution_id"])
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_marks_protected_workflow_run_as_approval_context_required(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = create_test_api_context()
                workflow = _workflow_definition("workflow-protected-contract-run").model_copy(
                    update={"metadata": {"protected_execution": True}}
                )
                asyncio.run(context.workflow_repo.create(workflow))
                client = TestClient(create_app(context=context))
                response = client.post(
                    "/tools/agency.workflow.run/run",
                    json={"workflow_id": "workflow-protected-contract-run"},
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "requires_approval_context")
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_requests_protected_workflow_approval_with_conversation_context(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = asyncio.run(_prepare_conversation_context("conversation-contract-run"))
                workflow = _workflow_definition("workflow-protected-approval-contract").model_copy(
                    update={"metadata": {"protected_execution": True}}
                )
                asyncio.run(context.workflow_repo.create(workflow))
                client = TestClient(create_app(context=context))
                response = client.post(
                    "/tools/agency.workflow.run/run",
                    json={
                        "workflow_id": "workflow-protected-approval-contract",
                        "input_payload": {"topic": "approval"},
                        "conversation_id": "conversation-contract-run",
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertEqual(body["result"]["status"], "approval_requested")
                self.assertEqual(body["result"]["approval_request"]["approval_type"], "workflow_execution")
                self.assertEqual(body["result"]["approval_request"]["target_id"], "workflow-protected-approval-contract")
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_creates_workflow_proposal_approval_with_conversation_context(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = asyncio.run(_prepare_conversation_context("conversation-contract-proposal"))
                client = TestClient(create_app(context=context))
                response = client.post(
                    "/tools/agency.workflow.propose-create/run",
                    json={
                        "summary": "Create contract proposal workflow.",
                        "workflow": _workflow_payload("workflow-contract-proposal"),
                        "conversation_id": "conversation-contract-proposal",
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertEqual(body["result"]["status"], "approval_requested")
                self.assertEqual(body["result"]["approval_request"]["approval_type"], "workflow_create")
                self.assertEqual(body["result"]["approval_request"]["target_id"], "workflow-contract-proposal")
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    @patch("app.tools.runtime.executor.execute_custom_api")
    def test_api_runs_contract_backed_http_request(self, mock_execute):
        mock_execute.return_value = {"status_code": 201, "response": {"created": True}}
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        previous_allowed_hosts = os.environ.get("TOOL_HTTP_ALLOWED_HOSTS")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                os.environ["TOOL_HTTP_ALLOWED_HOSTS"] = "api.example.test"
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.http.request/run",
                    json={"url": "https://api.example.test/items", "method": "POST", "body": {"name": "item"}},
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status_code"], 201)
                self.assertEqual(body["result"]["response"], {"created": True})
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            if previous_allowed_hosts is None:
                os.environ.pop("TOOL_HTTP_ALLOWED_HOSTS", None)
            else:
                os.environ["TOOL_HTTP_ALLOWED_HOSTS"] = previous_allowed_hosts
            reset_settings_cache()

    def test_api_runs_context_backed_workflow_list_get_and_tool_get(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = create_test_api_context()
                workflow = asyncio.run(context.workflow_repo.create(_workflow_definition("workflow-api")))
                execution = Execution(
                    id="execution-api-eval",
                    workflow_id=workflow.id,
                    runtime_adapter_id="native",
                    status=ExecutionStatus.COMPLETED,
                    input_payload={"topic": "contracts"},
                    output_payload={"final_output": "done"},
                )
                asyncio.run(context.execution_store.save_execution(execution))
                asyncio.run(
                    context.execution_store.save_event(
                        ExecutionEvent(
                            execution_id=execution.id,
                            workflow_id=workflow.id,
                            event_type=ExecutionEventType.EXECUTION_COMPLETED,
                            sequence=1,
                            payload={"output": execution.output_payload},
                        )
                    )
                )
                asyncio.run(
                    context.execution_store.save_artifact(
                        ExecutionArtifact(
                            id="artifact-api-eval",
                            execution_id=execution.id,
                            artifact_type="text",
                            name="result.txt",
                            content_text="artifact body",
                        )
                    )
                )
                asyncio.run(context.ensure_builtin_tool_seed_data())
                client = TestClient(create_app(context=context))

                list_response = client.post("/tools/agency.workflow.list/run", json={})
                get_response = client.post("/tools/agency.workflow.get/run", json={"workflow_id": workflow.id})
                tool_response = client.post("/tools/agency.tool.get/run", json={"tool_id": "agency.http.request"})
                execution_response = client.post(
                    "/tools/agency.execution.get/run",
                    json={"execution_id": execution.id},
                )
                events_response = client.post(
                    "/tools/agency.execution.events/run",
                    json={"execution_id": execution.id, "event_types": ["execution.completed"]},
                )
                artifacts_response = client.post(
                    "/tools/agency.execution.artifacts/run",
                    json={"execution_id": execution.id, "include_content": True},
                )

                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(list_response.json()["result"]["workflows"][0]["id"], workflow.id)
                self.assertEqual(get_response.status_code, 200)
                self.assertEqual(get_response.json()["result"]["workflow"]["id"], workflow.id)
                self.assertEqual(tool_response.status_code, 200)
                self.assertEqual(tool_response.json()["result"]["tool"]["id"], "agency.http.request")
                self.assertEqual(execution_response.status_code, 200)
                self.assertEqual(execution_response.json()["result"]["execution"]["id"], execution.id)
                self.assertEqual(events_response.status_code, 200)
                self.assertEqual(events_response.json()["result"]["count"], 1)
                self.assertEqual(artifacts_response.status_code, 200)
                self.assertEqual(artifacts_response.json()["result"]["items"][0]["content_text"], "artifact body")
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_marks_direct_proposal_tool_as_conversation_context_required(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.workflow.propose-create/run",
                    json={"goal": "Create a workflow that drafts a report."},
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "requires_conversation_context")
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_runs_context_backed_memory_crud(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        headers = {"x-agency-user-id": "user-memory", "x-agency-user-email": "memory@example.com"}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                context = create_test_api_context()
                client = TestClient(create_app(context=context))
                remember = client.post(
                    "/tools/agency.memory.remember/run",
                    headers=headers,
                    json={"scope": "user", "content": "Remember API contract memory.", "confirmed": True},
                )
                memory_id = remember.json()["result"]["memory"]["id"]
                listed = client.post(
                    "/tools/agency.memory.list/run",
                    headers=headers,
                    json={"scope": "user", "query": "contract"},
                )
                updated = client.post(
                    "/tools/agency.memory.update/run",
                    headers=headers,
                    json={"memory_id": memory_id, "summary": "API contract memory."},
                )
                deleted = client.post(
                    "/tools/agency.memory.delete/run",
                    headers=headers,
                    json={"memory_id": memory_id},
                )

                self.assertEqual(remember.status_code, 200)
                self.assertEqual(remember.json()["result"]["memory"]["created_by_user_id"], "user-memory")
                self.assertEqual(listed.json()["result"]["memories"][0]["id"], memory_id)
                self.assertEqual(updated.json()["result"]["memory"]["summary"], "API contract memory.")
                self.assertTrue(deleted.json()["result"]["deleted"])
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_runs_contract_backed_command(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.command.run/run",
                    json={"command": "printf 'api-command\\n'", "mode": "bash", "timeout_seconds": 2},
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "ok")
                self.assertEqual(body["result"]["stdout"], "api-command")
                self.assertTrue(body["signature"].startswith("sha256:"))
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_runs_contract_backed_file_write_text(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        previous_allowed_dirs = os.environ.get("TOOL_FILE_WRITE_ALLOWED_DIRS")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                os.environ["TOOL_RUN_STORE_PATH"] = str(root / "api_tool_runs.jsonl")
                os.environ["TOOL_FILE_WRITE_ALLOWED_DIRS"] = str(root)
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.file.write-text/run",
                    json={
                        "base_folder": str(root),
                        "filename": "api.txt",
                        "content": "api file",
                        "mode": "write",
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "success")
                self.assertEqual((root / "api.txt").read_text(encoding="utf-8"), "api file")
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            if previous_allowed_dirs is None:
                os.environ.pop("TOOL_FILE_WRITE_ALLOWED_DIRS", None)
            else:
                os.environ["TOOL_FILE_WRITE_ALLOWED_DIRS"] = previous_allowed_dirs
            reset_settings_cache()

    @patch("app.tools.implementations.documents.upload_to_s3")
    def test_api_runs_contract_backed_markdown_to_word(self, mock_upload):
        mock_upload.return_value = {"uploaded_files": ["user_api/workflow_reports/run_proc-api/report.docx"]}
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["TOOL_RUN_STORE_PATH"] = str(Path(tmp) / "api_tool_runs.jsonl")
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.document.markdown-to-word/run",
                    json={
                        "markdown_text": "# API Report\n\nBody",
                        "filename": "report.docx",
                        "img_directory": "reports",
                        "process_id": "proc-api",
                        "run_by": "api",
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "success")
                self.assertEqual(body["result"]["storage_uri"], "s3://mybucket/user_api/workflow_reports/run_proc-api/report.docx")
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_api_runs_contract_backed_excel_text_writer(self):
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        previous_allowed_dirs = os.environ.get("TOOL_FILE_WRITE_ALLOWED_DIRS")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workbook_path = _create_workbook(root / "results.xlsx")
                text_path = root / "result.txt"
                text_path.write_text("api spreadsheet", encoding="utf-8")
                os.environ["TOOL_RUN_STORE_PATH"] = str(root / "api_tool_runs.jsonl")
                os.environ["TOOL_FILE_WRITE_ALLOWED_DIRS"] = str(root)
                reset_settings_cache()
                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/agency.excel.write-text/run",
                    json={
                        "sheet_name": "Sheet1",
                        "excel_file_path": str(workbook_path),
                        "text_file_path": str(text_path),
                        "serial_number": 1,
                        "header_title": "Notes",
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "warn")
                self.assertEqual(body["result"]["status"], "success")
                self.assertEqual(load_workbook(workbook_path).active["A2"].value, "api spreadsheet")
        finally:
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            if previous_allowed_dirs is None:
                os.environ.pop("TOOL_FILE_WRITE_ALLOWED_DIRS", None)
            else:
                os.environ["TOOL_FILE_WRITE_ALLOWED_DIRS"] = previous_allowed_dirs
            reset_settings_cache()

    def test_api_runs_sandbox_edit_dry_run_against_allowlisted_repo(self):
        previous_allowlist = os.environ.get("SANDBOX_EDIT_ALLOWED_REPOS")
        previous_store = os.environ.get("TOOL_RUN_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
                (repo / "README.md").write_text("hello\n", encoding="utf-8")
                os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(repo)
                os.environ["TOOL_RUN_STORE_PATH"] = str(repo / "api_tool_runs.jsonl")
                reset_settings_cache()

                client = TestClient(create_app(context=create_test_api_context()))
                response = client.post(
                    "/tools/sandbox-edit/run",
                    json={
                        "repo": str(repo),
                        "ref": "main",
                        "changes": [{"path": "README.md", "patch": README_PATCH}],
                        "dryRun": True,
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["verdict"], "ok")
                self.assertTrue(body["signature"].startswith("sha256:"))
                self.assertEqual(body["filesChanged"][0]["path"], "README.md")
                self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "hello\n")
                records = JsonlToolRunStore(repo / "api_tool_runs.jsonl").list_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].tool_name, "sandbox-edit")
        finally:
            if previous_allowlist is None:
                os.environ.pop("SANDBOX_EDIT_ALLOWED_REPOS", None)
            else:
                os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = previous_allowlist
            if previous_store is None:
                os.environ.pop("TOOL_RUN_STORE_PATH", None)
            else:
                os.environ["TOOL_RUN_STORE_PATH"] = previous_store
            reset_settings_cache()

    def test_build_dry_run_pr_payload_is_external_integration_ready(self):
        payload = build_dry_run_pr_payload(
            repo="git@example.com:agency/agency-fe.git",
            branch="agent/1234-sandbox",
            base="main",
            title="[dry-run] propose change",
            files=[{"path": "README.md", "patch": README_PATCH}],
            metadata={"source": "test"},
            agent="codex/test",
        )

        self.assertEqual(payload["action"], "create_pr_dry_run")
        self.assertEqual(payload["patchJson"]["files"][0]["path"], "README.md")
        self.assertEqual(payload["metadata"]["agent"], "codex/test")
        self.assertEqual(payload["metadata"]["source"], "test")

    def test_runtime_publishes_tool_lifecycle_events(self):
        async def run_assertions():
            bus = RuntimeEventBus()
            set_default_runtime_event_bus(bus)
            subscriber = await bus.subscribe()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
                    (repo / "README.md").write_text("hello\n", encoding="utf-8")
                    executor = ToolRuntimeExecutor(
                        policy_engine=PolicyEngine(allowed_repos=[str(repo)]),
                        run_store=JsonlToolRunStore(repo / "tool_runs.jsonl"),
                    )

                    response = executor.run(
                        "sandbox-edit",
                        {
                            "repo": str(repo),
                            "ref": "main",
                            "changes": [{"path": "README.md", "patch": README_PATCH}],
                            "dryRun": True,
                        },
                        actor="codex/test",
                    )
                    await asyncio.sleep(0)

                    self.assertEqual(response.verdict, "ok")
                    events = []
                    while not subscriber.empty():
                        events.append(await subscriber.get())
                    semantic_types = [event.metadata.get("semanticType") for event in events]
                    self.assertIn("tool.run.started", semantic_types)
                    self.assertIn("tool.policy.completed", semantic_types)
                    self.assertIn("tool.run.completed", semantic_types)
                    self.assertIn("ok", [event.metadata.get("verdict") for event in events])
            finally:
                set_default_runtime_event_bus(None)

        asyncio.run(run_assertions())


if __name__ == "__main__":
    unittest.main()

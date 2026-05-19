from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import storage
from app.core.logging import configure_logging, get_logger
from app.domain import ModelProfileDefinition
from app.runtime.adapters.crewai.events import print_agent_output_to_json
from app.runtime.adapters.crewai.mapper import LLMmodel
from app.runtime.channels import agent_output_channel, human_reply_channel
from app.runtime.process_logs import initialize_log_file, log_file_path_for
from app.runtime.process_supervisor import ExecutionProcessManager
from app.tools.input_mapping import convert_str_to_dict, interpolate


class ToolInputMappingTests(unittest.TestCase):
    def test_convert_str_to_dict_supports_key_value_pairs(self):
        self.assertEqual(convert_str_to_dict("a=1,b=2"), {"a": "1", "b": "2"})

    def test_convert_str_to_dict_supports_json_dicts(self):
        self.assertEqual(convert_str_to_dict('{"a": 1}'), {"a": 1})

    def test_interpolate_recurses_through_structures(self):
        payload = {
            "url": "https://example.test/{id}",
            "headers": {"X-Run": "{run_id}"},
            "items": ["{name}", {"status": "{status}"}],
        }
        rendered = interpolate(payload, {"id": "42", "run_id": "abc", "name": "demo", "status": "ok"})
        self.assertEqual(
            rendered,
            {
                "url": "https://example.test/42",
                "headers": {"X-Run": "abc"},
                "items": ["demo", {"status": "ok"}],
            },
        )


class StorageMigrationTests(unittest.TestCase):
    def test_mock_upload_to_local_writes_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(storage, "LOCAL_STORAGE_PATH", temp_dir):
                target_path = storage.get_local_file_path("crew/file.txt")
                storage.mock_upload_to_local(b"hello", target_path)
                self.assertEqual(Path(target_path).read_bytes(), b"hello")

    def test_generate_presigned_url_uses_local_fallback_path(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}, clear=False):
            url = storage.generate_presigned_url("upload", "demo.txt", "text/plain")
        self.assertEqual(url, "http://localhost:8000/api/local-storage/upload?file=demo.txt")


class CrewAIUtilityMigrationTests(unittest.TestCase):
    def test_crewai_logging_writes_string_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir) / "logs"
            logs_dir.mkdir()
            cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                print_agent_output_to_json("proc-1", "hello", "Agent A")
            finally:
                os.chdir(cwd)

            content = (logs_dir / "proc-1.json").read_text(encoding="utf-8")
            self.assertIn('"agent_name": "Agent A"', content)
            self.assertIn('"output": "hello"', content)

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "secret",
        },
        clear=False,
    )
    @patch("crewai.LLM")
    def test_crewai_llm_factory_uses_azure_prefix(self, mock_llm):
        instance = mock_llm.return_value
        from app.runtime.adapters.crewai.mapper import create_llm_model, select_llm_model

        result = create_llm_model(LLMmodel.GPT4o.value, temperature=0.7)
        self.assertIs(result, instance)
        mock_llm.assert_called_once_with(
            model="azure/gpt-4o",
            endpoint="https://example.openai.azure.com",
            api_key="secret",
            api_version="2024-06-01",
        )
        self.assertEqual(instance.temperature, 0.7)
        self.assertIs(select_llm_model("gpt-4o"), instance)

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_ENDPOINT": "",
            "AZURE_OPENAI_API_KEY": "",
            "AZURE_ENDPOINT": "",
            "AZURE_API_KEY": "",
        },
        clear=False,
    )
    @patch("crewai.LLM")
    def test_crewai_llm_factory_allows_non_azure_fallback(self, mock_llm):
        instance = mock_llm.return_value
        from app.runtime.adapters.crewai.mapper import create_llm_model

        result = create_llm_model("gpt-4o-mini", temperature=0.2)

        self.assertIs(result, instance)
        mock_llm.assert_called_once_with(model="gpt-4o-mini")
        self.assertEqual(instance.temperature, 0.2)

    @patch("crewai.LLM")
    def test_crewai_llm_factory_uses_profile_base_url_and_api_key(self, mock_llm):
        instance = mock_llm.return_value
        from app.runtime.adapters.crewai.mapper import create_llm_model

        profile = ModelProfileDefinition(
            id="profile-openai-compatible",
            name="Local OpenAI Compatible",
            provider="openai_compatible",
            model="local-model",
            base_url="http://localhost:1234/v1",
            api_key_ref="local-api-key",
        )

        result = create_llm_model("local-model", temperature=0.3, profile=profile)

        self.assertIs(result, instance)
        mock_llm.assert_called_once_with(
            model="local-model",
            base_url="http://localhost:1234/v1",
            api_key="local-api-key",
        )
        self.assertEqual(instance.temperature, 0.3)

    @patch("crewai.LLM")
    def test_crewai_llm_factory_passes_ollama_provider(self, mock_llm):
        instance = mock_llm.return_value
        from app.runtime.adapters.crewai.mapper import create_llm_model

        profile = ModelProfileDefinition(
            id="profile-ollama",
            name="Ollama",
            provider="ollama",
            model="qwen3:30b",
            base_url="http://localhost:11434",
        )

        result = create_llm_model("qwen3:30b", temperature=0.1, profile=profile)

        self.assertIs(result, instance)
        mock_llm.assert_called_once_with(
            model="qwen3:30b",
            provider="ollama",
            base_url="http://localhost:11434",
        )
        self.assertEqual(instance.temperature, 0.1)


class RuntimeUtilityMigrationTests(unittest.TestCase):
    def test_process_manager_channel_helpers_are_app_owned(self):
        self.assertEqual(agent_output_channel("abc"), "agent_output_channel_abc")
        self.assertEqual(human_reply_channel("abc"), "human_reply_channel_abc")

    def test_process_log_helpers_create_expected_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                path = initialize_log_file("proc-2")
            finally:
                os.chdir(cwd)
        self.assertTrue(path.endswith("logs/proc-2.json"))
        self.assertEqual(log_file_path_for("proc-2"), "logs/proc-2.json")

    def test_execution_process_manager_status_without_process(self):
        manager = ExecutionProcessManager(redis_client=None)
        self.assertEqual(manager.process_status("missing"), {"status": "No active process"})

    def test_logging_module_returns_named_logger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "app.log"
            configure_logging(log_file=log_file)
            logger = get_logger("tests.utils")
            logger.info("hello")
            self.assertEqual(logger.name, "tests.utils")


if __name__ == "__main__":
    unittest.main()

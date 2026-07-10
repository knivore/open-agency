import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain import ModelProfileDefinition, ModelProviderDefinition
from app.llm.base import ModelMessage
from app.llm.openai_codex import OpenAICodexModelClient
from app.utils.oauth_pkce import OAuthPKCEHandler

class TestOpenAICodexEnhancedAuth(unittest.TestCase):

    def test_handler_device_auth_initiate(self):
        handler = OAuthPKCEHandler(client_id="test_client")
        
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "device_code": "dev_123",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.openai.com/activate",
                    "expires_in": 900,
                    "interval": 5
                }
            )
            
            data = asyncio.run(handler.initiate_device_auth())
            
            self.assertEqual(data["user_code"], "ABCD-1234")
            self.assertEqual(data["device_code"], "dev_123")
            mock_post.assert_called_once()
            # Verify URL transformation
            self.assertIn("/device/code", mock_post.call_args[0][0])

    def test_handler_device_auth_poll(self):
        handler = OAuthPKCEHandler(client_id="test_client")
        
        with patch("httpx.AsyncClient.post") as mock_post:
            # Mock pending then success
            mock_post.side_effect = [
                MagicMock(status_code=400, json=lambda: {"error": "authorization_pending"}),
                MagicMock(status_code=200, json=lambda: {"access_token": "acc_123", "refresh_token": "ref_123", "expires_in": 3600})
            ]
            
            # Patch sleep to avoid waiting
            with patch("asyncio.sleep", return_value=None):
                tokens = asyncio.run(handler.poll_device_token("dev_123", interval=0))
                
                self.assertEqual(tokens["access_token"], "acc_123")
                self.assertEqual(mock_post.call_count, 2)

    def test_codex_client_api_key_mode(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            parameters={
                "auth_mode": "api",
                "api_key": "sk-test-key"
            }
        )
        env_config = MagicMock()
        
        client = OpenAICodexModelClient(profile, env_config)
        
        self.assertEqual(client.auth_mode, "api")
        self.assertEqual(client.api_key, "sk-test-key")
        self.assertEqual(client.client.api_key, "sk-test-key")
        
        # Ensure _ensure_authorized doesn't try to refresh
        asyncio.run(client._ensure_authorized())

    def test_codex_client_oauth_mode_refresh(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            parameters={
                "auth_mode": "chatgpt",
                "access_token": "expired",
                "refresh_token": "refresh",
                "expires_at": time.time() - 100
            }
        )
        env_config = MagicMock()
        env_config.model_provider_repo = AsyncMock()
        env_config.model_provider_repo.get.return_value = None
        
        client = OpenAICodexModelClient(profile, env_config)
        
        with patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token") as mock_refresh:
            mock_refresh.return_value = {
                "access_token": "new_acc",
                "refresh_token": "new_ref",
                "expires_in": 3600
            }
            
            asyncio.run(client._ensure_authorized())
            
            self.assertEqual(client.access_token, "new_acc")
            self.assertEqual(client.client.api_key, "new_acc")
            mock_refresh.assert_called_once()
            env_config.model_provider_repo.update_tokens.assert_awaited_once()

    def test_extract_account_id_from_access_token(self):
        payload = "eyJhY2NvdW50SWQiOiJhY2N0XzEyMyJ9"
        token = f"header.{payload}.signature"
        self.assertEqual(OAuthPKCEHandler.extract_account_id(token), "acct_123")

    def test_extract_account_id_from_openai_auth_claim(self):
        payload = "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdF80NTYifX0"
        token = f"header.{payload}.signature"
        self.assertEqual(OAuthPKCEHandler.extract_account_id(token), "acct_456")

    def test_codex_client_hydrates_tokens_from_provider_config(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex-provider",
            model="gpt-5.1-codex",
            parameters={"oauth_profile_id": "acct-1"},
        )
        provider = ModelProviderDefinition(
            id="openai_codex-provider",
            name="Provider",
            provider_type="openai_codex",
            config={
                "default_oauth_profile_id": "acct-1",
                "auth_profiles": {
                    "acct-1": {
                        "access_token": "provider-token",
                        "refresh_token": "provider-refresh",
                        "expires_at": time.time() + 3600,
                        "client_id": "client-1",
                        "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                        "account_id": "acct_123",
                    }
                },
            },
        )
        env_config = MagicMock()
        env_config.model_provider_repo = AsyncMock()
        env_config.model_provider_repo.get.return_value = provider

        client = OpenAICodexModelClient(profile, env_config)
        asyncio.run(client._ensure_authorized())

        self.assertEqual(client.access_token, "provider-token")
        self.assertEqual(client.refresh_token, "provider-refresh")
        self.assertEqual(client.account_id, "acct_123")

    def test_codex_client_prefers_provider_api_key_for_public_api(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex-provider",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            parameters={"oauth_profile_id": "acct-1"},
        )
        provider = ModelProviderDefinition(
            id="openai_codex-provider",
            name="Provider",
            provider_type="openai_codex",
            config={
                "api_key": "sk-provider-key",
                "default_oauth_profile_id": "acct-1",
                "auth_profiles": {
                    "acct-1": {
                        "auth_mode": "chatgpt",
                        "access_token": "provider-token",
                    }
                },
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = AsyncMock()
        env_config.model_provider_repo.get.return_value = provider

        client = OpenAICodexModelClient(profile, env_config)
        asyncio.run(client._ensure_authorized())

        self.assertEqual(client.auth_mode, "api")
        self.assertEqual(client.api_key, "sk-provider-key")
        self.assertEqual(client.client.api_key, "sk-provider-key")

    def test_codex_client_omits_unset_optional_chat_options(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            temperature=0.2,
            max_tokens=None,
            parameters={
                "auth_mode": "api",
                "api_key": "sk-test-key",
            },
        )
        env_config = MagicMock()
        env_config.model_provider_repo = None
        client = OpenAICodexModelClient(profile, env_config)

        options = client._chat_options(temperature=None, max_tokens=None, stream=False)

        self.assertNotIn("temperature", options)
        self.assertNotIn("max_tokens", options)
        self.assertEqual(options["model"], "gpt-5.1-codex")
        self.assertFalse(options["stream"])

    def test_codex_client_uses_cli_for_chatgpt_oauth_mode(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            parameters={
                "auth_mode": "chatgpt",
                "access_token": "chatgpt-token",
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None

        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"

            def fake_run(command, **kwargs):
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text("CLI response", encoding="utf-8")
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            client = OpenAICodexModelClient(profile, env_config)
            with patch("app.llm.openai_codex.shutil.which", return_value="codex"), patch(
                "app.llm.openai_codex.subprocess.run",
                side_effect=fake_run,
            ) as mock_run, patch("app.llm.openai_codex.tempfile.NamedTemporaryFile") as mock_tmp:
                handle = MagicMock()
                handle.name = str(output_path)
                mock_tmp.return_value.__enter__.return_value = handle
                response = client.generate_text([])

        self.assertEqual(response.content, "CLI response")
        self.assertIn("--sandbox", mock_run.call_args.args[0])

    def test_codex_client_cli_sandbox_is_configurable(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            parameters={
                "auth_mode": "chatgpt",
                "access_token": "chatgpt-token",
                "codex_cli_sandbox": "danger-full-access",
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None

        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"

            def fake_run(command, **kwargs):
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text("CLI response", encoding="utf-8")
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            client = OpenAICodexModelClient(profile, env_config)
            with patch("app.llm.openai_codex.shutil.which", return_value="codex"), patch(
                "app.llm.openai_codex.subprocess.run",
                side_effect=fake_run,
            ) as mock_run, patch("app.llm.openai_codex.tempfile.NamedTemporaryFile") as mock_tmp:
                handle = MagicMock()
                handle.name = str(output_path)
                mock_tmp.return_value.__enter__.return_value = handle
                response = client.generate_text([])

        command = mock_run.call_args.args[0]
        sandbox_index = command.index("--sandbox")
        self.assertEqual(response.content, "CLI response")
        self.assertEqual(command[sandbox_index + 1], "danger-full-access")

    def test_codex_oauth_cli_returns_agency_native_tool_call(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.4",
            parameters={"auth_mode": "chatgpt", "access_token": "chatgpt-token"},
        )
        env_config = MagicMock(openai_api_key=None, model_provider_repo=None)
        tool_payload = [
            {
                "type": "function",
                "function": {
                    "name": "get_agency_graph_context",
                    "description": "Read bounded Agency graph context.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]

        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"

            def fake_run(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(
                    json.dumps(
                        {
                            "action": "tool_call",
                            "tool_name": "get_agency_graph_context",
                            "arguments_json": json.dumps({"query": "Agency architecture"}),
                            "content": "",
                        }
                    ),
                    encoding="utf-8",
                )
                return MagicMock(returncode=0, stdout="", stderr="")

            client = OpenAICodexModelClient(profile, env_config)
            with patch("app.llm.openai_codex.shutil.which", return_value="codex"), patch(
                "app.llm.openai_codex.subprocess.run", side_effect=fake_run
            ) as mock_run, patch("app.llm.openai_codex.tempfile.NamedTemporaryFile") as mock_tmp:
                output_handle = MagicMock()
                output_handle.name = str(output_path)
                schema_handle = MagicMock()
                schema_handle.name = str(Path(tmpdir) / "schema.json")
                mock_tmp.return_value.__enter__.side_effect = [output_handle, schema_handle]
                response = client.generate_text(
                    [ModelMessage(role="user", content="Map the Agency repository")],
                    tools=tool_payload,
                )

        self.assertIsNone(response.content)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "get_agency_graph_context")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "Agency architecture"})
        command = mock_run.call_args.args[0]
        self.assertIn("--output-schema", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        prompt = mock_run.call_args.kwargs["input"]
        self.assertIn("Agency owns all tool execution and policy checks", prompt)
        self.assertIn("get_agency_graph_context", prompt)

    def test_codex_oauth_cli_returns_final_after_agency_tool_result(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.4",
            parameters={"auth_mode": "chatgpt", "access_token": "chatgpt-token"},
        )
        client = OpenAICodexModelClient(
            profile,
            MagicMock(openai_api_key=None, model_provider_repo=None),
        )
        response = MagicMock(
            content=json.dumps(
                {
                    "action": "final",
                    "tool_name": "",
                    "arguments_json": "{}",
                    "content": "Agency graph context loaded.",
                }
            ),
            tool_calls=[],
        )

        parsed = client._parse_cli_tool_response(
            response,
            allowed_tool_names={"get_agency_graph_context"},
        )

        self.assertEqual(parsed.content, "Agency graph context loaded.")
        self.assertEqual(parsed.tool_calls, [])

    def test_codex_structured_oauth_uses_cli_profile_model(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.4",
            base_url="https://api.openai.com/v1",
            parameters={
                "auth_mode": "chatgpt",
                "access_token": "chatgpt-token",
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None

        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"

            def fake_run(command, **kwargs):
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text('{"label": "sop", "confidence": 0.91}', encoding="utf-8")
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            client = OpenAICodexModelClient(profile, env_config)
            with patch("app.llm.openai_codex.shutil.which", return_value="codex"), patch(
                "app.llm.openai_codex.subprocess.run",
                side_effect=fake_run,
            ) as mock_run, patch("app.llm.openai_codex.tempfile.NamedTemporaryFile") as mock_tmp, patch.object(
                client.client.chat.completions,
                "create",
            ) as mock_create:
                handle = MagicMock()
                handle.name = str(output_path)
                mock_tmp.return_value.__enter__.return_value = handle
                response = client.generate_structured(
                    [ModelMessage(role="user", content="classify this source")],
                    schema={
                        "type": "object",
                        "properties": {"label": {"type": "string"}, "confidence": {"type": "number"}},
                        "required": ["label", "confidence"],
                    },
                    schema_name="source_intelligence_classification",
                )

        command = mock_run.call_args.args[0]
        model_index = command.index("--model")
        prompt = mock_run.call_args.kwargs["input"]
        self.assertEqual(command[model_index + 1], "gpt-5.4")
        self.assertIn("source_intelligence_classification", prompt)
        self.assertEqual(response.content, {"label": "sop", "confidence": 0.91})
        mock_create.assert_not_called()

    def test_codex_async_structured_oauth_uses_cli_profile_model(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.4",
            base_url="https://api.openai.com/v1",
            parameters={
                "auth_mode": "chatgpt",
                "access_token": "chatgpt-token",
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None

        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"

            def fake_run(command, **kwargs):
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text('{"label": "policy"}', encoding="utf-8")
                completed = MagicMock()
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            client = OpenAICodexModelClient(profile, env_config)
            with patch("app.llm.openai_codex.shutil.which", return_value="codex"), patch(
                "app.llm.openai_codex.subprocess.run",
                side_effect=fake_run,
            ) as mock_run, patch("app.llm.openai_codex.tempfile.NamedTemporaryFile") as mock_tmp, patch.object(
                client.client.chat.completions,
                "create",
            ) as mock_create:
                handle = MagicMock()
                handle.name = str(output_path)
                mock_tmp.return_value.__enter__.return_value = handle
                response = asyncio.run(
                    client.agenerate_structured(
                        [ModelMessage(role="user", content="classify this source")],
                        schema={"type": "object", "properties": {"label": {"type": "string"}}},
                        schema_name="source_intelligence_classification",
                    )
                )

        command = mock_run.call_args.args[0]
        model_index = command.index("--model")
        self.assertEqual(command[model_index + 1], "gpt-5.4")
        self.assertEqual(response.content, {"label": "policy"})
        mock_create.assert_not_called()

    def test_codex_health_reports_oauth_ready_without_public_api_scope_check(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            parameters={
                "provider_id": "provider-codex",
                "oauth_profile_id": "acct-1",
                "auth_mode": "chatgpt",
                "access_token": "stale-token",
                "expires_at": time.time() + 3600,
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None

        client = OpenAICodexModelClient(profile, env_config)
        with patch("app.llm.openai_codex.shutil.which", return_value="/usr/local/bin/codex"), patch(
            "app.llm.openai_codex.httpx.Client",
        ) as mock_client:
            health = client.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(health["auth_status"], "ok")
        self.assertFalse(health["reauthorization_required"])
        self.assertIsNone(health["auth_action"])
        self.assertIsNone(health["auth_endpoint"])
        self.assertEqual(health["auth_profile_id"], "acct-1")
        self.assertTrue(health["codex_cli_available"])
        mock_client.assert_not_called()

    def test_codex_async_health_hydrates_provider_without_threaded_public_scope_check(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai-codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
        )
        provider = ModelProviderDefinition(
            id="openai-codex",
            name="OpenAI Codex",
            provider_type="openai_codex",
            config={
                "auth_mode": "chatgpt",
                "default_oauth_profile_id": "default",
                "auth_profiles": {
                    "default": {
                        "auth_mode": "chatgpt",
                        "access_token": "provider-token",
                        "refresh_token": "provider-refresh",
                        "expires_at": time.time() + 3600,
                    }
                },
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = AsyncMock()
        env_config.model_provider_repo.get.return_value = provider

        client = OpenAICodexModelClient(profile, env_config)
        with patch("app.llm.openai_codex.shutil.which", return_value="/usr/local/bin/codex"), patch(
            "app.llm.openai_codex.httpx.Client",
        ) as mock_client:
            health = asyncio.run(client.ahealth_check())

        self.assertTrue(health["ok"])
        self.assertEqual(health["auth_status"], "ok")
        self.assertFalse(health["reauthorization_required"])
        self.assertEqual(health["auth_profile_id"], "default")
        self.assertEqual(client.access_token, "provider-token")
        mock_client.assert_not_called()

    def test_codex_health_reports_api_key_scope_without_oauth_reauth(self):
        profile = ModelProfileDefinition(
            id="test-profile",
            name="Test Profile",
            provider="openai_codex",
            model="gpt-5.1-codex",
            base_url="https://api.openai.com/v1",
            parameters={
                "provider_id": "provider-codex",
                "auth_mode": "api",
                "api_key": "sk-test-key",
            },
        )
        env_config = MagicMock()
        env_config.openai_api_key = None
        env_config.model_provider_repo = None
        response = MagicMock()
        response.status_code = 401
        response.text = "missing scope"
        response.json.return_value = {
            "error": {
                "message": "Missing scopes: model.request",
                "code": "missing_scope",
            }
        }

        client = OpenAICodexModelClient(profile, env_config)
        with patch("app.llm.openai_codex.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = response
            health = client.health_check()

        self.assertFalse(health["ok"])
        self.assertEqual(health["auth_status"], "missing_scope")
        self.assertFalse(health["reauthorization_required"])
        self.assertEqual(health["auth_action"], "update_api_key")
        self.assertEqual(health["auth_endpoint"], "/model-providers/provider-codex")

if __name__ == "__main__":
    unittest.main()

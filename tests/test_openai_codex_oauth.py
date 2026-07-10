import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from app.core.config import reset_settings_cache
from app.domain import ModelProfileDefinition
from app.llm.openai_codex import OpenAICodexModelClient
from app.llm.registry import LLMEnvironmentConfig
from app.utils.oauth_pkce import OAuthPKCEHandler, _oauth_async_client


class TestOpenAICodexOAuth(unittest.TestCase):
    def test_pkce_generation(self):
        handler = OAuthPKCEHandler()
        verifier, challenge = handler.generate_pkce_data()
        self.assertTrue(len(verifier) > 0)
        self.assertTrue(len(challenge) > 0)
        self.assertNotEqual(verifier, challenge)

    def test_auth_url_construction(self):
        handler = OAuthPKCEHandler.for_provider("openai_codex", client_id="")
        url = handler.get_authorization_url()
        self.assertIn("client_id=app_EMoamEEZ73f0CkXaXp7hrann", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback", url)
        self.assertIn("response_type=code", url)
        self.assertIn("code_challenge=", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("id_token_add_organizations=true", url)
        self.assertIn("codex_cli_simplified_flow=true", url)
        self.assertIn("originator=agency", url)
        self.assertNotIn("audience=", url)
        self.assertNotIn("prompt=login", url)
        self.assertTrue(url.startswith("https://auth.openai.com/oauth/authorize?"))

    def test_codex_legacy_defaults_are_normalized(self):
        handler = OAuthPKCEHandler.for_provider(
            "openai_codex",
            client_id="app_EMoaD9zS2S",
            redirect_uri="http://127.0.0.1:1455/auth/callback",
        )

        self.assertEqual(handler.client_id, "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(handler.redirect_uri, "http://localhost:1455/auth/callback")

    def test_redirect_url_parsing(self):
        parsed = OAuthPKCEHandler.parse_redirect_url(
            "http://127.0.0.1:1455/auth/callback?code=abc123&state=state456"
        )
        self.assertEqual(parsed["code"], "abc123")
        self.assertEqual(parsed["state"], "state456")

    def test_oauth_client_uses_certifi_when_ca_env_path_is_missing(self):
        with (
            patch.dict(os.environ, {"SSL_CERT_FILE": "/missing/agency-ca.pem"}, clear=False),
            patch("app.utils.oauth_pkce.certifi.where", return_value="/certifi/cacert.pem"),
            patch("app.utils.oauth_pkce.httpx.AsyncClient") as mock_client,
        ):
            _oauth_async_client()

        mock_client.assert_called_once_with(verify="/certifi/cacert.pem")

    @patch("httpx.AsyncClient.post")
    def test_token_exchange(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "expires_in": 3600
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        handler = OAuthPKCEHandler()
        tokens = asyncio.run(handler.exchange_token("test_code", "test_verifier"))

        self.assertEqual(tokens["access_token"], "test_access_token")
        self.assertEqual(tokens["refresh_token"], "test_refresh_token")

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    def test_codex_client_token_refresh(self, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600
        }

        profile = ModelProfileDefinition(
            id="test_profile",
            name="Test Codex",
            provider="openai_codex",
            model="gpt-5.3-codex",
            parameters={
                "access_token": "old_token",
                "refresh_token": "old_refresh",
                "expires_at": time.time() - 100  # Expired
            }
        )
        env = LLMEnvironmentConfig()
        client = OpenAICodexModelClient(profile, env)

        asyncio.run(client._ensure_authorized())

        self.assertEqual(client.access_token, "new_access_token")
        self.assertEqual(client.client.api_key, "new_access_token")

    def test_codex_cli_timeout_uses_settings_floor_for_profile(self):
        with patch.dict("os.environ", {}, clear=True):
            reset_settings_cache()
            env = LLMEnvironmentConfig()
            profile = ModelProfileDefinition(
                id="test_profile",
                name="Test Codex",
                provider="openai_codex",
                model="gpt-5.3-codex",
                parameters={"codex_cli_timeout_seconds": "300"},
            )
            client = OpenAICodexModelClient(profile, env)

            self.assertEqual(client._codex_cli_timeout_seconds(), 1800)

            profile = profile.model_copy(update={"parameters": {"codex_cli_timeout_seconds": "2400"}})
            client = OpenAICodexModelClient(profile, env)
            self.assertEqual(client._codex_cli_timeout_seconds(), 2400)

        with patch.dict("os.environ", {"CODEX_CLI_TIMEOUT_SECONDS": "450"}, clear=False):
            self.assertEqual(client._codex_cli_timeout_seconds(), 450)
        reset_settings_cache()

    def test_codex_cli_timeout_respects_llm_timeout_when_no_explicit_cli_override(self):
        with patch.dict("os.environ", {"LLM_REQUEST_TIMEOUT_SECONDS": "15"}, clear=True):
            reset_settings_cache()
            env = LLMEnvironmentConfig()
            profile = ModelProfileDefinition(
                id="test_profile",
                name="Test Codex",
                provider="openai_codex",
                model="gpt-5.3-codex",
                parameters={"codex_cli_timeout_seconds": "300"},
            )
            client = OpenAICodexModelClient(profile, env)

            self.assertEqual(client._codex_cli_timeout_seconds(), 15)
            reset_settings_cache()

    def test_codex_cli_timeout_uses_settings_default(self):
        with patch.dict("os.environ", {}, clear=True):
            reset_settings_cache()
            env = LLMEnvironmentConfig()
            profile = ModelProfileDefinition(
                id="test_profile",
                name="Test Codex",
                provider="openai_codex",
                model="gpt-5.3-codex",
                parameters={},
            )
            client = OpenAICodexModelClient(profile, env)

            self.assertEqual(client._codex_cli_timeout_seconds(), 1800)
            reset_settings_cache()


if __name__ == "__main__":
    unittest.main()

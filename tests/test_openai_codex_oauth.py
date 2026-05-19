import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from app.domain import ModelProfileDefinition
from app.llm.openai_codex import OpenAICodexModelClient
from app.llm.registry import LLMEnvironmentConfig
from app.utils.oauth_pkce import OAuthPKCEHandler


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

    def test_codex_cli_timeout_prefers_profile_then_environment(self):
        env = LLMEnvironmentConfig()
        profile = ModelProfileDefinition(
            id="test_profile",
            name="Test Codex",
            provider="openai_codex",
            model="gpt-5.3-codex",
            parameters={"codex_cli_timeout_seconds": "300"},
        )
        client = OpenAICodexModelClient(profile, env)

        self.assertEqual(client._codex_cli_timeout_seconds(), 300)

        profile = profile.model_copy(update={"parameters": {}})
        client = OpenAICodexModelClient(profile, env)
        with patch.dict("os.environ", {"CODEX_CLI_TIMEOUT_SECONDS": "450"}, clear=False):
            self.assertEqual(client._codex_cli_timeout_seconds(), 450)


if __name__ == "__main__":
    unittest.main()

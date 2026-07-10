import unittest
from unittest.mock import AsyncMock, patch
import time

from app.llm.openai_codex import OpenAICodexModelClient
from app.llm.google import GoogleModelClient
from app.llm.azure import AzureOpenAIModelClient
from app.domain import ModelProfileDefinition, ModelProviderType
from app.llm.registry import LLMEnvironmentConfig

class TestOAuthPersistence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_repo = AsyncMock()
        self.mock_repo.get.return_value = None
        self.env_config = LLMEnvironmentConfig(model_provider_repo=self.mock_repo)
        
        self.profile = ModelProfileDefinition(
            id="test-profile",
            name="test-profile-name",
            provider="test-provider",
            model="gpt-5.3-codex",
            parameters={
                "access_token": "old-token",
                "refresh_token": "refresh-token",
                "expires_at": time.time() - 100 # Expired
            }
        )

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    @patch("app.llm.openai_codex.OpenAI")
    async def test_codex_persistence(self, _mock_openai, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "new-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600
        }
        
        client = OpenAICodexModelClient(self.profile, self.env_config)
        await client._ensure_authorized()
        
        self.assertEqual(client.access_token, "new-token")
        self.mock_repo.update_tokens.assert_called_once()
        args, _ = self.mock_repo.update_tokens.call_args
        self.assertEqual(args[0], "test-provider")
        self.assertEqual(args[1], "new-token")
        self.assertEqual(args[2], "new-refresh-token")

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    async def test_google_persistence(self, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "new-google-token",
            "refresh_token": "new-google-refresh",
            "expires_in": 3600
        }
        
        self.profile.provider = ModelProviderType.GOOGLE
        client = GoogleModelClient(self.profile, self.env_config)
        await client._ensure_authorized()
        
        self.assertEqual(client.access_token, "new-google-token")
        self.mock_repo.update_tokens.assert_called_once_with(
            ModelProviderType.GOOGLE, "new-google-token", "new-google-refresh", unittest.mock.ANY
        )

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    @patch("app.llm.azure.AzureOpenAI")
    async def test_azure_persistence(self, _mock_azure, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "new-azure-token",
            "refresh_token": "new-azure-refresh",
            "expires_in": 3600
        }
        
        self.profile.provider = ModelProviderType.AZURE_OPENAI
        client = AzureOpenAIModelClient(self.profile, self.env_config)
        await client._ensure_authorized()
        
        self.assertEqual(client.access_token, "new-azure-token")
        self.mock_repo.update_tokens.assert_called_once_with(
            ModelProviderType.AZURE_OPENAI, "new-azure-token", "new-azure-refresh", unittest.mock.ANY
        )

if __name__ == "__main__":
    unittest.main()

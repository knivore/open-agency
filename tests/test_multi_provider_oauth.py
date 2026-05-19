import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from app.domain import ModelProfileDefinition
from app.llm.azure import AzureOpenAIModelClient
from app.llm.google import GoogleModelClient
from app.llm.registry import LLMEnvironmentConfig
from app.utils.oauth_pkce import OAuthPKCEHandler


class TestMultiProviderOAuth(unittest.TestCase):
    def test_handler_factory_google(self):
        handler = OAuthPKCEHandler.for_provider("google", client_id="google_client", redirect_uri="http://localhost/cb")
        self.assertEqual(handler.auth_url, "https://accounts.google.com/o/oauth2/v2/auth")
        self.assertEqual(handler.token_url, "https://oauth2.googleapis.com/token")
        self.assertIn("https://www.googleapis.com/auth/cloud-platform", handler.scope)

    def test_handler_factory_azure(self):
        handler = OAuthPKCEHandler.for_provider("azure_openai", client_id="azure_client",
                                                redirect_uri="http://localhost/cb", tenant_id="my-tenant")
        self.assertIn("my-tenant", handler.auth_url)
        self.assertIn("my-tenant", handler.token_url)
        self.assertIn("https://cognitiveservices.azure.com/.default", handler.scope)

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    def test_google_client_token_refresh(self, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "google_new_token",
            "refresh_token": "google_new_refresh",
            "expires_in": 3600
        }

        profile = ModelProfileDefinition(
            id="test_google",
            name="Test Google",
            provider="google",
            model="gemini-pro",
            parameters={
                "access_token": "old_token",
                "refresh_token": "old_refresh",
                "expires_at": time.time() - 100  # Expired
            }
        )
        env = LLMEnvironmentConfig()
        client = GoogleModelClient(profile, env)

        asyncio.run(client._ensure_authorized())

        self.assertEqual(client.access_token, "google_new_token")
        self.assertEqual(client._ensure_api_key(), "google_new_token")

    @patch("app.utils.oauth_pkce.OAuthPKCEHandler.refresh_token")
    def test_azure_client_token_refresh(self, mock_refresh):
        mock_refresh.return_value = {
            "access_token": "azure_new_token",
            "refresh_token": "azure_new_refresh",
            "expires_in": 3600
        }

        profile = ModelProfileDefinition(
            id="test_azure",
            name="Test Azure",
            provider="azure_openai",
            model="gpt-4",
            base_url="https://my-resource.openai.azure.com/",
            parameters={
                "access_token": "old_token",
                "refresh_token": "old_refresh",
                "expires_at": time.time() - 100  # Expired
            }
        )
        env = LLMEnvironmentConfig()
        client = AzureOpenAIModelClient(profile, env)

        asyncio.run(client._ensure_authorized())

        self.assertEqual(client.access_token, "azure_new_token")
        # Azure OpenAI client uses azure_ad_token which is mapped from access_token in our client
        self.assertEqual(client.client._azure_ad_token, "azure_new_token")


if __name__ == "__main__":
    unittest.main()

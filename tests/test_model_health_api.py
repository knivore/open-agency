from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import ModelProfileDefinition, ModelProviderDefinition
from app.llm.base import ModelResponse


class _HealthyModelClient:
    provider_key = "openai"

    def __init__(self, profile: ModelProfileDefinition, env):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="ok", provider=self.profile.provider, model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "ok"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True, "provider": self.profile.provider, "model": self.profile.model}


class _CodexReauthModelClient(_HealthyModelClient):
    provider_key = "openai_codex"

    def health_check(self):
        return {
            "ok": False,
            "provider": self.profile.provider,
            "model": self.profile.model,
            "error": "Missing scopes: model.request",
            "error_code": "missing_model_request_scope",
            "auth_status": "missing_scope",
            "auth_required": True,
            "reauthorization_required": True,
            "auth_mode": "chatgpt",
            "auth_action": "device_authorize",
            "auth_endpoint": "/model-providers/provider-codex/device-authorize",
            "auth_profile_id": "acct-1",
            "provider_id": "provider-codex",
        }


class ModelHealthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("openai", lambda profile, env: _HealthyModelClient(profile, env))
        self.context.llm_provider_registry.register(
            "openai_codex",
            lambda profile, env: _CodexReauthModelClient(profile, env),
        )
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-models",
            "x-agency-user-email": "models@example.com",
        }

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_model_provider_health_endpoint(self) -> None:
        self._run(
            self.context.model_provider_repo.create(
                ModelProviderDefinition(
                    id="provider-openai",
                    name="OpenAI",
                    provider_type="openai",
                    config={"health_check_model": "gpt-health"},
                )
            )
        )

        response = self.client.post("/model-providers/provider-openai/test", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target_type"], "model_provider")
        self.assertEqual(payload["provider"], "openai")
        self.assertEqual(payload["model"], "gpt-health")

    def test_model_provider_models_endpoint_returns_curated_fallback(self) -> None:
        self._run(
            self.context.model_provider_repo.create(
                ModelProviderDefinition(
                    id="provider-openai",
                    name="OpenAI",
                    provider_type="openai",
                )
            )
        )

        response = self.client.get("/model-providers/provider-openai/models", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["target_type"], "model_provider")
        self.assertEqual(payload["target_id"], "provider-openai")
        self.assertEqual(payload["provider_type"], "openai")
        self.assertEqual(payload["source"], "curated")
        model_ids = {item["id"] for item in payload["models"]}
        self.assertIn("gpt-4.1", model_ids)

    def test_model_profile_health_endpoint(self) -> None:
        self._run(
            self.context.model_profile_repo.create(
                ModelProfileDefinition(
                    id="profile-openai",
                    name="OpenAI Profile",
                    provider="openai",
                    model="gpt-test",
                )
            )
        )

        response = self.client.get("/model-profiles/profile-openai/health", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target_type"], "model_profile")
        self.assertEqual(payload["target_id"], "profile-openai")

    def test_model_provider_health_surfaces_codex_reauth_fields(self) -> None:
        self._run(
            self.context.model_provider_repo.create(
                ModelProviderDefinition(
                    id="provider-codex",
                    name="OpenAI Codex",
                    provider_type="openai_codex",
                    config={
                        "default_oauth_profile_id": "acct-1",
                        "auth_profiles": {
                            "acct-1": {
                                "auth_mode": "chatgpt",
                                "access_token": "stale-token",
                            }
                        },
                    },
                )
            )
        )

        response = self.client.post("/model-providers/provider-codex/test", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["auth_status"], "missing_scope")
        self.assertTrue(payload["reauthorization_required"])
        self.assertEqual(payload["auth_action"], "device_authorize")
        self.assertEqual(payload["auth_endpoint"], "/model-providers/provider-codex/device-authorize")
        self.assertEqual(payload["error_code"], "missing_model_request_scope")
        self.assertEqual(payload["health"]["auth_profile_id"], "acct-1")


if __name__ == "__main__":
    unittest.main()

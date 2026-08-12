from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app


class SetupOnboardingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.preference_path = Path(self.temp_dir.name) / "tunnel-preference.json"
        self.runtime_control_path = Path(self.temp_dir.name) / "tunnel-runtime-control.json"
        self.openvoice_root = Path(self.temp_dir.name) / "openvoice"
        self.openvoice_checkpoints = Path(self.temp_dir.name) / "checkpoints"
        self.openvoice_settings = Path(self.temp_dir.name) / "openvoice-settings.json"
        (self.openvoice_root / ".venv" / "bin").mkdir(parents=True)
        (self.openvoice_root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        (self.openvoice_checkpoints / "base_speakers" / "EN").mkdir(parents=True)
        (self.openvoice_checkpoints / "base_speakers" / "EN" / "config.json").write_text("{}")
        (self.openvoice_checkpoints / "base_speakers" / "EN" / "checkpoint.pth").write_bytes(b"model")
        self.env_patch = patch.dict(
            os.environ,
            {
                "AGENCY_TUNNEL_PREFERENCE_PATH": str(self.preference_path),
                "AGENCY_TUNNEL_RUNTIME_CONTROL_PATH": str(self.runtime_control_path),
                "AGENCY_OPENVOICE_ROOT": str(self.openvoice_root),
                "AGENCY_OPENVOICE_CHECKPOINTS_DIR": str(self.openvoice_checkpoints),
                "AGENCY_OPENVOICE_SETTINGS_PATH": str(self.openvoice_settings),
            },
        )
        self.env_patch.start()
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))

        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(bootstrap.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(login.status_code, 200)
        self.auth_headers = {"authorization": f"Bearer {login.json()['access_token']}"}

    def tearDown(self) -> None:
        self.client.close()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_setup_can_create_openai_profile_and_main_agent(self) -> None:
        profile = self.client.post(
            "/setup/model-profile",
            headers=self.auth_headers,
            json={
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "api_key": "sk-test-setup",
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_body = profile.json()
        self.assertEqual(profile_body["provider"], "setup-provider-openai")
        self.assertEqual(profile_body["model"], "gpt-4.1-mini")

        status_after_profile = self.client.get("/setup/status")
        self.assertEqual(status_after_profile.status_code, 200)
        self.assertNotIn("no_model_profiles", status_after_profile.json()["blockers"])

        main_agent = self.client.post(
            "/setup/main-agent",
            headers=self.auth_headers,
            json={
                "model_profile_id": profile_body["id"],
                "agent_name": "Main Agent",
            },
        )
        self.assertEqual(main_agent.status_code, 200)
        self.assertEqual(main_agent.json()["default_model_profile_id"], profile_body["id"])

        status_after_agent = self.client.get("/setup/status")
        self.assertEqual(status_after_agent.status_code, 200)
        body = status_after_agent.json()
        self.assertTrue(body["ready"])
        self.assertEqual(body["next_path"], "/workflows")

    def test_setup_can_create_recommended_supporting_agents(self) -> None:
        response = self.client.post(
            "/setup/recommended-agents",
            headers=self.auth_headers,
            json={
                "include_coder": True,
                "include_embedding": True,
                "include_evaluation": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["coder_agent_id"], "coder")
        self.assertEqual(body["embedding_agent_id"], "embedding")
        self.assertEqual(body["embedding_model_profile_id"], "embedding-nemotron-nano")
        self.assertEqual(body["evaluation_agent_id"], "evaluation")

    def test_setup_can_manage_optional_openvoice_voice(self) -> None:
        initial = self.client.get("/setup/openvoice", headers=self.auth_headers)
        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.json()["ready"])
        self.assertEqual(initial.json()["settings"]["default_voice"], "friendly")

        updated = self.client.put(
            "/setup/openvoice",
            headers=self.auth_headers,
            json={"default_voice": "cheerful"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["settings"]["default_voice"], "cheerful")
        self.assertTrue(self.openvoice_settings.is_file())

        setup_status = self.client.get("/setup/status")
        self.assertEqual(setup_status.status_code, 200)
        self.assertTrue(setup_status.json()["openvoice"]["optional"])
        self.assertNotIn("openvoice_not_configured", setup_status.json()["blockers"])

    def test_openvoice_diagnostic_samples_use_unique_storage_keys(self) -> None:
        audio_path = Path(self.temp_dir.name) / "sample.wav"
        audio_path.write_bytes(b"RIFFtest-audio")
        generated = {
            "status": "generated",
            "provider": "openvoice_local",
            "voice": "friendly",
            "file_path": str(audio_path),
            "content_type": "audio/wav",
        }

        with patch("app.api.routes.setup.generate_voice", return_value=generated) as generate:
            first = self.client.post("/setup/openvoice/test", headers=self.auth_headers, json={})
            second = self.client.post("/setup/openvoice/test", headers=self.auth_headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        output_names = [call.kwargs["output_name"] for call in generate.call_args_list]
        self.assertEqual(len(output_names), 2)
        self.assertNotEqual(output_names[0], output_names[1])

    def test_setup_rejects_openai_without_api_key(self) -> None:
        response = self.client.post(
            "/setup/model-profile",
            headers=self.auth_headers,
            json={
                "provider": "openai",
                "model": "gpt-4.1-mini",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("API key", response.json()["detail"])

    def test_setup_can_create_ollama_profile_without_api_key(self) -> None:
        response = self.client.post(
            "/setup/model-profile",
            headers=self.auth_headers,
            json={
                "provider": "ollama",
                "model": "llama3.1:8b",
                "base_url": "http://localhost:11434",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "setup-provider-ollama")
        self.assertEqual(body["base_url"], "http://localhost:11434")

    def test_setup_can_save_tunnel_preference_with_custom_domain(self) -> None:
        initial = self.client.get("/setup/tunnel-preference")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["provider"], "auto")

        response = self.client.put(
            "/setup/tunnel-preference",
            headers=self.auth_headers,
            json={
                "provider": "ngrok",
                "custom_domain": "https://agency.example.com/",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "ngrok")
        self.assertEqual(body["custom_domain"], "agency.example.com")
        self.assertTrue(body["requirements"]["restart_required"])
        self.assertTrue(body["requirements"]["ngrok"]["requires_reserved_domain_and_dns"])
        self.assertTrue(self.preference_path.exists())

        persisted = self.client.get("/setup/tunnel-preference")
        self.assertEqual(persisted.status_code, 200)
        self.assertEqual(persisted.json()["custom_domain"], "agency.example.com")

    def test_setup_tunnel_preference_requires_authentication(self) -> None:
        response = self.client.put(
            "/setup/tunnel-preference",
            json={"provider": "cloudflare", "custom_domain": "agency.example.com"},
        )
        self.assertEqual(response.status_code, 401)

    def test_setup_rejects_custom_domain_when_tunnel_is_disabled(self) -> None:
        response = self.client.put(
            "/setup/tunnel-preference",
            headers=self.auth_headers,
            json={"provider": "none", "custom_domain": "agency.example.com"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("requires an explicit ngrok or Cloudflare", response.json()["detail"])

    def test_setup_accepts_blank_custom_domain_for_automatic_mode(self) -> None:
        response = self.client.put(
            "/setup/tunnel-preference",
            headers=self.auth_headers,
            json={"provider": "auto", "custom_domain": "   "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "auto")
        self.assertIsNone(response.json()["custom_domain"])

    def test_setup_can_request_an_immediate_tunnel_apply(self) -> None:
        response = self.client.put(
            "/setup/tunnel-preference",
            headers=self.auth_headers,
            json={"provider": "cloudflare", "custom_domain": None, "apply_now": True},
        )

        self.assertEqual(response.status_code, 200)
        control = response.json()["runtime_control"]
        self.assertEqual(control["state"], "requested")
        self.assertEqual(control["provider"], "cloudflare")
        self.assertTrue(control["request_id"])
        self.assertTrue(self.runtime_control_path.exists())

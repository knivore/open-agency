from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.core import storage
from app.tools.cli_discovery import list_builtin_tool_definitions
from app.tools.implementations.voice import VoiceGenerateInput, _openvoice_args, generate_voice
from app.tools.implementations.voice import _resolve_reference_voice_path
from app.services.openvoice_setup import OpenVoiceSetupService
from app.tools.runtime.executor import ToolRuntimeExecutor


class VoiceToolCatalogTests(unittest.TestCase):
    def test_builtin_catalog_includes_voice_generate_tool(self) -> None:
        tools = {tool.id: tool for tool in list_builtin_tool_definitions()}

        self.assertIn("agency.voice.generate", tools)
        tool = tools["agency.voice.generate"]
        self.assertIn("voice", tool.tags)
        self.assertIn("storage", tool.tags)
        self.assertTrue(tool.security.allow_filesystem)
        self.assertFalse(tool.security.allow_network)
        self.assertFalse(tool.security.requires_approval)
        self.assertFalse(tool.security.read_only)
        self.assertEqual(tool.implementation.module, "app.tools.implementations.voice")
        self.assertEqual(tool.implementation.function, "generate_voice")


class VoiceToolBehaviorTests(unittest.TestCase):
    def test_relative_reference_voice_resolves_against_backend_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                os.environ,
                {"AGENCY_BACKEND_WORKSPACE": temp_dir},
                clear=False,
        ):
            path = _resolve_reference_voice_path("data/input/voices/reference.wav")

        self.assertEqual(path, Path(temp_dir) / "data/input/voices/reference.wav")

    def test_voice_generation_requires_ai_disclosure(self) -> None:
        with self.assertRaises(ValidationError):
            VoiceGenerateInput(text="Hello", ai_disclosure=False)

    def test_provider_must_be_supported_voice_provider(self) -> None:
        with self.assertRaises(ValidationError):
            VoiceGenerateInput(
                text="Hello",
                ai_disclosure=True,
                provider="external_provider",
            )

    def test_reference_voice_requires_consent(self) -> None:
        with self.assertRaises(ValidationError):
            VoiceGenerateInput(
                text="Hello",
                ai_disclosure=True,
                provider="openvoice_local",
                reference_voice_path="/tmp/reference.wav",
                consent_confirmed=False,
            )

    def test_openvoice_without_reference_uses_friendly_voice(self) -> None:
        request = VoiceGenerateInput(
            text="Hello",
            ai_disclosure=True,
            provider="openvoice_local",
        )

        args = _openvoice_args(request, Path("/tmp/output.wav"))

        self.assertNotIn("--reference", args)
        self.assertEqual(args[args.index("--style") + 1], "friendly")

    def test_openvoice_without_explicit_voice_uses_saved_profile_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                os.environ,
                {"AGENCY_OPENVOICE_SETTINGS_PATH": str(Path(temp_dir) / "openvoice.json")},
                clear=False,
        ):
            OpenVoiceSetupService().save_settings(default_voice="cheerful")
            request = VoiceGenerateInput(
                text="Hello",
                ai_disclosure=True,
                provider="openvoice_local",
            )
            args = _openvoice_args(request, Path("/tmp/output.wav"))

        self.assertEqual(args[args.index("--style") + 1], "cheerful")

    def test_generate_voice_dry_run_returns_storage_target(self) -> None:
        result = generate_voice(
            text="Daily lesson",
            provider="system_tts",
            output_name="lessons/today.wav",
            ai_disclosure=True,
            dry_run=True,
        )

        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["provider"], "system_tts")
        self.assertEqual(result["storage_key"], "voice/lessons/today.wav")
        self.assertIsNone(result["media_url"])
        self.assertIn("reusable voice artifact", result["next_step"])

    def test_generate_voice_with_system_tts_publishes_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir) / "storage"

            def fake_run(args, check, capture_output, text, timeout):  # noqa: ANN001
                output_path = Path(args[args.index("-w") + 1])
                output_path.write_bytes(b"RIFFfake-wav")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ), patch("app.tools.implementations.voice.shutil.which", side_effect=lambda name: "/usr/bin/espeak" if name == "espeak" else None), patch(
                    "app.tools.implementations.voice.subprocess.run",
                    side_effect=fake_run,
            ):
                result = generate_voice(
                    text="Daily lesson",
                    provider="system_tts",
                    output_name="lesson.wav",
                    ai_disclosure=True,
                    dry_run=False,
                )
                stored_path = storage.get_local_file_path("voice/lesson.wav")
                stored_bytes = Path(storage.get_local_file_path("voice/lesson.wav")).read_bytes()

            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["storage_key"], "voice/lesson.wav")
            self.assertEqual(result["file_path"], stored_path)
            self.assertEqual(result["storage_uri"], "local-storage://voice/lesson.wav")
            self.assertEqual(stored_bytes, b"RIFFfake-wav")
            self.assertIn("api/local-storage/download", result["media_url"])

    def test_generate_voice_with_openvoice_local_publishes_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir) / "storage"
            openvoice_root = Path(temp_dir) / "openvoice"
            checkpoints_dir = openvoice_root / "checkpoints"
            reference_path = Path(temp_dir) / "reference.wav"
            openvoice_root.mkdir()
            checkpoints_dir.mkdir()
            reference_path.write_bytes(b"RIFFreference")

            def fake_run(args, check, capture_output, text, timeout, cwd, env):  # noqa: ANN001
                output_path = Path(args[args.index("--output") + 1])
                output_path.write_bytes(b"RIFFopenvoice-wav")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ), patch("app.tools.implementations.voice.OPENVOICE_ROOT", openvoice_root), patch(
                    "app.tools.implementations.voice.OPENVOICE_CHECKPOINTS_DIR",
                    checkpoints_dir,
            ), patch("app.tools.implementations.voice.subprocess.run", side_effect=fake_run):
                result = generate_voice(
                    text="Daily lesson",
                    provider="openvoice_local",
                    reference_voice_path=str(reference_path),
                    output_name="lesson.wav",
                    ai_disclosure=True,
                    consent_confirmed=True,
                    dry_run=False,
                )
                stored_bytes = Path(storage.get_local_file_path("voice/lesson.wav")).read_bytes()

            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["provider"], "openvoice_local")
            self.assertEqual(result["storage_key"], "voice/lesson.wav")
            self.assertEqual(stored_bytes, b"RIFFopenvoice-wav")
            self.assertEqual(result["setup"]["openvoice_checkpoints_dir"], str(checkpoints_dir))

    def test_generate_voice_with_builtin_openvoice_voice_publishes_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir) / "storage"
            openvoice_root = Path(temp_dir) / "openvoice"
            checkpoints_dir = openvoice_root / "checkpoints"
            openvoice_root.mkdir()
            checkpoints_dir.mkdir()

            def fake_run(args, check, capture_output, text, timeout, cwd, env):  # noqa: ANN001
                self.assertNotIn("--reference", args)
                self.assertEqual(args[args.index("--style") + 1], "friendly")
                output_path = Path(args[args.index("--output") + 1])
                output_path.write_bytes(b"RIFFopenvoice-friendly")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ), patch("app.tools.implementations.voice.OPENVOICE_ROOT", openvoice_root), patch(
                    "app.tools.implementations.voice.OPENVOICE_CHECKPOINTS_DIR",
                    checkpoints_dir,
            ), patch("app.tools.implementations.voice.subprocess.run", side_effect=fake_run):
                result = generate_voice(
                    text="Daily lesson",
                    provider="openvoice_local",
                    output_name="friendly.wav",
                    ai_disclosure=True,
                    dry_run=False,
                )
                stored_bytes = Path(storage.get_local_file_path("voice/friendly.wav")).read_bytes()

            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["voice"], "friendly")
            self.assertIsNone(result["reference_voice_path"])
            self.assertFalse(result["consent_confirmed"])
            self.assertEqual(stored_bytes, b"RIFFopenvoice-friendly")

    def test_contract_runtime_previews_voice_generation(self) -> None:
        response = ToolRuntimeExecutor().run(
            "agency.voice.generate",
            {
                "text": "Daily lesson",
                "provider": "system_tts",
                "ai_disclosure": True,
                "dry_run": True,
            },
            actor="voice-test",
        )

        self.assertEqual(response.verdict, "ok")
        self.assertTrue(response.dryRun)
        self.assertEqual(response.result["status"], "preview")
        self.assertEqual(response.result["provider"], "system_tts")


if __name__ == "__main__":
    unittest.main()

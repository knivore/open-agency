from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.runtime.native.errors import ToolExecutionError
from app.tools.definitions import get_tool_catalog_specs
from app.tools.implementations.audio import transcribe_audio


class _FakeTranscriptions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, *, file, **kwargs):  # noqa: ANN001
        self.calls.append({"filename": getattr(file, "name", None), "kwargs": kwargs})
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "text": "hello from audio",
                "duration": 1.25,
            }
        )


class _FakeOpenAIClient:
    def __init__(self):
        self.audio = SimpleNamespace(transcriptions=_FakeTranscriptions())


class AudioToolTests(unittest.TestCase):
    def test_audio_tool_is_in_builtin_catalog(self):
        spec = get_tool_catalog_specs()["agency.audio.transcribe"]
        tool = spec.tool_definition

        self.assertEqual(tool.name, "transcribe_audio")
        self.assertTrue(tool.security.allow_network)
        self.assertTrue(tool.security.read_only)
        self.assertEqual(tool.implementation.module, "app.tools.implementations.audio")
        self.assertEqual(tool.implementation.function, "transcribe_audio")

    def test_transcribe_audio_calls_openai_with_file_path(self):
        previous_api_key = os.environ.get("OPENAI_API_KEY")
        fake_client = _FakeOpenAIClient()
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "sample.webm"
                path.write_bytes(b"audio")
                with patch("app.tools.implementations.audio.OpenAI", return_value=fake_client):
                    result = transcribe_audio(
                        file_path=str(path),
                        model="whisper-1",
                        language="en",
                        prompt="test terms",
                    )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["text"], "hello from audio")
            self.assertEqual(result["duration"], 1.25)
            call = fake_client.audio.transcriptions.calls[0]
            self.assertEqual(call["kwargs"]["model"], "whisper-1")
            self.assertEqual(call["kwargs"]["language"], "en")
            self.assertEqual(call["kwargs"]["prompt"], "test terms")
        finally:
            if previous_api_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous_api_key

    def test_transcribe_audio_requires_one_audio_source(self):
        previous_api_key = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            with self.assertRaises(ValueError):
                transcribe_audio()
            with self.assertRaises(ValueError):
                transcribe_audio(file_path="/tmp/audio.webm", audio_base64="YXVkaW8=")
        finally:
            if previous_api_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous_api_key

    def test_transcribe_audio_requires_openai_key(self):
        previous_api_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            with self.assertRaises(ToolExecutionError):
                transcribe_audio(audio_base64="YXVkaW8=")
        finally:
            if previous_api_key is not None:
                os.environ["OPENAI_API_KEY"] = previous_api_key


if __name__ == "__main__":
    unittest.main()

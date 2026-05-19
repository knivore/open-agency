from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes.voice import create_voice_router
from app.core.config import reset_settings_cache
from app.domain import UserDefinition
from app.services.audio import OpenAIRealtimeTranscriptionService


class _FakeRealtimeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "value": "ek_test",
            "expires_at": 1770000000,
            "session": {"id": "sess_123", "type": "transcription"},
        }


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):  # noqa: ANN001
        return None

    async def post(self, url, *, headers, json):  # noqa: ANN001
        self.posts.append({"url": url, "headers": headers, "json": json})
        return _FakeRealtimeResponse()


class VoiceApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_voice_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-voice",
                "x-agency-user-email": "voice@example.com",
            }
        )
        import asyncio

        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-voice", email="voice@example.com", display_name="Voice User")
            )
        )

    def tearDown(self):
        reset_settings_cache()

    def test_create_realtime_transcription_session(self):
        previous_api_key = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            reset_settings_cache()
            create_session = AsyncMock(
                return_value={
                    "id": "sess_123",
                    "object": "realtime.transcription_session",
                    "client_secret": {"value": "ek_test"},
                    "connection": {"websocket_url": "wss://api.openai.com/v1/realtime", "mode": "transcription"},
                }
            )
            with patch(
                "app.api.routes.voice.OpenAIRealtimeTranscriptionService.create_session",
                create_session,
            ):
                response = self.client.post(
                    "/voice/realtime/transcription-session",
                    json={"language": "en", "prompt": "Agency tool names"},
                )

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["id"], "sess_123")
            self.assertEqual(body["client_secret"]["value"], "ek_test")
            create_session.assert_awaited_once()
            kwargs = create_session.await_args.kwargs
            self.assertEqual(kwargs["model"], "whisper-1")
            self.assertEqual(kwargs["language"], "en")
            self.assertEqual(kwargs["prompt"], "Agency tool names")
            self.assertEqual(kwargs["turn_detection"]["type"], "server_vad")
        finally:
            if previous_api_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous_api_key
            reset_settings_cache()

    def test_create_realtime_transcription_session_requires_identity(self):
        app = FastAPI()
        app.include_router(create_voice_router(self.context))
        client = TestClient(app)
        response = client.post("/voice/realtime/transcription-session", json={})

        self.assertEqual(response.status_code, 401)

    def test_realtime_service_uses_current_client_secret_payload(self):
        import asyncio

        fake_client = _FakeAsyncClient()
        with patch("app.services.audio.httpx.AsyncClient", return_value=fake_client):
            result = asyncio.run(
                OpenAIRealtimeTranscriptionService(api_key="sk-test").create_session(
                    model="whisper-1",
                    input_audio_format="pcm16",
                    language="en",
                    prompt="Agency terms",
                    turn_detection={"type": "server_vad"},
                    input_audio_noise_reduction={"type": "near_field"},
                    include=["item.input_audio_transcription.logprobs"],
                )
            )

        self.assertEqual(result["value"], "ek_test")
        self.assertEqual(result["connection"]["websocket_url"], "wss://api.openai.com/v1/realtime?intent=transcription")
        post = fake_client.posts[0]
        self.assertEqual(post["url"], "https://api.openai.com/v1/realtime/client_secrets")
        self.assertEqual(post["headers"]["Authorization"], "Bearer sk-test")
        session = post["json"]["session"]
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(session["audio"]["input"]["transcription"]["model"], "whisper-1")
        self.assertEqual(session["audio"]["input"]["transcription"]["language"], "en")
        self.assertEqual(session["audio"]["input"]["transcription"]["prompt"], "Agency terms")
        self.assertEqual(session["audio"]["input"]["turn_detection"], {"type": "server_vad"})
        self.assertEqual(session["audio"]["input"]["noise_reduction"], {"type": "near_field"})
        self.assertEqual(session["include"], ["item.input_audio_transcription.logprobs"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.tools.cli_discovery import list_builtin_tool_definitions
from app.tools.implementations.speech import continue_speech, listen_speech, speak_speech


class SpeechToolCatalogTests(unittest.TestCase):
    def test_builtin_catalog_includes_generic_speech_tools(self) -> None:
        tools = {tool.id: tool for tool in list_builtin_tool_definitions()}

        self.assertIn("agency.speech.listen", tools)
        self.assertIn("agency.speech.speak", tools)
        self.assertIn("agency.speech.continue", tools)
        self.assertIn("speech", tools["agency.speech.listen"].tags)
        self.assertIn("speech", tools["agency.speech.speak"].tags)
        self.assertIn("speech", tools["agency.speech.continue"].tags)
        self.assertTrue(tools["agency.speech.listen"].security.read_only)
        self.assertTrue(tools["agency.speech.speak"].security.read_only)
        self.assertFalse(tools["agency.speech.continue"].security.read_only)


class SpeechToolBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_listen_speech_routes_through_shared_transcription_flow(self) -> None:
        with patch(
            "app.tools.implementations.speech.transcribe_audio",
            return_value={"status": "success", "text": "hello from audio"},
        ) as transcribe:
            result = listen_speech(audio_base64="YXVkaW8=")

        self.assertEqual(result["text"], "hello from audio")
        transcribe.assert_called_once()

    async def test_speak_speech_routes_through_generic_speech_service(self) -> None:
        response = type(
            "AnnouncementResponse",
            (),
            {
                "model_dump": lambda self, mode="json": {
                    "announcementId": "announce_123",
                    "status": "accepted",
                    "text": "Dinner is ready.",
                    "targetKind": "speaker",
                    "targetRef": "media_player.kitchen",
                    "channel": "smart-home",
                    "voice": "default",
                    "metadata": {"room": "kitchen"},
                }
            },
        )()
        with patch(
            "app.tools.implementations.speech.SpeechAnnouncementService.announce",
            AsyncMock(return_value=response),
        ) as announce:
            result = await speak_speech(
                text="Dinner is ready.",
                targetKind="speaker",
                targetRef="media_player.kitchen",
                channel="smart-home",
                voice="default",
                metadata={"room": "kitchen"},
            )

        self.assertEqual(result["announcementId"], "announce_123")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["targetRef"], "media_player.kitchen")
        announce.assert_awaited_once()

    async def test_continue_speech_routes_through_generic_speech_continuation_service(self) -> None:
        response = type(
            "ContinuationResponse",
            (),
            {
                "model_dump": lambda self, mode="json": {
                    "continuationId": "continue_123",
                    "status": "completed",
                    "replyText": "Turning it back on.",
                    "replySsml": None,
                    "actionsTaken": [{"entity_id": "light.porch", "service": "turn_on"}],
                    "sessionId": "sess_123",
                    "priorAnnouncementId": "announce_123",
                    "channel": "smart-home",
                    "metadata": {"source": "speaker"},
                }
            },
        )()
        with patch(
            "app.tools.implementations.speech.SpeechContinuationService.continue_response",
            AsyncMock(return_value=response),
        ) as continue_response:
            result = await continue_speech(
                responseText="Yes, turn it back on.",
                surface="speaker",
                sessionId="sess_123",
                priorAnnouncementId="announce_123",
                channel="smart-home",
                metadata={"source": "speaker"},
            )

        self.assertEqual(result["continuationId"], "continue_123")
        self.assertEqual(result["actionsTaken"][0]["service"], "turn_on")
        continue_response.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

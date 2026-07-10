from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import storage
from app.services.conversations.channel_delivery import ChannelOutboundDeliveryService
from app.tools.cli_discovery import list_builtin_tool_definitions
from app.tools.implementations.media import MediaSendInput, build_media_delivery_payload, publish_media, send_media
from app.tools.runtime.executor import ToolRuntimeExecutor


class MediaToolCatalogTests(unittest.TestCase):
    def test_builtin_catalog_includes_shared_media_send_tool(self) -> None:
        tools = {tool.id: tool for tool in list_builtin_tool_definitions()}

        self.assertIn("agency.media.send", tools)
        tool = tools["agency.media.send"]
        self.assertIn("media", tool.tags)
        self.assertIn("delivery", tool.tags)
        self.assertTrue(tool.security.allow_network)
        self.assertTrue(tool.security.allow_filesystem)
        self.assertTrue(tool.security.requires_approval)
        self.assertFalse(tool.security.read_only)
        self.assertEqual(tool.implementation.module, "app.tools.implementations.media")
        self.assertEqual(tool.implementation.function, "send_media")

    def test_builtin_catalog_includes_shared_media_publish_tool(self) -> None:
        tools = {tool.id: tool for tool in list_builtin_tool_definitions()}

        self.assertIn("agency.media.publish", tools)
        tool = tools["agency.media.publish"]
        self.assertIn("media", tool.tags)
        self.assertIn("storage", tool.tags)
        self.assertTrue(tool.security.allow_filesystem)
        self.assertTrue(tool.security.allow_network)
        self.assertTrue(tool.security.requires_approval)
        self.assertEqual(tool.implementation.module, "app.tools.implementations.media")
        self.assertEqual(tool.implementation.function, "publish_media")


class MediaToolBehaviorTests(unittest.TestCase):
    def test_publish_media_dry_run_builds_storage_url_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"audio")
            storage_root = Path(temp_dir) / "storage"
            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ):
                result = publish_media(
                    file_path=str(source),
                    storage_key="media/voice.wav",
                    dry_run=True,
                )

                self.assertEqual(result["status"], "preview")
                self.assertEqual(result["storage_key"], "media/voice.wav")
                self.assertEqual(result["storage_uri"], "local-storage://media/voice.wav")
                self.assertEqual(
                    result["media_url"],
                    "http://localhost:8000/api/local-storage/download?file=media/voice.wav",
                )
                self.assertFalse(result["provider_fetchable"])
                self.assertFalse(Path(storage.get_local_file_path("media/voice.wav")).exists())

    def test_publish_media_copies_local_file_to_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"audio")
            storage_root = Path(temp_dir) / "storage"
            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ):
                result = publish_media(
                    file_path=str(source),
                    storage_key="media/voice.wav",
                    dry_run=False,
                )

                self.assertEqual(result["status"], "published")
                self.assertEqual(Path(storage.get_local_file_path("media/voice.wav")).read_bytes(), b"audio")

    def test_send_media_builds_whatsapp_voice_payload(self) -> None:
        result = send_media(
            provider="whatsapp",
            media_type="voice",
            media_url="https://cdn.example.test/output.wav",
            destination_id="+15551234567",
            dry_run=True,
            caption="Daily update",
        )

        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["provider"], "whatsapp-cloud-api")
        self.assertEqual(result["provider_message"]["method"], "messages")
        payload = result["provider_message"]["payload"]
        self.assertEqual(payload["type"], "audio")
        self.assertEqual(payload["audio"]["link"], "https://cdn.example.test/output.wav")

    def test_send_media_warns_for_local_file_without_hosted_url(self) -> None:
        result = send_media(
            provider="telegram",
            media_type="audio",
            file_path="/tmp/output.wav",
            destination_id="12345",
            dry_run=True,
        )

        self.assertEqual(result["status"], "preview")
        self.assertTrue(any("media_url" in warning for warning in result["warnings"]))

    def test_send_media_builds_discord_file_attachment_payload(self) -> None:
        result = send_media(
            provider="discord",
            media_type="voice",
            file_path="/tmp/output.wav",
            destination_id="channel-1",
            dry_run=True,
            caption="Daily lesson",
        )

        provider_payload = result["provider_message"]["payload"]
        self.assertEqual(result["provider"], "discord-bot")
        self.assertEqual(provider_payload["channel_id"], "channel-1")
        self.assertEqual(provider_payload["file_path"], "/tmp/output.wav")
        self.assertEqual(provider_payload["filename"], "output.wav")
        self.assertIn(provider_payload["content_type"], {"audio/wav", "audio/x-wav"})
        self.assertTrue(any("Discord can upload" in warning for warning in result["warnings"]))

    def test_build_media_delivery_payload_uses_discord_image_embed(self) -> None:
        request = MediaSendInput(
            provider="discord",
            media_type="image",
            media_url="https://cdn.example.test/image.png",
            destination_id="channel-1",
            caption="Render complete",
        )

        payload = build_media_delivery_payload(request)

        self.assertEqual(payload["provider"], "discord-bot")
        provider_payload = payload["provider_message"]["payload"]
        self.assertEqual(provider_payload["channel_id"], "channel-1")
        self.assertEqual(provider_payload["embeds"][0]["image"]["url"], "https://cdn.example.test/image.png")

    def test_contract_runtime_previews_media_send(self) -> None:
        response = ToolRuntimeExecutor().run(
            "agency.media.send",
            {
                "provider": "slack",
                "media_type": "audio",
                "media_url": "https://cdn.example.test/output.wav",
                "destination_id": "C123",
                "dry_run": True,
            },
            actor="media-test",
        )

        self.assertEqual(response.verdict, "ok")
        self.assertTrue(response.dryRun)
        self.assertEqual(response.result["status"], "preview")
        self.assertEqual(response.result["provider"], "slack-app")

    def test_contract_runtime_previews_media_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"audio")
            storage_root = Path(temp_dir) / "storage"
            with patch.object(storage, "LOCAL_STORAGE_PATH", str(storage_root)), patch.dict(
                    os.environ,
                    {"ENVIRONMENT": "local"},
                    clear=False,
            ):
                response = ToolRuntimeExecutor().run(
                    "agency.media.publish",
                    {
                        "file_path": str(source),
                        "storage_key": "media/voice.wav",
                        "dry_run": True,
                    },
                    actor="media-test",
                )

        self.assertEqual(response.verdict, "ok")
        self.assertTrue(response.dryRun)
        self.assertEqual(response.result["status"], "preview")
        self.assertEqual(response.result["storage_key"], "media/voice.wav")

    def test_contract_runtime_requires_context_for_real_send(self) -> None:
        response = ToolRuntimeExecutor().run(
            "agency.media.send",
            {
                "provider": "slack",
                "media_type": "audio",
                "media_url": "https://cdn.example.test/output.wav",
                "destination_id": "C123",
                "credential_id": "cred-1",
                "owner_user_id": "user-1",
                "dry_run": False,
            },
            actor="media-test",
        )

        self.assertEqual(response.verdict, "warn")
        self.assertFalse(response.dryRun)
        self.assertEqual(response.result["status"], "requires_context")

    def test_discord_delivery_request_uses_multipart_for_file_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"audio")

            request = ChannelOutboundDeliveryService(context=None)._request_for(
                provider="discord-bot",
                token="discord-token",
                credential_metadata={},
                provider_message={
                    "method": "createMessage",
                    "payload": {
                        "channel_id": "channel-1",
                        "content": "Daily lesson",
                        "file_path": str(source),
                        "filename": "lesson.wav",
                        "content_type": "audio/wav",
                    },
                },
            )

        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"], {"Authorization": "Bot discord-token"})
        self.assertIn("payload_json", request["data"])
        self.assertEqual(request["files"]["files[0]"][0], "lesson.wav")
        self.assertEqual(request["files"]["files[0]"][1], b"audio")
        self.assertEqual(request["files"]["files[0]"][2], "audio/wav")


if __name__ == "__main__":
    unittest.main()

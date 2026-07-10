from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.cli import main


class ConnectorInstallationsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.owner_user_id = "cli-user"

    def _run_cli(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_connector_setup_command_returns_backend_setup_payload(self) -> None:
        code, stdout, stderr = self._run_cli(
            [
                "connector",
                "setup",
                "telegram",
                "--owner-user-id",
                self.owner_user_id,
                "--name",
                "CLI Telegram",
                "--json",
            ]
        )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["installation"]["provider"], "telegram-bot")
        self.assertEqual(payload["installation"]["owner_user_id"], self.owner_user_id)
        self.assertEqual(payload["installation"]["status"], "setup_pending")
        self.assertEqual(
            payload["onecli_credential_ref"],
            f"onecli://users/{self.owner_user_id}/telegram-bot/{payload['installation']['id']}",
        )
        self.assertIn("setup_url", payload)
        self.assertIn("device_code", payload)

    def test_connector_list_and_status_commands_poll_installation_state(self) -> None:
        setup_code, setup_stdout, setup_stderr = self._run_cli(
            [
                "connector",
                "setup",
                "discord",
                "--owner-user-id",
                self.owner_user_id,
                "--json",
            ]
        )
        self.assertEqual(setup_code, 0, setup_stderr)
        installation_id = json.loads(setup_stdout)["installation"]["id"]

        list_code, list_stdout, list_stderr = self._run_cli(
            ["connector", "list", "--owner-user-id", self.owner_user_id, "--json"]
        )
        self.assertEqual(list_code, 0, list_stderr)
        list_payload = json.loads(list_stdout)
        self.assertEqual([item["id"] for item in list_payload["items"]], [installation_id])

        status_code, status_stdout, status_stderr = self._run_cli(
            [
                "connector",
                "status",
                installation_id,
                "--owner-user-id",
                self.owner_user_id,
                "--json",
            ]
        )
        self.assertEqual(status_code, 0, status_stderr)
        status_payload = json.loads(status_stdout)
        self.assertEqual(status_payload["id"], installation_id)
        self.assertEqual(status_payload["status"], "setup_pending")

    def test_connector_complete_rotate_and_revoke_commands_share_installation_lifecycle(self) -> None:
        setup_code, setup_stdout, setup_stderr = self._run_cli(
            [
                "connector",
                "setup",
                "discord",
                "--owner-user-id",
                self.owner_user_id,
                "--metadata-json",
                '{"workspace":"ops"}',
                "--json",
            ]
        )
        self.assertEqual(setup_code, 0, setup_stderr)
        installation_id = json.loads(setup_stdout)["installation"]["id"]

        complete_code, complete_stdout, complete_stderr = self._run_cli(
            [
                "connector",
                "complete",
                installation_id,
                "--owner-user-id",
                self.owner_user_id,
                "--metadata-json",
                '{"application_id":"app-123","bot_user_id":"bot-123","default_guild_id":"guild-123","webhook_public_key":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}',
                "--runtime-secret-value",
                "discord-runtime-secret",
                "--json",
            ]
        )
        self.assertEqual(complete_code, 0, complete_stderr)
        self.assertEqual(json.loads(complete_stdout)["status"], "active")

    def test_connector_complete_accepts_runtime_secret_value_for_direct_transport(self) -> None:
        setup_code, setup_stdout, setup_stderr = self._run_cli(
            [
                "connector",
                "setup",
                "telegram",
                "--owner-user-id",
                self.owner_user_id,
                "--json",
            ]
        )
        self.assertEqual(setup_code, 0, setup_stderr)
        installation_id = json.loads(setup_stdout)["installation"]["id"]

        complete_code, complete_stdout, complete_stderr = self._run_cli(
            [
                "connector",
                "complete",
                installation_id,
                "--owner-user-id",
                self.owner_user_id,
                "--metadata-json",
                '{"bot_user_id":"telegram-bot","bot_username":"agency_bot"}',
                "--runtime-secret-value",
                "telegram-runtime-secret",
                "--json",
            ]
        )

        self.assertEqual(complete_code, 0, complete_stderr)
        complete_payload = json.loads(complete_stdout)
        self.assertEqual(complete_payload["status"], "active")
        self.assertEqual(complete_payload["provider"], "telegram-bot")
        self.assertEqual(
            complete_payload["onecli_credential_ref"],
            f"onecli://users/{self.owner_user_id}/telegram-bot/{installation_id}",
        )
        self.assertEqual(complete_payload["metadata"]["bot_username"], "agency_bot")

        rotate_code, rotate_stdout, rotate_stderr = self._run_cli(
            [
                "connector",
                "rotate",
                installation_id,
                "--owner-user-id",
                self.owner_user_id,
                "--json",
            ]
        )
        self.assertEqual(rotate_code, 0, rotate_stderr)
        rotation_payload = json.loads(rotate_stdout)
        self.assertEqual(rotation_payload["installation"]["status"], "rotation_required")
        self.assertEqual(rotation_payload["installation"]["id"], installation_id)

        revoke_code, revoke_stdout, revoke_stderr = self._run_cli(
            [
                "connector",
                "revoke",
                installation_id,
                "--owner-user-id",
                self.owner_user_id,
                "--json",
            ]
        )
        self.assertEqual(revoke_code, 0, revoke_stderr)
        self.assertEqual(json.loads(revoke_stdout)["status"], "revoked")

    def test_connector_setup_command_rejects_raw_secret_metadata_shape(self) -> None:
        code, stdout, stderr = self._run_cli(
            [
                "connector",
                "setup",
                "telegram",
                "--owner-user-id",
                self.owner_user_id,
                "--metadata-json",
                "[]",
                "--json",
            ]
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Connector metadata must be a JSON object", stderr)


if __name__ == "__main__":
    unittest.main()

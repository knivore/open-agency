from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app


class OneCLIIdentityMappingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-onecli-owner",
            "x-agency-user-email": "owner-onecli@example.com",
        }
        self.other_headers = {
            "x-agency-user-id": "user-onecli-other",
            "x-agency-user-email": "other-onecli@example.com",
        }
        self.admin_headers = {
            "x-agency-user-id": "user-onecli-admin",
            "x-agency-user-email": "admin-onecli@example.com",
        }
        self.client.post(
            "/users/sync",
            json={
                "id": "user-onecli-owner",
                "email": "owner-onecli@example.com",
                "display_name": "OneCLI Owner",
            },
        )
        self.client.post(
            "/users/sync",
            json={
                "id": "user-onecli-other",
                "email": "other-onecli@example.com",
                "display_name": "OneCLI Other",
            },
        )
        self.client.post(
            "/users/sync",
            json={
                "id": "user-onecli-admin",
                "email": "admin-onecli@example.com",
                "display_name": "OneCLI Admin",
                "roles": ["admin"],
            },
        )

    def test_mapping_crud_is_owner_scoped_and_redacts_token_ref(self) -> None:
        create_response = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-owner-default",
                "name": "Owner Default",
                "onecli_agent_id": "onecli-agent-owner",
                "agent_token_secret_ref": "env://ONECLI_AGENT_OWNER_TOKEN",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertEqual(created["owner_user_id"], "user-onecli-owner")
        self.assertEqual(created["onecli_agent_id"], "onecli-agent-owner")
        self.assertTrue(created["agent_token_secret_ref_configured"])
        self.assertNotIn("agent_token_secret_ref", created)
        self.assertNotIn("ONECLI_AGENT_OWNER_TOKEN", str(created))
        self.assertEqual(created["metadata"]["onecli_rule_profile"]["id"], "agency-default-user-rules")
        self.assertEqual(created["metadata"]["onecli_rule_profile"]["status"], "pending_onecli_bootstrap")
        self.assertIn("block-gmail-message-delete", created["metadata"]["onecli_rule_profile"]["rule_ids"])

        owner_list = self.client.get("/onecli/identity-mappings", headers=self.owner_headers)
        self.assertEqual(owner_list.status_code, 200)
        self.assertEqual([item["id"] for item in owner_list.json()["items"]], ["mapping-owner-default"])

        other_get = self.client.get("/onecli/identity-mappings/mapping-owner-default", headers=self.other_headers)
        self.assertEqual(other_get.status_code, 404)

        update_response = self.client.put(
            "/onecli/identity-mappings/mapping-owner-default",
            headers=self.owner_headers,
            json={"name": "Owner Default Updated"},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["name"], "Owner Default Updated")

        disable_response = self.client.delete(
            "/onecli/identity-mappings/mapping-owner-default",
            headers=self.owner_headers,
        )
        self.assertEqual(disable_response.status_code, 200)
        self.assertTrue(disable_response.json()["disabled"])

        owner_list_after_disable = self.client.get("/onecli/identity-mappings", headers=self.owner_headers)
        self.assertEqual(owner_list_after_disable.status_code, 200)
        self.assertEqual(owner_list_after_disable.json()["items"], [])

    def test_mapping_cannot_be_created_for_another_user(self) -> None:
        response = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-cross-owner",
                "owner_user_id": "user-onecli-other",
                "name": "Cross Owner",
                "onecli_agent_id": "onecli-agent-cross",
                "agent_token_secret_ref": "env://ONECLI_AGENT_CROSS_TOKEN",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("current user", response.json()["detail"])

    def test_onecli_agent_identity_cannot_be_registered_by_two_users(self) -> None:
        first = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-shared-agent-owner",
                "name": "Shared Agent Owner",
                "onecli_agent_id": "onecli-agent-shared",
                "agent_token_secret_ref": "env://ONECLI_AGENT_OWNER_TOKEN",
            },
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/onecli/identity-mappings",
            headers=self.other_headers,
            json={
                "id": "mapping-shared-agent-other",
                "name": "Shared Agent Other",
                "onecli_agent_id": "onecli-agent-shared",
                "agent_token_secret_ref": "env://ONECLI_AGENT_OTHER_TOKEN",
            },
        )

        self.assertEqual(second.status_code, 400)
        self.assertIn("another Agency user", second.json()["detail"])

    def test_default_rule_profile_is_visible_and_token_free(self) -> None:
        response = self.client.get("/onecli/rule-profiles/default", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        profile = response.json()
        self.assertEqual(profile["id"], "agency-default-user-rules")
        self.assertGreaterEqual(len(profile["rules"]), 12)

        rules_by_id = {item["id"]: item for item in profile["rules"]}
        self.assertEqual(rules_by_id["block-gmail-message-delete"]["action"], "block")
        self.assertEqual(rules_by_id["block-gmail-message-delete"]["method"], "DELETE")
        self.assertEqual(rules_by_id["block-stripe-production-payment-mutation"]["host_pattern"], "api.stripe.com")
        self.assertEqual(rules_by_id["rate-limit-slack-post-message"]["action"], "rate_limit")
        self.assertEqual(rules_by_id["rate-limit-slack-post-message"]["rate_limit_count"], 10)
        self.assertEqual(rules_by_id["rate-limit-slack-post-message"]["rate_limit_window"], "hour")
        self.assertFalse(rules_by_id["approval-gmail-send"]["default_enabled"])
        self.assertNotIn("secret_ref", str(profile))
        self.assertNotIn("env://", str(profile))

        admin_response = self.client.get("/onecli/admin/rule-profiles/default", headers=self.admin_headers)
        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(admin_response.json()["id"], "agency-default-user-rules")

        forbidden = self.client.get("/onecli/admin/rule-profiles/default", headers=self.owner_headers)
        self.assertEqual(forbidden.status_code, 403)

    def test_admin_mapping_routes_manage_users_and_audit_without_secret_refs(self) -> None:
        forbidden = self.client.get("/onecli/admin/identity-mappings", headers=self.owner_headers)
        self.assertEqual(forbidden.status_code, 403)

        create_response = self.client.post(
            "/onecli/admin/users/user-onecli-other/identity-mappings",
            headers=self.admin_headers,
            json={
                "id": "mapping-admin-created",
                "name": "Admin Created",
                "onecli_agent_id": "onecli-agent-admin-created",
                "agent_token_secret_ref": "env://ONECLI_ADMIN_CREATED_TOKEN",
                "workflow_id": "workflow-admin-created",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertEqual(created["owner_user_id"], "user-onecli-other")
        self.assertTrue(created["agent_token_secret_ref_configured"])
        self.assertNotIn("agent_token_secret_ref", created)
        self.assertNotIn("ONECLI_ADMIN_CREATED_TOKEN", str(created))
        self.assertEqual(created["metadata"]["onecli_rule_profile"]["id"], "agency-default-user-rules")

        owner_list = self.client.get("/onecli/identity-mappings", headers=self.other_headers)
        self.assertEqual(owner_list.status_code, 200)
        self.assertEqual([item["id"] for item in owner_list.json()["items"]], ["mapping-admin-created"])

        update_response = self.client.put(
            "/onecli/admin/identity-mappings/mapping-admin-created",
            headers=self.admin_headers,
            json={"name": "Admin Updated", "owner_user_id": "user-onecli-owner"},
        )
        self.assertEqual(update_response.status_code, 400)
        self.assertIn("cannot be moved", update_response.json()["detail"])

        update_response = self.client.put(
            "/onecli/admin/identity-mappings/mapping-admin-created",
            headers=self.admin_headers,
            json={"name": "Admin Updated"},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["name"], "Admin Updated")

        admin_list = self.client.get("/onecli/admin/identity-mappings", headers=self.admin_headers)
        self.assertEqual(admin_list.status_code, 200)
        self.assertEqual([item["id"] for item in admin_list.json()["items"]], ["mapping-admin-created"])

        disable_response = self.client.delete(
            "/onecli/admin/identity-mappings/mapping-admin-created",
            headers=self.admin_headers,
        )
        self.assertEqual(disable_response.status_code, 200)
        self.assertTrue(disable_response.json()["disabled"])

        admin_list_after_disable = self.client.get("/onecli/admin/identity-mappings", headers=self.admin_headers)
        self.assertEqual(admin_list_after_disable.status_code, 200)
        disabled_item = admin_list_after_disable.json()["items"][0]
        self.assertEqual(disabled_item["status"], "disabled")

        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        lifecycle = [
            item for item in actions
            if item["action"].startswith("onecli.identity_mapping.")
        ]
        self.assertEqual(
            [item["action"] for item in lifecycle],
            [
                "onecli.identity_mapping.created",
                "onecli.identity_mapping.updated",
                "onecli.identity_mapping.disabled",
            ],
        )
        self.assertTrue(all(item["admin"] for item in lifecycle))
        self.assertTrue(all(item["actor_user_id"] == "user-onecli-admin" for item in lifecycle))
        self.assertNotIn("ONECLI_ADMIN_CREATED_TOKEN", str(lifecycle))
        self.assertNotIn("env://", str(lifecycle))

    def test_admin_cannot_reuse_onecli_agent_identity_across_users(self) -> None:
        first = self.client.post(
            "/onecli/admin/users/user-onecli-owner/identity-mappings",
            headers=self.admin_headers,
            json={
                "id": "mapping-admin-owner",
                "name": "Admin Owner",
                "onecli_agent_id": "onecli-agent-admin-shared",
                "agent_token_secret_ref": "env://ONECLI_AGENT_OWNER_TOKEN",
            },
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/onecli/admin/users/user-onecli-other/identity-mappings",
            headers=self.admin_headers,
            json={
                "id": "mapping-admin-other",
                "name": "Admin Other",
                "onecli_agent_id": "onecli-agent-admin-shared",
                "agent_token_secret_ref": "env://ONECLI_AGENT_OTHER_TOKEN",
            },
        )

        self.assertEqual(second.status_code, 400)
        self.assertIn("another Agency user", second.json()["detail"])

    def test_disabled_user_sync_disables_active_onecli_mappings(self) -> None:
        create_response = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-disabled-user",
                "name": "Disabled User Mapping",
                "onecli_agent_id": "onecli-agent-disabled-user",
                "agent_token_secret_ref": "env://ONECLI_DISABLED_USER_TOKEN",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        sync_response = self.client.post(
            "/users/sync",
            json={
                "id": "user-onecli-owner",
                "email": "owner-onecli@example.com",
                "display_name": "OneCLI Owner",
                "status": "disabled",
            },
        )
        self.assertEqual(sync_response.status_code, 200)

        admin_list = self.client.get("/onecli/admin/identity-mappings", headers=self.admin_headers)
        self.assertEqual(admin_list.status_code, 200)
        item = admin_list.json()["items"][0]
        self.assertEqual(item["id"], "mapping-disabled-user")
        self.assertEqual(item["status"], "disabled")

        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        disabled_actions = [
            item for item in actions
            if item["action"] == "onecli.identity_mapping.disabled"
            and item["mapping_id"] == "mapping-disabled-user"
        ]
        self.assertTrue(disabled_actions)
        self.assertEqual(disabled_actions[-1]["reason"], "user_disabled")
        self.assertNotIn("ONECLI_DISABLED_USER_TOKEN", str(disabled_actions))

    def test_admin_user_delete_disables_active_onecli_mappings(self) -> None:
        create_response = self.client.post(
            "/onecli/identity-mappings",
            headers=self.other_headers,
            json={
                "id": "mapping-deleted-user",
                "name": "Deleted User Mapping",
                "onecli_agent_id": "onecli-agent-deleted-user",
                "agent_token_secret_ref": "env://ONECLI_DELETED_USER_TOKEN",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        delete_response = self.client.delete("/users/user-onecli-other", headers=self.admin_headers)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["status"], "disabled")

        item = asyncio.run(
            self.context.onecli_identity_mapping_repo.get("mapping-deleted-user", include_deleted=True)
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.status.value, "disabled")

        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        disabled_actions = [
            item for item in actions
            if item["action"] == "onecli.identity_mapping.disabled"
            and item["mapping_id"] == "mapping-deleted-user"
        ]
        self.assertTrue(disabled_actions)
        self.assertEqual(disabled_actions[-1]["reason"], "user_deleted")
        self.assertEqual(disabled_actions[-1]["actor_user_id"], "user-onecli-admin")

    def test_admin_can_disable_user_and_workflow_mappings_as_kill_switches(self) -> None:
        owner_default = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-owner-kill-switch",
                "name": "Owner Kill Switch",
                "onecli_agent_id": "onecli-agent-owner-kill-switch",
                "agent_token_secret_ref": "env://ONECLI_OWNER_KILL_SWITCH_TOKEN",
            },
        )
        self.assertEqual(owner_default.status_code, 200)
        workflow_mapping = self.client.post(
            "/onecli/admin/users/user-onecli-other/identity-mappings",
            headers=self.admin_headers,
            json={
                "id": "mapping-workflow-kill-switch",
                "name": "Workflow Kill Switch",
                "onecli_agent_id": "onecli-agent-workflow-kill-switch",
                "agent_token_secret_ref": "env://ONECLI_WORKFLOW_KILL_SWITCH_TOKEN",
                "workflow_id": "workflow-kill-switch",
            },
        )
        self.assertEqual(workflow_mapping.status_code, 200)

        user_disable = self.client.delete(
            "/onecli/admin/users/user-onecli-owner/identity-mappings",
            headers=self.admin_headers,
        )
        self.assertEqual(user_disable.status_code, 200)
        self.assertEqual(user_disable.json()["count"], 1)
        self.assertEqual(user_disable.json()["ids"], ["mapping-owner-kill-switch"])

        workflow_disable = self.client.delete(
            "/onecli/admin/workflows/workflow-kill-switch/identity-mappings",
            headers=self.admin_headers,
        )
        self.assertEqual(workflow_disable.status_code, 200)
        self.assertEqual(workflow_disable.json()["count"], 1)
        self.assertEqual(workflow_disable.json()["ids"], ["mapping-workflow-kill-switch"])

        admin_list = self.client.get("/onecli/admin/identity-mappings", headers=self.admin_headers)
        statuses = {item["id"]: item["status"] for item in admin_list.json()["items"]}
        self.assertEqual(statuses["mapping-owner-kill-switch"], "disabled")
        self.assertEqual(statuses["mapping-workflow-kill-switch"], "disabled")

        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        disabled_reasons = {
            item["mapping_id"]: item.get("reason")
            for item in actions
            if item["action"] == "onecli.identity_mapping.disabled"
        }
        self.assertEqual(disabled_reasons["mapping-owner-kill-switch"], "admin_user_kill_switch")
        self.assertEqual(disabled_reasons["mapping-workflow-kill-switch"], "admin_workflow_kill_switch")
        self.assertNotIn("ONECLI_OWNER_KILL_SWITCH_TOKEN", str(actions))
        self.assertNotIn("ONECLI_WORKFLOW_KILL_SWITCH_TOKEN", str(actions))


if __name__ == "__main__":
    unittest.main()

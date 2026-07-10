"""Environment-backed target registry for outbound webhook delivery."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .schemas import ResolvedWebhookTarget, WebhookAuthType, WebhookTarget


class WebhookTargetRegistry:
    def __init__(
            self,
            targets: list[WebhookTarget] | None = None,
            *,
            environ: Mapping[str, str] | None = None,
    ):
        self._targets = {target.target: target for target in targets or []}
        self.environ = environ or os.environ

    def register(self, target: WebhookTarget) -> None:
        self._targets[target.target] = target

    def get(self, target: str) -> WebhookTarget | None:
        return self._targets.get(target)

    def resolve(self, target: str) -> ResolvedWebhookTarget:
        definition = self.get(target)
        if definition is None:
            raise KeyError(f"Webhook target '{target}' is not registered")
        url = self._required_env(definition.url_env, "webhook URL")
        token = None
        secret = None
        if definition.auth_type == WebhookAuthType.BEARER:
            token = self._required_env(definition.token_env or "", "bearer token")
        if definition.auth_type == WebhookAuthType.HMAC:
            secret = self._required_env(definition.secret_env or "", "HMAC secret")
        return ResolvedWebhookTarget(definition=definition, url=url, token=token, secret=secret)

    def _required_env(self, name: str, label: str) -> str:
        if not name:
            raise ValueError(f"Missing env var name for {label}")
        value = self.environ.get(name)
        if value is None or not value.strip():
            raise ValueError(f"Environment variable '{name}' is required for {label}")
        return value.strip()

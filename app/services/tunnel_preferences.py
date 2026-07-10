"""Persist non-secret local tunnel preferences for launcher and setup UI use."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, ValidationError, field_validator
from typing import Literal
from urllib.parse import urlsplit

TunnelProvider = Literal["auto", "none", "ngrok", "cloudflare"]


class TunnelPreference(BaseModel):
    provider: TunnelProvider = "auto"
    custom_domain: str | None = None
    source: str = "browser"
    updated_at: datetime

    @field_validator("custom_domain")
    @classmethod
    def _normalize_custom_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if not candidate:
            return None
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("Custom domain must be an HTTPS hostname without a path.")
        if parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment:
            raise ValueError("Custom domain must not include a port, query, or fragment.")
        hostname = parsed.hostname.rstrip(".")
        hostname_label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
        if "." not in hostname or any(
                not hostname_label.fullmatch(label) for label in hostname.split(".")
        ):
            raise ValueError("Custom domain must be a valid fully qualified hostname.")
        return hostname

    @field_validator("source")
    @classmethod
    def _normalize_source(cls, value: str) -> str:
        return value.strip().lower().replace("_", "-") or "browser"


def resolve_tunnel_preference_path() -> Path:
    explicit = os.getenv("AGENCY_TUNNEL_PREFERENCE_PATH")
    if explicit:
        return Path(explicit).expanduser()

    for env_name in ("AGENCY_BACKEND_WORKSPACE", "AGENCY_BACKEND_HOST_WORKSPACE"):
        candidate = os.getenv(env_name)
        if candidate and Path(candidate).expanduser().is_dir():
            return Path(candidate).expanduser() / ".agency" / "tunnel-preference.json"

    return Path.cwd() / ".agency" / "tunnel-preference.json"


@dataclass(slots=True)
class TunnelPreferenceService:
    path: Path | None = None

    @property
    def preference_path(self) -> Path:
        return self.path or resolve_tunnel_preference_path()

    def get(self) -> TunnelPreference:
        path = self.preference_path
        if not path.exists():
            return TunnelPreference(
                provider="auto",
                custom_domain=None,
                source="launcher-default",
                updated_at=datetime.now(timezone.utc),
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TunnelPreference.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError):
            return TunnelPreference(
                provider="auto",
                custom_domain=None,
                source="invalid-preference-fallback",
                updated_at=datetime.now(timezone.utc),
            )

    def save(
            self,
            *,
            provider: TunnelProvider,
            custom_domain: str | None,
            source: str = "browser",
    ) -> TunnelPreference:
        preference = TunnelPreference(
            provider=provider,
            custom_domain=custom_domain,
            source=source,
            updated_at=datetime.now(timezone.utc),
        )
        if preference.provider in {"auto", "none"} and preference.custom_domain:
            raise ValueError("A custom domain requires an explicit ngrok or Cloudflare Tunnel provider.")
        path = self.preference_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(preference.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return preference

    @staticmethod
    def requirements(preference: TunnelPreference) -> dict[str, object]:
        custom_domain = preference.custom_domain
        return {
            "restart_required": True,
            "custom_domain_requires_provider_setup": bool(custom_domain),
            "ngrok": {
                "custom_domain_supported": True,
                "requires_reserved_domain_and_dns": bool(custom_domain),
                "requires_paid_plan_for_custom_domain": bool(custom_domain),
            },
            "cloudflare": {
                "custom_domain_supported": True,
                "requires_managed_tunnel_token": bool(custom_domain),
                "requires_published_application_route": bool(custom_domain),
                "managed_tunnel_token_configured": bool(os.getenv("AGENCY_CLOUDFLARE_TUNNEL_TOKEN")),
            },
        }

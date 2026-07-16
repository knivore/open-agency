from __future__ import annotations

from html import escape
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.core import storage
from app.integrations.connectors import normalize_connector_provider_key
from app.runtime.native.errors import ToolExecutionError


MediaProvider = Literal[
    "telegram",
    "telegram-bot",
    "discord",
    "discord-bot",
    "slack",
    "slack-app",
    "microsoft-teams",
    "teams",
    "whatsapp",
    "whatsapp-cloud-api",
]
MediaKind = Literal["image", "audio", "voice", "video", "document"]


class MediaPublishInput(BaseModel):
    file_path: str = Field(description="Local media file path to copy into Agency storage.")
    storage_key: str | None = Field(
        default=None,
        description="Optional exact Agency storage key. When omitted, a unique key is generated under storage_key_prefix.",
    )
    storage_key_prefix: str = Field(
        default="media",
        description="Storage key prefix used when storage_key is omitted.",
    )
    filename: str | None = Field(
        default=None,
        description="Optional filename to use in generated storage keys.",
    )
    content_type: str | None = Field(
        default=None,
        description="Optional MIME type. When omitted, it is guessed from the filename.",
    )
    overwrite: bool = Field(default=False, description="Whether to replace an existing object at storage_key.")
    dry_run: bool = Field(default=True, description="Preview the storage key and URL without copying the file.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional audit or workflow metadata.")


class MediaSendInput(BaseModel):
    provider: MediaProvider = Field(description="Connector or chat provider that should receive the media.")
    media_type: MediaKind = Field(description="Media kind to send.")
    media_url: str | None = Field(
        default=None,
        description="Public or provider-fetchable media URL. Required for real delivery except Discord file uploads.",
    )
    file_path: str | None = Field(
        default=None,
        description="Local media file path for provenance, preview, or Discord attachment upload.",
    )
    text: str | None = Field(default=None, description="Optional message text to send with the media.")
    caption: str | None = Field(default=None, description="Optional media caption.")
    filename: str | None = Field(default=None, description="Optional filename for document-style media.")
    mime_type: str | None = Field(default=None, description="Optional MIME type for audit and downstream adapters.")
    destination_id: str | None = Field(
        default=None,
        description=(
            "Provider destination: Telegram chat id, Discord/Slack/Teams channel id, or WhatsApp recipient."
        ),
    )
    team_id: str | None = Field(default=None, description="Microsoft Teams team id when provider is Teams.")
    credential_id: str | None = Field(default=None, description="Agency credential or connector installation id.")
    owner_user_id: str | None = Field(default=None, description="Owner user id for resolving the credential.")
    dry_run: bool = Field(default=True, description="Preview the provider payload without sending it.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional audit or workflow metadata.")

    @model_validator(mode="after")
    def _requires_media_reference(self) -> "MediaSendInput":
        if not (self.media_url or self.file_path):
            raise ValueError("Provide media_url or file_path.")
        return self


def publish_media(
        *,
        file_path: str,
        storage_key: str | None = None,
        storage_key_prefix: str = "media",
        filename: str | None = None,
        content_type: str | None = None,
        overwrite: bool = False,
        dry_run: bool = True,
        metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = MediaPublishInput.model_validate(
        {
            "file_path": file_path,
            "storage_key": storage_key,
            "storage_key_prefix": storage_key_prefix,
            "filename": filename,
            "content_type": content_type,
            "overwrite": overwrite,
            "dry_run": dry_run,
            "metadata": metadata or {},
        }
    )
    source_path = _source_file_path(request.file_path)
    output_filename = _safe_storage_filename(request.filename or source_path.name)
    resolved_content_type = request.content_type or mimetypes.guess_type(output_filename)[0] or "application/octet-stream"
    resolved_storage_key = _publish_storage_key(request, output_filename)
    storage_uri = _storage_uri_for(resolved_storage_key)
    media_url = storage.generate_presigned_url("download", resolved_storage_key, resolved_content_type)
    local_environment = storage.is_local_environment()
    result = {
        "status": "preview" if request.dry_run else "published",
        "file_path": str(source_path),
        "storage_key": resolved_storage_key,
        "storage_uri": storage_uri,
        "media_url": media_url,
        "filename": output_filename,
        "content_type": resolved_content_type,
        "provider_fetchable": not local_environment,
        "warnings": _publish_warnings(media_url, local_environment=local_environment),
        "metadata": request.metadata,
    }
    if request.dry_run:
        return result

    if local_environment:
        target_path = storage.get_local_file_path(resolved_storage_key)
        if Path(target_path).exists() and not request.overwrite:
            raise ToolExecutionError(f"Storage key already exists: {resolved_storage_key}.")
        storage.mock_upload_to_local(str(source_path), target_path)
        return result

    _upload_to_object_storage(
        source_path=source_path,
        storage_key=resolved_storage_key,
        content_type=resolved_content_type,
        overwrite=request.overwrite,
    )
    return result


def send_media(
        *,
        provider: str,
        media_type: str,
        media_url: str | None = None,
        file_path: str | None = None,
        text: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
        destination_id: str | None = None,
        team_id: str | None = None,
        credential_id: str | None = None,
        owner_user_id: str | None = None,
        dry_run: bool = True,
        metadata: dict[str, Any] | None = None,
        tool_context=None,  # noqa: ANN001
) -> dict[str, Any] | Awaitable[dict[str, Any]]:
    request = _request_from_kwargs(
        provider=provider,
        media_type=media_type,
        media_url=media_url,
        file_path=file_path,
        text=text,
        caption=caption,
        filename=filename,
        mime_type=mime_type,
        destination_id=destination_id,
        team_id=team_id,
        credential_id=credential_id,
        owner_user_id=owner_user_id,
        dry_run=dry_run,
        metadata=metadata,
    )
    payload = build_media_delivery_payload(request)
    if request.dry_run:
        return {"status": "preview", **payload}
    runtime_executor = getattr(tool_context, "api_tool_runtime_executor", None)
    runtime_context = getattr(runtime_executor, "context", None)
    if runtime_context is not None:
        # Native Python tools receive the API runtime through ToolExecutionContext.
        # Reuse it for real delivery instead of degrading to requires_context.
        return send_media_with_context(
            runtime_context,
            provider=provider,
            media_type=media_type,
            media_url=media_url,
            file_path=file_path,
            text=text,
            caption=caption,
            filename=filename,
            mime_type=mime_type,
            destination_id=destination_id,
            team_id=team_id,
            credential_id=credential_id,
            owner_user_id=owner_user_id,
            dry_run=dry_run,
            metadata=metadata,
        )
    return {
        "status": "requires_context",
        **payload,
        "error": "Real media delivery requires Agency API context; run through the context-backed tool runtime.",
    }


async def send_media_with_context(context: Any, **kwargs: Any) -> dict[str, Any]:
    from app.services.conversations.channel_delivery import ChannelOutboundDeliveryService

    request = _request_from_kwargs(**kwargs)
    payload = build_media_delivery_payload(request)
    if request.dry_run:
        return {"status": "preview", **payload}

    missing = [
        name
        for name, value in {
            "credential_id": request.credential_id,
            "owner_user_id": request.owner_user_id,
        }.items()
        if not str(value or "").strip()
    ]
    if missing:
        raise ToolExecutionError(f"Real media delivery requires: {', '.join(missing)}.")
    if not request.media_url and not (payload["provider"] == "discord-bot" and request.file_path):
        raise ToolExecutionError(
            "Real media delivery requires media_url unless Discord is uploading a local file_path attachment."
        )

    delivery = await ChannelOutboundDeliveryService(context).deliver_for_owner(
        provider=payload["provider"],
        credential_id=str(request.credential_id),
        owner_user_id=str(request.owner_user_id),
        provider_outbound_messages=[payload["provider_message"]],
    )
    return {
        "status": "sent" if delivery and delivery.get("ok") else "failed",
        **payload,
        "delivery": delivery,
    }


def build_media_delivery_payload(request: MediaSendInput) -> dict[str, Any]:
    normalized_provider = _normalized_provider(request.provider)
    warnings = _warnings_for(request)
    provider_message = _provider_message(normalized_provider, request)
    return {
        "provider": normalized_provider,
        "media": {
            "type": request.media_type,
            "media_url": request.media_url,
            "file_path": request.file_path,
            "filename": request.filename or _filename_from(request),
            "mime_type": request.mime_type,
        },
        "destination": {
            "destination_id": request.destination_id,
            "team_id": request.team_id,
        },
        "provider_message": provider_message,
        "warnings": warnings,
        "metadata": request.metadata,
    }


def _request_from_kwargs(**kwargs: Any) -> MediaSendInput:
    return MediaSendInput.model_validate({key: value for key, value in kwargs.items() if value is not None})


def _source_file_path(file_path: str) -> Path:
    try:
        source_path = Path(file_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ToolExecutionError(f"Media file does not exist: {file_path}.") from exc
    if not source_path.is_file():
        raise ToolExecutionError(f"Media path is not a file: {source_path}.")
    return source_path


def _publish_storage_key(request: MediaPublishInput, filename: str) -> str:
    if request.storage_key:
        key = request.storage_key.strip()
    else:
        prefix = request.storage_key_prefix.strip().strip("/") or "media"
        key = f"{prefix}/{uuid4().hex}-{filename}"
    storage.get_local_file_path(key)
    return key


def _safe_storage_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    return name or "media.bin"


def _storage_uri_for(storage_key: str) -> str:
    if storage.is_local_environment():
        return f"local-storage://{storage_key}"
    bucket = storage.S3_BUCKET_NAME or ""
    return f"s3://{bucket}/{storage_key}" if bucket else storage_key


def _upload_to_object_storage(
        *,
        source_path: Path,
        storage_key: str,
        content_type: str,
        overwrite: bool,
) -> None:
    bucket = storage.S3_BUCKET_NAME
    if not bucket:
        raise ToolExecutionError("S3_BUCKET_NAME is required when ENVIRONMENT is not local.")
    client = storage.get_s3_client()
    if client is None:
        raise ToolExecutionError("Object storage client is unavailable.")
    if not overwrite and _s3_object_exists(client, bucket=bucket, storage_key=storage_key):
        raise ToolExecutionError(f"Storage key already exists: {storage_key}.")
    extra_args = {"ContentType": content_type} if content_type else None
    if extra_args:
        client.upload_file(str(source_path), bucket, storage_key, ExtraArgs=extra_args)
    else:
        client.upload_file(str(source_path), bucket, storage_key)


def _s3_object_exists(client: Any, *, bucket: str, storage_key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=storage_key)
    except Exception as exc:  # boto3 raises provider-specific ClientError for 404s.
        response = getattr(exc, "response", {})
        error = response.get("Error") if isinstance(response, dict) else {}
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def _normalized_provider(provider: str) -> str:
    normalized = normalize_connector_provider_key(provider) or provider.strip().lower()
    aliases = {
        "telegram": "telegram-bot",
        "discord": "discord-bot",
        "slack": "slack-app",
        "teams": "microsoft-teams",
        "whatsapp": "whatsapp-cloud-api",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {
        "telegram-bot",
        "discord-bot",
        "slack-app",
        "microsoft-teams",
        "whatsapp-cloud-api",
    }:
        raise ToolExecutionError(f"Unsupported media delivery provider '{provider}'.")
    return normalized


def _provider_message(provider: str, request: MediaSendInput) -> dict[str, Any]:
    if provider == "telegram-bot":
        return _telegram_message(request)
    if provider == "discord-bot":
        return _discord_message(request)
    if provider == "slack-app":
        return _slack_message(request)
    if provider == "microsoft-teams":
        return _teams_message(request)
    if provider == "whatsapp-cloud-api":
        return _whatsapp_message(request)
    raise ToolExecutionError(f"Unsupported media delivery provider '{provider}'.")


def _telegram_message(request: MediaSendInput) -> dict[str, Any]:
    chat_id = _required_destination(request, "Telegram")
    field_by_type = {
        "image": ("sendPhoto", "photo"),
        "audio": ("sendAudio", "audio"),
        "voice": ("sendVoice", "voice"),
        "video": ("sendVideo", "video"),
        "document": ("sendDocument", "document"),
    }
    method, media_field = field_by_type[request.media_type]
    payload: dict[str, Any] = {"chat_id": chat_id, media_field: _media_reference(request)}
    if request.caption and request.media_type != "audio":
        payload["caption"] = request.caption
    if request.caption and request.media_type == "audio":
        payload["caption"] = request.caption
    if request.filename and request.media_type == "document":
        payload["filename"] = request.filename
    return {"method": method, "payload": payload}


def _discord_message(request: MediaSendInput) -> dict[str, Any]:
    channel_id = _required_destination(request, "Discord")
    media_url = request.media_url
    content = _message_text(request)
    payload: dict[str, Any] = {"channel_id": channel_id, "content": content}
    if request.file_path:
        payload.update(
            {
                "file_path": request.file_path,
                "filename": request.filename or _filename_from(request),
                "content_type": request.mime_type or _guess_mime_type(request),
            }
        )
        if media_url:
            payload["content"] = _join_lines(content, media_url)
    elif media_url and request.media_type == "image":
        payload["embeds"] = [{"image": {"url": media_url}, "description": request.caption or ""}]
    elif media_url:
        payload["content"] = _join_lines(content, media_url)
    return {"method": "createMessage", "payload": payload}


def _slack_message(request: MediaSendInput) -> dict[str, Any]:
    channel = _required_destination(request, "Slack")
    media_url = request.media_url
    text = _message_text(request)
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if media_url and request.media_type == "image":
        payload["blocks"] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text or request.caption or "Media"}},
            {"type": "image", "image_url": media_url, "alt_text": request.caption or request.filename or "media"},
        ]
    elif media_url:
        payload["text"] = _join_lines(text, media_url)
    return {"method": "chat.postMessage", "payload": payload}


def _teams_message(request: MediaSendInput) -> dict[str, Any]:
    if not request.team_id:
        raise ToolExecutionError("Teams media delivery requires team_id.")
    channel_id = _required_destination(request, "Teams")
    media_url = request.media_url
    label = escape(request.caption or request.filename or request.media_type)
    link = f'<a href="{escape(media_url)}">{label}</a>' if media_url else label
    content = escape(_message_text(request))
    payload = {
        "team_id": request.team_id,
        "channel_id": channel_id,
        "content_type": "html",
        "content": _join_lines(content, link),
    }
    return {"method": "sendChannelMessage", "payload": payload}


def _whatsapp_message(request: MediaSendInput) -> dict[str, Any]:
    to = _required_destination(request, "WhatsApp")
    media_url = _media_reference(request)
    media_type = "audio" if request.media_type == "voice" else request.media_type
    if media_type == "document":
        media_payload: dict[str, Any] = {"link": media_url}
        if request.filename:
            media_payload["filename"] = request.filename
        if request.caption:
            media_payload["caption"] = request.caption
    elif media_type in {"image", "video"}:
        media_payload = {"link": media_url}
        if request.caption:
            media_payload["caption"] = request.caption
    elif media_type == "audio":
        media_payload = {"link": media_url}
    else:
        raise ToolExecutionError(f"WhatsApp does not support media type '{request.media_type}'.")
    return {
        "method": "messages",
        "payload": {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: media_payload,
        },
    }


def _message_text(request: MediaSendInput) -> str:
    return (request.text or request.caption or request.filename or f"{request.media_type.title()} media").strip()


def _required_destination(request: MediaSendInput, provider_label: str) -> str:
    destination = str(request.destination_id or "").strip()
    if not destination:
        raise ToolExecutionError(f"{provider_label} media delivery requires destination_id.")
    return destination


def _required_media_url(request: MediaSendInput) -> str:
    media_url = str(request.media_url or "").strip()
    if not media_url:
        raise ToolExecutionError("This provider media payload requires media_url.")
    return media_url


def _media_reference(request: MediaSendInput) -> str:
    media_url = str(request.media_url or "").strip()
    if media_url:
        return media_url
    file_path = str(request.file_path or "").strip()
    if file_path:
        return file_path
    return _required_media_url(request)


def _filename_from(request: MediaSendInput) -> str | None:
    if request.file_path:
        return Path(request.file_path).name
    if request.media_url:
        name = Path(request.media_url.split("?", 1)[0]).name
        return name or None
    return None


def _guess_mime_type(request: MediaSendInput) -> str:
    filename = request.filename or _filename_from(request) or ""
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    fallback = {
        "image": "image/png",
        "audio": "audio/wav",
        "voice": "audio/wav",
        "video": "video/mp4",
        "document": "application/octet-stream",
    }
    return fallback[request.media_type]


def _join_lines(*lines: str | None) -> str:
    return "\n".join(line for line in (line.strip() for line in lines if line) if line)


def _warnings_for(request: MediaSendInput) -> list[str]:
    warnings: list[str] = []
    if request.file_path and not request.media_url:
        if _normalized_provider(request.provider) == "discord-bot":
            warnings.append("Discord can upload local file_path attachments during real delivery with API context.")
        else:
            warnings.append("Local file_path is preview/provenance only; real delivery requires a provider-fetchable media_url.")
    if request.media_type == "voice":
        warnings.append("Voice-message delivery depends on provider support; fallback providers send an audio link.")
    if request.media_url and not request.media_url.startswith(("http://", "https://")):
        warnings.append("media_url should be provider-fetchable; most providers require http(s).")
    return warnings


def _publish_warnings(media_url: str, *, local_environment: bool) -> list[str]:
    warnings: list[str] = []
    if local_environment:
        warnings.append(
            "Local storage URLs require the Agency API to be reachable and may not be provider-fetchable outside local tests."
        )
    if media_url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
        warnings.append("Most external providers cannot fetch localhost media URLs without a public tunnel.")
    return warnings


__all__ = [
    "MediaPublishInput",
    "MediaSendInput",
    "build_media_delivery_payload",
    "publish_media",
    "send_media",
    "send_media_with_context",
]

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _normalize_key(raw_key: str) -> bytes:
    key = raw_key.strip().encode("utf-8")
    try:
        # Accept either a ready-made Fernet key or a plain 32-byte base64 string.
        decoded = urlsafe_b64decode(key)
        if len(decoded) != 32:
            raise ValueError("Invalid Fernet key length.")
        return key
    except Exception as exc:  # pragma: no cover - defensive parsing path
        raise ValueError("AGENCY_RUNTIME_SECRET_KEY must be a valid Fernet key.") from exc


def _get_fernet() -> Fernet | None:
    settings = get_settings()
    if not settings.agency_runtime_secret_key:
        return None
    return Fernet(_normalize_key(settings.agency_runtime_secret_key))


def seal_runtime_secret(secret_value: str) -> str:
    value = secret_value.strip()
    if not value:
        raise ValueError("Runtime secret value is empty.")

    fernet = _get_fernet()
    if fernet is None:
        # Development fallback keeps the mirror usable even when no key is
        # configured, but production should always set AGENCY_RUNTIME_SECRET_KEY.
        return f"plain:{urlsafe_b64encode(value.encode('utf-8')).decode('utf-8')}"
    return f"fernet:{fernet.encrypt(value.encode('utf-8')).decode('utf-8')}"


def open_runtime_secret(sealed_value: str) -> str | None:
    value = sealed_value.strip()
    if not value:
        return None

    if value.startswith("plain:"):
        encoded = value[len("plain:"):]
        return urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8")

    if value.startswith("fernet:"):
        fernet = _get_fernet()
        if fernet is None:
            raise ValueError("AGENCY_RUNTIME_SECRET_KEY is required to read encrypted runtime secrets.")
        token = value[len("fernet:"):].encode("utf-8")
        try:
            return fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Runtime secret value could not be decrypted.") from exc

    raise ValueError("Unsupported runtime secret encoding.")

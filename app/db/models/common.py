from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType
from typing import Any

JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


class _PGVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "vector"


class VectorVariant(TypeDecorator):
    """Postgres pgvector with text fallback for SQLite/tests."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PGVector())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return "[" + ",".join(str(float(item)) for item in value) + "]"

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip().strip("[]")
            if not stripped:
                return []
            return [float(part.strip()) for part in stripped.split(",")]
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

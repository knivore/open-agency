"""add durable execution wait ledger

Revision ID: 20260713_0001_execution_waits
Revises: 20260711_0001_tool_routing_metadata
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260713_0001_execution_waits"
down_revision: str | None = "20260711_0001_tool_routing_metadata"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if has_table("execution_waits"):
        return
    op.create_table(
        "execution_waits",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("active_slot", sa.String(length=16), nullable=True, server_default="active"),
        sa.Column("correlation_key", sa.String(length=255), nullable=True),
        sa.Column("checkpoint_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("request_payload_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("policy_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("resolution_payload_json", json_type(), nullable=True),
        sa.Column("resolution_key", sa.String(length=255), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "execution_id",
            "idempotency_key",
            name="uq_execution_waits_execution_id_idempotency_key",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "active_slot",
            name="uq_execution_waits_execution_id_active_slot",
        ),
    )
    op.create_index(
        "ix_execution_waits_execution_id_status",
        "execution_waits",
        ["execution_id", "status"],
    )
    op.create_index("ix_execution_waits_status_wake_at", "execution_waits", ["status", "wake_at"])
    op.create_index("ix_execution_waits_correlation_key", "execution_waits", ["correlation_key"])


def downgrade() -> None:
    if has_table("execution_waits"):
        op.drop_table("execution_waits")

"""persist compact tool-routing metadata

Revision ID: 20260711_0001_tool_routing_metadata
Revises: 20260702_0001_durable_goal_foundations
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260711_0001_tool_routing_metadata"
down_revision: str | None = "20260702_0001_durable_goal_foundations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    op.add_column("tools", sa.Column("routing_json", json_type(), nullable=True))


def downgrade() -> None:
    op.drop_column("tools", "routing_json")

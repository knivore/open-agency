"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-05-19

Fresh bootstrap baseline for the open-source backend surface.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for table in Base.metadata.sorted_tables:
        table.create(bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        table.drop(bind, checkfirst=False)

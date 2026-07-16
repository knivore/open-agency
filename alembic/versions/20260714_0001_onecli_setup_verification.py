"""Add bounded OneCLI connector setup sessions.

Revision ID: 20260714_0001_onecli_setup_verification
Revises: 20260713_0001_execution_waits
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260714_0001_onecli_setup_verification"
down_revision: str | None = "20260713_0001_execution_waits"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "connector_installations",
        sa.Column("setup_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connector_installations",
        sa.Column("setup_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connector_installations", "setup_expires_at")
    op.drop_column("connector_installations", "setup_started_at")

"""durable goal foundations

Revision ID: 20260702_0001_durable_goal_foundations
Revises: 20260519_0001_baseline
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260702_0001_durable_goal_foundations"
down_revision: str | None = "20260519_0001_baseline"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def has_column(table_name: str, column_name: str) -> bool:
    if not has_table(table_name):
        return False
    return any(column["name"] == column_name for column in inspect(op.get_bind()).get_columns(table_name))


def has_index(table_name: str, index_name: str) -> bool:
    if not has_table(table_name):
        return False
    return any(index["name"] == index_name for index in inspect(op.get_bind()).get_indexes(table_name))


def has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in inspect(op.get_bind()).get_foreign_keys(table_name))


def create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
    if has_table(table_name) and not has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    if not has_table("goals"):
        op.create_table(
            "goals",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("objective", sa.String(), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("priority", sa.String(length=64), nullable=False, server_default="normal"),
            sa.Column("owner_actor", sa.String(length=255), nullable=True),
            sa.Column("parent_goal_id", sa.String(length=64), sa.ForeignKey("goals.id"), nullable=True),
            sa.Column("success_criteria_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("constraints_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("execution_ids_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("evidence_json", json_type(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("evaluation_json", json_type(), nullable=True),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    create_index_if_missing("ix_goals_status", "goals", ["status"])
    create_index_if_missing("ix_goals_parent_goal_id", "goals", ["parent_goal_id"])
    create_index_if_missing("ix_goals_created_at", "goals", ["created_at"])
    create_index_if_missing("ix_goals_deadline_at", "goals", ["deadline_at"])

    if has_table("executions") and not has_column("executions", "goal_id"):
        op.add_column("executions", sa.Column("goal_id", sa.String(length=64), nullable=True))
    if (
        op.get_bind().dialect.name != "sqlite"
        and has_table("executions")
        and has_table("goals")
        and not has_foreign_key("executions", "fk_executions_goal_id_goals")
    ):
        op.create_foreign_key("fk_executions_goal_id_goals", "executions", "goals", ["goal_id"], ["id"])
    create_index_if_missing("ix_executions_goal_id", "executions", ["goal_id"])


def downgrade() -> None:
    if has_table("executions"):
        if has_index("executions", "ix_executions_goal_id"):
            op.drop_index("ix_executions_goal_id", table_name="executions")
        if op.get_bind().dialect.name != "sqlite" and has_foreign_key("executions", "fk_executions_goal_id_goals"):
            op.drop_constraint("fk_executions_goal_id_goals", "executions", type_="foreignkey")
        if has_column("executions", "goal_id"):
            op.drop_column("executions", "goal_id")
    if has_table("goals"):
        op.drop_table("goals")

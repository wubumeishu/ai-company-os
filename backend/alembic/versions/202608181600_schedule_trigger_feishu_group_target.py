"""Add Feishu group delivery targets to schedules and triggers.

Background:
  001_initial_schema builds agent_schedules / agent_triggers from the current
  SQLAlchemy metadata via create_all on fresh deployments; the registered
  models already carry delivery_target_id, so the column is present there.
  This revision's original unguarded add_column therefore raised
  DuplicateColumnError when run after 001 on a truly fresh database.

Scope:
  DDL-only. Adds delivery_target_id to agent_schedules and agent_triggers.
  No data backfill.

Idempotent:
  upgrade() adds each column only when it is absent; downgrade() drops each
  column only when present. Both paths are no-ops in the opposite state, so
  already-applied environments and 001-created fresh schemas both pass.

Revision ID: f065_feishu_group_target
Revises: f064_tool_call_tenants
Create Date: 2026-08-18 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f065_feishu_group_target"
down_revision: str | Sequence[str] | None = "f064_tool_call_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns(bind: sa.engine.Connection) -> dict[str, set[str]]:
    """Column names per table in the current schema state."""
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in ("agent_schedules", "agent_triggers")
        if table in tables
    }


def upgrade() -> None:
    columns = _existing_columns(op.get_bind())
    for table in ("agent_schedules", "agent_triggers"):
        if "delivery_target_id" in columns.get(table, set()):
            continue
        op.add_column(
            table,
            sa.Column("delivery_target_id", postgresql.UUID(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    columns = _existing_columns(op.get_bind())
    for table in ("agent_triggers", "agent_schedules"):
        if "delivery_target_id" in columns.get(table, set()):
            op.drop_column(table, "delivery_target_id")

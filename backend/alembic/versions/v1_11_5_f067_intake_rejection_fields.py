"""Add Intake rejection/pending-verifier fields to projects/repositories.

Background:
  docs/INTAKE_ARCHITECTURE_BRIEF_V1.md §4.1 DESIGN GAP 1: Phase 2A §E.2 /
  §G.3 require a Project's REJECTED state to persist the reason code and a
  readable detail on the projects table itself, and repositories need a
  pending-verifier mark + bounded retry counter for git sources whose
  verifiers do not exist yet (Phase 2B root task §四). The f066 tables
  carry none of these columns.

Scope:
  DDL-only. Adds four columns:
    projects.rejection_reason   VARCHAR(50) NULL
    projects.rejection_detail   TEXT NULL
    repositories.pending_verifier BOOLEAN NOT NULL DEFAULT FALSE
    repositories.retry_count   SMALLINT NOT NULL DEFAULT 0
  No data backfill, no enum changes, no index changes. The reason-code
  closed set is enforced in the service layer (VARCHAR), not as a DB enum.

Idempotent:
  Each ADD COLUMN / DROP COLUMN is guarded by a schema-introspection
  existence check (sa.inspect), so re-running the migration is a no-op and
  fresh deployments where 001_initial_schema's create_all already built the
  tables from the registered metadata are handled by the same guard.

Revision ID: f067_intake_rejection_fields
Revises: f066_add_project_repo_tables
Create Date: 2026-09-22 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f067_intake_rejection_fields"
down_revision: str | None = "f066_add_project_repo_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTS_TABLE = "projects"
REPOSITORIES_TABLE = "repositories"

PROJECTS_ADDED_COLUMNS = ("rejection_reason", "rejection_detail")
REPOSITORIES_ADDED_COLUMNS = ("pending_verifier", "retry_count")


def _existing_columns(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Column names present on ``table_name`` in the current schema."""
    try:
        return {str(col["name"]) for col in sa.inspect(bind).get_columns(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def upgrade() -> None:
    conn = op.get_bind()

    projects_columns = _existing_columns(conn, PROJECTS_TABLE)
    if "rejection_reason" not in projects_columns:
        op.add_column(
            PROJECTS_TABLE,
            sa.Column("rejection_reason", sa.String(length=50), nullable=True),
        )
    if "rejection_detail" not in projects_columns:
        op.add_column(
            PROJECTS_TABLE,
            sa.Column("rejection_detail", sa.Text(), nullable=True),
        )

    repositories_columns = _existing_columns(conn, REPOSITORIES_TABLE)
    if "pending_verifier" not in repositories_columns:
        op.add_column(
            REPOSITORIES_TABLE,
            sa.Column("pending_verifier", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if "retry_count" not in repositories_columns:
        op.add_column(
            REPOSITORIES_TABLE,
            sa.Column("retry_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()

    projects_columns = _existing_columns(conn, PROJECTS_TABLE)
    for column in PROJECTS_ADDED_COLUMNS:
        if column in projects_columns:
            op.drop_column(PROJECTS_TABLE, column)

    repositories_columns = _existing_columns(conn, REPOSITORIES_TABLE)
    for column in REPOSITORIES_ADDED_COLUMNS:
        if column in repositories_columns:
            op.drop_column(REPOSITORIES_TABLE, column)

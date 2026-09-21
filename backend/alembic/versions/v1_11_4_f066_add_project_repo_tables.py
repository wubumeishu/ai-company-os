"""Add Project and Repository persistence tables (Phase 2A R1 birth model).

Background:
  docs/PHASE2A_PROJECT_DESIGN_V1.md §D.1 defines the Project entity (the
  business object of one thing the company takes on, born at Intake
  acceptance with status RECEIVED) and §F.1 the minimal Repository shape
  (a source/asset registry row, not necessarily a Git repository).
  Neither table existed before this revision (Phase 1 audit §8: Project
  domain entity MISSING). Task/Agent/Workspace/Execution/Artifact/Review/
  Scheduler tables are intentionally untouched.

Scope:
  DDL-only. Creates the ``projects`` and ``repositories`` tables with their
  enum types (project_status_enum, repository_source_type_enum) and
  indexes. No data backfill. 001_initial_schema's create_all also builds
  these tables on fresh deployments, so upgrade() guards every DDL
  operation behind an existence check.

Idempotent:
  Tables, indexes and enum types are only created/dropped when the current
  schema state requires the operation.

Revision ID: f066_add_project_repo_tables
Revises: f065_feishu_group_target
Create Date: 2026-09-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f066_add_project_repo_tables"
down_revision: str | None = "f065_feishu_group_target"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTS_TABLE = "projects"
REPOSITORIES_TABLE = "repositories"
PROJECT_STATUS_ENUM = "project_status_enum"
SOURCE_TYPE_ENUM = "repository_source_type_enum"

PROJECTS_INDEXES = ("ix_projects_created_by", "ix_projects_tenant_id")
REPOSITORIES_INDEXES = ("ix_repositories_project_id", "ix_repositories_tenant_id")

PROJECT_STATUS_VALUES = [
    "RECEIVED",
    "SOURCES_OK",
    "INITIALIZED",
    "ANALYZING",
    "PENDING_CONFIRMATION",
    "EXECUTING",
    "BLOCKED",
    "COMPLETED",
    "ARCHIVED",
    "REJECTED",
]
SOURCE_TYPE_VALUES = [
    "manual",
    "local_folder",
    "document",
    "zip",
    "github",
    "gitlab",
    "local_git",
]


def _enum_values(values: list[str]) -> str:
    """Render SQL enum literals for a CREATE TYPE statement."""
    return ", ".join(f"'{value}'" for value in values)


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _present_enum_types(bind: sa.engine.Connection) -> set[str]:
    """PG user-defined enum types present. Non-PG dialects define none."""
    if bind.dialect.name != "postgresql":
        return {PROJECT_STATUS_ENUM, SOURCE_TYPE_ENUM}
    result = bind.execute(
        sa.text(
            "SELECT typname FROM pg_type "
            "WHERE typname IN ('project_status_enum', 'repository_source_type_enum') "
            "AND typtype = 'e'"
        )
    )
    return {row[0] for row in result}


def _referenced_enum_types(bind: sa.engine.Connection) -> set[str]:
    """Enum types still used by a column in the public schema."""
    if bind.dialect.name != "postgresql":
        return set()
    result = bind.execute(
        sa.text(
            "SELECT DISTINCT t.typname FROM pg_attribute a "
            "JOIN pg_type t ON a.atttypid = t.oid "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE t.typname IN ('project_status_enum', 'repository_source_type_enum') "
            "AND n.nspname = 'public'"
        )
    )
    return {row[0] for row in result}


def _create_projects(conn: sa.engine.Connection) -> None:
    op.create_table(
        PROJECTS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*PROJECT_STATUS_VALUES, name=PROJECT_STATUS_ENUM, create_type=False),
            server_default="RECEIVED",
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_projects_created_by", PROJECTS_TABLE, ["created_by"])
    op.create_index("ix_projects_tenant_id", PROJECTS_TABLE, ["tenant_id"])


def _create_repositories(conn: sa.engine.Connection) -> None:
    op.create_table(
        REPOSITORIES_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_type",
            postgresql.ENUM(*SOURCE_TYPE_VALUES, name=SOURCE_TYPE_ENUM, create_type=False),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("locator", postgresql.JSON(), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("verified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_repositories_project_id", REPOSITORIES_TABLE, ["project_id"])
    op.create_index("ix_repositories_tenant_id", REPOSITORIES_TABLE, ["tenant_id"])


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    # Create the enum types exactly once, before any table that uses them.
    # create_type=False above means the table DDL does not manage the types.
    if PROJECT_STATUS_ENUM not in present_types:
        conn.execute(
            sa.text(f"CREATE TYPE {PROJECT_STATUS_ENUM} AS ENUM ({_enum_values(PROJECT_STATUS_VALUES)})")
        )
    if SOURCE_TYPE_ENUM not in present_types:
        conn.execute(
            sa.text(f"CREATE TYPE {SOURCE_TYPE_ENUM} AS ENUM ({_enum_values(SOURCE_TYPE_VALUES)})")
        )

    if PROJECTS_TABLE not in tables:
        _create_projects(conn)
    if REPOSITORIES_TABLE not in tables:
        _create_repositories(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    if REPOSITORIES_TABLE in tables:
        for index_name in REPOSITORIES_INDEXES:
            op.drop_index(index_name, table_name=REPOSITORIES_TABLE)
        op.drop_table(REPOSITORIES_TABLE)
    if PROJECTS_TABLE in tables:
        for index_name in PROJECTS_INDEXES:
            op.drop_index(index_name, table_name=PROJECTS_TABLE)
        op.drop_table(PROJECTS_TABLE)

    # Drop an enum type only when no table column still references it.
    referenced = _referenced_enum_types(conn)
    if PROJECT_STATUS_ENUM in present_types and PROJECT_STATUS_ENUM not in referenced:
        conn.execute(sa.text(f"DROP TYPE {PROJECT_STATUS_ENUM}"))
    if SOURCE_TYPE_ENUM in present_types and SOURCE_TYPE_ENUM not in referenced:
        conn.execute(sa.text(f"DROP TYPE {SOURCE_TYPE_ENUM}"))

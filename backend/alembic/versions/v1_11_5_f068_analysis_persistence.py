"""Add the Phase 2C minimal Analysis persistence tables.

Background:
  docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2 defines the minimal typed model so
  the OS can "reliably store one Project Analysis + its sources": three
  tenant-scoped tables — ``analysis_runs`` (versioning + Git-revision
  binding), ``analysis_findings`` (transient findings owned by a run), and
  ``project_knowledge`` (durable, confirmed, revision-independent
  knowledge).  None of them existed before this revision; this migration
  creates them, chained off the single head ``f067_intake_rejection_fields``.
  It is the build-phase deliverable for the model that document only
  *defined*.

Scope:
  DDL-only.  Creates the three tables, their enum types
  (analysis_run_status_enum, analysis_severity_enum, analysis_category_enum,
  analysis_tag_enum, knowledge_status_enum), the indexes, and the
  versioning invariant ``uq_analysis_runs_project_revision``
  (``UNIQUE(project_id, revision_sha)`` — append-only: a re-analysis at a
  NEW commit is a NEW row, history is never clobbered).  No data
  backfill, no changes to ``projects`` / ``repositories`` (zero
  Project-row pollution; the existing ``project_status_enum`` is untouched).

Idempotent:
  Tables, indexes and enum types are only created/dropped when the current
  schema state requires the operation (same guard style as f066).  Fresh
  deployments where 001_initial_schema's ``create_all`` already built the
  tables from the registered metadata are handled by the same guards.

Revision ID: f068_analysis_persistence
Revises: f067_intake_rejection_fields
Create Date: 2026-09-24 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f068_analysis_persistence"
down_revision: str | None = "f067_intake_rejection_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANALYSIS_RUNS_TABLE = "analysis_runs"
ANALYSIS_FINDINGS_TABLE = "analysis_findings"
PROJECT_KNOWLEDGE_TABLE = "project_knowledge"

RUN_STATUS_ENUM = "analysis_run_status_enum"
SEVERITY_ENUM = "analysis_severity_enum"
CATEGORY_ENUM = "analysis_category_enum"
TAG_ENUM = "analysis_tag_enum"
KNOWLEDGE_STATUS_ENUM = "knowledge_status_enum"

ALL_ENUMS = (RUN_STATUS_ENUM, SEVERITY_ENUM, CATEGORY_ENUM, TAG_ENUM, KNOWLEDGE_STATUS_ENUM)

RUN_STATUS_VALUES = ["AN_OPEN", "AN_COMPLETED", "AN_FAILED"]
SEVERITY_VALUES = ["INFO", "WARN", "HIGH", "CRITICAL"]
CATEGORY_VALUES = ["SECURITY", "RISK", "TECH_DEBT", "OPEN_QUESTION", "FACT"]
TAG_VALUES = ["FACT", "OBSERVATION", "INFERENCE", "UNKNOWN"]
KNOWLEDGE_STATUS_VALUES = ["PROPOSED", "CONFIRMED", "SUPERSEDED"]

RUNS_INDEXES = ("ix_analysis_runs_revision_sha", "ix_analysis_runs_tenant_id")
FINDINGS_INDEXES = ("ix_analysis_findings_analysis_run_id", "ix_analysis_findings_tenant_id")
KNOWLEDGE_INDEXES = ("ix_project_knowledge_project_id", "ix_project_knowledge_subject", "ix_project_knowledge_tenant_id")


def _enum_values(values: list[str]) -> str:
    """Render SQL enum literals for a CREATE TYPE statement."""
    return ", ".join(f"'{value}'" for value in values)


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _existing_indexes(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Index names present on ``table_name``.

    Empty set when the table itself is absent (nothing to reflect against),
    so a guarded ``op.drop_index`` is a no-op for a table that was never
    created (the create_all-provisioned fresh-DB path, where ``downgrade``
    may run on a table whose indexes the migration never produced).
    """
    if table_name not in _existing_tables(bind):
        return set()
    return {str(name) for name in sa.inspect(bind).get_indexes(table_name)}


def _present_enum_types(bind: sa.engine.Connection) -> set[str]:
    """PG user-defined enum types present. Non-PG dialects define none."""
    if bind.dialect.name != "postgresql":
        return set(ALL_ENUMS)
    result = bind.execute(
        sa.text(f"SELECT typname FROM pg_type WHERE typname IN ({_quoted(ALL_ENUMS)}) AND typtype = 'e'")
    )
    return {row[0] for row in result}


def _referenced_enum_types(bind: sa.engine.Connection) -> set[str]:
    """Enum types still used by a column in the public schema."""
    if bind.dialect.name != "postgresql":
        return set()
    result = bind.execute(
        sa.text(
            f"SELECT DISTINCT t.typname FROM pg_attribute a "
            f"JOIN pg_type t ON a.atttypid = t.oid "
            f"JOIN pg_namespace n ON t.typnamespace = n.oid "
            f"WHERE t.typname IN ({_quoted(ALL_ENUMS)}) AND n.nspname = 'public'"
        )
    )
    return {row[0] for row in result}


def _quoted(names: tuple[str, ...]) -> str:
    """Render quoted string literals for an IN (...) clause."""
    return ", ".join(f"'{name}'" for name in names)


def _create_analysis_runs(conn: sa.engine.Connection) -> None:
    op.create_table(
        ANALYSIS_RUNS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revision_sha", sa.String(length=64), nullable=False),
        sa.Column("requested_ref", sa.String(length=200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*RUN_STATUS_VALUES, name=RUN_STATUS_ENUM, create_type=False),
            server_default="AN_OPEN",
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        # SET NULL: deleting the running agent must never clobber analysis
        # history — the run is owned by the project, not the agent.
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("project_id", "revision_sha", name="uq_analysis_runs_project_revision"),
    )
    op.create_index("ix_analysis_runs_revision_sha", ANALYSIS_RUNS_TABLE, ["revision_sha"])
    op.create_index("ix_analysis_runs_tenant_id", ANALYSIS_RUNS_TABLE, ["tenant_id"])
    # NOTE: no standalone ix_analysis_runs_project_id. Project-keyed reads are
    # covered by the project_id-leading btree of uq_analysis_runs_project_revision
    # (UNIQUE(project_id, revision_sha)), matching the model, whose
    # AnalysisRun.project_id carries no index=True. A redundant standalone
    # index would also never exist on create_all-provisioned fresh DBs (001
    # pre-creates the table from the model metadata), so downgrade() would
    # have to DROP an index that was never created.


def _create_analysis_findings(conn: sa.engine.Connection) -> None:
    op.create_table(
        ANALYSIS_FINDINGS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # CASCADE: transient findings die with their run (design §9.2 hard
        # transient-vs-durable boundary).
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "severity",
            postgresql.ENUM(*SEVERITY_VALUES, name=SEVERITY_ENUM, create_type=False),
            server_default="INFO",
            nullable=False,
        ),
        sa.Column(
            "category",
            postgresql.ENUM(*CATEGORY_VALUES, name=CATEGORY_ENUM, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "tag",
            postgresql.ENUM(*TAG_VALUES, name=TAG_ENUM, create_type=False),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSON(), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_analysis_findings_analysis_run_id", ANALYSIS_FINDINGS_TABLE, ["analysis_run_id"])
    op.create_index("ix_analysis_findings_tenant_id", ANALYSIS_FINDINGS_TABLE, ["tenant_id"])


def _create_project_knowledge(conn: sa.engine.Connection) -> None:
    op.create_table(
        PROJECT_KNOWLEDGE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        # SET NULL: a superseded / deleted run never destroys a durable
        # knowledge row — provenance is copied, not owned (design §9.2).
        sa.Column("source_analysis_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*KNOWLEDGE_STATUS_VALUES, name=KNOWLEDGE_STATUS_ENUM, create_type=False),
            server_default="PROPOSED",
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_analysis_run_id"], ["analysis_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_project_knowledge_project_id", PROJECT_KNOWLEDGE_TABLE, ["project_id"])
    op.create_index("ix_project_knowledge_subject", PROJECT_KNOWLEDGE_TABLE, ["subject"])
    op.create_index("ix_project_knowledge_tenant_id", PROJECT_KNOWLEDGE_TABLE, ["tenant_id"])


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    type_map = {
        RUN_STATUS_ENUM: RUN_STATUS_VALUES,
        SEVERITY_ENUM: SEVERITY_VALUES,
        CATEGORY_ENUM: CATEGORY_VALUES,
        TAG_ENUM: TAG_VALUES,
        KNOWLEDGE_STATUS_ENUM: KNOWLEDGE_STATUS_VALUES,
    }
    # Create every enum type exactly once, before any table that uses them.
    # create_type=False above means the table DDL does not manage the types.
    for enum_name, values in type_map.items():
        if enum_name not in present_types:
            conn.execute(sa.text(f"CREATE TYPE {enum_name} AS ENUM ({_enum_values(values)})"))

    if ANALYSIS_RUNS_TABLE not in tables:
        _create_analysis_runs(conn)
    if ANALYSIS_FINDINGS_TABLE not in tables:
        _create_analysis_findings(conn)
    if PROJECT_KNOWLEDGE_TABLE not in tables:
        _create_project_knowledge(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    # Drop in dependency order: findings (CASCADE child) -> knowledge -> runs.
    # Each index drop is guarded against the index actually existing (mirrors
    # the _existing_tables guard above), so downgrade is a clean no-op on the
    # create_all-provisioned fresh-DB path, where 001 pre-created the tables
    # from the model metadata and this migration's create_index never ran.
    if PROJECT_KNOWLEDGE_TABLE in tables:
        knowledge_indexes = _existing_indexes(conn, PROJECT_KNOWLEDGE_TABLE)
        for index_name in KNOWLEDGE_INDEXES:
            if index_name in knowledge_indexes:
                op.drop_index(index_name, table_name=PROJECT_KNOWLEDGE_TABLE)
        op.drop_table(PROJECT_KNOWLEDGE_TABLE)
    if ANALYSIS_FINDINGS_TABLE in tables:
        findings_indexes = _existing_indexes(conn, ANALYSIS_FINDINGS_TABLE)
        for index_name in FINDINGS_INDEXES:
            if index_name in findings_indexes:
                op.drop_index(index_name, table_name=ANALYSIS_FINDINGS_TABLE)
        op.drop_table(ANALYSIS_FINDINGS_TABLE)
    if ANALYSIS_RUNS_TABLE in tables:
        runs_indexes = _existing_indexes(conn, ANALYSIS_RUNS_TABLE)
        for index_name in RUNS_INDEXES:
            if index_name in runs_indexes:
                op.drop_index(index_name, table_name=ANALYSIS_RUNS_TABLE)
        op.drop_table(ANALYSIS_RUNS_TABLE)

    # Drop an enum type only when no table column still references it.
    referenced = _referenced_enum_types(conn)
    for enum_name in ALL_ENUMS:
        if enum_name in present_types and enum_name not in referenced:
            conn.execute(sa.text(f"DROP TYPE {enum_name}"))

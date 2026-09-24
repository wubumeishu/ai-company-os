"""Add the Phase 2D Task Graph edge table and Task provenance columns.

Background:
  docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §3/§4 defines the V1 minimum
  Task Graph & Provenance model: one edge table ``task_dependencies``
  (tenant-scoped, UNIQUE pair, no-self CHECK, CASCADE on both ends) plus five
  provenance columns on ``tasks`` (``project_id`` CASCADE,
  ``analysis_run_id`` / ``finding_id`` SET NULL, ``revision_sha`` denormalized
  snapshot, ``created_reason`` closed 3-value enum).  The design document
  defines them; this migration persists them, chained off the single head
  ``f068_analysis_persistence``.

Scope:
  DDL-only.  Creates the ``task_created_reason_enum`` type, the five
  ``tasks`` provenance columns, the ``task_dependencies`` table with its
  indexes + constraints, and the three physical foreign keys on the new
  ``tasks`` columns.  No data backfill: ``created_reason`` carries
  ``server_default='MANUAL'`` so every existing Task row is MANUAL by
  definition, and the analysis columns start NULL (legacy Tasks have no
  provenance).  No change to ``task_status_enum`` (3 values, design ADR-2)
  and no Task->Run reverse FK (design ADR-3).

Idempotent:
  Enum types, columns, table, indexes and constraints are only created/dropped
  when the current schema state requires the operation (same guard skeleton as
  f066/f068).  Fresh deployments where 001_initial_schema's create_all already
  built the tables/columns from the registered metadata (task.py now declares
  TaskDependency + the five Task columns, all imported by env.py) are handled
  by the same guards — every operation is a no-op.

Index lockstep (f068 lesson, carried over):
  The model declares ``index=True`` on all five provenance columns and on the
  three TaskDependency FK columns, so create_all produces PLAIN btree indexes
  named ``ix_tasks_<col>`` / ``ix_task_dependencies_<col>``.  This migration
  creates indexes with exactly those names so a fresh DB (create_all path) and
  an existing DB (migration path) end with the identical index set; downgrade
  can drop by name on both paths.  The design draft's partial
  (``WHERE col IS NOT NULL``) indexes are NOT used: the model cannot express
  them, and model/migration index lockstep is the repo invariant.

  Physical FKs on ``tasks`` and ``task_dependencies`` follow the Phase 2B/2C
  precedent (f066-f068 physical ``ForeignKeyConstraint``; design ADR-1).
  ``tasks.project_id`` ON DELETE CASCADE; ``analysis_run_id`` /
  ``finding_id`` ON DELETE SET NULL (design §4.1); edge-table FKs CASCADE
  both ends (design §3.2).

Revision ID: f069_task_graph_provenance
Revises: f068_analysis_persistence
Create Date: 2026-09-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f069_task_graph_provenance"
down_revision: str | None = "f068_analysis_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASKS_TABLE = "tasks"
TASK_DEPENDENCIES_TABLE = "task_dependencies"

CREATED_REASON_ENUM = "task_created_reason_enum"
CREATED_REASON_VALUES = ["MANUAL", "ANALYSIS_FINDING", "ANALYSIS_PLANNING"]

# The five provenance columns added to tasks (design §4.4).
TASKS_ADDED_COLUMNS = ("project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason")

TASKS_NEW_INDEXES = (
    "ix_tasks_project_id",
    "ix_tasks_analysis_run_id",
    "ix_tasks_finding_id",
    "ix_tasks_revision_sha",
    "ix_tasks_created_reason",
)

# Index name -> single column it covers (the plain btree names create_all
# produces for the model's index=True columns; see the lockstep note in the
# docstring).
TASKS_NEW_INDEX_COLUMNS = {
    "ix_tasks_project_id": "project_id",
    "ix_tasks_analysis_run_id": "analysis_run_id",
    "ix_tasks_finding_id": "finding_id",
    "ix_tasks_revision_sha": "revision_sha",
    "ix_tasks_created_reason": "created_reason",
}

TASK_DEPENDENCIES_INDEXES = (
    "ix_task_dependencies_task_id",
    "ix_task_dependencies_depends_on_task_id",
    "ix_task_dependencies_tenant_id",
)

# Physical FK constraint names created on an existing (pre-f069) DB.  On a
# fresh create_all-provisioned DB the equivalent constraints carry SQLAlchemy's
# auto names; downgrade() discovers them by introspection (see _drop_fk_on).
TASKS_FK_NAMES = ("fk_tasks_project", "fk_tasks_analysis_run", "fk_tasks_finding")
DEPS_FK_NAMES = ("fk_task_dep_tenant", "fk_task_dep_task", "fk_task_dep_depends")


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _existing_columns(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Column names present on ``table_name`` in the current schema."""
    try:
        return {str(col["name"]) for col in sa.inspect(bind).get_columns(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def _existing_indexes(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Index NAMES present on ``table_name``.

    Empty set when the table itself is absent (nothing to reflect against),
    so a guarded ``op.drop_index`` / ``op.create_index`` is a no-op for a
    table that was never created (the create_all-provisioned fresh-DB path,
    mirroring f068).  ``get_indexes`` returns one dict per index; we keep the
    ``name`` key so callers can test ``index_name in <this set>``.
    """
    if table_name not in _existing_tables(bind):
        return set()
    return {str(row["name"]) for row in sa.inspect(bind).get_indexes(table_name)}


def _present_enum_types(bind: sa.engine.Connection) -> set[str]:
    """PG user-defined enum types present. Non-PG dialects define none."""
    if bind.dialect.name != "postgresql":
        return {CREATED_REASON_ENUM}
    result = bind.execute(
        sa.text(
            f"SELECT typname FROM pg_type WHERE typname IN ('{CREATED_REASON_ENUM}') AND typtype = 'e'"
        )
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
            f"WHERE t.typname IN ('{CREATED_REASON_ENUM}') AND n.nspname = 'public'"
        )
    )
    return {row[0] for row in result}


def _existing_fk_constraints(bind: sa.engine.Connection, table_name: str) -> list[dict]:
    """FK constraint metadata (name + constrained columns) on a table."""
    try:
        return [dict(row) for row in sa.inspect(bind).get_foreign_keys(table_name)]
    except sa.exc.NoSuchTableError:
        return []


def _drop_fk_on(bind: sa.engine.Connection, table_name: str, column: str) -> None:
    """Drop every FK constraint on ``table_name`` that anchors on ``column``.

    Introspection-based (not name-based) so the same downgrade works on both
    provisioning paths: an existing DB (our explicit ``fk_tasks_*`` names) and
    a fresh create_all DB (SQLAlchemy's auto names like ``tasks_project_id_fkey``).
    """
    for fk in _existing_fk_constraints(bind, table_name):
        if column in [str(c) for c in fk.get("constrained_columns", [])]:
            op.drop_constraint(str(fk["name"]), table_name=table_name)


def _create_task_dependencies(conn: sa.engine.Connection) -> None:
    op.create_table(
        TASK_DEPENDENCIES_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # 依赖方（下游）/ 被依赖方（上游）。  两端均 CASCADE：Task 删除即边消失。
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depends_on_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_depends_pair"),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dep_no_self"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_task_dep_tenant"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE", name="fk_task_dep_task"
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"], ["tasks.id"], ondelete="CASCADE", name="fk_task_dep_depends"
        ),
    )
    op.create_index("ix_task_dependencies_task_id", TASK_DEPENDENCIES_TABLE, ["task_id"])
    op.create_index(
        "ix_task_dependencies_depends_on_task_id", TASK_DEPENDENCIES_TABLE, ["depends_on_task_id"]
    )
    op.create_index("ix_task_dependencies_tenant_id", TASK_DEPENDENCIES_TABLE, ["tenant_id"])


def _add_tasks_provenance_columns(conn: sa.engine.Connection) -> None:
    """Add the five provenance columns + their indexes + physical FKs."""
    columns = _existing_columns(conn, TASKS_TABLE)

    if "project_id" not in columns:
        op.add_column(
            TASKS_TABLE,
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if "analysis_run_id" not in columns:
        op.add_column(
            TASKS_TABLE,
            sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if "finding_id" not in columns:
        op.add_column(
            TASKS_TABLE,
            sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if "revision_sha" not in columns:
        op.add_column(TASKS_TABLE, sa.Column("revision_sha", sa.String(length=64), nullable=True))
    if "created_reason" not in columns:
        # NOT NULL + server_default: every existing row becomes MANUAL with no
        # data backfill (DDL-only rule).
        op.add_column(
            TASKS_TABLE,
            sa.Column(
                "created_reason",
                postgresql.ENUM(*CREATED_REASON_VALUES, name=CREATED_REASON_ENUM, create_type=False),
                server_default="MANUAL",
                nullable=False,
            ),
        )

    indexes = _existing_indexes(conn, TASKS_TABLE)
    for index_name, column in TASKS_NEW_INDEX_COLUMNS.items():
        if index_name not in indexes:
            op.create_index(index_name, TASKS_TABLE, [column])

    # Physical FKs follow the Phase 2B/2C precedent (design ADR-1).  Guarded by
    # introspecting the anchors, so a create_all-provisioned fresh DB (where
    # the FKs already exist under auto names) is a no-op.
    existing_anchors = {
        frozenset(str(c) for c in fk.get("constrained_columns", []))
        for fk in _existing_fk_constraints(conn, TASKS_TABLE)
    }
    if frozenset(["project_id"]) not in existing_anchors:
        op.create_foreign_key(
            "fk_tasks_project",
            TASKS_TABLE,
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if frozenset(["analysis_run_id"]) not in existing_anchors:
        # SET NULL: deleting an analysis run never destroys a durable Task
        # (provenance is copied, not owned — design §4.1).
        op.create_foreign_key(
            "fk_tasks_analysis_run",
            TASKS_TABLE,
            "analysis_runs",
            ["analysis_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if frozenset(["finding_id"]) not in existing_anchors:
        op.create_foreign_key(
            "fk_tasks_finding",
            TASKS_TABLE,
            "analysis_findings",
            ["finding_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _create_reason_enum(conn: sa.engine.Connection) -> None:
    """CREATE the task_created_reason_enum type (idempotent caller-side)."""
    values = ", ".join(f"'{value}'" for value in CREATED_REASON_VALUES)
    conn.execute(sa.text(f"CREATE TYPE {CREATED_REASON_ENUM} AS ENUM ({values})"))


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)

    # Create the enum type exactly once, before any column DDL that uses it.
    # create_type=False on the column means the ADD COLUMN statement does not
    # manage the type.
    if CREATED_REASON_ENUM not in _present_enum_types(conn):
        _create_reason_enum(conn)

    if TASK_DEPENDENCIES_TABLE not in tables:
        _create_task_dependencies(conn)

    if TASKS_TABLE in tables:
        _add_tasks_provenance_columns(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)

    # Drop in dependency order: edge table -> tasks columns -> enum type.
    # Every operation is guarded, so downgrade is a clean no-op on the
    # create_all-provisioned fresh-DB path and re-runnable otherwise.
    if TASK_DEPENDENCIES_TABLE in tables:
        for index_name in TASK_DEPENDENCIES_INDEXES:
            if index_name in _existing_indexes(conn, TASK_DEPENDENCIES_TABLE):
                op.drop_index(index_name, table_name=TASK_DEPENDENCIES_TABLE)
        for fk in _existing_fk_constraints(conn, TASK_DEPENDENCIES_TABLE):
            op.drop_constraint(str(fk["name"]), table_name=TASK_DEPENDENCIES_TABLE)
        op.drop_table(TASK_DEPENDENCIES_TABLE)

    if TASKS_TABLE in tables:
        # FKs first (they reference the columns), then indexes, then columns.
        for column in ("project_id", "analysis_run_id", "finding_id"):
            _drop_fk_on(conn, TASKS_TABLE, column)
        for index_name in TASKS_NEW_INDEXES:
            if index_name in _existing_indexes(conn, TASKS_TABLE):
                op.drop_index(index_name, table_name=TASKS_TABLE)
        for column in TASKS_ADDED_COLUMNS:
            if column in _existing_columns(conn, TASKS_TABLE):
                op.drop_column(TASKS_TABLE, column)

    # Drop the enum type only when no column still references it (a partially
    # rolled-down schema must not leave an orphaned type behind, and a
    # create_all-provisioned schema where the column is still present keeps it).
    if CREATED_REASON_ENUM in _present_enum_types(conn) and CREATED_REASON_ENUM not in _referenced_enum_types(conn):
        conn.execute(sa.text(f"DROP TYPE {CREATED_REASON_ENUM}"))

"""Add the Phase 3 Planning Domain persistence tables.

Background:
  docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md §3 defines
  the minimal Planning Domain data layer (card t_b342df97): five
  tenant-scoped tables — ``planning_runs`` (the durable, append-only plan
  revision bound to one Project + one analysis revision),
  ``planning_goals`` (derived operational objectives, cascade-owned by a
  run), ``work_packages`` (the structural grouping the flat Task Graph
  cannot express), ``milestones`` (optional ordering/phase buckets), and
  ``work_package_tasks`` (the ONLY new link table connecting Planning to
  the frozen ``Task`` model — an explicit FK link, design P5, without
  touching the Task model).  None of them existed before this revision;
  this migration creates them, chained off the single head
  ``f070_analysis_task_dedup``.

Scope:
  DDL-only.  Creates the two status enum types
  (planning_run_status_enum, planning_goal_status_enum — closed
  result-code sets, decision D5: NOT workflow state machines), the five
  tables, their indexes, and the invariants of design §6:
  - ``uq_planning_runs_project_revision`` — one run per
    (project, analysis revision): the append-only versioning guard
    (invariant §6.1, mirroring uq_analysis_runs_project_revision);
  - ``uq_milestones_run_seq`` — one bucket per position (invariant §6.4);
  - ``uq_wp_tasks`` — one task per (work_package, task) slot
    (invariant §6.2, the materialization idempotency key);
  - ``ck_wp_tasks_materialized`` — a materialized_at timestamp requires a
    task_id (invariant §6.3).
  No data backfill, no changes to ``tasks`` / ``analysis_runs`` /
  ``task_dependencies`` / any frozen Phase 2A–2F code path.  The
  ``work_package_tasks.task_id`` FK (SET NULL) is the only DDL that
  *references* the frozen tasks table — it adds no column and no
  constraint to tasks itself.

Idempotent:
  Tables, indexes and enum types are only created/dropped when the current
  schema state requires the operation (same guard style as f068/f069).
  Fresh deployments where 001_initial_schema's ``create_all`` already built
  the tables from the registered metadata (planning.py is now imported by
  env.py) are handled by the same guards.

Index lockstep (f068/f069 lesson):
  The model declares ``index=True`` on the FK/snapshot columns, so
  create_all produces plain btree indexes named ``ix_<table>_<col>``.
  This migration creates indexes with exactly those names so a fresh DB
  (create_all path) and an existing DB (migration path) end with the
  identical index set; downgrade can drop by name on both paths.
  Named UNIQUE constraints create the backing UNIQUE indexes under the
  SAME names on both paths (create_all names a UNIQUE constraint index
  after the constraint), so no separate create_index calls are needed for
  them.

Revision ID: f071_planning_persistence
Revises: f070_analysis_task_dedup
Create Date: 2026-09-27 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f071_planning_persistence"
down_revision: str | None = "f070_analysis_task_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING_RUNS_TABLE = "planning_runs"
PLANNING_GOALS_TABLE = "planning_goals"
WORK_PACKAGES_TABLE = "work_packages"
MILESTONES_TABLE = "milestones"
WORK_PACKAGE_TASKS_TABLE = "work_package_tasks"

RUN_STATUS_ENUM = "planning_run_status_enum"
GOAL_STATUS_ENUM = "planning_goal_status_enum"

ALL_ENUMS = (RUN_STATUS_ENUM, GOAL_STATUS_ENUM)

RUN_STATUS_VALUES = ["PL_OPEN", "PL_COMPLETED", "PL_FAILED"]
GOAL_STATUS_VALUES = ["PL_PROPOSED", "PL_APPROVED", "PL_MATERIALIZED"]

# Plain btree index names create_all produces for the model's index=True
# columns (model/migration lockstep — see docstring).  Name -> single column.
RUNS_INDEXES = {
    "ix_planning_runs_analysis_revision_sha": "analysis_revision_sha",
    "ix_planning_runs_plan_sha256": "plan_sha256",
    "ix_planning_runs_tenant_id": "tenant_id",
}
GOALS_INDEXES = {
    "ix_planning_goals_planning_run_id": "planning_run_id",
    "ix_planning_goals_tenant_id": "tenant_id",
}
PACKAGES_INDEXES = {
    "ix_work_packages_planning_run_id": "planning_run_id",
    "ix_work_packages_planning_goal_id": "planning_goal_id",
    "ix_work_packages_milestone_id": "milestone_id",
    "ix_work_packages_tenant_id": "tenant_id",
}
MILESTONES_INDEXES = {
    "ix_milestones_planning_run_id": "planning_run_id",
    "ix_milestones_tenant_id": "tenant_id",
}
WP_TASKS_INDEXES = {
    "ix_work_package_tasks_work_package_id": "work_package_id",
    "ix_work_package_tasks_task_id": "task_id",
    "ix_work_package_tasks_tenant_id": "tenant_id",
}


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
    created (the create_all-provisioned fresh-DB path, where downgrade()
    may run on a table whose indexes the migration never produced).
    """
    if table_name not in _existing_tables(bind):
        return set()
    return {str(row["name"]) for row in sa.inspect(bind).get_indexes(table_name)}


def _present_enum_types(bind: sa.engine.Connection) -> set[str]:
    """PG user-defined enum types present. Non-PG dialects define none."""
    if bind.dialect.name != "postgresql":
        return set(ALL_ENUMS)
    result = bind.execute(
        sa.text(
            f"SELECT typname FROM pg_type WHERE typname IN ({_quoted(ALL_ENUMS)}) AND typtype = 'e'"
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
            f"WHERE t.typname IN ({_quoted(ALL_ENUMS)}) AND n.nspname = 'public'"
        )
    )
    return {row[0] for row in result}


def _quoted(names: tuple[str, ...]) -> str:
    """Render quoted string literals for an IN (...) clause."""
    return ", ".join(f"'{name}'" for name in names)


def _create_planning_runs(conn: sa.engine.Connection) -> None:
    op.create_table(
        PLANNING_RUNS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_revision_sha", sa.String(length=64), nullable=False),
        # Content hash of the emitted plan; NULL while the run is open
        # (design P1) — set when the plan is recorded and locked.
        sa.Column("plan_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*RUN_STATUS_VALUES, name=RUN_STATUS_ENUM, create_type=False),
            server_default="PL_OPEN",
            nullable=False,
        ),
        # SET NULL: deleting the planner agent / its Run never destroys
        # planning history (the run is owned by the project, not the agent).
        sa.Column("planner_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_payload", postgresql.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Invariant §6.1: one planning run per (project, analysis revision)
        # — append-only revision binding, the concurrency guard.  The backing
        # UNIQUE index is named after the constraint on both provisioning
        # paths, so it needs no separate create_index call.
        sa.UniqueConstraint(
            "project_id", "analysis_revision_sha", name="uq_planning_runs_project_revision"
        ),
        # CASCADE mirrors the f068 analysis_runs FK: deleting the project
        # removes its planning history.
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["planner_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    for index_name, column in RUNS_INDEXES.items():
        op.create_index(index_name, PLANNING_RUNS_TABLE, [column])


def _create_planning_goals(conn: sa.engine.Connection) -> None:
    op.create_table(
        PLANNING_GOALS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # CASCADE: goals die with their run revision (append-only revisions,
        # design §5 — a re-plan is a NEW run's goals, never a clobber).
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Assignment INPUT (design P5 / squad S2): bounded capability list
        # drawn from WORK_CAPABILITIES — never the assignment fact.
        sa.Column("required_capabilities", postgresql.JSON(), nullable=True),
        sa.Column("analysis_finding_ids", postgresql.JSON(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*GOAL_STATUS_VALUES, name=GOAL_STATUS_ENUM, create_type=False),
            server_default="PL_PROPOSED",
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["planning_run_id"], ["planning_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    for index_name, column in GOALS_INDEXES.items():
        op.create_index(index_name, PLANNING_GOALS_TABLE, [column])


def _create_work_packages(conn: sa.engine.Connection) -> None:
    op.create_table(
        WORK_PACKAGES_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # CASCADE: a WP belongs to a revision and dies with it.
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Every WP pursues exactly one goal (design P3).
        sa.Column("planning_goal_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Optional phase/milestone bucket; nullable so a plan can have no
        # milestones at all.  SET NULL: deleting a bucket keeps the WP.
        sa.Column("milestone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        # The materialization intent (design P7 / squad §4.1 slot shape).
        sa.Column("task_scope", postgresql.JSON(), nullable=True),
        # Planner PROPOSAL (P12, closed set serial/parallel/
        # parallel_then_serial); the authoritative order is the DAG.
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        # Shared-resource DECLARATION (P10): planning detects, the runtime
        # Redis locks + advisory lane enforce — no new lock layer.
        sa.Column("shared_resources", postgresql.JSON(), nullable=True),
        # Review-independence flag (P11); enforced at assignment time
        # (squad REV-1), not by a new gate.
        sa.Column(
            "requires_independent_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        # Per-WP parallelism hint (P16) — advisory, feeds the deferred
        # project cap, not a scheduler.
        sa.Column("max_parallel_tasks", sa.Integer(), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["planning_run_id"], ["planning_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["planning_goal_id"], ["planning_goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    for index_name, column in PACKAGES_INDEXES.items():
        op.create_index(index_name, WORK_PACKAGES_TABLE, [column])


def _create_milestones(conn: sa.engine.Connection) -> None:
    op.create_table(
        MILESTONES_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Closed set phase/gate/delivery (design P9): bounded vocabulary
        # validated at the DAO/service layer (no DB enum, matching the
        # model's String(20) column).
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Invariant §6.4: one bucket per position within the run.
        sa.UniqueConstraint("planning_run_id", "seq", name="uq_milestones_run_seq"),
        sa.ForeignKeyConstraint(["planning_run_id"], ["planning_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    for index_name, column in MILESTONES_INDEXES.items():
        op.create_index(index_name, MILESTONES_TABLE, [column])


def _create_work_package_tasks(conn: sa.engine.Connection) -> None:
    op.create_table(
        WORK_PACKAGE_TASKS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # CASCADE: the slots die with their work package.
        sa.Column("work_package_id", postgresql.UUID(as_uuid=True), nullable=False),
        # SET NULL + nullable: task_id is NULL until materialized (design
        # §4); deleting a Task preserves the slot intent.  This FK is the
        # explicit link the card permits to the frozen tasks table — no
        # column or constraint is added to tasks itself.
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Invariant §6.2: one task per (work_package, task) slot.  PG
        # treats NULL task_id as distinct, so many open slots coexist.
        sa.UniqueConstraint("work_package_id", "task_id", name="uq_wp_tasks"),
        # Invariant §6.3: a materialized_at timestamp requires a task_id.
        sa.CheckConstraint(
            "materialized_at IS NULL OR task_id IS NOT NULL", name="ck_wp_tasks_materialized"
        ),
        sa.ForeignKeyConstraint(["work_package_id"], ["work_packages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    for index_name, column in WP_TASKS_INDEXES.items():
        op.create_index(index_name, WORK_PACKAGE_TASKS_TABLE, [column])


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    type_map = {
        RUN_STATUS_ENUM: RUN_STATUS_VALUES,
        GOAL_STATUS_ENUM: GOAL_STATUS_VALUES,
    }
    # Create every enum type exactly once, before any table that uses them.
    # create_type=False on the column means the table DDL does not manage
    # the types.
    for enum_name, values in type_map.items():
        if enum_name not in present_types:
            conn.execute(
                sa.text(f"CREATE TYPE {enum_name} AS ENUM ({_enum_values(values)})")
            )

    # Create in dependency order: runs -> goals -> milestones -> packages
    # (packages reference all three) -> the link table.
    if PLANNING_RUNS_TABLE not in tables:
        _create_planning_runs(conn)
    if PLANNING_GOALS_TABLE not in tables:
        _create_planning_goals(conn)
    if MILESTONES_TABLE not in tables:
        _create_milestones(conn)
    if WORK_PACKAGES_TABLE not in tables:
        _create_work_packages(conn)
    if WORK_PACKAGE_TASKS_TABLE not in tables:
        _create_work_package_tasks(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    present_types = _present_enum_types(conn)

    # Drop in reverse dependency order: link -> packages -> milestones ->
    # goals -> runs.  Every index drop is guarded against the index actually
    # existing (mirrors the _existing_tables guard above), so downgrade is a
    # clean no-op on the create_all-provisioned fresh-DB path, where 001
    # pre-created the tables from the model metadata and this migration's
    # create_index never ran.
    if WORK_PACKAGE_TASKS_TABLE in tables:
        wp_tasks_indexes = _existing_indexes(conn, WORK_PACKAGE_TASKS_TABLE)
        for index_name in WP_TASKS_INDEXES:
            if index_name in wp_tasks_indexes:
                op.drop_index(index_name, table_name=WORK_PACKAGE_TASKS_TABLE)
        op.drop_table(WORK_PACKAGE_TASKS_TABLE)
    if WORK_PACKAGES_TABLE in tables:
        packages_indexes = _existing_indexes(conn, WORK_PACKAGES_TABLE)
        for index_name in PACKAGES_INDEXES:
            if index_name in packages_indexes:
                op.drop_index(index_name, table_name=WORK_PACKAGES_TABLE)
        op.drop_table(WORK_PACKAGES_TABLE)
    if MILESTONES_TABLE in tables:
        milestones_indexes = _existing_indexes(conn, MILESTONES_TABLE)
        for index_name in MILESTONES_INDEXES:
            if index_name in milestones_indexes:
                op.drop_index(index_name, table_name=MILESTONES_TABLE)
        op.drop_table(MILESTONES_TABLE)
    if PLANNING_GOALS_TABLE in tables:
        goals_indexes = _existing_indexes(conn, PLANNING_GOALS_TABLE)
        for index_name in GOALS_INDEXES:
            if index_name in goals_indexes:
                op.drop_index(index_name, table_name=PLANNING_GOALS_TABLE)
        op.drop_table(PLANNING_GOALS_TABLE)
    if PLANNING_RUNS_TABLE in tables:
        runs_indexes = _existing_indexes(conn, PLANNING_RUNS_TABLE)
        for index_name in RUNS_INDEXES:
            if index_name in runs_indexes:
                op.drop_index(index_name, table_name=PLANNING_RUNS_TABLE)
        op.drop_table(PLANNING_RUNS_TABLE)

    # Drop an enum type only when no table column still references it (a
    # partially rolled-down schema must not orphan the type).
    referenced = _referenced_enum_types(conn)
    for enum_name in ALL_ENUMS:
        if enum_name in present_types and enum_name not in referenced:
            conn.execute(sa.text(f"DROP TYPE {enum_name}"))

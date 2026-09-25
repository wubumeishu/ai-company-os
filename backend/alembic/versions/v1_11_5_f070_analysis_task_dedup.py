"""Add the Phase 2D Analysis->Task dedup constraint on tasks (f070).

Background:
  docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md §4 defines the idempotency invariant:
  "a finding may be converted once per analysis run".  The last-resort guard
  for that invariant (racing conversions) is a UNIQUE constraint on
  (analysis_run_id, finding_id).  f069 landed the five provenance COLUMNS but
  deliberately deferred this dedup DDL to the mapping lane (t_b8545ece),
  reserving slot f070 on the single head.  This migration persists it.

Scope:
  DDL-only.  Creates ONE named unique constraint
  ``uq_tasks_analysis_finding`` on ``tasks (analysis_run_id, finding_id)``.
  No data backfill, no new columns, no enum.  ``created_reason`` / the
  provenance columns are already present from f069 (down_revision).

Idempotent:
  The constraint is only created when the current schema state lacks it (same
  guarded-introspection skeleton as f068/f069).  A fresh deployment where
  001's create_all already built it from the model metadata (task.py now
  declares ``UniqueConstraint("analysis_run_id","finding_id",
  name="uq_tasks_analysis_finding")``) is a no-op; an existing DB stamped
  before f070 gets the constraint added.

Lockstep (f068 lesson, carried over):
  The model declares the constraint as a plain NAMED ``UniqueConstraint``
  (not the spec's partial WHERE-finding_id-not-null index, which the model
  cannot express).  PostgreSQL therefore represents it as a unique index with
  that name, so this migration's ``create_unique_constraint`` and the
  create_all path agree on the index name; downgrade drops by the same name
  on both provisioning paths.  NULL finding_id values are distinct in
  PostgreSQL, so many MANUAL rows coexist; only a repeated
  (analysis_run_id, finding_id) pair collides — exactly the mapping §4 intent.

Revision ID: f070_analysis_task_dedup
Revises: f069_task_graph_provenance
Create Date: 2026-09-25 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f070_analysis_task_dedup"
down_revision: str | None = "f069_task_graph_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASKS_TABLE = "tasks"
DEDUP_UNIQUE_INDEX = "uq_tasks_analysis_finding"
DEDUP_COLUMNS = ("analysis_run_id", "finding_id")


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _existing_indexes(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Index NAMES present on ``table_name`` (a named UNIQUE constraint is a
    backing unique index, so it appears here; empty when the table is absent)."""
    if table_name not in _existing_tables(bind):
        return set()
    return {str(row["name"]) for row in sa.inspect(bind).get_indexes(table_name)}


def _existing_columns(bind: sa.engine.Connection, table_name: str) -> set[str]:
    try:
        return {str(col["name"]) for col in sa.inspect(bind).get_columns(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def upgrade() -> None:
    conn = op.get_bind()
    if TASKS_TABLE not in _existing_tables(conn):
        return
    columns = _existing_columns(conn, TASKS_TABLE)
    # Both columns come from f069; if either is missing the invariant has
    # nothing to bind to, so a no-op is correct (guard against a torn-down schema).
    if not all(column in columns for column in DEDUP_COLUMNS):
        return
    if DEDUP_UNIQUE_INDEX not in _existing_indexes(conn, TASKS_TABLE):
        op.create_unique_constraint(DEDUP_UNIQUE_INDEX, TASKS_TABLE, list(DEDUP_COLUMNS))


def downgrade() -> None:
    conn = op.get_bind()
    if TASKS_TABLE in _existing_tables(conn) and DEDUP_UNIQUE_INDEX in _existing_indexes(conn, TASKS_TABLE):
        op.drop_constraint(DEDUP_UNIQUE_INDEX, TASKS_TABLE, type_="unique")

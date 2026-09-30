"""Add the Phase 4 Artifact / Evidence domain ledger tables.

Background:
  docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md §3 defines the
  minimal V1 Artifact / Evidence data layer (card t_436ddafb, Task 1): two
  append-only, tenant-scoped, content-addressed ledger tables —
  ``artifact_records`` ("what was produced": identity, type, ref, content
  hash, provenance, seal) and ``evidence_records`` ("what proves it":
  provenance, kind, outcome, re-verifiable) — built *on top of* the frozen
  Phase 2A–3 foundations, never in parallel.  None of them existed before
  this revision; this migration creates them, chained off the single head
  ``f071_planning_persistence``.

Scope:
  DDL-only.  Creates the two tables, their plain btree indexes (named exactly
  as the model's ``index=True`` columns produce under create_all), the two DB
  CHECK invariants of design §7, the ``uq_artifact_records_tenant_ref`` UNIQUE
  dedup constraint, and the ``uq_evidence_records_reverify`` partial UNIQUE
  index (AgentRunEvent partial-unique precedent).  No PG enum types (the
  closed sets ``ARTIFACT_TYPES`` / ``STORAGE_SCHEMES`` / ``SEAL_STATUSES`` /
  ``EVIDENCE_KINDS`` / ``EVIDENCE_OUTCOMES`` are "String + service
  validation", the f069 created_reason precedent — matching the model's plain
  String columns).  No data backfill; no changes to the frozen ``tasks`` /
  ``agent_tool_executions`` / ``workspace_file_revisions`` /
  ``published_pages`` / ``repositories`` — the new FKs (SET NULL) only
  *reference* those tables, adding no column or constraint to them.

Idempotent:
  Tables and indexes are only created/dropped when the current schema state
  requires the operation (same guard style as f071 / f068 / f069).  Fresh
  deployments where create_all already built the two tables from the
  registered metadata (artifact_evidence.py is now imported by env.py) are
  handled by the same guards.

Index lockstep (f068/f069/f071 lesson):
  The model declares ``index=True`` on the FK / snapshot columns, so
  create_all produces plain btree indexes named ``ix_<table>_<col>``.  This
  migration creates indexes with exactly those names so a fresh DB
  (create_all path) and an existing DB (migration path) end with the identical
  index set; downgrade can drop by name on both paths.  The named UNIQUE
  constraint ``uq_artifact_records_tenant_ref`` creates its backing UNIQUE
  index under the same name on both paths (no separate create_index needed).
  The partial UNIQUE index ``uq_evidence_records_reverify`` is declared as an
  ``Index`` object in the model (create_all creates it by name) and is created
  here with the same name + the same ``postgresql_where`` clause.

Revision ID: f072_artifact_evidence
Revises: f071_planning_persistence
Create Date: 2026-09-30 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f072_artifact_evidence"
down_revision: str | None = "f071_planning_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARTIFACT_RECORDS_TABLE = "artifact_records"
EVIDENCE_RECORDS_TABLE = "evidence_records"

# Plain btree index names create_all produces for the model's index=True
# columns (model/migration lockstep — see docstring).  Name -> single column.
ARTIFACT_INDEXES = {
    "ix_artifact_records_tenant_id": "tenant_id",
    "ix_artifact_records_project_id": "project_id",
    "ix_artifact_records_task_id": "task_id",
    "ix_artifact_records_execution_id": "execution_id",
    "ix_artifact_records_revision_ref": "revision_ref",
}
EVIDENCE_INDEXES = {
    "ix_evidence_records_tenant_id": "tenant_id",
    "ix_evidence_records_project_id": "project_id",
    "ix_evidence_records_task_id": "task_id",
    "ix_evidence_records_execution_id": "execution_id",
    "ix_evidence_records_revision_ref": "revision_ref",
}

# The partial UNIQUE index (invariant 5, AgentRunEvent precedent): one original
# capture per (tenant, kind, subject, revision); a re-verify row (payload
# carrying reverify_of) is excluded via the WHERE clause.  Declared in the model
# as an Index object, so create_all creates it under this exact name; the
# migration must match the name AND the WHERE clause.
REVERIFY_PARTIAL_INDEX = "uq_evidence_records_reverify"
REVERIFY_PARTIAL_WHERE = "NOT COALESCE(payload ? 'reverify_of', FALSE)"


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _existing_indexes(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Index names present on ``table_name`` (empty when the table is absent)."""
    if table_name not in _existing_tables(bind):
        return set()
    return {str(row["name"]) for row in sa.inspect(bind).get_indexes(table_name)}


def _create_artifact_records(conn: sa.engine.Connection) -> None:
    op.create_table(
        ARTIFACT_RECORDS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Project / Task / execution are the provenance edges (design §3.A1);
        # all nullable + SET NULL: the new FKs only *reference* the frozen
        # tables — no column or constraint is added to them.
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user", postgresql.UUID(as_uuid=True), nullable=True),
        # Closed set ARTIFACT_TYPES (§3.1) + closed STORAGE_SCHEMES (§3.2):
        # plain String columns, validated at the DAO/service layer (no PG
        # enum, matching the model's String(40)/String(500) spec).
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("storage_scheme", sa.String(length=40), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("revision_ref", sa.String(length=120), nullable=True),
        # One-way DRAFT -> SEALED boundary (decision D2); server_default so the
        # create_all and migration paths agree on the DDL (f069 lesson).
        sa.Column("seal_status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        # Self-FK: the ONLY row-to-row link a rework adds (D2), SET NULL.
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Invariant 1 (D5): exactly one of (execution_id, created_by_user).
        sa.CheckConstraint(
            "(execution_id IS NULL) <> (created_by_user IS NULL)",
            name="ck_artifact_records_source",
        ),
        # Invariant 2 (D2): seal_status='SEALED' <=> sealed_at IS NOT NULL.
        sa.CheckConstraint(
            "(seal_status = 'SEALED') = (sealed_at IS NOT NULL)",
            name="ck_artifact_records_seal",
        ),
        # Invariant 4 (dedup): one record per (tenant, scheme, ref).
        sa.UniqueConstraint(
            "tenant_id", "storage_scheme", "storage_ref", name="uq_artifact_records_tenant_ref"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_tool_executions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by"], ["artifact_records.id"], ondelete="SET NULL"),
    )
    for index_name, column in ARTIFACT_INDEXES.items():
        op.create_index(index_name, ARTIFACT_RECORDS_TABLE, [column])


def _create_evidence_records(conn: sa.engine.Connection) -> None:
    op.create_table(
        EVIDENCE_RECORDS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Closed set EVIDENCE_KINDS (§5.1): plain String(40), validated at the
        # DAO/service layer.
        sa.Column("kind", sa.String(length=40), nullable=False),
        # Closed set EVIDENCE_OUTCOMES (§A2): plain String(20).
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("subject_ref", sa.String(length=500), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=True),
        sa.Column("revision_ref", sa.String(length=120), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_by_agent", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user", postgresql.UUID(as_uuid=True), nullable=True),
        # Born immutable: a single created_at, NO updated_at (decision D2).
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Invariant 3 (D5): at least one source AND a non-empty subject.
        sa.CheckConstraint(
            "(artifact_id IS NOT NULL OR execution_id IS NOT NULL "
            "OR created_by_agent IS NOT NULL OR created_by_user IS NOT NULL) "
            "AND char_length(subject_ref) > 0",
            name="ck_evidence_records_source",
        ),
        # outcome closed set as a DB CHECK backstop.
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'inconclusive')",
            name="ck_evidence_records_outcome",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_tool_executions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_agent"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user"], ["users.id"], ondelete="SET NULL"),
    )
    for index_name, column in EVIDENCE_INDEXES.items():
        op.create_index(index_name, EVIDENCE_RECORDS_TABLE, [column])
    # Invariant 5 (partial UNIQUE dedup, AgentRunEvent precedent): one original
    # capture per (tenant, kind, subject, revision); a re-verify row (payload
    # reverify_of) is excluded.  Matches the model's Index object exactly.
    op.create_index(
        REVERIFY_PARTIAL_INDEX,
        EVIDENCE_RECORDS_TABLE,
        ["tenant_id", "kind", "subject_ref", "revision_ref"],
        unique=True,
        postgresql_where=sa.text(REVERIFY_PARTIAL_WHERE),
    )


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    # Create in dependency order: artifact_records first (evidence_records
    # references it via the artifact_id FK), then evidence_records.
    if ARTIFACT_RECORDS_TABLE not in tables:
        _create_artifact_records(conn)
    if EVIDENCE_RECORDS_TABLE not in tables:
        _create_evidence_records(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    # Drop in reverse dependency order: evidence_records -> artifact_records.
    # Every index drop is guarded against the index actually existing
    # (mirrors the _existing_tables guard above), so downgrade is a clean
    # no-op on the create_all-provisioned fresh-DB path, where 001 pre-created
    # the tables from the model metadata and this migration's create_index
    # never ran.
    if EVIDENCE_RECORDS_TABLE in tables:
        ev_indexes = _existing_indexes(conn, EVIDENCE_RECORDS_TABLE)
        for index_name in EVIDENCE_INDEXES:
            if index_name in ev_indexes:
                op.drop_index(index_name, table_name=EVIDENCE_RECORDS_TABLE)
        if REVERIFY_PARTIAL_INDEX in ev_indexes:
            op.drop_index(REVERIFY_PARTIAL_INDEX, table_name=EVIDENCE_RECORDS_TABLE)
        op.drop_table(EVIDENCE_RECORDS_TABLE)
    if ARTIFACT_RECORDS_TABLE in tables:
        art_indexes = _existing_indexes(conn, ARTIFACT_RECORDS_TABLE)
        for index_name in ARTIFACT_INDEXES:
            if index_name in art_indexes:
                op.drop_index(index_name, table_name=ARTIFACT_RECORDS_TABLE)
        op.drop_table(ARTIFACT_RECORDS_TABLE)

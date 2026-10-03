"""Add the Phase 4 Delivery record table.

Background:
  docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md §6
  (card t_4185daed, C-D1..C-D4) fixes the delivery *contract*; this card
  (t_af586c02) owns the design-reserved delivery *record* — the ONE bounded
  new schema piece of Phase 4 (the ``delivery_records`` table + its DAO + a
  service lane, design §8 "Delivery record storage owner").  The record is
  built ONLY on Completed + Approved + Evidence (C-D1): a row may be opened
  only against a scope whose live evaluation is ``CP_OK`` and may cite only
  ``SEALED`` + current-valid-approving artifact ids (the provenance chain
  delivery -> CP evaluation C5 row -> approving review row -> sealed
  artifact set).  "Agent says done -> Delivery" is structurally impossible:
  with no SEALED + approving ledger set there is no delivery input and no
  row is written.

Scope:
  DDL-only.  Creates the single ``delivery_records`` table, its plain
  btree indexes (named exactly as the model's ``index=True`` columns
  produce under create_all), and the three DB CHECK invariants (the D5 XOR
  source, the closed DELIVERY_STATES set, and the terminal-stamp coupling
  state-in-{DELIVERED,FAILED} <=> executed_at IS NOT NULL, mirrors of the
  parent f072 ``ck_artifact_records_seal``).  No PG enum types (the closed
  sets ``DELIVERY_STATES`` / ``DELIVERY_DESTINATION_KINDS`` are "String +
  service validation", the f069 created_reason precedent — matching the
  model's plain String columns).  No data backfill; no changes to the
  frozen models or to the f072 ledger tables — the new FKs (SET NULL /
  CASCADE on tenants) only *reference* those tables, adding no column or
  constraint to them.

Index lockstep (f068/f069/f072 lesson):
  The model declares ``index=True`` on tenant_id / project_id / scope_ref,
  so create_all produces plain btree indexes named ``ix_delivery_records_
  <col>``.  This migration creates indexes with exactly those names so a
  fresh DB (create_all path) and an existing DB (migration path) end with
  the identical index set; downgrade drops them by name.

Chains off the single head ``f072_artifact_evidence`` (the cp_decision_row_id
FK references the f072 ``evidence_records`` table).

Revision ID: f073_delivery_records
Revises: f072_artifact_evidence
Create Date: 2026-10-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f073_delivery_records"
down_revision: str | None = "f072_artifact_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_RECORDS_TABLE = "delivery_records"

# Plain btree index names create_all produces for the model's index=True
# columns (model/migration lockstep — see docstring).  Name -> single column.
DELIVERY_INDEXES = {
    "ix_delivery_records_tenant_id": "tenant_id",
    "ix_delivery_records_project_id": "project_id",
    "ix_delivery_records_scope_ref": "scope_ref",
}


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    """Table names present in the current schema."""
    return {str(name) for name in sa.inspect(bind).get_table_names()}


def _existing_indexes(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Index names present on ``table_name`` (empty when the table is absent)."""
    if table_name not in _existing_tables(bind):
        return set()
    return {str(row["name"]) for row in sa.inspect(bind).get_indexes(table_name)}


def _create_delivery_records(conn: sa.engine.Connection) -> None:
    op.create_table(
        DELIVERY_RECORDS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Tenant ownership (D6): non-nullable + indexed, CASCADE — the tenant
        # row owns its delivery history.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Owning project of the delivery scope (convenience link, matrix P1 —
        # never a second authority).  SET NULL: deleting a project keeps its
        # delivery history.
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        # C-D1 scope locator: "wp://{id}" | "project://{id}" (closed
        # vocabulary, service-validated).
        sa.Column("scope_ref", sa.String(length=500), nullable=False),
        # C-D3: the bounded cited SEALED+approved artifact ids + the citing
        # current-valid-approving review row ids (JSONB lists of uuid
        # strings, bounded at the DAO/service boundary).
        sa.Column("artifact_ids", postgresql.JSONB(), nullable=True),
        sa.Column("cited_review_row_ids", postgresql.JSONB(), nullable=True),
        # The C5 decision row (f072 evidence_records kind='structured') that
        # recorded the scope's CP_OK evaluation this delivery is gated on —
        # the provenance chain link.  SET NULL: a C5 row delete never
        # destroys a delivery record.
        sa.Column("cp_decision_row_id", postgresql.UUID(as_uuid=True), nullable=True),
        # C-D2 destination (closed V1 vocabulary, "String + service
        # validation" — the closed set lives with the completion lane; no DB
        # enum, the f069 precedent).  destination_ref is the locator (a
        # channel-delivery fact / a PublishedPage.short_id / NULL for
        # project_record, where the record IS the destination).
        sa.Column("destination_kind", sa.String(length=20), nullable=False),
        sa.Column("destination_ref", sa.String(length=500), nullable=True),
        # C-D3 state: closed DELIVERY_STATES, PENDING on decision; the
        # server_default so the create_all and f073 migration paths agree on
        # the DDL (f069 index-lockstep lesson).
        sa.Column("state", sa.String(length=20), server_default="PENDING", nullable=False),
        # C-D3 timestamps: decided_at stamps the decision; executed_at is
        # NULL while PENDING and stamped one-way on the terminal transition.
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        # D5 XOR source (decided_by agent OR user, ck_delivery_records_source
        # backstop); both SET NULL so a principal delete keeps the record.
        sa.Column("decided_by_agent", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_user", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # D5 XOR source (mirror of ck_artifact_records_source).
        sa.CheckConstraint(
            "(decided_by_agent IS NULL) <> (decided_by_user IS NULL)",
            name="ck_delivery_records_source",
        ),
        # Closed DELIVERY_STATES as a DB backstop (mirror of
        # ck_evidence_records_outcome).
        sa.CheckConstraint(
            "state IN ('PENDING', 'DELIVERED', 'FAILED')",
            name="ck_delivery_records_state",
        ),
        # The terminal-state stamping boundary (mirror of
        # ck_artifact_records_seal): a terminal row must carry executed_at;
        # PENDING must not.
        sa.CheckConstraint(
            "(state IN ('DELIVERED', 'FAILED')) = (executed_at IS NOT NULL)",
            name="ck_delivery_records_executed",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cp_decision_row_id"], ["evidence_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_agent"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user"], ["users.id"], ondelete="SET NULL"),
    )
    for index_name, column in DELIVERY_INDEXES.items():
        op.create_index(index_name, DELIVERY_RECORDS_TABLE, [column])


def upgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    # Fresh deployments where create_all already built the table from the
    # registered metadata (delivery_record.py is imported by env.py) are
    # handled by the same guard.
    if DELIVERY_RECORDS_TABLE not in tables:
        _create_delivery_records(conn)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _existing_tables(conn)
    # Drop the indexes (guarded against the index actually existing —
    # mirrors the f072 downgrade guard so a create_all-provisioned fresh-DB
    # path is a clean no-op), then the table.
    if DELIVERY_RECORDS_TABLE in tables:
        indexes = _existing_indexes(conn, DELIVERY_RECORDS_TABLE)
        for index_name in DELIVERY_INDEXES:
            if index_name in indexes:
                op.drop_index(index_name, table_name=DELIVERY_RECORDS_TABLE)
        op.drop_table(DELIVERY_RECORDS_TABLE)

"""The Phase 4 Delivery lane record table (card ``t_af586c02``, design §6).

Per docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md §6
(C-D1..C-D4), delivery is a **separate, later decision** built ONLY on
Completed + Approved + Evidence.  This table is the design-reserved
``delivery_records`` record (design §8 "Delivery record storage owner") —
the ONE bounded new schema piece in Phase 4 (its table + DAO + a service
lane).  It reuses the CD_* contract + the CP evaluator + the two f072
ledger tables from the completion lane; it does NOT reinvent any of them.

Guarantees honored (Root §6 / design §6):

- **C-D1 (gate).**  A record may be opened only against a scope whose live
  evaluation is ``CP_OK`` and may cite only ``SEALED`` + current-valid-approving
  artifacts.  "Agent says done -> Delivery" is structurally impossible at
  every hop: with no SEALED + approving ledger set there is no delivery input
  and no row is written (fail-closed, nothing persisted on a non-``CD_OK``).
- **C-D2 (destination).**  ``destination_kind`` is the closed V1 vocabulary
  (``channel`` / ``published_page`` / ``project_record`` — the closed set
  ``DELIVERY_DESTINATION_KINDS`` owned by the completion lane, consumed here,
  never redefined).  No external publish platform in V1.
- **C-D3 (record fields).**  provenance (the CP_OK C5 decision row + cited
  artifact ids + citing review row ids), delivered artifact ids, destination,
  delivery state (closed ``DELIVERY_STATES``), decided/executed timestamps,
  D5 XOR source (``decided_by_agent`` XOR ``decided_by_user``), and the
  append-only audit boundary.
- **C-D4 (independence).**  the record's trusted inputs are the ledger rows
  named above; the Run's ``final_answer`` / ``Task.status`` / gate verdict are
  NEVER delivery inputs.

Retention + immutability boundary (documented per the card):

- **Append-only:** there is no delete path on the owning DAO; a "change" to a
  delivery is a NEW record row, and a re-delivery of a scope that already has
  a terminal record is idempotent (the existing record is returned, never a
  duplicate).  ``RECALLED`` / record supersession is deferred (design §8) —
  V1 has no recall consumer.
- **Provenance is immutable once written:** the citing fields
  (``cp_decision_row_id`` / ``cited_review_row_ids`` / ``artifact_ids`` /
  ``scope_ref`` / ``destination_*`` / ``decided_by_*`` / ``decided_at``) are
  stamped at the PENDING write and are never mutated afterward.  The ONLY
  mutable fields are ``state`` (one-way PENDING -> a terminal state) and
  ``executed_at`` (stamped when a terminal state is reached) — the narrow
  transition window the closed ``DELIVERY_STATES`` boundary defines.

The migration that provisions this table is f073
(``alembic/versions/v1_11_5_f073_delivery_records.py``), chained off the
single head f072; the model and the migration are kept in lockstep (the
f068/f069/f072 lesson) so a fresh-DB (create_all) path and an existing-DB
(migration) path end with the identical schema.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: The closed V1 delivery state set (design §6.3 C-D3): PENDING on decision,
#: terminal states set by the owning transport or by the lane on
#: ``project_record``.  No ``RECALLED`` in V1 (recall policy is deferred,
#: design §8 — there is no recall consumer).  A DB CHECK backstop enforces
#: this closed set (mirror of ``ck_evidence_records_outcome``).
DELIVERY_STATES = ("PENDING", "DELIVERED", "FAILED")

#: Bounded citation lists (complete-or-absent, backend AGENTS.md): the cited
#: artifact set and the citing review rows are capped so one record can never
#: carry an unbounded payload.
MAX_DELIVERY_CITED_IDS = 1000


class DeliveryRecord(Base):
    """One delivery record (design §6.3 C-D3) — a durable, tenant-scoped,
    append-only record of a delivery event (the done work was handed over,
    where, when), built ONLY on Completed + Approved + Evidence (C-D1).

    The record *is* an event, not a second authority over completion: it
    cites the CP_OK C5 decision row + the approving review rows + the SEALED
    artifact set (the provenance chain: delivery -> CP evaluation ->
    approving review row -> sealed artifact set, all ledger-queryable), and it
    never feeds back into ``CT``/``CW``/``CP`` (design §6.1, no cycle / no
    second authority).
    """

    __tablename__ = "delivery_records"
    # Tenant ownership (D6): non-nullable, indexed, picked up by the
    # do_orm_execute tenant filter automatically.
    __tenant_scoped__ = True

    __table_args__ = (
        # D5 XOR source (mirror of ck_artifact_records_source): exactly one of
        # (decided_by_agent, decided_by_user) is non-null.
        CheckConstraint(
            "(decided_by_agent IS NULL) <> (decided_by_user IS NULL)",
            name="ck_delivery_records_source",
        ),
        # Closed DELIVERY_STATES as a DB backstop (mirror of
        # ck_evidence_records_outcome): a state outside the closed set is
        # rejected at the boundary, fail-closed.
        CheckConstraint(
            "state IN ('PENDING', 'DELIVERED', 'FAILED')",
            name="ck_delivery_records_state",
        ),
        # The terminal-state stamping boundary (mirror of
        # ck_artifact_records_seal): a DELIVERED / FAILED row must carry its
        # executed_at; a PENDING row must not.  The owning DAO/transport is
        # the only writer of this narrow transition window; the provenance
        # fields are immutable once the PENDING row is written.
        CheckConstraint(
            "(state IN ('DELIVERED', 'FAILED')) = (executed_at IS NOT NULL)",
            name="ck_delivery_records_executed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Tenant ownership (D6).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Owning project of the delivery scope (C-D3: project_id + scope +
    # tenant_id).  SET NULL: deleting a project keeps its delivery history.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # C-D1 scope: ``wp://{id}`` | ``project://{id}`` — the scope the delivery
    # was opened against (the subject the CP gate evaluated).  Non-empty.
    scope_ref: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    # C-D3 artifact_ids: the BOUNDED list of SEALED + current-valid-approving
    # artifact ids being delivered (JSONB list of uuid strings; each id is
    # re-checked against the current-valid ledger state at write time — a
    # rework that superseded one between decision and write fails the gate,
    # CD_NO_SEALED_APPROVED, and nothing is written).
    artifact_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Citing review row ids: the current-valid-approving ``kind='review'``
    # rows (evidence_records) the cited set relies on (bounded JSONB list of
    # uuid strings).  The provenance chain link (delivery -> approving
    # review row).
    cited_review_row_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # The C5 decision row (evidence_records kind='structured') that recorded
    # the scope's CP_OK evaluation this delivery is gated on (the "citing C5
    # decision row" / "CP_OK evaluation" provenance field).  SET NULL: a C5
    # row delete never destroys a delivery record.
    cp_decision_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_records.id", ondelete="SET NULL"), nullable=True
    )
    # C-D2 destination (closed V1 vocabulary, decision C-D2): the kind is the
    # closed ``DELIVERY_DESTINATION_KINDS`` set (channel / published_page /
    # project_record), validated at the service/DAO boundary (String + service
    # validation, the f069 precedent — the set is a reviewable closed-set
    # extension, never a hard DB CHECK); the ref is the destination locator
    # (a channel-delivery fact / a PublishedPage.short_id / NULL for
    # project_record, where the record IS the destination).
    destination_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    destination_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # C-D3 state: closed DELIVERY_STATES; PENDING on decision, terminal states
    # set by the owning transport or by the lane on project_record.  The
    # server_default so the create_all and f073 migration paths agree on the
    # DDL (f069 index-lockstep lesson).
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", server_default="PENDING"
    )
    # C-D3 timestamps: decided_at stamps the decision (PENDING write);
    # executed_at stamps a terminal state (the delivery happened).
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # D5 XOR source (decided_by agent OR user, ck_delivery_records_source).
    decided_by_agent: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # updated_at changes only in the state transition (PENDING -> terminal);
    # the provenance fields are immutable once the PENDING row is written.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "DELIVERY_STATES",
    "MAX_DELIVERY_CITED_IDS",
    "DeliveryRecord",
]

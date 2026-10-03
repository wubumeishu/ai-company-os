"""Tenant-scoped, append-only DAO for the Phase 4 delivery record table.

Per docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md §6
(card ``t_af586c02``), the ``delivery_records`` table is the ONE bounded new
schema piece of Phase 4: the design-reserved delivery record + its DAO + a
service lane.  This module owns the *storage boundary* only (the append, the
closed-set / provenance / state-boundary validation, and the bounded audit
reads); the CD_* *decision* logic (C-D1..C-D4) belongs to the owning delivery
service, never here (backend/app/dao/AGENTS.md §1 "No Business Logic in DAO").

Boundary rules honored:

- **Append-only, no delete path** (C-D3 / the design §8 retention note): a
  "change" to a delivery is a NEW record row; rework supersedes the cited set
  and the next evaluation fails closed — history is never overwritten.
- **Closed sets fail closed at the DAO boundary** (the f069 "String, closed"
  + service re-validation precedent, mirrored by the parent ``f072`` ledger
  DAOs): ``destination_kind`` is re-checked against the CLOSED set the
  completion lane owns (``DELIVERY_DESTINATION_KINDS`` — consumed, never
  redefined), and ``state`` against the model's closed ``DELIVERY_STATES``.
  An unknown value is rejected with the named fail-closed code before any row
  is written (Root §5: unknown -> the catch-all, never the OK code).
- **D5 XOR provenance** (``ck_delivery_records_source``): exactly one of
  (``decided_by_agent``, ``decided_by_user``) — the DB CHECK is the backstop,
  the named-code check here is the boundary.
- **The only mutable window** (documented immutability boundary, C-D3): the
  PENDING->terminal ``state`` transition (``transition_state``) is the ONLY
  write after the PENDING append; it stamps ``executed_at`` one-way and is
  rejected out of a terminal state (fail-closed).  All provenance fields
  (``cp_decision_row_id`` / ``cited_review_row_ids`` / ``artifact_ids`` /
  ``scope_ref`` / ``destination_*`` / ``decided_by_*`` / ``decided_at``) are
  immutable once written — the DAO exposes no path to mutate them.
- **Bounded reads** (backend AGENTS.md complete-operation bounds): the audit
  reads are limit-bounded and tenant-scoped (D6 scope-inject).

Layering: this DAO re-checks the destination closed set against the
completion lane's ``DELIVERY_DESTINATION_KINDS`` constant (the closed-set
*owner*, design C-D2) — a constant import, not a behavior import (no
service->DAO cycle: the completion lane never imports this module).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from app.dao.base import TenantScopedBaseDAO
from app.models.delivery_record import DELIVERY_STATES, MAX_DELIVERY_CITED_IDS, DeliveryRecord
from app.services.completion_service import (
    CD_DESTINATION_INVALID,
    CD_EVAL_ERROR,
    DELIVERY_DESTINATION_KINDS,
)


class DeliveryRecordClosedError(ValueError):
    """A closed-set / provenance / state-boundary validation failure (fail-closed).

    Carries a ``code`` naming the closed rejection code (a member of the CD_*
    contract the delivery lane consumes, or the catch-all ``CD_EVAL_ERROR``)
    so the owning service / transport maps it without re-parsing the message
    (mirrors ``ArtifactEvidenceClosedError``).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}" + (f": {detail}" if detail else ""))


def _validate_cited_ids(value: object, field_name: str) -> None:
    """The cited lists are bounded JSONB lists of uuid strings (C-D3).

    A non-list or an over-cap list is an unreadable / unbounded input ->
    fail closed (never a silent partial, complete-or-absent).
    """
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeliveryRecordClosedError(CD_EVAL_ERROR, f"{field_name} must be a list of uuid strings")
    if len(value) > MAX_DELIVERY_CITED_IDS:
        raise DeliveryRecordClosedError(
            CD_EVAL_ERROR, f"{field_name} exceeds the bounded cap {MAX_DELIVERY_CITED_IDS}"
        )


class DeliveryRecordDAO(TenantScopedBaseDAO[DeliveryRecord]):
    """Tenant-scoped, append-only DAO for ``delivery_records`` rows.

    Read + insert + the one-way PENDING->terminal state transition.  No
    ``delete`` path (C-D3, design §8): a rework supersedes the cited set and
    the next evaluation fails closed; the PENDING record is never clobbered.
    Writes flush; the owning service/transaction commits.
    """

    def __init__(self) -> None:
        super().__init__(DeliveryRecord)

    def _validate_new(self, record: DeliveryRecord) -> None:
        """The PENDING-append boundary: closed state + destination, D5 XOR
        source, bounded cited lists, and the terminal-state stamping rule
        (a terminal row must carry ``executed_at``; PENDING must not —
        mirrors the parent ``ck_artifact_records_seal`` stamp coupling)."""
        if record.state not in DELIVERY_STATES:
            # Unknown state value: the catch-all, fail closed (Root §5).
            raise DeliveryRecordClosedError(CD_EVAL_ERROR, f"state={record.state!r} is not in {DELIVERY_STATES}")
        # C-D2: the destination kind is the CLOSED V1 vocabulary owned by the
        # completion lane (consumed, never redefined here).
        if record.destination_kind not in DELIVERY_DESTINATION_KINDS:
            raise DeliveryRecordClosedError(
                CD_DESTINATION_INVALID,
                f"destination_kind={record.destination_kind!r} is not in the closed V1 vocabulary {DELIVERY_DESTINATION_KINDS}",
            )
        if not record.scope_ref or not record.scope_ref.strip():
            raise DeliveryRecordClosedError(CD_EVAL_ERROR, "scope_ref must be non-empty")
        # D5 XOR source (ck_delivery_records_source): exactly one of
        # (decided_by_agent, decided_by_user).
        if record.decided_by_agent is not None and record.decided_by_user is not None:
            raise DeliveryRecordClosedError(
                CD_EVAL_ERROR, "a delivery record has exactly one source: decided_by_agent XOR decided_by_user"
            )
        if record.decided_by_agent is None and record.decided_by_user is None:
            raise DeliveryRecordClosedError(CD_EVAL_ERROR, "a delivery record must carry decided_by_agent OR decided_by_user")
        # C-D3 bounded citation lists (complete-or-absent).
        _validate_cited_ids(record.artifact_ids, "artifact_ids")
        _validate_cited_ids(record.cited_review_row_ids, "cited_review_row_ids")
        # The terminal-state stamping boundary (ck_delivery_records_executed):
        # a DELIVERED / FAILED row must carry its executed_at; PENDING may not.
        if record.state in ("DELIVERED", "FAILED") and record.executed_at is None:
            record.executed_at = datetime.now(UTC)
        if record.state == "PENDING" and record.executed_at is not None:
            raise DeliveryRecordClosedError(CD_EVAL_ERROR, "a PENDING delivery record must not carry executed_at")

    async def add_record(
        self,
        record: DeliveryRecord,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> DeliveryRecord:
        """Append one delivery record in the caller's tenant + transaction.

        PENDING rows are the decision-time write (C-D3); a ``project_record``
        destination is written already terminal (DELIVERED + ``executed_at``
        stamped — the lane owns that terminal state because the record IS the
        destination, design §6.3).  Writes flush; the owning service commits.
        """
        self._validate_new(record)
        self.add_scoped(db, record, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(record, attribute_names=["created_at", "updated_at"])
        return record

    # --- bounded audit reads (C-D3 "audit trail", D6 scope-inject) --------
    async def list_by_scope(
        self,
        scope_ref: str,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[DeliveryRecord]:
        """The delivery records of one scope (newest first), tenant-scoped.

        The audit read-back (C-D3: the row is its own audit record) + the
        idempotent re-delivery read (the service looks for an existing terminal
        record of the cited set before appending).  Bounded (complete-or-
        absent); a scope with more than ``limit`` records is a real-volume
        signal — retention is an ops decision (design §8), never an unbounded
        fan-out here.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(DeliveryRecord)
            .where(DeliveryRecord.scope_ref == scope_ref)
            .order_by(DeliveryRecord.created_at.desc(), DeliveryRecord.id.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(DeliveryRecord.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    # --- the ONLY post-append write path: the one-way terminal transition --
    async def transition_state(
        self,
        record: DeliveryRecord,
        *,
        new_state: str,
        db,
    ) -> DeliveryRecord:
        """Move one PENDING record to a terminal state (the owning transport's
        terminal write, C-D3: "terminal states set by the owning transport").

        One-way + fail-closed: PENDING -> DELIVERED / FAILED is the ONLY legal
        move; a record already terminal (or in an unknown state) refuses the
        transition (``CD_EVAL_ERROR``).  Stamps ``executed_at`` exactly once,
        on entering the terminal state.  This is the narrow mutable window —
        the provenance fields are never touched.
        """
        if new_state not in DELIVERY_STATES:
            raise DeliveryRecordClosedError(CD_EVAL_ERROR, f"new_state={new_state!r} is not in {DELIVERY_STATES}")
        if record.state != "PENDING":
            # No terminal -> anything move (no RECALLED in V1, design §8):
            # a re-delivery is a NEW record, never a rewrite of this one.
            raise DeliveryRecordClosedError(
                CD_EVAL_ERROR, f"delivery record is already in terminal state {record.state!r}"
            )
        record.state = new_state
        record.executed_at = datetime.now(UTC)
        async with self.session(db=db) as session_db:
            session_db.add(record)
            await session_db.flush()
            await session_db.refresh(record, attribute_names=["updated_at", "executed_at"])
        return record


delivery_record_dao = DeliveryRecordDAO()

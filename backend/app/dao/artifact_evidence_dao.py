"""Tenant-scoped, append-only DAOs for the Phase 4 Artifact / Evidence domain.

Per docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md §3/§7/§9 the two
ledger tables (``artifact_records`` / ``evidence_records``) are reached ONLY
through ``TenantScopedBaseDAO`` (decision D6): the active tenant (bound by
``TenantContextMiddleware`` on every request, or ``tenant_context()`` in
background code) filters reads automatically, and ``add_scoped`` forces
tenant alignment on writes.

Layering rules honored (backend/app/dao/AGENTS.md):
- No business logic in this module — only DB reads, writes, scoped queries,
  and the closed-set / provenance / seal-boundary validation that the design
  assigns to the owning layer of the *complete* write operation.  The
  Review / Rework transition guards (the ``RV_*`` result codes) belong to the
  owning review service, NOT here.
- **Append-only (decision D2, invariant 7):** neither DAO exposes a
  ``delete`` path.  ``ArtifactRecordDAO`` supports read + insert + a DRAFT-only
  update + the one-way DRAFT→SEALED seal; ``EvidenceRecordDAO`` supports read +
  insert only (evidence is born immutable — no ``updated_at`` at all).
- Closed sets fail closed at the DAO boundary (the ``PLANNING_RUN_STATUSES``
  re-validation pattern, design §7.8): a bad ``type`` / ``kind`` /
  ``storage_scheme`` / ``seal_status`` value is rejected with a named closed
  code before any row is written.
- Provenance (decision D5) and the re-verify dedup (invariant 5) are
  service/DAO fail-closed checks that ride the frozen DB CHECKs / partial
  unique index as the backstop.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.dao.base import TenantScopedBaseDAO
from app.models.artifact_evidence import (
    ARTIFACT_TYPES,
    EVIDENCE_KINDS,
    EVIDENCE_OUTCOMES,
    MAX_EVIDENCE_PAYLOAD_BYTES,
    SEAL_STATUSES,
    STORAGE_SCHEMES,
    ArtifactRecord,
    EvidenceRecord,
)


class ArtifactEvidenceClosedError(ValueError):
    """A closed-set / provenance / seal-boundary validation failure (fail-closed).

    Carries a ``code`` attribute naming the closed rejection code so the owning
    service can map it to the transport without re-parsing the message
    (mirrors ``planning_dao.ClosedCodeError`` with an explicit closed code).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}" + (f": {detail}" if detail else ""))


def _validate_closed(value: Any, closed_set: tuple[str, ...], field_name: str, code: str) -> None:
    """Fail closed when ``value`` is not in the closed set (design D4/§7.8)."""
    if value not in closed_set:
        raise ArtifactEvidenceClosedError(code, f"{field_name}={value!r} is not in the closed set {closed_set}")


def _payload_byte_size(payload: dict | None) -> int:
    """The encoded JSONB size of an evidence payload (complete-operation bound)."""
    if payload is None:
        return 0
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class ArtifactRecordDAO(TenantScopedBaseDAO[ArtifactRecord]):
    """Tenant-scoped, append-only DAO for ``artifact_records`` rows.

    Read + insert + DRAFT-only update + the one-way DRAFT→SEALED seal.  No
    ``delete`` path (invariant 7, decision D2): "change" = insert a new row +
    ``superseded_by`` link.  Writes flush; the owning service/transaction
    commits.
    """

    def __init__(self) -> None:
        super().__init__(ArtifactRecord)

    def _validate_new(self, record: ArtifactRecord) -> None:
        _validate_closed(record.type, ARTIFACT_TYPES, "type", "AE_UNKNOWN_TYPE")
        _validate_closed(record.storage_scheme, STORAGE_SCHEMES, "storage_scheme", "AE_UNKNOWN_SCHEME")
        if not record.storage_ref or not record.storage_ref.strip():
            raise ArtifactEvidenceClosedError("AE_INVALID_INPUT", "storage_ref must be non-empty")
        # D5 (invariant 1): exactly one provenance edge — an agent-produced
        # row carries execution_id (and its agent), a human-supplied row
        # carries created_by_user.  The DB CHECK is the backstop; this is the
        # named-code boundary (fail-closed "no Artifact without provenance").
        has_execution = record.execution_id is not None
        has_user = record.created_by_user is not None
        if has_execution and has_user:
            raise ArtifactEvidenceClosedError("EV_NO_SOURCE", "an artifact has exactly one source: execution XOR created_by_user")
        if not has_execution and not has_user:
            raise ArtifactEvidenceClosedError("EV_NO_SOURCE", "an artifact must carry execution_id OR created_by_user")
        # A NEW row is born DRAFT; sealing is a distinct one-way step.  The
        # column's DRAFT default is not materialized on a pre-insert ORM
        # instance (seal_status None == "take the DRAFT default"), so normalize
        # here — the model default is what the row is actually born with.
        if record.seal_status is None:
            record.seal_status = "DRAFT"
        _validate_closed(record.seal_status, SEAL_STATUSES, "seal_status", "AE_UNKNOWN_SEAL")

    async def add_artifact(
        self,
        record: ArtifactRecord,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> ArtifactRecord:
        """Register one DRAFT artifact row in the caller's tenant + transaction.

        The flush may raise ``IntegrityError`` on
        ``uq_artifact_records_tenant_ref`` (an idempotent-cite re-run citing the
        same locator); the caller owns the re-read recovery (the
        ``AE_ALREADY_EXISTS`` → return the existing row pattern, design §10).
        """
        self._validate_new(record)
        self.add_scoped(db, record, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(record, attribute_names=["created_at", "updated_at"])
        return record

    async def get_by_locator(
        self,
        storage_scheme: str,
        storage_ref: str,
        *,
        db=None,
    ) -> ArtifactRecord | None:
        """The (unique) record for one locator under the active tenant.

        ``UNIQUE(tenant_id, storage_scheme, storage_ref)`` (invariant 4) makes
        this a single-row read — the idempotent-cite re-read path (design §10).
        """
        tenant_id = self._require_tenant_id()
        stmt = select(ArtifactRecord).where(
            ArtifactRecord.storage_scheme == storage_scheme,
            ArtifactRecord.storage_ref == storage_ref,
        )
        if tenant_id is not None:
            stmt = stmt.where(ArtifactRecord.tenant_id == tenant_id)
        stmt = stmt.limit(1)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalar_one_or_none()

    async def list_by_task(
        self,
        task_id: uuid.UUID,
        *,
        current_only: bool = False,
        db=None,
        limit: int = 200,
    ) -> Sequence[ArtifactRecord]:
        """Artifacts produced by one Task (design §4.2 "由哪个 Task 产生").

        ``current_only`` returns only the current (non-superseded) set
        ``A_c(T)`` — the input the review / completion lanes read; every row
        is always tenant-scoped.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(ArtifactRecord)
            .where(ArtifactRecord.task_id == task_id)
            .order_by(ArtifactRecord.created_at.asc())
            .limit(limit)
        )
        if current_only:
            stmt = stmt.where(ArtifactRecord.superseded_by.is_(None))
        if tenant_id is not None:
            stmt = stmt.where(ArtifactRecord.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    def _validate_draft_update(self, record: ArtifactRecord) -> None:
        """The DRAFT-only update boundary: the only mutable window (design §3.3).

        ``title`` / ``content_hash`` / ``revision_ref`` may change while DRAFT;
        any write to a SEALED row is rejected (invariant 10, ``AE_ALREADY_SEALED``).
        """
        if record.seal_status != "DRAFT":
            raise ArtifactEvidenceClosedError("AE_ALREADY_SEALED", "a SEALED artifact row is immutable")

    async def update_draft(
        self,
        record: ArtifactRecord,
        *,
        title: str | None = None,
        content_hash: str | None = None,
        revision_ref: str | None = None,
        db,
    ) -> ArtifactRecord:
        """Edit a DRAFT artifact in place; reject a SEALED row (invariant 10)."""
        self._validate_draft_update(record)
        async with self.session(db=db) as session_db:
            if title is not None:
                record.title = title
            if content_hash is not None:
                record.content_hash = content_hash
            if revision_ref is not None:
                record.revision_ref = revision_ref
            record.seal_status = "DRAFT"
            session_db.add(record)
            await session_db.flush()
            await session_db.refresh(record, attribute_names=["updated_at"])
            return record

    async def seal(
        self,
        record: ArtifactRecord,
        *,
        db,
    ) -> ArtifactRecord:
        """One-way DRAFT→SEALED (decision D2, invariant 10).

        A second seal on the same row is rejected (``AE_ALREADY_SEALED``); the
        only mutation a record ever owns.  Stamps ``sealed_at``.
        """
        if record.seal_status == "SEALED":
            raise ArtifactEvidenceClosedError("AE_ALREADY_SEALED", "artifact row is already SEALED")
        _validate_closed("SEALED", SEAL_STATUSES, "seal_status", "AE_UNKNOWN_SEAL")
        async with self.session(db=db) as session_db:
            record.seal_status = "SEALED"
            record.sealed_at = datetime.now(UTC)
            session_db.add(record)
            await session_db.flush()
            await session_db.refresh(record, attribute_names=["updated_at", "sealed_at"])
            return record

    async def supersede(
        self,
        old: ArtifactRecord,
        *,
        new_id: uuid.UUID,
        db,
    ) -> ArtifactRecord:
        """Link an old (current) artifact row to the new row that replaces it.

        Decision D2 rework: the *new* row is current, the *old* row becomes
        historical via ``superseded_by`` — never deleted or overwritten.  A
        row already superseded (SEALED-and-historical) refuses a second
        supersession so the link stays unambiguous.
        """
        if old.superseded_by is not None:
            raise ArtifactEvidenceClosedError(
                "AE_ALREADY_SEALED", f"artifact {old.id} was already superseded by {old.superseded_by}"
            )
        async with self.session(db=db) as session_db:
            old.superseded_by = new_id
            session_db.add(old)
            await session_db.flush()
            await session_db.refresh(old, attribute_names=["updated_at"])
            return old

    async def get_for_task(
        self,
        task_id: uuid.UUID,
        *,
        db=None,
    ) -> list[ArtifactRecord]:
        """All artifact rows of one Task (current + historical), tenant-scoped."""
        return list(await self.list_by_task(task_id, db=db, limit=1000))


class EvidenceRecordDAO(TenantScopedBaseDAO[EvidenceRecord]):
    """Tenant-scoped, insert-only DAO for ``evidence_records`` rows.

    Evidence is born immutable (decision D2, ``created_at`` only): the DAO
    supports read + insert.  No update, no delete — a re-verification writes a
    NEW row (``payload.reverify_of`` / ``rework_of``), never rewrites this one.
    """

    def __init__(self) -> None:
        super().__init__(EvidenceRecord)

    def _validate_new(self, record: EvidenceRecord, *, reviewer_builder_agents: set[uuid.UUID] | None = None) -> None:
        _validate_closed(record.kind, EVIDENCE_KINDS, "kind", "EV_UNKNOWN_KIND")
        _validate_closed(record.outcome, EVIDENCE_OUTCOMES, "outcome", "EV_UNKNOWN_OUTCOME")
        # D5 (invariant 3): at least one source + a non-empty subject.
        has_source = (
            record.artifact_id is not None
            or record.execution_id is not None
            or record.created_by_agent is not None
            or record.created_by_user is not None
        )
        if not has_source:
            raise ArtifactEvidenceClosedError(
                "EV_NO_SOURCE",
                "evidence must carry at least one of artifact_id / execution_id / created_by_agent / created_by_user",
            )
        if not record.subject_ref or not record.subject_ref.strip():
            raise ArtifactEvidenceClosedError("EV_NO_SUBJECT", "evidence must carry a non-empty subject_ref")
        # Invariant 12: a test result must say which command execution produced
        # it (an EV_NO_SOURCE specialization).
        if record.kind == "test_result" and record.execution_id is None:
            raise ArtifactEvidenceClosedError("EV_NO_SOURCE", "a test_result evidence must carry execution_id")
        # Invariant 11: bounded payload, complete-or-absent.
        if _payload_byte_size(record.payload) > MAX_EVIDENCE_PAYLOAD_BYTES:
            raise ArtifactEvidenceClosedError(
                "EV_PAYLOAD_OVERRUN",
                f"evidence payload exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes (complete-or-absent)",
            )
        # Invariant 13 (storage half of "no self-review"): a kind='review' row
        # must be captured by a disjoint reviewer.  The caller passes the set
        # of builder agents on the WorkPackage; a reviewer in that set is a
        # self-review and is rejected here (EV_REVIEW_NOT_INDEPENDENT).  The
        # assignment-time half stays the frozen PL_REVIEWER_NOT_INDEPENDENT —
        # never re-implemented.
        if record.kind == "review":
            if record.created_by_agent is None and record.created_by_user is None:
                raise ArtifactEvidenceClosedError(
                    "EV_NO_SOURCE", "a review verdict must carry a reviewer (created_by_agent or created_by_user)"
                )
            if (
                record.created_by_agent is not None
                and reviewer_builder_agents is not None
                and record.created_by_agent in reviewer_builder_agents
            ):
                raise ArtifactEvidenceClosedError(
                    "EV_REVIEW_NOT_INDEPENDENT",
                    f"reviewer {record.created_by_agent} is a builder on the same work package",
                )

    async def add_evidence(
        self,
        record: EvidenceRecord,
        *,
        tenant_id: uuid.UUID,
        db,
        reviewer_builder_agents: set[uuid.UUID] | None = None,
    ) -> EvidenceRecord:
        """Register one evidence row in the caller's tenant + transaction.

        ``reviewer_builder_agents`` (invariant 13) is the set of builder agent
        ids on the WorkPackage being reviewed; when the reviewer is one of
        them the insert is rejected with ``EV_REVIEW_NOT_INDEPENDENT``.  The
        flush may raise ``IntegrityError`` on the ``uq_evidence_records_reverify``
        partial unique index (an original capture of the same subject); the
        caller owns that recovery.
        """
        self._validate_new(record, reviewer_builder_agents=reviewer_builder_agents)
        self.add_scoped(db, record, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(record, attribute_names=["created_at"])
        return record

    async def list_reviews_for_task(
        self,
        task_id: uuid.UUID,
        *,
        db=None,
        limit: int = 200,
    ) -> Sequence[EvidenceRecord]:
        """All ``kind='review'`` verdict rows touching one Task's artifacts.

        The review lane uses this to derive the "current valid review" (design
        t_dbb0c0dd §3.4): the latest row over the *current* (non-superseded)
        artifact set.  Every row is tenant-scoped.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(EvidenceRecord)
            .where(EvidenceRecord.kind == "review")
            .order_by(EvidenceRecord.created_at.asc())
            .limit(limit)
        )
        # A review cites its artifact set; restrict to reviews whose cited
        # artifact belongs to this Task, OR that carry the task id directly.
        task_evidence = EvidenceRecord.task_id == task_id
        async with self.session(db=db, readonly=True) as session_db:
            rows: list[EvidenceRecord] = []
            # Pass 1: reviews recorded with the task id.
            stmt1 = stmt.where(task_evidence)
            if tenant_id is not None:
                stmt1 = stmt1.where(EvidenceRecord.tenant_id == tenant_id)
            rows.extend((await session_db.execute(stmt1)).scalars().all())
            # Pass 2: reviews whose cited artifact_id belongs to this Task.
            art_ids = select(ArtifactRecord.id).where(ArtifactRecord.task_id == task_id)
            if tenant_id is not None:
                art_ids = art_ids.where(ArtifactRecord.tenant_id == tenant_id)
            stmt2 = stmt.where(EvidenceRecord.artifact_id.in_(art_ids))
            if tenant_id is not None:
                stmt2 = stmt2.where(EvidenceRecord.tenant_id == tenant_id)
            rows.extend((await session_db.execute(stmt2)).scalars().all())
            seen: set[uuid.UUID] = set()
            deduped = [r for r in rows if not (r.id in seen or seen.add(r.id))]
            return deduped

    async def current_valid_review(
        self,
        task_id: uuid.UUID,
        *,
        db=None,
    ) -> EvidenceRecord | None:
        """The "current valid review" for one Task (design t_dbb0c0dd §3.4).

        The latest ``kind='review'`` row whose cited ``artifact_id`` is in the
        *current* (non-superseded) artifact set ``A_c(T)``.  An old APPROVE on
        a superseded set drops out automatically (it stays a historical row,
        never clobbering later changes — guarantee G2).  ``None`` when no
        review has been written over the current set yet.
        """
        tenant_id = self._require_tenant_id()
        current_art_ids = select(ArtifactRecord.id).where(
            ArtifactRecord.task_id == task_id,
            ArtifactRecord.superseded_by.is_(None),
        )
        if tenant_id is not None:
            current_art_ids = current_art_ids.where(ArtifactRecord.tenant_id == tenant_id)
        stmt = (
            select(EvidenceRecord)
            .where(
                EvidenceRecord.kind == "review",
                EvidenceRecord.artifact_id.in_(current_art_ids),
            )
            .order_by(EvidenceRecord.created_at.desc(), EvidenceRecord.id.desc())
            .limit(1)
        )
        if tenant_id is not None:
            stmt = stmt.where(EvidenceRecord.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalar_one_or_none()


artifact_record_dao = ArtifactRecordDAO()
evidence_record_dao = EvidenceRecordDAO()

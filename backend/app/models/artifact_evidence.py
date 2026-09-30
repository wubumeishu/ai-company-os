"""Artifact / Evidence persistence models (Phase 4, V1 minimal model).

Per docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md §3/§5/§7, two
append-only, tenant-scoped, content-addressed ledger tables make "what was
produced" (Artifact) and "what proves it" (Evidence) durable, re-verifiable
facts — built *on top of* the frozen Phase 2A–3 foundations, never in
parallel with them:

- ``ArtifactRecord`` (``artifact_records``) — a durable record of a produced
  thing (decision D1).  It never stores bytes: the ``storage_scheme`` +
  ``storage_ref`` pair points at one of the five existing storage authorities
  (decision D3), and ``content_hash`` makes the record content-addressed so a
  re-verification can detect drift or loss.
- ``EvidenceRecord`` (``evidence_records``) — a durable, re-verifiable
  assertion about an artifact / a task / a run (file revision, git revision,
  test result, tool result, published page, review verdict, structured
  result).  The Review verdict persists as a ``kind='review'`` row owned by
  the review lane (card ``t_dbb0c0dd``); this card owns the *storage*.

Design constraints honored here (design §3/§7/§9, root AGENTS.md §2):

- Every table carries a non-nullable ``tenant_id`` and sets
  ``__tenant_scoped__ = True`` so rows are tenant-owned by schema and are
  picked up by the ``do_orm_execute`` tenant filter in
  ``app/dao/base.py`` automatically.  They are reached ONLY through
  ``TenantScopedBaseDAO`` (decision D6, the Phase 2C ``analysis.py``
  precedent).
- No state machine: ``kind`` / ``seal_status`` / ``outcome`` are CLOSED
  result-code sets (decision D4, the Phase 3 D5 precedent).  The only
  lifecycle a record owns is the one-way DRAFT→SEALED boundary (decision D2);
  rework = a NEW row, history is never overwritten (invariant D2 / §7.2).
- Provenance is mandatory at creation, fail-closed (decision D5): the DB
  CHECKs ``ck_artifact_records_source`` / ``ck_evidence_records_source`` are
  the backstop; the named closed rejection codes (``EV_NO_SOURCE`` /
  ``EV_NO_SUBJECT`` / ...) are raised by the owning DAO before a row is
  written.
- The frozen Phase 2A–3 models (``Task`` / ``AgentToolExecution`` /
  ``AgentRunEvent`` / ``WorkspaceFileRevision`` / ``PublishedPage`` /
  ``repositories``) are NOT modified: this module is additive only
  (design §9, Phase 3 §5 rule).

The migration that provisions these tables is f072
(``alembic/versions/v1_11_5_f072_artifact_evidence.py``), chained off f071;
the model and the migration are kept in lockstep (the f068/f069
index-lockstep lesson) so a fresh-DB (create_all) path and an existing-DB
(migration) path end with the identical schema.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ---------------------------------------------------------------------------
# Closed result-code / enum value sets (decision D4, Phase 3 D5 precedent):
# bounded vocabularies enforced at the DAO/service layer, NOT persisted as PG
# enums (the f069 ``created_reason`` precedent inverted: "String(20), closed"
# + service validation).  A value outside the set fails closed before any
# row is written (mirrors the PLANNING_RUN_STATUSES closed-code pattern).
# ---------------------------------------------------------------------------

#: artifact_records.type — a CLOSED set (design §3.1, decision D1): every
#: Root-brief "artifact" concept is a *value* on this set, not a new entity.
ARTIFACT_TYPES = (
    "file",
    "document",
    "tool_result",
    "published_page",
    "git_snapshot",
    "test_report",
    "structured",
    "db_record",
)

#: artifact_records.storage_scheme — a CLOSED list of *existing* authorities
#: (design §3.2, decision D3): this is the "don't duplicate foundations"
#: guard (audit Q13).  A value outside the set is rejected — the mechanism
#: that enforces "no second blob store / no parallel storage".
STORAGE_SCHEMES = (
    "tool_result",
    "blob_key",
    "workspace_path",
    "published_page",
    "git_acq_tar",
    "external_url",
)

#: artifact_records.seal_status — a CLOSED set (design §3.3, decision D2/D4):
#: DRAFT (mutable window) -> SEALED (immutable, one-way).  The ONLY lifecycle
#: a record owns; who may seal it is defined by the consuming review/completion
#: lane, not by a new state machine.
SEAL_STATUSES = ("DRAFT", "SEALED")

#: evidence_records.kind — a CLOSED set (design §5.1, decision D1): the
#: re-verifiable proof kinds; ``review`` is written by the review lane
#: (card t_dbb0c0dd) and ``test_result`` is the one genuinely new persisted
#: fact in V1 (audit Q3).
EVIDENCE_KINDS = (
    "file_revision",
    "git_revision",
    "test_result",
    "tool_result",
    "published_page",
    "review",
    "structured",
)

#: evidence_records.outcome — the recorded verdict at capture time (design
#: §A2): re-verification later writes a NEW evidence row, it never rewrites
#: this one (decision D2).
EVIDENCE_OUTCOMES = ("pass", "fail", "inconclusive")

#: Closed service/DAO rejection codes for the Artifact ledger (design §7.8-10,
#: fail-closed, closed set — "unknown value fails closed").
AE_CLOSED_CODES = (
    "AE_UNKNOWN_TYPE",
    "AE_UNKNOWN_SCHEME",
    "AE_PROJECT_MISMATCH",
    "AE_ALREADY_SEALED",
    "AE_ALREADY_EXISTS",
)

#: Closed service/DAO rejection codes for the Evidence ledger (design §7.8-13,
#: fail-closed).  ``EV_REVIEW_NOT_INDEPENDENT`` is the storage-level half of
#: the Root "no self-review" guarantee (invariant 13); the assignment-time
#: half is ``PL_REVIEWER_NOT_INDEPENDENT`` (frozen, assignment_service.py).
EV_CLOSED_CODES = (
    "EV_NO_SOURCE",
    "EV_NO_SUBJECT",
    "EV_UNKNOWN_KIND",
    "EV_PAYLOAD_OVERRUN",
    "EV_REVIEW_NOT_INDEPENDENT",
)

#: Bounded evidence payload (design §A2 / §7.11): kind-specific structured
#: facts, complete-or-absent — a half-proof is worse than none (backend
#: AGENTS.md complete-operation-bounds rule).  32 KiB.
MAX_EVIDENCE_PAYLOAD_BYTES = 32 * 1024


class ArtifactRecord(Base):
    """One durable record of a produced thing (design §3.A1).

    **It never stores bytes.**  The ``storage_scheme`` + ``storage_ref`` pair
    points at one of the five existing storage authorities (decision D3);
    ``content_hash`` makes the record content-addressed so a re-verification
    can detect drift or loss.  ``id`` is the stable identity; the model ref
    is ``artifact://{id}`` (design §4.1).  ``seal_status`` owns the one-way
    DRAFT→SEALED boundary (decision D2): rows are mutable while DRAFT and
    immutable after SEALED; a "change" is a NEW row whose
    ``superseded_by`` links back — history is never deleted, only linked.
    """

    __tablename__ = "artifact_records"
    # Tenant ownership (decision D6): non-nullable, indexed, picked up by the
    # do_orm_execute tenant filter automatically.  Explicit flag so the model
    # is tenant-owned even if the column nullability is ever revisited.
    __tenant_scoped__ = True

    __table_args__ = (
        # D5 (invariant 1): the primary provenance edge — exactly one of
        # (execution_id, created_by_user) is non-null.  XOR via boolean <>:
        # an agent-produced row carries execution_id; a human-supplied row
        # carries created_by_user; a row with neither (or both) is rejected.
        CheckConstraint(
            "(execution_id IS NULL) <> (created_by_user IS NULL)",
            name="ck_artifact_records_source",
        ),
        # D2 (invariant 2): the seal boundary is one-way and stamped — a
        # SEALED row must carry its sealed_at; DRAFT rows may not.
        CheckConstraint(
            "(seal_status = 'SEALED') = (sealed_at IS NOT NULL)",
            name="ck_artifact_records_seal",
        ),
        # D1/dedup (invariant 4): the same locator under the same tenant is
        # ONE record (an artifact *is* its locator); a second execution citing
        # the same locator returns the existing row (idempotent-cite, design
        # §10) — provenance of "who cited it" lives on the citing side.
        UniqueConstraint(
            "tenant_id", "storage_scheme", "storage_ref", name="uq_artifact_records_tenant_ref"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Tenant ownership (decision D6).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Project scope; NULL allowed for tool-level artifacts (matrix P1: the
    # authoritative project link is execution -> run -> task -> project; this
    # column is a convenience, never a second authority).  SET NULL: deleting
    # a project keeps its artifact history.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # "Produced by which Task"; NULL when there is no Task context.  SET NULL:
    # the frozen tasks table is untouched — this FK only references it.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # **The primary provenance edge** (decision D5): which tool execution
    # produced it; NULL only for the created_by_user path.  SET NULL so
    # deleting an execution never destroys an artifact record.
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # "Who" when the producer is an agent (mirrors PublishedPage.agent_id,
    # analysis.py AnalysisRun.agent_id).  SET NULL: deleting an agent keeps
    # the artifact.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # "Who" for human-supplied artifacts (decision D5, ck_artifact_records_source).
    created_by_user: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # What kind of thing (closed set ARTIFACT_TYPES, §3.1) — validated at the
    # DAO/service layer; the column is a plain String.
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Human-readable name (mirror of PublishedPage.title, WorkspaceFileRevision.path).
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # Where the content physically lives — one of the existing authorities
    # (closed set STORAGE_SCHEMES, §3.2); storage_ref is the locator
    # (opaque tool-result:// ref, blob key, published short id, file path,
    # tar key, or external URL).  Non-empty on both (storage is mandatory).
    storage_scheme: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    # SHA-256 when the producer computed it (mirror of WorkspaceFileRevision
    # .content_hash, PlanningRun.plan_sha256); NULL only for live URLs whose
    # hash is unknowable at creation.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "Which revision": resolved_rev / commit sha (git), a WorkspaceFileRevision
    # content hash (file), or a DB snapshot tag.  The single column answers the
    # Root "对应哪个 revision" question.
    revision_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # The one-way DRAFT -> SEALED boundary (decision D2 / §3.3); server_default
    # so the create_all-provisioned fresh-DB path and the f072 migration path
    # agree on the DDL (f069 index-lockstep lesson).
    seal_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    # Set on DRAFT->SEALED; ck_artifact_records_seal couples it to seal_status.
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The *only* row-to-row link a rework ever adds (decision D2): "this
    # artifact was replaced by that one" (points at the NEW row).  Self-FK,
    # SET NULL so a row always survives its own link target's deletion.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_records.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # updated_at changes only while DRAFT (the mutable window); the SEALED
    # boundary makes the row immutable regardless.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EvidenceRecord(Base):
    """One durable, re-verifiable assertion (design §3.A2).

    Evidence never *is* content; it points at a content authority
    (``subject_ref`` + ``subject_hash`` + ``revision_ref``) and records an
    **outcome** + the binding that makes it checkable later.  A Review verdict
    is a ``kind='review'`` row (design §5 R1) written by the disjoint
    reviewer (invariant 13, ``EV_REVIEW_NOT_INDEPENDENT``).  It is born
    immutable: a single ``created_at`` timestamp, no ``updated_at``
    (decision D2).
    """

    __tablename__ = "evidence_records"
    __tenant_scoped__ = True

    __table_args__ = (
        # D5 (invariant 3): at least one source AND a non-empty subject —
        # "no Evidence without provenance" is a DB backstop; the owning DAO
        # raises the named codes (EV_NO_SOURCE / EV_NO_SUBJECT) first.
        # char_length > 0 catches the empty-string subject the NOT NULL
        # column type would otherwise allow.
        CheckConstraint(
            "(artifact_id IS NOT NULL OR execution_id IS NOT NULL "
            "OR created_by_agent IS NOT NULL OR created_by_user IS NOT NULL) "
            "AND char_length(subject_ref) > 0",
            name="ck_evidence_records_source",
        ),
        # outcome closed set (EVIDENCE_OUTCOMES, §A2) as a DB CHECK backstop.
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'inconclusive')",
            name="ck_evidence_records_outcome",
        ),
        # Dedup (invariant 5): one evidence row per (tenant, kind, subject,
        # revision) — a re-verify row (payload carrying reverify_of) is
        # EXCLUDED from this constraint via a partial unique index (the
        # AgentRunEvent partial-unique precedent), so re-verification of the
        # same subject appends freely while the *original* capture is
        # deduplicated.
        Index(
            "uq_evidence_records_reverify",
            "tenant_id",
            "kind",
            "subject_ref",
            "revision_ref",
            unique=True,
            postgresql_where=text("NOT COALESCE(payload ? 'reverify_of', FALSE)"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Tenant ownership (decision D6).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Same scoping logic as ArtifactRecord (matrix P1: convenience, not
    # authority).  SET NULL: the frozen project/task tables are untouched.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which artifact this evidence speaks about; NULL for execution-level
    # evidence (e.g. a test result with no named artifact).  SET NULL.
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_records.id", ondelete="SET NULL"), nullable=True
    )
    # The source execution ("which run/test/command produced this proof"); the
    # provenance edge for test_result (invariant 12).  SET NULL.
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # What kind of proof (closed set EVIDENCE_KINDS, §5.1) — validated at the
    # DAO/service layer; the column is a plain String.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # The recorded verdict at capture time (closed set EVIDENCE_OUTCOMES, §A2);
    # re-verification writes a NEW row, it never rewrites this one (D2).
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    # The locator the verifier re-checks: file path (+ revision_ref hash), git
    # sha, tool-result:// ref, published short id, command string.  Non-empty
    # (char_length > 0) — ck_evidence_records_source.
    subject_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    # Content hash of the subject at capture time (drift detection on
    # re-verify; mirror of content_hash on A1).
    subject_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "Which revision/commit" (Root brief question); e.g. resolved_rev for
    # git evidence.
    revision_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Kind-specific structured facts (test counts, verdict text, structured
    # result) — bounded to 32 KiB at the service layer (design §A2 / §7.11).
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Who captured it (agent reviewer, builder, or system gate) — the
    # created_by_agent = the disjoint reviewer for kind='review' (invariant 13).
    created_by_agent: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # Human-captured evidence (decision D5 provenance).
    created_by_user: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # The *only* timestamp — evidence has no updated_at; it is born immutable
    # (decision D2).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


__all__ = [
    "AE_CLOSED_CODES",
    "ARTIFACT_TYPES",
    "EVIDENCE_KINDS",
    "EVIDENCE_OUTCOMES",
    "EV_CLOSED_CODES",
    "MAX_EVIDENCE_PAYLOAD_BYTES",
    "SEAL_STATUSES",
    "STORAGE_SCHEMES",
    "ArtifactRecord",
    "EvidenceRecord",
]

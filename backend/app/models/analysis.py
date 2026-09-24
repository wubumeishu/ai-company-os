"""Analysis persistence models (Phase 2C minimal model).

Per docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2, three tenant-scoped tables let
the OS "reliably store one Project Analysis + its sources".  They follow the
Git-Acquisition closed-code / storage-facade / revision-binding pattern
(reuse-first: ONE minimal typed model, NOT a full Artifact/Evidence platform).

Entities
--------
- ``AnalysisRun`` (``analysis_runs``) — versioning + Git-revision binding.
  Append-only: one row per analysis execution, bound to exactly one commit
  via ``revision_sha`` under ``UNIQUE(project_id, revision_sha)``.  A
  re-analysis at a NEW commit sha is a NEW row, so history is never
  clobbered.  "current" = the latest run; "history" = all prior runs.
- ``AnalysisFinding`` (``analysis_findings``) — TRANSIENT findings owned by
  a run (true of one revision, at one time, by one agent).  They die with
  their run via ``ON DELETE CASCADE`` on ``analysis_run_id``.
- ``ProjectKnowledge`` (``project_knowledge``) — DURABLE, confirmed,
  revision-independent knowledge.  A finding is PROMOTED here only after
  human/company confirmation (the PENDING_CONFIRMATION step); the promotion
  copies provenance (``source_analysis_run_id``) and is NOT invalidated by a
  later analysis of a different commit.

Design constraints honored here (the Phase 2C minimal-model card, §9.2):
- Every table carries a non-nullable ``tenant_id`` so rows are tenant-owned
  by schema and are automatically picked up by the ``do_orm_execute``
  tenant filter in ``app/dao/base.py`` (``_is_tenant_scoped_model``) with no
  opt-in flag.  They are reached ONLY through ``TenantScopedBaseDAO`` +
  ``verify_tenant_scope`` (the M9 fourth gate).
- No new step-by-step workflow state machine: ``AnalysisRun.status`` is a
  CLOSED result-code enum (mirroring the ACQ_* closed-code pattern), not a
  workflow SM (root AGENTS.md §2 — a new SM needs an independent owner +
  need; it does not).
- No knowledge graph: a single flat knowledge row, provenance carried by a
  nullable ``source_analysis_run_id`` FK.
- ``Project`` / ``Repository`` rows are NOT modified and the existing
  ``project_status_enum`` is unchanged beyond the decision that Phase 2C
  owns the ANALYZING transition (the enum already declares it).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ---------------------------------------------------------------------------
# Closed result-code / enum value sets.  Persisted as PG enum types by the
# f068 migration; the service layer re-validates against these closed sets so
# a bad value fails closed before any state is written (mirrors the ACQ_*
# closed-code + INTAKE_TRANSITIONS closed-set pattern).
# ---------------------------------------------------------------------------

#: analysis_runs.status — a CLOSED result-code set, NOT a workflow SM.
#: AN_OPEN: the run is open and findings may still be recorded against it.
#: AN_COMPLETED / AN_FAILED: terminal outcomes (findings are locked).
ANALYSIS_RUN_STATUSES = ("AN_OPEN", "AN_COMPLETED", "AN_FAILED")

#: analysis_findings.severity — closed severity ladder (design §9.2).
ANALYSIS_SEVERITY_VALUES = ("INFO", "WARN", "HIGH", "CRITICAL")

#: analysis_findings.category — closed subject taxonomy (design §9.2).
ANALYSIS_CATEGORY_VALUES = ("SECURITY", "RISK", "TECH_DEBT", "OPEN_QUESTION", "FACT")

#: analysis_findings.tag — the provenance tag every finding carries
#: (FACT / OBSERVATION / INFERENCE / UNKNOWN; README-as-truth is forbidden,
#: so every finding must carry a traceable tag + evidence).
ANALYSIS_FACING_TAGS = ("FACT", "OBSERVATION", "INFERENCE", "UNKNOWN")

#: project_knowledge.status — the durable lifecycle (PROPOSED -> CONFIRMED
#: -> SUPERSEDED).  The minimal promote path inserts a CONFIRMED row on
#: confirmation; PROPOSED / SUPERSEDED stay available for the (future)
#: confirmation UI + supersession.
KNOWLEDGE_STATUSES = ("PROPOSED", "CONFIRMED", "SUPERSEDED")


class AnalysisRun(Base):
    """One analysis execution, bound to exactly one Git revision.

    ``revision_sha`` is the commit hash the analysis was produced against —
    the typed, indexed revision carrier that decouples the analysis from the
    free-form ``repositories.locator`` JSON (OQ-5).  Its source is
    ``repositories.locator.resolved_rev`` captured at analysis time.  The
    append-only ``UNIQUE(project_id, revision_sha)`` invariant means a later
    analysis of a new commit is a new row, never a clobber.
    """

    __tablename__ = "analysis_runs"
    # The versioning invariant (design §9.2): one analysis run per
    # (project, revision).  A re-analysis at a NEW commit is a NEW row;
    # the SAME revision is never analyzed twice (append-only history,
    # never clobbered).  The constraint is the concurrency guard: two
    # racing launches at the same revision — one wins, the other re-reads.
    __table_args__ = (
        UniqueConstraint("project_id", "revision_sha", name="uq_analysis_runs_project_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable + SET NULL so deleting the running agent never clobbers
    # analysis history (the run is owned by the project, not the agent).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # The typed revision binding: commit hash, indexed for revision-keyed
    # queries ("which findings belong to rev X?").
    revision_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The ref the analysis targeted (None = the repo's own default branch).
    requested_ref: Mapped[str | None] = mapped_column(String(200))
    # When the source revision was resolved / the analysis produced.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Enum(*ANALYSIS_RUN_STATUSES, name="analysis_run_status_enum", create_constraint=False),
        default="AN_OPEN",
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tenant ownership (M9): non-nullable, indexed, picked up by the
    # do_orm_execute tenant filter automatically.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnalysisFinding(Base):
    """A transient finding, owned by one analysis run.

    Transient = true of one revision, at one time, by one agent.  It dies
    with its run (``ON DELETE CASCADE``).  Every finding carries a
    provenance ``tag`` + an ``evidence`` JSON (``path:line`` anchors +
    source-card provenance) so it is traceable; README-as-truth is
    forbidden.
    """

    __tablename__ = "analysis_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(
        Enum(*ANALYSIS_SEVERITY_VALUES, name="analysis_severity_enum", create_constraint=False),
        default="INFO",
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        Enum(*ANALYSIS_CATEGORY_VALUES, name="analysis_category_enum", create_constraint=False),
        nullable=False,
    )
    tag: Mapped[str] = mapped_column(
        Enum(*ANALYSIS_FACING_TAGS, name="analysis_tag_enum", create_constraint=False),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # path:line anchors + source-card provenance (a bounded, secret-free dict).
    evidence: Mapped[dict | None] = mapped_column(JSON)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProjectKnowledge(Base):
    """Durable, confirmed, revision-independent project knowledge.

    A finding is promoted here only after human/company confirmation; the
    promotion copies provenance (``source_analysis_run_id``) but the row is
    NOT invalidated by a later analysis at a different commit.  A knowledge
    graph is deliberately NOT modeled — this is a single flat row.
    """

    __tablename__ = "project_knowledge"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # e.g. "backend framework"
    subject: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # e.g. "backend uses FastAPI"
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance: the analysis run this knowledge was promoted from.  Nullable
    # (hand-entered knowledge has no run) and SET NULL so a superseded /
    # deleted run never destroys the durable knowledge row.
    source_analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Enum(*KNOWLEDGE_STATUSES, name="knowledge_status_enum", create_constraint=False),
        default="PROPOSED",
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

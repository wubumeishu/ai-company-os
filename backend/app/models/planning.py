"""Planning persistence models (Phase 3 Planning Domain, V1 minimal model).

Per docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md §3, five
tenant-scoped tables make the Planning Domain a **durable data layer**
(decision D1):

- ``PlanningRun`` (``planning_runs``) — the durable plan revision, bound to
  one Project + one analysis revision.  Append-only, mirroring
  ``AnalysisRun``: ``UNIQUE(project_id, analysis_revision_sha)`` is the
  concurrency guard and dedup key; a re-plan at a new revision is a NEW row.
- ``PlanningGoal`` (``planning_goals``) — a concrete operational objective of
  a run (derived, refined — NOT a second authority for ``Project.goal``).
- ``WorkPackage`` (``work_packages``) — the structural grouping the flat Task
  Graph cannot express: task_scope materialization intent, execution-mode
  hint, shared-resource declaration, review-independence flag.
- ``Milestone`` (``milestones``) — an optional ordering/phase bucket for work
  packages within one run; a "phase" is a closed milestone ``kind``, not a
  separate table.
- ``WorkPackageTask`` (``work_package_tasks``) — the ONLY new link table
  connecting Planning to Task: a pure link + intent record that adds an
  explicit FK link *to* the frozen ``Task`` model without touching it.

Design constraints honored here (design §3/§5/§6, root AGENTS.md §2):
- Every table carries a non-nullable ``tenant_id`` so rows are tenant-owned
  by schema and are picked up by the ``do_orm_execute`` tenant filter in
  ``app/dao/base.py`` automatically.  They are reached ONLY through
  ``TenantScopedBaseDAO`` (decision D6, the Phase 2C ``analysis.py``
  precedent).
- No new state machine: ``planning_runs.status`` and
  ``planning_goals.status`` are CLOSED result-code enums (decision D5,
  mirroring ``ANALYSIS_RUN_STATUSES`` in ``app/models/analysis.py``) — not
  workflow SMs; the planner Run's lifecycle stays in ``AgentRun`` checkpoints.
- No second edge/readiness/assignment fact (design D2/§7): the Task Graph,
  ``Task.agent_id``, and the Runtime spine are REUSED, not duplicated.  The
  only new link to Task is ``work_package_tasks``; no ``TaskAssignment`` /
  ``Squad`` / org entity exists in V1 (design D4 + S1).
- The ``Task`` / ``AnalysisRun`` / graph models are NOT modified: the
  ``work_package_tasks`` FK is the explicit link permitted by the card.

The frozen Phase 2A–2F code paths are untouched; this module is additive
only.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ---------------------------------------------------------------------------
# Closed result-code / enum value sets.  The two status columns are persisted
# as PG enum types by the f071 migration (mirroring the f068
# analysis_run_status_enum pattern); the service layer re-validates against
# these closed sets so a bad value fails closed before any row is written
# (mirrors the ANALYSIS_RUN_STATUSES closed-code pattern, app/models/analysis.py).
# ---------------------------------------------------------------------------

#: planning_runs.status — a CLOSED result-code set, NOT a workflow SM (D5).
#: PL_OPEN: the plan may still be recorded.  PL_COMPLETED / PL_FAILED:
#: terminal outcomes (the plan payload is locked; goals/WPs may not be
#: appended — invariant §6.7).
PLANNING_RUN_STATUSES = ("PL_OPEN", "PL_COMPLETED", "PL_FAILED")

#: planning_goals.status — a CLOSED result-code set (D5).  PL_PROPOSED:
#: emitted by the planner, not yet approved.  PL_APPROVED:
#: human/company confirmation (reusing the 2C confirmation precedent) — the
#: ONLY status from which a goal may be materialized.  PL_MATERIALIZED:
#: terminal; all of the goal's work packages have materialized into Tasks.
#: A later re-plan creates a NEW run's goals (append-only), never clobbering
#: an already-materialized one.
PLANNING_GOAL_STATUSES = ("PL_PROPOSED", "PL_APPROVED", "PL_MATERIALIZED")

#: milestones.kind — a CLOSED set (design P9): phase / gate / delivery.
#: Phase is a kind of Milestone, not a separate entity.  ``gate`` is where
#: review-independence / quality gates attach (a gate blocks later WPs'
#: materialization until its tasks are done — via the frozen
#: task_graph_service.ensure_ready, design §6 invariant 10).  Persisted as a
#: plain String column; the closed set is enforced at the DAO/service layer
# (the f069 created_reason precedent inverted: bounded vocabulary + service
# validation, not a DB enum — matches the design's "String(20), closed" spec).
MILESTONE_KINDS = ("phase", "gate", "delivery")

#: work_packages.execution_mode — a CLOSED set (design P12).  This is a
#: planner PROPOSAL recorded on the WP; the authoritative task order is
#: always the task_dependencies DAG (design §6 invariant 11: "on conflict,
#: the DAG wins").  Persisted as a plain String column, validated against
#: this closed set at the DAO/service layer.
WORK_PACKAGE_EXECUTION_MODES = ("serial", "parallel", "parallel_then_serial")

#: task_scope slot ``kind`` — a CLOSED set (squad design §4.1): each
#: materialized slot declares what it is.  ``review`` slots are reviewer
#: tasks (must be a disjoint Agent from the builders — REV-1); ``gate``
#: slots map to a ``gate`` Milestone that blocks later WPs.  Bounded plan
#: data riding inside the ``task_scope`` JSON (squad design §9: an extension
#: of the blob, not a new table).
TASK_SCOPE_SLOT_KINDS = ("build", "review", "gate", "other")

#: WORK_CAPABILITIES — a CLOSED capability vocabulary (squad design §3/S2):
#: a "role" in V1 is a named bundle of capability codes, NOT a stored
#: entity.  ``PlanningGoal.required_capabilities`` is a bounded JSON list
#: drawn from this tuple — an INPUT to the assignment step, never the
#: assignment fact (the fact stays ``Task.agent_id``, design D2).  A
#: deterministic capability→Agent join (a structured ``Agent.capabilities``
#: column) is deliberately DEFERRED (squad design §11 D-1).
WORK_CAPABILITIES = (
    "code",
    "frontend",
    "backend",
    "db-migration",
    "testing",
    "security",
    "review",
    "docs",
    "ops",
)


class PlanningRun(Base):
    """One planning execution, bound to one Project and one analysis revision.

    The durable revision (design P1): append-only, mirroring ``AnalysisRun``.
    ``UNIQUE(project_id, analysis_revision_sha)`` (invariant §6.1) makes a
    re-plan at a new revision a new row — history is never clobbered — and
    the same revision is planned at most once (a racing launch loses the
    insert race and re-reads).  ``plan_sha256`` is the content hash of the
    emitted payload: the materialization idempotency key (design §4), NULL
    while the run is open.  The planner *Run's* lifecycle (running/done/
    failed execution state) stays in ``AgentRun`` checkpoints (D5); the
    traceability link is ``source_agent_run_id``.
    """

    __tablename__ = "planning_runs"
    # The versioning invariant (§6.1): one planning run per
    # (project, analysis revision).  The constraint is the concurrency
    # guard: two racing launches at the same revision — one wins, the
    # other re-reads (mirrors uq_analysis_runs_project_revision).
    __table_args__ = (
        UniqueConstraint(
            "project_id", "analysis_revision_sha", name="uq_planning_runs_project_revision"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # A plan belongs to one project; CASCADE mirrors the f068 analysis_runs
    # FK: deleting the project removes its planning history.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # The revision the plan was produced against (String(64), indexed for
    # revision-keyed reads — mirrors analysis_runs.revision_sha).  A plan is
    # only valid against a known revision; re-analysis implies a new plan.
    analysis_revision_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Content hash of the emitted (goals + packages + milestones) payload —
    # the materialization idempotency key (design §4); NULL while the run
    # is open, set when the plan is recorded and locked.
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        Enum(*PLANNING_RUN_STATUSES, name="planning_run_status_enum", create_constraint=False),
        default="PL_OPEN",
        nullable=False,
    )
    # Who produced it (audit analog of AnalysisRun.agent_id) — nullable +
    # SET NULL so deleting the planner agent never destroys planning
    # history (the run is owned by the project, not the agent).
    planner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # Ties this durable row to the planner Run (D3): the LLM step executes
    # on the shared Checkpointer; this is the traceability carrier.  SET
    # NULL — the durable row outlives the run row.
    source_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    # The full emitted plan (bounded JSON) — the authoritative body the
    # translator reads.  Deliberately distinct from the transient LLM plan
    # artifact in the AgentRun checkpoint (design §5 row 6): this is the
    # durable, project-scoped, materializable record.
    plan_payload: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tenant ownership (D6): non-nullable, indexed, picked up by the
    # do_orm_execute tenant filter automatically.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlanningGoal(Base):
    """One concrete operational objective of a planning run (design P2).

    This is the "derived, refined goal" of the boundary matrix: NOT a second
    authority for business intent (that stays on ``Project.goal``).  It is
    traceable to the analysis context that justified it
    (``analysis_finding_ids``) and declares the capability input
    (``required_capabilities``, a bounded list drawn from
    ``WORK_CAPABILITIES``) that feeds the assignment step — an input, never
    the assignment fact (design D2 / squad design S2).  Goals die with their
    run revision (``ON DELETE CASCADE`` on planning_run_id).
    """

    __tablename__ = "planning_goals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Goals die with their run revision (append-only revisions, §5): a later
    # re-plan creates a NEW run's goals, never clobbering an
    # already-materialized one.
    planning_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("planning_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Assignment input (P5): e.g. ["backend", "db-migration"] — a bounded
    # list drawn from WORK_CAPABILITIES (squad design S2), not the
    # assignment fact.
    required_capabilities: Mapped[list | None] = mapped_column(JSON)
    # Which findings / Knowledge motivated this goal (bounded list) —
    # traceable like a finding's ``evidence``.
    analysis_finding_ids: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        Enum(*PLANNING_GOAL_STATUSES, name="planning_goal_status_enum", create_constraint=False),
        default="PL_PROPOSED",
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkPackage(Base):
    """The structural grouping the flat Task Graph cannot express (design P3).

    A WorkPackage = "the set of tasks needed to reach one PlanningGoal,
    with a suggested execution shape and resource footprint" (design §1).
    It owns the boundary-matrix rows for grouping, ordering, shared
    resources, and review independence (P7/P10/P11/P12).  ``task_scope`` is
    the materialization intent — the translator's input (design §4) — whose
    slot shape is pinned by the squad design §4.1 (slot / kind /
    required_capabilities / candidate_agent_ids / depends_on_slots /
    per-slot shared_resources).  ``execution_mode`` is a planner hint; the
    authoritative order is always the frozen task_dependencies DAG.
    """

    __tablename__ = "work_packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    planning_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("planning_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Every WP pursues exactly one goal (design P3).
    planning_goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("planning_goals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Optional phase/milestone bucket (P9); nullable so a plan can have no
    # milestones at all.  SET NULL: deleting a milestone keeps the WP —
    # the bucket is an ordering hint, not the WP's owner.
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # The materialization intent: task titles/descriptions + dependency pairs
    # this WP should become (design §4 translator input).  Slot shape per
    # the squad design §4.1 contract.
    task_scope: Mapped[list | None] = mapped_column(JSON)
    # Proposed execution shape (P12, closed set WORK_PACKAGE_EXECUTION_MODES):
    # a hint, NOT the enforcement — on conflict the DAG wins (§6 invariant 11).
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # Shared-resource DECLARATION for conflict detection (P10):
    # e.g. {"files": [...], "db": [...], "api": [...], "workspace": [...]}.
    # Planning *detects/conflicts*; enforcement stays in the existing Redis
    # workspace locks + advisory lane — no new lock layer (§6 invariant 12).
    shared_resources: Mapped[dict | None] = mapped_column(JSON)
    # which WP's outputs need a non-builder reviewer (P11); enforcement of
    # reviewer-≠executor happens at assignment time (squad REV-1), the
    # runtime gate is a known inherited fail-open gap (REV-3, deferred D-3).
    # server_default so the create_all-provisioned fresh-DB path and the
    # f071 migration path agree on the DDL (f069 index-lockstep lesson).
    requires_independent_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Per-WP parallelism hint (P16); feeds the (deferred) project-level cap
    # — advisory only, not a scheduler.
    max_parallel_tasks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Milestone(Base):
    """An optional ordering/phase bucket for work packages within one run.

    Phase is a closed ``kind`` of Milestone, not a separate table (design P9)
    — this keeps the model minimal: no ``Phase`` entity, no nested structure.
    ``UNIQUE(planning_run_id, seq)`` (§6 invariant 4) means one bucket per
    position; ``seq`` 0 = unordered.
    """

    __tablename__ = "milestones"
    # One bucket per position within the run (invariant §6.4).
    __table_args__ = (
        UniqueConstraint("planning_run_id", "seq", name="uq_milestones_run_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    planning_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("planning_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Closed set MILESTONE_KINDS (P9): phase / gate / delivery.  Bounded
    # vocabulary, validated against the closed tuple at the DAO/service layer
    # (design §3 "String(20), closed" spec — no DB enum, mirroring the
    # f069 created_reason handling at the service).
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkPackageTask(Base):
    """The ONLY new link table connecting Planning to Task (design P5).

    A *pure link + intent record*: it adds the missing planning-side link
    (WP → task) that no existing Task column carries, WITHOUT touching the
    frozen ``Task`` model (the card's hard boundary).  ``task_id`` is NULL
    until materialized (design §4 — the translator fills it, sets
    ``materialized_at``, and the CHECK invariant holds); ``ON DELETE SET
    NULL`` so deleting a Task preserves the intent.  The end-to-end
    traceability carrier: ``Task.created_reason = ANALYSIS_PLANNING``
    (existing column) + this row gives "this task came from plan revision
    R, WP W".  No second assignment fact: ``Task.agent_id`` remains the
    single source of truth (design D2 / §7 rule 4).

    Documented interaction of the two §6 invariants (fail-closed, not a
    second authority):

    - deleting a Task that an OPEN slot references is impossible (open
      slots carry ``task_id NULL`` — nothing references the Task yet);
    - deleting a Task that a MATERIALIZED slot references is REJECTED by
      the database: the FK ``SET NULL`` would null ``task_id`` while
      ``materialized_at`` stays set, violating
      ``ck_wp_tasks_materialized`` (§6.3).  The intent-preserving path is
      to re-open the slot first (``task_id = NULL``,
      ``materialized_at = NULL`` — the owning planning service lane,
      design §4) and then delete the Task: the link row survives with
      ``task_id`` NULL, exactly as the P5 spec requires ("on task
      deletion the link survives the intent").  The rejection is the
      fail-closed boundary of that lane, so no materialization record can
      be silently orphaned.
    """

    __tablename__ = "work_package_tasks"
    # Invariants §6.2/§6.3: one task per (work_package, task) slot; a
    # materialized_at timestamp requires a task_id.
    __table_args__ = (
        UniqueConstraint("work_package_id", "task_id", name="uq_wp_tasks"),
        CheckConstraint("materialized_at IS NULL OR task_id IS NOT NULL", name="ck_wp_tasks_materialized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # CASCADE: the slots die with their work package (append-only revisions).
    work_package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL + nullable: NULL until materialized (design §4); on task
    # deletion the link survives the intent.  The frozen tasks table is
    # untouched — this FK is the explicit link the card permits.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # When the Task row was created for this WP slot (NULL while open).
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

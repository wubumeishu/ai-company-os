"""Tenant-scoped DAOs for the Phase 3 Planning persistence domain.

Per docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md §3/§10 the
five Planning tables (``planning_runs`` / ``planning_goals`` /
``work_packages`` / ``milestones`` / ``work_package_tasks``) are reached
ONLY through ``TenantScopedBaseDAO`` (decision D6, the Phase 2C
``analysis_dao.py`` precedent): the active tenant (bound by
``TenantContextMiddleware`` on every request, or ``tenant_context()`` in
background code) filters reads automatically, and ``add_scoped`` forces
tenant alignment on writes.

Layering rules honored (backend/app/dao/AGENTS.md):
- No business logic in this module — only DB reads, writes, scoped queries,
  and closed-set validation within the Planning -> Task-link boundary.
  Materialization (creating Task rows), assignment, and conflict detection
  (CONF-1..5 / REV-1..3, squad design §6/§7) belong to the owning
  service/assignment cards, NOT here.
- Writes flush; the owning service/transaction commits.
- Status columns are CLOSED result-code sets (decision D5, no workflow SM):
  DAO writes re-validate against the closed tuples in
  ``app/models/planning.py`` so a bad value fails closed before any row is
  written (mirrors the ANALYSIS_RUN_STATUSES pattern in analysis_dao).
- ``work_package_tasks`` is the ONLY planning-side link to the frozen
  ``Task`` model (design P5): the DAO inserts link rows; it never mutates
  ``Task`` rows, ``task_dependencies``, or the Runtime spine.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.dao.base import TenantScopedBaseDAO
from app.models.agent import Agent
from app.models.planning import (
    MILESTONE_KINDS,
    PLANNING_GOAL_STATUSES,
    PLANNING_RUN_STATUSES,
    TASK_SCOPE_SLOT_KINDS,
    WORK_CAPABILITIES,
    WORK_PACKAGE_EXECUTION_MODES,
    Milestone,
    PlanningGoal,
    PlanningRun,
    WorkPackage,
    WorkPackageTask,
)
from app.models.task import Task


class ClosedCodeError(ValueError):
    """A closed-set validation failure (fail-closed, design §6 invariant 7)."""


def _validate_closed(value: Any, closed_set: tuple[str, ...], field_name: str) -> None:
    """Fail closed when ``value`` is not in the closed set (design D5/§6.7)."""
    if value not in closed_set:
        raise ClosedCodeError(f"{field_name}={value!r} is not in the closed set {closed_set}")


class PlanningRunDAO(TenantScopedBaseDAO[PlanningRun]):
    """Tenant-scoped DAO for planning_runs rows (the durable revision).

    Reads are scoped to the active tenant via the ContextVar; for
    platform-admin cross-tenant queries use the parent ``BaseDAO`` methods
    and annotate the call site with ``# arch-guard: allow (platform_admin cross-tenant)``.
    """

    def __init__(self) -> None:
        super().__init__(PlanningRun)

    async def get_plan_for_project(
        self,
        project_id: uuid.UUID,
        analysis_revision_sha: str,
        db=None,
    ) -> PlanningRun | None:
        """Fetch the (unique) plan revision bound to one (project, analysis revision).

        Design §10 mapping for ``get_plan_for_project``: the
        ``UNIQUE(project_id, analysis_revision_sha)`` invariant (§6.1) makes
        this a single-row read; it is the concurrency guard's re-read path
        when a racing planner launch loses the insert race (mirrors
        ``AnalysisRunDAO.get_for_project_and_revision``).
        """
        tenant_id = self._require_tenant_id()
        if tenant_id is None:
            if db is None:
                async with self.session(readonly=True) as session_db:
                    stmt = select(PlanningRun).where(
                        PlanningRun.project_id == project_id,
                        PlanningRun.analysis_revision_sha == analysis_revision_sha,
                    )
                    return (await session_db.execute(stmt)).scalar_one_or_none()
            stmt = select(PlanningRun).where(
                PlanningRun.project_id == project_id,
                PlanningRun.analysis_revision_sha == analysis_revision_sha,
            )
            return (await db.execute(stmt)).scalar_one_or_none()
        async with self.session(db=db, readonly=True) as session_db:
            stmt = select(PlanningRun).where(
                PlanningRun.project_id == project_id,
                PlanningRun.analysis_revision_sha == analysis_revision_sha,
                PlanningRun.tenant_id == tenant_id,
            )
            return (await session_db.execute(stmt)).scalar_one_or_none()

    async def list_plans_for_project(
        self,
        project_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[PlanningRun]:
        """All plan revisions for a project, newest first (single query).

        The table is append-only (design §1): "current" = the latest
        terminal row; "history" = all prior revisions, never clobbered.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(PlanningRun)
            .where(PlanningRun.project_id == project_id)
            .order_by(PlanningRun.started_at.desc(), PlanningRun.created_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(PlanningRun.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def open_run(
        self,
        run: PlanningRun,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> PlanningRun:
        """Register one open plan revision in the caller's tenant scope + transaction.

        The flush rides the caller's session (committed by the owning
        service's transaction).  The flush may raise ``IntegrityError`` on
        the ``UNIQUE(project_id, analysis_revision_sha)`` invariant (a
        racing launch); the caller owns the re-read recovery (same contract
        as ``AnalysisRunDAO.open_run``).
        """
        _validate_closed(run.status or "PL_OPEN", PLANNING_RUN_STATUSES, "status")
        self.add_scoped(db, run, tenant_id=tenant_id)
        await db.flush()
        # Re-load the server-generated timestamps inside the session so the
        # caller's view never triggers a greenlet refresh (f068 contract).
        await db.refresh(run, attribute_names=["created_at", "updated_at", "started_at"])
        return run

    async def complete_run(
        self,
        run: PlanningRun,
        *,
        new_status: str,
        plan_sha256: str | None = None,
        db=None,
    ) -> PlanningRun:
        """Stamp a terminal status + the finished_at window, locking the payload.

        ``new_status`` must be a terminal member of the closed
        ``PLANNING_RUN_STATUSES`` set (invariant §6.7: PL_COMPLETED /
        PL_FAILED lock the payload; goals/WPs may not be appended after).
        """
        _validate_closed(new_status, ("PL_COMPLETED", "PL_FAILED"), "new_status")
        async with self.session(db=db) as session_db:
            run.status = new_status
            if plan_sha256 is not None:
                run.plan_sha256 = plan_sha256
            run.finished_at = datetime.now(UTC)
            session_db.add(run)
            await session_db.flush()
            await session_db.refresh(run, attribute_names=["updated_at"])
            return run


class PlanningGoalDAO(TenantScopedBaseDAO[PlanningGoal]):
    """Tenant-scoped DAO for planning_goals rows (derived operational objectives).

    Goals die with their run revision (``ON DELETE CASCADE``); the status
    closed set (§6.7-8) is validated on every write so a materialization
    attempt against a bad status fails closed before any row changes.
    """

    def __init__(self) -> None:
        super().__init__(PlanningGoal)

    def _validate_goal(self, goal: PlanningGoal) -> None:
        _validate_closed(goal.status or "PL_PROPOSED", PLANNING_GOAL_STATUSES, "status")
        caps = goal.required_capabilities
        if caps is not None:
            if not isinstance(caps, list):
                raise ClosedCodeError(
                    "required_capabilities must be a bounded JSON list (squad design S2)"
                )
            unknown = [c for c in caps if c not in WORK_CAPABILITIES]
            if unknown:
                raise ClosedCodeError(
                    f"required_capabilities {unknown!r} outside the closed WORK_CAPABILITIES set"
                )
        finding_ids = goal.analysis_finding_ids
        if finding_ids is not None and not isinstance(finding_ids, list):
            raise ClosedCodeError("analysis_finding_ids must be a bounded JSON list")

    async def add_goal(
        self,
        goal: PlanningGoal,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> PlanningGoal:
        """Register one goal for an open run in the caller's transaction."""
        self._validate_goal(goal)
        self.add_scoped(db, goal, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(goal, attribute_names=["created_at", "updated_at"])
        return goal

    async def list_goals_for_run(
        self,
        planning_run_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[PlanningGoal]:
        """All goals owned by one run, oldest first (single query)."""
        tenant_id = self._require_tenant_id()
        stmt = (
            select(PlanningGoal)
            .where(PlanningGoal.planning_run_id == planning_run_id)
            .order_by(PlanningGoal.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(PlanningGoal.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def set_status(
        self,
        goal: PlanningGoal,
        *,
        new_status: str,
        db=None,
    ) -> PlanningGoal:
        """Move a goal along the closed PL_PROPOSED -> PL_APPROVED ->
        PL_MATERIALIZED result-code ladder (D5: codes, not a workflow SM).

        The owning service enforces the step order (a goal materializes only
        from PL_APPROVED, invariant §6.8); this method validates membership
        in the closed set and persists the decided value.
        """
        _validate_closed(new_status, PLANNING_GOAL_STATUSES, "new_status")
        async with self.session(db=db) as session_db:
            goal.status = new_status
            session_db.add(goal)
            await session_db.flush()
            await session_db.refresh(goal, attribute_names=["updated_at"])
            return goal


class WorkPackageDAO(TenantScopedBaseDAO[WorkPackage]):
    """Tenant-scoped DAO for work_packages rows (the structural grouping).

    ``task_scope`` entries follow the squad design §4.1 slot contract
    (slot / kind / required_capabilities / candidate_agent_ids /
    depends_on_slots / per-slot shared_resources) — the pinned shared
    surface between the translator (this card) and the assignment step
    (t_9820b3d3).  Slot ``kind`` values and slot-level capabilities are
    validated against the closed sets on write.
    """

    def __init__(self) -> None:
        super().__init__(WorkPackage)

    def _validate_package(self, wp: WorkPackage) -> None:
        _validate_closed(wp.execution_mode, WORK_PACKAGE_EXECUTION_MODES, "execution_mode")
        if wp.requires_independent_review is not None and not isinstance(
            wp.requires_independent_review, (bool,)
        ):
            raise ClosedCodeError("requires_independent_review must be a boolean")
        slots = wp.task_scope
        if slots is None:
            return
        if not isinstance(slots, list):
            raise ClosedCodeError("task_scope must be a bounded JSON list of slot objects")
        seen: set[str] = set()
        for slot in slots:
            if not isinstance(slot, dict):
                raise ClosedCodeError("task_scope entries must be JSON objects (squad §4.1)")
            slot_id = slot.get("slot")
            if not isinstance(slot_id, str) or not slot_id:
                raise ClosedCodeError("task_scope slot requires a non-empty string 'slot' id")
            if slot_id in seen:
                raise ClosedCodeError(f"duplicate task_scope slot id {slot_id!r}")
            seen.add(slot_id)
            kind = slot.get("kind")
            if kind is not None:
                _validate_closed(kind, TASK_SCOPE_SLOT_KINDS, f"slot {slot_id!r} kind")
            cap = slot.get("required_capabilities")
            if cap is not None and (not isinstance(cap, list) or any(c not in WORK_CAPABILITIES for c in cap)):
                raise ClosedCodeError(f"slot {slot_id!r} required_capabilities must be drawn from WORK_CAPABILITIES")
            candidates = slot.get("candidate_agent_ids")
            if candidates is not None and (
                not isinstance(candidates, list)
                or not all(isinstance(c, str) for c in candidates)
            ):
                raise ClosedCodeError(
                    f"slot {slot_id!r} candidate_agent_ids must be a list of UUID strings"
                )
            depends = slot.get("depends_on_slots")
            if depends is not None and (
                not isinstance(depends, list)
                or not all(isinstance(d, str) for d in depends)
            ):
                raise ClosedCodeError(f"slot {slot_id!r} depends_on_slots must be a list of slot ids")

    async def add_package(
        self,
        package: WorkPackage,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> WorkPackage:
        """Register one work package for an open run in the caller's transaction."""
        self._validate_package(package)
        self.add_scoped(db, package, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(package, attribute_names=["created_at", "updated_at"])
        return package

    async def list_work_packages_for_run(
        self,
        planning_run_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[WorkPackage]:
        """All work packages owned by one run revision, oldest first."""
        tenant_id = self._require_tenant_id()
        stmt = (
            select(WorkPackage)
            .where(WorkPackage.planning_run_id == planning_run_id)
            .order_by(WorkPackage.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(WorkPackage.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def list_work_packages_by_milestone(
        self,
        milestone_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[WorkPackage]:
        """All work packages bucketed under one milestone (design §10).

        A plan may legitimately have no milestones at all (milestone_id is
        nullable on the WP); WPs with ``milestone_id IS NULL`` are the
        "no milestone" set and are NOT returned here — query them via
        ``list_work_packages_for_run`` when the run carries no milestones.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(WorkPackage)
            .where(WorkPackage.milestone_id == milestone_id)
            .order_by(WorkPackage.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(WorkPackage.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def list_milestones_for_run(
        self,
        planning_run_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[Milestone]:
        """All milestone buckets of one run, in ``seq`` order (single query).

        ``seq`` 0 = unordered (design P4); the ``UNIQUE(planning_run_id,
        seq)`` invariant (§6.4) makes the ordering key deterministic.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(Milestone)
            .where(Milestone.planning_run_id == planning_run_id)
            .order_by(Milestone.seq.asc(), Milestone.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(Milestone.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def get_assignment_candidates_for_work_package(
        self,
        work_package_id: uuid.UUID,
        *,
        db=None,
    ) -> dict[str, Any] | None:
        """Assignment INPUT for one work package (design §10 mapping).

        A single bounded query (work_package_tasks -> tasks -> agents JOIN,
        permitted for read-heavy queries within the same DAO) returning:

        - ``work_package`` — the WP row (task_scope slots, execution_mode,
          shared_resources, requires_independent_review);
        - ``goal`` — the owning PlanningGoal with ``required_capabilities``
          (the role/capability need; input to assignment, NOT the fact);
        - ``linked_tasks`` — the materialized Task rows bound to this WP's
          slots via ``work_package_tasks``;
        - ``candidate_agents`` — the live Agents bound to those tasks
          (``Task.agent_id`` is the single assignment fact, D2 — returned
          for the assignment step to re-validate against the live roster;
          the DAO does not resolve, pick, or validate candidates, that is
          the t_9820b3d3 contract).

        Returns ``None`` when the work package does not exist in the
        active tenant scope.  The result is a read-only view: no candidate
        set is persisted (squad design S5: AssignmentPlan is a computed
        report, not a table).
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(WorkPackage, PlanningGoal, Task, Agent)
            .outerjoin(WorkPackageTask, WorkPackageTask.work_package_id == WorkPackage.id)
            .outerjoin(Task, Task.id == WorkPackageTask.task_id)
            .outerjoin(Agent, Agent.id == Task.agent_id)
            .join(PlanningGoal, PlanningGoal.id == WorkPackage.planning_goal_id)
            .where(WorkPackage.id == work_package_id)
        )
        if tenant_id is not None:
            stmt = stmt.where(WorkPackage.tenant_id == tenant_id)
        stmt = stmt.order_by(WorkPackageTask.id.asc())
        async with self.session(db=db, readonly=True) as session_db:
            rows = (await session_db.execute(stmt)).all()
        if not rows:
            return None
        wp = rows[0][0]
        goal = rows[0][1]
        linked_tasks = [r[2] for r in rows if r[2] is not None]
        agent_by_task: dict[uuid.UUID, Agent] = {}
        for r in rows:
            if r[2] is not None and r[3] is not None:
                agent_by_task[r[2].id] = r[3]
        return {
            "work_package": wp,
            "goal": goal,
            "linked_tasks": linked_tasks,
            "candidate_agents": [agent_by_task[t.id] for t in linked_tasks if t.id in agent_by_task],
            "required_capabilities": goal.required_capabilities,
        }


class MilestoneDAO(TenantScopedBaseDAO[Milestone]):
    """Tenant-scoped DAO for milestones rows (ordering/phase buckets)."""

    def __init__(self) -> None:
        super().__init__(Milestone)

    async def add_milestone(
        self,
        milestone: Milestone,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> Milestone:
        """Register one milestone bucket for a run in the caller's transaction.

        The flush may raise ``IntegrityError`` on
        ``UNIQUE(planning_run_id, seq)`` (§6.4: one bucket per position);
        the caller owns that recovery.
        """
        _validate_closed(milestone.kind, MILESTONE_KINDS, "kind")
        self.add_scoped(db, milestone, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(milestone, attribute_names=["created_at", "updated_at"])
        return milestone


class WorkPackageTaskDAO(TenantScopedBaseDAO[WorkPackageTask]):
    """Tenant-scoped DAO for work_package_tasks rows (the Planning->Task link).

    The link is a pure intent record (design P5): ``task_id`` is NULL until
    materialized, and the CHECK invariant §6.3 means a row carrying
    ``materialized_at`` must also carry ``task_id``.  The DAO never
    creates or mutates ``Task`` rows — materialization (creating the Task,
    emitting DAG edges through the frozen task_graph_service) is the
    owning service's lane (design §4); the DAO only persists the link row
    in the caller's transaction.
    """

    def __init__(self) -> None:
        super().__init__(WorkPackageTask)

    async def add_slot(
        self,
        slot: WorkPackageTask,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> WorkPackageTask:
        """Register one (possibly still-open, task_id NULL) link slot."""
        if slot.materialized_at is not None and slot.task_id is None:
            # Fails closed before hitting the CHECK constraint (§6.3).
            raise ClosedCodeError("materialized_at requires task_id (ck_wp_tasks_materialized)")
        self.add_scoped(db, slot, tenant_id=tenant_id)
        await db.flush()
        await db.refresh(slot, attribute_names=["created_at", "updated_at"])
        return slot

    async def materialize_slot(
        self,
        slot: WorkPackageTask,
        *,
        task_id: uuid.UUID,
        db,
    ) -> WorkPackageTask:
        """Fill an open slot with its materialized Task row (design §4).

        Idempotency: the flush may raise ``IntegrityError`` on
        ``UNIQUE(work_package_id, task_id)`` (§6.2) when the slot was
        already filled by a racing call; the caller owns the re-read
        recovery (the PL_TASK_EXISTS no-op path of design §4).
        """
        if slot.task_id is not None and slot.task_id != task_id:
            raise ClosedCodeError(
                f"slot already materialized to task {slot.task_id}; refusing to re-point at {task_id}"
            )
        slot.task_id = task_id
        slot.materialized_at = datetime.now(UTC)
        db.add(slot)
        await db.flush()
        await db.refresh(slot, attribute_names=["updated_at"])
        return slot

    async def reopen_slot(
        self,
        slot: WorkPackageTask,
        *,
        db,
    ) -> WorkPackageTask:
        """Re-open a materialized slot (task_id + materialized_at -> NULL).

        The intent-preserving path of the SET NULL / §6.3 interaction
        (model docstring): re-opening BEFORE a Task delete lets the link
        row survive the delete with ``task_id`` NULL, keeping the
        materialization intent queryable in both directions.
        """
        if slot.task_id is None and slot.materialized_at is None:
            return slot  # already open
        slot.task_id = None
        slot.materialized_at = None
        db.add(slot)
        await db.flush()
        await db.refresh(slot, attribute_names=["updated_at"])
        return slot

    async def list_slots_for_package(
        self,
        work_package_id: uuid.UUID,
        *,
        db=None,
        limit: int = 200,
    ) -> Sequence[WorkPackageTask]:
        """All link slots of one work package (open + materialized)."""
        tenant_id = self._require_tenant_id()
        stmt = (
            select(WorkPackageTask)
            .where(WorkPackageTask.work_package_id == work_package_id)
            .order_by(WorkPackageTask.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(WorkPackageTask.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def get_link_for_task(
        self,
        task_id: uuid.UUID,
        *,
        db=None,
    ) -> WorkPackageTask | None:
        """The (single) link slot that materialized one Task, if any.

        Bounded one-row read by the link's ``task_id``: a Task is materialized
        into at most one work-package slot (the ``uq_wp_tasks`` UNIQUE guard,
        §6.2), so this is a single-row read — ``None`` when the Task was not
        produced by the planning lane (e.g. a MANUAL or ANALYSIS_FINDING task,
        or a planning task whose link row was re-opened / not yet materialized).
        The provenance-query path (``PlanningService.provenance_for_task``)
        consumes this to walk Task -> work package -> goal -> findings.
        """
        tenant_id = self._require_tenant_id()
        stmt = select(WorkPackageTask).where(
            WorkPackageTask.task_id == task_id,
            WorkPackageTask.task_id.isnot(None),
        )
        if tenant_id is not None:
            stmt = stmt.where(WorkPackageTask.tenant_id == tenant_id)
        stmt = stmt.limit(1)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalar_one_or_none()


planning_run_dao = PlanningRunDAO()
planning_goal_dao = PlanningGoalDAO()
work_package_dao = WorkPackageDAO()
milestone_dao = MilestoneDAO()
work_package_task_dao = WorkPackageTaskDAO()

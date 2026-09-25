"""Tenant-scoped DAOs for the Phase 2D Task Graph + Task Provenance domain.

Per docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §3/§4 the two new
persistence objects (``task_dependencies`` edge table + the five
``tasks`` provenance columns) are reached ONLY through
``TenantScopedBaseDAO`` + ``verify_tenant_scope`` (the M9 fourth gate), the
same access path as the Phase 2B/2C Project/Analysis tables.

Layering rules honored (backend/app/dao/AGENTS.md):
- No business logic in this module — edge add/remove, dependency
  aggregation and provenance consistency are bounded DB reads/writes only.
  Cycle detection, blocked/ready computation and the created_reason
  *creation-time* gates are Service-layer concerns (design §5/§6 — a later
  lane owns the graph service); this module gives them the bounded,
  tenant-scoped SQL they consume.
- Every query enforces ``tenant_id`` (P0 C2).  Reads go through the
  active-tenant ContextVar filter; writes use ``add_scoped`` which forces
  tenant alignment.
- DAO methods flush; the owning service/transaction commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dao.base import TenantScopedBaseDAO
from app.models.task import Task, TaskDependency, TaskLog


class TaskDependencyDAO(TenantScopedBaseDAO[TaskDependency]):
    """Tenant-scoped DAO for task_dependencies rows (V1 Task Graph edges).

    An edge row ``task_id -> depends_on_task_id`` means *task_id depends on
    depends_on_task_id* (the arrow points at the upstream dependency).  Both
    ends CASCADE at the DB level so the graph never dangles (design §3.2).
    """

    def __init__(self) -> None:
        super().__init__(TaskDependency)

    async def list_dependencies(
        self,
        task_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
        limit: int = 200,
    ) -> Sequence[TaskDependency]:
        """All direct edges OUT of one task (its dependencies), tenant-scoped.

        Bounded single query: the task's direct upstreams, oldest first.
        This is the "aggregate dependencies on query" read (design §6);
        blocked/ready is derived from these rows by the owning service.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(TaskDependency)
            .where(TaskDependency.task_id == task_id)
            .order_by(TaskDependency.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(TaskDependency.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def list_dependents(
        self,
        task_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
        limit: int = 200,
    ) -> Sequence[TaskDependency]:
        """All direct edges INTO one task (its dependents / downstream).

        Answers "which tasks are blocked by this task's (un)state?" — the
        downstream recompute at completion (design §5.4).  Bounded.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(TaskDependency)
            .where(TaskDependency.depends_on_task_id == task_id)
            .order_by(TaskDependency.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(TaskDependency.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def add_dependency(
        self,
        edge: TaskDependency,
        *,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> TaskDependency:
        """Register one edge in the caller's tenant scope + transaction.

        The flush rides the caller's session (committed by the owning
        service's transaction).  The flush may raise ``IntegrityError`` on
        the ``ck_task_dep_no_self`` / ``uq_task_depends_pair`` invariants
        (dirty direct write); the service layer is the authoritative gate
        and supplies the clear error codes (design §5.1).
        """
        self.add_scoped(db, edge, tenant_id=tenant_id)
        await db.flush()
        # The INSERT flush expires the server-generated columns (created_at /
        # updated_at) on the in-memory object; re-load them inside the session
        # so the caller's view never triggers a greenlet refresh (f068
        # analysis_dao precedent).
        await db.refresh(edge, attribute_names=["created_at", "updated_at"])
        return edge

    async def add_dependencies(
        self,
        edges: Sequence[TaskDependency],
        *,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> Sequence[TaskDependency]:
        """Register a bounded set of edges for one dependee, one transaction.

        All-or-nothing: one offending edge rolls the batch back at commit.
        The service layer does the per-edge validation (self / tenant /
        project / cycle) up front so this only ever sees validated edges.
        """
        for edge in edges:
            self.add_scoped(db, edge, tenant_id=tenant_id)
        await db.flush()
        for edge in edges:
            await db.refresh(edge, attribute_names=["created_at", "updated_at"])
        return list(edges)

    async def list_edges_for(
        self,
        task_ids: Sequence[uuid.UUID],
        *,
        db: AsyncSession | None = None,
        limit: int = 200,
    ) -> Sequence[TaskDependency]:
        """All direct edges OUT of a bounded set of tasks in one read (no N+1).

        The batched edge half of the §5.3 ready/blocked derivation: given a
        set of tasks, return every dependency edge they own so the owning
        service can batch-load the dependency statuses.  Empty input returns
        an empty list (no query issued).  Bounded by ``limit``.
        """
        if not task_ids:
            return []
        tenant_id = self._require_tenant_id()
        stmt = (
            select(TaskDependency)
            .where(TaskDependency.task_id.in_(list(task_ids)))
            .order_by(TaskDependency.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(TaskDependency.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def project_dependency_edges(
        self,
        project_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
        limit: int = 1000,
    ) -> Sequence[TaskDependency]:
        """The bounded subgraph of edges owned by one project (one JOIN read).

        The §5.2 cycle check needs the candidate task's whole-project edge set
        in a single bounded read (design: "项目级图远小于全表", never an
        unbounded tenant-wide topology).  Design §3.1 guarantees every edge has
        both ends in the same project, so joining the edge's dependee
        (``task_id``) onto ``tasks.project_id`` captures the entire project
        subgraph.  Empty / cross-project reads return an empty list.
        """
        from app.models.task import Task

        tenant_id = self._require_tenant_id()
        stmt = (
            select(TaskDependency)
            .join(Task, Task.id == TaskDependency.task_id)
            .where(Task.project_id == project_id)
            .order_by(TaskDependency.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(TaskDependency.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def remove_dependency(
        self,
        task_id: uuid.UUID,
        depends_on_task_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        """Delete one edge within the active tenant scope.

        Returns True when a row was removed, False when the edge did not
        exist for this tenant (404 handling belongs to the service/API
        layer).  Tenant-scoped: an edge of the same pair in another tenant
        is never touched.
        """
        tenant_id = self._require_tenant_id()
        async with self.session(db=db) as session_db:
            stmt = delete(TaskDependency).where(
                TaskDependency.task_id == task_id,
                TaskDependency.depends_on_task_id == depends_on_task_id,
            )
            if tenant_id is not None:
                stmt = stmt.where(TaskDependency.tenant_id == tenant_id)
            result = await session_db.execute(stmt)
            await session_db.flush()
            # ``Result.rowcount`` reflects ``cursor.rowcount`` for the DELETE
            # (verified against the live Postgres Result: a removed row reads
            # ``rowcount == 1``).  Read through ``getattr`` because the async
            # generic ``Result`` surface isn't in pyright's static model; a
            # mock / non-DB session (DB-free tiers) lacks the attribute and
            # falls back to 0 = "not removed".
            row_count = int(getattr(result, "rowcount", 0) or 0)
            return row_count > 0


class TaskProvenanceDAO(TenantScopedBaseDAO[Task]):
    """Task-row provenance + dependency aggregation (the read side of §6).

    Bounded reads that span the Task row and the Task Graph edge table.
    Inherits ``TenantScopedBaseDAO`` over ``Task`` (which declares
    ``__tenant_scoped__ = True``) so every read is tenant-filtered by the
    M9 gate exactly like the Phase 2B/2C tables.
    """

    def __init__(self) -> None:
        super().__init__(Task)

    async def latest_task_logs(
        self,
        task_id: uuid.UUID,
        *,
        since: datetime | None = None,
        limit: int = 5,
        db: AsyncSession | None = None,
    ) -> Sequence[TaskLog]:
        """The newest bounded set of TaskLog lines for one task.

        Phase 2E §10.2: the query endpoint's ``result_summary`` reuses the
        latest settlement TaskLog line (root §十五: no new Artifact system —
        "reuse Run result / TaskLog / revisions").  Bounded by ``limit``;
        TaskLog has no tenant column, so the Task-row scope (``task_id``
        FK + the caller's tenant-scoped task load) is the isolation seam.
        """
        stmt = (
            select(TaskLog)
            .where(TaskLog.task_id == task_id)
            .order_by(TaskLog.created_at.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(TaskLog.created_at >= since)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def create_with_provenance(
        self,
        task: Task,
        *,
        db: AsyncSession,
    ) -> Task:
        """Persist a Task row carrying its provenance columns in one write.

        This is the "write provenance on task creation" path (design §6,
        card t_650ddd87): the five provenance columns ride the standard Task
        INSERT.  The flush rides the caller's session (committed by the owning
        service/transaction).  A Task created with no provenance fields simply
        persists with the analysis columns NULL and ``created_reason``
        defaulting to MANUAL (the legacy manual path is byte-identical).

        Cross-table consistency of the carried provenance is validated by
        ``provenance_consistency`` (the §4.2 rules) by the owning service;
        this DAO only persists the decided values.
        """
        db.add(task)
        await db.flush()
        # The INSERT flush expires the server-generated columns (created_at /
        # updated_at) on the in-memory object; re-load them inside the session
        # so the caller's view (the TaskOut enrichment) never triggers a
        # greenlet refresh (f068 analysis_dao precedent).
        await db.refresh(task, attribute_names=["created_at", "updated_at"])
        return task

    async def converted_finding_ids(
        self,
        analysis_run_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
        limit: int = 200,
    ) -> set[uuid.UUID]:
        """The set of ``finding_id``s already converted into Tasks for one run.

        The bounded dedup pre-check of the Analysis→Task mapping lane
        (PHASE_2D_ANALYSIS_TASK_MAPPING.md §4): a finding may be converted once
        per run.  One read returns every finding of this run that already has a
        converted Task, so the owning service skips it as ``skipped_duplicate``
        instead of re-creating.  The last-resort guard is the f070
        ``UNIQUE(analysis_run_id, finding_id)`` constraint on a racing write.
        Empty when the run has no converted Tasks (a normal first invocation).
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(Task.finding_id)
            .where(
                Task.analysis_run_id == analysis_run_id,
                Task.finding_id.isnot(None),
                Task.created_reason == "ANALYSIS_FINDING",
            )
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(Task.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            rows = (await session_db.execute(stmt)).all()
        # The WHERE clause already excludes NULL finding_id rows; drop any None
        # defensively so the returned set is a clean set[uuid.UUID] (the owner
        # uses it to skip already-converted findings — spec §4).
        result: set[uuid.UUID] = set()
        for row in rows:
            finding_id = row[0]
            if finding_id is not None:
                result.add(finding_id)
        return result

    async def task_status_map(
        self,
        task_ids: Sequence[uuid.UUID],
        *,
        db: AsyncSession | None = None,
    ) -> dict[uuid.UUID, str]:
        """Bounded ``{task_id: status}`` for one set of tasks, tenant-scoped.

        The second half of the ready/blocked derivation (design §5.3): given
        a task's direct dependency ids, this one query returns their
        statuses so the service can compute blocked/ready without an N+1.
        Empty input returns an empty map (no query issued).
        """
        if not task_ids:
            return {}
        tenant_id = self._require_tenant_id()
        stmt = select(Task.id, Task.status).where(Task.id.in_(list(task_ids)))
        if tenant_id is not None:
            stmt = stmt.where(Task.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            rows = (await session_db.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def provenance_consistency(
        self,
        task: Task,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        """Single-SQL cross-table check of the §4.2 provenance integrity rules.

        Validates that the task's ``created_reason`` is backed by consistent,
        tenant-aligned provenance rows:

        - ``MANUAL``            -> all four analysis-side columns must be NULL
          (a manual Task carries no partial provenance — design §4.2).
        - ``ANALYSIS_PLANNING`` -> project_id, analysis_run_id, revision_sha
          present (finding_id may be NULL); the run must exist, belong to the
          project, and carry the snapshot revision.
        - ``ANALYSIS_FINDING``  -> all four columns present; the run must
          exist in the same tenant with matching project + revision, be in
          terminal ``AN_COMPLETED`` state (design §4.2 run gate, audit §E),
          and the finding must belong to that run (cross-table, one SQL).

        Returns True when the rules hold; the service layer turns False into
        a closed fail-closed rejection.  A task with a NULL/unknown
        ``created_reason`` fails closed.
        """
        from app.models.analysis import AnalysisFinding, AnalysisRun

        reason = task.created_reason
        if reason not in {"MANUAL", "ANALYSIS_PLANNING", "ANALYSIS_FINDING"}:
            return False

        # MANUAL: no analysis-side provenance may be present (design §4.2).
        if reason == "MANUAL":
            return (
                task.project_id is None
                and task.analysis_run_id is None
                and task.finding_id is None
                and task.revision_sha is None
            )

        # ANALYSIS_FINDING / ANALYSIS_PLANNING: project_id + analysis_run_id +
        # revision_sha are mandatory; ANALYSIS_FINDING additionally requires
        # finding_id (a finding is what makes the reason "FINDING").
        if task.project_id is None or task.analysis_run_id is None or task.revision_sha is None:
            return False
        if reason == "ANALYSIS_FINDING" and task.finding_id is None:
            return False

        tenant_id = self._require_tenant_id()
        stmt = (
            select(AnalysisRun.id)
            .where(
                AnalysisRun.id == task.analysis_run_id,
                AnalysisRun.project_id == task.project_id,
                AnalysisRun.revision_sha == task.revision_sha,
            )
            .limit(1)
        )
        if tenant_id is not None:
            stmt = stmt.where(AnalysisRun.tenant_id == tenant_id)
        if reason == "ANALYSIS_FINDING":
            # Terminal run gate + the finding-must-belong-to-the-run join in
            # ONE SQL (inner join enforces finding existence for this run).
            stmt = (
                stmt.join(
                    AnalysisFinding,
                    and_(
                        AnalysisFinding.analysis_run_id == AnalysisRun.id,
                        AnalysisFinding.id == task.finding_id,
                    ),
                )
                .where(AnalysisRun.status == "AN_COMPLETED")
            )
            if tenant_id is not None:
                stmt = stmt.where(AnalysisFinding.tenant_id == tenant_id)

        async with self.session(db=db, readonly=True) as session_db:
            result = (await session_db.execute(stmt)).first()
        return result is not None


task_dependency_dao = TaskDependencyDAO()
task_provenance_dao = TaskProvenanceDAO()

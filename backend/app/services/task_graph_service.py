"""Phase 2D Task Graph service — validation, blocked/ready, and the execution gate.

Per docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §5/§6 this service OWNS the
graph semantics that the persistence layer (t_650ddd87) deliberately does NOT
carry ("不实现复杂图计算"):

- **Edge validation** (§5.1): self-dependency, tenant alignment, same-project,
  todo-only, and cycle prevention (bounded DFS, §5.2) — fail closed, in order,
  with a closed rejection-code set before any row is written.
  - **R1 serialization (§8 R1):** the §5.2 reachability check is a read-then-
    write; to close the concurrent-inverse-insert 2-cycle window under Postgres
    READ COMMITTED, ``_add_edges`` takes a project-scoped transaction advisory
    lock (``_lock_project_graph``, ``pg_advisory_xact_lock``) around the check
    + insert, so the second inverse insert observes the first committed edge
    and is refused by the reachability check.
- **blocked/ready** (§5.3): DERIVED, not persisted — ``ready`` = every direct
  dependency is ``done`` (an empty dependency set is trivially ready);
  ``blocked`` = some direct dependency is not done. Bounded reads only.
- **execution gate** (§5.4): ``ensure_ready`` returns the unmet dependency ids
  of a task; the caller (``task_executor.enqueue_task_runtime``) refuses to
  create a Run when that list is non-empty (status stays ``pending``, a TaskLog
  explains it) and passes a ready task through the existing path unchanged.

Layering honored (backend/app/dao/AGENTS.md): business policy lives HERE; the
DAO (``task_dao``) supplies the bounded, tenant-scoped reads/writes this module
consumes (``task_status_map`` + ``provenance_consistency`` are the bounded
primitives the persistence lane left for us).  Every graph read is bounded to
one task's direct edges or one project's edge set — never an unbounded
tenant-wide topology.

The API lane (card t_b4a29991) is the consumer of these methods; they return
closed code + data so the transport maps to 400/404/409 without re-reading the
graph.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dao import task_dependency_dao, task_provenance_dao
from app.models.task import Task, TaskDependency

# ---------------------------------------------------------------------------
# Closed rejection-code set for graph edge operations (design §5.1).  A code
# outside this set is a programming error; the service validates against it
# before returning an outcome (mirrors the ACQ_*/AN_* closed-code pattern).
# ---------------------------------------------------------------------------

GRAPH_SELF = "GRAPH_SELF"
GRAPH_MISMATCH_TENANT = "GRAPH_MISMATCH_TENANT"
GRAPH_MISMATCH_PROJECT = "GRAPH_MISMATCH_PROJECT"
GRAPH_SUPERVISION_NOT_ALLOWED = "GRAPH_SUPERVISION_NOT_ALLOWED"
GRAPH_CYCLE = "GRAPH_CYCLE"
GRAPH_EXISTS = "GRAPH_EXISTS"
GRAPH_NOT_FOUND = "GRAPH_NOT_FOUND"
GRAPH_INVALID = "GRAPH_INVALID"

GRAPH_REJECTION_CODES = frozenset(
    {
        GRAPH_SELF,
        GRAPH_MISMATCH_TENANT,
        GRAPH_MISMATCH_PROJECT,
        GRAPH_SUPERVISION_NOT_ALLOWED,
        GRAPH_CYCLE,
        GRAPH_EXISTS,
        GRAPH_NOT_FOUND,
        GRAPH_INVALID,
    }
)

#: Bounded input limits (a pathological set fails closed rather than scanning
#: an unbounded tenant-wide graph — design §5.2 "项目级图远小于全表").
MAX_PROJECT_EDGES = 1000
MAX_BATCH_EDGES = 100


class GraphEdgeError(Exception):
    """A closed GRAPH_* rejection outcome (400/404/409-class).

    Carries the rejection code + a human detail.  The API lane maps this to the
    transport (409 for validation rejections, 404 for GRAPH_NOT_FOUND) instead
    of surfacing a 500 — the same fail-closed contract the AN_*/ACQ_* codes
    carry.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if code not in GRAPH_REJECTION_CODES:
            raise ValueError(f"graph code {code!r} is not in the closed rejection set")
        self.code = code
        self.detail = message


class TaskBlockedError(Exception):
    """The execution gate (§5.4) has unmet dependencies for a task.

    ``reason`` is the bounded, actionable list of not-yet-done direct
    dependency ids — the payload the TaskLog / API surface.  A task that
    blocks has NOT been enqueued and keeps status ``pending``.
    """

    def __init__(self, task_id: uuid.UUID, unmet: Sequence[uuid.UUID]) -> None:
        super().__init__(f"task {task_id} is blocked by unmet dependencies {list(unmet)}")
        self.task_id = task_id
        self.reason = list(unmet)


@dataclass
class GraphEdgeOutcome:
    """The transport shape of an edge / readiness write (mirrors AN_* outcomes)."""

    state: str  # "added" | "removed" | "blocked"
    code: str | None = None
    detail: str = ""
    edge: TaskDependency | None = None


@dataclass
class ReadinessOutcome:
    """The bounded per-task graph view the API returns (design §6 GET /graph)."""

    task_id: uuid.UUID
    state: str  # "ready" | "blocked" | "not_applicable"
    direct_dependencies: list[dict] = field(default_factory=list)
    blocking: list[uuid.UUID] = field(default_factory=list)


def upstream_reachable(
    pairs: Iterable[tuple[uuid.UUID, uuid.UUID]],
    start: uuid.UUID,
    target: uuid.UUID,
    *,
    limit: int = MAX_PROJECT_EDGES,
) -> bool:
    """Pure, DB-free reachability core of the §5.2 cycle check (unit-testable).

    A directed edge ``(task_id, depends_on_task_id)`` means ``task_id`` depends
    on ``depends_on_task_id`` (the arrow points at the upstream).  Inserting
    ``t <- u`` (t depends on u) creates a cycle IFF u already (transitively)
    depends on t, i.e. ``target`` is reachable from ``start`` by following the
    depends-on direction.  Bounded by ``limit`` visited nodes; an oversized
    graph is reported reachable so the candidate edge is REFUSED (fail closed)
    rather than admitted into a graph beyond the V1 bounded assumption.
    """
    dependents: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task_id, dep_on in pairs:
        dependents.setdefault(task_id, []).append(dep_on)

    seen: set[uuid.UUID] = set()
    queue: deque[uuid.UUID] = deque([start])
    visited = 0
    while queue:
        node = queue.popleft()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        visited += 1
        if visited > limit:
            return True
        for nxt in dependents.get(node, ()):
            if nxt not in seen:
                queue.append(nxt)
    return False


async def _lock_project_graph(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """Serialize concurrent edge authoring for one (tenant, project) scope (design §8 R1).

    The §5.2 cycle gate is a read-then-write over the project edge set. Under
    Postgres READ COMMITTED two concurrent *inverse* inserts (``A->B`` and
    ``B->A``) can each read the pre-commit state, both pass the reachability
    check, and both commit a 2-cycle that ``UNIQUE(self-pair)`` + the self
    ``CHECK`` cannot catch.  Taking a transaction-scoped advisory lock
    (``pg_advisory_xact_lock``, held until the surrounding transaction
    commits/rolls back) keyed on the project scope serializes that window: the
    second writer blocks here until the first commits, then re-reads the
    committed edge and is refused by the reachability check.  Same precedent as
    ``chat_session_service._lock_direct_scope`` (hashtextextended scope key).
    """
    scope_key = f"task_graph:{tenant_id}:{project_id}"
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(scope_key, 0))))


class TaskGraphService:
    """The V1 Task Graph semantics owner.  The API lane calls ``add_edge`` /
    ``bulk_add_edges`` / ``remove_edge`` / ``graph`` / ``ensure_ready``;
    everything else is internal.  All reads are bounded + tenant-scoped (M9)."""

    # ------------------------------------------------------------------
    # Edge authoring (design §5.1 / §5.2).
    # ------------------------------------------------------------------
    async def add_edge(
        self,
        db,
        *,
        task_id: uuid.UUID,
        depends_on_task_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> GraphEdgeOutcome:
        """Add one edge ``task_id -> depends_on_task_id`` (task depends on dep).

        Fail-closed validation order (design §5.1): not-found / tenant / self /
        supervision / project / cycle / exists.  A rejection writes nothing; a
        success registers the edge in the caller's transaction.
        """
        return await self._add_edges(db, task_id, [depends_on_task_id], tenant_id)

    async def bulk_add_edges(
        self,
        db,
        *,
        task_id: uuid.UUID,
        depends_on_task_ids: Sequence[uuid.UUID],
        tenant_id: uuid.UUID,
    ) -> GraphEdgeOutcome:
        """Add a bounded set of edges out of ``task_id`` in one transaction.

        All-or-nothing: one offending edge rejects the whole batch and writes
        nothing (design §5.1 batch row).  Bounded to MAX_BATCH_EDGES.
        """
        if not depends_on_task_ids:
            return GraphEdgeOutcome(state="blocked", code=GRAPH_INVALID, detail="no candidate edges")
        if len(depends_on_task_ids) > MAX_BATCH_EDGES:
            return GraphEdgeOutcome(
                state="blocked",
                code=GRAPH_INVALID,
                detail=f"batch of {len(depends_on_task_ids)} edges exceeds the {MAX_BATCH_EDGES} bound",
            )
        return await self._add_edges(db, task_id, list(depends_on_task_ids), tenant_id)

    async def _add_edges(
        self,
        db,
        task_id: uuid.UUID,
        candidate_deps: Sequence[uuid.UUID],
        tenant_id: uuid.UUID,
    ) -> GraphEdgeOutcome:
        # 1) Load the dependee (downstream) + every candidate upstream.  A scoped
        #    read returns None cross-tenant / missing -> GRAPH_NOT_FOUND.
        dependee = await task_provenance_dao.get_scoped(task_id, db=db)
        if dependee is None:
            return GraphEdgeOutcome(state="blocked", code=GRAPH_NOT_FOUND, detail="dependee task not found")

        resolved: list[tuple[uuid.UUID, Task]] = []
        for dep in candidate_deps:
            up = await task_provenance_dao.get_scoped(dep, db=db)
            if up is None:
                return GraphEdgeOutcome(state="blocked", code=GRAPH_NOT_FOUND, detail="dependency task not found")
            resolved.append((dep, up))

        # 2) Per-edge validation, in the closed order (design §5.1).
        for dep, up in resolved:
            if up.tenant_id is None or dependee.tenant_id is None or up.tenant_id != dependee.tenant_id:
                return GraphEdgeOutcome(state="blocked", code=GRAPH_MISMATCH_TENANT, detail="edge endpoints carry different tenants")
            if dep == task_id:
                return GraphEdgeOutcome(state="blocked", code=GRAPH_SELF, detail="a task may not depend on itself")
            if up.type != "todo" or dependee.type != "todo":
                return GraphEdgeOutcome(state="blocked", code=GRAPH_SUPERVISION_NOT_ALLOWED, detail="dependencies only attach to todo tasks")
            if not dependee.project_id or dependee.project_id != up.project_id:
                return GraphEdgeOutcome(state="blocked", code=GRAPH_MISMATCH_PROJECT, detail="edge endpoints belong to different projects")

        # 3) Cycle gate (design §5.2): the bounded project edge set ONCE, then
        #    every candidate against it.  ``task <- dep`` cycles iff ``dep``
        #    already transitively depends on ``task``.  (project_id was validated
        #    non-None per edge above; capture it narrowed for the bounded read.)
        project_id = dependee.project_id
        if project_id is None:
            # Defensive: unreachable once the per-edge project guard passed, but
            # keeps the nullable narrowing honest for the bounded DAO read.
            return GraphEdgeOutcome(state="blocked", code=GRAPH_MISMATCH_PROJECT, detail="edge endpoints belong to different projects")

        # R1 (design §8): serialize this project scope for the check+write window.
        # The lock is held to transaction end, so the reachability read below
        # and the §5 write above the caller's commit are one atomic section; a
        # concurrent inverse insert blocks here until this tx commits, then
        # re-reads the committed edge and is refused (see _lock_project_graph).
        await _lock_project_graph(db, tenant_id, project_id)

        project_edges = await task_dependency_dao.project_dependency_edges(project_id, db=db, limit=MAX_PROJECT_EDGES)
        pairs = [(e.task_id, e.depends_on_task_id) for e in project_edges]
        for dep, _up in resolved:
            if upstream_reachable(pairs, start=dep, target=task_id, limit=MAX_PROJECT_EDGES):
                return GraphEdgeOutcome(state="blocked", code=GRAPH_CYCLE, detail="edge would create a cycle")

        # 4) Duplicate guard (the UNIQUE pair invariant; service-level fast path).
        existing = {
            e.depends_on_task_id
            for e in await task_dependency_dao.list_dependencies(task_id, db=db, limit=MAX_PROJECT_EDGES)
        }
        for dep, _up in resolved:
            if dep in existing:
                return GraphEdgeOutcome(state="blocked", code=GRAPH_EXISTS, detail="dependency edge already exists")

        # 5) Write — validated edges only, in the caller's transaction.
        edges = [
            TaskDependency(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                task_id=task_id,
                depends_on_task_id=dep,
            )
            for dep, _up in resolved
        ]
        written = await task_dependency_dao.add_dependencies(edges, tenant_id=tenant_id, db=db)
        return GraphEdgeOutcome(state="added", edge=(written[0] if len(written) == 1 else None))

    async def remove_edge(
        self,
        db,
        *,
        task_id: uuid.UUID,
        depends_on_task_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> GraphEdgeOutcome:
        """Remove one edge (404-class when the edge is absent for this tenant)."""
        removed = await task_dependency_dao.remove_dependency(task_id, depends_on_task_id, db=db)
        if not removed:
            return GraphEdgeOutcome(state="blocked", code=GRAPH_NOT_FOUND, detail="edge not found")
        return GraphEdgeOutcome(state="removed")

    # ------------------------------------------------------------------
    # blocked / ready (design §5.3 — derived, bounded, not persisted).
    # ------------------------------------------------------------------
    async def is_ready(self, db, *, task_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        """True when the task's direct dependencies are all ``done`` (or none).

        The graph fact the gate and API consume.  A task with no dependency
        edges is trivially ready (design §5.3).
        """
        deps = await task_dependency_dao.list_dependencies(task_id, db=db, limit=MAX_PROJECT_EDGES)
        if not deps:
            return True
        status_map = await task_provenance_dao.task_status_map([d.depends_on_task_id for d in deps], db=db)
        return all(status_map.get(d.depends_on_task_id) == "done" for d in deps)

    async def ready_states(
        self,
        db,
        *,
        task_ids: Sequence[uuid.UUID],
        tenant_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """Bounded ``{task_id: "ready"|"blocked"}`` over a set of tasks (§5.3).

        Two reads total (no N+1): one batched edge read for the set, then one
        batched status read for all dependency ids.  "blocked" when any direct
        dependency is not ``done``; "ready" otherwise (an empty set is ready).
        """
        if not task_ids:
            return {}
        edges = await task_dependency_dao.list_edges_for(list(task_ids), db=db, limit=MAX_PROJECT_EDGES)
        by_task: dict[uuid.UUID, set[uuid.UUID]] = {tid: set() for tid in task_ids}
        all_dep_ids: set[uuid.UUID] = set()
        for e in edges:
            by_task.setdefault(e.task_id, set()).add(e.depends_on_task_id)
            all_dep_ids.add(e.depends_on_task_id)
        status_map = await task_provenance_dao.task_status_map(list(all_dep_ids), db=db)
        return {
            tid: ("ready" if all(status_map.get(d) == "done" for d in by_task.get(tid, set())) else "blocked")
            for tid in task_ids
        }

    async def graph(self, db, *, task_id: uuid.UUID, tenant_id: uuid.UUID) -> ReadinessOutcome:
        """The bounded per-task graph view the API returns (design §6 GET /graph).

        Supervision tasks have no dependency edges (design §3.1) ->
        ``not_applicable``.  A todo task is "ready" when every direct
        dependency is done, "blocked" otherwise.
        """
        task = await task_provenance_dao.get_scoped(task_id, db=db)
        if task is None:
            raise GraphEdgeError(GRAPH_NOT_FOUND, "task not found")
        if task.type != "todo":
            return ReadinessOutcome(task_id=task_id, state="not_applicable")
        deps = await task_dependency_dao.list_dependencies(task_id, db=db, limit=MAX_PROJECT_EDGES)
        if not deps:
            return ReadinessOutcome(task_id=task_id, state="ready")
        status_map = await task_provenance_dao.task_status_map([d.depends_on_task_id for d in deps], db=db)
        direct = [{"id": d.depends_on_task_id, "status": status_map.get(d.depends_on_task_id)} for d in deps]
        blocking = [d.depends_on_task_id for d in deps if status_map.get(d.depends_on_task_id) != "done"]
        return ReadinessOutcome(
            task_id=task_id,
            state="blocked" if blocking else "ready",
            direct_dependencies=direct,
            blocking=blocking,
        )

    # ------------------------------------------------------------------
    # The execution gate (design §5.4).
    # ------------------------------------------------------------------
    async def ensure_ready(self, db, *, task: Task, tenant_id: uuid.UUID) -> list[uuid.UUID]:
        """Return the unmet dependency ids of ``task`` (empty = ready).

        Supervision tasks carry no edges (design §3.1) -> empty.  The caller
        (``enqueue_task_runtime``) refuses to create a Run when this is
        non-empty: the task stays ``pending`` and a TaskLog records the block.
        """
        if task.type != "todo":
            return []
        deps = await task_dependency_dao.list_dependencies(task.id, db=db, limit=MAX_PROJECT_EDGES)
        if not deps:
            return []
        status_map = await task_provenance_dao.task_status_map([d.depends_on_task_id for d in deps], db=db)
        return [d.depends_on_task_id for d in deps if status_map.get(d.depends_on_task_id) != "done"]


task_graph_service = TaskGraphService()

__all__ = [
    "GRAPH_CYCLE",
    "GRAPH_EXISTS",
    "GRAPH_INVALID",
    "GRAPH_MISMATCH_PROJECT",
    "GRAPH_MISMATCH_TENANT",
    "GRAPH_NOT_FOUND",
    "GRAPH_REJECTION_CODES",
    "GRAPH_SELF",
    "GRAPH_SUPERVISION_NOT_ALLOWED",
    "GraphEdgeError",
    "GraphEdgeOutcome",
    "ReadinessOutcome",
    "TaskBlockedError",
    "TaskGraphService",
    "task_graph_service",
    "upstream_reachable",
]

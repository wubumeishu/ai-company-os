"""Phase 3 minimal adapter: hand a materialized ProjectPlan to the existing
Phase-2E task execution intake (card t_e89399cc).

Design contract (PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN D2/D3, the root
brief's hard rule "do not create a second runtime"):

- Planning materializes a durable ProjectPlan: real Task rows
  (``created_reason=ANALYSIS_PLANNING``) + ``task_dependencies`` edges +
  ``work_package_tasks`` links, via :mod:`app.services.planning_service`.
- Assignment resolves one ``Task.agent_id`` fact per work package, via
  :mod:`app.services.assignment_service`.
- Execution is the separately-authorized Phase-2E lane:
  :meth:`app.services.task_execution_service.TaskExecutionService.execute`
  gates + enqueues a real Run through the Phase-2F Runtime spine
  (``enqueue_task_runtime``).  Settlement is owned by that same Runtime
  worker (``TaskRuntimeCompletionHandler``) — nothing here re-implements
  claiming, lanes, settlement, or retry.

This module is a THIN transport between the planning/assignment outputs and
that intake.  It deliberately owns NO lifecycle of its own:

- no new tables, state machines, locks, or org entities;
- no automatic retry of blocked tasks (the Phase-2E ``R4`` rule: a new
  attempt id is minted only by a human-initiated Execute call);
- blocked / rejected outcomes are reported, not raised — the caller decides
  whether to resolve the unmet dependencies and call again.

It also exposes a pure bounded topological ordering helper over the plan's
dependency DAG so a caller (or test) can enqueue in dependency order
without re-implementing the graph walk.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.dao.base import tenant_context
from app.dao.planning_dao import (
    work_package_dao,
    work_package_task_dao,
)
from app.dao.task_dao import task_dependency_dao, task_provenance_dao
from app.models.agent import Agent
from app.models.task import Task
from app.models.user import User
from app.services.assignment_service import assignment_service
from app.services.task_execution_service import (
    TaskExecutionError,
    TaskExecutionOutcome,
    task_execution_service,
)

# Bounded reads at the owning layer (data access is bounded + evidence-driven):
# a single plan revision carries a bounded task set + DAG; never load an
# unbounded set for downstream filtering.
_PLAN_TASK_READ_LIMIT = 200
_PLAN_EDGE_READ_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class PlanExecutionReport:
    """The transport result of one ``enqueue_plan_tasks`` call.

    - ``enqueued``   — task ids accepted by the Phase-2E gate (a real Run
      was created or reused); these settle through the existing Runtime
      worker, never through this module.
    - ``blocked``    — (task_id, unmet_dependency_ids) rejected by the gate
      as TASK_BLOCKED; the task stays pending, nothing is auto-retried.
    - ``rejected``         — task_id -> closed gate code for any other
      rejection.
    - ``assignment``       — work_package_id -> (state, code) when
      ``apply_assignment`` is requested.
    - ``skipped_settled``  — task ids already in a settled / in-flight Task
      state at load time (``done`` / ``doing``): re-invoking the adapter
      after settling an upstream dependency must not re-feed the terminal
      task to the gate (which would raise TASK_TERMINAL); the caller's
      re-invocation only enqueues the tasks that are still actionable.
    """

    enqueued: tuple[TaskExecutionOutcome, ...] = ()
    blocked: tuple[tuple[uuid.UUID, tuple[uuid.UUID, ...]], ...] = ()
    rejected: dict[uuid.UUID, str] = field(default_factory=dict)
    assignment: dict[uuid.UUID, tuple[str, str | None]] = field(default_factory=dict)
    skipped_settled: tuple[uuid.UUID, ...] = ()

    @property
    def blocked_task_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(task_id for task_id, _ in self.blocked)

    @property
    def enqueued_task_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(outcome.task_id for outcome in self.enqueued)

    @property
    def everything_enqueued(self) -> bool:
        """True when no task was blocked or rejected by the gate."""
        return not self.blocked and not self.rejected


def topological_order(
    task_ids: Sequence[uuid.UUID],
    edges: Sequence[Any],
) -> list[uuid.UUID]:
    """Deterministic dependency-safe order over the plan's direct DAG.

    ``edges`` are ``TaskDependency`` rows (``task_id`` depends on
    ``depends_on_task_id``).  The order is stable: tasks whose in-DAG
    dependencies are already placed come first, ties broken by first
    appearance in ``task_ids`` (the planner's materialization order — a
    serial delivery group).  A cycle or an edge pointing outside the
    bounded set is a data defect and raises ``ValueError`` (fail closed —
    never enqueue a truncated walk).
    """
    ids = list(dict.fromkeys(task_ids))
    id_set = frozenset(ids)
    deps: dict[uuid.UUID, set[uuid.UUID]] = {tid: set() for tid in ids}
    for edge in edges:
        if edge.task_id not in id_set or edge.depends_on_task_id not in id_set:
            raise ValueError(
                "plan dependency edge points outside the bounded task set; refusing"
            )
        deps[edge.task_id].add(edge.depends_on_task_id)

    order: list[uuid.UUID] = []
    remaining = set(ids)
    appearance = {tid: index for index, tid in enumerate(ids)}
    while remaining:
        ready = sorted(
            (tid for tid in remaining if not (deps[tid] & remaining)),
            key=lambda tid: appearance[tid],
        )
        if not ready:
            raise ValueError("plan dependency DAG has a cycle; refusing to order")
        chosen = ready[0]
        order.append(chosen)
        remaining.discard(chosen)
    return order


async def _linked_tasks_for_package(
    work_package_id: uuid.UUID,
    *,
    db: AsyncSession,
) -> list[Task]:
    """The materialized Tasks of one work package (bounded batched read)."""
    slots = list(
        await work_package_task_dao.list_slots_for_package(
            work_package_id, db=db, limit=_PLAN_TASK_READ_LIMIT
        )
    )
    task_ids = [slot.task_id for slot in slots if slot.task_id is not None]
    if not task_ids:
        return []
    # One bounded batched read (no N+1): TaskProvenanceDAO is tenant-scoped,
    # so the read runs under the active tenant_context.
    rows = list(
        await task_provenance_dao.list_scoped(
            extra_filters=[Task.id.in_(task_ids)],
            limit=_PLAN_TASK_READ_LIMIT,
            db=db,
        )
    )
    by_id = {row.id: row for row in rows}
    # Preserve the package's declared slot order.
    return [by_id[tid] for tid in task_ids if tid in by_id]


class PlanExecutionService:
    """Feeds Planning + Assignment outputs into the Phase-2E execution gate."""

    async def enqueue_plan_tasks(
        self,
        db: AsyncSession,
        *,
        planning_run_id: uuid.UUID,
        agent: Agent,
        current_user: User,
        apply_assignment: bool = True,
    ) -> PlanExecutionReport:
        """Run the plan's materialized tasks through the real Phase-2E gate.

        All work is in the caller transaction and tenant-scoped via the
        owning DAOs + ``tenant_context``:

        1. For each work package of the run, if ``apply_assignment``:
           :meth:`assignment_service.apply_assignment` — the fail-closed
           constraint layer.  A refused package's tasks are NOT enqueued
           (the planner / human resolves it; the report carries the code).
        2. Load that package's materialized Tasks (bounded) + the plan's
           direct dependency edges (bounded); derive the deterministic
           dependency-safe enqueue order.
        3. For each task in order: ``TaskExecutionService.execute``.  A
           ``TASK_BLOCKED`` rejection is captured (the task stays pending;
           nothing is auto-retried — the Phase-2E R4 rule).  Other gate
           rejections are captured by their closed code.

        Nothing here executes the Runs: they are enqueued into the command
        inbox and settle through the Phase-2F Runtime worker, exactly like
        the manually-triggered Phase-2E path.
        """
        if agent.tenant_id is None:
            raise TaskExecutionError("TENANT_CONTEXT_MISSING", "Agent has no tenant context")
        write_tenant = agent.tenant_id
        report = PlanExecutionReport()

        with tenant_context(write_tenant):
            packages = list(
                await work_package_dao.list_work_packages_for_run(
                    planning_run_id, db=db, limit=_PLAN_TASK_READ_LIMIT
                )
            )

            # 1) Per-package assignment + linked-task load (assignment first,
            # so the linked rows carry the post-assignment agent_id fact).
            plan_tasks: list[Task] = []
            for package in packages:
                if apply_assignment:
                    outcome = await assignment_service.apply_assignment(
                        db,
                        work_package_id=package.id,
                        current_user=current_user,
                    )
                    report.assignment[package.id] = (outcome.state, outcome.code)
                    if outcome.state != "assigned":
                        # Fail-closed: a refused package's tasks stay out of
                        # the execution gate.
                        continue
                plan_tasks.extend(await _linked_tasks_for_package(package.id, db=db))

            if not plan_tasks:
                return report

            # 2) The plan's direct dependency DAG (bounded) + safe order.
            plan_task_ids = [task.id for task in plan_tasks]
            edges = list(
                await task_dependency_dao.list_edges_for(
                    plan_task_ids, db=db, limit=_PLAN_EDGE_READ_LIMIT
                )
            )
            if len(edges) >= _PLAN_EDGE_READ_LIMIT:
                raise TaskExecutionError(
                    "TASK_DEPENDENCY_SET_UNBOUNDED",
                    "plan dependency edges exceed the bounded-read cap; refusing to enqueue",
                )
            ordered_ids = topological_order(plan_task_ids, edges)
            task_by_id = {task.id: task for task in plan_tasks}

            # 3) Gate + enqueue each task in dependency-safe order.  A
            # task already in a settled / in-flight Task state (``done`` /
            # ``doing`` — e.g. a re-invocation after its upstream just
            # settled) is recorded, not re-fed to the gate: the gate treats
            # ``done`` as TASK_TERMINAL and ``doing`` as TASK_ALREADY_RUNNING,
            # both of which are the caller's re-invocation artefacts, not
            # plan defects.
            skipped_settled: list[uuid.UUID] = []
            for task_id in ordered_ids:
                task = task_by_id[task_id]
                if task.status in ("done", "doing"):
                    skipped_settled.append(task.id)
                    continue
                try:
                    outcome = await task_execution_service.execute(
                        db, task=task, agent=agent, current_user=current_user
                    )
                except TaskExecutionError as error:
                    if error.code == "TASK_BLOCKED":
                        report.blocked = report.blocked + (
                            (task.id, tuple(error.unmet_dependencies)),
                        )
                        continue
                    report.rejected[task.id] = error.code
                    continue
                report.enqueued = report.enqueued + (outcome,)
            report.skipped_settled = tuple(skipped_settled)
        return report


plan_execution_service = PlanExecutionService()


__all__ = [
    "PlanExecutionReport",
    "PlanExecutionService",
    "plan_execution_service",
    "topological_order",
]

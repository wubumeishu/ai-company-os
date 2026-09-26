"""Task management API routes.

Phase 2D (docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §6) adds the Task
Graph lane on top of the legacy CRUD routes:

    POST   /agents/{agent_id}/tasks/{task_id}/dependencies
                                                  { "depends_on_task_ids": [...] }
    DELETE /agents/{agent_id}/tasks/{task_id}/dependencies/{dep_task_id}
    GET    /agents/{agent_id}/tasks/{task_id}/graph

These handlers are pure transport adapters: the closed ``GRAPH_*`` codes and
the blocked/ready semantics live in ``task_graph_service``; the tenant scope
lives in the request context (``TenantContextMiddleware``) and the DAO
tenant filter.  The handlers only map an outcome to a status — 404 for
``GRAPH_NOT_FOUND``, 409 for the validation rejections, 200/201 on success.
The ``/graph`` payload embeds the full TaskOut, so every graph read also
carries the Task's Provenance + Status (design §4/§6).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dao import query_dao
from app.dao.base import tenant_context
from app.dao.task_dao import task_provenance_dao
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.task import Task, TaskLog
from app.models.user import User
from app.schemas.schemas import (
    TaskCreate,
    TaskExecuteOut,
    TaskExecutionOut,
    TaskLogCreate,
    TaskLogOut,
    TaskOut,
    TaskRunOut,
    TaskUpdate,
)
from app.schemas.task_graph import TaskDependenciesIn, TaskEdgeOut, TaskGraphOut
from app.services.task_execution_service import TaskExecutionError, task_execution_service
from app.services.task_graph_service import GraphEdgeError, task_graph_service

router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])


async def _enrich_task_out(task: Task, db: AsyncSession) -> TaskOut:
    """Convert Task to TaskOut with creator_username populated."""
    out = TaskOut.model_validate(task)
    if task.created_by:
        user_result = await query_dao.execute(db, select(User).where(User.id == task.created_by))
        user = user_result.scalar_one_or_none()
        if user:
            out.creator_username = user.username
    return out


@router.get("/", response_model=list[TaskOut])
async def list_tasks(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    type_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List tasks for an agent."""
    await check_agent_access(db, current_user, agent_id)
    query = select(Task).where(Task.agent_id == agent_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if type_filter:
        query = query.where(Task.type == type_filter)
    query = query.order_by(Task.created_at.desc())
    result = await query_dao.execute(db, query)
    tasks_list = result.scalars().all()
    # Batch-load creator usernames
    creator_ids = {t.created_by for t in tasks_list if t.created_by}
    creator_map = {}
    if creator_ids:
        users_result = await query_dao.execute(db, select(User).where(User.id.in_(creator_ids)))
        creator_map = {u.id: u.username for u in users_result.scalars().all()}
    out_list = []
    for t in tasks_list:
        t_out = TaskOut.model_validate(t)
        t_out.creator_username = creator_map.get(t.created_by)
        out_list.append(t_out)
    return out_list


@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    agent_id: uuid.UUID,
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new task for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    task = Task(
        agent_id=agent_id,
        title=data.title,
        description=data.description,
        type=data.type,
        priority=data.priority,
        due_date=data.due_date,
        created_by=current_user.id,
        supervision_target_name=data.supervision_target_name,
        supervision_channel=data.supervision_channel,
        remind_schedule=data.remind_schedule,
        # Phase 2D provenance (design §4/§6): ride the Task INSERT.  Omitted
        # (None) fields leave the analysis columns NULL and created_reason
        # defaults to MANUAL — the legacy manual path is byte-identical.
        project_id=data.project_id,
        analysis_run_id=data.analysis_run_id,
        finding_id=data.finding_id,
        revision_sha=data.revision_sha,
        created_reason=data.created_reason or "MANUAL",
    )
    # D2 / Final-Gate gate #2 (design §4.2 "落库前 fail closed"): when the
    # caller touches ANY provenance field, the resulting row must satisfy the
    # §4.2 cross-table integrity rules before it is written.  The validator
    # (``provenance_consistency``) is the shipped, unit-tested owner of those
    # rules; it enforces MANUAL ⇒ all analysis cols NULL, and ANALYSIS_* ⇒
    # a consistent, tenant-aligned run/finding/revision.  On violation: 400,
    # NOTHING is written (fail closed).  The pure-manual path (no provenance
    # fields) is byte-identical to before — the MANUAL branch is in-memory, so
    # no extra query is issued for it.
    if any(
        (
            data.created_reason,
            data.project_id,
            data.analysis_run_id,
            data.finding_id,
            data.revision_sha,
        )
    ):
        consistent = await task_provenance_dao.provenance_consistency(task, db=db)
        if not consistent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PROVENANCE_INCONSISTENT",
                    "message": "provenance fields are not mutually consistent (§4.2); nothing was written",
                },
            )
    query_dao.add(db, task)
    await query_dao.flush(db)

    runtime_handle = None
    blocked_unmet: list[uuid.UUID] = []
    if data.type == "todo":
        from app.services.task_executor import TaskBlockedError, enqueue_task_runtime

        try:
            runtime_handle = await enqueue_task_runtime(
                db,
                task=task,
                agent=agent,
            )
        except TaskBlockedError as blocked:
            # Dependency gate (design §5.4): a todo task whose dependencies are
            # not all done is not enqueued.  A freshly-created task has no
            # edges yet, so this is a defensive fail-closed branch — but it keeps
            # the auto-enqueue call point honest: the task stays pending and no
            # Run is created rather than the POST surfacing a 500.
            blocked_unmet = blocked.reason
            db.add(
                TaskLog(
                    task_id=task.id,
                    content=(
                        "⛔ 依赖未满足，暂不可执行：未 done 前置 = ["
                        + ", ".join(str(x) for x in blocked_unmet)
                        + "]"
                    ),
                )
            )
            await query_dao.flush(db)

    task_out = await _enrich_task_out(task, db)

    # Commit so the background executor can see the task in its own session
    await query_dao.commit(db)

    # Fire background execution for todo tasks — but never for a task the
    # dependency gate blocked (it stays pending for a human to resolve / re-trigger).
    if data.type == "todo" and runtime_handle is None and not blocked_unmet:
        import asyncio
        from app.services.task_executor import execute_task
        asyncio.create_task(execute_task(task.id, agent_id))

    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a task."""
    await check_agent_access(db, current_user, agent_id)
    result = await query_dao.execute(db, select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    updates = data.model_dump(exclude_unset=True)
    # §1.2/U-6 (Phase 2E spec): PATCH may write ``status`` ONLY to
    # ``pending`` (a human reset of a failed/blocked task back to a queueable
    # state).  ``doing``/``done`` are owned by the Runtime completion
    # handler — writing them via PATCH is a 409 rejection (root §十二: no
    # second lifecycle, no human override of terminal state).  Any other
    # value is rejected at the transport as a 400.
    if updates.get("status") is not None:
        if updates["status"] not in ("pending", "doing", "done"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "TASK_STATUS_INVALID", "message": "unknown task status value"},
            )
        if updates["status"] in ("doing", "done"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "TASK_STATUS_WRITE_FORBIDDEN", "message": "status may only be reset to 'pending' via PATCH"},
            )
    # D2 / Final-Gate gate #2 (design §4.2 "落库前 fail closed"): when the patch
    # touches ANY of the five provenance fields, the resulting row must satisfy
    # the §4.2 cross-table integrity rules BEFORE the managed row is mutated.
    # We build the merged values on a DETACHED probe Task (never added to the
    # session) so the validator's internal SELECT cannot trigger an autoflush
    # of the forged UPDATE — a partial patch that breaks an otherwise-consistent
    # ANALYSIS_* row, or that forges a provenance set the run/finding/revision
    # do not back, is refused with 400 and NOTHING is written.  Only on a pass
    # do we apply the changes to the managed row (the single authoritative write).
    _PROVENANCE_FIELDS = ("project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason")
    if any(f in updates for f in _PROVENANCE_FIELDS):
        # ``exclude_unset=True`` means a key is present IFF the patch set it;
        # merge with ``.get()`` so an unset column keeps its managed value.
        probe = Task(
            agent_id=task.agent_id,
            created_by=task.created_by,
            tenant_id=task.tenant_id,
            project_id=updates.get("project_id", task.project_id),
            analysis_run_id=updates.get("analysis_run_id", task.analysis_run_id),
            finding_id=updates.get("finding_id", task.finding_id),
            revision_sha=updates.get("revision_sha", task.revision_sha),
            created_reason=updates.get("created_reason", task.created_reason) or "MANUAL",
        )
        consistent = await task_provenance_dao.provenance_consistency(probe, db=db)
        if not consistent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PROVENANCE_INCONSISTENT",
                    "message": "provenance fields are not mutually consistent (§4.2); nothing was written",
                },
            )
    for field, value in updates.items():
        setattr(task, field, value)
    await query_dao.flush(db)
    return await _enrich_task_out(task, db)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get progress logs for a task."""
    await check_agent_access(db, current_user, agent_id)
    result = await query_dao.execute(db, 
        select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.asc())
    )
    return [TaskLogOut.model_validate(log) for log in result.scalars().all()]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskLogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a progress log entry to a task."""
    await check_agent_access(db, current_user, agent_id)
    log = TaskLog(task_id=task_id, content=data.content)
    query_dao.add(db, log)
    await query_dao.flush(db)
    return TaskLogOut.model_validate(log)


@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a supervision task execution (for testing)."""
    from app.core.permissions import is_agent_expired
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    result = await query_dao.execute(db, select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    # Phase 2E §10.1 (spec §1.2/U-3): a re-trigger of a ``done`` todo Task is
    # rejected at BOTH entry points — the legacy trigger path gains the same
    # terminal guard as Execute (audit §5.3 I-2 hole: today the trigger would
    # re-open done → doing via task_executor.py:127).
    if task.type == "todo" and task.status == "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "TASK_TERMINAL", "message": "Task is already complete (terminal for todo Tasks)"},
        )

    import asyncio
    from app.services.task_executor import execute_task
    asyncio.create_task(execute_task(task.id, agent_id))

    return {"status": "triggered", "task_id": str(task_id)}


# ---------------------------------------------------------------------------
# Task Graph (Phase 2D, design §6) — dependency authoring + bounded graph view.
#
# All policy (the closed GRAPH_* codes, the §5.1 fail-closed validation order,
# the §5.3 derived blocked/ready, the tenant/project guards) lives in
# ``task_graph_service``; these handlers map the outcome to the transport.
# The read writes nothing; the edge writes ride the caller's request
# transaction (``get_db`` commits on clean exit — same as ``create_task``).
# ---------------------------------------------------------------------------


def _graph_edge_http(outcome: Any) -> HTTPException:
    """A blocked ``GRAPH_*`` outcome -> the closed transport mapping.

    Per the service contract (task_graph_service docstring): ``GRAPH_NOT_FOUND``
    is a 404 (the referenced task / edge is absent for this tenant — a not-found
    resource); every other closed code is a 409 validation rejection.  Nothing
    is written on failure.
    """
    code = outcome.code
    if code == "GRAPH_NOT_FOUND":
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code, "message": outcome.detail})
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": outcome.detail})


async def _load_agent_scoped_task(
    db: AsyncSession, agent_id: uuid.UUID, task_id: uuid.UUID, current_user: User
) -> tuple[Task, uuid.UUID]:
    """``check_agent_access`` + the tenant-scoped task load.

    Returns the task AND its narrowed write-tenant (a 403-class refusal when
    either the agent or the task carries no tenant context — a tenant-scoped
    graph edge cannot be authored without one, fail-closed).  A task of
    another tenant / agent is a 404 by construction (the scoped DAO returns
    None, never a cross-tenant disclosure).
    """
    agent, _access = await check_agent_access(db, current_user, agent_id)
    # ``check_agent_access`` already guarantees agent.tenant_id == user.tenant_id
    # (cross-tenant agent -> 403/404).  The graph lane additionally needs a
    # non-None write tenant: a tenant-scoped edge cannot be authored without one.
    if agent.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task has no tenant context")
    with tenant_context(agent.tenant_id):
        task = await task_provenance_dao.get_scoped(task_id, db=db)
    if task is None or task.agent_id != agent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.tenant_id is None:
        # The task row carries no tenant: the graph lane (M9 tenant-scoped
        # writes) cannot operate on it — refuse rather than write unscoped.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task has no tenant context")
    return task, task.tenant_id


@router.post("/{task_id}/dependencies", status_code=status.HTTP_201_CREATED)
async def add_task_dependencies(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskDependenciesIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach direct dependencies to a todo Task (batch of 1 = a single edge).

    The service's §5.1 closed-code validation runs fail-closed before any row
    is written: 404 when an endpoint is absent for this tenant/agent, 409
    for ``GRAPH_SELF`` / ``GRAPH_MISMATCH_TENANT`` / ``GRAPH_MISMATCH_PROJECT``
    / ``GRAPH_SUPERVISION_NOT_ALLOWED`` / ``GRAPH_CYCLE`` / ``GRAPH_EXISTS`` /
    ``GRAPH_INVALID``.  Success writes the edges in the request transaction.
    Attaching dependencies never enqueues or executes anything — execution
    stays on the existing trigger path (Task ≠ Run).
    """
    task, tenant = await _load_agent_scoped_task(db, agent_id, task_id, current_user)
    with tenant_context(tenant):
        outcome = await task_graph_service.bulk_add_edges(
            db,
            task_id=task_id,
            depends_on_task_ids=data.depends_on_task_ids,
            tenant_id=tenant,
        )
    if outcome.state != "added":
        raise _graph_edge_http(outcome)
    return {"task_id": task_id, "added": [str(dep) for dep in data.depends_on_task_ids], "state": outcome.state}


@router.delete("/{task_id}/dependencies/{dep_task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_task_dependency(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    dep_task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one direct dependency edge (404 when the edge is absent).

    Deleting an edge can unblock the task; the task's READY is re-derived on
    its next execution-gate check (design §5.3/§5.4) — this endpoint never
    enqueues or executes anything itself.
    """
    task, tenant = await _load_agent_scoped_task(db, agent_id, task_id, current_user)
    with tenant_context(tenant):
        outcome = await task_graph_service.remove_edge(
            db, task_id=task_id, depends_on_task_id=dep_task_id, tenant_id=tenant
        )
    if outcome.state != "removed":
        # The URL-named edge is the subject: absent for this tenant -> 404
        # (GRAPH_NOT_FOUND), mirroring the documented service contract.
        raise _graph_edge_http(outcome)


@router.get("/{task_id}/graph", response_model=TaskGraphOut)
async def get_task_graph(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The bounded per-task graph view + Provenance + Status (design §6).

    ``ready`` is the §5.3 derived state (ready / blocked / not_applicable);
    ``task`` is the full TaskOut, so the caller sees the Task's status AND
    its provenance (``project_id`` / ``analysis_run_id`` / ``finding_id`` /
    ``revision_sha`` / ``created_reason``) in one read.  A read-only
    endpoint — it performs no writes and no execution.
    """
    task, tenant = await _load_agent_scoped_task(db, agent_id, task_id, current_user)
    try:
        with tenant_context(tenant):
            view = await task_graph_service.graph(db, task_id=task_id, tenant_id=tenant)
    except GraphEdgeError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found") from None
    return TaskGraphOut(
        task=await _enrich_task_out(task, db),
        ready=view.state,
        direct_dependencies=[TaskEdgeOut(id=e["id"], status=e["status"]) for e in view.direct_dependencies],
        blocking=list(view.blocking),
    )


# ---------------------------------------------------------------------------
# Task Execution (Phase 2E, spec §10) — Execute + execution query.
#
# Both handlers are pure transport adapters (backend/AGENTS.md boundary
# rule): parse → ``check_agent_access`` → call the owning service
# (``task_execution_service``) → map the outcome.  The closed ``§6.3``
# codes, the P1–P8 gate, the R1–R5 attempt keying, and the audit writes
# all live in the service; nothing here touches ORM rows.
# ---------------------------------------------------------------------------

# §6.3 → HTTP mapping (spec §4 "HTTP mapping in §7.2"): the 409 class for
# every closed gate code that is NOT a not-found resource; a 404 for the
# not-found resources themselves.
_NOT_FOUND_CODES = frozenset({"TASK_NOT_FOUND", "PROJECT_NOT_FOUND", "AGENT_NOT_FOUND"})


def _execute_http(exc: TaskExecutionError) -> HTTPException:
    """Map a closed gate code to its transport response (fail-closed)."""
    body: dict[str, Any] = {"code": exc.code, "message": exc.detail}
    if exc.unmet_dependencies:
        body["unmet_dependencies"] = [str(t) for t in exc.unmet_dependencies]
    if exc.active_run_id is not None:
        body["active_run_id"] = str(exc.active_run_id)
    if exc.code in _NOT_FOUND_CODES:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=body)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=body)


@router.post("/{task_id}/execute", response_model=TaskExecuteOut)
async def execute_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a ready todo Task (assign + trigger in one, spec §10.1).

    The binding is the ``Task.agent_id`` FK (spec §1.1 — no agent id in the
    body).  On 200 the response carries ``created`` (R1: a double-click
    reuses the in-flight Run with ``created=false``), the Run identity, and
    the §6.2 ``derived_state``.  Rejections are 404 (not-found resource) /
    409 (the closed gate code set, §6.3) — on ANY gate failure NOTHING is
    enqueued (fail-closed, spec §4).  Writes ride the request transaction
    (``get_db`` commits on clean exit).
    """
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if agent.tenant_id is None:
        # A tenant-scoped execute cannot run without a write tenant
        # (spec §4 P1 / §9) — refuse rather than write unscoped.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent has no tenant context")
    task = await _agent_task_for_execution(db, agent_id, task_id)
    try:
        outcome = await task_execution_service.execute(
            db, task=task, agent=agent, current_user=current_user
        )
    except TaskExecutionError as exc:
        raise _execute_http(exc) from exc
    return TaskExecuteOut(
        task_id=outcome.task_id,
        created=outcome.created,
        run_id=outcome.run_id,
        source_execution_id=outcome.source_execution_id,
        attempt_id=outcome.attempt_id,
        derived_state=outcome.derived_state,
    )


@router.get("/{task_id}/execution", response_model=TaskExecutionOut)
async def query_task_execution(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The task's execution projection (spec §10.2).

    ``derived_state`` is the §6.2 projection (computed, never stored);
    ``runs`` is the task's bounded Run registry list (stable first attempt
    + ordered retry attempts), each with its attempt label, settlement
    state, and the latest TaskLog line as ``result_summary`` (root §十五:
    reuse TaskLog — no new Artifact system).  A read-only endpoint.
    """
    agent, _access = await check_agent_access(db, current_user, agent_id)
    task = await _agent_task_for_execution(db, agent_id, task_id)
    view = await task_execution_service.query_execution(
        db, task=task, agent=agent, current_user=current_user
    )
    return TaskExecutionOut(
        task=await _enrich_task_out(task, db),
        derived_state=view.derived_state,
        active_run_id=view.active_run_id,
        runs=[
            TaskRunOut(
                run_id=r.run_id,
                source_execution_id=r.source_execution_id,
                attempt=r.attempt,
                started_at=r.started_at,
                settled_state=r.settled_state,
                result_summary=r.result_summary,
            )
            for r in view.runs
        ],
    )


async def _agent_task_for_execution(db: AsyncSession, agent_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    """The transport's task load: a task of another agent/tenant → 404."""
    result = await query_dao.execute(db, select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task

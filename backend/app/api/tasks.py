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
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate
from app.schemas.task_graph import TaskDependenciesIn, TaskEdgeOut, TaskGraphOut
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

    for field, value in data.model_dump(exclude_unset=True).items():
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
        raise HTTPException(status_code=404, detail="Task not found")

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

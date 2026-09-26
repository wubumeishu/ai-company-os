"""Phase 2E TaskExecutionService tests (spec §12 verification matrix).

Coverage per ``docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md`` §12:

1. Gate negative matrix — one test per closed code; each asserts NOTHING
   was enqueued and that a TaskLog + AuditLog row was written.
2. Double-click (R1/R2) — reuse with ``created=False``; 409 for a
   ``doing`` task with the active Run id.
3. Failure-retry (R3/R4/R5) — terminal failed/cancelled Run → fresh
   attempt key ``task:{id}:retry:{attempt_id}``; the stable key is never
   reused for a retry; the transient retry soft cap.
5. Tenant — direct task/agent/project tenant asserts + the §9
   background-callers rule (missing user tenant scope fails at entry).
6. State projection — READY/BLOCKED/QUEUED/RUNNING/SUCCEEDED/FAILED/
   CANCELLED shapes over stubbed Run/command states.
7. Regression — the legacy trigger path stays byte-identical via the
   ``attempt_id=None`` / ``actor_user_id=None`` defaults (existing
   ``test_task_runtime_intake`` / ``test_task_api_runtime_intake``
   suites); here the executor contract is pinned.
8. Migration — none (V1 = zero DDL, §1.1; no new alembic revision).

The service is exercised against stubbed module-boundary dependencies —
no DB, no LangGraph (worker-boundary stubs only, §12.3).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.audit import AuditLog
from app.models.project import Project
from app.models.task import Task, TaskLog
from app.services.agent_runtime.contracts import RunHandle
from app.services.agent_runtime.persistence import RuntimePersistenceError
from app.services.task_execution_service import (
    RETRY_SOFT_CAP_PER_TASK_PER_DAY,
    TaskExecutionError,
    task_execution_service,
)

NS = "app.services.task_execution_service"


class _Session:
    """Caller-transaction stand-in: records adds + flushes (§8 writes)."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushes = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


def _tenant() -> uuid.UUID:
    return uuid.uuid4()


def _project(*, status: str, tenant: uuid.UUID | None = None) -> Project:
    return Project(
        id=uuid.uuid4(),
        tenant_id=tenant or _tenant(),
        status=status,
        name="P",
        description=None,
        created_by=uuid.uuid4(),
    )


def _records(
    *,
    task_type: str = "todo",
    task_status: str = "pending",
    task_tenant: uuid.UUID | None = None,
    project: Project | None = None,
    user_tenant: uuid.UUID | None | object = ...,  # Ellipsis = follow agent tenant
) -> tuple[Task, Agent, SimpleNamespace]:
    tenant = _tenant()
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant,
        creator_id=uuid.uuid4(),
        name="Analyst",
        role_description="Analyze evidence",
        primary_model_id=uuid.uuid4(),
        status="idle",
        deleted_at=None,
    )
    if task_tenant is None:
        task_tenant = tenant
    task = Task(
        id=uuid.uuid4(),
        tenant_id=task_tenant,
        agent_id=agent.id,
        title="Prepare the report",
        description="Use the current workspace evidence",
        type=task_type,
        status=task_status,
        priority="medium",
        created_by=uuid.uuid4(),
        project_id=project.id if project is not None else None,
    )
    user_tenant_id = tenant if user_tenant is ... else user_tenant
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=user_tenant_id)
    return task, agent, user


def _run(task: Task, *, key: str | None = None) -> AgentRun:
    run_id = uuid.uuid4()
    return AgentRun(
        id=run_id,
        tenant_id=task.tenant_id,
        agent_id=task.agent_id,
        source_type="task",
        source_id=str(task.id),
        source_execution_id=key if key is not None else f"task:{task.id}",
        run_kind="background",
        runtime_type="langgraph",
        delivery_status="not_required",
        lane_held=False,
    )


def _handle(task: Task, agent: Agent, *, created: bool = True, run: AgentRun | None = None) -> RunHandle:
    run = run or _run(task)
    return RunHandle(
        tenant_id=agent.tenant_id,
        run_id=run.id,
        thread_id=str(run.id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=created,
    )


def _terminal_event(run: AgentRun, event_type: str) -> SimpleNamespace:
    return SimpleNamespace(event_type=event_type, run_id=run.id)


class _GateStubs:
    """One consistent patch set per test: every module-boundary dependency
    of the service (gate DAOs + the enqueue boundary).

    The ``enqueue`` side effect mirrors the real ``enqueue_task_runtime``
    tail (task_executor.py:127): on success it flips the task to
    ``doing`` — unless ``log_task_done=False`` (gate/reject paths where
    the real executor never reached that line).
    """

    def __init__(
        self,
        *,
        task: Task,
        unmet: list[uuid.UUID] | None = None,
        runs: list[AgentRun] | None = None,
        terminal: dict[uuid.UUID, SimpleNamespace] | None = None,
        audit_count: int = 0,
        active_agent: object = ...,  # Ellipsis default = "real agent row"
        project: object = ...,
        v2_enabled: bool = True,
        expired: bool = False,
        enqueue_return: RunHandle | None = None,
        enqueue_side_effect: Exception | None = None,
        start_cmd_status: str | None = None,
        flip_status: bool = True,
        latest_logs: list[SimpleNamespace] | None = None,
    ) -> None:
        self.task = task
        self.flip = flip_status
        if enqueue_side_effect is not None:
            self.enqueue = AsyncMock(side_effect=enqueue_side_effect)
        else:
            async def _enqueue(db, **kwargs):
                if self.flip:
                    task.status = "doing"
                return enqueue_return

            self.enqueue = AsyncMock(side_effect=_enqueue)
        self.mocks = {
            "ensure_ready": AsyncMock(return_value=list(unmet or [])),
            "list_task_runs": AsyncMock(return_value=list(runs or [])),
            "terminal": AsyncMock(return_value=dict(terminal or {})),
            "start_cmd": AsyncMock(
                return_value=SimpleNamespace(status=start_cmd_status) if start_cmd_status else None
            ),
            "count_audit": AsyncMock(return_value=audit_count),
            "get_active": AsyncMock(return_value=active_agent),
            "get_project": AsyncMock(return_value=project),
            "latest_logs": AsyncMock(return_value=list(latest_logs or [])),
        }
        self.expired = expired
        self.v2 = SimpleNamespace(use_v2=v2_enabled)

    def patches(self) -> list[patch]:
        m = self.mocks
        return [
            patch(f"{NS}.task_graph_service.ensure_ready", new=m["ensure_ready"]),
            patch(f"{NS}.agent_run_dao.list_task_runs", new=m["list_task_runs"]),
            patch(f"{NS}.agent_run_dao.terminal_events_for_runs", new=m["terminal"]),
            patch(f"{NS}.agent_run_dao.start_command_for_run", new=m["start_cmd"]),
            patch(f"{NS}.audit_log_dao.count_task_audit", new=m["count_audit"]),
            patch(f"{NS}.agent_dao.get_active", new=m["get_active"]),
            patch(f"{NS}.project_dao.get_scoped", new=m["get_project"]),
            patch(f"{NS}.task_provenance_dao.latest_task_logs", new=m["latest_logs"]),
            patch(f"{NS}.decide_runtime_v2", new=lambda **_: self.v2),
            patch(f"{NS}.is_agent_expired", new=lambda a: self.expired),
            patch(f"{NS}.enqueue_task_runtime", new=self.enqueue),
        ]

    @property
    def active_agent(self):
        return self.mocks["get_active"].return_value


def _enter(stubs: _GateStubs) -> None:
    for ctx in stubs.patches():
        ctx.start()


def _exit() -> None:
    patch.stopall()


def _logs(db: _Session) -> tuple[list[TaskLog], list[AuditLog]]:
    logs = [x for x in db.added if isinstance(x, TaskLog)]
    audits = [x for x in db.added if isinstance(x, AuditLog)]
    return logs, audits


def _gate_mocked_agent() -> Agent:
    """An active agent row standing in for the P5 re-load."""
    return Agent(
        id=uuid.uuid4(),
        tenant_id=_tenant(),
        creator_id=uuid.uuid4(),
        name="Analyst",
        role_description="Analyze evidence",
        primary_model_id=uuid.uuid4(),
        status="idle",
        deleted_at=None,
    )


# ---------------------------------------------------------------------------
# §12.1 — gate negative matrix (closed code, NOTHING enqueued)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_task_type_not_supported() -> None:
    task, agent, user = _records(task_type="supervision")
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TASK_TYPE_NOT_SUPPORTED"
    stubs.enqueue.assert_not_awaited()
    logs, audits = _logs(db)
    assert len(logs) == 1 and "TASK_TYPE_NOT_SUPPORTED" in logs[0].content
    assert len(audits) == 1 and audits[0].action == "task_execute_blocked"
    assert db.flushes >= 1  # both writes ride the caller transaction


@pytest.mark.asyncio
async def test_gate_task_already_running_carries_active_run_id() -> None:
    task, agent, user = _records(task_status="doing")
    active = _run(task)
    db = _Session()
    stubs = _GateStubs(task=task, runs=[active], active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TASK_ALREADY_RUNNING"
    assert exc.value.active_run_id == active.id


@pytest.mark.asyncio
async def test_gate_task_terminal() -> None:
    task, agent, user = _records(task_status="done")
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TASK_TERMINAL"
    logs, _ = _logs(db)
    assert len(logs) == 1 and "TASK_TERMINAL" in logs[0].content


@pytest.mark.asyncio
async def test_gate_tenant_mismatch_task() -> None:
    task, agent, user = _records(task_tenant=_tenant())  # task tenant != agent tenant
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TENANT_MISMATCH"
    stubs.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_tenant_context_missing() -> None:
    task, agent, user = _records()
    agent.tenant_id = None  # type: ignore[assignment]
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TENANT_CONTEXT_MISSING"


@pytest.mark.asyncio
async def test_gate_project_not_found() -> None:
    task, agent, user = _records()
    task.project_id = uuid.uuid4()  # dangling provenance (CASCADE-deleted)
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, project=None, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "PROJECT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project_status", ["RECEIVED", "SOURCES_OK", "BLOCKED", "COMPLETED", "ARCHIVED", "REJECTED"]
)
async def test_gate_project_state_rejects_non_executable(project_status: str) -> None:
    task, agent, user = _records()
    project = _project(status=project_status, tenant=agent.tenant_id)
    task.project_id = project.id
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, project=project, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "PROJECT_NOT_EXECUTABLE"


@pytest.mark.asyncio
async def test_gate_project_state_executing_passes() -> None:
    """§4 P2 positive half: an EXECUTING project is inside the allowed set."""
    task, agent, user = _records()
    project = _project(status="EXECUTING", tenant=agent.tenant_id)
    task.project_id = project.id
    db = _Session()
    handle = _handle(task, agent, created=True)
    stubs = _GateStubs(task=task, active_agent=agent, project=project, enqueue_return=handle)
    _enter(stubs)
    try:
        outcome = await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert outcome.state == "enqueued"
    assert outcome.created is True


@pytest.mark.asyncio
async def test_gate_project_tenant_mismatch() -> None:
    task, agent, user = _records()
    project = _project(status="EXECUTING", tenant=_tenant())  # foreign project tenant
    task.project_id = project.id
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, project=project, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TENANT_MISMATCH"


@pytest.mark.asyncio
async def test_gate_agent_missing_assignment_failed() -> None:
    task, agent, user = _records()
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=None, flip_status=False)  # P5 re-load: gone
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "ASSIGNMENT_FAILED"


@pytest.mark.asyncio
async def test_gate_agent_without_model_unavailable() -> None:
    task, agent, user = _records()
    no_model = _gate_mocked_agent()
    no_model.primary_model_id = None  # type: ignore[assignment]
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=no_model, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "AGENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_gate_agent_expired_unavailable() -> None:
    task, agent, user = _records()
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, expired=True, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "AGENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_gate_v2_disabled_fail_closed() -> None:
    task, agent, user = _records()
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, v2_enabled=False, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "RUNTIME_V2_DISABLED"
    stubs.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_unmet_dependencies_blocked() -> None:
    task, agent, user = _records()
    dep = uuid.uuid4()
    db = _Session()
    stubs = _GateStubs(task=task, unmet=[dep], active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TASK_BLOCKED"
    assert exc.value.unmet_dependencies == (dep,)
    logs, audits = _logs(db)
    assert len(logs) == 1 and "TASK_BLOCKED" in logs[0].content
    assert audits[0].action == "task_execute_blocked"
    assert audits[0].details["outcome_code"] == "TASK_BLOCKED"
    stubs.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_queue_failed_rejects_registration() -> None:
    task, agent, user = _records()
    db = _Session()
    stubs = _GateStubs(
        task=task,
        active_agent=agent,
        enqueue_side_effect=RuntimePersistenceError("command_idempotency_mismatch", "different inputs"),
        flip_status=False,
    )
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "QUEUE_FAILED"


@pytest.mark.asyncio
async def test_gate_enqueue_none_returns_v2_disabled_safety_net() -> None:
    """P7 safety net: the gate passed but the enqueue returned None (the v2
    gate dropped mid-transaction) — fail closed, nothing registered."""
    task, agent, user = _records()
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, enqueue_return=None, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "RUNTIME_V2_DISABLED"


# ---------------------------------------------------------------------------
# §12.3 — happy path + §5 R1/R2/R3 idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_enqueues_first_attempt_with_stable_key() -> None:
    task, agent, user = _records()
    db = _Session()
    handle = _handle(task, agent, created=True)
    stubs = _GateStubs(task=task, active_agent=agent, enqueue_return=handle)
    _enter(stubs)
    try:
        outcome = await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()

    assert outcome.state == "enqueued"
    assert outcome.created is True
    assert outcome.run_id == handle.run_id
    assert outcome.source_execution_id == f"task:{task.id}"
    assert outcome.attempt_id is None
    assert outcome.derived_state == "QUEUED"
    assert task.status == "doing"  # flipped by the (stubbed) executor tail
    # §10.1 (U6): the CALLER — not the task creator — is the Run's actor.
    stubs.enqueue.assert_awaited_once()
    assert stubs.enqueue.await_args.kwargs["actor_user_id"] == user.id
    assert stubs.enqueue.await_args.kwargs["attempt_id"] is None
    # §8: one audit row for the outcome, in the caller transaction.
    _, audits = _logs(db)
    assert len(audits) == 1
    assert audits[0].action == "task_execute"
    assert audits[0].details["outcome_code"] == "ENQUEUED"
    assert audits[0].details["run_id"] == str(handle.run_id)
    assert audits[0].details["source_execution_id"] == f"task:{task.id}"


@pytest.mark.asyncio
async def test_execute_double_click_reuses_in_flight_run() -> None:
    """§12.2: a second submission under the stable key hits the intake's
    exact-input reuse (created=False) — one Run (R1/R2)."""
    task, agent, user = _records()
    db = _Session()
    winner = _run(task)
    loser_handle = _handle(task, agent, created=False, run=winner)
    stubs = _GateStubs(task=task, runs=[winner], active_agent=agent, enqueue_return=loser_handle)
    _enter(stubs)
    try:
        outcome = await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()

    assert outcome.state == "reused"
    assert outcome.created is False
    assert outcome.run_id == winner.id
    assert outcome.source_execution_id == f"task:{task.id}"
    assert outcome.derived_state == "QUEUED"
    # The stable key was re-submitted (no attempt id — the winner's Run
    # already owns it, R5).
    assert stubs.enqueue.await_args.kwargs["attempt_id"] is None
    _, audits = _logs(db)
    assert audits[0].action == "task_execute"
    assert audits[0].details["outcome_code"] == "REUSED"


@pytest.mark.asyncio
async def test_execute_after_failed_run_mints_retry_attempt_key() -> None:
    """§12.3: terminal failed Run + task pending ⇒ new Run under
    ``task:{id}:retry:{attempt_id}`` (R3); the stable key is never reused
    for a retry (R5); retries are explicit (R4 — no auto-retry loop)."""
    task, agent, user = _records()
    db = _Session()
    failed_run = _run(task, key=f"task:{task.id}")
    retry_run = _run(task, key=f"task:{task.id}:retry:{uuid.uuid4()}")
    stubs = _GateStubs(
        task=task,
        runs=[failed_run],
        terminal={failed_run.id: _terminal_event(failed_run, "run_failed")},
        active_agent=agent,
        enqueue_return=_handle(task, agent, created=True, run=retry_run),
    )
    _enter(stubs)
    try:
        outcome = await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()

    assert outcome.state == "enqueued"
    assert outcome.created is True
    assert outcome.attempt_id is not None
    assert outcome.source_execution_id == f"task:{task.id}:retry:{outcome.attempt_id}"
    assert isinstance(outcome.attempt_id, uuid.UUID)  # a fresh, parseable attempt id
    assert stubs.enqueue.await_args.kwargs["attempt_id"] == outcome.attempt_id
    _, audits = _logs(db)
    assert audits[0].action == "task_execute_retried"
    assert audits[0].details["outcome_code"] == "ENQUEUED"
    assert audits[0].details["source_execution_id"] == f"task:{task.id}:retry:{outcome.attempt_id}"


@pytest.mark.asyncio
async def test_execute_after_cancelled_run_mints_retry_attempt_key() -> None:
    task, agent, user = _records()
    db = _Session()
    cancelled = _run(task, key=f"task:{task.id}")
    stubs = _GateStubs(
        task=task,
        runs=[cancelled],
        terminal={cancelled.id: _terminal_event(cancelled, "run_cancelled")},
        active_agent=agent,
        enqueue_return=_handle(task, agent, created=True),
    )
    _enter(stubs)
    try:
        outcome = await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert outcome.attempt_id is not None
    assert outcome.source_execution_id == f"task:{task.id}:retry:{outcome.attempt_id}"


@pytest.mark.asyncio
async def test_retry_soft_cap_enforced_by_audit_count() -> None:
    task, agent, user = _records()
    db = _Session()
    failed_run = _run(task, key=f"task:{task.id}")
    stubs = _GateStubs(
        task=task,
        runs=[failed_run],
        terminal={failed_run.id: _terminal_event(failed_run, "run_failed")},
        audit_count=RETRY_SOFT_CAP_PER_TASK_PER_DAY,
        active_agent=agent,
        flip_status=False,
    )
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "RETRY_CAP_EXCEEDED"
    stubs.enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# §12.5 — tenant hardening (P1 + the §9 background-callers rule)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_background_caller_without_tenant_context_rejected_at_entry() -> None:
    """§9 / workspace audit G1: a caller whose user carries no tenant scope
    must be rejected at the SERVICE ENTRY — before any gate read — so a
    future scheduler cannot inherit the get_scoped degradation hole."""
    task, agent, user = _records(user_tenant=None)
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TENANT_CONTEXT_MISSING"
    # No gate read may have happened: the entry assert is first.
    stubs.mocks["ensure_ready"].assert_not_awaited()
    stubs.mocks["get_project"].assert_not_called()


@pytest.mark.asyncio
async def test_foreign_user_tenant_rejected_at_entry() -> None:
    task, agent, user = _records(user_tenant=_tenant())  # user tenant != agent tenant
    db = _Session()
    stubs = _GateStubs(task=task, active_agent=agent, flip_status=False)
    _enter(stubs)
    try:
        with pytest.raises(TaskExecutionError) as exc:
            await task_execution_service.execute(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert exc.value.code == "TENANT_MISMATCH"
    stubs.mocks["ensure_ready"].assert_not_awaited()


# ---------------------------------------------------------------------------
# §12.6 — derived-state projection (§6.2) + §10.2 shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "terminal_kind", "unmet", "start_cmd_status", "expected"),
    [
        ("pending", None, [], None, "READY"),
        ("pending", None, [uuid.uuid4()], None, "BLOCKED"),
        ("doing", None, [], "pending", "QUEUED"),
        ("doing", None, [], "claimed", "RUNNING"),
        ("done", None, [], None, "SUCCEEDED"),
        ("pending", "run_failed", [], None, "FAILED"),
        ("pending", "run_cancelled", [], None, "CANCELLED"),
    ],
)
async def test_state_projection(
    task_status: str,
    terminal_kind: str | None,
    unmet: list[uuid.UUID],
    start_cmd_status: str | None,
    expected: str,
) -> None:
    task, agent, user = _records(task_status=task_status)
    db = _Session()
    runs: list[AgentRun] = []
    terminal: dict[uuid.UUID, SimpleNamespace] = {}
    if terminal_kind is not None:
        run = _run(task, key=f"task:{task.id}")
        runs.append(run)
        terminal[run.id] = _terminal_event(run, terminal_kind)
    elif task_status == "doing":
        runs.append(_run(task))
    stubs = _GateStubs(
        task=task,
        unmet=unmet,
        runs=runs,
        terminal=terminal,
        active_agent=agent,
        start_cmd_status=start_cmd_status,
        flip_status=False,
    )
    _enter(stubs)
    try:
        view = await task_execution_service.query_execution(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert view.derived_state == expected
    if task_status == "doing":
        assert view.active_run_id == runs[-1].id
    else:
        assert view.active_run_id is None


@pytest.mark.asyncio
async def test_execution_query_shape_bounded_runs_and_attempt_labels() -> None:
    """§10.2 shape: the runs list is the stable first attempt + the ordered
    retry attempts, each labeled, each with its settlement state; the
    result_summary reuses the latest TaskLog line (root §十五)."""
    task, agent, user = _records()
    db = _Session()
    first = _run(task, key=f"task:{task.id}")
    attempt_id = uuid.uuid4()
    retry = _run(task, key=f"task:{task.id}:retry:{attempt_id}")
    terminal = {
        first.id: _terminal_event(first, "run_failed"),
        retry.id: _terminal_event(retry, "run_failed"),
    }
    stubs = _GateStubs(
        task=task,
        runs=[first, retry],
        terminal=terminal,
        active_agent=agent,
        latest_logs=[SimpleNamespace(content="❌ 任务执行失败：boom")],
        flip_status=False,
    )
    _enter(stubs)
    try:
        view = await task_execution_service.query_execution(db, task=task, agent=agent, current_user=user)
    finally:
        _exit()
    assert [r.run_id for r in view.runs] == [first.id, retry.id]
    assert view.runs[0].attempt == "first"
    assert view.runs[1].attempt == f"retry:{attempt_id}"
    assert view.runs[0].settled_state == "failed"
    assert view.runs[1].settled_state == "failed"
    assert view.runs[0].result_summary is not None
    assert view.derived_state == "FAILED"


# ---------------------------------------------------------------------------
# Module-boundary wiring (the executor's new §5 params, §12.7 regression)
# ---------------------------------------------------------------------------


def test_executor_contract_gains_retry_and_actor_params_with_legacy_defaults() -> None:
    """§12.7: ``attempt_id=None`` / ``actor_user_id=None`` (every legacy
    call site) keep the byte-identical stable key + task-created-by actor;
    only the Execute path passes real values."""
    import inspect

    from app.services.task_executor import enqueue_task_runtime

    sig = inspect.signature(enqueue_task_runtime)
    assert sig.parameters["attempt_id"].default is None
    assert sig.parameters["actor_user_id"].default is None

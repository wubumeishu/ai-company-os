"""Phase 2E Task execution service — the Task→Run bridge (V1).

Owning service per ``docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md`` (§4–§10).
It is called ONLY by the Execute/execution transport (``app/api/tasks.py``):

* §4 fail-closed precondition gate P1–P8 (fixed order, cheapest → most
  expensive): first failure wins, the closed code is returned, and NOTHING
  is enqueued.  Every gate failure writes a TaskLog (⛔ with the closed
  code) AND an AuditLog row (§8) — the blocked outcome is an event, not a
  silent no-op.
* §5 idempotency R1–R5: the stable key ``task:{id}`` is reserved for the
  FIRST attempt; an explicit human retry after a terminal
  failed/cancelled Run registers a NEW Run under
  ``task:{id}:retry:{attempt_id}`` (R3, the attempt id is minted here,
  never reused); a second Execute while the Task is ``doing`` reuses the
  in-flight Run (``created=False``, R1/R2); retries are NEVER automatic
  (R4); the §6.3 transient soft cap (3 retried attempts per task per UTC
  day) is enforced here via the audit COUNT — never by queue machinery
  (G2: no queueing facility exists or is invented).
* §6 derived-state projection: READY/BLOCKED/QUEUED/RUNNING/SUCCEEDED/
  FAILED/CANCELLED are COMPUTED from the Task row + command row + latest
  terminal Run event — no second lifecycle, no new column, no event.
* §8 one AuditLog row per Execute outcome, written in the caller
  transaction (the transport commits it).  Audit write failure is
  narrow-caught + logged and never swallows the primary outcome
  (materialization precedent).
* §9 tenant hardening: a non-HTTP caller MUST hold the tenant context; the
  service re-asserts ``verify_tenant_scope(agent.tenant_id,
  current_user.tenant_id)`` at entry, so an unscoped or foreign context
  fails closed at the service boundary (workspace audit G1, root §二十一).

Everything from ``enqueue_task_runtime`` downward (intake → command queue →
LangGraph → settlement) is EXISTING and untouched; this service only
provides the validated input payload (root §三 boundary, spec §2).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_agent_expired
from app.dao import agent_dao, agent_run_dao, audit_log_dao
from app.dao.base import tenant_context
from app.dao.project_intake_dao import project_dao
from app.dao.task_dao import task_provenance_dao
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.task import Task, TaskLog
from app.models.user import User
from app.services.agent_runtime.config import decide_runtime_v2
from app.services.agent_runtime.contracts import RunHandle
from app.services.agent_runtime.persistence import RuntimePersistenceError
from app.services.intake_security import TenantScopeViolation, verify_tenant_scope
from app.services.task_executor import TaskBlockedError, enqueue_task_runtime
from app.services.task_graph_service import task_graph_service

# §4 P2 (U-1 ruling): the Project execution-allowed set is a NAMED constant
# — 2E depends on the set, it does not own the Project state machine.
PROJECT_EXECUTABLE_STATUSES: frozenset[str] = frozenset(
    {"ANALYZING", "PENDING_CONFIRMATION", "EXECUTING"}
)

# §6.3 soft retry cap (WORKSPACE_CONFLICT / QUEUE_FAILED transient class):
# at most this many human-initiated retried attempts per task per UTC day.
RETRY_SOFT_CAP_PER_TASK_PER_DAY = 3

_RETRY_KEY_PATTERN = re.compile(
    r"^task:(?P<task_id>[0-9a-f-]{36}):retry:(?P<attempt_id>[0-9a-f-]{36})$"
)


class TaskExecutionError(Exception):
    """A fail-closed gate rejection (spec §6.3 closed code set)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.unmet_dependencies: tuple[uuid.UUID, ...] = ()
        self.active_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class TaskExecutionOutcome:
    """Result of one Execute call (§10.1 response shape)."""

    task_id: uuid.UUID
    state: str  # "enqueued" | "reused" | "blocked"
    run_id: uuid.UUID | None = None
    created: bool = False
    attempt_id: uuid.UUID | None = None
    source_execution_id: str | None = None
    derived_state: str | None = None
    active_run_id: uuid.UUID | None = None
    unmet_dependencies: tuple[uuid.UUID, ...] = ()
    code: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRunProjection:
    """One entry of the §10.2 ``runs`` list (bounded per Task, root §2)."""

    run_id: uuid.UUID
    source_execution_id: str
    attempt: str  # "first" | "retry:<attempt_id>"
    started_at: datetime
    settled_state: str | None  # "completed" | "failed" | "cancelled" | None
    result_summary: str | None


@dataclass(frozen=True, slots=True)
class TaskExecutionQuery:
    """§10.2 execution projection — computed, never stored (root §十二)."""

    derived_state: str
    active_run_id: uuid.UUID | None
    unmet_dependencies: tuple[uuid.UUID, ...]
    runs: tuple[TaskRunProjection, ...]


def _stable_key(task: Task) -> str:
    return f"task:{task.id}"


def _run_attempt_label(source_execution_id: str) -> str:
    """§10.2: label which attempt produced the Run (root §十五)."""
    match = _RETRY_KEY_PATTERN.match(source_execution_id or "")
    return f"retry:{match.group('attempt_id')}" if match else "first"


class TaskExecutionService:
    """Owns the §4 gate, §5 attempt keying, §8 audit, and §6 projection."""

    # ------------------------------------------------------------------
    # Execute (§10.1)
    # ------------------------------------------------------------------

    async def execute(
        self,
        db: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        current_user: User,
    ) -> TaskExecutionOutcome:
        """Gate + keying + enqueue, all in the caller transaction.

        §9 background-callers rule: this method reads tenant-scoped DAOs,
        so a non-HTTP caller MUST hold the tenant context.  Entry
        re-asserts the scope — a missing or foreign context is rejected
        HERE (before any gate or enqueue), not at query time.
        """
        if agent.tenant_id is None:
            raise TaskExecutionError("TENANT_CONTEXT_MISSING", "Agent has no tenant context")
        caller_tenant = getattr(current_user, "tenant_id", None)
        if caller_tenant is None:
            # §9: a caller with NO tenant scope cannot address a tenant-scoped
            # Run — fail closed at the entry (the get_scoped degradation
            # must not be inherited by a future background caller).
            raise TaskExecutionError(
                "TENANT_CONTEXT_MISSING", "Calling user has no tenant context"
            )
        try:
            verify_tenant_scope(agent.tenant_id, caller_tenant)
        except TenantScopeViolation as exc:
            raise TaskExecutionError(
                "TENANT_MISMATCH", "Task/Agent tenant does not match the calling user's tenant"
            ) from exc
        write_tenant = agent.tenant_id

        with tenant_context(write_tenant):
            unmet = await task_graph_service.ensure_ready(db, task=task, tenant_id=write_tenant)
            runs = await agent_run_dao.list_task_runs(task.id, db=db)
            terminal = await agent_run_dao.terminal_events_for_runs([r.id for r in runs], db=db)

            gate_failure = await self._gate(db, task=task, agent=agent, unmet=unmet)
            if gate_failure is not None:
                code, message = gate_failure
                error = TaskExecutionError(code, message)
                if code == "TASK_BLOCKED":
                    error.unmet_dependencies = tuple(unmet)
                if code == "TASK_ALREADY_RUNNING":
                    error.active_run_id = self._in_flight_run_id(runs, terminal)
                await self._record_blocked(db, task, agent, current_user, write_tenant, error)
                raise error

            attempt = self._new_attempt_id(task, runs, terminal)
            if attempt is not None:
                cap = await audit_log_dao.count_task_audit(
                    task.id,
                    action="task_execute_retried",
                    since=self._utc_day_start(),
                    db=db,
                )
                if cap >= RETRY_SOFT_CAP_PER_TASK_PER_DAY:
                    error = TaskExecutionError(
                        "RETRY_CAP_EXCEEDED",
                        f"transient retry soft cap reached ({cap} retried attempts today)",
                    )
                    await self._record_blocked(db, task, agent, current_user, write_tenant, error)
                    raise error

            handle = await self._enqueue(
                db,
                task=task,
                agent=agent,
                attempt_id=attempt,
                actor_user_id=current_user.id,
            )
            source_key = f"task:{task.id}:retry:{attempt}" if attempt is not None else _stable_key(task)
            await self._record_enqueued(
                db, task, agent, current_user, write_tenant, handle, source_key, attempt is not None
            )
            # enqueue_task_runtime flipped the task to "doing"; a new Run's
            # start command is "pending" until the worker claims it.
            derived = self._derive_state("doing", unmet, handle.run_id, command_status="pending", terminal=terminal)
            return TaskExecutionOutcome(
                task_id=task.id,
                state="reused" if not handle.created else "enqueued",
                run_id=handle.run_id,
                created=handle.created,
                attempt_id=attempt,
                source_execution_id=source_key,
                derived_state=derived,
                active_run_id=handle.run_id,
            )

    # ------------------------------------------------------------------
    # Query (§10.2)
    # ------------------------------------------------------------------

    async def query_execution(
        self,
        db: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        current_user: User,
    ) -> TaskExecutionQuery:
        """§10.2 projection: derived_state + the task's bounded Run list.

        ``runs`` = the task's Runs ordered by ``created_at`` (the bounded
        prefix read, root §2 data-bound rule: one task, small N); the
        ``result_summary`` reuses the latest settlement TaskLog line (root
        §十五 — no new Artifact system).
        """
        if agent.tenant_id is None:
            raise TaskExecutionError("TENANT_CONTEXT_MISSING", "Agent has no tenant context")
        caller_tenant = getattr(current_user, "tenant_id", None)
        if caller_tenant is None:
            raise TaskExecutionError("TENANT_CONTEXT_MISSING", "Calling user has no tenant context")
        try:
            verify_tenant_scope(agent.tenant_id, caller_tenant)
        except TenantScopeViolation as exc:
            raise TaskExecutionError(
                "TENANT_MISMATCH", "Task/Agent tenant does not match the calling user's tenant"
            ) from exc
        with tenant_context(agent.tenant_id):
            unmet = await task_graph_service.ensure_ready(db, task=task, tenant_id=agent.tenant_id)
            runs = await agent_run_dao.list_task_runs(task.id, db=db)
            terminal = await agent_run_dao.terminal_events_for_runs([r.id for r in runs], db=db)
            logs = await task_provenance_dao.latest_task_logs(task.id, limit=3, db=db)
            result_summary = logs[0].content if logs else None

            active_run_id: uuid.UUID | None = None
            command_status: str | None = None
            if task.status == "doing":
                active_run_id = self._in_flight_run_id(runs, terminal)
                start_cmd = (
                    await agent_run_dao.start_command_for_run(active_run_id, db=db)
                    if active_run_id
                    else None
                )
                command_status = start_cmd.status if start_cmd else None

            derived = self._derive_state(
                task.status,
                unmet,
                active_run_id,
                command_status=command_status,
                terminal=terminal,
            )
            projections = tuple(
                TaskRunProjection(
                    run_id=r.id,
                    # list_task_runs filters on the task-prefix LIKE, so every
                    # row carries the key; the ORM type stays Optional.
                    source_execution_id=r.source_execution_id or "",
                    attempt=_run_attempt_label(r.source_execution_id or ""),
                    started_at=r.created_at,
                    settled_state=terminal[r.id].event_type.removeprefix("run_")
                    if r.id in terminal
                    else None,
                    result_summary=result_summary,
                )
                for r in runs
            )
        return TaskExecutionQuery(
            derived_state=derived,
            active_run_id=active_run_id,
            unmet_dependencies=tuple(unmet),
            runs=projections,
        )

    # ------------------------------------------------------------------
    # §4 gate — fixed evaluation order, first failure wins, fail-closed
    # ------------------------------------------------------------------

    async def _gate(
        self,
        db: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        unmet: Sequence[uuid.UUID],
    ) -> tuple[str, str] | None:
        """Return ``(closed_code, message)`` for the first failing gate.

        Order (cheapest → most expensive, per spec §4): P3 task state →
        P1 tenant direct-assert → P8 project re-load → P2 project state →
        P5 agent availability → P7 v2 gate → P4 dependency readiness.
        P6 adds NO enqueue-time check by ruling (U-2): the storage owner
        decides availability at run time; a busy lock surfaces as the
        transient WORKSPACE_CONFLICT (§6.3), never as a gate.
        """
        # P3 — task state (type + the re-trigger hole fix, §1.2/U-3)
        if task.type != "todo":
            return ("TASK_TYPE_NOT_SUPPORTED", f"Task type {task.type!r} is not executable via Execute")
        if task.status == "doing":
            return ("TASK_ALREADY_RUNNING", "Task has an in-flight Run; one attempt per Task at a time")
        if task.status == "done":
            return ("TASK_TERMINAL", "Task is already complete (terminal for todo Tasks)")

        # P1 — tenant direct assertion (closes the agent-side-only gap §6.3)
        if task.tenant_id is not None and task.tenant_id != agent.tenant_id:
            return ("TENANT_MISMATCH", "Task tenant does not match the Agent tenant")

        # P8 — provenance re-load: a dangling / CASCADE-deleted project is
        # caught as NOT_FOUND here, not executed blind.
        project = None
        if task.project_id is not None:
            project = await project_dao.get_scoped(task.project_id, db=db)
            if project is None:
                return ("PROJECT_NOT_FOUND", "Task's Project no longer exists in this tenant")

        # P2 — Project state (execution-allowed set, U-1 ruling)
        if project is not None:
            if project.tenant_id != agent.tenant_id:
                return ("TENANT_MISMATCH", "Project tenant does not match the Agent tenant")
            if project.status not in PROJECT_EXECUTABLE_STATUSES:
                allowed = ", ".join(sorted(PROJECT_EXECUTABLE_STATUSES))
                return (
                    "PROJECT_NOT_EXECUTABLE",
                    f"Project is in status {project.status}; execution is allowed at {allowed} only",
                )
        # P5 — agent availability.  U-A ruling: ``Agent.status`` (container
        # lifecycle vocabulary) is NOT consulted; the row, its expiration,
        # and the model binding are the V1 availability facts.  (The DAO
        # reuses the caller session via the context; read-only.)
        active_agent = await agent_dao.get_active(agent.id)
        if active_agent is None:
            return ("ASSIGNMENT_FAILED", "Task's Agent does not exist (or was deleted)")
        if active_agent.primary_model_id is None:
            return ("AGENT_UNAVAILABLE", "Agent has no configured primary model")
        if is_agent_expired(active_agent):
            return ("AGENT_UNAVAILABLE", "Agent has expired")

        # P7 — v2 runtime gate, fail-closed, no legacy fallback
        decision = decide_runtime_v2(agent_id=agent.id, source_type="task")
        if not decision.use_v2:
            return ("RUNTIME_V2_DISABLED", "Agent Runtime v2 is not enabled for task execution")

        # P4 — dependency readiness (defense-in-depth re-run of
        # ensure_ready; readiness is never persisted, task readiness audit §2)
        if unmet:
            return ("TASK_BLOCKED", "Task has unmet dependencies")

        return None

    # ------------------------------------------------------------------
    # §5 attempt keying (R1–R5)
    # ------------------------------------------------------------------

    def _new_attempt_id(
        self,
        task: Task,
        runs,
        terminal: dict,
    ) -> uuid.UUID | None:
        """Fresh retry attempt id ONLY when R3 applies (spec §5).

        The P3 gate guarantees the task is ``pending`` here, so:
        * the task's latest Run is terminal failed/cancelled → mint a NEW
          attempt id (R3: explicit new attempt key; the stable key stays
          reserved for the first attempt, R5);
        * otherwise → first attempt (stable key) or nothing enqueued.
        Never automatic (R4): the id is minted only by THIS human-initiated
        Execute call.
        """
        for run in reversed(runs):
            event = terminal.get(run.id)
            if event is None:
                continue
            if event.event_type in ("run_failed", "run_cancelled"):
                return uuid.uuid4()
        return None

    @staticmethod
    def _in_flight_run_id(runs, terminal: dict) -> uuid.UUID | None:
        """Latest not-yet-settled Run (the active attempt, one bounded read).

        ``runs`` is oldest-first; the newest row without a terminal event is
        the in-flight attempt.  The authoritative QUEUED vs RUNNING split
        comes from the command row in the §6.2 projection.
        """
        for run in reversed(runs):
            if run.id not in terminal:
                return run.id
        return None

    # ------------------------------------------------------------------
    # §8 audit / log writes (intake boundary — NOT the Runtime)
    # ------------------------------------------------------------------

    async def _record_blocked(
        self,
        db: AsyncSession,
        task: Task,
        agent: Agent,
        current_user: User,
        write_tenant: uuid.UUID,
        error: TaskExecutionError,
    ) -> None:
        """TaskLog (⛔ with the closed code) + one AuditLog row, one path.

        Both ride the caller transaction (the transport commits).  The
        AuditLog write is narrow-caught: a failed audit write must never
        undo or mask the blocked primary outcome (materialization precedent,
        spec §8).
        """
        db.add(
            TaskLog(
                task_id=task.id,
                content=f"⛔ 执行门禁拒绝: {error.code} — {error.detail}",
            )
        )
        try:
            db.add(
                AuditLog(
                    tenant_id=write_tenant,
                    user_id=current_user.id,
                    agent_id=agent.id,
                    action="task_execute_blocked",
                    details={
                        "task_id": str(task.id),
                        "project_id": str(task.project_id) if task.project_id else None,
                        "run_id": None,
                        "source_execution_id": _stable_key(task),
                        "outcome_code": error.code,
                    },
                )
            )
            await db.flush()
        except Exception as audit_exc:  # noqa: BLE001 — narrow: audit write only
            logger.error(f"[TaskExec] blocked audit write failed: {audit_exc}")

    async def _record_enqueued(
        self,
        db: AsyncSession,
        task: Task,
        agent: Agent,
        current_user: User,
        write_tenant: uuid.UUID,
        handle: RunHandle,
        source_key: str,
        is_retry: bool,
    ) -> None:
        action = "task_execute_retried" if is_retry else "task_execute"
        try:
            db.add(
                AuditLog(
                    tenant_id=write_tenant,
                    user_id=current_user.id,
                    agent_id=agent.id,
                    action=action,
                    details={
                        "task_id": str(task.id),
                        "project_id": str(task.project_id) if task.project_id else None,
                        "run_id": str(handle.run_id),
                        "source_execution_id": source_key,
                        "outcome_code": "ENQUEUED" if handle.created else "REUSED",
                    },
                )
            )
            await db.flush()
        except Exception as audit_exc:  # noqa: BLE001 — narrow: audit write only
            logger.error(f"[TaskExec] enqueue audit write failed: {audit_exc}")

    # ------------------------------------------------------------------
    # Enqueue — the EXISTING chain; we only provide the input payload
    # ------------------------------------------------------------------

    async def _enqueue(
        self,
        db: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        attempt_id: uuid.UUID | None,
        actor_user_id: uuid.UUID,
    ) -> RunHandle:
        try:
            handle = await enqueue_task_runtime(
                db,
                task=task,
                agent=agent,
                attempt_id=attempt_id,
                actor_user_id=actor_user_id,
            )
        except TaskBlockedError as blocked:
            # Defense in depth: P4 already cleared the gate, so this only
            # fires if the graph changed mid-transaction.  Same closed code,
            # fail-closed, nothing enqueued.
            raise TaskExecutionError("TASK_BLOCKED", "Task became blocked by unmet dependencies") from blocked
        except RuntimePersistenceError as exc:
            # §6.3 QUEUE_FAILED: the intake's exact-input / uniqueness
            # contract rejected the registration.  The task stays pending
            # and the human may re-Execute under a fresh attempt key.
            raise TaskExecutionError("QUEUE_FAILED", f"Runtime intake rejected the Run: {exc.code}") from exc
        if handle is None:
            # P7 safety net: the gate already failed closed; a None return
            # from enqueue means the v2 gate dropped out mid-transaction.
            raise TaskExecutionError("RUNTIME_V2_DISABLED", "Runtime v2 not enabled; nothing enqueued")
        return handle

    # ------------------------------------------------------------------
    # §6 projection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_state(
        task_status: str,
        unmet: Sequence[uuid.UUID],
        active_run_id: uuid.UUID | None,
        *,
        command_status: str | None = None,
        terminal: dict | None = None,
    ) -> str:
        """Closed derived_state set (spec §6.2) — a projection, not a table.

        Computed ONLY from task.status + unmet deps + active Run + the
        command row (+ latest terminal event when available) — the
        projection never claims more truth than those rows give (root §十二).
        """
        if task_status == "doing":
            return "RUNNING" if command_status == "claimed" else "QUEUED"
        if task_status == "done":
            return "SUCCEEDED"
        # task.status == "pending"
        if unmet:
            return "BLOCKED"
        if terminal:
            for event in reversed(list(terminal.values())):
                if event.event_type == "run_failed":
                    return "FAILED"
                if event.event_type == "run_cancelled":
                    return "CANCELLED"
                if event.event_type == "run_completed":
                    # A completed terminal with a pending task is the
                    # supervision path — not a V1 todo outcome; READY.
                    break
        if active_run_id is not None:
            return "QUEUED"
        return "READY"

    @staticmethod
    def _utc_day_start() -> datetime:
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


task_execution_service = TaskExecutionService()

__all__ = [
    "PROJECT_EXECUTABLE_STATUSES",
    "RETRY_SOFT_CAP_PER_TASK_PER_DAY",
    "TaskExecutionError",
    "TaskExecutionOutcome",
    "TaskExecutionQuery",
    "TaskExecutionService",
    "TaskRunProjection",
    "task_execution_service",
]

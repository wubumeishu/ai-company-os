"""Phase 2F — VERIFY Result Settlement & Error Handling (task t_77399eca).

Sibling `t_45477a14` proved the full Task -> Agent -> Real LLM -> Tool ->
Result -> Verification -> `run_completed` -> Task `done` loop end-to-end.
That task's report explicitly scoped the failure / verification settlement
sub-cases to THIS task.

What this driver validates, all against a DEDICATED scratch Postgres DB
(isolated from the live pool) using the REAL runtime code paths:

  * real Phase-2E intake (`enqueue_task_runtime`) -> Run + start command +
    `run_created` event + `task.status pending -> doing`
  * the REAL settlement seam (`RuntimeCheckpointSideEffects.handle`) that
    projects the terminal `agent_run_event` (run_completed / run_failed /
    run_cancelled) AND runs the REAL `TaskRuntimeCompletionHandler` that
    settles the Task row + writes the terminal TaskLog
  * the REAL Phase-2E derived-state projection
    (`TaskExecutionService.query_execution` / `_derive_state`)

Scenarios (task body: Success / Tool Failure / Verification Failure /
Agent Failure, plus BLOCKED for the closed-state set):

  1. Success             -> task.status "done",  derived SUCCEEDED
  2. Tool failure        -> task NOT done ("pending"), derived FAILED
                            (NO false positive: a failed Run must not settle
                            the Task to success)
  3. Verification failure-> task RECOVERABLE ("pending"), derived FAILED
                            (run finished but verification rejected; the Task
                            must land in a state a human can retry)
  4. Agent failure        -> task NOT done ("pending"), derived FAILED
                            (no false success on a model-level failure)
  5. Cancelled           -> task "pending", derived CANCELLED
  6. BLOCKED             -> task "pending" with an unmet dependency, derived
                            BLOCKED; intake raises TaskBlockedError (no Run)

The ONLY things this driver constructs are the *terminal checkpoint
observations* — i.e. the durable lifecycle state the real LangGraph executor
would have written at each terminal boundary. Those are built
byte-faithfully from the exact transition code in
`app/services/agent_runtime/node_executor.py` (tool / verification / model
failure reasons). Everything from the intake downward — Run + command
registration, terminal event projection, task settlement, and the Phase-2E
derived-state projection — is REAL runtime code executing against a real
Postgres. The LLM is NOT re-invoked here: the settlement layer's job is to
interpret a terminal checkpoint correctly, and that is what this validates
deterministically (the real graph producing these checkpoints was already
proven by the sibling task).

Run:
    cd backend && uv run --no-sync python scripts/verify_2f_result_settlement.py

Environment (the ONLY external inputs):
    AGNES_API_KEY    real LLM credential (seeded into the llm_models row;
                     NOT re-invoked by the settlement path)
    AGNES_BASE_URL   e.g. https://apihub.agnes-ai.com/v1
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

# ── external inputs (real LLM credential, seeded but not re-invoked) ────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")

if not AGNES_API_KEY:
    print("FATAL: AGNES_API_KEY not set — need a real llm_models row for the seed.")
    sys.exit(2)

# ── isolated scratch coordinates ──────────────────────────────────────────────
SCRATCH_DB = f"clawith_2f_settle_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-settle-secret"
SCRATCH_WS = f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_settle_ws_{uuid.uuid4().hex[:6]}"

os.environ["DATABASE_URL"] = SCRATCH_DB_URL
os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = SCRATCH_DB_URL
os.environ["SECRET_KEY"] = SCRATCH_SECRET
os.environ["JWT_SECRET_KEY"] = SCRATCH_SECRET
os.environ["AGENT_DATA_DIR"] = SCRATCH_WS
os.environ["STORAGE_LOCAL_ROOT"] = SCRATCH_WS
os.environ.setdefault("PROCESS_ROLE", "worker")
os.environ.setdefault("LOG_LEVEL", "WARNING")

if sys.platform == "win32":
    _aio = asyncio
    _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select

# Register all ORM models on Base.metadata before create_all.
_pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "models")
for _m in os.listdir(_pkg):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import Base, async_session, create_async_engine, engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.llm import LLMModel
from app.models.task import Task, TaskDependency, TaskLog
from app.models.user import User

settings = get_settings()

# ── REAL runtime seams exercised below ───────────────────────────────────────
from app.services.agent_runtime.checkpoint_side_effects import (
    RuntimeCheckpointSideEffects,
)
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeCommandRecord,
    RuntimeRunRecord,
)
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeGraphState,
)
from app.services.agent_runtime.task_completion import TaskRuntimeCompletionHandler
from app.services.task_executor import TaskBlockedError, enqueue_task_runtime

RESULTS: list[dict] = []


def _record(name: str, ok: bool, detail: str, **extra) -> None:
    RESULTS.append({"scenario": name, "ok": bool(ok), "detail": detail, **extra})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}: {detail}")


# ---------------------------------------------------------------------------
# Real scratch DB bootstrap (identical seam to the sibling driver).
# ---------------------------------------------------------------------------
async def _bootstrap() -> None:
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(_ddl(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin.dispose()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import assert_runtime_schema_ready

    saver = create_checkpointer(settings)
    async with saver as s:
        await s.setup()
    await assert_runtime_schema_ready(engine, settings=settings)


def _ddl(q: str):
    from sqlalchemy import text

    return text(q)


async def _seed_base() -> dict:
    """Seed one tenant + user + LLMModel + Agent. Returns the ids."""
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()

    async with async_session() as s, s.begin():
        s.add(
            __import__("app.models.tenant", fromlist=["Tenant"]).Tenant(
                id=tenant,
                name="T-2F-SETTLE",
                slug=f"t2f-settle-{tenant.hex[:8]}",
                im_provider="web_only",
            )
        )
        s.add(User(id=user, tenant_id=tenant, display_name="2f-settle-user"))
    async with async_session() as s, s.begin():
        s.add(
            LLMModel(
                id=model,
                tenant_id=tenant,
                provider="openai",
                model=AGNES_MODEL,
                api_key_encrypted=encrypt_data(AGNES_API_KEY, SCRATCH_SECRET),
                base_url=AGNES_BASE_URL,
                label=f"2f-settle-{AGNES_MODEL}",
                enabled=True,
                supports_vision=False,
                supports_tool_calling=True,
                request_timeout=120,
                max_output_tokens=2048,
                context_window_tokens=32768,
            )
        )
    async with async_session() as s, s.begin():
        s.add(
            Agent(
                id=agent,
                tenant_id=tenant,
                name="SettleAgent",
                creator_id=user,
                agent_type="native",
                status="idle",
                primary_model_id=model,
                is_system=False,
                access_mode="company",
                company_access_level="use",
                expires_at=None,
                is_expired=False,
            )
        )
    return {"tenant": tenant, "user": user, "model": model, "agent": agent}


# ---------------------------------------------------------------------------
# Checkpoint construction — byte-faithful terminal lifecycles from
# node_executor.py. Only these are constructed; everything downstream is real.
# ---------------------------------------------------------------------------
def _registry(base: dict, run_id: uuid.UUID, task_id: uuid.UUID) -> RunRegistrySnapshot:
    return RunRegistrySnapshot(
        tenant_id=str(base["tenant"]),
        run_id=str(run_id),
        goal="2f-settle goal",
        run_kind="background",
        source_type="task",
        model_id=str(base["model"]),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(base["agent"]),
        session_id=None,
        system_role=None,
        parent_run_id=None,
        root_run_id=None,
    )


def _snapshots() -> RunInputSnapshots:
    return RunInputSnapshots(
        session_context={},
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"task_id": "seed"},
    )


def _build_checkpoint(
    base: dict, run_id: uuid.UUID, checkpoint_id: str, lifecycle: dict
) -> CheckpointObservation:
    state: RuntimeGraphState = {
        "registry": _registry(base, run_id, None),
        "snapshots": _snapshots(),
        "lifecycle": lifecycle,  # type: ignore[typeddict-item]
        "messages": [],
    }
    return CheckpointObservation(
        checkpoint_id=checkpoint_id,
        state=state,
        next_nodes=(),
        tasks=(),
        interrupts=(),
        metadata={"clawith_run_id": str(run_id)},
        created_at=datetime.now(UTC),
    )


# Terminal lifecycle shapes (authoritative transition strings from node_executor).
def _lc_success() -> dict:
    return {
        "status": "completed",
        "next_route": "terminal",
        "final_answer": "Settlement success: task goal completed and verified.",
        "verification_result": {"outcome": "pass", "details": {"code": "completion_gates_passed"}},
        "result_summary": {"summary": "Settlement success.", "verification": {"code": "completion_gates_passed"}},
        "error": None,
    }


def _lc_tool_failure() -> dict:
    return {
        "status": "failed",
        "next_route": "terminal",
        "reason": "tool_execution_failed",
        "error": {
            "code": "tool_execution_failed",
            "message": "Runtime tool step failed: a tool execution returned a hard error",
        },
    }


def _lc_verification_failure() -> dict:
    return {
        "status": "failed",
        "next_route": "terminal",
        "reason": "verification_repair_limit_reached",
        "error": {
            "code": "verification_repair_limit_reached",
            "message": "The finish candidate did not pass verification.",
        },
        "verification_result": {
            "outcome": "fail",
            "reason": "verification_repair_limit_reached",
        },
    }


def _lc_agent_failure() -> dict:
    # Model-level failure (deterministic agent failure: the model-step budget
    # was exhausted before the Run could finish) — NOT a tool failure.
    return {
        "status": "failed",
        "next_route": "terminal",
        "reason": "model_step_limit_reached",
        "error": {
            "code": "model_step_limit_reached",
            "message": "The Runtime model step limit was reached.",
        },
    }


def _lc_cancelled() -> dict:
    return {
        "status": "cancelled",
        "next_route": "terminal",
        "reason": "cancelled_by_command",
        "error": None,
    }


# ---------------------------------------------------------------------------
# Per-scenario driver: real intake -> read run/command -> real settlement seam
# -> real Phase-2E projection.
# ---------------------------------------------------------------------------
async def _make_task(base: dict, title: str, description: str = "") -> uuid.UUID:
    task_id = uuid.uuid4()
    async with async_session() as s, s.begin():
        s.add(
            Task(
                id=task_id,
                tenant_id=base["tenant"],
                agent_id=base["agent"],
                title=title,
                description=description,
                type="todo",
                status="pending",
                priority="medium",
                assignee="self",
                created_by=base["user"],
                project_id=None,
            )
        )
    return task_id


async def _settle_scenario(
    base: dict,
    name: str,
    title: str,
    lifecycle: dict,
    expected_task_status: str,
    expected_derived: str,
    expect_terminal_event: str,
    no_false_success: bool,
) -> dict:
    from app.dao.base import tenant_context as _tc
    from app.services.task_execution_service import task_execution_service

    task_id = await _make_task(base, title)
    tenant = base["tenant"]

    # 1. REAL Phase-2E intake: registers Run + start command + run_created,
    #    flips task pending -> doing.
    run_id: uuid.UUID | None = None
    async with async_session() as s:
        async with s.begin():
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
            handle = await enqueue_task_runtime(
                s, task=task, agent=agent_row, execution_id=uuid.uuid4(), actor_user_id=base["user"]
            )
        run_id = handle.run_id if handle else None
        task_status_after_intake = task.status
    if run_id is None:
        _record(f"{name} (intake)", False, "intake returned no Run — v2 gate not selected")
        return {}
    print(f"    intake: run={run_id} task.status_after={task_status_after_intake}")
    assert task_status_after_intake == "doing", "intake must flip the Task to doing"

    # 2. Read back the REAL run + its start command.
    async with async_session() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        cmd = (
            await s.execute(
                select(AgentRunCommand)
                .where(AgentRunCommand.run_id == run_id, AgentRunCommand.command_type == "start")
                .order_by(AgentRunCommand.created_at)
                .limit(1)
            )
        ).scalar_one()
        agent_run = RuntimeRunRecord(
            tenant_id=tenant,
            run_id=run_id,
            thread_id=row.runtime_thread_id,
            runtime_type=row.runtime_type,
            goal=row.goal,
            run_kind=row.run_kind,
            source_type=row.source_type,
            model_id=str(row.model_id),
            graph_name=row.graph_name,
            graph_version=row.graph_version,
            agent_id=str(row.agent_id) if row.agent_id else None,
            session_id=str(row.session_id) if row.session_id else None,
            system_role=row.system_role,
            parent_run_id=str(row.parent_run_id) if row.parent_run_id else None,
            root_run_id=str(row.root_id) if False else None,
            model_turn_limit=row.model_turn_limit,
        )
        command = RuntimeCommandRecord(
            id=cmd.id,
            tenant_id=cmd.tenant_id,
            run_id=cmd.run_id,
            command_type=cmd.command_type,
            payload=cmd.payload,
            actor_user_id=cmd.actor_user_id,
            actor_agent_id=cmd.actor_agent_id,
            attempt_count=cmd.attempt_count,
        )

    # 3. REAL settlement seam: project the terminal event AND settle the task.
    side_effects = RuntimeCheckpointSideEffects(
        session_factory=async_session,
        checkpoint_handlers=(),
        terminal_handlers=(TaskRuntimeCompletionHandler(session_factory=async_session),),
    )
    checkpoint_id = f"chk-{uuid.uuid4().hex}"
    checkpoint = _build_checkpoint(base, run_id, checkpoint_id, lifecycle)
    await side_effects.handle(run=agent_run, command=command, checkpoint=checkpoint)

    # 4. REAL Phase-2E derived-state projection.
    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
        user_row = (await s.execute(select(User).where(User.id == base["user"]))).scalar_one()
        with _tc(tenant):
            projection = await task_execution_service.query_execution(
                s, task=task, agent=agent_row, current_user=user_row
            )
        settled_task_status = task.status
        settled_completed_at = task.completed_at

    # 5. Read the real terminal event + settlement TaskLog.
    async with async_session() as s:
        ev = (
            await s.execute(
                select(AgentRunEvent.event_type)
                .where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_type.in_(("run_completed", "run_failed", "run_cancelled")),
                )
                .order_by(AgentRunEvent.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        logs = (
            await s.execute(
                select(TaskLog.content).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.desc())
            )
        ).scalars().all()
    terminal_event = ev
    latest_log = logs[0] if logs else ""

    # 6. Assertions.
    checks = []
    checks.append(("terminal_event", terminal_event == expect_terminal_event))
    checks.append(("task_status", settled_task_status == expected_task_status))
    checks.append(("derived_state", projection.derived_state == expected_derived))
    # false-positive guard: a non-success terminal must NEVER settle to done/SUCCEEDED.
    if no_false_success:
        checks.append(("no_false_success", settled_task_status != "done" and projection.derived_state != "SUCCEEDED"))
    if expected_task_status == "done":
        checks.append(("log_success", latest_log.startswith("✅ 任务完成")))
    else:
        checks.append(("log_failure_or_cancel", latest_log.startswith(("❌ 任务执行失败", "⏹️"))))

    all_ok = all(c[1] for c in checks)
    _record(
        name,
        all_ok,
        f"task={settled_task_status} derived={projection.derived_state} terminal={terminal_event} "
        f"log={latest_log[:34]!r}",
        checks={f"{k}:{v}": bool(v) for k, v in checks},
        run_id=str(run_id),
        task_id=str(task_id),
        completed_at=settled_completed_at,
    )
    return {"run_id": str(run_id), "task_id": str(task_id)}


async def _blocked_scenario(base: dict) -> None:
    """A todo Task with an unmet dependency must stay BLOCKED, no Run."""
    from app.dao.base import tenant_context as _tc
    from app.services.task_execution_service import task_execution_service

    parent_id = await _make_task(base, "parent-settle")
    child_id = await _make_task(base, "child-settle-blocked")

    # parent stays pending (unmet); wire child -> parent dependency.
    async with async_session() as s, s.begin():
        s.add(
            TaskDependency(
                id=uuid.uuid4(),
                tenant_id=base["tenant"],
                task_id=child_id,
                depends_on_task_id=parent_id,
            )
        )

    # intake must fail closed with TaskBlockedError and create NO Run.
    intake_blocked = False
    try:
        async with async_session() as s, s.begin():
            task = (await s.execute(select(Task).where(Task.id == child_id))).scalar_one()
            agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
            await enqueue_task_runtime(
                s, task=task, agent=agent_row, execution_id=uuid.uuid4(), actor_user_id=base["user"]
            )
    except TaskBlockedError as e:
        intake_blocked = True
        print(f"    intake raised TaskBlockedError (unmet={len(e.reason)}) — fail-closed, no Run")

    # real projection: derived_state must be BLOCKED with the unmet dep.
    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == child_id))).scalar_one()
        agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
        user_row = (await s.execute(select(User).where(User.id == base["user"]))).scalar_one()
        with _tc(base["tenant"]):
            projection = await task_execution_service.query_execution(
                s, task=task, agent=agent_row, current_user=user_row
            )
        status = task.status
        child_runs = (
            await s.execute(
                select(AgentRun.id).where(
                    AgentRun.source_type == "task",
                    AgentRun.source_execution_id.like(f"task:{child_id}%"),
                )
            )
        ).scalars().all()

    unmet_ok = tuple(projection.unmet_dependencies) == (parent_id,)
    all_ok = (
        intake_blocked
        and status == "pending"
        and projection.derived_state == "BLOCKED"
        and unmet_ok
        and not child_runs
    )
    _record(
        "Blocked (dependency unmet)",
        all_ok,
        f"intake_blocked={intake_blocked} task={status} derived={projection.derived_state} "
        f"unmet_match={unmet_ok} runs_created={len(child_runs)}",
        checks={
            "intake_fail_closed": intake_blocked,
            "task_stays_pending": status == "pending",
            "derived_BLOCKED": projection.derived_state == "BLOCKED",
            "unmet_dependency": unmet_ok,
            "no_run_created": not child_runs,
        },
    )


async def main() -> int:
    print(f"=== Phase 2F Result Settlement & Error Handling ({SCRATCH_DB}) ===")
    print(f"  llm endpoint : {AGNES_BASE_URL} (model={AGNES_MODEL}) — seeded, not re-invoked")
    print(f"  scratch db   : {SCRATCH_DB}")

    await _bootstrap()
    base = await _seed_base()
    print(f"  tenant       : {base['tenant']}")
    print(f"  agent        : {base['agent']}  model={base['model']}")

    # ── settlement scenarios ────────────────────────────────────────────────
    print("\n--- Settlement scenarios (real intake + real settlement + real projection) ---")
    await _settle_scenario(
        base, "Success", "settle-success",
        _lc_success(), "done", "SUCCEEDED", "run_completed", no_false_success=False,
    )
    await _settle_scenario(
        base, "Tool failure", "settle-tool-failure",
        _lc_tool_failure(), "pending", "FAILED", "run_failed", no_false_success=True,
    )
    await _settle_scenario(
        base, "Verification failure", "settle-verification-failure",
        _lc_verification_failure(), "pending", "FAILED", "run_failed", no_false_success=True,
    )
    await _settle_scenario(
        base, "Agent failure", "settle-agent-failure",
        _lc_agent_failure(), "pending", "FAILED", "run_failed", no_false_success=True,
    )
    await _settle_scenario(
        base, "Cancelled", "settle-cancelled",
        _lc_cancelled(), "pending", "CANCELLED", "run_cancelled", no_false_success=True,
    )

    print("\n--- BLOCKED (dependency) scenario ---")
    await _blocked_scenario(base)

    # ── evidence + verdict ──────────────────────────────────────────────────
    evidence = {
        "scratch_db": SCRATCH_DB,
        "llm": {"base_url": AGNES_BASE_URL, "provider": "openai", "model": AGNES_MODEL,
                "seeded_only_not_reinvoked": True},
        "results": RESULTS,
        "all_pass": all(r["ok"] for r in RESULTS),
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHASE_2F_RESULT_SETTLEMENT_EVIDENCE.json")
    Path(out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  evidence written to {out}")

    ok = evidence["all_pass"]
    print("\n=== VERDICT ===")
    for r in RESULTS:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['scenario']}: {r['detail']}")
    print(f"\n  {'PASS' if ok else 'BLOCKED'}  ({sum(1 for r in RESULTS if r['ok'])}/{len(RESULTS)} scenarios passed)")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

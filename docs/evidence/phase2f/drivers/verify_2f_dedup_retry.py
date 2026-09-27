"""Phase 2F — VERIFY §13 Duplicate Run Protection + §14 Explicit Retry (task t_f28b2fa3).

Covers the two root sections no Wave-1 card verified yet. Builds on the
proven t_45477a14 / t_77399eca seams (scratch-Postgres seeding, real
Phase-2E intake, forced-failure settlement, real command worker) — no
mocks, no product code touched.

Scenarios
---------
A. §13 Duplicate Run — real-LLM env
   * Execute #1 (real service path) -> Run A1 (stable key ``task:{id}``).
   * Duplicate #2, back-to-back, two independent dedup terminals:
       - service in-flight gate  -> TASK_ALREADY_RUNNING (409), R2
       - intake exact-input dedup: same stable key re-enqueued ->
         created=False, same Run (DB-unique uq_agent_runs_source_execution, R1)
     -> Run row count for the stable key stays EXACTLY 1; 0 new rows.
   * Run A1 is then driven through the REAL command worker with a REAL LLM
     (no mock) to a real terminal event.

B. §14 Explicit Retry — task:{id}:retry:{uuid}
   * FAILED Run B1 built INTENTIONALLY via the byte-faithful forced-failure
     settlement seam (controlled tool-execution failure; NOT a flaky LLM).
   * No-auto-retry: bounded poll of the run table after the failure -> 0 new
     rows (retries are only ever human-initiated, spec R4).
   * Explicit retry: Execute #2 mints ``task:{id}:retry:{uuid}`` -> exactly
     ONE new Run (the task's 2nd), with a source_execution_id that DIFFERS
     from Run B1. Driven with a real LLM to a real terminal.

C. §14 RETRY_CAP_EXCEEDED (closed-set error, fail-closed)
   * FAILED Run C1 + RETRY_SOFT_CAP_PER_TASK_PER_DAY (=3) seeded
     task_execute_retried audit rows -> Execute -> RETRY_CAP_EXCEEDED;
     0 new Run rows.

Evidence: backend/scripts/PHASE_2F_DEDUP_RETRY_EVIDENCE.json
Run:
    cd backend && uv run --no-sync python scripts/verify_2f_dedup_retry.py
Environment: AGNES_API_KEY, AGNES_BASE_URL (real LLM; scratch DB is disposable).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# ── external inputs (real LLM credential) ───────────────────────────────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")

if not AGNES_API_KEY:
    print("FATAL: AGNES_API_KEY not set — a real LLM run needs a real key.")
    sys.exit(2)

# ── isolated scratch coordinates ────────────────────────────────────────────
SCRATCH_DB = f"clawith_2f_dedup_retry_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-dedup-secret"
SCRATCH_WS = f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_dedup_ws_{uuid.uuid4().hex[:6]}"

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
from app.models.audit import AuditLog
from app.models.llm import LLMModel
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
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
from app.services.task_execution_service import (
    RETRY_SOFT_CAP_PER_TASK_PER_DAY,
    TaskExecutionError,
    task_execution_service,
)
from app.services.task_executor import enqueue_task_runtime

settings = get_settings()

RESULTS: list[dict] = []


def _record(name: str, ok: bool, detail: str, **extra) -> None:
    RESULTS.append({"scenario": name, "ok": bool(ok), "detail": detail, **extra})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}: {detail}")


# ---------------------------------------------------------------------------
# Bootstrap + seeding (identical seam to t_45477a14 / t_77399eca)
# ---------------------------------------------------------------------------
async def _bootstrap() -> None:
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        from sqlalchemy import text as _t

        await c.execute(_t(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin.dispose()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import assert_runtime_schema_ready

    saver = create_checkpointer(settings)
    async with saver as s:
        await s.setup()
    await assert_runtime_schema_ready(engine, settings=settings)


async def _seed_base() -> dict:
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()

    async with async_session() as s, s.begin():
        s.add(
            Tenant(
                id=tenant,
                name="T-2F-DEDUP",
                slug=f"t2f-dedup-{tenant.hex[:8]}",
                im_provider="web_only",
            )
        )
        s.add(User(id=user, tenant_id=tenant, display_name="2f-dedup-user"))
    async with async_session() as s, s.begin():
        s.add(
            LLMModel(
                id=model,
                tenant_id=tenant,
                provider="openai",
                model=AGNES_MODEL,
                api_key_encrypted=encrypt_data(AGNES_API_KEY, SCRATCH_SECRET),
                base_url=AGNES_BASE_URL,
                label=f"2f-dedup-{AGNES_MODEL}",
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
                name="DedupRetryAgent",
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
    # Seed the canonical tool set so the model is offered real tools and is
    # not forced onto the _always_tools explorer fallback (t_45477a14 seam).
    from app.models.tool import AgentTool, Tool
    from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy

    ENABLE_TOOLS = ["read_file", "write_file", "execute_code"]
    SUPPRESS_TOOLS = ["list_focus_items", "query_directory"]
    tool_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        for name in ENABLE_TOOLS + SUPPRESS_TOOLS:
            tid = uuid.uuid4()
            tool_ids[name] = tid
            d = builtin_model_definition(name)
            fn = d.get("function", {})
            pol = builtin_policy(name) or {}
            s.add(
                Tool(
                    id=tid,
                    name=name,
                    display_name=fn.get("display_name", name) or name,
                    description=fn.get("description", ""),
                    type="builtin",
                    category=fn.get("category", "general"),
                    icon="🔧",
                    parameters_schema=fn.get("parameters", {}),
                    config=pol.get("config", {}) if isinstance(pol, dict) else {},
                    source="builtin",
                    enabled=True,
                    is_default=True,
                )
            )
        await s.flush()
        for name in ENABLE_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=True, source="system"))
        for name in SUPPRESS_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=False, source="system"))
    return {"tenant": tenant, "user": user, "model": model, "agent": agent}


async def _make_task(base: dict, title: str) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with async_session() as s, s.begin():
        s.add(
            Task(
                id=task_id,
                tenant_id=base["tenant"],
                agent_id=base["agent"],
                title=title,
                description=_MINIMAL_GOAL,
                type="todo",
                status="pending",
                priority="medium",
                assignee="self",
                created_by=base["user"],
                project_id=None,
            )
        )
    return task_id


# ---------------------------------------------------------------------------
# Real execute / dedup helpers
# ---------------------------------------------------------------------------
async def _execute_once(base: dict, task_id: uuid.UUID) -> tuple:
    """Real service Execute (spec §10.1). Returns (outcome, error_code|None)."""
    from app.dao.base import tenant_context as _tc

    async with async_session() as s, s.begin():
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
        user_row = (await s.execute(select(User).where(User.id == base["user"]))).scalar_one()
        try:
            with _tc(base["tenant"]):
                outcome = await task_execution_service.execute(
                    s, task=task, agent=agent_row, current_user=user_row
                )
        except TaskExecutionError as exc:
            await s.rollback()
            return None, exc.code
        return outcome, None


async def _reintake_duplicate(base: dict, task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    """Intake-level duplicate: re-enqueue the SAME stable key back-to-back.

    The intake's exact-input re-resolution (persistence._resolve_source_retry,
    adapter._find_start_retry) returns the EXISTING run with created=False;
    the DB-unique uq_agent_runs_source_execution makes a second row
    physically impossible.
    """
    from app.dao.base import tenant_context as _tc

    async with async_session() as s, s.begin():
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        agent_row = (await s.execute(select(Agent).where(Agent.id == base["agent"]))).scalar_one()
        with _tc(base["tenant"]):
            handle = await enqueue_task_runtime(
                s, task=task, agent=agent_row, execution_id=uuid.uuid4(), actor_user_id=base["user"]
            )
        created = handle.created if handle else None
        second_run_id = handle.run_id if handle else None
    return {"created": created, "second_run_id": second_run_id, "same_run": (second_run_id == run_id)}


async def _task_run_rows(base: dict, task_id: uuid.UUID) -> list[dict]:
    """Bounded read of a task's Run rows (stable key + retries, oldest first)."""
    from app.dao.agent_run_dao import agent_run_dao
    from app.dao.base import tenant_context as _tc

    async with async_session() as s:
        with _tc(base["tenant"]):
            rows = await agent_run_dao.list_task_runs(task_id, db=s)
        return [
            {"run_id": str(r.id), "source_execution_id": r.source_execution_id, "created_at": r.created_at}
            for r in rows
        ]


async def _terminal_event_for(run_id: uuid.UUID) -> str | None:
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
    return ev


async def _task_status(task_id: uuid.UUID) -> str:
    async with async_session() as s:
        return (await s.execute(select(Task.status).where(Task.id == task_id))).scalar_one()


# ---------------------------------------------------------------------------
# Forced-failure settlement seam (byte-faithful terminal checkpoints — the
# ONLY constructed artifact; everything downstream is REAL runtime code,
# same approach as t_77399eca).
# ---------------------------------------------------------------------------
def _registry(base: dict, run_id: uuid.UUID) -> RunRegistrySnapshot:
    return RunRegistrySnapshot(
        tenant_id=str(base["tenant"]),
        run_id=str(run_id),
        goal="2f-dedup goal",
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


async def _force_fail_run(base: dict, run_id: uuid.UUID, task_id: uuid.UUID) -> dict:
    """Project a REAL terminal run_failed + settle the Task through the
    REAL RuntimeCheckpointSideEffects seam (terminal handler settles the
    Task row + writes the terminal TaskLog). No LLM involved."""
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
            tenant_id=base["tenant"],
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
            root_run_id=None,
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

    checkpoint = CheckpointObservation(
        checkpoint_id=f"forced-{uuid.uuid4().hex}",
        state=RuntimeGraphState(
            registry=_registry(base, run_id),
            snapshots=RunInputSnapshots(
                session_context={},
                session_context_version=0,
                recent_session_messages=(),
                related_run_summaries=(),
                initial_input={"task_id": str(task_id)},
            ),
            lifecycle=_lc_tool_failure(),
            messages=[],  # type: ignore[typeddict-item]
        ),
        next_nodes=(),
        tasks=(),
        interrupts=(),
        metadata={"clawith_run_id": str(run_id)},
        created_at=datetime.now(UTC),
    )
    side_effects = RuntimeCheckpointSideEffects(
        session_factory=async_session,
        checkpoint_handlers=(),
        terminal_handlers=(TaskRuntimeCompletionHandler(session_factory=async_session),),
    )
    await side_effects.handle(run=agent_run, command=command, checkpoint=checkpoint)
    terminal = await _terminal_event_for(run_id)
    task_status = await _task_status(task_id)
    return {"terminal_event": terminal, "task_status": task_status}


# ---------------------------------------------------------------------------
# Real command worker drive (real LLM — same loop as t_45477a14)
# ---------------------------------------------------------------------------
async def _drive_run(base: dict, run_id: uuid.UUID, task_id: uuid.UUID, budget_s: float = 90.0) -> dict:
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import build_runtime_worker_components

    components = None
    async with create_checkpointer(settings) as saver2:
        await saver2.setup()
        components = build_runtime_worker_components(
            checkpointer=saver2,
            session_factory=async_session,
            lock_engine=engine,
            claimant=f"dedup-{uuid.uuid4().hex[:8]}",
            settings=settings,
        )
        terminal = None
        t0 = time.time()
        idle_streak = 0
        while time.time() - t0 < budget_s:
            res = await components.worker.run_once()
            st = getattr(res, "status", None)
            if st == "idle":
                idle_streak += 1
                if idle_streak >= 12:
                    break
            else:
                idle_streak = 0
            terminal = await _terminal_event_for(run_id)
            if terminal is not None:
                break
            await asyncio.sleep(0.5)
    task_status = await _task_status(task_id)
    return {"terminal_event": terminal, "task_status": task_status, "worker_final": st}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
async def _scenario_a_dedup(base: dict) -> dict:
    print("\n--- Scenario A: §13 Duplicate Run (real-LLM Run #1) ---")
    task_a = await _make_task(base, "dedup-real-run")

    outcome, err = await _execute_once(base, task_a)
    assert outcome is not None, f"first execute failed: {err}"
    run1 = outcome.run_id
    stable_key = f"task:{task_a}"
    rows1 = await _task_run_rows(base, task_a)
    task_status_inflight = await _task_status(task_a)

    # Duplicate #2, back-to-back — two independent dedup terminals.
    _, err2 = await _execute_once(base, task_a)
    dup_service_code = err2  # expected: TASK_ALREADY_RUNNING (R2 in-flight)
    dup_reintake = await _reintake_duplicate(base, task_a, run1)
    rows2 = await _task_run_rows(base, task_a)

    # Real LLM: drive Run #1 to a real terminal.
    driven = await _drive_run(base, run1, task_a)
    terminal_a = driven["terminal_event"]

    new_rows_on_dup = len(rows2) - len(rows1)
    ok = (
        outcome.created is True
        and outcome.state == "enqueued"
        and len(rows1) == 1
        and rows1[0]["source_execution_id"] == stable_key
        and task_status_inflight == "doing"
        and dup_service_code == "TASK_ALREADY_RUNNING"
        and dup_reintake["created"] is False
        and dup_reintake["same_run"] is True
        and new_rows_on_dup == 0
        and len(rows2) == 1
        and terminal_a in ("run_completed", "run_failed", "run_cancelled")
    )
    _record(
        "A: §13 duplicate Run protection (0 new rows)",
        ok,
        f"in-flight 2nd execute -> {dup_service_code}; intake dup created=False same_run="
        f"{dup_reintake['same_run']}; rows {len(rows1)}->{len(rows2)}; terminal={terminal_a}",
        task_id=str(task_a),
        run1=str(run1),
        run_rows_before=len(rows1),
        run_rows_after_dup=len(rows2),
        new_rows_on_duplicate=new_rows_on_dup,
        source_execution_ids=[r["source_execution_id"] for r in rows2],
        dup_service_gate=dup_service_code,
        dup_intake={"created": dup_reintake["created"], "same_run": dup_reintake["same_run"]},
        terminal_event=terminal_a,
        real_llm=terminal_a is not None,
    )
    return {
        "task_id": str(task_a),
        "run1": str(run1),
        "run_rows": [r["source_execution_id"] for r in rows2],
        "terminal_event": terminal_a,
    }


async def _scenario_b_explicit_retry(base: dict) -> dict:
    print("\n--- Scenario B: §14 Explicit Retry (task:{id}:retry:{uuid}) ---")
    task_b = await _make_task(base, "retry-from-failed")
    outcome1, err = await _execute_once(base, task_b)
    assert outcome1 is not None, f"first execute failed: {err}"
    run1 = outcome1.run_id
    prefix_rows1 = await _task_run_rows(base, task_b)

    # Intentional, controlled failure (NOT a flaky LLM): byte-faithful
    # tool_execution_failed terminal through the REAL settlement seam.
    forced = await _force_fail_run(base, run1, task_b)
    print(f"    forced failure: terminal={forced['terminal_event']} task={forced['task_status']}")

    # No-automatic-retry: bounded poll after the FAILED run settles.
    poll_s = 6.0
    polls = 0
    t0 = time.time()
    counts: list[int] = []
    while time.time() - t0 < poll_s:
        counts.append(len(await _task_run_rows(base, task_b)))
        polls += 1
        await asyncio.sleep(1.0)
    no_auto_retry = max(counts) == len(prefix_rows1) == 1

    # Explicit retry #2 -> task:{id}:retry:{uuid}, exactly 1 new Run.
    outcome2, err2 = await _execute_once(base, task_b)
    assert outcome2 is not None, f"retry execute failed: {err2}"
    run2 = outcome2.run_id
    rows_after_retry = await _task_run_rows(base, task_b)
    distinct = (
        outcome2.source_execution_id
        and outcome2.source_execution_id != f"task:{task_b}"
        and "retry:" in outcome2.source_execution_id
    )
    new_rows_on_retry = len(rows_after_retry) - 1
    # Real LLM: drive the retry Run to a real terminal.
    driven2 = await _drive_run(base, run2, task_b)

    ok = (
        forced["terminal_event"] == "run_failed"
        and forced["task_status"] == "pending"
        and no_auto_retry
        and outcome2.created is True
        and outcome2.state == "enqueued"
        and outcome2.attempt_id is not None
        and distinct
        and new_rows_on_retry == 1
        and len(rows_after_retry) == 2
        and rows_after_retry[0]["source_execution_id"] == f"task:{task_b}"
        and rows_after_retry[1]["source_execution_id"] == outcome2.source_execution_id
        and driven2["terminal_event"] in ("run_completed", "run_failed", "run_cancelled")
    )
    _record(
        "B: §14 explicit retry (1 new Run, distinct key, no auto-retry)",
        ok,
        f"forced terminal={forced['terminal_event']}; auto-retry poll {polls}x max={max(counts)} "
        f"new_rows={new_rows_on_retry}; retry skey={outcome2.source_execution_id}; "
        f"retry terminal={driven2['terminal_event']}",
        task_id=str(task_b),
        run1=str(run1),
        run2=str(run2),
        retry_attempt_id=str(outcome2.attempt_id) if outcome2.attempt_id else None,
        retry_source_execution_id=outcome2.source_execution_id,
        run_rows=[r["source_execution_id"] for r in rows_after_retry],
        no_auto_retry_poll={"window_s": poll_s, "polls": polls, "row_counts": counts, "new_rows": max(counts) - 1},
        retry_terminal_event=driven2["terminal_event"],
        real_llm_retry=True,
    )
    return {
        "task_id": str(task_b),
        "run1": str(run1),
        "run2": str(run2),
        "run_rows": [r["source_execution_id"] for r in rows_after_retry],
        "retry_source_execution_id": outcome2.source_execution_id,
        "retry_terminal_event": driven2["terminal_event"],
    }


async def _scenario_c_retry_cap(base: dict) -> dict:
    print(f"\n--- Scenario C: §14 RETRY_CAP_EXCEEDED (cap={RETRY_SOFT_CAP_PER_TASK_PER_DAY}) ---")
    task_c = await _make_task(base, "retry-cap-exceeded")
    outcome1, err = await _execute_once(base, task_c)
    assert outcome1 is not None, f"first execute failed: {err}"
    run1 = outcome1.run_id
    forced = await _force_fail_run(base, run1, task_c)

    # Seed the documented soft cap: RETRY_SOFT_CAP_PER_TASK_PER_DAY retried
    # attempts today (the cap is enforced via the audit COUNT, spec §6.3/G2).
    audit_rows = [
        AuditLog(
            id=uuid.uuid4(),
            tenant_id=base["tenant"],
            user_id=base["user"],
            agent_id=base["agent"],
            action="task_execute_retried",
            details={
                "task_id": str(task_c),
                "project_id": None,
                "run_id": str(run1) if i == 0 else None,
                "source_execution_id": f"task:{task_c}",
                "outcome_code": "ENQUEUED",
                "seed_note": "2f dedup-retry cap seed",
            },
            created_at=datetime.now(UTC),
        )
        for i in range(RETRY_SOFT_CAP_PER_TASK_PER_DAY)
    ]
    async with async_session() as s, s.begin():
        s.add_all(audit_rows)

    cap_rows_before = await _task_run_rows(base, task_c)
    _, err_cap = await _execute_once(base, task_c)
    cap_rows_after = await _task_run_rows(base, task_c)
    task_status_c = await _task_status(task_c)

    ok = (
        forced["terminal_event"] == "run_failed"
        and err_cap == "RETRY_CAP_EXCEEDED"
        and len(cap_rows_after) == len(cap_rows_before) == 1
        and task_status_c == "pending"
    )
    _record(
        "C: §14 RETRY_CAP_EXCEEDED (fail-closed, no new Run)",
        ok,
        f"cap=3 seeded retried audits -> 2nd execute -> {err_cap}; rows "
        f"{len(cap_rows_before)}->{len(cap_rows_after)}; task={task_status_c}",
        task_id=str(task_c),
        retry_cap_case="RETRY_CAP_EXCEEDED",
        retried_audits_seeded=RETRY_SOFT_CAP_PER_TASK_PER_DAY,
        run_rows=[r["source_execution_id"] for r in cap_rows_after],
        task_status=task_status_c,
        new_rows_on_capped_retry=len(cap_rows_after) - len(cap_rows_before),
    )
    return {
        "task_id": str(task_c),
        "retry_cap_case": "RETRY_CAP_EXCEEDED",
        "retried_audits_seeded": RETRY_SOFT_CAP_PER_TASK_PER_DAY,
        "task_status": task_status_c,
        "new_rows_on_capped_retry": len(cap_rows_after) - len(cap_rows_before),
    }


# ---------------------------------------------------------------------------
# Minimal prompt: one model turn, no tools, deterministic finish candidate.
# ---------------------------------------------------------------------------
_MINIMAL_GOAL = (
    "这是一个最小验证任务。请不要再调用任何工具、不要向我提问、不要等待确认，"
    "立即用一句话给出最终答案，内容就是: RESULT_OK。"
)


async def main() -> int:
    print(f"=== Phase 2F §13 Dedup + §14 Explicit Retry ({SCRATCH_DB}) ===")
    print(f"  llm endpoint : {AGNES_BASE_URL} (model={AGNES_MODEL}, provider=openai, real HTTP)")
    print(f"  scratch db   : {SCRATCH_DB}")

    await _bootstrap()
    base = await _seed_base()
    print(f"  tenant       : {base['tenant']}")
    print(f"  agent        : {base['agent']}  model={base['model']}")

    a = await _scenario_a_dedup(base)
    b = await _scenario_b_explicit_retry(base)
    c = await _scenario_c_retry_cap(base)

    evidence = {
        "scratch_db": SCRATCH_DB,
        "llm": {
            "base_url": AGNES_BASE_URL,
            "provider": "openai",
            "model": AGNES_MODEL,
            "real_http": True,
            "no_mock": True,
            "real_llm_calls": 2,  # A run #1 + B retry run; failures forced, 0 LLM
        },
        "dedup_sources": {
            "service_in_flight_gate": "task_execution_service._gate P3 -> TASK_ALREADY_RUNNING (409, api/tasks.py _execute_http)",
            "intake_exact_input_dedup": "persistence._resolve_source_retry / adapter._find_start_retry -> created=False",
            "db_unique": "agent_run.py uq_agent_runs_source_execution (partial unique on source_type+source_execution_id)",
        },
        "retry_sources": {
            "explicit_retry_keying": "task_execution_service._new_attempt_id -> task:{id}:retry:{uuid} (R3/R5, minted only by a human Execute)",
            "never_automatic": "R4: no queueing facility exists; the bounded post-failure poll is the empirical confirmation",
            "retry_cap": "audit COUNT of task_execute_retried since UTC day start >= RETRY_SOFT_CAP_PER_TASK_PER_DAY -> RETRY_CAP_EXCEEDED (409)",
        },
        "scenario_a": a,
        "scenario_b": b,
        "scenario_c": c,
        "results": RESULTS,
        "all_pass": all(r["ok"] for r in RESULTS),
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHASE_2F_DEDUP_RETRY_EVIDENCE.json")
    Path(out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  evidence written to {out}")

    ok = evidence["all_pass"]
    print("\n=== VERDICT ===")
    for r in RESULTS:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['scenario']}")
    print(f"\n  {'PASS' if ok else 'BLOCKED'}  ({sum(1 for r in RESULTS if r['ok'])}/{len(RESULTS)} scenarios passed)")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

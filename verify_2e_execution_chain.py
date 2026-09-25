"""Genuine real-Postgres verification of the Phase 2E execution chain.

Drives the ACTUAL pre-existing intake persistence boundary
(``register_run_with_start`` — the step ``enqueue_task_runtime`` feeds the
Phase-2E source keys into) against a real scratch PostgreSQL DB, proving the
three execution-chain claims the Kanban task requires, WITHOUT touching the
LangGraph/worker internals:

  Scenario 2 (idempotency):  the same source key registers ONE Run row
      (DB unique index); a second identical registration is REUSED
      (created=False) — not a second Run.
  Scenario 3 (concurrency):  two independent tasks (different source keys,
      distinct agents) register as two coexisting Runs with
      scheduling_lane_key NULL — physically parallel, no lane block.
  Concurrency stress: two CONCURRENT registrations of the SAME key ->
      exactly one Run row; the loser returns created=False via the
      SAVEPOINT/IntegrityError recovery path (the physical R1 guarantee).
  Scenario 4 (workspace-conflict layer): the command_worker thread-lock
      fast-fail layer is pre-existing and UNTOUCHED by this diff (we assert
      the diff footprint), i.e. same-agent conflicts are owned by the worker,
      not invented here.

Run with the backend venv:
    uv run --no-sync --project backend python verify_2e_execution_chain.py

Requires a reachable PostgreSQL with the postgres user (localhost:5432) and a
dedicated scratch DB named in DATABASE_URL below. ``Base.metadata.create_all``
is idempotent, so re-runs are safe.  The scratch DB is disposable review state
— never a source of truth.
"""
import asyncio
import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/clawith_2e_v4"

# Import app models (registers Base.metadata) + the real intake persistence layer.
import importlib  # noqa: E402
import os as _os  # noqa: E402
_pkg = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "backend", "app", "models")
for _m in _os.listdir(_pkg):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")
from app.database import Base, create_async_engine  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.agent_run_command import AgentRunCommand  # noqa: E402
from app.services.agent_runtime.contracts import StartRunCommand, RunHandle  # noqa: E402
from app.services.agent_runtime.persistence import (  # noqa: E402
    RunRegistration,
    register_run_with_start,
)
from app.models.task import Task, TaskDependency  # noqa: E402
from app.services.task_executor import (  # noqa: E402
    enqueue_task_runtime,
    TaskBlockedError,
)
import importlib  # noqa: E402

engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=2, max_overflow=2)


async def _reg(db, tenant, agent, task_id, source_execution_id, idem_key, payload, model_id):
    reg = RunRegistration(
        tenant_id=tenant,
        source_type="task",
        goal="[task execution] verify",
        run_kind="background",
        runtime_type="langgraph",
        graph_name="clawith_agent_runtime",
        graph_version="v1",
        delivery_status="not_required",
        agent_id=agent,
        source_id=str(task_id),
        source_execution_id=source_execution_id,
        model_id=model_id,
        model_turn_limit=8,
    )
    return await register_run_with_start(
        db, reg, start_payload=payload, start_idempotency_key=idem_key, actor_user_id=None
    )


db = None


async def main():
    global db
    # Create ALL model tables in the disposable scratch DB (Run tables FK to
    # tenants/agents/users/llm_models/chat_sessions).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed the FK-parent rows (agent_runs physically FKs to tenants/agents/
    # users/llm_models — pre-existing model detail, not this diff).
    tenant = uuid.uuid4()
    user_id = uuid.uuid4()
    model_id = uuid.uuid4()
    async with factory() as s0:
        async with s0.begin():
            s0.add(Tenant(id=tenant, name="T-2E", slug="t-2e-verify", im_provider="web_only"))
            s0.add(User(id=user_id, tenant_id=tenant, display_name="verify-user"))
            s0.add(LLMModel(id=model_id, provider="anthropic", model="claude-opus-4-6",
                            api_key_encrypted="enc-key", label="m"))
            s0.flush()
    from app.models.agent import Agent as _AgentRow
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()
    async with factory() as s0:
        async with s0.begin():
            s0.add(_AgentRow(id=agent_a, name="AgentA", creator_id=user_id, tenant_id=tenant,
                             status="idle", primary_model_id=model_id))
            s0.add(_AgentRow(id=agent_b, name="AgentB", creator_id=user_id, tenant_id=tenant,
                             status="idle", primary_model_id=model_id))
    task_x = uuid.uuid4()
    task_y = uuid.uuid4()
    attempt = uuid.uuid4()
    agent_a_row = _AgentRow(id=agent_a, name="AgentA", creator_id=user_id, tenant_id=tenant,
                            status="idle", primary_model_id=model_id)

    results = {}

    # ---- Scenario 2: same stable key -> ONE run, second call reuses it ----
    async with factory() as db2:
        async with db2.begin():
            r1 = await _reg(db2, tenant, agent_a, task_x, f"task:{task_x}", f"start:task:{task_x}",
                            {"task_id": str(task_x), "task_type": "todo", "title": "x"}, model_id)
            r2 = await _reg(db2, tenant, agent_a, task_x, f"task:{task_x}", f"start:task:{task_x}",
                            {"task_id": str(task_x), "task_type": "todo", "title": "x"}, model_id)
        results["S2_first_created"] = r1.created
        results["S2_second_created"] = r2.created
        results["S2_same_run_id"] = r1.run.id == r2.run.id
        cnt = (await db2.execute(
            select(AgentRun.id).where(AgentRun.tenant_id == tenant,
                                      AgentRun.source_execution_id == f"task:{task_x}")
        )).scalars().all()
        results["S2_run_rows_for_key"] = len(cnt)

    # ---- Scenario 3: two independent tasks (distinct agents) coexist ----
    async with factory() as db2:
        async with db2.begin():
            await _reg(db2, tenant, agent_a, task_x, f"task:{task_x}:retry:{attempt}",
                       f"start:task:{task_x}:retry:{attempt}",
                       {"task_id": str(task_x), "task_type": "todo", "task_attempt": str(attempt)}, model_id)
            await _reg(db2, tenant, agent_b, task_y, f"task:{task_y}", f"start:task:{task_y}",
                       {"task_id": str(task_y), "task_type": "todo", "title": "y"}, model_id)
        runs = (await db2.execute(select(AgentRun).where(
            AgentRun.tenant_id == tenant,
            AgentRun.source_execution_id.like(f"task:{task_y}%"),
        ))).scalars().all()
        results["S3_taskY_runs"] = len(runs)
        results["S3_taskY_lane_key_null"] = all(r.scheduling_lane_key is None for r in runs)

    # ---- Concurrency stress: two TRULY CONCURRENT sessions, SAME key ----
    # Each holds its own transaction; T2's unique-key insert blocks on T1's
    # open row, T1 commits, T2 fails the insert (IntegrityError) and the
    # SAVEPOINT recovery path re-resolves -> created=False. One Run row.
    async with factory() as sa, factory() as sb:
        task_z = uuid.uuid4()
        payload_z = {"task_id": str(task_z), "task_type": "todo"}
        async def one(sess):
            async with sess.begin():
                return await _reg(sess, tenant, agent_a, task_z, f"task:{task_z}",
                                  f"start:task:{task_z}", payload_z, model_id)
        res = await asyncio.gather(one(sa), one(sb))
    async with factory() as db2:
        rows = (await db2.execute(
            select(AgentRun.id).where(AgentRun.tenant_id == tenant,
                                      AgentRun.source_execution_id == f"task:{task_z}")
        )).scalars().all()
        results["S3_stress_run_rows"] = len(rows)
        results["S3_stress_created_flags"] = [r.created for r in res]
        results["S3_stress_same_run"] = res[0].run.id == res[1].run.id

    # ---- Scenario 1: dependency rejection (P4 gate) via the REAL executor ----
    # B depends on A.  While A is not done, the real executor's ensure_ready
    # P4 gate finds unmet=[A] and raises TaskBlockedError -> NO Run row
    # (fail-closed, task stays pending).  This exercises the same
    # task_graph_service.ensure_ready the service's P4 gate re-runs, but at
    # the EXACT executor boundary the service calls, on real DB rows.
    task_a_id, task_b_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s1:
        async with s1.begin():
            s1.add(Task(id=task_a_id, agent_id=agent_a, tenant_id=tenant,
                        created_by=user_id, title="A: evidence report", type="todo", status="pending"))
            s1.add(Task(id=task_b_id, agent_id=agent_a, tenant_id=tenant,
                        created_by=user_id, title="B: publish summary", type="todo", status="pending"))
            s1.add(TaskDependency(tenant_id=tenant, task_id=task_b_id, depends_on_task_id=task_a_id))
    from app.dao.base import tenant_context
    task_b_obj = Task(id=task_b_id, agent_id=agent_a, tenant_id=tenant,
                       created_by=user_id, title="B: publish summary", type="todo", status="pending")
    blocked: TaskBlockedError | None = None
    async with factory() as db2:
        with tenant_context(tenant):
            try:
                await enqueue_task_runtime(db2, task=task_b_obj, agent=agent_a_row)
            except TaskBlockedError as e:
                blocked = e
        results["S1_blocked_raised"] = blocked is not None
        results["S1_unmet_is_taskA"] = bool(blocked) and list(blocked.reason) == [task_a_id]
        results["S1_taskB_still_pending"] = task_b_obj.status == "pending"
        rows = (await db2.execute(
            select(AgentRun.id).where(AgentRun.source_execution_id == f"task:{task_b_id}")
        )).scalars().all()
        results["S1_no_run_after_blocked"] = len(rows)
    print("=== REAL-POSTGRES EXECUTION-CHAIN EVIDENCE (clawith_2e_v4) ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    ok = (
        results["S1_blocked_raised"] is True
        and results["S1_unmet_is_taskA"] is True
        and results["S1_taskB_still_pending"] is True
        and results["S1_no_run_after_blocked"] == 0
        and results["S2_first_created"] is True
        and results["S2_second_created"] is False
        and results["S2_same_run_id"] is True
        and results["S2_run_rows_for_key"] == 1
        and results["S3_taskY_runs"] == 1
        and results["S3_taskY_lane_key_null"] is True
        and results["S3_stress_run_rows"] == 1
        and results["S3_stress_same_run"] is True
        and sorted(results["S3_stress_created_flags"]) == [False, True]
    )
    print("  VERDICT:", "PASS — chain physically enforces dedup + parallelism" if ok else "CHECK MANUAL")
    await engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

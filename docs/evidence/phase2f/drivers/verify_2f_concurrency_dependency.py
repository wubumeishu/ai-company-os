"""Phase 2F — VERIFY CONCURRENT Execution & Dependency Chains (task t_edbd5f78).

Builds on the proven t_45477a14 driver (`verify_2f_real_llm_run.py`): the same
scratch-Postgres isolation seams, the real Phase-2E intake
(`enqueue_task_runtime`), the real `RuntimeCommandWorker.run_once()` loop, the
host-portable sandbox (documented win32 artifact), and a REAL LLM
(`agnes-3.0-flash` via ambient AGNES_API_KEY + AGNES_BASE_URL, NO mocks).

What this driver exercises (root t_4910b73e §11 concurrent + §12 dependency):

  * PARALLEL independent tasks  D, E, F  (no dependencies -> ready at t0).
  * DEPENDENT chain             A -> B -> C  (B blocks on A, C blocks on B).
  * Simultaneous execution       -> observe MULTIPLE Runs in a "running" state
    at the same moment (true concurrency, not DB-simulated parallelism).
  * Dependency gating           -> B and C have NO Run at t0 (the Phase-2D
    execution gate `ensure_ready` refuses to enqueue them while their direct
    dependency is not `done`); they are explicitly re-enqueued ONLY after the
    upstream settles `done` (the documented V1 re-trigger), so B waits for A
    and C waits for B.
  * Workspace isolation under concurrent load -> each task writes a distinct
    file; all 6 output files coexist with their own content (no clobbering),
    each backed by its own WorkspaceFileRevision.
  * Per-branch execution evidence -> for ALL six branches: an AgentRun row, a
    terminal lifecycle event, >=1 real tool execution, and a workspace
    revision.

Design notes (why the shape is what it is):

  * The claim queue uses `SELECT ... FOR UPDATE SKIP LOCKED` (persistence.py
    `_claim_statement`). N worker instances with distinct claimants therefore
    claim DISTINCT pending start-commands in parallel -> genuine multi-Run
    in-flight execution. Task intake sets no `scheduling_lane_key`, so
    `_acquire_start_lane` returns True for every task Run (no lane
    serialization) and concurrency is bounded only by the worker pool.
  * The Phase-2D gate blocks at ENQUEUE time. V1 does not auto-re-trigger a
    downstream dependent when an upstream settles; the documented mechanism is
    an explicit re-Execute. This driver plays that role deterministically:
    wait for A terminal -> enqueue B; wait for B terminal -> enqueue C.

Run:
    cd backend && uv run --no-sync python scripts/verify_2f_concurrency_dependency.py

Environment (the ONLY external inputs):
    AGNES_API_KEY    real LLM credential (OpenAI-compatible)
    AGNES_BASE_URL   e.g. https://apihub.agnes-ai.com/v1
    CONCURRENCY_WORKERS  (optional) number of concurrent command workers,
                         default 4
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── external inputs (the real LLM credential) ────────────────────────────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")
NUM_WORKERS = int(os.environ.get("CONCURRENCY_WORKERS", "4"))
GLOBAL_DEADLINE_S = float(os.environ.get("CONCURRENCY_DEADLINE_S", "600"))
CMD_SLEEP_S = int(os.environ.get("CMD_SLEEP_S", "3"))  # widens the overlap window

if not AGNES_API_KEY:
    print("FATAL: AGNES_API_KEY not set — a REAL LLM concurrency run needs a real key.")
    sys.exit(2)

# ── isolated scratch coordinates ─────────────────────────────────────────────
SCRATCH_DB = f"clawith_2f_cc_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-cc-secret"
SCRATCH_WS = f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_cc_ws_{uuid.uuid4().hex[:6]}"

# Point the global engine + checkpointer + secret at scratch BEFORE any `app.*`
# import reads settings. This scopes the whole run to scratch state.
os.environ["DATABASE_URL"] = SCRATCH_DB_URL
os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = SCRATCH_DB_URL
os.environ["SECRET_KEY"] = SCRATCH_SECRET
os.environ["JWT_SECRET_KEY"] = SCRATCH_SECRET
os.environ["AGENT_DATA_DIR"] = SCRATCH_WS
os.environ["STORAGE_LOCAL_ROOT"] = SCRATCH_WS
os.environ.setdefault("PROCESS_ROLE", "worker")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import importlib  # noqa: E402
import json  # noqa: E402

# psycopg's async driver cannot use the Windows ProactorEventLoop; install the
# selector policy BEFORE any async work (psycopg-specific host requirement).
if sys.platform == "win32":
    import asyncio as _aio
    _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())

# Register every model module so the full Base.metadata is present for
# create_all + checkpointer setup (same as the 2F real-run driver).
for _m in os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "models")):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

from app.config import get_settings  # noqa: E402
from app.core.security import encrypt_data  # noqa: E402
from app.database import Base, async_session, create_async_engine, engine  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.agent_run_command import AgentRunCommand  # noqa: E402
from app.models.agent_run_event import AgentRunEvent  # noqa: E402
from app.models.agent_tool_execution import AgentToolExecution  # noqa: E402
from app.models.llm import LLMModel  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.workspace import WorkspaceFileRevision  # noqa: E402
from app.services import agent_tools, workspace_collaboration as wcs  # noqa: E402
from app.services.storage_runtime.local import LocalStorageBackend  # noqa: E402
from sqlalchemy import func, select, text as sa_text  # noqa: E402

settings = get_settings()
STORAGE = LocalStorageBackend(SCRATCH_WS)

_TERMINAL = ("run_completed", "run_failed", "run_cancelled")


# ── host-portable command runner (documented win32 host artifact) ────────────
class _HostPortableSandbox:
    """SubprocessBackend stand-in for a win32 host (documented artifact).

    The container `SubprocessBackend` uses bubblewrap + `preexec_fn`
    (Unix-only) and cannot spawn on Windows. This runner executes the exact
    host subprocess the LLM requested, returning a REAL `ExecutionResult`
    with real stdout/stderr/exit_code. It is a host I/O artifact, NOT a mock
    of the LLM or the tool plumbing (reservation, normalization, result store,
    ledger, verification all remain the real runtime code).
    """

    name = "subprocess-hostportable"

    def _build(self, language, code, work_dir):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="clawith_2f_cc_hostexec_"))
        script = tmp / ("main.py" if language == "python" else "main.sh")
        script.write_text(code, encoding="utf-8")
        if language == "python":
            argv = [sys.executable, "-I", "-B", str(script)]
        else:
            argv = ["bash", "--noprofile", "--norc", str(script)]
        return tmp, argv

    async def execute(self, code, language, timeout=30, work_dir=None, **_kw):
        from app.services.sandbox.base import ExecutionResult
        import subprocess

        language = language or "python"
        t0 = time.time()
        try:
            tmp, argv = self._build(language, code, work_dir)

            def _spawn():
                return subprocess.run(
                    argv, cwd=str(tmp), capture_output=True, text=True, timeout=timeout,
                )

            # `asyncio.create_subprocess_exec` is Proactor-loop only and raises
            # under the WindowsSelectorEventLoopPolicy that psycopg-async
            # requires. Offloading `subprocess.run` to a thread works under the
            # selector loop AND still spawns a REAL host subprocess.
            try:
                cp = await asyncio.wait_for(asyncio.to_thread(_spawn), timeout=timeout + 10)
            except asyncio.TimeoutError:
                return ExecutionResult(False, "", "command_timeout", 124,
                                       int((time.time() - t0) * 1000), "command_timeout")
            return ExecutionResult(
                success=cp.returncode == 0,
                stdout=cp.stdout[:20000],
                stderr=cp.stderr[:10000],
                exit_code=cp.returncode if cp.returncode is not None else 0,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as exc:  # host spawn failure -> captured, not crash
            return ExecutionResult(False, "", str(exc), 1,
                                   int((time.time() - t0) * 1000), f"host_spawn_failed: {exc}")

    async def health_check(self) -> bool:
        return True

    def get_capabilities(self):
        from app.services.sandbox.base import SandboxCapabilities
        return SandboxCapabilities(["python", "bash"], 300, 256, True, True)


def _install_isolation() -> None:
    """Point tool storage + workspace + locks + sandbox at scratch (no live DB/Redis)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(*_a, **_k):
        yield

    for mod in (agent_tools, wcs):
        try:
            mod.workspace_locks = _cm
        except Exception:
            pass
    agent_tools.get_storage_backend = lambda: STORAGE
    wcs.get_storage_backend = lambda: STORAGE
    agent_tools.WORKSPACE_ROOT = Path(SCRATCH_WS)
    import app.services.sandbox.registry as registry
    host = _HostPortableSandbox()
    registry.get_sandbox_backend = lambda cfg: host
    if hasattr(agent_tools, "get_sandbox_backend"):
        agent_tools.get_sandbox_backend = lambda cfg: host


# ── the per-task goal (carried in task.description so the model is forced onto
#    a deterministic, minimal, safe 2-tool-call script) ───────────────────────
def _task_goal(label: str) -> str:
    out = f"output_{label}.txt"
    marker = f"MARK_{label}"
    return (
        "这是一个确定性两步任务, 只调用且恰好调用下面两个工具, 不要调用任何其他工具"
        "(不要 list_files / read_file / query_directory), 每个工具只调用一次, 不要重复、"
        "不要重试、不要向我提问、不要等待确认、不要用纯文本总结代替工具调用。\n"
        f"第1步: 调用 write_file, 参数 {{\"path\":\"workspace/{out}\",\"content\":\"{marker}\"}}。\n"
        f"第2步: 调用 execute_code, 参数 language=python, code=下面这段Python(写一行stdout并以0退出, 立即返回):\n"
        f"```python\nprint('{marker}_CMD_OK')\n```\n"
        f"两步各调用一次执行完, 最后用一句话汇报: 你写了 {out} (内容={marker}), 该命令真实 stdout 是 {marker}_CMD_OK。"
    )


def _ddl(q: str):
    return sa_text(q)


# ── concurrency monitor (read-only): samples how many Runs are in-flight ─────
async def _snapshot_runs(run_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Return {run_id: phase} where phase in {not_started, running, terminal}.

    A run is "running" (actively executing) when its start command is in the
    ``claimed`` state: the worker claims it, holds it for the run's in-flight
    duration via ``renew_command_claim`` (claim TTL 60s, renew 20s), and flips
    it to ``applied`` only at the terminal checkpoint. ``run_created`` fires at
    INTAKE (before any worker claims it), so it is NOT a "running" signal.
    Terminal is the authoritative terminal lifecycle event.
    """
    phases: dict[uuid.UUID, str] = {r: "not_started" for r in run_ids}
    if not run_ids:
        return phases
    try:
        async with async_session() as s:
            cmds = (await s.execute(
                select(AgentRunCommand.run_id, AgentRunCommand.command_type, AgentRunCommand.status)
                .where(
                    AgentRunCommand.run_id.in_(run_ids),
                    AgentRunCommand.command_type == "start",
                )
            )).all()
            evs = (await s.execute(
                select(AgentRunEvent.run_id, AgentRunEvent.event_type)
                .where(
                    AgentRunEvent.run_id.in_(run_ids),
                    AgentRunEvent.event_type.in_(_TERMINAL),
                )
            )).all()
    except Exception as exc:  # a transient DB blip must not kill the monitor
        print(f"  [monitor] snapshot read failed: {exc}")
        return phases
    terminal = {rid for rid, _et in evs}
    claimed = {rid for rid, _ct, st in cmds if st == "claimed"}
    for rid in run_ids:
        if rid in terminal:
            phases[rid] = "terminal"
        elif rid in claimed:
            phases[rid] = "running"
        else:
            phases[rid] = "not_started"
    return phases


async def _run_monitor(
    known_runs: set[uuid.UUID],
    deadline: float,
    trace: list,
    stop_evt: asyncio.Event,
    conc_stats: dict,
) -> None:
    """Poll the DB; record the peak number of simultaneously-running Runs.

    ``known_runs`` is the live set (mutated by the chain driver as B/C get
    enqueued). A run counts as "running" only while its start command is held
    in the ``claimed`` state (the worker releases it only at the terminal
    checkpoint), so this is a true concurrency signal, not the intake-time
    ``run_created`` event. ``conc_stats`` is updated every sample, so the peak
    survives even if this task is cancelled early by the main driver.
    """
    peak = 0
    peak_runs: list[uuid.UUID] = []
    samples = 0
    conc_stats["peak"] = 0
    conc_stats["peak_runs"] = []
    conc_stats["samples"] = 0
    while time.time() < deadline and not stop_evt.is_set():
        phases = await _snapshot_runs(list(known_runs))
        running = [r for r in phases.values() if phases[r] == "running"]
        now = datetime.now(timezone.utc)
        trace.append({"ts": now.isoformat(), "running": [str(r) for r in running], "count": len(running)})
        samples += 1
        if len(running) > peak:
            peak = len(running)
            peak_runs = running
        conc_stats["peak"] = peak
        conc_stats["peak_runs"] = [str(r) for r in peak_runs]
        conc_stats["samples"] = samples
        await asyncio.sleep(0.4)


async def main() -> int:
    # 1. scratch Postgres DB (fresh, isolated, disposable)
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(_ddl(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin.dispose()

    # 2. product schema (idempotent)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    # 3. checkpoint schema (LangGraph) -> must reach the pinned migration
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import assert_runtime_schema_ready

    saver_ctx = create_checkpointer(settings)
    async with saver_ctx as saver:
        await saver.setup()
    await assert_runtime_schema_ready(engine, settings=settings)

    # 4. seed FK-parent rows in sequence
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    project = uuid.uuid4()

    async with async_session() as s, s.begin():
        s.add(Tenant(id=tenant, name="T-2F-CC", slug=f"t2f-cc-{tenant.hex[:8]}", im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name="2f-cc-user"))
    async with async_session() as s, s.begin():
        s.add(LLMModel(
            id=model, tenant_id=tenant,
            provider="openai",
            model=AGNES_MODEL,
            api_key_encrypted=encrypt_data(AGNES_API_KEY, SCRATCH_SECRET),
            base_url=AGNES_BASE_URL,
            label=f"2f-cc-{AGNES_MODEL}",
            enabled=True,
            supports_vision=False,
            supports_tool_calling=True,
            request_timeout=120,
            max_output_tokens=2048,
            context_window_tokens=32768,
        ))
    async with async_session() as s, s.begin():
        s.add(Agent(
            id=agent, tenant_id=tenant, name="ConcAgent", creator_id=user,
            agent_type="native", status="idle", primary_model_id=model,
            is_system=False, access_mode="company", company_access_level="use",
            expires_at=None, is_expired=False,
            # Bound the per-run model-turn budget so a pathological tool-retry
            # loop (observed: a run firing ~29 execute_code calls) terminates in
            # bounded time and every branch reaches a terminal event within the
            # global deadline. 20 turns comfortably covers the 2-tool task
            # (write_file + execute_code + report) plus a few retries.
            max_tool_rounds=20,
        ))
    # Dependency edges require a shared Project (graph service §5.1 project guard).
    async with async_session() as s, s.begin():
        s.add(Project(id=project, tenant_id=tenant, created_by=user, name="2f-cc-project", status="RECEIVED"))

    # 4a. seed the task-relevant tool set so the model is offered write_file +
    #     execute_code (without Tool rows the loader falls back to _always_tools).
    from app.models.tool import Tool, AgentTool
    from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy

    ENABLE_TOOLS = ["write_file", "execute_code", "list_files"]
    SUPPRESS_TOOLS = ["read_file", "list_focus_items", "query_directory"]
    tool_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        for name in ENABLE_TOOLS + SUPPRESS_TOOLS:
            tid = uuid.uuid4()
            tool_ids[name] = tid
            d = builtin_model_definition(name)
            fn = d.get("function", {})
            try:
                pol = builtin_policy(name) or {}
            except Exception:
                pol = {}
            s.add(Tool(
                id=tid, name=name,
                display_name=fn.get("display_name", name) or name,
                description=fn.get("description", ""),
                type="builtin", category=fn.get("category", "general"),
                icon="🔧", parameters_schema=fn.get("parameters", {}),
                config=pol.get("config", {}) if isinstance(pol, dict) else {},
                source="builtin", enabled=True, is_default=True,
            ))
        await s.flush()
        for name in ENABLE_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=True, source="system"))
        for name in SUPPRESS_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=False, source="system"))

    # 4b. the six tasks: chain A->B->C + independent D/E/F
    LABELS = ["A", "B", "C", "D", "E", "F"]
    task_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        for lab in LABELS:
            tid = uuid.uuid4()
            task_ids[lab] = tid
            s.add(Task(
                id=tid, tenant_id=tenant, agent_id=agent,
                title=f"2f-cc:{lab}",
                description=_task_goal(lab),
                type="todo", status="pending", priority="medium",
                assignee="self", created_by=user, project_id=project,
            ))
        # Dependency chain: B depends on A, C depends on B (arrow -> upstream).
        from app.models.task import TaskDependency
        s.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant, task_id=task_ids["B"], depends_on_task_id=task_ids["A"]))
        s.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant, task_id=task_ids["C"], depends_on_task_id=task_ids["B"]))

    # 5. isolation seams + shared checkpointer
    _install_isolation()
    saver2_ctx = create_checkpointer(settings)

    print(f"=== Phase 2F CONCURRENT Execution & Dependency Chains ({SCRATCH_DB}) ===")
    print(f"  llm endpoint : {AGNES_BASE_URL} (model={AGNES_MODEL}, provider=openai)")
    print(f"  scratch db   : {SCRATCH_DB}")
    print(f"  scratch ws   : {SCRATCH_WS}")
    print(f"  agent        : {agent}  project={project}")
    print(f"  tasks        : { {k: str(v)[:8] for k, v in task_ids.items()} }")
    print(f"  workers      : {NUM_WORKERS} concurrent command workers")

    # 6. INTAKE all six tasks at t0 through the REAL Phase-2E gate.
    #    Expected: A/D/E/F enqueue (ready), B/C BLOCKED (unmet deps) -> no Run.
    from app.services.task_executor import enqueue_task_runtime, TaskBlockedError

    intake: dict[str, dict] = {}
    async with saver2_ctx as saver2:
        await saver2.setup()
        deadline = time.time() + GLOBAL_DEADLINE_S

        run_ids_known: set[uuid.UUID] = set()
        run_ids_by_task: dict[str, uuid.UUID | None] = {lab: None for lab in LABELS}

        async with async_session() as s, s.begin():
            agent_row = (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()
            for lab in LABELS:
                task = (await s.execute(select(Task).where(Task.id == task_ids[lab]))).scalar_one()
                try:
                    handle = await enqueue_task_runtime(
                        s, task=task, agent=agent_row,
                        execution_id=uuid.uuid4(), actor_user_id=user,
                    )
                    run_id = handle.run_id if handle else None
                    blocked = False
                    block_log = None
                except TaskBlockedError as be:
                    run_id, blocked, block_log = None, True, ", ".join(str(u) for u in be.reason)
                run_ids_by_task[lab] = run_id
                if run_id:
                    run_ids_known.add(run_id)
                intake[lab] = {"run_id": str(run_id) if run_id else None, "blocked": blocked,
                               "block_reason": block_log, "status": task.status}
                print(f"  intake {lab}: {'BLOCKED(no run, unmet=' + str(block_log) + ')' if blocked else 'enqueued run=' + str(run_id)[:8]}  task.status={task.status}")

        # 7. Spin up N concurrent workers + the read-only monitor + the chain
        #    re-trigger, all as parallel asyncio tasks.
        from app.services.agent_runtime.worker_service import build_runtime_worker_components

        known_runs: set[uuid.UUID] = set(run_ids_known)  # grows as B/C are enqueued
        trace: list = []
        stop_evt = asyncio.Event()
        conc_stats: dict = {"peak": 0, "peak_runs": [], "samples": 0}
        re_enqueue_ts: dict[str, datetime] = {}
        task_done_ts: dict[str, datetime] = {}
        # "B was blocked" evidence captured at t0: B and C had NO run at intake.
        b_no_run_at_t0 = run_ids_by_task.get("B") is None
        c_no_run_at_t0 = run_ids_by_task.get("C") is None

        worker_components = [
            build_runtime_worker_components(
                checkpointer=saver2,
                session_factory=async_session,
                lock_engine=engine,
                claimant=f"cc-w{i}-{uuid.uuid4().hex[:6]}",
                settings=settings,
            )
            for i in range(NUM_WORKERS)
        ]

        async def worker_loop(comp) -> None:
            while time.time() < deadline and not stop_evt.is_set():
                res = await comp.worker.run_once()
                st = getattr(res, "status", None)
                # idle/retry -> poll; settled commands -> brief drain pause.
                await asyncio.sleep(0.15 if st in ("applied", "reconciled", "rejected") else 0.3)

        async def _wait_task_done(tid: uuid.UUID, label: str) -> bool:
            """Poll until the Task row reads 'done' (settlement post-checkpoint)."""
            while time.time() < deadline:
                async with async_session() as s:
                    t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
                if t is not None and t.status == "done":
                    task_done_ts[label] = datetime.now(timezone.utc)
                    return True
                await asyncio.sleep(0.4)
            print(f"  [chain] WARN: task {label} never reached done within deadline")
            return False

        async def _re_enqueue(lab: str) -> uuid.UUID | None:
            """Explicit V1 re-Execute of a dependency-blocked task (the re-trigger)."""
            async with async_session() as s, s.begin():
                task = (await s.execute(select(Task).where(Task.id == task_ids[lab]))).scalar_one()
                agent_row = (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()
                try:
                    handle = await enqueue_task_runtime(
                        s, task=task, agent=agent_row,
                        execution_id=uuid.uuid4(), actor_user_id=user,
                    )
                except TaskBlockedError:
                    print(f"  [chain] re-enqueue {lab} STILL blocked (upstream not done)")
                    return None
                re_enqueue_ts[lab] = datetime.now(timezone.utc)
                return (handle.run_id if handle else None)

        async def chain_driver() -> None:
            """Drive the A->B->C chain: re-trigger B after A done, C after B done.

            D/E/F were enqueued at t0 and run in parallel with A. The gate
            (§5.4) refused to create a Run for B/C while their upstream was not
            done, so the only way B/C execute is an explicit re-Execute once the
            upstream settles — this driver plays exactly that role.
            """
            if not await _wait_task_done(task_ids["A"], "A"):
                pass  # A failed -> B stays blocked (correct gate behavior); verdict fails
            print("  [chain] A task done; re-enqueueing B (gate now sees A=done)")
            b_run = await _re_enqueue("B")
            run_ids_by_task["B"] = b_run
            if b_run:
                known_runs.add(b_run)

            if b_run and not await _wait_task_done(task_ids["B"], "B"):
                pass
            print("  [chain] B task done; re-enqueueing C (gate now sees B=done)")
            c_run = await _re_enqueue("C")
            run_ids_by_task["C"] = c_run
            if c_run:
                known_runs.add(c_run)

            # Wait for C + all independent D/E/F to settle before stopping.
            if c_run:
                await _wait_task_done(task_ids["C"], "C")
            for lab in ("D", "E", "F"):
                rid = run_ids_by_task.get(lab)
                if rid:
                    await _wait_task_done(task_ids[lab], lab)
            stop_evt.set()

        worker_tasks = [asyncio.create_task(worker_loop(c)) for c in worker_components]
        monitor_task = asyncio.create_task(_run_monitor(known_runs, deadline, trace, stop_evt, conc_stats))
        chain_task = asyncio.create_task(chain_driver())

        # Workers keep draining pending claims while the chain driver advances
        # B and C; D/E/F complete in parallel with A.
        await chain_task
        stop_evt.set()
        for wt in worker_tasks:
            wt.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        if not monitor_task.done():
            monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)

        # 8. Collect per-branch evidence.
        evidence = await _collect_evidence(run_ids_by_task, task_ids, agent, model, tenant)
        evidence["intake"] = intake
        evidence["b_no_run_at_t0"] = b_no_run_at_t0
        evidence["c_no_run_at_t0"] = c_no_run_at_t0
        evidence["re_enqueue_ts"] = {k: v.isoformat() for k, v in re_enqueue_ts.items()}
        evidence["task_done_ts"] = {k: v.isoformat() for k, v in task_done_ts.items()}
        evidence["run_ids_by_task"] = {k: str(v) if v else None for k, v in run_ids_by_task.items()}

        # 9. concurrency evidence: the resilient live-sampler peak PLUS an
        #    authoritative overlap proof computed from persisted timestamps
        #    (independent of the sampler, so a racy monitor can't void it).
        exec_windows = await _collect_exec_windows(run_ids_by_task, agent)  # {label: (start,end)}
        overlap = _pairwise_overlap(exec_windows, labels_present(exec_windows))
        monitor_peak = conc_stats.get("peak", 0)
        evidence["concurrency"] = {
            "num_workers": NUM_WORKERS,
            "peak_simultaneous_running": monitor_peak,
            "peak_runs": conc_stats.get("peak_runs", []),
            "samples": conc_stats.get("samples", 0),
            "trace_sampled": trace[:200],
            "exec_windows": {k: [s.isoformat(), e.isoformat()] for k, (s, e) in exec_windows.items()},
            "overlapping_pairs": overlap,
            "authoritative_concurrent_pairs": [p for p in overlap],
        }
        evidence["scratch_db"] = SCRATCH_DB
        evidence["llm"] = {"base_url": AGNES_BASE_URL, "provider": "openai", "model": AGNES_MODEL,
                           "real_http": True, "no_mock": True}
        evidence["host_artifact"] = (
            "container SubprocessBackend (bwrap+preexec_fn) is Unix-only; one-shot host "
            "subprocess stand-in used for the command primitive (documented; stdout/stderr/exit_code REAL)"
        )

        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHASE_2F_CONCURRENCY_DEP_EVIDENCE.json")
        Path(out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        print("\n=== EVIDENCE (per-branch) ===")
        for lab in LABELS:
            b = evidence["branches"].get(lab, {})
            print(f"  {lab}: status={b.get('task_status')} terminal={b.get('run_terminal_event')} "
                  f"tools={b.get('tool_count')} rev={b.get('has_revision')} file_ok={b.get('file_isolated')}")
        verdict, reasons = _verdict(evidence, b_no_run_at_t0, c_no_run_at_t0)
        print("\n=== VERDICT ===")
        print(f"  {'PASS' if verdict else 'BLOCKED'}  peak_running={monitor_peak}  authoritative_pairs={len(overlap)}")
        for r in reasons:
            print(f"   - {r}")
        return 0 if verdict else 3


# ── evidence collection ───────────────────────────────────────────────────────
async def _collect_exec_windows(run_ids_by_task, agent) -> dict:
    """Authoritative per-run execution windows from PERSISTED timestamps.

    Independent of the live sampler: a run's in-flight window is [earliest
    tool-start, terminal-event]. Tools only execute while a worker is running
    the Run, so two runs whose windows overlap were concurrently in-flight.
    Returns {label: (start_dt, end_dt)} for every run that actually has a
    start/end to compare.
    """
    pairs: list[tuple[str, uuid.UUID]] = [(lab, rid) for lab, rid in run_ids_by_task.items() if rid]
    if not pairs:
        return {}
    run_ids = [rid for _lab, rid in pairs]
    label_of = {rid: lab for lab, rid in pairs}
    windows: dict[uuid.UUID, tuple[datetime, datetime]] = {}
    async with async_session() as s:
        first_tools = (await s.execute(
            select(AgentToolExecution.run_id, func.min(AgentToolExecution.started_at))
            .where(AgentToolExecution.run_id.in_(run_ids))
            .group_by(AgentToolExecution.run_id)
        )).all()
        created = (await s.execute(
            select(AgentRunEvent.run_id, func.min(AgentRunEvent.created_at))
            .where(AgentRunEvent.run_id.in_(run_ids))
            .group_by(AgentRunEvent.run_id)
        )).all()
        terminals = (await s.execute(
            select(AgentRunEvent.run_id, func.max(AgentRunEvent.created_at)).where(
                AgentRunEvent.run_id.in_(run_ids),
                AgentRunEvent.event_type.in_(_TERMINAL),
            ).group_by(AgentRunEvent.run_id)
        )).all()
        any_latest = (await s.execute(
            select(AgentRunEvent.run_id, func.max(AgentRunEvent.created_at))
            .where(AgentRunEvent.run_id.in_(run_ids))
            .group_by(AgentRunEvent.run_id)
        )).all()
    created_map = dict(created)
    terminal_map = dict(terminals)
    latest_map = dict(any_latest)
    first_tool_map = dict(first_tools)
    for rid in run_ids:
        start = first_tool_map.get(rid, created_map.get(rid))
        end = terminal_map.get(rid, latest_map.get(rid))
        if start is None or end is None:
            continue
        windows[rid] = (start, end)
    return {label_of[rid]: w for rid, w in windows.items()}


def labels_present(d: dict) -> list:
    return list(d.keys())


def _pairwise_overlap(windows: dict, labels: list) -> list:
    """All label pairs whose execution windows overlap (authoritative proof)."""
    pairs = []
    labs = [l for l in labels if l in windows]
    for i in range(len(labs)):
        for j in range(i + 1, len(labs)):
            a, b = labs[i], labs[j]
            sa, ea = windows[a]
            sb, eb = windows[b]
            if sa < eb and sb < ea:  # classic interval overlap
                pairs.append(f"{a}x{b}")
    return pairs


async def _collect_evidence(run_ids_by_task, task_ids, agent, model, tenant) -> dict:
    ev: dict = {"branches": {}}
    all_run_ids = [r for r in run_ids_by_task.values() if r is not None]
    all_task_ids = list(task_ids.values())

    async with async_session() as s:
        tasks = (await s.execute(select(Task).where(Task.id.in_(all_task_ids)))).scalars().all()
        task_by_id = {t.id: t for t in tasks}
        run_rows = (await s.execute(select(AgentRun.id).where(AgentRun.id.in_(all_run_ids)))).scalars().all()
        run_exists = set(run_rows)
        cmds = (await s.execute(select(AgentRunCommand).where(AgentRunCommand.run_id.in_(all_run_ids)))).scalars().all()
        tools = (await s.execute(
            select(AgentToolExecution).where(AgentToolExecution.run_id.in_(all_run_ids)).order_by(AgentToolExecution.started_at)
        )).scalars().all()
        events = (await s.execute(
            select(AgentRunEvent).where(AgentRunEvent.run_id.in_(all_run_ids)).order_by(AgentRunEvent.created_at)
        )).scalars().all()
        revs = (await s.execute(
            select(WorkspaceFileRevision).where(
                WorkspaceFileRevision.scope_type == "agent",
                WorkspaceFileRevision.scope_id == agent,
            )
        )).scalars().all()

    # group by run
    def by_run(rows, key):
        m: dict = {}
        for r in rows:
            m.setdefault(getattr(r, key), []).append(r)
        return m

    tools_by_run = by_run(tools, "run_id")
    cmds_by_run = by_run(cmds, "run_id")
    events_by_run = by_run(events, "run_id")
    rev_paths = [r.path for r in revs]

    for lab in ["A", "B", "C", "D", "E", "F"]:
        rid = run_ids_by_task.get(lab)
        tid = task_ids[lab]
        task = task_by_id.get(tid)
        expected_file = f"output_{lab}.txt"
        expected_marker = f"MARK_{lab}"
        run_terminal = None
        run_created = None
        for e in events_by_run.get(rid, []):
            if e.event_type in _TERMINAL:
                run_terminal = e.event_type
            if e.event_type == "run_created":
                run_created = e.event_type
        tl = tools_by_run.get(rid, [])
        write_tools = [t for t in tl if t.tool_name == "write_file"]
        cmd_tools = [t for t in tl if t.tool_name == "execute_code"]
        # byte-truthful isolation: read the file back from scratch storage.
        file_ok = False
        file_marker_ok = False
        try:
            blob = await STORAGE.read_bytes(f"{agent}/workspace/{expected_file}")
            file_ok = blob is not None and len(blob) > 0
            file_marker_ok = blob is not None and expected_marker.encode() in blob
        except Exception:
            file_ok = False
        has_rev = any(expected_file in p for p in rev_paths)
        ev["branches"][lab] = {
            "task_status": task.status if task else None,
            "run_id": str(rid) if rid else None,
            "run_row_exists": bool(rid) and rid in run_exists,
            "run_created_event": run_created is not None,
            "run_terminal_event": run_terminal,
            "command_status": [c.status for c in cmds_by_run.get(rid, [])],
            "tool_count": len(tl),
            "write_file_ok": any(w.status == "succeeded" for w in write_tools),
            "execute_code_present": bool(cmd_tools),
            "execute_code_marker": any(f"MARK_{lab}_CMD_OK" in (t.result_summary or "") for t in cmd_tools),
            "has_revision": has_rev,
            "file_isolated": file_ok,
            "file_marker_ok": file_marker_ok,
            "tools": [{"tool": t.tool_name, "status": t.status} for t in tl],
        }
    ev["workspace_revision_paths"] = sorted(rev_paths)
    ev["total_revisions"] = len(revs)
    return ev


def _verdict(ev, b_no_run_at_t0, c_no_run_at_t0):
    reasons: list[str] = []
    b = ev.get("branches", {})
    conc = ev.get("concurrency", {})
    live_peak = conc.get("peak_simultaneous_running", 0)
    auth_pairs = conc.get("authoritative_concurrent_pairs", [])
    # True concurrency: proven by the live sampler (>=2 in-flight at once) OR
    # by the authoritative persisted-timestamp overlap proof (>=1 pair of runs
    # whose in-flight windows overlap). The overlap proof is independent of the
    # racy sampler, so a monitor that never recorded a sample still yields a
    # verifiable concurrency fact.
    concurrency_ok = (live_peak >= 2) or (len(auth_pairs) >= 1)
    if not concurrency_ok:
        reasons.append(
            f"true concurrency not observed: live peak={live_peak}, "
            f"authoritative overlapping pairs={auth_pairs}"
        )

    # Dependency gating: B & C had NO run at t0 (blocked on upstream).
    if not b_no_run_at_t0:
        reasons.append("B had a Run at t0 — dependency gate did NOT block it on A")
    if not c_no_run_at_t0:
        reasons.append("C had a Run at t0 — dependency gate did NOT block it on B")

    # Every branch must reach a terminal + evidence.
    for lab in ["A", "B", "C", "D", "E", "F"]:
        br = b.get(lab, {})
        if br.get("run_id") is None:
            reasons.append(f"{lab}: no Run registered (dependency chain never released it?)")
            continue
        if br.get("run_terminal_event") not in _TERMINAL:
            reasons.append(f"{lab}: Run never reached a terminal lifecycle event")
        if br.get("task_status") != "done":
            reasons.append(f"{lab}: task status is {br.get('task_status')!r}, expected 'done'")
        if not br.get("write_file_ok"):
            reasons.append(f"{lab}: write_file did not succeed (expected 1 succeeded tool)")
        if not br.get("has_revision"):
            reasons.append(f"{lab}: no WorkspaceFileRevision for output_{lab}.txt")
        if not br.get("file_marker_ok"):
            reasons.append(f"{lab}: output file missing or marker absent (workspace isolation breach under concurrent load)")
    # D/E/F must be terminal (they were ready at t0 and independent).
    for lab in ["D", "E", "F"]:
        if not b.get(lab, {}).get("run_terminal_event"):
            reasons.append(f"{lab} (independent) did not reach a terminal Run")

    # Dependency ordering: B's re-enqueue must follow A settling done, and C's
    # must follow B settling done (B waits for A, C waits for B).
    done_ts = ev.get("task_done_ts", {})
    re_ts = ev.get("re_enqueue_ts", {})
    def _ts(d, k):
        v = d.get(k)
        if not v:
            return None
        from datetime import datetime as _dt
        try:
            return _dt.fromisoformat(v)
        except Exception:
            return v
    a_done, b_re = _ts(done_ts, "A"), _ts(re_ts, "B")
    b_done, c_re = _ts(done_ts, "B"), _ts(re_ts, "C")
    if a_done and b_re and b_re < a_done:
        reasons.append(f"dependency order: B re-enqueued ({b_re}) BEFORE A was done ({a_done})")
    if b_done and c_re and c_re < b_done:
        reasons.append(f"dependency order: C re-enqueued ({c_re}) BEFORE B was done ({b_done})")
    return (len(reasons) == 0), reasons


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

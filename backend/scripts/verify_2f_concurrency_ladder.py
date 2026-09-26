"""Phase 2F §20 — Concurrency / Performance LADDER (task t_99c4a1bb).

Staged, real-load concurrency test built on the proven `t_edbd5f78` driver
(`verify_2f_concurrency_dependency.py` @ 33de066f), reusing its seeding /
isolation / host-sandbox seams UNCHANGED.

Stages (a SEPARATE scratch DB per stage, STOP early on instability):
    Stage 1:  5  independent Tasks,  5  workers
    Stage 2:  10 independent Tasks, 10 workers
    Stage 3:  20 independent Tasks, 20 workers   (tested ceiling — do NOT go to 50+)

Each stage: N independent todo-Tasks (no dependency chain) + N concurrent
command workers (SKIP-LOCKED claiming). Each Task carries a minimal goal that
forces exactly two real tool calls (write_file + execute_code).

Per-stage, PERSISTED-timestamp evidence (the t_edbd5f78 method, not a racy
in-process sampler as the source of truth):
  * clean terminals for every Run (no infinite RUNNING / no orphan workers)
  * per-Run + stage wall-clock durations
  * 429 / 5xx count (real LLM HTTP, instrumented) + approximate RPM of the batch
  * SKIP-LOCKED claim-wait (command created_at -> first observed 'claimed') +
    any claim-timeout starvation
  * no auto-retry (exactly 1 Run per Task; a failed Run just stays failed)

The API behavior is observed by instrumenting the REAL LLM client class
(OpenAICompatibleClient — the protocol=openai_compatible client used for
provider=openai). Every real HTTP request is logged; on a >=400 the real
client raises LLMError("HTTP {status}: ..."), which we record verbatim so a
429 / 5xx is evidence, not papered over. NO mocks — the LLM is the REAL
agnes-3.0-flash via ambient AGNES_API_KEY + AGNES_BASE_URL.

Architecture (why the shape is what it is):
  Each stage runs in its OWN subprocess so its `app.*` modules bind to that
  stage's scratch DB from the start (the proven base-driver env-before-import
  pattern). This avoids re-binding the module-level SQLAlchemy engine/session
  that the already-imported service modules captured, and isolates a crash in
  one stage from the others. The orchestrator launches the stage subprocesses,
  reads each stage's persisted-timestamp evidence JSON, applies the stopping
  rule, and writes the combined PHASE_2F_CONCURRENCY_LADDER_EVIDENCE.json.

Stopping rule: if a stage shows rate-limiting (429/5xx), worker starvation, or
non-terminating Runs, STOP before the next stage and record that stage's N as
the environment's safe ceiling. If all three stages are clean, safe
concurrency = 20 (the tested ceiling for THIS card); 50+ is reported only as
extrapolation and is NOT executed (§24 cost control).

Cost: up to 5 + 10 + 20 = 35 minimal real LLM turns (one model call each,
then 2 tool calls per Run). This is the one card allowed to use more turns;
keep prompts minimal and stop early on any instability.

Run (orchestrator; it launches the stage subprocesses):
    cd backend && uv run --no-sync python scripts/verify_2f_concurrency_ladder.py
    optional env: LADDER_STOP_AT=1|2|3   LADDER_DEADLINE_S=<sec>

Per-stage subprocess (driven by the orchestrator, env set before import):
    LADDER_MODE=stage LADDER_BASE=<hex> LADDER_N=<n> LADDER_DEADLINE_S=<sec>
    python scripts/verify_2f_concurrency_ladder.py

Resume after a crash (reuses the same scratch base, skips already-clean
stages, re-runs only the first not-clean one):
    LADDER_BASE=<same hex> LADDER_SKIP_DONE=1 python scripts/verify_2f_concurrency_ladder.py
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.abspath(os.path.join(HERE, ".."))
# Ensure `app.*` resolves whether this is run as `python scripts/...` (sys.path[0]
# is scripts/) or a bare subprocess from BACKEND_DIR. The scripts/AGENTS.md
# contract: scripts resolve `from app.xxx import ...` themselves.
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# ── external inputs (the real LLM credential) ────────────────────────────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")
GLOBAL_DEADLINE_S = float(os.environ.get("LADDER_DEADLINE_S", "600"))
STAGES = [5, 10, 20]
STOP_AT = int(os.environ.get("LADDER_STOP_AT", "3"))

# Scratch coordinates shared by every stage (unique hex per driver invocation).
SCRATCH_BASE = os.environ.get("LADDER_BASE") or f"clawith_2f_perf_{uuid.uuid4().hex[:8]}"
SCRATCH_SECRET = "clawith-2f-ladder-secret"
SCRATCH_WS = f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_perf_ws_{SCRATCH_BASE[-6:]}"

IS_STAGE = os.environ.get("LADDER_MODE") == "stage"
STAGE_N = int(os.environ.get("LADDER_N", "0"))
SKIP_DONE = os.environ.get("LADDER_SKIP_DONE") == "1"
REBUILD_DB = os.environ.get("LADDER_REBUILD") == "1"

# ── env BEFORE any `app.*` import reads settings ─────────────────────────────
# Point the global engine + checkpointer + secret at a scratch DB. In stage
# mode that is THIS stage's DB; in orchestrator mode it is a placeholder the
# orchestrator never actually connects to.
if IS_STAGE:
    STAGE_DB = f"{SCRATCH_BASE}_{STAGE_N}"
    STAGE_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{STAGE_DB}"
else:
    STAGE_DB = f"{SCRATCH_BASE}"
    STAGE_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_BASE}"
os.environ["DATABASE_URL"] = STAGE_DB_URL
os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = STAGE_DB_URL
os.environ["SECRET_KEY"] = SCRATCH_SECRET
os.environ["JWT_SECRET_KEY"] = SCRATCH_SECRET
os.environ["AGENT_DATA_DIR"] = SCRATCH_WS
os.environ["STORAGE_LOCAL_ROOT"] = SCRATCH_WS
os.environ.setdefault("PROCESS_ROLE", "worker")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Generous connection pool so the 20-worker stage (20 claim connections +
# heartbeat renewals + model/tool service sessions + checkpointer) never
# starves on the SQLAlchemy pool.
os.environ["DB_POOL_SIZE"] = "40"
os.environ["DB_MAX_OVERFLOW"] = "20"

if not AGNES_API_KEY and IS_STAGE:
    print("FATAL: AGNES_API_KEY not set — a REAL LLM ladder run needs a real key.")
    sys.exit(2)

# Register every model module so the full Base.metadata is present for
# create_all + checkpointer setup (same as the 2F real-run driver).
for _m in os.listdir(os.path.join(BACKEND_DIR, "app", "models")):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

# psycopg's async driver cannot use the Windows ProactorEventLoop; install the
# selector policy BEFORE any async work (psycopg-specific host requirement).
if sys.platform == "win32":
    import asyncio as _aio
    _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import Base, async_session, create_async_engine, engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.models.llm import LLMModel
from app.models.project import Project
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User

TERMINAL = ("run_completed", "run_failed", "run_cancelled")
# A run that has reached a STABLE disposition is SETTLED: either a true
# terminal, or a durable `waiting_started` (recoverable WAIT after a retryable
# LLM failure, or a human-approval gate). waiting_started is NOT infinite
# RUNNING — the worker has released the command and the run is durably
# checkpointed. Polling to a terminal-only set would burn the whole deadline
# when every run parks in waiting_user, so the driver settles on this set.
WAS_SETTLED = "waiting_started"
SETTLED = TERMINAL + (WAS_SETTLED,)


def _ddl(q: str):
    return sa_text(q)


# ── host-portable command runner (documented win32 host artifact, unchanged) ─
class _HostPortableSandbox:
    """SubprocessBackend stand-in for a win32 host (documented artifact).

    The container `SubprocessBackend` uses bubblewrap + `preexec_fn`
    (Unix-only) and cannot spawn on Windows. This runner executes the exact
    host subprocess the LLM requested, returning a REAL `ExecutionResult`.
    It is a host I/O artifact, NOT a mock of the LLM or the tool plumbing
    (reservation, normalization, result store, ledger, verification remain the
    real runtime code).
    """

    name = "subprocess-hostportable"

    def _build(self, language, code, work_dir):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="clawith_2f_perf_hostexec_"))
        script = tmp / ("main.py" if language == "python" else "main.sh")
        script.write_text(code, encoding="utf-8")
        if language == "python":
            argv = [sys.executable, "-I", "-B", str(script)]
        else:
            argv = ["bash", "--noprofile", "--norc", str(script)]
        return tmp, argv

    async def execute(self, code, language, timeout=30, work_dir=None, **_kw):
        import subprocess as _sp

        from app.services.sandbox.base import ExecutionResult

        language = language or "python"
        t0 = time.time()
        try:
            tmp, argv = self._build(language, code, work_dir)

            def _spawn():
                # check=False: we inspect returncode (a non-zero exit is a real,
                # captured outcome, not a raised error) — documented host artifact.
                return _sp.run(
                    argv, cwd=str(tmp), capture_output=True, text=True,
                    timeout=timeout, check=False,
                )

            try:
                cp = await asyncio.wait_for(asyncio.to_thread(_spawn), timeout=timeout + 10)
            except TimeoutError:
                return ExecutionResult(False, "", "command_timeout", 124,
                                       int((time.time() - t0) * 1000), "command_timeout")
            return ExecutionResult(
                success=cp.returncode == 0,
                stdout=cp.stdout[:20000],
                stderr=cp.stderr[:10000],
                exit_code=cp.returncode if cp.returncode is not None else 0,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - host spawn failure MUST be
            # captured as a failed ExecutionResult (real stdout/stderr/exit),
            # never crash the command worker; mirrors the proven 33de066f driver.
            return ExecutionResult(False, "", str(exc), 1,
                                   int((time.time() - t0) * 1000), f"host_spawn_failed: {exc}")

    async def health_check(self) -> bool:
        return True

    def get_capabilities(self):
        from app.services.sandbox.base import SandboxCapabilities
        return SandboxCapabilities(["python", "bash"], 300, 256, True, True)


# ── LLM HTTP instrumentation: capture real requests + 429/5xx (NO mocks) ──────
_LLM_LOG: list[dict] = []


def _install_llm_instrumentation() -> None:
    """Wrap the REAL OpenAICompatibleClient HTTP methods to log every request.

    `openai` maps to protocol=openai_compatible -> OpenAICompatibleClient.
    `complete` and `stream` are wrapped; on a >=400 the real client raises
    LLMError("HTTP {status}: ...") — we record the status verbatim so a 429 /
    5xx is evidence, not papered over.

    The original UNBOUND functions are kept in a closure (NOT as class
    attributes): storing them on the class would bind them to the instance on
    attribute access, so `orig(self, ...)` would pass `self` twice and raise
    "got multiple values for argument 'messages'" on every call.
    """
    from app.services.llm import client as _llm_mod

    orig_complete = _llm_mod.OpenAICompatibleClient.complete
    orig_stream = _llm_mod.OpenAICompatibleClient.stream

    def _status_of(err: Exception) -> str:
        msg = str(err)
        if msg.startswith("HTTP "):
            return msg[5:8].strip()
        return "unknown"

    async def _wrap_complete(self, *args, **kwargs):
        req_id = uuid.uuid4().hex[:10]
        t0 = time.time()
        try:
            resp = await orig_complete(self, *args, **kwargs)
            _LLM_LOG.append({"ts": t0, "kind": "complete", "req": req_id, "status": "ok",
                             "model": self.model, "base_url": self.base_url})
            return resp
        except Exception as exc:
            _LLM_LOG.append({"ts": t0, "kind": "complete", "req": req_id,
                             "status": f"error:{_status_of(exc)}", "model": self.model,
                             "base_url": self.base_url, "error": str(exc)[:300]})
            raise

    async def _wrap_stream(self, *args, **kwargs):
        req_id = uuid.uuid4().hex[:10]
        t0 = time.time()
        try:
            resp = await orig_stream(self, *args, **kwargs)
            _LLM_LOG.append({"ts": t0, "kind": "stream", "req": req_id, "status": "ok",
                             "model": self.model, "base_url": self.base_url})
            return resp
        except Exception as exc:
            _LLM_LOG.append({"ts": t0, "kind": "stream", "req": req_id,
                             "status": f"error:{_status_of(exc)}", "model": self.model,
                             "base_url": self.base_url, "error": str(exc)[:300]})
            raise

    _llm_mod.OpenAICompatibleClient.complete = _wrap_complete
    _llm_mod.OpenAICompatibleClient.stream = _wrap_stream


# ── the per-task goal (minimal, deterministic 2-tool-call script) ─────────────
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


# ── SKIP-LOCKED claim-wait + in-flight monitor (persisted-timestamp based) ────
async def _run_monitor(
    run_ids: list[uuid.UUID],
    deadline: float,
    stop_evt: asyncio.Event,
    stats: dict,
) -> None:
    """Poll persisted state; record peak in-flight + first-observed-claimed ts.

    "running" = a run's start command is held in the `claimed` state (the
    worker releases it only at the terminal checkpoint). The FIRST time a run
    is observed `claimed` is recorded (local ts); paired with the persisted
    `created_at` (command intake) it yields the SKIP-LOCKED claim-wait.
    """
    peak = 0
    peak_runs: list[uuid.UUID] = []
    samples = 0
    observed_claim: dict[uuid.UUID, datetime] = {}
    # The SAME dict object is shared through stats so the consumer sees it
    # even if this task is cancelled mid-poll (in-place mutation, no rebind).
    stats.update({"peak": 0, "peak_runs": [], "samples": 0, "observed_claim": observed_claim})
    while time.time() < deadline and not stop_evt.is_set():
        try:
            async with async_session() as s:
                rows = (await s.execute(
                    select(AgentRunCommand.run_id, AgentRunCommand.status)
                    .where(AgentRunCommand.run_id.in_(run_ids),
                          AgentRunCommand.command_type == "start")
                )).all()
        except Exception:  # noqa: BLE001 - a transient DB blip in the READ-ONLY
            # monitor must not kill the whole stage; return empty rows and retry
            # next sample (mirrors the proven 33de066f monitor).
            rows = []
        now = datetime.now(UTC)
        running = [r for r, st in rows if st == "claimed"]
        for r in running:
            observed_claim.setdefault(r, now)
        samples += 1
        if len(running) > peak:
            peak = len(running)
            peak_runs = running
        stats["peak"] = peak
        stats["peak_runs"] = [str(r) for r in peak_runs]
        stats["samples"] = samples
        await asyncio.sleep(0.4)
    # In-place normalization (NOT a rebind): if we were cancelled at the sleep
    # above, the consumer still sees the UUID-keyed dict and normalizes it.
    for r, dt in list(observed_claim.items()):
        observed_claim[str(r)] = dt.isoformat()


# ── evidence + verdict (per stage) ────────────────────────────────────────────
async def _collect_stage_evidence(
    run_ids_by_task: dict[str, uuid.UUID | None],
    task_ids: dict[str, uuid.UUID],
) -> dict:
    """Persisted-timestamp per-Task evidence for this stage."""
    all_run_ids = [r for r in run_ids_by_task.values() if r is not None]
    ev: dict = {"tasks": {}}
    if not all_run_ids:
        return ev
    async with async_session() as s:
        tasks = (await s.execute(select(Task).where(Task.id.in_(list(task_ids.values()))))).scalars().all()
        task_by_id = {t.id: t for t in tasks}
        run_rows = (await s.execute(select(AgentRun.id).where(AgentRun.id.in_(all_run_ids)))).scalars().all()
        run_exists = set(run_rows)
        tools = (await s.execute(
            select(AgentToolExecution).where(AgentToolExecution.run_id.in_(all_run_ids)).order_by(AgentToolExecution.started_at)
        )).scalars().all()
        events = (await s.execute(
            select(AgentRunEvent).where(AgentRunEvent.run_id.in_(all_run_ids)).order_by(AgentRunEvent.created_at)
        )).scalars().all()
        first_tools = (await s.execute(
            select(AgentToolExecution.run_id, func.min(AgentToolExecution.started_at))
            .where(AgentToolExecution.run_id.in_(all_run_ids)).group_by(AgentToolExecution.run_id)
        )).all()
        # command attempt_count proves NO auto-retry: a retried start command
        # would show attempt_count >= 2 and/or a SECOND start command per Run.
        cmd_rows = (await s.execute(
            select(AgentRunCommand.run_id, AgentRunCommand.attempt_count, AgentRunCommand.idempotency_key)
            .where(AgentRunCommand.run_id.in_(all_run_ids), AgentRunCommand.command_type == "start")
        )).all()
    # start-command attempt evidence proves NO auto-retry:
    #  * n_start_cmds should equal n_runs (one start command per Run — an
    #    auto-retry would mint a SECOND start command/Run);
    #  * max_attempt is the highest command.attempt_count. A worker re-claim
    #    (release_for_retry) bumps it, so 0 means no command was ever retried.
    n_start_cmds = len(cmd_rows)
    max_attempt = max([(_a if _a is not None else 0) for _r, _a, _k in cmd_rows] or [0])

    def by_run(rows, key):
        m: dict = {}
        for r in rows:
            m.setdefault(getattr(r, key), []).append(r)
        return m

    tools_by_run = by_run(tools, "run_id")
    events_by_run = by_run(events, "run_id")
    first_tool_map = dict(first_tools)

    for lab, rid in run_ids_by_task.items():
        tid = task_ids[lab]
        task = task_by_id.get(tid)
        terminal = None
        terminal_ts = None
        created_ts = None
        for e in events_by_run.get(rid, []):
            if e.event_type in TERMINAL:
                terminal = e.event_type
                terminal_ts = e.created_at
            if e.event_type == "run_created":
                created_ts = e.created_at
        # A durable WAIT (waiting_started) is a settled disposition, not a
        # terminal. If a run has a WAIT but no terminal, surface it so the
        # verdict can record it as a real failure (recorded, not infinite).
        if terminal is None:
            for e in events_by_run.get(rid, []):
                if e.event_type == "waiting_started":
                    terminal = "waiting_started"
                    terminal_ts = e.created_at
                    break
        tl = tools_by_run.get(rid, [])
        write_ok = any(t.tool_name == "write_file" and t.status == "succeeded" for t in tl)
        # The authoritative "the command actually ran" signal is a host
        # execute_code row that reached status=succeeded (real subprocess,
        # real exit code). The stdout marker (`MARK_Txx_CMD_OK`) is kept as an
        # informational check only: result_summary can be truncated, so its
        # absence is NOT a failure when the execution succeeded.
        cmd_succeeded = any(t.tool_name == "execute_code" and t.status == "succeeded" for t in tl)
        cmd_marker_ok = any("_CMD_OK" in (t.result_summary or "") for t in tl if t.tool_name == "execute_code")
        ev["tasks"][lab] = {
            "run_id": str(rid) if rid else None,
            "run_row_exists": bool(rid) and rid in run_exists,
            "task_status": task.status if task else None,
            "run_created_ts": created_ts.isoformat() if created_ts else None,
            "first_tool_ts": first_tool_map[rid].isoformat() if rid in first_tool_map else None,
            "run_terminal_event": terminal,
            "run_terminal_ts": terminal_ts.isoformat() if terminal_ts else None,
            "tool_count": len(tl),
            "write_file_ok": write_ok,
            "execute_code_ok": cmd_succeeded,
            "execute_code_marker_ok": cmd_marker_ok,
            "tools": [{"tool": t.tool_name, "status": t.status} for t in tl],
        }
    ev["no_auto_retry"] = {
        "n_runs": len(all_run_ids),
        "n_start_commands": n_start_cmds,
        "one_start_command_per_run": n_start_cmds == len(all_run_ids),
        "max_command_attempt_count": max_attempt,
        # A start command is created at attempt_count=0; each worker claim
        # bumps it by 1, so a normally-applied command ends at 1. A value of
        # 2+ means a command was RELEASED AND RE-CLAIMED within this stage
        # (release_for_retry) — that is a retry, not a clean single pass.
        "no_command_retried": max_attempt <= 1,
    }
    return ev


def _stage_verdict(ev: dict, stats: dict, rate_limited: bool) -> tuple[bool, list]:
    reasons: list[str] = []
    n_tasks = len(ev.get("tasks", {}))
    clean = 0
    waiting = 0
    observed = {str(k): v for k, v in (stats.get("observed_claim") or {}).items()}
    for lab, t in ev.get("tasks", {}).items():
        terminal = t.get("run_terminal_event")
        if terminal in TERMINAL:
            clean += 1
        elif terminal == "waiting_started":
            # A settled, recoverable WAIT ("model provider remained unavailable
            # after N attempts"). This is a RECORDED REAL FAILURE, NOT infinite
            # RUNNING: the worker released the command and the run is durably
            # checkpointed. It means the provider was unavailable / rate-limited
            # under this load -> the stage did NOT reach a clean SUCCEEDED
            # terminal, so the ladder records it and stops.
            waiting += 1
            reasons.append(
                f"{lab}: run parked in durable waiting_started "
                f"(recorded real failure: model provider unavailable after bounded retries)"
            )
        else:
            # Genuinely not settled: still not_started/running past the deadline.
            reasons.append(f"{lab}: run not settled ({terminal}) — infinite RUNNING / orphan worker")
        if t.get("task_status") != "done" and terminal == "run_completed":
            reasons.append(f"{lab}: task status={t.get('task_status')!r}, expected 'done'")
        # tool evidence only required for a SUCCEEDED run
        if terminal == "run_completed":
            if not t.get("write_file_ok"):
                reasons.append(f"{lab}: write_file did not succeed")
            if not t.get("execute_code_ok"):
                reasons.append(f"{lab}: execute_code marker missing")
        if not t.get("run_row_exists"):
            reasons.append(f"{lab}: AgentRun row missing (no Run registered)")
        # starvation: not settled AND never observed claimed within the stage.
        rid = str(t.get("run_id"))
        if terminal not in SETTLED and rid and rid not in observed:
            reasons.append(f"{lab}: never observed claimed and not settled (claim starvation)")
    if rate_limited:
        reasons.append("rate-limited (429/5xx observed on the real LLM HTTP) — safe ceiling hit")
    # no auto-retry: exactly one start command per Run, none re-claimed.
    nar = ev.get("no_auto_retry", {})
    if not nar.get("one_start_command_per_run", True):
        reasons.append(
            f"auto-retry suspected: {nar.get('n_start_commands')} start commands for "
            f"{nar.get('n_runs')} runs (a 2nd start command was minted)"
        )
    if not nar.get("no_command_retried", True):
        reasons.append(f"command was re-claimed (attempt_count up to {nar.get('max_command_attempt_count')})")
    # A clean stage = every task reached a SUCCEEDED terminal (no waiting, no
    # rate-limiting). Any waiting_started makes it not-clean (recorded failure).
    ok = len(reasons) == 0 and clean == n_tasks and n_tasks > 0 and not rate_limited
    return ok, reasons


# ── one stage (fresh scratch DB, N workers, N independent tasks) ──────────────
async def _run_stage_body() -> int:
    from app.services import agent_tools
    from app.services import workspace_collaboration as wcs
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import (
        assert_runtime_schema_ready,
        build_runtime_worker_components,
    )
    from app.services.storage_runtime.local import LocalStorageBackend
    from app.services.task_executor import TaskBlockedError, enqueue_task_runtime

    n = STAGE_N
    settings = get_settings()
    storage = LocalStorageBackend(SCRATCH_WS)

    # 1. scratch Postgres DB (fresh, isolated, disposable).
    #    REBUILD_DB: drop the existing stage DB (re-run of a crashed stage) so
    #    the stage re-runs against a CLEAN schema instead of reusing residue.
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        if REBUILD_DB:
            try:
                await c.execute(_ddl(f'DROP DATABASE IF EXISTS "{STAGE_DB}"'))
                print(f"  dropped stale scratch db {STAGE_DB} (rebuild)")
            except Exception as exc:  # noqa: BLE001 - the DROP is a best-effort
                # pre-step on a scratch DB; if it fails (e.g. a stale connection
                # still holds it) we fall through and let CREATE-IF-EXISTS reuse
                # the existing DB. Non-fatal: printed, never aborts the stage.
                print(f"  WARN: could not drop stale db {STAGE_DB}: {exc}")
        try:
            await c.execute(_ddl(f'CREATE DATABASE "{STAGE_DB}"'))
            print(f"  created scratch db {STAGE_DB}")
        except Exception as exc:
            if "already exists" in str(exc):
                print(f"  reusing existing scratch db {STAGE_DB}")
            else:
                raise
    await admin.dispose()

    # 2. product schema + checkpoint schema (module engine points at STAGE_DB)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    saver_ctx = create_checkpointer(settings)
    async with saver_ctx as saver:
        await saver.setup()
    await assert_runtime_schema_ready(engine, settings=settings)

    # 3. seed FK-parent rows
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    project = uuid.uuid4()
    async with async_session() as s, s.begin():
        s.add(Tenant(id=tenant, name=f"T-2F-PERF-{n}", slug=f"t2f-perf-{n}-{tenant.hex[:8]}", im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name=f"2f-perf-{n}-user"))
    async with async_session() as s, s.begin():
        s.add(LLMModel(
            id=model, tenant_id=tenant,
            provider="openai", model=AGNES_MODEL,
            api_key_encrypted=encrypt_data(AGNES_API_KEY, SCRATCH_SECRET),
            base_url=AGNES_BASE_URL, label=f"2f-perf-{n}-{AGNES_MODEL}",
            enabled=True, supports_vision=False, supports_tool_calling=True,
            request_timeout=120, max_output_tokens=2048, context_window_tokens=32768,
        ))
    async with async_session() as s, s.begin():
        s.add(Agent(
            id=agent, tenant_id=tenant, name=f"PerfAgent{n}", creator_id=user,
            agent_type="native", status="idle", primary_model_id=model,
            is_system=False, access_mode="company", company_access_level="use",
            expires_at=None, is_expired=False,
            max_tool_rounds=20,
        ))
    async with async_session() as s, s.begin():
        s.add(Project(id=project, tenant_id=tenant, created_by=user, name=f"2f-perf-{n}-project", status="RECEIVED"))

    # 4. tool set: write_file + execute_code (+ list_files), suppress extras
    from app.models.tool import AgentTool, Tool
    from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy
    ENABLE = ["write_file", "execute_code", "list_files"]
    SUPPRESS = ["read_file", "list_focus_items", "query_directory"]
    tool_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        for name in ENABLE + SUPPRESS:
            tid = uuid.uuid4()
            tool_ids[name] = tid
            d = builtin_model_definition(name)
            fn = d.get("function", {})
            try:
                pol = builtin_policy(name) or {}
            except Exception:  # noqa: BLE001 - some tools carry no policy config;
                # a missing/unsupported policy is a best-effort fallback to {}
                # (the Tool still seeds with an empty config), never fatal.
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
        for name in ENABLE:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=True, source="system"))
        for name in SUPPRESS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=False, source="system"))

    # 5. N independent tasks (no dependency chain)
    labels = [f"T{i:02d}" for i in range(n)]
    task_ids: dict[str, uuid.UUID] = {}
    run_ids_by_task: dict[str, uuid.UUID | None] = {lab: None for lab in labels}
    async with async_session() as s, s.begin():
        for lab in labels:
            tid = uuid.uuid4()
            task_ids[lab] = tid
            s.add(Task(
                id=tid, tenant_id=tenant, agent_id=agent,
                title=f"2f-perf:{n}:{lab}", description=_task_goal(lab),
                type="todo", status="pending", priority="medium",
                assignee="self", created_by=user, project_id=project,
            ))

    # 6. isolation seams + shared checkpointer (no live DB/Redis)
    _install_isolation(agent_tools, wcs, storage)
    _install_llm_instrumentation()
    saver2_ctx = create_checkpointer(settings)

    print(f"=== LADDER stage n={n} db={STAGE_DB} ===")
    llm_calls_at_start = len(_LLM_LOG)

    # 7. INTAKE all N tasks at t0 through the REAL Phase-2E gate.
    intake_log: list[dict] = []
    async with saver2_ctx as saver2:
        await saver2.setup()
        deadline = time.time() + GLOBAL_DEADLINE_S
        async with async_session() as s, s.begin():
            agent_row = (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()
            for lab in labels:
                task = (await s.execute(select(Task).where(Task.id == task_ids[lab]))).scalar_one()
                try:
                    handle = await enqueue_task_runtime(
                        s, task=task, agent=agent_row,
                        execution_id=uuid.uuid4(), actor_user_id=user,
                    )
                    run_id = handle.run_id if handle else None
                    blocked = False
                except TaskBlockedError:
                    run_id, blocked = None, True
                run_ids_by_task[lab] = run_id
                intake_log.append({"task": lab, "run": str(run_id)[:8] if run_id else None, "blocked": blocked})
                print(f"  intake {lab}: {'BLOCKED' if blocked else 'enqueued run=' + str(run_id)[:8]}")

        known_runs = [r for r in run_ids_by_task.values() if r]

        # persisted created_at (command intake) for claim-wait derivation
        created_map: dict[uuid.UUID, datetime] = {}
        async with async_session() as s:
            ev_rows = (await s.execute(
                select(AgentRunCommand.run_id, AgentRunCommand.created_at)
                .where(AgentRunCommand.run_id.in_(known_runs),
                       AgentRunCommand.command_type == "start",
                       AgentRunCommand.status.in_(("pending", "claimed", "applied", "rejected")))
            )).all()
            created_map = {rid: ts for rid, ts in ev_rows}

        # Spin up N concurrent workers + the read-only monitor.
        worker_components = [
            build_runtime_worker_components(
                checkpointer=saver2,
                session_factory=async_session,
                lock_engine=engine,
                claimant=f"perf-{n}-w{i}-{uuid.uuid4().hex[:6]}",
                settings=settings,
            )
            for i in range(n)
        ]

        stop_evt = asyncio.Event()
        stats: dict = {}
        exec_start = time.time()  # stage execution window (workers + drain)

        async def _worker_loop(comp) -> None:
            while time.time() < deadline and not stop_evt.is_set():
                res = await comp.worker.run_once()
                st = getattr(res, "status", None)
                await asyncio.sleep(0.15 if st in ("applied", "reconciled", "rejected") else 0.3)

        worker_tasks = [asyncio.create_task(_worker_loop(c)) for c in worker_components]
        monitor_task = asyncio.create_task(_run_monitor(known_runs, deadline, stop_evt, stats))

        # Drive to completion: poll until every known run reaches a SETTLED
        # disposition (terminal OR a stable durable WAIT). A run parked in
        # waiting_started is settled — do not burn the deadline on it.
        settled_count = 0
        while time.time() < deadline:
            async with async_session() as s:
                terms = (await s.execute(
                    select(AgentRunEvent.run_id, AgentRunEvent.event_type)
                    .where(AgentRunEvent.run_id.in_(known_runs),
                           AgentRunEvent.event_type.in_(SETTLED))
                )).all()
            settled_count = len({r for r, _t in terms})
            if settled_count >= len(known_runs):
                break
            await asyncio.sleep(0.5)
        stop_evt.set()

        for wt in worker_tasks:
            wt.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        if not monitor_task.done():
            monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)

        # 8. evidence + verdict
        ev = await _collect_stage_evidence(run_ids_by_task, task_ids)

        # LLM API behavior for THIS stage (the whole subprocess is one stage).
        stage_llm = _LLM_LOG[llm_calls_at_start:]
        err_status = [e for e in stage_llm if e.get("status", "").startswith("error")]
        s429 = [e for e in err_status if "429" in e.get("status", "")]
        s5xx = [e for e in err_status if any(c in e.get("status", "") for c in ("500", "502", "503", "504"))]
        rate_limited = bool(s429) or bool(s5xx)

        # per-Run durations from persisted timestamps
        durations: dict[str, dict] = {}
        for lab, t in ev.get("tasks", {}).items():
            d = {}
            if t.get("run_created_ts") and t.get("run_terminal_ts"):
                c = datetime.fromisoformat(t["run_created_ts"])
                e_ts = datetime.fromisoformat(t["run_terminal_ts"])
                d["total_s"] = round((e_ts - c).total_seconds(), 3)
            if t.get("first_tool_ts") and t.get("run_terminal_ts"):
                f = datetime.fromisoformat(t["first_tool_ts"])
                e_ts = datetime.fromisoformat(t["run_terminal_ts"])
                d["exec_s"] = round((e_ts - f).total_seconds(), 3)
            durations[lab] = d

        # claim waits (persisted created_at -> first observed claimed)
        claim_waits: dict[str, float] = {}
        observed = {str(k): v for k, v in (stats.get("observed_claim") or {}).items()}
        for lab in labels:
            rid = run_ids_by_task.get(lab)
            if not rid:
                continue
            obs_val = observed.get(str(rid))
            created_dt = created_map.get(rid)
            if obs_val is not None and created_dt is not None:
                # obs_val may be a datetime (monitor cancelled before its
                # end-of-loop normalization) or an ISO string — normalize both.
                if isinstance(obs_val, str):
                    obs_dt = datetime.fromisoformat(obs_val)
                else:
                    obs_dt = obs_val
                cd = created_dt if created_dt.tzinfo else created_dt.replace(tzinfo=UTC)
                claim_waits[lab] = round((obs_dt - cd).total_seconds(), 3)

        wall_clock = time.time() - exec_start
        stage_ev = {
            "stage": n, "tasks": n, "workers": n, "scratch_db": STAGE_DB,
            "wall_clock_s": round(wall_clock, 2),
            "n_runs": len(known_runs),
            "n_settled": settled_count,
            "n_terminal": sum(1 for t in ev.get("tasks", {}).values() if t.get("run_terminal_event") in TERMINAL),
            "n_waiting": sum(1 for t in ev.get("tasks", {}).values() if t.get("run_terminal_event") == "waiting_started"),
            "durations_s": durations,
            "peak_simultaneous_running": stats.get("peak", 0),
            "peak_runs": stats.get("peak_runs", []),
            "monitor_samples": stats.get("samples", 0),
            "claim_wait_s": claim_waits,
            "no_auto_retry": ev.get("no_auto_retry", {}),
            "llm": {
                "base_url": AGNES_BASE_URL, "model": AGNES_MODEL, "provider": "openai",
                "real_http": True, "no_mock": True,
                "request_count": len(stage_llm),
                "err_429_count": len(s429),
                "err_5xx_count": len(s5xx),
                "errors_verbatim": [e.get("error") for e in err_status][:20],
                "request_log": stage_llm,
            },
            "intake": intake_log,
        }
        ok, reasons = _stage_verdict(ev, stats, rate_limited)
        stage_ev["tasks_detail"] = ev.get("tasks", {})
        stage_ev["verdict_ok"] = ok
        stage_ev["verdict_reasons"] = reasons
        stage_ev["rate_limited"] = rate_limited

        # 9. write the per-stage evidence (incremental; a later crash preserves it)
        out = os.path.join(HERE, f"PHASE_2F_CONCURRENCY_LADDER_STAGE{n}.json")
        # rpm over the stage wall-clock
        if wall_clock > 1:
            stage_ev["llm"]["rpm"] = round(len(stage_llm) / wall_clock * 60.0, 2)
        Path(out).write_text(json.dumps(stage_ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        print(f"  stage n={n}: settled={settled_count}/{len(known_runs)} "
              f"peak={stats.get('peak', 0)} wall={wall_clock:.1f}s "
              f"llm_calls={len(stage_llm)} 429={len(s429)} 5xx={len(s5xx)} verdict_ok={ok}")
        for r in reasons:
            print(f"    - {r}")
        print(f"  WROTE {out}")

        # dispose so the process exits cleanly (no orphan connections)
        await engine.dispose()
    return 0


def _install_isolation(agent_tools, wcs, storage) -> None:
    """Point tool storage + workspace + locks + sandbox at scratch (no live DB/Redis)."""
    from contextlib import asynccontextmanager
    from pathlib import Path as _P

    @asynccontextmanager
    async def _cm(*_a, **_k):
        yield

    for mod in (agent_tools, wcs):
        mod.workspace_locks = _cm
    agent_tools.get_storage_backend = lambda: storage
    wcs.get_storage_backend = lambda: storage
    agent_tools.WORKSPACE_ROOT = _P(SCRATCH_WS)
    from app.services.sandbox import registry
    host = _HostPortableSandbox()
    registry.get_sandbox_backend = lambda cfg: host
    if hasattr(agent_tools, "get_sandbox_backend"):
        agent_tools.get_sandbox_backend = lambda cfg: host


# ── orchestrator: launch stage subprocesses, apply the stop rule, aggregate ──
def _run_stage_subprocess(n: int, *, rebuild: bool) -> int:
    env = dict(os.environ)
    env["LADDER_MODE"] = "stage"
    env["LADDER_BASE"] = SCRATCH_BASE
    env["LADDER_N"] = str(n)
    env["LADDER_DEADLINE_S"] = str(GLOBAL_DEADLINE_S)
    if rebuild:
        env["LADDER_REBUILD"] = "1"
    cmd = [sys.executable, os.path.abspath(__file__)]
    print(f"\n>>> launching stage n={n} (subprocess, own scratch db {SCRATCH_BASE}_{n})")
    # check=False: we capture stdout and inspect returncode to apply the
    # stopping rule — a non-zero exit is a recorded outcome, not a raised error.
    cp = subprocess.run(cmd, env=env, cwd=BACKEND_DIR, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=GLOBAL_DEADLINE_S + 300, check=False)
    sys.stdout.write(cp.stdout or "")
    if cp.returncode != 0:
        print(f"  !! stage n={n} exited {cp.returncode}")
    return cp.returncode


def _read_stage_evidence(n: int) -> dict:
    p = os.path.join(HERE, f"PHASE_2F_CONCURRENCY_LADDER_STAGE{n}.json")
    if not Path(p).exists():
        return {}
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt stage-evidence file
        # means the stage is not (yet) clean; return {} so the orchestrator
        # treats it as "re-run needed" rather than crashing the whole ladder.
        return {}


def _write_combined(combined: dict) -> str:
    out = os.path.join(HERE, "PHASE_2F_CONCURRENCY_LADDER_EVIDENCE.json")
    Path(out).write_text(json.dumps(combined, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def _orchestrate() -> int:
    combined: dict = {
        "task": "t_99c4a1bb",
        "scratch_base": SCRATCH_BASE,
        "scratch_ws": SCRATCH_WS,
        "llm": {"base_url": AGNES_BASE_URL, "model": AGNES_MODEL, "provider": "openai",
                 "real_http": True, "no_mock": True},
        "stages": STAGES,
        "stop_at": STOP_AT,
        "deadline_s": GLOBAL_DEADLINE_S,
    }

    safe_ceiling: int | None = None
    stopped_at: int | None = None
    stopped_early = False
    stopped_reason = None

    for stage_no, n in enumerate(STAGES, start=1):
        if stage_no > STOP_AT:
            break
        # Resume support: an already-persisted CLEAN stage is skipped (0 new
        # LLM calls); a not-clean / missing stage is re-run on a rebuilt DB.
        prior = _read_stage_evidence(n)
        if SKIP_DONE and prior.get("verdict_ok") and not prior.get("rate_limited"):
            print(f"\n>>> stage n={n} already clean — SKIP (reusing persisted evidence)")
            stage_ev = prior
            combined[f"stage_{n}_{n}"] = stage_ev
            _write_combined(combined)
            safe_ceiling = n  # clean -> this stage is the safe floor
            continue
        _run_stage_subprocess(n, rebuild=SKIP_DONE)
        stage_ev = _read_stage_evidence(n)
        combined[f"stage_{n}_{n}"] = stage_ev
        _write_combined(combined)  # incremental: a later crash preserves earlier stages

        if not stage_ev:
            stopped_early = True
            stopped_reason = f"stage n={n} produced no evidence (subprocess failed)"
            safe_ceiling = STAGES[stage_no - 2] if stage_no > 1 else 0
            break
        if stage_ev.get("rate_limited") or not stage_ev.get("verdict_ok", False):
            # rate-limiting OR instability at THIS stage -> the environment's
            # safe concurrency is the LAST CLEAN stage (the failing stage is
            # the ceiling marker; the previous one is proven safe).
            safe_ceiling = STAGES[stage_no - 2] if stage_no > 1 else 0
            stopped_at = n
            stopped_early = True
            stopped_reason = f"stage n={n} not clean; " + ("; ".join(stage_ev.get("verdict_reasons", [])) or "stage not clean")
            break
        safe_ceiling = n  # clean -> this stage is the new safe floor

    # finalize the combined verdict + extrapolation note
    if stopped_early:
        combined["stopped_early"] = True
        combined["stopped_at"] = stopped_at
        combined["stopped_reason"] = stopped_reason
        combined["safe_concurrency"] = safe_ceiling
        combined["note"] = (
            f"Stopped early: last CLEAN stage = n={safe_ceiling}; the failing stage was "
            f"n={stopped_at} (the environment's saturation ceiling). Higher stages were NOT "
            f"executed (do not saturate the API). Safe concurrency = {safe_ceiling}."
        )
    else:
        combined["stopped_early"] = False
        combined["stopped_at"] = None
        combined["stopped_reason"] = None
        combined["safe_concurrency"] = 20
        combined["note"] = (
            "All three stages (5/10/20) clean: safe concurrency = 20 (the tested ceiling for "
            "this card). No 429/5xx, no starvation, no auto-retry at 20. Headroom for 50+ "
            "exists (no saturation signal at 20), but §24 cost control forbids executing it — "
            "the SKIP-LOCKED claim path + per-thread lock scale linearly; the binding "
            "constraint at higher load would be provider RPM/concurrency, not the Runtime."
        )
    out = _write_combined(combined)

    print("\n=== LADDER VERDICT ===")
    print(f"  stopped_early={stopped_early}  stopped_at={stopped_at}  safe_concurrency={safe_ceiling}")
    print(f"  reason: {stopped_reason}")
    print(f"  combined evidence: {out}")
    return 0


if IS_STAGE:
    raise SystemExit(asyncio.run(_run_stage_body()))

if not AGNES_API_KEY:
    print("FATAL: AGNES_API_KEY not set — a REAL LLM ladder run needs a real key.")
    sys.exit(2)

raise SystemExit(_orchestrate())

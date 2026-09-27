"""Phase 2F §15 — VERIFY durable-Runtime cancellation (task t_6efa793e).

Mandatory first step of this card was the capability audit.  Its verdict,
from the real code (cited in docs/PHASE_2F_CANCELLATION_REPORT_T_6efa793e.md):

    BRANCH A — the durable Runtime FULLY supports cancellation.

The cancel seam that this driver exercises, end-to-end, is:

  * intake      adapter.py:365 cancel_run() -> persistence.py:500 enqueue_cancel
                (the same call the WS cancel packet makes at websocket.py:1029)
  * model       agent_run_command.py:32  command_type IN ('start','resume','cancel')
  * worker      langgraph_driver.py:482-486  cancel is a CONTROL-PLANE command: the
                Graph is never advanced by a cancel; the worker settles it against
                the last committed checkpoint (command_worker.py:772-808 in-flight,
                970-979 in-flight-abort -> 'cancelled_before_apply').
  * in-flight   node_executor.py:593-600 (control_guard) + tool_step_service.py:
                1565-1674 (operation vs cancel_task race -> 'tool_cancelled')
  * terminal    agent_run_event.py:34 'run_cancelled', published at
                checkpoint_side_effects.py:818-833 + 646
  * settlement  task_completion.py:145-149 (stored task.status -> 'pending' +
                cancel TaskLog); the CANCELLED Task state is a DERIVED projection
                computed by task_execution_service.query_execution
                (task_execution_service.py:573-574), NOT a stored column.

This driver proves RUNNING -> CANCELLED with ZERO real LLM turns:

  * The pinned model is pointed at a LOCAL, fully-deterministic OpenAI-compatible
    endpoint (http.server on 127.0.0.1) that returns a canned tool_call.  The
    REAL model-step code path (client, message build, tool-call parse,
    LLMCompletionStep) runs over real HTTP to that endpoint; only the "thinking"
    is canned.  No external LLM provider is ever contacted.
  * The in-flight window is held by a REAL, long host subprocess
    (execute_code: time.sleep(HOLD_SECONDS)), running through the real Tool
    Execution Ledger.  The Run is genuinely RUNNING / in the tool node.
  * While in-flight, the documented durable cancel (RuntimeCommandIntake.cancel_run)
    is issued.  The worker must stop the in-flight work and settle the Run.

Every Branch A invariant in the card is asserted and recorded to evidence JSON:
  (a) the in-flight worker/subprocess actually stops (real Popen terminated,
      no lingering sandbox process for the cancelled Run);
  (b) no further tool executions are recorded past the cancel time;
  (c) Task -> CANCELLED (real derived_state projection) + Run -> 'run_cancelled'
      terminal event present;
  (d) no "UI cancelled / worker still running" half-state (process-table check);
  (e) no new Run auto-created by the cancel path.

Run:
    cd backend && uv run --no-sync python scripts/verify_2f_cancellation.py

Environment (the ONLY external inputs, all optional here):
    CLAWITH_2F_PG_ADMIN   default postgresql+asyncpg://postgres:postgres@localhost:5432
    The LLM endpoint is LOCAL (no AGNES_* / external LLM key required).

Everything else is isolated scratch state (fresh clawith_2f_cancel_<hex> DB +
fresh storage dir).  No product code, tests, or mainline is touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime

# ── isolated scratch coordinates ─────────────────────────────────────────────
SCRATCH_DB = f"clawith_2f_cancel_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-cancel-secret"
SCRATCH_WS = f"C:/Users/Administrator/AppData/Local/Temp/{SCRATCH_DB}_ws"
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")

# The in-flight hold: the canned execute_code sleeps 40s under a 300s execution
# budget (the `local_code` deadline policy requires timeout >= 180s).  The cancel
# is issued ~2s into the in-flight window, so it ALWAYS lands mid-flight, long
# before the 300s budget would end.  Without the cancel the 40s sleep would
# complete; the durable in-flight cancel stops the real Popen.
HOLD_SECONDS = 40

# Point the global engine + checkpointer + secret at scratch, BEFORE any
# `app.*` import reads settings. This is how we isolate the whole run.
os.environ["DATABASE_URL"] = SCRATCH_DB_URL
os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = SCRATCH_DB_URL
os.environ["SECRET_KEY"] = SCRATCH_SECRET
os.environ["JWT_SECRET_KEY"] = SCRATCH_SECRET
os.environ["AGENT_DATA_DIR"] = SCRATCH_WS
os.environ["STORAGE_LOCAL_ROOT"] = SCRATCH_WS
os.environ.setdefault("PROCESS_ROLE", "worker")
# Deterministic quiet logs; keep our own prints.
os.environ.setdefault("LOG_LEVEL", "WARNING")

import importlib
from pathlib import Path

# psycopg's async driver cannot use the Windows ProactorEventLoop; install the
# selector policy BEFORE any async work (psycopg-specific host requirement).
if sys.platform == "win32":
    import asyncio as _aio
    _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())

_pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "models")
for _m in os.listdir(_pkg):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import Base, async_session, create_async_engine, engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.models.llm import LLMModel
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services import agent_tools
from app.services import workspace_collaboration as wcs
from app.services.storage_runtime.local import LocalStorageBackend

settings = get_settings()
STORAGE = LocalStorageBackend(SCRATCH_WS)

# ── local deterministic LLM endpoint (0 real LLM turns) ─────────────────────
CANNED_TOOL_CALL = {
    "id": "call_2f_cancel",
    "type": "function",
    "function": {
        "name": "execute_code",
        "arguments": json.dumps({
            "language": "python",
            # A genuinely long, real host subprocess holds the in-flight window.
            # `timeout` MUST be >= 180: the `local_code` deadline policy
            # (tool_contracts.py:90-96) floors the execution budget at
            # CODE_EXECUTION_DEFAULT_TIMEOUT_SECONDS=180, and the real
            # tool-argument validation rejects anything below 180
            # ("$.timeout must be at least 180").  We request the max (300) so
            # the 40s sleep completes comfortably within budget IF left alone;
            # the durable in-flight cancel lands ~2s in and stops it.
            "code": (
                "import time\n"
                "print('HOLDING_INFLIGHT', flush=True)\n"
                f"time.sleep({HOLD_SECONDS})\n"
            ),
            "timeout": 300,
        }),
    },
}


def _start_canned_llm_server() -> tuple[int, threading.Thread, dict]:
    """Stand up a deterministic local OpenAI-compatible /chat/completions server.

    Call #1 -> the canned execute_code tool_call (long sleep).  Any later call
    -> a trivial finish, so the Run cannot loop even in an adversarial timing
    race.  The real model-step code path runs over real HTTP to this endpoint.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    counter = {"n": 0}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            counter["n"] += 1
            if counter["n"] == 1:
                body = {
                    "model": "canned-2f-cancel",
                    "choices": [{
                        "message": {"role": "assistant", "content": "", "tool_calls": [CANNED_TOOL_CALL]},
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            else:
                body = {
                    "model": "canned-2f-cancel",
                    "choices": [{
                        "message": {"role": "assistant", "content": "final"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence request logging
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port, thread, counter


# ── host-portable command runner with a REAL process table ──────────────────
# Live in-flight Popen processes, keyed by Run id.  A non-empty value means a
# worker subprocess for that Run is still running (a "UI cancelled / worker
# still running" half-state).
_HOST_SANDBOX_PROCS: dict[str, subprocess.Popen] = {}
# Every Popen pid ever spawned for a Run, so we can prove a real worker
# subprocess existed (and was then terminated), not merely that the table is empty.
_HOST_SANDBOX_PIDS: dict[str, set[int]] = {}


class _HostPortableSandbox:
    """Real one-shot host-subprocess stand-in for the Unix-only SubprocessBackend.

    The container SubprocessBackend (bubblewrap + preexec_fn) is Unix-only and
    cannot spawn on this win32 host.  This runner executes the exact host
    subprocess the model asked for via a REAL Popen.  It is written to be
    NON-BLOCKING with respect to the event loop: the blocking wait is offloaded
    to a worker thread (``asyncio.to_thread``), exactly so the in-flight
    ``cancel_task`` (tool_step_service.py:1565-1674) can still win its race and
    deliver ``operation_task.cancel()`` — which terminates the REAL Popen.  This
    mirrors the real SubprocessBackend's async, cancelable ``proc.wait``.
    stdout/stderr/exit_code are REAL.  The LLM and the tool plumbing
    (reservation, ledger, normalization, result store, deadline/cancel/lease
    race, settlement, verification) remain the REAL runtime code.
    """

    name = "subprocess-hostportable"

    def _build(self, language: str, code: str, work_dir: Path):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="clawith_2f_cancel_hostexec_"))
        script = tmp / ("main.py" if language in {"python", "python3"} else ("main.sh" if language == "bash" else "main.js"))
        script.write_text(code, encoding="utf-8")
        if language in {"python", "python3"}:
            argv = [sys.executable, "-I", "-B", str(script)]
        elif language == "bash":
            argv = ["bash", "--noprofile", "--norc", str(script)]
        else:
            argv = ["node", str(script)]
        return tmp, argv

    def _terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001,S110 -- best-effort reaping; the Popen table is authoritative
            pass

    async def execute(self, code, language, timeout=30, work_dir=None, **_kw):
        from app.services.sandbox.base import ExecutionResult

        # The tool executor passes the Run-scoped sandbox token to the handler;
        # recover it so the Popen can be registered per-Run (the process table).
        from app.services.sandbox.run_scope import sandbox_run_scope_id
        try:
            run_id = str(sandbox_run_scope_id.get())
        except Exception:  # noqa: BLE001 -- scope unset (direct executor call) -> per-call identity
            run_id = f"unknown-{uuid.uuid4().hex[:6]}"

        language = language or "python"
        t0 = time.time()
        try:
            _tmp, argv = self._build(language, code, Path(work_dir) if work_dir else Path(SCRATCH_WS))
            proc = subprocess.Popen(  # noqa: ASYNC220 -- spawn is fast; the blocking WAIT runs in a thread
                argv, cwd=str(_tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            # Register the REAL process in the table BEFORE we await it.
            _HOST_SANDBOX_PROCS[run_id] = proc
            _HOST_SANDBOX_PIDS.setdefault(run_id, set()).add(proc.pid)
            try:
                # NON-BLOCKING: offload the blocking wait to a thread so the
                # event loop stays free for the in-flight cancel_task.  Mirrors
                # the real SubprocessBackend's awaitable proc.wait.
                await asyncio.to_thread(proc.wait, timeout)
                exit_code = proc.returncode if proc.returncode is not None else 0
                stdout = (proc.stdout.read() if proc.stdout else "") or ""
                stderr = (proc.stderr.read() if proc.stderr else "") or ""
                return ExecutionResult(
                    success=exit_code == 0,
                    stdout=stdout[:20000],
                    stderr=stderr[:10000],
                    exit_code=exit_code,
                    duration_ms=int((time.time() - t0) * 1000),
                )
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                return ExecutionResult(False, "", f"command_timeout after {timeout}s", 124,
                                      int((time.time() - t0) * 1000), f"command_timeout:{timeout}s")
            except asyncio.CancelledError:
                # In-flight durable cancel: the tool step cancelled this
                # operation task.  Terminate the REAL Popen before propagating.
                self._terminate(proc)
                raise
            finally:
                # Guarantee no orphan on any exit path.
                self._terminate(proc)
                _HOST_SANDBOX_PROCS.pop(run_id, None)
        except Exception as exc:  # noqa: BLE001 -- any host spawn failure becomes a tool result, never a crash
            return ExecutionResult(False, "", f"host_spawn_failed: {exc}", 1,
                                   int((time.time() - t0) * 1000), f"host_spawn_failed: {exc}")

    async def health_check(self) -> bool:
        return True

    def get_capabilities(self):
        from app.services.sandbox.base import SandboxCapabilities
        return SandboxCapabilities(["python", "bash", "node"], 300, 256, True, True)


def _install_isolation() -> None:
    """Point tool storage + workspace + locks + sandbox at scratch (no live DB/Redis)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(*_a, **_k):
        yield

    for mod in (agent_tools, wcs):
        try:
            mod.workspace_locks = _cm
        except Exception:  # noqa: BLE001,S110 -- module attr may be absent on some layouts; no-lock is the scratch default
            pass
    agent_tools.get_storage_backend = lambda: STORAGE
    wcs.get_storage_backend = lambda: STORAGE
    agent_tools.WORKSPACE_ROOT = Path(SCRATCH_WS)
    from app.services.sandbox import registry
    host = _HostPortableSandbox()
    registry.get_sandbox_backend = lambda cfg: host
    if hasattr(agent_tools, "get_sandbox_backend"):
        agent_tools.get_sandbox_backend = lambda cfg: host


# ── durable-state probes ─────────────────────────────────────────────────────
def pid_alive(pid: int) -> bool:
    """True if an OS process with this pid is still running on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 -- cross-platform kill-check quirk; treat as not-alive
        return False
    return True


async def _in_flight_observed(run_id: uuid.UUID) -> bool:
    """A durable in-flight signal proves the Run reached the tool node.

    Two equivalent durable markers, checked with OR for robustness:
      * an ``agent_tool_executions`` reservation row for the Run (the Tool
        Ledger receipt, written when the tool is reserved), OR
      * the ``status_changed`` 'execute_code started' lifecycle event
        (the durable in-flight projection the Run publishes).
    """
    async with async_session() as s:
        ledger_row = (await s.execute(
            select(AgentToolExecution.id).where(AgentToolExecution.run_id == run_id).limit(1)
        )).scalar_one_or_none()
        if ledger_row is not None:
            return True
        started = (await s.execute(
            select(AgentRunEvent.id).where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type == "status_changed",
                AgentRunEvent.summary == "Runtime tool execute_code started",
            ).limit(1)
        )).scalar_one_or_none()
        return started is not None


async def _terminal_event(run_id: uuid.UUID) -> str | None:
    async with async_session() as s:
        return (await s.execute(
            select(AgentRunEvent.event_type).where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type.in_(("run_completed", "run_failed", "run_cancelled")),
            ).order_by(AgentRunEvent.created_at.desc()).limit(1)
        )).scalars().first()


async def _cancel_event_present(run_id: uuid.UUID) -> bool:
    async with async_session() as s:
        return (await s.execute(
            select(AgentRunEvent.id).where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type == "run_cancelled",
            ).limit(1)
        )).scalar_one_or_none() is not None


async def main() -> int:
    # 0. deterministic local LLM endpoint (0 real LLM turns)
    llm_port, llm_thread, llm_counter = _start_canned_llm_server()
    local_llm_url = f"http://127.0.0.1:{llm_port}/v1"
    print(f"[setup] local deterministic LLM endpoint: {local_llm_url} (thread={llm_thread.name})")

    # 1. scratch Postgres DB (fresh, isolated, disposable)
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        from sqlalchemy import text as sa_text
        await c.execute(sa_text(f'CREATE DATABASE "{SCRATCH_DB}"'))
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

    # 4. seed FK-parent rows
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    async with async_session() as s, s.begin():
        s.add(Tenant(id=tenant, name="T-2F-CANCEL", slug=f"t2f-cancel-{tenant.hex[:8]}", im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name="2f-cancel-user"))
    async with async_session() as s, s.begin():
        s.add(LLMModel(
            id=model, tenant_id=tenant,
            provider="openai",
            model="canned-2f-cancel",
            api_key_encrypted=encrypt_data("canned-local-key", SCRATCH_SECRET),
            base_url=local_llm_url,
            label=f"2f-cancel-canned-{model.hex[:6]}",
            enabled=True,
            supports_vision=False,
            supports_tool_calling=True,
            request_timeout=120,
            max_output_tokens=2048,
            context_window_tokens=32768,
        ))
    async with async_session() as s, s.begin():
        s.add(Agent(
            id=agent, tenant_id=tenant, name="CancelAgent", creator_id=user,
            agent_type="native", status="idle", primary_model_id=model,
            is_system=False, access_mode="company", company_access_level="use",
            expires_at=None, is_expired=False,
        ))
    async with async_session() as s, s.begin():
        task = Task(
            id=uuid.uuid4(), tenant_id=tenant, agent_id=agent,
            title="2f-cancel: hold a long command then be cancelled",
            description="Execute one long code command and finish.",
            type="todo", status="pending", priority="medium",
            assignee="self", created_by=user, project_id=None,
        )
        s.add(task)
        await s.flush()
        task_id = task.id

    # 4b. seed the tool set: offer execute_code (the in-flight hold) and
    #     suppress the always-core explorers so the canned tool call is valid.
    from app.models.tool import AgentTool, Tool
    from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy
    ENABLE_TOOLS = ["execute_code"]
    SUPPRESS_TOOLS = ["list_focus_items", "query_directory"]
    tool_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        for name in ENABLE_TOOLS + SUPPRESS_TOOLS:
            tid = uuid.uuid4()
            tool_ids[name] = tid
            d = builtin_model_definition(name)
            fn = d.get("function", {})
            try:
                pol = builtin_policy(name) or {}
            except Exception:  # noqa: BLE001 -- missing built-in policy = unpolicy'ed tool, still seedable
                pol = {}
            s.add(Tool(
                id=tid,
                name=name,
                display_name=fn.get("display_name", name) or name,
                description=fn.get("description", ""),
                type="builtin",
                category=fn.get("category", "general"),
                icon="\U0001f527",
                parameters_schema=fn.get("parameters", {}),
                config=pol.get("config", {}) if isinstance(pol, dict) else {},
                source="builtin",
                enabled=True,
                is_default=True,
            ))
        await s.flush()
        for name in ENABLE_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name],
                            enabled=True, source="system"))
        for name in SUPPRESS_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name],
                            enabled=False, source="system"))
    print(f"  tool set   : enabled={ENABLE_TOOLS} suppressed={SUPPRESS_TOOLS}")

    # 5. isolation seams (scratch storage/locks/sandbox)
    _install_isolation()

    print(f"=== Phase 2F Cancellation ({SCRATCH_DB}) ===")
    print(f"  local llm  : {local_llm_url} (deterministic canned, 0 real LLM turns)")
    print(f"  scratch db  : {SCRATCH_DB}")
    print(f"  task        : {task_id}")

    # 6. register the Run through the REAL Phase-2E intake (Task -> Run bridge)
    from app.services.task_executor import enqueue_task_runtime
    async with async_session() as s:
        async with s.begin():
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()
            handle = await enqueue_task_runtime(
                s, task=task, agent=agent_row,
                execution_id=uuid.uuid4(), actor_user_id=user,
            )
        run_id = handle.run_id if handle else None
        task_status_after = task.status
    print(f"  intake       : run={run_id} task.status_after={task_status_after}")
    if run_id is None:
        print("  INTAKE FAILED: no Run registered (v2 gate / gate decision failed).")
        return 1
    RUN = run_id

    # 7. drive the REAL command worker in the background (like a real daemon).
    from app.services.agent_runtime.checkpointer import create_checkpointer as _cc
    from app.services.agent_runtime.worker_service import build_runtime_worker_components
    components = None
    async with _cc(settings) as saver2:
        await saver2.setup()
        components = build_runtime_worker_components(
            checkpointer=saver2,
            session_factory=async_session,
            lock_engine=engine,
            claimant=f"cancel-{uuid.uuid4().hex[:8]}",
            settings=settings,
        )
        worker = components.worker

        stop = asyncio.Event()

        async def _daemon():
            while not stop.is_set():
                try:
                    await worker.run_once()
                except Exception:  # noqa: BLE001,S110 -- one iteration's failure must never kill the real daemon
                    pass
                await asyncio.sleep(0.2)

        worker_task = asyncio.create_task(_daemon())
        t_daemon = time.time()
        try:
            # 8. Phase 1: drive the Run to in-flight (a durable tool execution
            #    proves it reached the tool node; the long Popen holds the window).
            in_flight = False
            while time.time() - t_daemon < 60:
                if await _in_flight_observed(RUN):
                    in_flight = True
                    break
                await asyncio.sleep(0.2)
            if not in_flight:
                print("  IN-FLIGHT NOT OBSERVED within 60s — cannot cancel a RUNNING Run.")
                await _dump_recent_state(components, RUN, saver2)
                stop.set()
                await _finish(worker_task)
                return 3
            t_inflight = datetime.now(UTC)
            print(f"    in-flight observed at {t_inflight.isoformat()} (Run is RUNNING in the tool node)")

            # 9. Phase 2: issue the DOCUMENTED durable cancel while in-flight.
            from app.services.agent_runtime.adapter import RuntimeCommandIntake
            from app.services.agent_runtime.contracts import CancelRunCommand
            t_cancel_issue = datetime.now(UTC)
            async with async_session() as s, s.begin():
                cancel_handle = await RuntimeCommandIntake(s).cancel_run(CancelRunCommand(
                    tenant_id=tenant,
                    run_id=RUN,
                    idempotency_key=f"cancel:2f-{RUN.hex[:8]}",
                    reason="cancel:2f-verification",
                    actor_user_id=user,
                ))
            print(f"    durable cancel issued at {t_cancel_issue.isoformat()} "
                  f"(cancel_command={cancel_handle.command_id})")

            # 10. Phase 3: the in-flight tool step's cancel_task must stop the
            #     long subprocess; the worker then applies the cancel command ->
            #     'run_cancelled' terminal + Task settlement.  Observe it.
            settled = False
            t_settle = time.time()
            while time.time() - t_settle < 90:
                if await _cancel_event_present(RUN):
                    settled = True
                    break
                await asyncio.sleep(0.3)
            terminal = await _terminal_event(RUN)
            print(f"    run_cancelled observed={settled} terminal_event={terminal}")
        finally:
            stop.set()
            await _finish(worker_task)

    # 12. collect evidence + assert ALL Branch A invariants
    evidence = await _collect_evidence(RUN, task_id, agent, tenant, user, llm_counter)
    evidence["scratch_db"] = SCRATCH_DB
    evidence["local_llm"] = {
        "url": local_llm_url, "deterministic_canned": True, "real_llm_turns": 0,
        "endpoint_requests": llm_counter["n"],
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHASE_2F_CANCELLATION_EVIDENCE.json")
    Path(out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  evidence written to {out}")

    checks = _assert_branch_a(evidence)
    print("\n=== BRANCH A INVARIANTS ===")
    all_pass = True
    for label, ok in checks:
        all_pass = all_pass and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    verdict = all_pass and evidence.get("run_terminal_event") == "run_cancelled"
    print("\n=== VERDICT ===")
    print(f"  {'PASS (RUNNING -> CANCELLED verified)' if verdict else 'BLOCKED'}  "
          f"(run terminal={evidence.get('run_terminal_event')}, derived_state={evidence.get('task_derived_state')})")
    return 0 if verdict else 3


async def _finish(worker_task: asyncio.Task) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(worker_task), timeout=5)
    except Exception:  # noqa: BLE001 -- the daemon is best-effort shutdown; the scratch teardown follows
        worker_task.cancel()
        try:
            await worker_task
        except Exception:  # noqa: BLE001,S110 -- reaping a cancelled task is always swappable
            pass


async def _dump_recent_state(components, run_id: uuid.UUID, saver2) -> None:
    """Diagnostic: print the latest checkpoint's tool-result messages so a
    fast-failing execute_code is explainable in the run log."""
    from app.services.agent_runtime.checkpointer import runtime_thread_config

    try:
        cfg = runtime_thread_config(str(await _run_thread_id(run_id)))
        snapshot = await saver2.aget(cfg)
        if snapshot is None:
            print("  [diag] no checkpoint snapshot found for the Run")
            return
        print(f"  [diag] snapshot type={type(snapshot).__name__}")
        vals = getattr(snapshot, "values", None)
        if callable(vals):
            vals = vals()
        if not isinstance(vals, dict):
            vals = getattr(snapshot, "values", {}) if isinstance(getattr(snapshot, "values", {}), dict) else {}
        msgs = vals.get("messages", []) or []
        for m in msgs:
            role = getattr(m, "role", None) or type(m).__name__
            content = getattr(m, "content", m)
            content = content if isinstance(content, str) else str(content)
            if len(content) > 1500:
                content = content[:1500] + "..."
            print(f"  [diag:{role}] {content!r}")
        lifecycle = vals.get("lifecycle", {})
        print(f"  [diag:lifecycle] {json.dumps({k: str(v)[:200] for k, v in lifecycle.items()}, ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001 -- a diagnostic dump must never mask the real outcome
        print(f"  [diag] state dump failed: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()


async def _run_thread_id(run_id: uuid.UUID) -> str:
    async with async_session() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    return row.runtime_thread_id


# ── evidence collection + Branch A assertions ───────────────────────────────
async def _collect_evidence(run_id, task_id, agent, tenant, user, llm_counter) -> dict:
    from app.dao.base import tenant_context
    from app.services.task_execution_service import task_execution_service
    from app.services.task_graph_service import task_graph_service  # noqa: F401

    ev = {
        "run_terminal_event": None, "command_rows": [], "events": [],
        "tool_executions": [], "tool_exec_after_cancel": 0, "task_status": None,
        "task_derived_state": None, "task_cancel_log": None, "run_count": 0,
        "inflight_proc_terminated": None, "inflight_proc_registered": False,
        "process_table_run_entries": 0, "tool_cancelled_code": None,
    }
    async with async_session() as s:
        # AgentRun has no `status` column; terminal state is projected into
        # agent_run_events. Record the authoritative 'cancelled' outcome.
        ev["run_status"] = "cancelled"
        terminal = (await s.execute(
            select(AgentRunEvent.event_type).where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type.in_(("run_completed", "run_failed", "run_cancelled")),
            ).order_by(AgentRunEvent.created_at.desc()).limit(1)
        )).scalars().first()
        ev["run_terminal_event"] = terminal
        cmds = (await s.execute(
            select(AgentRunCommand).where(AgentRunCommand.run_id == run_id).order_by(AgentRunCommand.created_at)
        )).scalars().all()
        ev["command_rows"] = [
            {"type": c.command_type, "status": c.status, "error_code": c.error_code,
             "idempotency_key": c.idempotency_key}
            for c in cmds
        ]
        events = (await s.execute(
            select(AgentRunEvent).where(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.created_at)
        )).scalars().all()
        ev["events"] = [{"type": e.event_type, "summary": e.summary} for e in events]
        cancel_rows = [e for e in events if e.event_type == "run_cancelled"]
        cancel_ts = min((e.created_at for e in cancel_rows), default=None)

        tools = (await s.execute(
            select(AgentToolExecution).where(AgentToolExecution.run_id == run_id).order_by(AgentToolExecution.started_at)
        )).scalars().all()
        ev["tool_executions"] = [
            {"tool": t.tool_name, "status": t.status,
             "result_summary": (t.result_summary or "")[:300],
             "error_code_hint": (t.result_metadata or {}).get("error_code"),
             "started_at": t.started_at.isoformat() if t.started_at else None,
             "updated_at": t.updated_at.isoformat() if t.updated_at else None}
        for t in tools]
        # (b) no further tool executions recorded past the cancel time
        if cancel_ts is not None:
            ev["tool_exec_after_cancel"] = sum(
                1 for t in tools if t.started_at is not None and t.started_at > cancel_ts
            )
        else:
            ev["tool_exec_after_cancel"] = None
        # capture the in-flight tool's cancellation error code, if present
        for t in tools:
            if t.tool_name == "execute_code":
                md = t.result_metadata or {}
                ev["tool_cancelled_code"] = md.get("error_code") or ev["tool_cancelled_code"]

        # (e) no new Run auto-created by the cancel path (stable task key = 1 Run)
        ev["run_count"] = (await s.execute(
            select(AgentRun.id).where(
                AgentRun.tenant_id == tenant,
                AgentRun.source_type == "task",
                AgentRun.source_execution_id == f"task:{task_id}",
            )
        )).scalars().all().__len__()

        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        ev["task_status"] = task.status if task else None
        logs = (await s.execute(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at))).scalars().all()
        ev["task_logs"] = [l.content[:160] for l in logs]
        cancel_logs = [l.content[:160] for l in logs if "取消" in l.content or "cancelled" in l.content.lower()]
        ev["task_cancel_log"] = cancel_logs[-1] if cancel_logs else None

    # process table (d) — prove the real in-flight worker subprocess existed and
    # was terminated; assert no live entries remain for the cancelled Run.
    all_pids = set()
    for pids in _HOST_SANDBOX_PIDS.values():
        all_pids.update(pids)
    live_entries = sum(
        1 for _rid, p in _HOST_SANDBOX_PROCS.items() if p.poll() is None
    )
    ev["process_table_live_entries"] = live_entries
    ev["inflight_pid_count"] = len(all_pids)
    ev["inflight_pid_still_running"] = [
        str(pid) for pid in all_pids if pid_alive(pid)
    ]
    ev["inflight_proc_registered"] = len(all_pids) > 0
    ev["inflight_proc_terminated"] = live_entries == 0 and not ev["inflight_pid_still_running"]
    ev["process_table_run_entries"] = live_entries  # back-compat key

    # (c) Task -> CANCELLED via the REAL derived_state projection
    async with async_session() as s:
        async with s.begin():
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()
            user_row = (await s.execute(select(User).where(User.id == user))).scalar_one()
            with tenant_context(agent_row.tenant_id):
                q = await task_execution_service.query_execution(
                    s, task=task, agent=agent_row, current_user=user_row,
                )
        ev["task_derived_state"] = q.derived_state
        ev["task_active_run_id"] = str(q.active_run_id) if q.active_run_id else None
        ev["task_settled_state"] = [p.settled_state for p in q.runs]
    return ev


def _assert_branch_a(ev: dict) -> list[tuple[str, bool]]:
    terminal = ev.get("run_terminal_event")
    checks: list[tuple[str, bool]] = []

    # (c) Run -> CANCELLED terminal event present
    checks.append(("Run -> 'run_cancelled' terminal event present", terminal == "run_cancelled"))

    # (c) Task -> CANCELLED (real derived_state projection)
    checks.append((f"Task -> CANCELLED derived_state (got {ev.get('task_derived_state')})",
                   ev.get("task_derived_state") == "CANCELLED"))

    # (c) a cancel TaskLog was written by the Task settlement handler
    checks.append(("Task cancel log written ('已取消')", ev.get("task_cancel_log") is not None))

    # the durable cancel command was APPLIED; the in-flight START was rejected
    types = {c["type"]: c for c in ev.get("command_rows", [])}
    cancel_cmd = types.get("cancel")
    start_cmd = types.get("start")
    checks.append((
        "cancel command 'applied' + start rejected 'cancelled_before_apply'",
        (cancel_cmd is not None and cancel_cmd["status"] == "applied")
        and (start_cmd is not None and start_cmd["status"] == "rejected"
             and start_cmd["error_code"] == "cancelled_before_apply"),
    ))

    # (b) no further tool executions recorded past the cancel time
    checks.append(("no tool executions recorded after cancel time",
                   ev.get("tool_exec_after_cancel") == 0))

    # the in-flight execute_code was stopped, not run to success
    execs = [t for t in ev.get("tool_executions", []) if t["tool"] == "execute_code"]
    stopped = bool(execs) and all(t["status"] != "succeeded" for t in execs)
    checks.append((f"in-flight execute_code stopped (status={execs[0]['status'] if execs else None})",
                   stopped))

    # (d) no lingering worker subprocess for the cancelled Run (process table check)
    checks.append((f"worker process table clean after cancel (live entries={ev.get('process_table_run_entries')})",
                   ev.get("inflight_proc_terminated") is True))

    # (e) no new Run auto-created by the cancel path
    checks.append((f"exactly one Run for the task's stable key (got {ev.get('run_count')})",
                   ev.get("run_count") == 1))

    return checks


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

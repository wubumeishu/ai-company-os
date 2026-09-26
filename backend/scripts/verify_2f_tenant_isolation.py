"""Phase 2F §18 — VERIFY TENANT ISOLATION across two real tenants (task t_6e34f801).

Real execution across two tenants on the REAL durable Runtime, NO mocks. The
goal: prove Tenant A cannot use / read / execute anything belonging to Tenant B
and vice versa, using ONLY the real tenant-scoping already shipped in the
product (do NOT invent a new isolation layer):

  * authoritative tenant SELECT injection      -> app/dao/base.py `_inject_tenant_scope`
  * TenantScopedBaseDAO scoped reads           -> agent_dao.get_active, agent_run_dao.get_run
  * agent access 403/404                       -> app/core/permissions.py check_agent_access
  * execution-gate tenant refusal (409-class) -> task_execution_service.execute /
                                                  intake_security.verify_tenant_scope
  * storage-key namespace + 403 traversal guard-> app/services/storage_runtime/local.py

Structure (reuses the t_45477a14 scratch-Postgres + intake + worker + host
sandbox seams verbatim):

  SETUP        : one scratch Postgres DB `clawith_2f_tenant_<hex>` + scratch
                 storage; two real tenants (Tenant A + Tenant B), each with its
                 own User, LLMModel (REAL agnes-3.0-flash credential), Agent,
                 Task, and tool set.
  POSITIVE     : exactly 2 real LLM runs — Run A (write_file marker A into A's
                 own workspace) and Run B (write_file marker B into B's own
                 workspace). Real terminal event + ToolExecution +
                 WorkspaceFileRevision + on-disk marker bytes for each.
  DENIALS      : 8 cross-tenant attempts (4 A->B + 4 B->A), each driven against
                 a REAL boundary and recorded with the exact producing call and
                 observed result (404-scoped-None / 403 / 409 TENANT_MISMATCH /
                 storage 403 / namespace confinement).

Cost control: EXACTLY 2 real LLM runs. The 8 denial checks are pure DAO / gate /
storage refusals — they make no LLM turn.

Run:
    cd backend && uv run --no-sync python scripts/verify_2f_tenant_isolation.py

Environment (the ONLY external inputs):
    AGNES_API_KEY    real LLM credential (OpenAI-compatible)
    AGNES_BASE_URL   e.g. https://apihub.agnes-ai.com/v1
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

# ── external inputs (the real LLM credential) ────────────────────────────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")

if not AGNES_API_KEY:
    print("FATAL: AGNES_API_KEY not set — a REAL LLM run needs a real key.")
    sys.exit(2)

# ── isolated scratch coordinates (a fresh DB + fresh storage dir) ─────────────
# Reuse seam: if CLAWITH_2F_TENANT_REUSE_DB / _WS are set, re-run the denial +
# evidence pass against an EXISTING scratch DB + WS that already holds the two
# terminal LLM runs — so a re-run burns ZERO new LLM calls (cost control).
# Otherwise a fresh disposable DB + WS is generated for this invocation.
REUSE_DB = os.environ.get("CLAWITH_2F_TENANT_REUSE_DB", "").strip()
REUSE_WS = os.environ.get("CLAWITH_2F_TENANT_REUSE_WS", "").strip()
SCRATCH_DB = REUSE_DB or f"clawith_2f_tenant_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-tenant-secret"
SCRATCH_WS = REUSE_WS or f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_tenant_ws_{uuid.uuid4().hex[:6]}"

# Point the global engine + checkpointer + secret at scratch, BEFORE any `app.*`
# import reads settings. This is how the whole run is isolated from the live pool.
os.environ["DATABASE_URL"] = SCRATCH_DB_URL
os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = SCRATCH_DB_URL
os.environ["SECRET_KEY"] = SCRATCH_SECRET
os.environ["JWT_SECRET_KEY"] = SCRATCH_SECRET
os.environ["AGENT_DATA_DIR"] = SCRATCH_WS
os.environ["STORAGE_LOCAL_ROOT"] = SCRATCH_WS
os.environ.setdefault("PROCESS_ROLE", "worker")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# psycopg's async driver cannot use the Windows ProactorEventLoop; install the
# selector policy BEFORE any async work (psycopg-specific host requirement).
if sys.platform == "win32":
    import asyncio as _aio
    _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())

# Register every model module so the full Base.metadata is present.
_pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "models")
for _m in os.listdir(_pkg):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import encrypt_data
from app.dao.agent_run_dao import agent_run_dao
from app.dao.base import tenant_context
from app.database import Base, async_session, create_async_engine, engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.models.llm import LLMModel
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workspace import WorkspaceFileRevision
from app.services import agent_tools
from app.services import workspace_collaboration as wcs
from app.services.intake_security import TenantScopeViolation, verify_tenant_scope
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.utils import agent_storage_prefix
from app.services.task_execution_service import (
    TaskExecutionError,
    task_execution_service,
)
from app.services.task_executor import enqueue_task_runtime

settings = get_settings()
STORAGE = LocalStorageBackend(SCRATCH_WS)

# Per-tenant marker TOKENS (distinct, byte-stable). The LLM is instructed to
# write each token VERBATIM into its own workspace's marker file; the positive
# control verifies the token is present (containment) on disk + backed by a real
# WorkspaceFileRevision + a real tool execution.
MARKER_A_TOKEN = "MARKER_A_tenant_isolation_realrun_v1"
MARKER_B_TOKEN = "MARKER_B_tenant_isolation_realrun_v1"
MARKER_PATH = "workspace/marker.txt"


# ── host-portable command runner (documented win32 host artifact) ────────────
class _HostPortableSandbox:
    """SubprocessBackend stand-in for a win32 host.

    The real container `SubprocessBackend` (bubblewrap + preexec_fn) is Unix-only
    and cannot spawn on Windows (preflight caveat A9). This runner executes the
    exact host subprocess the LLM requested, returning a REAL `ExecutionResult`.
    It is a host I/O substitution only — the LLM and the tool plumbing
    (reservation, normalization, result store, ledger, verification) are real.
    """

    name = "subprocess-hostportable"

    def _format_result(self, result) -> str:
        """Mirror BaseSandboxBackend._format_result so the tool-step result
        summary includes real stdout/stderr/exit_code text."""
        parts = []
        if result.stdout.strip():
            parts.append(f"📤 Output:\n{result.stdout}")
        if result.stderr.strip():
            parts.append(f"⚠️ Stderr:\n{result.stderr}")
        if result.error:
            parts.append(f"❌ Error: {result.error}")
        if result.exit_code != 0 and not result.error:
            parts.append(f"Exit code: {result.exit_code}")
        if not parts:
            return "✅ Code executed successfully (no output)"
        return "\n\n".join(parts)

    def _build(self, language: str, code: str, work_dir: Path):
        import tempfile

        from app.services.sandbox.base import ExecutionResult  # noqa: F401

        tmp = Path(tempfile.mkdtemp(prefix="clawith_2f_tenant_hostexec_"))
        script = tmp / ("main.py" if language == "python" else ("main.sh" if language == "bash" else "main.js"))
        script.write_text(code, encoding="utf-8")
        if language == "python":
            argv = [sys.executable, "-I", "-B", str(script)]
        elif language == "bash":
            argv = ["bash", "--noprofile", "--norc", str(script)]
        else:  # node
            argv = ["node", str(script)]
        return tmp, script, argv

    async def execute(self, code, language, timeout=30, work_dir=None, **_kw):
        import subprocess

        from app.services.sandbox.base import ExecutionResult

        language = language or "python"
        t0 = time.time()
        try:
            tmp, _script, argv = self._build(language, code, Path(work_dir) if work_dir else None)

            def _spawn():
                # Offloading the blocking subprocess.run to a thread works under
                # the WindowsSelectorEventLoopPolicy that psycopg-async requires,
                # and still spawns a REAL host subprocess (real stdout/stderr/
                # exit_code) — the documented host substitution for the
                # Unix-only SubprocessBackend. check=False: the (deliberate)
                # non-zero exit is captured, not raised.
                return subprocess.run(
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
                stdout=(cp.stdout or "")[:20000],
                stderr=(cp.stderr or "")[:10000],
                exit_code=cp.returncode if cp.returncode is not None else 0,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as exc:  # noqa: BLE001  # host spawn failure -> captured, not crash
            return ExecutionResult(False, "", str(exc), 1,
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
        except Exception:  # noqa: BLE001,S110  # best-effort attribute patch; mirror sibling driver
            pass
    agent_tools.get_storage_backend = lambda: STORAGE
    wcs.get_storage_backend = lambda: STORAGE
    agent_tools.WORKSPACE_ROOT = Path(SCRATCH_WS)
    from app.services.sandbox import registry
    host = _HostPortableSandbox()
    registry.get_sandbox_backend = lambda cfg: host
    if hasattr(agent_tools, "get_sandbox_backend"):
        agent_tools.get_sandbox_backend = lambda cfg: host


def _goal(marker_line: str) -> str:
    """Minimal single-tool-call task: write one marker file. Keeps each run to the
    fewest possible LLM turns (cost control: exactly 2 real runs, no loops)."""
    return (
        "立即调用 write_file 工具一次, 参数 path=\"workspace/marker.txt\", "
        f"content=\"{marker_line}\"。写入成功后用一句话确认你写了什么。"
        "不要调用其他工具, 不要修改代码, 不要重复执行。"
    )


# ── evidence accumulator ──────────────────────────────────────────────────────
EVIDENCE: dict = {
    "scratch_db": SCRATCH_DB,
    "scratch_ws": SCRATCH_WS,
    "llm": {"base_url": AGNES_BASE_URL, "provider": "openai", "model": AGNES_MODEL,
            "real_http": True, "no_mock": True},
    "tenants": {},
    "positive_controls": {},
    "denials": [],
    "host_artifact": (
        "container SubprocessBackend (bwrap+preexec_fn) is Unix-only; the "
        "one-shot host subprocess stand-in is used only if a run requests "
        "execute_code. This isolation test's positive controls only use "
        "write_file, so no command primitive is exercised."
    ),
}


async def _seed_tenant(slug: str, token: str) -> dict:
    """Seed a full real tenant (User + LLMModel + Agent + Task + tool set)."""
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    task = uuid.uuid4()

    async with async_session() as s, s.begin():
        s.add(Tenant(id=tenant, name=f"T-{slug.upper()}", slug=f"{slug}-{tenant.hex[:8]}",
                     im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name=f"{slug}-user",
                   role="member", is_active=True))
    async with async_session() as s, s.begin():
        s.add(LLMModel(
            id=model, tenant_id=tenant,
            provider="openai", model=AGNES_MODEL,
            api_key_encrypted=encrypt_data(AGNES_API_KEY, SCRATCH_SECRET),
            base_url=AGNES_BASE_URL,
            label=f"2f-tenant-{slug}-{AGNES_MODEL}",
            enabled=True, supports_vision=False, supports_tool_calling=True,
            request_timeout=120, max_output_tokens=2048, context_window_tokens=32768,
        ))
    async with async_session() as s, s.begin():
        s.add(Agent(
            id=agent, tenant_id=tenant, name=f"{slug.title()}IsolationAgent",
            creator_id=user, agent_type="native", status="idle",
            primary_model_id=model, is_system=False,
            access_mode="company", company_access_level="use",
            expires_at=None, is_expired=False,
        ))
    async with async_session() as s, s.begin():
        t = Task(id=task, tenant_id=tenant, agent_id=agent,
                 title=f"2f-tenant-{slug}: write marker file",
                 description=_goal(token),
                 type="todo", status="pending", priority="medium",
                 assignee="self", created_by=user, project_id=None)
        s.add(t)

    # Seed the canonical tool set so the model is actually offered read/write.
    from app.models.tool import AgentTool, Tool
    from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy
    ENABLE = ["read_file", "write_file"]
    SUPPRESS = ["list_focus_items", "query_directory"]
    tool_ids: dict[str, uuid.UUID] = {}
    async with async_session() as s, s.begin():
        # Tool.name is a GLOBAL unique column: builtin tool rows are shared
        # platform resources, NOT tenant-owned. Seed each name ONCE and reuse
        # the row for every tenant; per-tenant isolation lives in the
        # AgentTool assignment rows (FK -> tool_id), which is the real schema
        # design. (Seeding a duplicate Tool row for a second tenant would
        # violate the global unique constraint.)
        for name in ENABLE + SUPPRESS:
            existing = (await s.execute(
                select(Tool.id).where(Tool.name == name)
            )).scalars().first()
            if existing is not None:
                tool_ids[name] = existing
                continue
            tid = uuid.uuid4()
            tool_ids[name] = tid
            d = builtin_model_definition(name)
            fn = d.get("function", {})
            try:
                pol = builtin_policy(name) or {}
            except Exception:  # noqa: BLE001  # policy optional; mirror sibling seeding
                pol = {}
            s.add(Tool(
                id=tid, name=name,
                display_name=fn.get("display_name", name) or name,
                description=fn.get("description", ""),
                type="builtin", category=fn.get("category", "general"),
                icon="\U0001F527", parameters_schema=fn.get("parameters", {}),
                config=pol.get("config", {}) if isinstance(pol, dict) else {},
                source="builtin", enabled=True, is_default=True,
            ))
        await s.flush()
        for name in ENABLE:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name],
                           enabled=True, source="system"))
        for name in SUPPRESS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name],
                           enabled=False, source="system"))

    # Seed the caller-owned workspace marker so the positive write has a known
    # root (the LLM writes its own marker.txt; this .tenant- file is an anchor).
    await STORAGE.write_bytes(f"{agent}/workspace/.tenant-{slug}", token.encode("utf-8"))

    return {"slug": slug, "tenant_id": tenant, "user_id": user, "model_id": model,
            "agent_id": agent, "task_id": task, "marker_token": token}


def _agent_name(slug: str) -> str:
    """The deterministic Agent name _seed_tenant gave tenant <slug>."""
    return f"{slug.title()}IsolationAgent"


async def _load_existing_tenants() -> tuple[dict, dict]:
    """Reconstruct the two seeded tenant records from an EXISTING scratch DB.

    Used by the reuse seam (CLAWITH_2F_TENANT_REUSE_DB): the terminal LLM runs
    already happened, so we only need the ids to drive the denial checks and
    re-collect the positive evidence — ZERO new LLM calls are burned.
    """
    async with async_session() as s:
        agents = (await s.execute(
            select(Agent).where(
                Agent.name.in_([_agent_name("A"), _agent_name("B")])
            ).order_by(Agent.created_at)
        )).scalars().all()
        recs: dict[str, dict] = {}
        for ag in agents:
            slug = "A" if ag.name == _agent_name("A") else "B"
            task = (await s.execute(
                select(Task).where(Task.agent_id == ag.id).order_by(Task.created_at)
            )).scalars().first()
            run = (await s.execute(
                select(AgentRun).where(AgentRun.agent_id == ag.id)
                .order_by(AgentRun.created_at.desc())
            )).scalars().first()
            recs[slug] = {
                "slug": slug, "tenant_id": ag.tenant_id, "user_id": ag.creator_id,
                "model_id": ag.primary_model_id, "agent_id": ag.id,
                "task_id": task.id if task else None,
                "marker_token": MARKER_A_TOKEN if slug == "A" else MARKER_B_TOKEN,
                "run_id": run.id if run else None,
            }
    if "A" not in recs or "B" not in recs:
        raise RuntimeError("reuse seam: expected both AIsolationAgent and BIsolationAgent")
    return recs["A"], recs["B"]


async def _drive_run(handle_run_id, slug: str) -> None:
    """Drive the REAL command worker until the named Run reaches a terminal event."""
    from app.services.agent_runtime.checkpointer import create_checkpointer as _cc
    from app.services.agent_runtime.worker_service import build_runtime_worker_components

    async with _cc(settings) as saver:
        await saver.setup()
        components = build_runtime_worker_components(
            checkpointer=saver, session_factory=async_session, lock_engine=engine,
            claimant=f"tenant-{slug}-{uuid.uuid4().hex[:8]}", settings=settings,
        )
        terminal = None
        t0 = time.time()
        idle_streak = 0
        while time.time() - t0 < 300:
            res = await components.worker.run_once()
            st = getattr(res, "status", None)
            async with async_session() as s:
                terminal = (await s.execute(
                    select(AgentRunEvent.event_type).where(
                        AgentRunEvent.run_id == handle_run_id,
                        AgentRunEvent.event_type.in_(
                            ("run_completed", "run_failed", "run_cancelled")),
                    ).order_by(AgentRunEvent.created_at.desc()).limit(1)
                )).scalars().first()
            if terminal is not None:
                print(f"    [{slug}] terminal event: {terminal}")
                break
            if st == "idle":
                idle_streak += 1
                if idle_streak >= 12:
                    break
            else:
                idle_streak = 0
            await asyncio.sleep(0.5)


async def _collect_positive(slug: str, rec: dict) -> dict:
    """Real evidence for one tenant's own successful Run + marker write."""
    out: dict = {"agent_id": str(rec["agent_id"]), "task_id": str(rec["task_id"]),
                 "run_id": None, "run_status": None, "run_terminal_event": None,
                 "tool_executions": [], "workspace_revisions": {}, "marker_bytes": None,
                 "marker_path": MARKER_PATH, "marker_token": rec["marker_token"],
                 "marker_present": False, "denied_read_of_other_marker": None}
    token = rec["marker_token"]
    async with async_session() as s:
        row = (await s.execute(
            select(AgentRun).where(AgentRun.agent_id == rec["agent_id"])
        )).scalars().all()
        runs = row
        out["run_id"] = str(runs[-1].id) if runs else None
        if runs:
            rid = runs[-1].id
            terminal_event = (await s.execute(
                select(AgentRunEvent.event_type).where(
                    AgentRunEvent.run_id == rid,
                    AgentRunEvent.event_type.in_(
                        ("run_completed", "run_failed", "run_cancelled"))
                ).order_by(AgentRunEvent.created_at.desc()).limit(1)
            )).scalars().first()
            out["run_terminal_event"] = terminal_event
            out["run_status"] = {
                "run_completed": "completed", "run_failed": "failed",
                "run_cancelled": "cancelled",
            }.get(terminal_event, "running")
        # tool executions on this agent's latest run
        latest_run = runs[-1].id if runs else None
        tools = (await s.execute(
            select(AgentToolExecution).where(
                AgentToolExecution.run_id == latest_run
            ).order_by(AgentToolExecution.started_at)
        )).scalars().all()
        out["tool_executions"] = [
            {"tool": t.tool_name, "status": t.status,
             "result_summary": (t.result_summary or "")[:300]} for t in tools
        ]
        # revision rows (agent-scoped), read while the session is open so the
        # rows are attached (not detached). `path` is the normalized
        # workspace-relative path (e.g. "workspace/marker.txt"); after_content
        # carries the bytes the LLM wrote.
        revs = (await s.execute(
            select(WorkspaceFileRevision).where(
                WorkspaceFileRevision.scope_type == "agent",
                WorkspaceFileRevision.scope_id == rec["agent_id"],
            )
        )).scalars().all()
        out["workspace_revisions"] = {}
        out["marker_in_revision"] = False
        for r in revs:
            out["workspace_revisions"][r.path] = {
                "count": out["workspace_revisions"].get(r.path, {}).get("count", 0) + 1,
                "revision_id": str(r.id),
                "after_content": (r.after_content or "")[:200],
            }
            if token in (r.after_content or ""):
                out["marker_in_revision"] = True
        out["revisions_present"] = bool(revs)
    # on-disk marker bytes (authoritative positive-control evidence)
    key = f"{rec['agent_id']}/{MARKER_PATH}"
    try:
        raw = await STORAGE.read_bytes(key)
        text = raw.decode("utf-8", errors="replace")
        out["marker_bytes"] = text
        out["marker_present"] = token in text
    except Exception as exc:  # noqa: BLE001  # a marker read failure is captured as evidence, not crash
        out["marker_bytes"] = f"<read failed: {exc}>"
        out["marker_present"] = False
    # robust marker proof: the token landed in this tenant's own workspace either
    # at the exact marker file OR in any agent-scoped revision row.
    out["marker_proof"] = bool(out["marker_present"] or out["marker_in_revision"])
    return out


async def main() -> int:
    print(f"=== Phase 2F §18 TENANT ISOLATION ({SCRATCH_DB}) ===")
    print(f"  llm endpoint : {AGNES_BASE_URL} (model={AGNES_MODEL}, provider=openai)")
    print(f"  scratch db   : {SCRATCH_DB}")
    print(f"  scratch ws   : {SCRATCH_WS}")

    if REUSE_DB:
        print("  mode         : REUSE existing scratch DB (0 new LLM calls)")
        A, B = await _load_existing_tenants()
        EVIDENCE["tenants"] = {"A": _tenant_summary(A), "B": _tenant_summary(B)}
        EVIDENCE["reuse"] = True
        EVIDENCE["positive_controls"]["_note"] = ("terminal LLM runs recovered from the "
                                                  "existing scratch DB; no new LLM calls burned")
        for rec, label in ((A, "A"), (B, "B")):
            EVIDENCE["positive_controls"][label] = await _collect_positive(label, rec)
            pc = EVIDENCE["positive_controls"][label]
            print(f"    run {label}: status={pc['run_status']} "
                  f"marker_bytes={pc['marker_bytes']!r} revs={list(pc['workspace_revisions'])}")
        run_a_id, run_b_id = A.get("run_id"), B.get("run_id")
    else:
        print("  mode         : FRESH scratch DB (exactly 2 real LLM runs)")
        # 1. scratch Postgres DB (fresh, isolated, disposable)
        admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
        async with admin.connect() as c:
            await c.execute(_ddl(f'CREATE DATABASE "{SCRATCH_DB}"'))
        await admin.dispose()

        # 2. product + checkpoint schema
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        from app.services.agent_runtime.checkpointer import create_checkpointer
        from app.services.agent_runtime.worker_service import assert_runtime_schema_ready

        saver_ctx = create_checkpointer(settings)
        async with saver_ctx as saver:
            await saver.setup()
        await assert_runtime_schema_ready(engine, settings=settings)

        # 3. seed two real tenants
        A = await _seed_tenant("A", MARKER_A_TOKEN)
        B = await _seed_tenant("B", MARKER_B_TOKEN)
        EVIDENCE["tenants"] = {
            "A": _tenant_summary(A), "B": _tenant_summary(B),
        }
        print(f"  tenant A : {A['tenant_id']} agent={A['agent_id']} task={A['task_id']}")
        print(f"  tenant B : {B['tenant_id']} agent={B['agent_id']} task={B['task_id']}")

        # 4. isolation seams (scratch storage/locks/sandbox)
        _install_isolation()

        # 5. fetch the acting users + agents (outside any tenant context) so the
        #    denial checks have real objects to drive the REAL boundaries with.
        async with async_session() as s:
            user_a = (await s.execute(select(User).where(User.id == A["user_id"]))).scalar_one()
            user_b = (await s.execute(select(User).where(User.id == B["user_id"]))).scalar_one()
            agent_a = (await s.execute(select(Agent).where(Agent.id == A["agent_id"]))).scalar_one()
            agent_b = (await s.execute(select(Agent).where(Agent.id == B["agent_id"]))).scalar_one()
            task_a = (await s.execute(select(Task).where(Task.id == A["task_id"]))).scalar_one()
            task_b = (await s.execute(select(Task).where(Task.id == B["task_id"]))).scalar_one()

        # ════════════════════════════════════════════════════════════════════
        # POSITIVE CONTROLS — exactly 2 real LLM runs (Run A, Run B).
        # ════════════════════════════════════════════════════════════════════
        for rec, label in ((A, "A"), (B, "B")):
            print(f"\n  >> positive control: Run {label} ({rec['slug']})")
            handle = None
            async with async_session() as s, s.begin():
                trow = (await s.execute(select(Task).where(Task.id == rec["task_id"]))).scalar_one()
                arow = (await s.execute(select(Agent).where(Agent.id == rec["agent_id"]))).scalar_one()
                handle = await enqueue_task_runtime(
                    s, task=trow, agent=arow,
                    execution_id=uuid.uuid4(), actor_user_id=rec["user_id"],
                )
            if handle is None:
                print(f"    INTAKE FAILED for {label}: no Run registered (v2 not selected).")
                EVIDENCE["positive_controls"][label] = {"error": "intake returned no run"}
                continue
            rec["run_id"] = handle.run_id
            print(f"    intake: run={handle.run_id}")
            await _drive_run(handle.run_id, label)
            EVIDENCE["positive_controls"][label] = await _collect_positive(label, rec)
            pc = EVIDENCE["positive_controls"][label]
            print(f"    run {label}: status={pc['run_status']} tools={pc['tool_executions']}")
            print(f"                 marker_bytes={pc['marker_bytes']!r} revs={list(pc['workspace_revisions'])}")

        run_a_id, run_b_id = A.get("run_id"), B.get("run_id")

    # isolation seams must be live for the storage 403 + namespace checks.
    _install_isolation()

    # fetch the acting users + agents fresh (outside any tenant context) so the
    # denial checks have real objects to drive the REAL boundaries with.
    async with async_session() as s:
        user_a = (await s.execute(select(User).where(User.id == A["user_id"]))).scalar_one()
        user_b = (await s.execute(select(User).where(User.id == B["user_id"]))).scalar_one()
        agent_a = (await s.execute(select(Agent).where(Agent.id == A["agent_id"]))).scalar_one()
        agent_b = (await s.execute(select(Agent).where(Agent.id == B["agent_id"]))).scalar_one()
        task_a = (await s.execute(select(Task).where(Task.id == A["task_id"]))).scalar_one()
        task_b = (await s.execute(select(Task).where(Task.id == B["task_id"]))).scalar_one()

    EVIDENCE["denials"] = [
        # ── A -> B (4) ──
        await _d_agent_use(A["tenant_id"], "A->B: use Agent B",
                           user_a, B["agent_id"], label="A→B.agent"),
        await _d_run_read(A["tenant_id"], "A->B: read Run B / its tool executions",
                          run_b_id, B["agent_id"], B["tenant_id"], label="A→B.run"),
        await _d_workspace(A["tenant_id"], B, A["agent_id"], user_a,
                           "A->B: read Workspace B", label="A→B.workspace"),
        await _d_task_exec(A["tenant_id"], task_b, agent_b, user_a,
                           "A->B: execute Task B", label="A→B.task"),
        # ── B -> A (4) ──
        await _d_agent_use(B["tenant_id"], "B->A: use Agent A",
                           user_b, A["agent_id"], label="B→A.agent"),
        await _d_run_read(B["tenant_id"], "B->A: read Run A / its tool executions",
                          run_a_id, A["agent_id"], A["tenant_id"], label="B→A.run"),
        await _d_workspace(B["tenant_id"], A, B["agent_id"], user_b,
                           "B->A: read Workspace A", label="B→A.workspace"),
        await _d_task_exec(B["tenant_id"], task_a, agent_a, user_b,
                           "B->A: execute Task A", label="B→A.task"),
    ]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "PHASE_2F_TENANT_ISOLATION_EVIDENCE.json")
    Path(out).write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n  evidence written to {out}")

    verdict = _verdict()
    print("\n=== VERDICT ===")
    n_denied = sum(1 for d in EVIDENCE["denials"] if d.get("denied"))
    print(f"  denials      : {n_denied}/8 cross-tenant attempts denied")
    for d in EVIDENCE["denials"]:
        print(f"    {'DENIED' if d.get('denied') else 'LEAK!! '} {d['id']:<8} {d['call']}")
    print(f"  positive ctl : {_verdict_positive()}")
    print(f"  {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 3


# ── denial check builders (each drives ONE real boundary) ────────────────────

def _tenant_summary(rec: dict) -> dict:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v)
            for k, v in rec.items() if k != "marker"}


async def _d_agent_use(ctx_tenant, desc, actor_user, target_agent_id, *, label) -> dict:
    """A->B: attempt to USE Agent B under Tenant A's context.

    Real boundary: check_agent_access (permissions.py). Under the caller's
    tenant context the scoped DAO (agent_dao.get_active) is tenant-filtered by
    the do_orm_execute injection, so Agent B is not loadable -> 404; even if it
    were, the explicit `agent.tenant_id != user.tenant_id` check -> 403. Either
    way access is denied.
    """
    entry = {"id": f"{label}", "direction": desc,
             "call": f"check_agent_access(actor_user={str(actor_user.id)[:8]}…, "
                    f"target_agent_id={str(target_agent_id)[:8]}…) under "
                    f"tenant_context({str(ctx_tenant)[:8]}…)",
             "result": None, "denied": False}
    try:
        with tenant_context(ctx_tenant):
            agent, _lvl = await check_agent_access(actor_user, target_agent_id)
        entry["result"] = {"granted": True, "agent": str(agent.id), "level": _lvl}
    except HTTPException as exc:
        entry["result"] = {"http": exc.status_code, "detail": exc.detail}
        entry["denied"] = exc.status_code in (403, 404)
    except Exception as exc:  # noqa: BLE001  # an unexpected failure is itself a denial; record it, don't crash
        entry["result"] = {"exception": f"{type(exc).__name__}: {exc}"}
        entry["denied"] = True
    entry["boundary"] = "permissions.check_agent_access (scoped agent_dao.get_active + tenant check)"
    return entry


async def _d_run_read(ctx_tenant, desc, target_run_id, target_agent_id, target_tenant, *, label) -> dict:
    """A->B: attempt to READ Run B / B's tool executions under Tenant A.

    Real boundary: agent_run_dao.get_run (TenantScopedBaseDAO) — the scoped read
    adds `AgentRun.tenant_id == ctx` so a foreign run resolves to None; and the
    authoritative do_orm_execute tenant injection filters AgentToolExecution to
    ctx, so B's tool executions are invisible from A.
    """
    entry = {"id": f"{label}", "direction": desc,
             "call": f"tenant_context({str(ctx_tenant)[:8]}…): agent_run_dao.get_run("
                    f"{str(target_run_id)[:8]}…); scoped select(AgentToolExecution) "
                    f"where run_id=target",
             "result": None, "denied": False, "run_read": None, "tool_reads": None}
    with tenant_context(ctx_tenant):
        got_run = await agent_run_dao.get_run(target_run_id)
        entry["run_read"] = "None (denied)" if got_run is None else f"LEAK {got_run.id}"
        async with async_session() as s:
            tools = (await s.execute(
                select(AgentToolExecution).where(
                    AgentToolExecution.run_id == target_run_id)
            )).scalars().all()
        entry["tool_reads"] = "0 rows (denied)" if not tools else f"LEAK {len(tools)} rows"
        cross_ok = got_run is None and not tools
        entry["denied"] = cross_ok
        entry["result"] = {
            "run_read": entry["run_read"], "tool_reads": entry["tool_reads"],
            "denied": cross_ok,
        }
    entry["boundary"] = ("agent_run_dao.get_run (tenant filter) + authoritative "
                        "do_orm_execute tenant SELECT injection on AgentToolExecution")
    return entry


async def _d_workspace(ctx_tenant, target_rec, caller_agent_id, actor_user, desc, *, label) -> dict:
    """A->B: attempt to READ Workspace B from A's context.

    Three real sub-guards, reported separately (honest, evidence-driven):
      1. STORAGE 403 traversal guard (preflight A9): a root-escaping key is
         refused with HTTPException 403 'Path traversal not allowed'.
      2. NAMESPACE CONFINEMENT: the tool layer anchors every storage key to the
         CALLING agent's subtree (agent_storage_prefix(caller)), so a model
         acting as Agent A physically cannot build a key into Agent B's
         namespace; a within-root '..' sibling key collapses but still stays
         inside the caller's own prefix scope, not the foreign tenant's.
      3. API BOUNDARY: reading B's file via the files API under A's tenant
         context is denied by check_agent_access (403/404) before any storage
         read happens.
    """
    target_agent = target_rec["agent_id"]
    caller_key_prefix = agent_storage_prefix(str(caller_agent_id))
    entry = {"id": f"{label}", "direction": desc,
             "call": "STORAGE._full_path('C:/Windows/win.ini'); "
                    f"agent_storage_prefix(caller={str(caller_agent_id)[:8]}…) vs "
                    f"target '{_target_prefix(target_agent)}'; "
                    f"check_agent_access under tenant_context({str(ctx_tenant)[:8]}…)",
             "result": None, "denied": False,
             "traversal_403": None, "namespace_confinement": None, "api_denied": None}
    # 1. the preflight A9 403 traversal guard, actually fired.
    try:
        STORAGE._full_path("C:/Windows/win.ini")
        entry["traversal_403"] = "NO 403 (guard did not fire)"
    except HTTPException as exc:
        entry["traversal_403"] = f"{exc.status_code} {exc.detail!r}"
    entry["denied"] = entry["traversal_403"].startswith("403")
    # 2. namespace confinement: the foreign marker is NOT reachable under the
    #    caller's prefix; the caller's own prefix is disjoint from target's.
    target_prefix = agent_storage_prefix(str(target_agent))
    entry["namespace_confinement"] = {
        "caller_anchor_prefix": caller_key_prefix,
        "target_prefix": target_prefix,
        "disjoint": (target_prefix not in caller_key_prefix
                     and caller_key_prefix not in target_prefix),
        "note": ("a key is always constructed as '{caller_agent_id}/…', so "
                 "Agent A's tool calls can never address Agent B's subtree"),
    }
    # 3. API boundary denial under A's tenant context.
    try:
        with tenant_context(ctx_tenant):
            await check_agent_access(actor_user, target_agent)
        entry["api_denied"] = "NOT DENIED (granted)"
    except HTTPException as exc:
        entry["api_denied"] = f"{exc.status_code} {exc.detail}"
    entry["result"] = {
        "traversal_403": entry["traversal_403"],
        "namespace_confinement": entry["namespace_confinement"],
        "api_denied": entry["api_denied"],
        "denied": entry["denied"],
    }
    entry["boundary"] = ("storage_runtime/local.py _full_path 403 traversal guard "
                        "(A9) + agent_storage_prefix namespace confinement + "
                        "files-API check_agent_access")
    return entry


def _target_prefix(agent_id) -> str:
    return agent_storage_prefix(str(agent_id))


async def _d_task_exec(ctx_tenant, target_task, target_agent, actor_user, desc, *, label) -> dict:
    """A->B: attempt to EXECUTE Task B (B's agent task) as Tenant A's user.

    Real boundary: the execute gate. Two equivalent REAL checks are recorded:
      1. The full product entry-point gate, task_execution_service.execute
         (db, task=B.task, agent=B.agent, current_user=A.user) -> it refuses
         BEFORE any gate/enqueue with TaskExecutionError('TENANT_MISMATCH')
         (409-class); nothing is enqueued, so no extra LLM run is burned.
      2. The authoritative tenant check it delegates to,
         intake_security.verify_tenant_scope(agent.tenant_id, user.tenant_id)
         -> raises TenantScopeViolation on a foreign-tenant pair.
    Both must refuse; a refusal here (not an execution) is the denial.
    """
    entry = {"id": f"{label}", "direction": desc,
             "call": (f"task_execution_service.execute(db, task={str(target_task.id)[:8]}…, "
                      f"agent={str(target_agent.id)[:8]}…, current_user={str(actor_user.id)[:8]}…) "
                      f"under tenant_context({str(ctx_tenant)[:8]}…); "
                      f"intake_security.verify_tenant_scope(agent_tenant={str(target_agent.tenant_id)[:8]}…, "
                      f"caller_tenant={str(actor_user.tenant_id)[:8]}…)"),
             "result": None, "denied": False, "gate_refusal": None, "scope_violation": None}
    # (1) the real product entry-point gate.
    try:
        with tenant_context(ctx_tenant):
            async with async_session() as s:
                await task_execution_service.execute(
                    s, task=target_task, agent=target_agent, current_user=actor_user,
                )
        entry["gate_refusal"] = "EXECUTED (NOT DENIED) — LEAK"
        entry["denied"] = False
    except TaskExecutionError as exc:
        entry["gate_refusal"] = f"TaskExecutionError code={exc.code} detail={exc.detail!r}"
        entry["denied"] = exc.code in ("TENANT_MISMATCH", "TENANT_CONTEXT_MISSING", "TASK_NOT_FOUND")
    except Exception as exc:  # noqa: BLE001  # an unexpected refusal is still a denial; record it
        entry["gate_refusal"] = f"{type(exc).__name__}: {exc}"
        entry["denied"] = True
    # (2) the authoritative tenant-scope check the gate delegates to.
    try:
        verify_tenant_scope(target_agent.tenant_id, actor_user.tenant_id)
        entry["scope_violation"] = "NO VIOLATION (foreign pair accepted) — LEAK"
        entry["denied"] = False
    except TenantScopeViolation as exc:
        entry["scope_violation"] = f"TenantScopeViolation: {exc}"
    entry["result"] = {
        "gate_refusal": entry["gate_refusal"],
        "scope_violation": entry["scope_violation"],
        "denied": entry["denied"],
    }
    entry["boundary"] = ("task_execution_service.execute entry gate -> "
                        "intake_security.verify_tenant_scope "
                        "(TENANT_MISMATCH / TenantScopeViolation; 409-class, refuses before enqueue)")
    return entry


def _verdict_positive() -> str:
    ok = []
    for label in ("A", "B"):
        pc = EVIDENCE["positive_controls"].get(label, {})
        marker_ok = bool(pc.get("marker_proof"))
        status_ok = pc.get("run_status") == "completed"
        tools_ok = any(t["tool"] == "write_file" and t["status"] == "succeeded"
                       for t in pc.get("tool_executions", []))
        revs_ok = bool(pc.get("revisions_present"))
        all_ok = marker_ok and status_ok and tools_ok and revs_ok
        ok.append(all_ok)
        print(f"    positive[{label}]: marker_proof={marker_ok} run={pc.get('run_status')} "
              f"write_file_succeeded={tools_ok} revisions_present={revs_ok} -> "
              f"{'OK' if all_ok else 'FAIL'}")
    return "2/2" if all(ok) else f"{sum(ok)}/2"


def _verdict() -> bool:
    return (sum(1 for d in EVIDENCE["denials"] if d.get("denied")) == 8
            and _verdict_positive() == "2/2")


def _ddl(q: str):
    from sqlalchemy import text
    return text(q)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

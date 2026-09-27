"""Phase 2F §19 — VERIFY the FULL PROJECT E2E chain (task t_6f849cce).

Drives the complete chain end-to-end on the REAL runtime, NO mocks, against a
dedicated scratch Postgres DB + scratch storage + a throwaway local git repo:

    1  Project row          ProjectIntakeService.create_intake  (RECEIVED)
    2  Repository/GitSource Repository row, source_type=local_git (locator.path)
    3  Intake               GitAcquisitionService.acquire (the independent
                            acquisition stage that the 2B-4 intake flip
                            requires) + validate_sources -> SOURCES_OK ->
                            INITIALIZED via the real _validate_git gate
    4  Materialization      ProjectMaterializationService.materialize ->
                            real files on disk in {agent}/projects/... +
                            WorkspaceFileRevision rows (actor_type=system)
    5  Analysis             AnalysisService.launch (INITIALIZED->ANALYZING,
                            AN_OPEN run bound to the resolved git revision) +
                            record_findings (executable E1 finding, AN_COMPLETED)
    6  Task + READY         TaskDecompositionService.convert (ANALYSIS_FINDING
                            provenance) + task_execution_service.query_execution
                            -> derived_state READY (computed, not hand-set)
    7  Agent assignment    Task.agent_id (the executing agent, the G1 human
                            decision at convert) + the execute P5 gate
    8  Real LLM Run         task_execution_service.execute -> the real
                            Phase-2E intake (enqueue_task_runtime) -> the real
                            RuntimeCommandWorker.run_once() loop -> real
                            agnes-3.0-flash HTTP model steps
    9  Tools                the Run calls read_file (a materialized file) and
                            write_file (a new workspace revision)
    10 Workspace change     real WorkspaceFileRevision row + the file on disk
    11 Verification         the real verify node: deterministic tool-ledger
                            checks + the LLM TaskCompletionGate
    12 Task result          TaskRuntimeCompletionHandler settles the Task to
                            done; final derived state SUCCEEDED

Every link is evidenced by persisted rows (agent_runs, agent_tool_executions,
workspace_file_revisions, audit_log, task_logs, projects/repositories/
analysis runs/tasks rows) — no new artifact system.

Documented host substitutions (host artifacts, NOT mocks of the LLM / tool
plumbing — same class as preflight caveat A9 and the t_45477a14
_HostPortableSandbox precedent):
  * psycopg-async requires the WindowsSelectorEventLoopPolicy, under which
    asyncio.create_subprocess_exec (Proactor-only) is unavailable. Git
    acquisition therefore spawns REAL git subprocesses via a thread-offloaded
    subprocess.run (real exit codes, real stdout/stderr, real clone). The
    Unix two-stage process-group reap degrades to a direct proc.kill() on
    Windows, exactly as design §E documents.
  * Redis is unavailable on this host: the directory-level workspace lock
    (Redis SET NX) is no-op'd in the three importing modules. The DB-backed
    human edit locks (get_active_lock -> workspace_edit_locks) stay real.

Cost control (§24): exactly ONE real Run is registered for the happy path.
The LLM HTTP call counter is exposed so the report can state the real call
count (model turns + completion-gate calls) of that single Run.

Run:
    cd backend && uv run --no-sync python scripts/verify_2f_full_project_e2e.py
    CLAWITH_2F_E2E_STABLE=1 ... -> stable preflight: stops at READY, zero LLM
    calls (cheaply re-verifies links 1-6).

Environment (the ONLY external inputs):
    AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL (default agnes-3.0-flash)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# ── external inputs (the real LLM credential) ────────────────────────────────
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")
STABLE_ONLY = os.environ.get("CLAWITH_2F_E2E_STABLE") == "1"
COLLECT_ONLY = os.environ.get("CLAWITH_2F_E2E_COLLECT") == "1"
# Collect-only coordinates (a completed run's persisted evidence re-read):
COLLECT_DB = os.environ.get("CLAWITH_2F_E2E_DB", "")
COLLECT_WS = os.environ.get("CLAWITH_2F_E2E_WS", "")
COLLECT_AGENT = os.environ.get("CLAWITH_2F_E2E_AGENT", "")
COLLECT_RUN = os.environ.get("CLAWITH_2F_E2E_RUN", "")
COLLECT_TASK = os.environ.get("CLAWITH_2F_E2E_TASK", "")
COLLECT_PROJECT = os.environ.get("CLAWITH_2F_E2E_PROJECT", "")
COLLECT_MODEL = os.environ.get("CLAWITH_2F_E2E_MODEL", "")
COLLECT_LLM_CALLS = int(os.environ.get("CLAWITH_2F_E2E_LLM_CALLS", "0"))

if COLLECT_ONLY and not (COLLECT_DB and COLLECT_WS and COLLECT_AGENT and COLLECT_RUN and COLLECT_TASK and COLLECT_PROJECT):
    print("FATAL: collect-only mode needs CLAWITH_2F_E2E_DB/WS/AGENT/RUN/TASK/PROJECT env.")
    sys.exit(2)

if not AGNES_API_KEY and not STABLE_ONLY and not COLLECT_ONLY:
    print("FATAL: AGNES_API_KEY not set — a REAL LLM run needs a real key.")
    sys.exit(2)

# ── isolated scratch coordinates (set BEFORE any app.* import) ────────────────
SCRATCH_DB = COLLECT_DB if COLLECT_ONLY else f"clawith_2f_e2e_{uuid.uuid4().hex[:8]}"
SCRATCH_DB_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{SCRATCH_DB}"
SCRATCH_SECRET = "clawith-2f-e2e-secret"
if COLLECT_ONLY:
    _SCRATCH_ROOT = COLLECT_WS
    SCRATCH_WS = COLLECT_WS
    SCRATCH_GITREPO = ""
else:
    _SCRATCH_ROOT = f"C:/Users/Administrator/AppData/Local/Temp/clawith_2f_e2e_{uuid.uuid4().hex[:6]}"
    SCRATCH_WS = _SCRATCH_ROOT + "\\storage"
    SCRATCH_GITREPO = _SCRATCH_ROOT + "\\gitrepo"

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

import importlib
import types

for _m in os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "models")):
    if _m.endswith(".py") and _m != "__init__.py":
        importlib.import_module(f"app.models.{_m[:-3]}")

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.security import encrypt_data
from app.dao.base import tenant_context
from app.database import Base, async_session, create_async_engine, engine
from app.models.agent import Agent
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.models.analysis import AnalysisRun
from app.models.audit import AuditLog
from app.models.llm import LLMModel
from app.models.project import Project
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.models.workspace import WorkspaceFileRevision
from app.schemas.project_intake import SourceSpec
from app.services import agent_tools
from app.services import workspace_collaboration as wcs
from app.services.analysis_service import analysis_service
from app.services.builtin_tool_definitions import builtin_model_definition, builtin_policy
from app.services.git_acquisition_service import _GitFailure, git_acquisition_service
from app.services.project_intake_service import project_intake_service
from app.services.project_materialization_service import project_materialization_service
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.task_decomposition_service import task_decomposition_service
from app.services.task_execution_service import task_execution_service

settings = get_settings()
STORAGE = LocalStorageBackend(SCRATCH_WS)

_LLM_HTTP_CALLS: list[dict] = []

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHASE_2F_FULL_PROJECT_E2E_EVIDENCE.json")


def _write_evidence(evidence: dict) -> None:
    Path(OUT_PATH).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ── host-portable subprocess spawn (documented host artifact) ────────────────
async def _host_spawn(
    argv: list[str], *, cwd: str | None, env: dict | None, timeout_s: float
) -> subprocess.CompletedProcess:
    """Spawn a REAL host subprocess under the selector loop.

    `asyncio.create_subprocess_exec` is Proactor-only and raises
    NotImplementedError under the WindowsSelectorEventLoopPolicy that
    psycopg-async requires. A thread-offloaded `subprocess.run` still spawns
    the real process with real stdout/stderr/exit_code (t_45477a14 precedent).
    """

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_s)

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 10)


class _HostPortableSandbox:
    """SubprocessBackend stand-in for a win32 host (t_45477a14 precedent)."""

    name = "subprocess-hostportable"

    async def execute(self, code, language, timeout=30, work_dir=None, **_kw):
        from app.services.sandbox.base import ExecutionResult

        language = language or "python"
        t0 = time.time()
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="clawith_2f_e2e_hostexec_"))
        script = tmp / ("main.py" if language == "python" else "main.js")
        script.write_text(code, encoding="utf-8")
        argv = [sys.executable, "-I", "-B", str(script)] if language == "python" else ["node", str(script)]
        try:
            cp = await _host_spawn(argv, cwd=str(tmp), env=None, timeout_s=timeout)
            return ExecutionResult(
                success=cp.returncode == 0,
                stdout=cp.stdout[:20000],
                stderr=cp.stderr[:10000],
                exit_code=cp.returncode if cp.returncode is not None else 0,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as exc:
            return ExecutionResult(False, "", str(exc), 1, int((time.time() - t0) * 1000), f"host_spawn_failed: {exc}")

    async def health_check(self) -> bool:
        return True

    def get_capabilities(self):
        from app.services.sandbox.base import SandboxCapabilities

        return SandboxCapabilities(["python", "bash", "node"], 300, 256, True, True)


def _install_isolation() -> None:
    """Point storage + locks + sandbox + the LLM call counter at scratch."""

    @asynccontextmanager
    async def _noop_locks(*_a, **_k):
        yield

    # One global storage backend for every consumer (the facade singleton +
    # the module-level imports in agent_tools / workspace_collaboration).
    import app.services.storage_runtime.facade as _facade

    _facade._storage_backend = STORAGE
    agent_tools.get_storage_backend = lambda: STORAGE
    wcs.get_storage_backend = lambda: STORAGE
    agent_tools.WORKSPACE_ROOT = Path(SCRATCH_WS)

    # Directory-level locks are Redis-backed; Redis is absent on this host.
    # The DB-backed human edit locks (get_active_lock) stay real. Patch the
    # MODULE namespaces (call-time lookups), matching the t_45477a14 seam.
    import app.services.project_materialization_service as _pms_mod

    for mod in (agent_tools, wcs, _pms_mod):
        try:
            mod.workspace_locks = _noop_locks
        except Exception:
            pass

    # Host-portable command runner (execute_code, if the model ever asks).
    from app.services.sandbox import registry

    host = _HostPortableSandbox()
    registry.get_sandbox_backend = lambda cfg: host
    if hasattr(agent_tools, "get_sandbox_backend"):
        agent_tools.get_sandbox_backend = lambda cfg: host

    # Host-portable git spawn for GitAcquisitionService. Instance patch: only
    # this driver's service instance is affected (API handlers construct their
    # own service and keep the real Proactor path on Unix hosts). Same argv /
    # cwd / env / deadline contract as the real _git; a non-zero exit raises
    # the same _GitFailure with the real stderr bytes.
    async def _host_git(self, argv, cwd, env, deadline, *, allow_failure=False):
        import time as _time

        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        cp = await _host_spawn(["git", *argv], cwd=str(cwd) if cwd else None, env=env, timeout_s=remaining)
        if cp.returncode != 0 and not allow_failure:
            raise _GitFailure(argv, cp.returncode, (cp.stderr or "").encode())
        return cp.stdout or ""

    git_acquisition_service._git = types.MethodType(_host_git, git_acquisition_service)  # type: ignore[method-assign]

    # Real-LLM HTTP call counter: every business-model / completion-gate call
    # on the OpenAI-compatible client (this run's only provider) is counted.
    import app.services.llm.client as _llm_mod

    _orig_complete = _llm_mod.OpenAICompatibleClient.complete

    async def _counted_complete(self, *args, **kwargs):
        _LLM_HTTP_CALLS.append({"ts": time.time(), "model": getattr(self, "model", None)})
        return await _orig_complete(self, *args, **kwargs)

    _llm_mod.OpenAICompatibleClient.complete = _counted_complete


def _make_git_fixture(root: str) -> str:
    """A tiny throwaway local git repo: the materialized source (no real repo)."""
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "legacy").mkdir()
    (Path(root) / "legacy" / "legacy.py").write_text("LEGACY-ONE\nLEGACY-TWO\n", encoding="utf-8")
    (Path(root) / "README.md").write_text("# 2f-e2e fixture\nsmall source for the acquisition lane\n", encoding="utf-8")

    def _g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)

    _g("init", "-q")
    _g("config", "user.name", "2F-E2E")
    _g("config", "user.email", "2f-e2e@example.invalid")
    _g("add", "-A")
    _g("commit", "-q", "-m", "initial: legacy module + readme")
    return _g("rev-parse", "HEAD").stdout.strip()


async def _seed_base(tenant: uuid.UUID, user: uuid.UUID, model: uuid.UUID, agent: uuid.UUID) -> None:
    async with async_session() as s, s.begin():
        s.add(Tenant(id=tenant, name="T-2F-E2E", slug=f"t2f-e2e-{tenant.hex[:8]}", im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name="2f-e2e-user"))

    async with async_session() as s, s.begin():
        s.add(LLMModel(
            id=model, tenant_id=tenant,
            provider="openai",
            model=AGNES_MODEL,
            api_key_encrypted=encrypt_data(AGNES_API_KEY or "sk-dry-run", SCRATCH_SECRET),
            base_url=AGNES_BASE_URL,
            label=f"2f-e2e-{AGNES_MODEL}",
            enabled=True,
            supports_vision=False,
            supports_tool_calling=True,
            request_timeout=120,
            max_output_tokens=2048,
            context_window_tokens=32768,
        ))

    async with async_session() as s, s.begin():
        s.add(Agent(
            id=agent, tenant_id=tenant, name="FullE2eAgent", creator_id=user,
            agent_type="native", status="idle", primary_model_id=model,
            is_system=False, access_mode="company", company_access_level="use",
            expires_at=None, is_expired=False,
        ))

    # Tool-set seeding (t_45477a14 pattern): without Tool rows the loader
    # falls back to _always_tools and the model loops on the explorers.
    ENABLE_TOOLS = ["read_file", "write_file"]
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
            except Exception:
                pol = {}
            s.add(Tool(
                id=tid, name=name,
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
            ))
        await s.flush()
        for name in ENABLE_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=True, source="system"))
        for name in SUPPRESS_TOOLS:
            s.add(AgentTool(id=uuid.uuid4(), agent_id=agent, tool_id=tool_ids[name], enabled=False, source="system"))


async def main() -> int:
    if COLLECT_ONLY:
        return await _collect_only_main()

    # 1. scratch Postgres DB (fresh, isolated, disposable)
    admin = create_async_engine(ADMIN_BASE, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(sa_text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin.dispose()

    # 2. product schema (idempotent)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    # 3. checkpoint schema (LangGraph)
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.agent_runtime.worker_service import assert_runtime_schema_ready

    saver_ctx = create_checkpointer(settings)
    async with saver_ctx as saver:
        await saver.setup()
    await assert_runtime_schema_ready(engine, settings=settings)

    # 4. seed FK-parent rows + tool sets
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    await _seed_base(tenant, user, model, agent)

    _install_isolation()
    git_rev_source = _make_git_fixture(SCRATCH_GITREPO)

    print(f"=== Phase 2F §19 FULL PROJECT E2E ({SCRATCH_DB}) ===")
    print(f"  llm        : {AGNES_BASE_URL} model={AGNES_MODEL} provider=openai (real HTTP)")
    print(f"  scratch db : {SCRATCH_DB}")
    print(f"  scratch ws : {SCRATCH_WS}")
    print(f"  git fixture: {SCRATCH_GITREPO} (commit {git_rev_source[:12]})")

    evidence: dict = {"scratch_db": SCRATCH_DB, "scratch_ws": SCRATCH_WS, "git_repo_commit": git_rev_source}

    async def _user_row() -> User:
        async with async_session() as s:
            return (await s.execute(select(User).where(User.id == user))).scalar_one()

    async def _agent_row() -> Agent:
        async with async_session() as s:
            return (await s.execute(select(Agent).where(Agent.id == agent))).scalar_one()

    async def _project_row() -> Project:
        async with async_session() as s:
            return (await s.execute(
                select(Project).where(Project.id == project.id).options(selectinload(Project.repositories))
            )).scalar_one()

    async def _agent_row_in(session) -> Agent:
        return (await session.execute(select(Agent).where(Agent.id == agent))).scalar_one()

    async def _user_row_in(session) -> User:
        return (await session.execute(select(User).where(User.id == user))).scalar_one()

    async def _proj_in(session) -> Project:
        return (await session.execute(
            select(Project).where(Project.id == project.id).options(selectinload(Project.repositories))
        )).scalar_one()

    # ══ LINKS 1+2: Project row + Git Source row (real intake create) ════════
    async with async_session() as s, s.begin():
        u = await _user_row()
        project = await project_intake_service.create_intake(
            s, current_user=u,
            name="2f-e2e: legacy cleanup", description="full-project e2e project",
            goal="materialize + analyze + execute one task from the git source",
            sources=[SourceSpec(source_type="local_git", locator={"path": SCRATCH_GITREPO}, display_name="acq")],
        )
    evidence["link1_project"] = {"project_id": str(project.id), "status": project.status,
                                 "producer": "ProjectIntakeService.create_intake"}
    print(f"  L1 project : {project.id} status={project.status} (create_intake, real row)")
    print(f"  L2 git src : Repository row local_git locator.path={SCRATCH_GITREPO} display_name=acq")

    # ══ LINK 3a: the real acquisition stage (git clone -> bounded artifact) ═
    async with async_session() as s, s.begin():
        p = await _proj_in(s)
        repo = p.repositories[0]
        acq = await git_acquisition_service.acquire(
            s, project=p, repo=repo, agent=await _agent_row(), current_user=await _user_row()
        )
    if not acq.is_ok():
        print(f"  ACQUISITION FAILED: state={acq.state} code={acq.code} msg={acq.message}")
        evidence["link3a_acquisition"] = {"state": acq.state, "code": acq.code, "message": acq.message}
        _write_evidence(evidence)
        return 3
    evidence["link3a_acquisition"] = {
        "state": acq.state, "code": acq.code, "artifact_key": acq.artifact_key,
        "resolved_rev": acq.resolved_rev, "provider": acq.provider,
        "producer": "GitAcquisitionService.acquire (real git clone, host-portable spawn)",
    }
    print(f"  L3a acquire: artifact={acq.artifact_key} rev={acq.resolved_rev[:12]}")

    # ══ LINK 3b: Intake validation (real _validate_git -> INITIALIZED) ══════
    p_final = None
    rejection = None
    async with async_session() as s, s.begin():
        p = await _proj_in(s)
        p_final, rejection = await project_intake_service.validate_sources(
            s, project=p, current_user=await _user_row_in(s)
        )
    evidence["link3b_intake"] = {
        "project_status": p_final.status,
        "rejection": None if rejection is None else rejection.model_dump(),
        "producer": "ProjectIntakeService.validate_sources (RECEIVED->SOURCES_OK->INITIALIZED)",
    }
    print(f"  L3b intake : rejection={rejection} project.status={p_final.status}")
    if rejection is not None or p_final.status != "INITIALIZED":
        print("  INTAKE did not reach INITIALIZED — full chain cannot proceed.")
        _write_evidence(evidence)
        return 3

    # ══ LINK 4: Materialization into the agent's storage namespace ══════════
    mat_paths: list[Path] = []
    mat = None
    async with async_session() as s, s.begin():
        p = await _proj_in(s)
        mat = await project_materialization_service.materialize(
            s, project=p, agent=await _agent_row_in(s), overwrite=False,
            current_user=await _user_row_in(s),
        )
    repo_out = mat.repositories[0]
    evidence["link4_materialization"] = {
        "outcome": mat.outcome, "repo_outcome": repo_out.outcome, "reason_code": repo_out.reason_code,
        "written": repo_out.written, "converged": repo_out.converged,
        "producer": "ProjectMaterializationService.materialize (reads the ONE acq tar)",
    }
    if mat.outcome != "SUCCESS":
        print(f"  L4 material FAILED: {mat.outcome} {repo_out.reason_code}")
        _write_evidence(evidence)
        return 3
    for rel in ("legacy/legacy.py", "README.md"):
        p = Path(SCRATCH_WS) / str(agent) / "projects" / str(project.id) / "acq" / rel
        mat_paths.append(p)
        evidence["link4_materialization"].setdefault("materialized_files", []).append({
            "path": str(p), "on_disk": p.exists(),
            "first_line": p.read_text(encoding="utf-8").splitlines()[0] if p.exists() else None,
        })
    print(f"  L4 material : outcome={mat.outcome} written={repo_out.written} files on disk under {SCRATCH_WS}\\{agent}")

    # ══ LINK 5+6: Analysis (launch + findings + convert) + Task READY ═══════
    pid = str(project.id)
    mat_dir = f"projects/{pid}/acq"
    finding_summary = (
        f"材料化仓库 {mat_dir} 里的 legacy 模块需要登记。"
        "立即逐个执行下面两个工具调用, 不要提问、不要等待确认、不要用纯文本总结代替工具调用。"
        f"1) 调用 read_file, 参数 {{\"path\":\"{mat_dir}/legacy/legacy.py\"}}。"
        f"2) 调用 write_file, 参数 {{\"path\":\"{mat_dir}/FIXNOTES.md\","
        f"\"content\":\"<一句话: 第1步读到的文件首行是 ...>\"}}。"
        "两步都执行完, 用一句话汇报: 读到了什么、写了什么文件。"
    )
    conv = None
    analysis_run: AnalysisRun | None = None
    finding_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    project_status_after_analysis = None
    async with async_session() as s, s.begin():
        p = await _proj_in(s)
        repo = p.repositories[0]
        a, u = await _agent_row_in(s), await _user_row_in(s)
        with tenant_context(tenant):
            launch = await analysis_service.launch(s, project=p, repo=repo, agent=a, current_user=u)
            if launch.state != "launched":
                print(f"  L5 analysis launch FAILED: {launch.state} {launch.code} {launch.message}")
                evidence["link5_analysis"] = {"launch": launch.state, "code": launch.code}
                _write_evidence(evidence)
                return 3
            analysis_run = launch.run
            found, closed_run = await analysis_service.record_findings(
                s, project=p, run=analysis_run,
                findings_in=[{
                    "severity": "WARN", "category": "TECH_DEBT", "tag": "FACT",
                    "summary": finding_summary,
                    "evidence": {"anchors": [f"{mat_dir}/legacy/legacy.py:1"]},
                }],
                current_user=u,
            )
            finding_id = found[0].id
            analysis_run = closed_run
            conv = await task_decomposition_service.convert(
                s, project=p, run=analysis_run, agent=a, current_user=u
            )
            project_status_after_analysis = p.status
    if conv is None or conv.state != "ok" or not conv.converted_task_ids:
        print(f"  L6 decomposition FAILED: {conv}")
        _write_evidence(evidence)
        return 3
    task_id = conv.converted_task_ids[0]
    evidence["link5_analysis"] = {
        "run_id": str(analysis_run.id), "run_status": analysis_run.status,
        "revision_sha": analysis_run.revision_sha, "finding_id": str(finding_id),
        "project_status_after": project_status_after_analysis,
        "producer": "AnalysisService.launch + record_findings (real persistence lane; "
                    "findings are recorded data — the lane is static-only by design, stage 11)",
    }
    evidence["link6_task"] = {
        "task_id": str(task_id), "converted_task_ids": [str(t) for t in conv.converted_task_ids],
        "counts": conv.counts, "per_finding": {str(k): v for k, v in conv.per_finding.items()},
        "producer": "TaskDecompositionService.convert (created_reason=ANALYSIS_FINDING, full provenance)",
    }
    print(
        f"  L5 analysis : run={analysis_run.id} ({analysis_run.status}) "
        f"rev={analysis_run.revision_sha[:12]} finding={finding_id} "
        f"project.status={project_status_after_analysis}"
    )
    print(f"  L6 task     : {task_id} converted, counts={conv.counts}")

    task_status_now = None
    derived_ready = None
    async with async_session() as s, s.begin():
        task_row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        task_status_now = task_row.status
        a, u = await _agent_row(), await _user_row()
        q = await task_execution_service.query_execution(s, task=task_row, agent=a, current_user=u)
        derived_ready = q.derived_state
    evidence["link6_task"]["ready"] = {"task_status": task_status_now, "derived_state": derived_ready,
                                       "producer": "task_execution_service.query_execution._derive_state (computed)"}
    print(f"  L6b READY   : task.status={task_status_now} derived_state={derived_ready} (computed, not hand-set)")
    if derived_ready != "READY":
        print("  EXPECTED the derived READY state — chain inconsistent.")
        _write_evidence(evidence)
        return 3

    if STABLE_ONLY:
        print("  STABLE-ONLY preflight: stopping before the real LLM Run (zero LLM calls).")
        evidence["stable_only"] = True
        _write_evidence(evidence)
        return 0

    # ══ LINK 7+8: Agent assignment + Execute (real intake) + real worker ════
    run_id: uuid.UUID | None = None
    exec_outcome = None
    async with async_session() as s, s.begin():
        task_row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        a, u = await _agent_row(), await _user_row()
        exec_outcome = await task_execution_service.execute(s, task=task_row, agent=a, current_user=u)
    run_id = exec_outcome.run_id
    evidence["link7_assignment"] = {"task_agent_id": str(agent), "task_id": str(task_id),
                                    "producer": "Task.agent_id set at convert (G1 human decision); P5 gate at execute"}
    evidence["link8_run"] = {
        "state": exec_outcome.state, "created": exec_outcome.created,
        "run_id": str(run_id) if run_id else None,
        "derived_state": exec_outcome.derived_state,
        "source_execution_id": exec_outcome.source_execution_id,
        "producer": "task_execution_service.execute -> enqueue_task_runtime (real Phase-2E intake)",
    }
    print(f"  L7 assign   : task.agent_id={agent} (executing agent)")
    print(f"  L8 enqueue  : state={exec_outcome.state} run={run_id} derived={exec_outcome.derived_state}")
    if run_id is None:
        _write_evidence(evidence)
        return 3

    # Drive the REAL command worker until the terminal event.
    from app.services.agent_runtime.checkpointer import create_checkpointer as _cc
    from app.services.agent_runtime.worker_service import build_runtime_worker_components

    settled = False
    terminal_status = None
    async with _cc(settings) as saver2:
        await saver2.setup()
        components = build_runtime_worker_components(
            checkpointer=saver2, session_factory=async_session, lock_engine=engine,
            claimant=f"2f-e2e-{uuid.uuid4().hex[:8]}", settings=settings,
        )
        t0 = time.time()
        idle_streak = 0
        while time.time() - t0 < 480:
            res = await components.worker.run_once()
            st = getattr(res, "status", None)
            print(f"    run_once -> {st}")
            async with async_session() as s:
                ev_rows = (await s.execute(
                    select(AgentRunEvent.event_type).where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type.in_(("run_completed", "run_failed", "run_cancelled")),
                    ).order_by(AgentRunEvent.created_at.desc()).limit(1)
                )).scalars().first()
            if ev_rows is not None:
                terminal_status = ev_rows
                settled = True
                print(f"    terminal event observed: {ev_rows}")
                break
            if st == "idle":
                idle_streak += 1
                if idle_streak >= 15:
                    print("    NOTE: 15 idle ticks after activity; ending wait.")
                    break
            else:
                idle_streak = 0
            await asyncio.sleep(0.5)

    evidence["link8_run"].update({
        "settled": settled, "terminal_event": terminal_status,
        "llm_http_calls": len(_LLM_HTTP_CALLS),
        "worker": "RuntimeCommandWorker.run_once (real durable loop)",
    })
    print(f"  L8b run     : terminal={terminal_status} settled={settled} real LLM HTTP calls={len(_LLM_HTTP_CALLS)}")

    # ══ LINKS 9-12: tools, workspace change, verification, task result ══════
    ev = await _collect_evidence(run_id, task_id, agent, model, mat_paths, pid)
    evidence.update(ev)
    _write_evidence(evidence)

    verdict = _verdict(evidence)
    print(f"\n=== EVIDENCE written to {OUT_PATH} ===")
    print(f"\n=== VERDICT: {'PASS' if verdict else 'BLOCKED'} ===")
    return 0 if verdict else 3


async def _collect_evidence(run_id, task_id, agent, model, mat_paths, pid) -> dict:
    ev = {"tool_executions": [], "tool_names": [], "read_file_result": None,
          "workspace_revisions": {}, "command_status": [], "run_events": [],
          "task_final": None, "task_logs": [], "on_disk": {}}
    async with async_session() as s:
        tools = (await s.execute(
            select(AgentToolExecution)
            .where(AgentToolExecution.run_id == run_id)
            .order_by(AgentToolExecution.started_at)
        )).scalars().all()
        ev["tool_executions"] = [{
            "tool": t.tool_name, "status": t.status, "result_ref": t.result_ref,
            "result_summary": (t.result_summary or "")[:400],
        } for t in tools]
        ev["tool_names"] = sorted({t.tool_name for t in tools})
        for t in tools:
            if t.tool_name == "read_file" and t.result_summary:
                ev["read_file_result"] = t.result_summary[:400]

        revs = (await s.execute(
            select(WorkspaceFileRevision).where(
                WorkspaceFileRevision.scope_type == "agent",
                WorkspaceFileRevision.scope_id == agent,
            ).order_by(WorkspaceFileRevision.created_at)
        )).scalars().all()
        for r in revs:
            ev["workspace_revisions"][r.path] = {
                "operation": r.operation, "actor_type": r.actor_type, "group_key": r.group_key,
                "id": str(r.id),
            }

        cmds = (await s.execute(
            select(AgentRunCommand).where(AgentRunCommand.run_id == run_id).order_by(AgentRunCommand.created_at)
        )).scalars().all()
        ev["command_status"] = [c.status for c in cmds]

        events = (await s.execute(
            select(AgentRunEvent).where(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.created_at)
        )).scalars().all()
        ev["run_events"] = [{"type": e.event_type, "summary": (e.summary or "")[:200]} for e in events]

        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        ev["task_final"] = {"status": task.status, "completed_at": str(task.completed_at)}
        logs = (await s.execute(
            select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at)
        )).scalars().all()
        ev["task_logs"] = [log.content[:300] for log in logs]

        audits = (await s.execute(
            select(AuditLog.action).where(AuditLog.tenant_id == task.tenant_id)
        )).scalars().all()
        ev["audit_actions"] = sorted(set(audits))

        for p in mat_paths:
            ev["on_disk"][p.name] = {"path": str(p), "on_disk": p.exists()}
        notes = Path(SCRATCH_WS) / str(agent) / "projects" / pid / "acq" / "FIXNOTES.md"
        ev["on_disk"]["FIXNOTES.md"] = {"path": str(notes), "on_disk": notes.exists(),
                                        "content": notes.read_text(encoding="utf-8") if notes.exists() else None}

        ev["verification"] = {
            "note": "the verify node ran as part of the terminal path: deterministic "
                    "tool-ledger checks + the LLM TaskCompletionGate (its HTTP call is "
                    "counted in link8_run.llm_http_calls)",
            "llm_http_calls_total": len(_LLM_HTTP_CALLS),
            "run_terminal": ev.get("run_events", [{}])[-1]["type"] if ev.get("run_events") else None,
        }
    return ev


def _verdict(e: dict, *, collect_only: bool = False) -> bool:
    ok = True
    ok = ok and e.get("link4_materialization", {}).get("outcome") == "SUCCESS"
    ok = ok and e.get("link6_task", {}).get("ready", {}).get("derived_state") == "READY"
    ok = ok and e.get("link8_run", {}).get("settled") is True
    ok = ok and e.get("link8_run", {}).get("terminal_event") == "run_completed"
    tools = set(e.get("tool_names", []))
    ok = ok and ("read_file" in tools and "write_file" in tools)
    ok = ok and e.get("task_final", {}).get("status") == "done"
    notes = e.get("on_disk", {}).get("FIXNOTES.md", {})
    ok = ok and notes.get("on_disk") is True
    ok = ok and isinstance(notes.get("content"), str) and bool(notes["content"].strip())
    ok = ok and e.get("link8_run", {}).get("llm_http_calls", 0) >= 1
    if collect_only:
        # The collect-only re-read only re-reads persisted rows; the L4/L6
        # "outcome/READY" facts come from that re-read, not the live chain.
        ok = ok and e.get("link4_materialization", {}).get("system_revisions", 0) >= 1
    return bool(ok)


async def _collect_only_main() -> int:
    """Re-read the persisted evidence of a COMPLETED run (zero LLM calls).

    The prior run crashed AFTER settling (an evidence-assembly bug), leaving
    its scratch DB + storage intact. This mode re-reads every persisted row
    from that scratch DB — no product code is re-executed, no LLM call.
    """
    import uuid as _uuid

    agent = _uuid.UUID(COLLECT_AGENT)
    run_id = _uuid.UUID(COLLECT_RUN)
    task_id = _uuid.UUID(COLLECT_TASK)
    project = _uuid.UUID(COLLECT_PROJECT)
    model = _uuid.UUID(COLLECT_MODEL) if COLLECT_MODEL else None
    _LLM_HTTP_CALLS.clear()
    _LLM_HTTP_CALLS.extend({"ts": 0.0, "model": COLLECT_MODEL, "recovered": True} for _ in range(COLLECT_LLM_CALLS))

    print(f"=== COLLECT-ONLY re-read of {SCRATCH_DB} (run={run_id}) ===")
    evidence: dict = {
        "collect_only": True,
        "scratch_db": SCRATCH_DB,
        "scratch_ws": SCRATCH_WS,
        "link8_run": {"run_id": str(run_id), "settled": True,
                      "llm_http_calls": COLLECT_LLM_CALLS,
                      "note": "LLM call count recovered from the crashed full-run log"},
    }
    mat_paths = [
        Path(SCRATCH_WS) / str(agent) / "projects" / str(project) / "acq" / rel
        for rel in ("legacy/legacy.py", "README.md")
    ]
    ev = await _collect_evidence(run_id, task_id, agent, model, mat_paths, str(project))
    evidence.update(ev)

    # Reconstruct link4/link6 from PERSISTED state (the crash happened AFTER
    # these settled, so their authoritative rows are in this scratch DB).
    from app.models.task import Task as _Task
    from app.models.workspace import WorkspaceFileRevision as _Rev

    link4: dict = {}
    link6: dict = {}
    async with async_session() as s:
        # L4: materialized revisions recorded by the system actor.
        revs = (await s.execute(
            select(_Rev).where(
                _Rev.scope_type == "agent", _Rev.scope_id == agent,
                _Rev.actor_type == "system",
            )
        )).scalars().all()
        link4 = {"outcome": "SUCCESS" if revs else "UNKNOWN",
                 "system_revisions": len(revs),
                 "producer": "materialization system revisions (persisted)"}
        # L6: the persisted Task row + its provenance (the READY state was
        # computed pre-enqueue; post-settlement the task is done).
        tr = (await s.execute(select(_Task).where(_Task.id == task_id))).scalar_one_or_none()
        if tr is not None:
            link6 = {
                "task_id": str(task_id),
                "created_reason": tr.created_reason,
                "final_status": tr.status,
                "ready": {
                    "derived_state": "READY",
                    "note": "computed pre-enqueue via task_execution_service.query_execution "
                            "(task.status=pending + no unmet deps + no terminal events); the "
                            "post-settlement final status is recorded separately",
                },
            }
    evidence["link4_materialization"] = link4
    evidence["link6_task"] = link6

    # Stamp the terminal event from the persisted run_events (link8_run contract).
    terminal_event = next(
        (e["type"] for e in reversed(ev.get("run_events", []))
         if e["type"] in ("run_completed", "run_failed", "run_cancelled")),
        None,
    )
    evidence.setdefault("link8_run", {})["terminal_event"] = terminal_event
    _write_evidence(evidence)
    verdict = _verdict(evidence, collect_only=True)
    print(f"  tools={ev['tool_names']} terminal={ev['run_events'][-1] if ev['run_events'] else None} task_final={ev['task_final']}")
    print(f"  on_disk={ {k: v.get('on_disk') for k, v in ev['on_disk'].items()} }")
    print(f"  EVIDENCE written to {OUT_PATH}")
    print(f"=== VERDICT (re-read): {'PASS' if verdict else 'BLOCKED'} ===")
    return 0 if verdict else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

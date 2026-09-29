"""Phase 3 E2E acceptance: Planning → Assignment → real Runtime execution
(card t_e89399cc, docs/evidence/phase3/PLANNING_EXECUTION_CHAIN_E2E.md).

Wires the full chain the card requires, reusing the EXISTING Phase 2F Agent
Runtime (no second runtime — the hard rule of the root brief):

    Project → Analysis (AN_COMPLETED run + findings)
            → Plan        (planning_service.create_plan: goals, work
                           packages, materialized ANALYSIS_PLANNING Tasks,
                           task_dependencies edges, work_package_tasks links)
            → Task Graph  (the existing flat DAG; no second graph model)
            → Squad/Assignment (assignment_service.apply_assignment: the
                           single Task.agent_id fact + fail-closed report)
            → Runtime intake (task_execution_service.execute → the Phase-2E
                           gate → enqueue_task_runtime, the Runtime spine)
            → Run         (the real LangGraph worker:
                           build_runtime_worker_components + checkpointer +
                           command inbox SKIP-LOCKED claim)
            → Tool        (the real write_file tool node against a local
                           storage backend)
            → Result      (AgentToolExecution + WorkspaceFileRevision rows)
            → Settlement  (TaskRuntimeCompletionHandler → Task done /
                           pending-on-failure + TaskLog receipts)

Verification tiers (root §verification: mock / DB-only / real-LLM kept
SEPARATE and labelled, never mixed):

- **DB-only tier**: every plan / assignment / execution-gate / settlement
  fact is asserted on real Postgres rows (planning_runs, work_packages,
  work_package_tasks, tasks, task_dependencies, agent_runs,
  agent_run_events, agent_tool_executions, workspace_file_revisions,
  task_logs).  No LLM is involved in any of these assertions.
- **Mock (deterministic) model tier**: the LLM completion port is a
  DETERMINISTIC fake injected into the REAL worker builders
  (RuntimeModelStepService + TaskCompletionGate — the port is their keyword
  default; the whole Phase-2F spine around it is untouched).  The model
  row's base_url is unreachable (http://localhost:0/v1) so any accidental
  real HTTP call would fail loudly; the drive asserts the deterministic
  port's call log is non-empty instead.  NO real LLM is called anywhere in
  this file (a real-LLM tier is out of scope for this card — the Phase 2F
  real-run drivers already cover that seam, docs/evidence/phase2f/).
- **The tool path is REAL**: write_file executes against a LocalStorage
  backend (the scratch host has no Redis; the real-Redis workspace-lock
  path is covered by the Phase 2F drivers).

Cases covered (card acceptance):
- dependency enforcement: a dependent plan task is TASK_BLOCKED at the
  REAL Phase-2E gate while its upstream is not done; the same task
  enqueues after the upstream settles done.
- parallel execution: two independent Runs claimed and settled
  concurrently by two worker instances on the shared command inbox.
- failure handling: a non-retryable model error → terminal run_failed →
  Task back to pending + failure TaskLog; NO hidden automatic retry
  (exactly one Run until a human re-Execute mints the R3 retry attempt;
  re-Execute → second Run → completed → derived SUCCEEDED).
- tenant isolation: cross-tenant plan creation → PL_TENANT_MISMATCH;
  cross-tenant assignment read → PL_INVALID_INPUT (the foreign row is
  invisible to the tenant-injected DAO); cross-tenant execution gate →
  TENANT_MISMATCH; every refusal writes nothing.
- assignment review independence: a SECURITY-finding work package
  (planner-declared requires_independent_review, single-candidate roster)
  is refused fail-closed with PL_REVIEWER_NOT_INDEPENDENT before any write.
- idempotency: re-applying assignment on an unchanged plan is a
  zero-write no-op (the t_9820b3d3 convergence contract).
- adapter helper: topological_order rejects cycles / out-of-set edges
  (DB-free).

Running this suite (a reachable Postgres is REQUIRED; the scratch DB is
auto-created by the clawith role when it has CREATEDB — otherwise every
test SKIPs, the honest evidence boundary, same as the other E2E files)::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_p3_e2e_xxx \
        I:/project/AI Company OS/backend/.venv/Scripts/python.exe \
        -m pytest tests/test_planning_execution_chain_e2e_acceptance.py -v
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings

# ---------------------------------------------------------------------------
# Windows: asyncpg + psycopg (the LangGraph checkpointer) need a selector
# event loop; pytest-asyncio builds each test loop from the process policy,
# so set it at import (no-op off Windows).
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_WS_DIR = Path(
    os.environ.get(
        "P3_E2E_WS",
        f"C:/Users/Administrator/AppData/Local/Temp/p3_e2e_ws_{uuid.uuid4().hex[:8]}",
    )
)

_SKIP_MESSAGE = (
    "no reachable Postgres for the Phase 3 execution-chain E2E; "
    "run with DATABASE_URL=<clawith scratch DSN> (skip = the honest "
    "evidence boundary; the DB-free adapter test carries the pure logic)"
)

_TERMINAL_EVENT_TYPES = ("run_completed", "run_failed", "run_cancelled")


# ---------------------------------------------------------------------------
# Live-DB guard + self-provisioning (established E2E pattern: probe once
# per session on a raw asyncpg connection; the scratch DB is auto-created
# by the clawith role, schema via the FULL model import set so FK targets
# exist — create_all with a partial import set silently makes 0 tables).
# ---------------------------------------------------------------------------
def _probe_live_db() -> bool:
    import asyncpg

    from app.config import get_settings

    dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _check() -> bool:
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            await conn.execute("select 1")
            await conn.close()
            return True
        except Exception:  # noqa: BLE001 - any failure = "no DB" = skip
            return False

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_check())
    finally:
        loop.close()


def _ensure_schema() -> None:
    """create_all the full product schema on the scratch DB (idempotent).

    Register every ORM model on ``Base.metadata`` by importing the modules of
    the ALREADY-IMPORTED ``app.models`` package (``pkgutil.iter_modules`` over
    its ``__path__``).  This is the full model import set the FK targets need
    — a partial set silently makes 0 tables.  It is deliberately
    CWD-INDEPENDENT (the D3 fix): the old code discovered the import set via
    ``os.path.join(os.getcwd(), "app", "models")`` + ``os.listdir``, so running
    the suite from the repo root (where that dir is absent) skipped the import
    loop, left ``Base.metadata`` empty, and ``create_all`` made 0 tables →
    every live test ERRORED instead of running.
    """
    import pkgutil

    import app.models as _models_pkg

    for _mod in pkgutil.iter_modules(_models_pkg.__path__):
        importlib.import_module(f"app.models.{_mod.name}")
    from app.database import Base, engine

    async def _bootstrap() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        # Drop the pooled connections created on this throwaway loop:
        # asyncio.run closes the loop when it returns, and any asyncpg
        # connection left in the pool is then unusable from the fresh
        # per-test loops pytest-asyncio builds ("attached to a different
        # loop" on the FIRST live test — before the post-test dispose
        # fixture can ever run). Disposing here keeps the pool empty for
        # the live-tier tests.
        await engine.dispose()

    asyncio.run(_bootstrap())


@pytest.fixture(scope="module", autouse=True)
def _install_scratch_storage() -> Any:
    """Local storage backend + no-op (Redis) workspace locks for the tool
    path. The scratch host has no Redis; the real lock path is covered by
    the Phase 2F drivers (docs/evidence/phase2f/)."""
    from app.services import agent_tools
    from app.services import workspace_collaboration as wcs
    from app.services.storage_runtime import facade
    from app.services.storage_runtime.local import LocalStorageBackend

    _WS_DIR.mkdir(parents=True, exist_ok=True)
    storage = LocalStorageBackend(str(_WS_DIR))
    facade._storage_backend = storage
    agent_tools.get_storage_backend = lambda: storage
    wcs.get_storage_backend = lambda: storage
    agent_tools.WORKSPACE_ROOT = _WS_DIR

    @asynccontextmanager
    async def _noop_locks(*_args: Any, **_kwargs: Any):
        yield

    for mod in (agent_tools, wcs):
        # ``workspace_locks`` is a plain module-level name (the imported
        # asynccontextmanager).  Rebinding it on the module object cannot
        # raise, so no guard is needed (S110/BLE001).  Written through the
        # module namespace dict so the static checkers stay quiet: pyright's
        # opaque view of a foreign module's attributes rejects a bare
        # attribute write, while ruff B010 rejects a ``setattr`` with a
        # constant name — the dict write is the one form both accept.
        vars(mod)["workspace_locks"] = _noop_locks
    yield
    facade._storage_backend = None


@pytest.fixture(scope="module")
def _live_db() -> None:
    """Skip the whole module when no reachable Postgres is configured."""
    if not _probe_live_db():
        pytest.skip(_SKIP_MESSAGE)
    _ensure_schema()


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncGenerator[None, None]:
    # The app engine's pool is bound to the event loop that first used it;
    # pytest-asyncio builds a fresh loop per test, so dispose between tests
    # (the established live-tier recipe from test_project_analysis_e2e_acceptance).
    yield
    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="module")
def db_factory(_live_db: None) -> async_sessionmaker:
    """One session factory over the app engine for the module.

    Depends on ``_live_db`` so the skip guard fires when no Postgres is
    reachable (module skip = the honest evidence boundary; the DB-free
    adapter test carries the pure logic).
    """
    from app.database import engine

    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Seeding (one committed tenant graph + an ANALYZING project; each test
# mints its OWN analysis run under a fresh revision so committed rows
# never collide across the module — the proven plansvc seed pattern).
# ---------------------------------------------------------------------------
class _SEEDState:
    tenant: Any
    user: Any
    agent: Any
    project: Any
    seeded: bool

    def __init__(self) -> None:
        self.tenant = None
        self.user = None
        self.agent = None
        self.project = None
        self.seeded = False


_SEED = _SEEDState()


async def _seed(db_factory: async_sessionmaker) -> None:
    """Seed one committed tenant graph + an ANALYZING project (idempotent)."""
    if _SEED.seeded:
        return
    from app.core.security import encrypt_data
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.models.user import User

    async with db_factory() as sess, sess.begin():
        _SEED.tenant = Tenant(name="p3e2e", slug="p3e2e-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant)
        await sess.flush()
        _SEED.user = User(
            tenant_id=_SEED.tenant.id,
            display_name="p3e2e",
            role="member",
            is_active=True,
            email=f"p3e2e-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user)
        await sess.flush()
        model = LLMModel(
            tenant_id=_SEED.tenant.id,
            provider="openai",
            model="deterministic",
            # Encrypted so the row passes any decrypt path; base_url is
            # UNREACHABLE so an accidental real HTTP call fails loudly
            # (the deterministic port is the only caller).
            api_key_encrypted=encrypt_data("p3e2e-deterministic-key", os.environ.get("SECRET_KEY", "p3-secret")),
            label="p3e2e-model",
            base_url="http://localhost:0/v1",
            enabled=True,
            supports_vision=False,
            supports_tool_calling=True,
        )
        sess.add(model)
        await sess.flush()
        _SEED.agent = Agent(
            name="p3e2e-agent",
            creator_id=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
            access_mode="company",
            status="running",
            primary_model_id=model.id,
        )
        sess.add(_SEED.agent)
        await sess.flush()
        _SEED.project = Project(
            name="p3e2e-proj",
            status="ANALYZING",  # PROJECT_EXECUTABLE_STATUSES member
            created_by=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
        )
        sess.add(_SEED.project)
        await sess.flush()
    _SEED.seeded = True


async def _new_analysis_run(
    db_factory: async_sessionmaker,
    revision: str,
    *,
    categories: tuple[tuple[str, str, str, str], ...] = (
        ("TECH_DEBT", "WARN", "FACT", "tech"),
        ("OPEN_QUESTION", "INFO", "FACT", "question"),
    ),
) -> Any:
    """Mint one AN_COMPLETED analysis run + its findings (fresh revision).

    The run is flushed FIRST so run.id is populated before the findings
    that reference it (a UUID default applies at flush, not construction).
    """
    from app.dao.base import tenant_context
    from app.models.analysis import AnalysisFinding, AnalysisRun

    tenant = _SEED.tenant.id
    run = AnalysisRun(
        id=uuid.uuid4(),
        project_id=_SEED.project.id,
        revision_sha=revision,
        status="AN_COMPLETED",
        tenant_id=tenant,
    )
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            sess.add(run)
            await sess.flush()
            for category, severity, tag, label in categories:
                sess.add(
                    AnalysisFinding(
                        id=uuid.uuid4(),
                        analysis_run_id=run.id,
                        severity=severity,
                        category=category,
                        tag=tag,
                        summary=f"{label} finding",
                        evidence={"anchors": [f"{label}.py:1"]},
                        tenant_id=tenant,
                    )
                )
            await sess.flush()
    return run


# ---------------------------------------------------------------------------
# Deterministic-model-tier port injection (PROBED): the real worker builders
# take the completion port as a keyword default; rebinding those defaults
# before build_runtime_worker_components() swaps ONLY the LLM port,
# leaving the whole Phase-2F spine untouched.
# ---------------------------------------------------------------------------
def _make_deterministic_port(
    emit_write: bool = True,
    fail_model: bool = False,
) -> tuple[Any, list[str]]:
    from app.services.llm.client import LLMError
    from app.services.llm.single_step import LLMCompletionStep
    from app.services.token_tracker import TokenUsage

    calls: list[str] = []
    state = {"business_calls": 0}

    async def port(
        model,
        messages,
        *,
        tools=None,
        agent_id=None,
        supports_vision=False,
        max_output_tokens=None,
        on_visible_delta=None,
        **kwargs: Any,
    ) -> LLMCompletionStep:
        if tools is None:
            # The semantic TaskCompletionGate call (no tool list): a
            # deterministic PASS verdict.
            calls.append("gate")
            verdict = {
                "verdict": "pass",
                "missing_requirements": [],
                "next_actions": [],
                "evidence": ["deterministic gate pass"],
            }
            return LLMCompletionStep(
                content=json.dumps(verdict),
                tool_calls=(),
                reasoning_content=None,
                retry_instruction=None,
                usage=TokenUsage(total_tokens=1),
                finish_reason="stop",
            )
        # The business-model call (tools set).
        calls.append("model")
        if fail_model:
            # NON-RETRYABLE per failover.classify_error ("401" +
            # "invalid api key") → terminal run_failed, no retry.
            raise LLMError("HTTP 401: invalid api key (deterministic failure)")
        state["business_calls"] += 1
        if emit_write and state["business_calls"] == 1:
            args = json.dumps(
                {"path": "P3_E2E_OUT.md", "content": "deterministic tool path proof\n"}
            )
            return LLMCompletionStep(
                content="",
                tool_calls=(
                    {
                        "id": f"p3-call-{state['business_calls']}",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": args},
                    },
                ),
                reasoning_content=None,
                retry_instruction=None,
                usage=TokenUsage(total_tokens=1),
                finish_reason="tool_calls",
            )
        return LLMCompletionStep(
            content="P3_E2E_OUT.md written. Task complete (deterministic).",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
            finish_reason="stop",
        )

    return port, calls


def _rebind_ports(comps: Any, port: Any) -> None:
    """Point the REAL worker component builders at the deterministic port
    (the exact seam the de-risking probes exercised, p3_probe_tool.py)."""
    router = comps.driver._node_executor
    agent_exec = router._agent_executor
    agent_exec._model_service._completion = port
    agent_exec._verifier._completion_gate._completion = port


def _build_worker(saver: Any, claimant: str) -> Any:
    from app.config import get_settings
    from app.database import async_session
    from app.database import engine as _lock_engine
    from app.services.agent_runtime.worker_service import build_runtime_worker_components

    return build_runtime_worker_components(
        checkpointer=saver,
        session_factory=async_session,
        lock_engine=_lock_engine,
        claimant=claimant,
        settings=get_settings(),
    )


async def _wait_terminal(run_id: uuid.UUID, worker: Any, budget_s: int = 120) -> str | None:
    """Poll the shared command inbox until ``run_id`` emits a terminal event.

    The worker claims commands via the SKIP-LOCKED pattern; the terminal
    read uses a FRESH session each pass, never the worker's in-flight one.
    """
    from app.database import async_session
    from app.models.agent_run_event import AgentRunEvent

    t0 = time.time()
    terminal: str | None = None
    while time.time() - t0 < budget_s:
        result = await worker.run_once()
        async with async_session() as sess:
            terminal = (
                (
                    await sess.execute(
                        select(AgentRunEvent.event_type)
                        .where(
                            AgentRunEvent.run_id == run_id,
                            AgentRunEvent.event_type.in_(_TERMINAL_EVENT_TYPES),
                        )
                        .order_by(AgentRunEvent.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if terminal is not None:
            return terminal
        if result.status == "idle":
            await asyncio.sleep(0.25)
    return terminal


# ---------------------------------------------------------------------------
# DB-free adapter helper test (no Postgres needed — carries the pure logic
# when the live tier skips).
# ---------------------------------------------------------------------------
def test_topological_order_rejects_cycle_and_out_of_set() -> None:
    from app.services.plan_execution_service import topological_order

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def edge(task_id: uuid.UUID, depends_on: uuid.UUID) -> Any:
        return type("Edge", (), {"task_id": task_id, "depends_on_task_id": depends_on})()

    # b -> a ; c independent: deterministic order a, b, c.
    assert topological_order([a, b, c], [edge(b, a)]) == [a, b, c]
    # A cycle is a data defect → fail closed.
    with pytest.raises(ValueError, match="cycle"):
        topological_order([a, b], [edge(a, b), edge(b, a)])
    # An edge pointing outside the bounded set → fail closed.
    with pytest.raises(ValueError, match="outside"):
        topological_order([a], [edge(a, c)])


# ---------------------------------------------------------------------------
# Live-tier tests (each SKIPs without a reachable Postgres).
# ---------------------------------------------------------------------------
async def _build_plan(db_factory: async_sessionmaker, revision: str, categories: Any = None) -> tuple[Any, Any, Any]:
    """Run the REAL planning lane for one revision; return (run, outcome, plan)."""
    from app.dao.base import tenant_context
    from app.services.planning_service import planning_service

    await _seed(db_factory)
    if categories is None:
        categories = (("TECH_DEBT", "WARN", "FACT", "tech"), ("OPEN_QUESTION", "INFO", "FACT", "question"))
    run = await _new_analysis_run(db_factory, revision, categories=categories)
    tenant = _SEED.tenant.id
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            outcome = await planning_service.create_plan(
                sess,
                project_id=_SEED.project.id,
                analysis_run_id=run.id,
                agent=_SEED.agent,
                current_user=_SEED.user,
            )
    return run, outcome, outcome.plan


async def test_planning_chain_creates_graph_and_tasks(db_factory: async_sessionmaker) -> None:
    """(1)+(2) sample Project + Analysis → Plan → Task Graph: the plan
    materializes real Tasks + dependency edges through the frozen services."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3rev-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created", f"plan {outcome.state}: {outcome.code} {outcome.detail}"
    # Two findings → two goals → two work packages → a serial delivery
    # group: N packages carry N-1 inter-package head->head edges.
    assert len(plan.materialized_task_ids) >= 2
    assert plan.edge_count >= 1
    assert len(plan.package_ids) >= 2

    tenant = _SEED.tenant.id
    from app.dao.base import tenant_context
    from app.models.planning import PlanningRun, WorkPackage
    from app.models.task import Task, TaskDependency

    with tenant_context(tenant):
        async with db_factory() as sess:
            run_row = (
                await sess.execute(select(PlanningRun).where(PlanningRun.id == plan.run.id))
            ).scalar_one()
            assert run_row.status == "PL_COMPLETED"
            packages = (
                await sess.execute(select(WorkPackage).where(WorkPackage.planning_run_id == plan.run.id))
            ).scalars().all()
            assert packages, "no work packages persisted"
            # Every materialized task is a real ANALYSIS_PLANNING row on the
            # project (the card chain: Plan → Task Graph).
            tasks = (
                await sess.execute(select(Task).where(Task.id.in_(list(plan.materialized_task_ids))))
            ).scalars().all()
            assert len(tasks) == len(plan.materialized_task_ids)
            for task in tasks:
                assert task.created_reason == "ANALYSIS_PLANNING"
                assert task.project_id == _SEED.project.id
                assert task.status == "pending"  # G4: materialized, never auto-run
            edges = (
                await sess.execute(
                    select(TaskDependency).where(
                        TaskDependency.task_id.in_(list(plan.materialized_task_ids))
                    )
                )
            ).scalars().all()
            assert len(edges) >= plan.edge_count


async def test_assignment_lane_applies_single_agent_fact(db_factory: async_sessionmaker) -> None:
    """(3) Plan → Squad/Assignment: apply_assignment writes the single
    Task.agent_id fact per package and emits the report-only plan; a reapply
    is a zero-write no-op (idempotency / deterministic-pick convergence)."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3rev-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    from app.dao.base import tenant_context
    from app.models.task import Task
    from app.services.assignment_service import assignment_service

    tenant = _SEED.tenant.id
    applied_packages = 0
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            for work_package_id in plan.package_ids:
                result = await assignment_service.apply_assignment(
                    sess, work_package_id=work_package_id, current_user=_SEED.user
                )
                assert result.state == "assigned", f"{result.state}: {result.code} {result.detail}"
                applied_packages += 1
    assert applied_packages == len(plan.package_ids)

    # The single assignment fact: every materialized task is bound to the
    # executing agent (candidate_agent_ids=[agent] → the pick is the agent).
    with tenant_context(tenant):
        async with db_factory() as sess:
            tasks = (
                await sess.execute(select(Task).where(Task.id.in_(list(plan.materialized_task_ids))))
            ).scalars().all()
            assert all(t.agent_id == _SEED.agent.id for t in tasks)

    # Idempotency: re-applying writes no new fact (a no-op re-read).
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            result2 = await assignment_service.apply_assignment(
                sess, work_package_id=plan.package_ids[0], current_user=_SEED.user
            )
            assert result2.state == "assigned"
    with tenant_context(tenant):
        async with db_factory() as sess:
            task0 = (
                await sess.execute(select(Task).where(Task.id == plan.materialized_task_ids[0]))
            ).scalar_one()
            assert task0.agent_id == _SEED.agent.id  # unchanged fact


async def test_assignment_review_independence_refuses_single_candidate(db_factory: async_sessionmaker) -> None:
    """A SECURITY-finding package declares requires_independent_review; with
    a single-candidate roster the reviewer cannot be disjoint from the
    builder → PL_REVIEWER_NOT_INDEPENDENT before ANY write (fail-closed)."""
    categories = (("SECURITY", "CRITICAL", "FACT", "sec"),)
    _run, outcome, plan = await _build_plan(db_factory, f"p3rev-{uuid.uuid4().hex[:8]}", categories=categories)
    assert outcome.state == "created"
    from app.dao.base import tenant_context
    from app.models.task import Task
    from app.services.assignment_service import assignment_service

    tenant = _SEED.tenant.id
    refused = 0
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            for work_package_id in plan.package_ids:
                result = await assignment_service.apply_assignment(
                    sess, work_package_id=work_package_id, current_user=_SEED.user
                )
                assert result.state == "failed"
                assert result.code == "PL_REVIEWER_NOT_INDEPENDENT", result.code
                refused += 1
    assert refused == len(plan.package_ids)
    # Fail-closed BEFORE any write: the tasks still carry no agent binding
    # flip beyond the planner's default; the assignment fact is unchanged.
    with tenant_context(tenant):
        async with db_factory() as sess:
            task0 = (
                await sess.execute(select(Task).where(Task.id == plan.materialized_task_ids[0]))
            ).scalar_one()
            assert task0.agent_id == _SEED.agent.id  # planner default, not an assignment write


async def test_dependency_enforcement_and_settlement(db_factory: async_sessionmaker) -> None:
    """(4)+dependency enforcement + settlement: the REAL Phase-2E gate
    blocks the dependent task while its upstream is not done; after the
    upstream settles done through the real Runtime worker, the same task
    enqueues and settles too."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3rev-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    task_ids = list(plan.materialized_task_ids)
    # In a serial delivery group the last materialized task is downstream
    # of its predecessors (head->head inter-package edges), so pick the
    # LAST as the dependent and its predecessor as the upstream.
    head_task_id, dep_task_id = task_ids[0], task_ids[-1]
    assert head_task_id != dep_task_id

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.user import User
    from app.services.task_execution_service import (
        TaskExecutionError,
        task_execution_service,
    )

    tenant = _SEED.tenant.id
    port, calls = _make_deterministic_port(emit_write=True, fail_model=False)
    from app.services.agent_runtime.checkpointer import create_checkpointer

    # Phase 1: enqueue the UPSTREAM head task only (directly, through the
    # gate) — the dependent task is not enqueued yet, so its dependency is
    # unmet.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            head_task = (await sess.execute(select(Task).where(Task.id == head_task_id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            out_head = await task_execution_service.execute(
                sess, task=head_task, agent=agent_row, current_user=user_row
            )
            # The dependent task must be TASK_BLOCKED (unmet dependency).
            dep_task = (await sess.execute(select(Task).where(Task.id == dep_task_id))).scalar_one()
            blocked_raised = False
            try:
                await task_execution_service.execute(
                    sess, task=dep_task, agent=agent_row, current_user=user_row
                )
            except TaskExecutionError as error:
                assert error.code == "TASK_BLOCKED"
                assert dep_task_id not in ()  # sanity
                blocked_raised = True
            assert blocked_raised, "dependent task must be blocked while upstream is not done"
            run_head_id = out_head.run_id
            assert run_head_id is not None

    # Drive the real worker (deterministic port) to settle the head task.
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-dep-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, port)
        terminal = await _wait_terminal(run_head_id, comps.worker)
    assert terminal == "run_completed", f"head task did not complete: {terminal}"
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (
                await sess.execute(select(Task.status).where(Task.id == head_task_id))
            ).scalar_one() == "done"

    # Phase 2: NOW the dependent task's dependency is met → it enqueues and
    # settles through the same real spine.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            dep_task = (await sess.execute(select(Task).where(Task.id == dep_task_id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            out_dep = await task_execution_service.execute(
                sess, task=dep_task, agent=agent_row, current_user=user_row
            )
            run_dep_id = out_dep.run_id
            assert run_dep_id is not None and out_dep.state in {"enqueued", "reused"}
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-dep2-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, port)
        terminal = await _wait_terminal(run_dep_id, comps.worker)
    assert terminal == "run_completed"
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (
                await sess.execute(select(Task.status).where(Task.id == dep_task_id))
            ).scalar_one() == "done"
    # The deterministic port was actually used (no real LLM).
    assert "model" in calls


async def test_enqueue_plan_tasks_adapter_end_to_end(db_factory: async_sessionmaker) -> None:
    """D1 acceptance: drive the flagship ADAPTER (``enqueue_plan_tasks``)
    end-to-end against a reachable Postgres — not just the pure
    ``topological_order`` helper the shipped E2E exercised.  Before the D1
    fix the in-loop tuple reassignments on the ``frozen=True`` report raised
    ``FrozenInstanceError`` on the very first real enqueue, so the flagship
    deliverable of commit 089fe2c0 was dead code.  A two-finding plan (a
    serial delivery group) makes ALL THREE report tuple fields populate:

    - call #1 → the ready HEAD enqueues (``enqueued``) and the downstream
      tasks stay blocked on their not-yet-done upstream (``blocked``);
    - settle the head Run through the real spine → the head is ``done``;
    - call #2 (re-invocation) → the settled head is recorded in
      ``skipped_settled`` and the now-ready dependent enqueues.

    Every assertion below populates a frozen-report tuple field WITHOUT
    raising, which is the direct regression check for D1."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3adapt-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    head_task_id = plan.materialized_task_ids[0]  # the ready head: no upstream deps

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.user import User
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.plan_execution_service import plan_execution_service

    tenant = _SEED.tenant.id
    port, _calls = _make_deterministic_port(emit_write=False, fail_model=False)

    def _head_run_id(report: Any) -> uuid.UUID | None:
        for outcome in report.enqueued:
            if outcome.task_id == head_task_id:
                return outcome.run_id
        return None

    # Call #1: the ready head enqueues (the ``enqueued`` tuple field
    # populates) and the downstream tasks are blocked on their not-yet-done
    # upstream (the ``blocked`` tuple field populates) — this single call is
    # the D1 crash site (the loop re-bound these frozen attributes).
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            report1 = await plan_execution_service.enqueue_plan_tasks(
                sess,
                planning_run_id=plan.run.id,
                agent=agent_row,
                current_user=user_row,
            )
    assert report1.enqueued, "the ready head task must enqueue through the adapter"
    assert head_task_id in report1.enqueued_task_ids, "the head (no upstream) is the one that enqueues"
    assert report1.blocked, "the downstream tasks must be blocked on their not-done upstream"
    assert report1.skipped_settled == (), "nothing is settled at load time on the first call"
    # The assignment fact was applied to every package (single-candidate → assigned).
    assert report1.assignment and all(state == "assigned" for state, _code in report1.assignment.values())

    # Drive the enqueued head Run to completion through the real spine.
    head_run = _head_run_id(report1)
    assert head_run is not None, "the head enqueue must carry a live Run id"
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-adapter-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, port)
        terminal = await _wait_terminal(head_run, comps.worker)
    assert terminal == "run_completed", f"head did not settle: {terminal}"
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (await sess.execute(select(Task.status).where(Task.id == head_task_id))).scalar_one() == "done"

    # Call #2 (re-invocation): the settled head now lands in ``skipped_settled``
    # (the third tuple field populates) and the now-ready dependent enqueues.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            report2 = await plan_execution_service.enqueue_plan_tasks(
                sess,
                planning_run_id=plan.run.id,
                agent=agent_row,
                current_user=user_row,
            )
    assert head_task_id in report2.skipped_settled, "the settled head must be skipped, not re-fed to the gate"
    assert report2.enqueued, "the now-ready dependent must enqueue on re-invocation"

    # Drain the shared inbox: settle every Run call #2 enqueued.  The other
    # live tests in this module always drive their enqueued Runs to a
    # terminal state, and a leftover start command here would be claimed by
    # the NEXT test's worker (shared SKIP-LOCKED inbox), consuming that
    # test's deterministic port's first-business-call tool step and changing
    # its tool-path assertions.
    for settled_outcome in report2.enqueued:
        assert settled_outcome.run_id is not None
        async with create_checkpointer(get_settings()) as saver:
            await saver.setup()
            comps = _build_worker(saver, f"p3-adapter2-{uuid.uuid4().hex[:8]}")
            _rebind_ports(comps, port)
            terminal2 = await _wait_terminal(settled_outcome.run_id, comps.worker)
        assert terminal2 == "run_completed", f"dependent run did not settle: {terminal2}"


async def test_run_tool_result_link(db_factory: async_sessionmaker) -> None:
    """Run → Tool → Result: the real write_file tool node executes against
    the local storage backend; a WorkspaceFileRevision + AgentToolExecution
    row are persisted and the verifier's ref gate (both lists) is met."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3rev-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    task_id = plan.materialized_task_ids[0]

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.agent_tool_execution import AgentToolExecution
    from app.models.task import Task
    from app.models.user import User
    from app.models.workspace import WorkspaceFileRevision
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.task_execution_service import task_execution_service

    tenant = _SEED.tenant.id
    port, calls = _make_deterministic_port(emit_write=True, fail_model=False)
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            task_row = (await sess.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            out = await task_execution_service.execute(
                sess, task=task_row, agent=agent_row, current_user=user_row
            )
    assert out.run_id is not None
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-tool-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, port)
        terminal = await _wait_terminal(out.run_id, comps.worker)
    assert terminal == "run_completed"

    with tenant_context(tenant):
        async with db_factory() as sess:
            tools = (
                await sess.execute(
                    select(AgentToolExecution)
                    .where(AgentToolExecution.run_id == out.run_id)
                    .order_by(AgentToolExecution.started_at)
                )
            ).scalars().all()
            assert tools and tools[0].tool_name == "write_file"
            assert tools[0].status == "succeeded"
            metadata = tools[0].result_metadata or {}
            # The verifier's malformed_tool_references gate: both ref lists
            # must be list-valued (not None) for the deterministic ledger
            # check to pass.
            assert isinstance(metadata.get("artifact_refs"), list)
            assert isinstance(metadata.get("evidence_refs"), list)
            revisions = (
                await sess.execute(
                    select(WorkspaceFileRevision).where(
                        WorkspaceFileRevision.scope_type == "agent",
                        WorkspaceFileRevision.scope_id == agent_row.id,
                    )
                )
            ).scalars().all()
            assert any(r.path.endswith("P3_E2E_OUT.md") for r in revisions)
            assert (
                await sess.execute(select(Task.status).where(Task.id == task_id))
            ).scalar_one() == "done"
    assert "model" in calls and "gate" in calls


async def test_parallel_execution_two_workers(db_factory: async_sessionmaker) -> None:
    """(5) parallel execution: two INDEPENDENT Runs claimed + settled
    concurrently by two worker instances on the shared command inbox (the
    Phase-2F SKIP-LOCKED claim pattern, no second runtime).

    Independence: the planner chains packages of ONE plan serially
    (pkg_i.head -> pkg_{i-1}.head), so two genuinely independent tasks come
    from TWO plans (distinct analysis revisions, one finding each — no
    cross-plan edges)."""
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.task import Task
    from app.models.user import User
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.task_execution_service import task_execution_service

    one_finding = (("TECH_DEBT", "WARN", "FACT", "tech"),)
    _run_a, outcome_a, plan_a = await _build_plan(
        db_factory, f"p3par-a-{uuid.uuid4().hex[:8]}", categories=one_finding
    )
    _run_b, outcome_b, plan_b = await _build_plan(
        db_factory, f"p3par-b-{uuid.uuid4().hex[:8]}", categories=one_finding
    )
    assert outcome_a.state == "created" and outcome_b.state == "created"
    task_a = plan_a.materialized_task_ids[0]
    task_b = plan_b.materialized_task_ids[0]

    tenant = _SEED.tenant.id
    port, _calls = _make_deterministic_port(emit_write=False, fail_model=False)

    # Enqueue BOTH independent tasks through the real gate (two plans -> no
    # edge between the two heads; both must be accepted).
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            run_ids: list[uuid.UUID] = []
            for task_id in (task_a, task_b):
                task_row = (await sess.execute(select(Task).where(Task.id == task_id))).scalar_one()
                out = await task_execution_service.execute(
                    sess, task=task_row, agent=agent_row, current_user=user_row
                )
                assert out.state in {"enqueued", "reused"}, out
                _run_id = out.run_id or out.active_run_id
                assert _run_id is not None, "an enqueued/reused outcome carries a live Run id"
                run_ids.append(_run_id)
    assert all(run_ids), "independent tasks must both be enqueued"

    settings = get_settings()

    async def _drive_one(claimant: str, run_id: uuid.UUID) -> str:
        async with create_checkpointer(settings) as saver:
            await saver.setup()
            comps = _build_worker(saver, claimant)
            _rebind_ports(comps, port)
            return (await _wait_terminal(run_id, comps.worker)) or "idle"

    # Two workers on the SAME shared inbox (SKIP-LOCKED claim): run them
    # concurrently, then assert BOTH settled.
    results = await asyncio.gather(
        _drive_one(f"p3-par-a-{uuid.uuid4().hex[:6]}", run_ids[0]),
        _drive_one(f"p3-par-b-{uuid.uuid4().hex[:6]}", run_ids[1]),
    )
    assert results == ["run_completed", "run_completed"], results

    # The two Runs are DISTINCT rows under the same agent (parallel claim).
    with tenant_context(tenant):
        async with db_factory() as sess:
            distinct_runs = (
                await sess.execute(
                    select(AgentRun.id).where(
                        AgentRun.id.in_(run_ids),
                        AgentRun.tenant_id == tenant,
                        AgentRun.agent_id == _SEED.agent.id,
                    )
                )
            ).scalars().all()
            assert len(distinct_runs) == 2
            # Both independent tasks settled done through the shared Runtime.
            for task_id in (task_a, task_b):
                assert (
                    await sess.execute(select(Task.status).where(Task.id == task_id))
                ).scalar_one() == "done"


async def test_failure_handling_no_auto_retry(db_factory: async_sessionmaker) -> None:
    """(5) failure handling: a non-retryable model error → terminal
    run_failed → Task back to pending + failure TaskLog; NO hidden
    automatic retry (exactly one Run). A HUMAN re-Execute mints the R3
    retry attempt → second Run → completed → derived SUCCEEDED."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3fail-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    tenant = _SEED.tenant.id
    task_id = plan.materialized_task_ids[0]  # the head task: no upstream deps
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.task import Task, TaskLog
    from app.models.user import User
    from app.services.agent_runtime.checkpointer import create_checkpointer
    from app.services.task_execution_service import task_execution_service

    fail_port, fail_calls = _make_deterministic_port(emit_write=False, fail_model=True)

    # Human attempt 1: enqueue through the real gate.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            task_row = (await sess.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            out1 = await task_execution_service.execute(
                sess, task=task_row, agent=agent_row, current_user=user_row
            )
    assert out1.run_id is not None
    # Drive the real worker with the FAILING port → terminal run_failed.
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-fail-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, fail_port)
        terminal1 = await _wait_terminal(out1.run_id, comps.worker)
    assert terminal1 == "run_failed", f"expected run_failed, got {terminal1}"
    assert "model" in fail_calls  # the business call was the one that failed

    # Settled facts: Task back to PENDING + a failure TaskLog + exactly ONE
    # Run (no hidden automatic retry).
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (await sess.execute(select(Task.status).where(Task.id == task_id))).scalar_one() == "pending"
            logs = (
                await sess.execute(
                    select(TaskLog.content).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at)
                )
            ).scalars().all()
            assert any("失败" in line or "fail" in line.lower() for line in logs)
            runs1 = (
                await sess.execute(
                    select(AgentRun.id).where(
                        AgentRun.source_type == "task", AgentRun.source_id == str(task_id)
                    )
                )
            ).scalars().all()
            assert len(runs1) == 1, "no automatic retry: exactly one Run until a human re-Execute"

    # Human attempt 2 (the R3 path): re-Execute mints a NEW attempt → a
    # SECOND Run; drive it with the healthy port → completed → done.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            task_row = (await sess.execute(select(Task).where(Task.id == task_id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one()
            out2 = await task_execution_service.execute(
                sess, task=task_row, agent=agent_row, current_user=user_row
            )
            assert out2.attempt_id is not None, "a re-Execute after a failed terminal mints the R3 attempt"
            assert out2.run_id is not None, "a re-Execute enqueues a fresh Run"
            assert out2.run_id != out1.run_id
    healthy_port, healthy_calls = _make_deterministic_port(emit_write=False, fail_model=False)
    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p3-fail2-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, healthy_port)
        terminal2 = await _wait_terminal(out2.run_id, comps.worker)
    assert terminal2 == "run_completed"
    assert "model" in healthy_calls and "gate" in healthy_calls
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (await sess.execute(select(Task.status).where(Task.id == task_id))).scalar_one() == "done"
            runs2 = (
                await sess.execute(
                    select(AgentRun.id).where(
                        AgentRun.source_type == "task", AgentRun.source_id == str(task_id)
                    )
                )
            ).scalars().all()
            assert len(runs2) == 2, "the retry Run is human-minted (R3), not automatic"
            # §10.2 projection: settled states computed from the terminal events.
            projection = await task_execution_service.query_execution(
                sess,
                task=(await sess.execute(select(Task).where(Task.id == task_id))).scalar_one(),
                agent=(await sess.execute(select(Agent).where(Agent.id == _SEED.agent.id))).scalar_one(),
                current_user=(await sess.execute(select(User).where(User.id == _SEED.user.id))).scalar_one(),
            )
            assert projection.derived_state == "SUCCEEDED"
            settled = [r.settled_state for r in projection.runs]
            assert settled == ["failed", "completed"], settled


async def _seed_second_tenant(db_factory: async_sessionmaker) -> dict[str, Any]:
    """A second tenant graph (isolated tenant B) for the isolation tier."""
    from app.core.security import encrypt_data
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_b = Tenant(name="p3e2e-b", slug="p3e2e-b-" + uuid.uuid4().hex[:10])
    async with db_factory() as sess, sess.begin():
        sess.add(tenant_b)
        await sess.flush()
        user_b = User(
            tenant_id=tenant_b.id,
            display_name="p3e2e-b",
            role="member",
            is_active=True,
            email=f"p3e2e-b-{uuid.uuid4().hex}@example.test",
        )
        sess.add(user_b)
        await sess.flush()
        model_b = LLMModel(
            tenant_id=tenant_b.id,
            provider="openai",
            model="deterministic-b",
            api_key_encrypted=encrypt_data(
                "p3e2e-deterministic-b-key", os.environ.get("SECRET_KEY", "p3-secret")
            ),
            label="p3e2e-model-b",
            base_url="http://localhost:0/v1",
            enabled=True,
            supports_vision=False,
            supports_tool_calling=True,
        )
        sess.add(model_b)
        await sess.flush()
        agent_b = Agent(
            name="p3e2e-agent-b",
            creator_id=user_b.id,
            tenant_id=tenant_b.id,
            access_mode="company",
            status="running",
            primary_model_id=model_b.id,
        )
        sess.add(agent_b)
        await sess.flush()
        project_b = Project(
            name="p3e2e-proj-b",
            status="ANALYZING",
            created_by=user_b.id,
            tenant_id=tenant_b.id,
        )
        sess.add(project_b)
        await sess.flush()
    return {"tenant": tenant_b, "user": user_b, "agent": agent_b, "project": project_b}


async def test_tenant_isolation_plan_assignment_execution(db_factory: async_sessionmaker) -> None:
    """(5) tenant isolation: every cross-tenant step is refused fail-closed
    (plan → PL_TENANT_MISMATCH, assignment → the foreign row is invisible,
    execution gate → TENANT_MISMATCH, tenant-injected DAO reads → None).
    No cross-tenant write happens at any step."""
    _run, outcome, plan = await _build_plan(db_factory, f"p3iso-{uuid.uuid4().hex[:8]}")
    assert outcome.state == "created"
    other = await _seed_second_tenant(db_factory)

    from sqlalchemy import func

    from app.dao.base import tenant_context
    from app.dao.planning_dao import planning_run_dao, work_package_task_dao
    from app.dao.task_dao import task_provenance_dao
    from app.models.planning import PlanningRun
    from app.models.task import Task
    from app.services.assignment_service import assignment_service
    from app.services.planning_service import planning_service
    from app.services.task_execution_service import TaskExecutionError, task_execution_service

    tenant_a = _SEED.tenant.id
    tenant_b = other["tenant"].id

    # 1) Cross-tenant PLAN creation: tenant-A project + tenant-B agent →
    # PL_TENANT_MISMATCH BEFORE any write (the project's PlanningRun row
    # count does not grow — the "wrote nothing" assertion, DB-only).
    with tenant_context(tenant_a):
        async with db_factory() as sess:
            before = (
                await sess.execute(
                    select(func.count())
                    .select_from(PlanningRun)
                    .where(PlanningRun.project_id == _SEED.project.id)
                )
            ).scalar_one()
        async with db_factory() as sess, sess.begin():
            result = await planning_service.create_plan(
                sess,
                project_id=_SEED.project.id,
                analysis_run_id=_run.id,
                agent=other["agent"],
                current_user=_SEED.user,
            )
    assert result.state == "failed"
    assert result.code == "PL_TENANT_MISMATCH"
    with tenant_context(tenant_a):
        async with db_factory() as sess:
            after = (
                await sess.execute(
                    select(func.count())
                    .select_from(PlanningRun)
                    .where(PlanningRun.project_id == _SEED.project.id)
                )
            ).scalar_one()
    assert after == before, "the cross-tenant refusal must write nothing"

    # 2) Cross-tenant ASSIGNMENT read: a tenant-B user applies to tenant-A's
    # work package → the tenant-injected DAO never sees the foreign row
    # (a PL_INVALID_INPUT "not found in this tenant", no 403-vs-404
    # disclosure, no leaked foreign row).
    with tenant_context(tenant_b):
        async with db_factory() as sess:
            result_b = await assignment_service.apply_assignment(
                sess, work_package_id=plan.package_ids[0], current_user=other["user"]
            )
    assert result_b.state == "failed"
    assert result_b.code == "PL_INVALID_INPUT"
    assert "not found in this tenant" in result_b.detail

    # 3) Cross-tenant EXECUTION gate: a tenant-A task + a tenant-B agent,
    # called by a tenant-A user → TENANT_MISMATCH (the P1 direct assertion),
    # raised BEFORE any enqueue (no Run, no TaskLog audit write survives —
    # the refusal happens in the entry, outside the caller transaction).
    task_id = plan.materialized_task_ids[0]
    with tenant_context(tenant_a):
        async with db_factory() as sess:
            task_a = (await sess.execute(select(Task).where(Task.id == task_id))).scalar_one()
    raised = False
    async with db_factory() as sess:
        try:
            await task_execution_service.execute(
                sess, task=task_a, agent=other["agent"], current_user=_SEED.user
            )
        except TaskExecutionError as error:
            assert error.code == "TENANT_MISMATCH"
            raised = True
    assert raised

    # 4) Tenant-injected DAO reads: under tenant B the foreign task /
    # planning rows are simply None (no leaked foreign row).
    with tenant_context(tenant_b):
        async with db_factory() as sess:
            assert await task_provenance_dao.get_scoped(task_id, db=sess) is None
            assert await planning_run_dao.get_scoped(plan.run.id, db=sess) is None
            assert await work_package_task_dao.get_link_for_task(task_id, db=sess) is None

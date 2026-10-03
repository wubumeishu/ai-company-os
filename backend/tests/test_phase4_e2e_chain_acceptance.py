"""Phase 4 §7 real E2E: the FULL integrated chain on real execution
(card t_ab1fb86a, Root §10 / §7 acceptance proof).

One continuous execution on a REAL scratch Postgres (the real ``alembic
upgrade head`` single-head f073 chain — never a second runtime), reusing
the FROZEN Phase 2F Runtime spine + the four APPROVED Phase 4 lanes:

    Task ─→ Assignment ─→ Runtime ─→ Run ─→ Artifact/Evidence ─→ Review
         ─→ Rework ─→ Re-review ─→ Completion ─→ Delivery

Consumed lanes (APPROVED upstream, executed here, never re-invented):
- storage + review/rework core (t_b9e57a65, F1-closed): rework writes NEW
  artifact + evidence + ``superseded_by`` links and NO kind='review' row;
- completion lane (t_e399386f): CT/CW/CP + CP_*/CD_* + the SINGLE owning
  COMPLETED write site + C5 decision rows;
- delivery record lane (t_af586c02): CP_OK-gated, cites only SEALED +
  current-valid-approving artifacts;
- fail-closed fix (t_56fbca2e): a TaskCompletionGate ERROR fails CLOSED —
  the Run is terminal-failed and the Task is NOT marked done.

Execution-marking discipline (Root §7 — no mixing, stated per sub-test):
- **DB-only tier (every live test below)**: real Postgres rows (the real
  f072/f073 DDL), the real frozen Phase-2F worker spine, but the LLM
  completion port is a DETERMINISTIC fake injected into the real worker
  builders (the Phase-3 seam, ``_rebind_ports``); the model row's base_url
  is unreachable (http://localhost:0/v1) so any accidental real HTTP call
  fails loudly; the drive asserts the deterministic port's call log is
  non-empty. NO real LLM is called anywhere in this module — the
  real-LLM variant is OUT OF SCOPE here (the Phase 2F real-run drivers
  own that seam, docs/evidence/phase2f/).
- **DB-free tier (``test_db_free_*``)**: pure ledger reads (G2 stale
  approve drop-out, G3 rework provenance walk) over in-memory rows — no
  Postgres at all.
Each live test's docstring carries its own marker: [DB-only + mock agent].

The live tests are ONE continuous chain: each test commits its ledger
state and the next runs over it (pytest file-order is the contract).  The
chain's hops and their proving tests:
- hop Task/Assignment → ``test_assignment_picks_builder_and_disjoint_reviewer``
  (invariant-13 at assignment time; the negative single-candidate package
  refuses with PL_REVIEWER_NOT_INDEPENDENT before any write,
  ``test_assignment_review_independence_refuses_single_candidate_wp``);
- hop Runtime/Run/Artifact/Evidence (G4) →
  ``test_runtime_run_produces_artifact_and_evidence``;
- hop Review verdicts + Rework plan (the disjoint reviewer's kind='review'
  verdicts, F1: the builder's verdict is rejected, I-4: no-proof rework
  fails closed) → ``test_review_rework_rereview_chain``;
- hop Rework Run + Re-review (a second real Run mints the rework's
  execution; NEW v2 rows + superseded_by → disjoint re-review, G2
  stale-approve drop-out + G3 provenance walk on the live ledger) →
  ``test_rework_run_and_disjoint_rereview``;
- hop the disjoint reviewer's OWN Run on the review task (its
  execution-linked artifact + verdict, CP in-scope for the review task) →
  ``test_reviewer_run_produces_review_task_ledger_rows``;
- hop Completion (CP_OK drives the SINGLE owning write site; COMPLETED
  exactly once; idempotent re-call; Root §5: Task.status / final_answer /
  gate verdict are NOT completion inputs) →
  ``test_completion_cp_ok_completes_project_exactly_once``;
- hop Delivery (cites ONLY the SEALED + current-valid-approving set;
  idempotent re-delivery) → ``test_delivery_cites_only_sealed_approving``;
- negatives → ``test_non_cp_ok_project_writes_no_delivery``,
  ``test_cross_tenant_blocked_before_any_write``,
  ``test_gate_error_fails_closed_task_not_done``.

Running this suite (a reachable Postgres + the real alembic chain are
REQUIRED; the live tier SKIPs otherwise — the honest evidence boundary;
the DB-free tier carries the pure logic)::

    "I:/project/AI Company OS/backend/.venv/Scripts/python.exe" _provision_p4_e2e.py
    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t_ab1fb86a_f073 \
        I:/project/AI Company OS/backend/.venv/Scripts/python.exe \
        -m pytest tests/test_phase4_e2e_chain_acceptance.py -v

``_provision_p4_e2e.py`` also pre-applies the LangGraph checkpointer
migrations on the scratch DB (``CREATE INDEX CONCURRENTLY`` waits for all
open snapshots, so running them inside the test process — where an idle
in-transaction session from an earlier hop can exist — blocks forever; the
provision step is the single point where no test session is open yet).
On Windows, the pytest process needs the selector event loop: the module
sets ``WindowsSelectorEventLoopPolicy`` at import time when available.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

# Register the full model graph in the shared Base metadata so raw-FK
# resolution works on first flush: tasks carries FKs to analysis_runs /
# analysis_findings (app/models/task.py), and SQLAlchemy resolves those
# only when the referenced models are mapped.
import app.models.analysis  # noqa: F401  (registers AnalysisRun / AnalysisFinding / ProjectKnowledge)
from app.config import get_settings

# ---------------------------------------------------------------------------
# Windows: asyncpg + psycopg (the LangGraph checkpointer) need a selector
# event loop; pytest-asyncio builds each test loop from the process policy.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_WS_DIR = Path(
    os.environ.get(
        "P4_E2E_WS",
        f"C:/Users/Administrator/AppData/Local/Temp/p4_e2e_ws_{uuid.uuid4().hex[:8]}",
    )
)

_SKIP_MESSAGE = (
    "no reachable Postgres for the Phase 4 §7 E2E chain; provision with "
    "backend/_provision_p4_e2e.py (real alembic upgrade head, single head) "
    "and run with DATABASE_URL=<clawith scratch DSN> (skip = the honest "
    "evidence boundary; the DB-free tier carries the pure logic)"
)

_TERMINAL_EVENT_TYPES = ("run_completed", "run_failed", "run_cancelled")

# Fixed, ordered agent ids (the main tenant group only): the deterministic
# REV-1 pick (``resolve_candidates`` — first survivor in sorted-planner
# order) is then REPRODUCIBLE across runs: the builder ALWAYS sorts before
# the reviewer, so the build slot binds the builder and the flagged review
# slot binds the disjoint reviewer.  The chain's facts assert exact ids.
BUILDER_AGENT_ID = uuid.UUID("61000000-0000-0000-0000-000000000001")
REVIEWER_AGENT_ID = uuid.UUID("61000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Live-DB guard (the established E2E pattern: probe once per session on a raw
# asyncpg connection).  Unlike the Phase-3 module there is NO create_all:
# the scratch DB is provisioned by the REAL alembic chain (_provision_p4_e2e.py
# → ``alembic upgrade head``), so the live tier runs against the real f072
# ledger + f073 delivery DDL (the D3 lesson).  A missing f073 table SKIPs
# the live tier — the honest boundary, never a silent re-provision.
# ---------------------------------------------------------------------------
def _probe_live_db() -> bool:
    import asyncpg

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


def _require_alembic_chain() -> None:
    """The real f072/f073 DDL must exist (the module reuses frozen schema,
    it never re-provisions it): probe both ledger tables + the f073 delivery
    table + a LangGraph checkpoint table on one raw connection."""
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(get_settings().DATABASE_URL, pool_pre_ping=True)

    async def _check() -> None:
        async with engine.connect() as conn:
            for table in ("artifact_records", "evidence_records", "delivery_records"):
                await conn.execute(_text(f"SELECT 1 FROM {table} LIMIT 1"))
            tables = {row[0] for row in (await conn.execute(_text("SELECT tablename FROM pg_tables")))}
            assert any("checkpoint" in t for t in tables), (
                "LangGraph checkpoint tables missing — provision the scratch DB via _provision_p4_e2e.py "
                "(the real alembic chain), never create_all"
            )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_check())
    finally:
        loop.close()
        try:
            loop2 = asyncio.new_event_loop()
            loop2.run_until_complete(engine.dispose())
            loop2.close()
        except Exception:  # noqa: BLE001, S110 - best-effort teardown
            pass


# ---------------------------------------------------------------------------
# Module seed (ONE committed tenant graph the whole chain runs over).  The
# planning-lane rows are materialized through the FROZEN DAOs the planning
# service itself uses (planning_run_dao / planning_goal_dao / milestone_dao /
# work_package_dao / work_package_task_dao / task_graph_service /
# task_provenance_dao) — no second planning lane: the candidate rosters are
# the one test-controlled input (create_plan emits single-candidate rosters,
# which cannot pick a DISJOINT reviewer — the multi-candidate scope is what
# exercises invariant-13 at assignment time, this chain's requirement 2).
# ---------------------------------------------------------------------------
class _SEEDState:
    tenant_a: Any
    user_a: Any
    model_a: Any
    builder: Any
    reviewer: Any
    project_main: Any
    project_rework: Any
    project_gate: Any
    project_orphan: Any
    project_negwp: Any
    gate_task: Any
    orphan_task: Any
    build_task: Any
    review_task: Any
    rework_build_task: Any
    rework_review_task: Any
    build2_task: Any
    review2_task: Any
    tenant_b: Any
    user_b: Any
    agent_b: Any
    project_b: Any
    task_b: Any
    wp_main: Any
    wp_rework: Any
    wp_neg: Any
    seeded: bool

    def __init__(self) -> None:
        self.tenant_a = None
        self.user_a = None
        self.model_a = None
        self.builder = None
        self.reviewer = None
        self.project_main = None
        self.project_rework = None
        self.project_gate = None
        self.project_orphan = None
        self.project_negwp = None
        self.gate_task = None
        self.orphan_task = None
        self.build_task = None
        self.review_task = None
        self.rework_build_task = None
        self.rework_review_task = None
        self.build2_task = None
        self.review2_task = None
        self.tenant_b = None
        self.user_b = None
        self.agent_b = None
        self.project_b = None
        self.task_b = None
        self.wp_main = None
        self.wp_rework = None
        self.wp_neg = None
        self.seeded = False


_SEED = _SEEDState()


async def _make_agent(
    sess: Any, *, name: str, user: Any, model: Any, tenant: Any, tag: str, fixed_id: uuid.UUID | None = None
) -> Any:
    from app.models.agent import Agent

    agent = Agent(
        **({"id": fixed_id} if fixed_id is not None else {}),
        name=f"p4e2e-{name}-{tag}",
        creator_id=user.id,
        tenant_id=tenant.id,
        access_mode="company",
        status="running",
        primary_model_id=model.id,
    )
    sess.add(agent)
    await sess.flush()
    return agent


async def _make_model(sess: Any, *, tenant: Any, user: Any, tag: str) -> Any:
    from app.core.security import encrypt_data
    from app.models.llm import LLMModel

    model = LLMModel(
        tenant_id=tenant.id,
        provider="openai",
        model="deterministic",
        # Encrypted so the row passes any decrypt path; base_url is
        # UNREACHABLE so an accidental real HTTP call fails loudly (the
        # deterministic port is the only model caller in this module).
        api_key_encrypted=encrypt_data(f"p4e2e-{tag}-key", os.environ.get("SECRET_KEY", "p4-secret")),
        label=f"p4e2e-{tag}-model",
        base_url="http://localhost:0/v1",
        enabled=True,
        supports_vision=False,
        supports_tool_calling=True,
    )
    sess.add(model)
    await sess.flush()
    return model


async def _seed_agent_group(
    sess: Any, *, tenant: Any, user: Any, tag: str, agents: tuple[str, ...] = ("builder", "reviewer")
) -> dict[str, Any]:
    model = await _make_model(sess, tenant=tenant, user=user, tag=tag)
    out: dict[str, Any] = {"model": model}
    fixed: dict[str, uuid.UUID] = {}
    if tag == "main":
        # Fixed ids: the builder sorts before the reviewer in the
        # deterministic REV-1 pick (see BUILDER_AGENT_ID /
        # REVIEWER_AGENT_ID) — the chain's disjointness facts assert
        # exact agent ids, so the roster order must be reproducible.
        fixed = {"builder": BUILDER_AGENT_ID, "reviewer": REVIEWER_AGENT_ID}
    for name in agents:
        out[name] = await _make_agent(
            sess, name=name, user=user, model=model, tenant=tenant, tag=tag, fixed_id=fixed.get(name)
        )
    return out


# ---------------------------------------------------------------------------
# Storage fixture (the established E2E pattern): local storage backend +
# no-op (Redis) workspace locks for the tool path.  The scratch host has no
# Redis; the real lock path is covered by the Phase 2F drivers.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _install_scratch_storage() -> Any:
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
        vars(mod)["workspace_locks"] = _noop_locks
    yield
    facade._storage_backend = None


# ---------------------------------------------------------------------------
# The planning-lane materialization, through the frozen DAOs the planning
# service itself uses.  One WP: build task + review task (the reviewer slot
# blocks every builder task, REV-2), multi-candidate rosters so the
# deterministic pick has room to stay disjoint.
# ---------------------------------------------------------------------------
async def _materialize_wp(
    db_factory: async_sessionmaker,
    *,
    tenant: Any,
    user: Any,
    project: Any,
    agents: tuple[Any, ...],
    wp_tag: str,
    requires_independent_review: bool = True,
    milestone_kind: str = "delivery",
) -> tuple[Any, Any, list[Any], list[Any], Any]:
    """Materialize one committed plan revision + WP (run/goal/milestone/WP/
    tasks/link rows) through the frozen DAOs; commit.  The build task and
    the review task are INDEPENDENT slots (no blocking edge between them —
    the reviewer's Run is the reviewer agent's own execution, gated by the
    reviewer's separate assignment pick, not by the builder task's status):
    that keeps the rework lane's CP scope checkable — the reviewer task is
    in the delivery scope, and the rework's artifact rows ride on the
    REVIEWER task's run, so the rework WP stays OUTSIDE the delivery
    milestones (a gate/phase bucket, ordering only, never completion)."""
    from app.dao.base import tenant_context
    from app.dao.planning_dao import (
        milestone_dao,
        planning_goal_dao,
        planning_run_dao,
        work_package_dao,
        work_package_task_dao,
    )
    from app.dao.task_dao import task_provenance_dao
    from app.models.planning import Milestone, PlanningGoal, PlanningRun, WorkPackage, WorkPackageTask
    from app.models.task import Task
    from app.services.task_graph_service import task_graph_service

    tenant_id = tenant.id
    revision = f"p4rev-{wp_tag}-{uuid.uuid4().hex[:8]}"
    slots = [
        {
            "slot": "build",
            "kind": "build",
            "title": f"p4 build task {wp_tag}",
            "description": "the builder slot",
            "is_head": True,
            "required_capabilities": ["code"],
            "candidate_agent_ids": [str(a.id) for a in agents],
        },
        {
            "slot": "review",
            "kind": "review",
            "title": f"p4 review task {wp_tag}",
            "description": "the reviewer slot",
            "is_head": False,
            "required_capabilities": ["review"],
            "candidate_agent_ids": [str(a.id) for a in agents],
        },
    ]
    tasks: list[Any] = []
    with tenant_context(tenant_id):
        async with db_factory() as sess, sess.begin():
            run = PlanningRun(
                project_id=project.id,
                analysis_revision_sha=revision,
                planner_agent_id=agents[0].id,
                status="PL_OPEN",
                tenant_id=tenant_id,
            )
            await planning_run_dao.open_run(run, tenant_id=tenant_id, db=sess)
            goal = PlanningGoal(
                planning_run_id=run.id,
                title=f"p4 goal {wp_tag}",
                required_capabilities=["code"],
                status="PL_PROPOSED",
                tenant_id=tenant_id,
            )
            await planning_goal_dao.add_goal(goal, tenant_id=tenant_id, db=sess)
            milestone = await milestone_dao.add_milestone(
                Milestone(
                    planning_run_id=run.id,
                    kind=milestone_kind,
                    seq=0,
                    title=f"p4 {milestone_kind} {wp_tag}",
                    tenant_id=tenant_id,
                ),
                tenant_id=tenant_id,
                db=sess,
            )
            wp = WorkPackage(
                planning_run_id=run.id,
                planning_goal_id=goal.id,
                milestone_id=milestone.id,
                title=f"p4 work package {wp_tag}",
                task_scope=slots,
                execution_mode="serial",
                requires_independent_review=requires_independent_review,
                tenant_id=tenant_id,
            )
            await work_package_dao.add_package(wp, tenant_id=tenant_id, db=sess)
            for entry in slots:
                task = Task(
                    agent_id=agents[0].id,
                    created_by=user.id,
                    tenant_id=tenant_id,
                    project_id=project.id,
                    revision_sha=revision,
                    created_reason="ANALYSIS_PLANNING",
                    title=entry["title"],
                    description=entry["description"],
                    type="todo",
                    status="pending",
                    priority="medium",
                )
                await task_provenance_dao.create_with_provenance(task, db=sess)
                tasks.append(task)
                link = WorkPackageTask(work_package_id=wp.id, tenant_id=tenant_id)
                await work_package_task_dao.add_slot(link, tenant_id=tenant_id, db=sess)
                await work_package_task_dao.materialize_slot(link, task_id=task.id, db=sess)
            if requires_independent_review:
                # REV-2: the flagged package's reviewer task blocks the
                # builder task (the reviewer cannot be said independent of
                # work it does not gate) — the frozen DAG service lane.
                edge = await task_graph_service.bulk_add_edges(
                    sess,
                    task_id=tasks[1].id,
                    depends_on_task_ids=[tasks[0].id],
                    tenant_id=tenant_id,
                )
                assert edge.state == "added", f"the review->build REV-2 edge must be added: {edge}"
            await planning_run_dao.complete_run(run, new_status="PL_COMPLETED", db=sess)
    return run, wp, tasks, slots, revision


# ---------------------------------------------------------------------------
# The module seed — ONE committed tenant graph + every project the chain
# needs, mints its own rows under fresh revisions so committed facts never
# collide across the module (the proven plansvc seed pattern).
# ---------------------------------------------------------------------------
async def _seed(db_factory: async_sessionmaker) -> None:
    if _SEED.seeded:
        return
    from app.dao.base import tenant_context
    from app.dao.task_dao import task_provenance_dao
    from app.models.project import Project
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User

    async with db_factory() as sess, sess.begin():
        _SEED.tenant_a = Tenant(name="p4e2e", slug="p4e2e-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant_a)
        await sess.flush()
        _SEED.user_a = User(
            tenant_id=_SEED.tenant_a.id,
            display_name="p4e2e",
            role="member",
            is_active=True,
            email=f"p4e2e-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user_a)
        await sess.flush()
        grp = await _seed_agent_group(sess, tenant=_SEED.tenant_a, user=_SEED.user_a, tag="main")
        _SEED.model_a = grp["model"]
        _SEED.builder = grp["builder"]
        _SEED.reviewer = grp["reviewer"]
        _SEED.project_main = Project(
            name="p4e2e-main", status="EXECUTING", created_by=_SEED.user_a.id, tenant_id=_SEED.tenant_a.id
        )
        sess.add(_SEED.project_main)
        # The rework carrier project: hosts the rework Run's tasks, held in
        # a GATE milestone bucket so the rework WP is ordering-only (never
        # completion state) — the rework lane's artifact/evidence rows ride
        # on the REVIEWER task, whose execution is the reviewer agent's own.
        _SEED.project_rework = Project(
            name="p4e2e-rework", status="EXECUTING", created_by=_SEED.user_a.id, tenant_id=_SEED.tenant_a.id
        )
        sess.add(_SEED.project_rework)
        # The fail-closed negative lane: a gate-error project, executable.
        _SEED.project_gate = Project(
            name="p4e2e-gate", status="EXECUTING", created_by=_SEED.user_a.id, tenant_id=_SEED.tenant_a.id
        )
        sess.add(_SEED.project_gate)
        await sess.flush()
        # Root §5 proof: a non-CP_OK project whose in-scope task stays
        # PENDING with ZERO runs — completion is ledger-gated, Task status
        # is not an input.
        _SEED.project_orphan = Project(
            name="p4e2e-orphan", status="EXECUTING", created_by=_SEED.user_a.id, tenant_id=_SEED.tenant_a.id
        )
        sess.add(_SEED.project_orphan)
        # A second flagged project for the negative-assignment test.
        _SEED.project_negwp = Project(
            name="p4e2e-negwp", status="EXECUTING", created_by=_SEED.user_a.id, tenant_id=_SEED.tenant_a.id
        )
        sess.add(_SEED.project_negwp)
        for proj, title in (
            (_SEED.project_gate, "p4e2e gate task"),
            (_SEED.project_orphan, "p4e2e orphan task"),
        ):
            t = Task(
                agent_id=_SEED.builder.id,
                created_by=_SEED.user_a.id,
                tenant_id=_SEED.tenant_a.id,
                project_id=proj.id,
                title=title,
                type="todo",
                status="pending",
                priority="medium",
            )
            await task_provenance_dao.create_with_provenance(t, db=sess)
            if proj is _SEED.project_gate:
                _SEED.gate_task = t
            else:
                _SEED.orphan_task = t
        # Tenant B (the cross-tenant negative): its own user + agent +
        # project + in-scope task.
        _SEED.tenant_b = Tenant(name="p4e2e-b", slug="p4e2e-b-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant_b)
        await sess.flush()
        _SEED.user_b = User(
            tenant_id=_SEED.tenant_b.id,
            display_name="p4e2e-b",
            role="member",
            is_active=True,
            email=f"p4e2e-b-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user_b)
        await sess.flush()
        grp_b = await _seed_agent_group(sess, tenant=_SEED.tenant_b, user=_SEED.user_b, tag="b", agents=("builder",))
        _SEED.agent_b = grp_b["builder"]
        _SEED.project_b = Project(
            name="p4e2e-b-proj", status="EXECUTING", created_by=_SEED.user_b.id, tenant_id=_SEED.tenant_b.id
        )
        sess.add(_SEED.project_b)
        await sess.flush()
        _SEED.task_b = Task(
            agent_id=_SEED.agent_b.id,
            created_by=_SEED.user_b.id,
            tenant_id=_SEED.tenant_b.id,
            project_id=_SEED.project_b.id,
            title="p4e2e b task",
            type="todo",
            status="pending",
            priority="medium",
        )
        await task_provenance_dao.create_with_provenance(_SEED.task_b, db=sess)
    # The flagged WPs: one healthy multi-candidate package (the chain's
    # assignment target) + one single-candidate package (the negative).
    _run_main, wp, tasks, _slots, _rev = await _materialize_wp(
        db_factory,
        tenant=_SEED.tenant_a,
        user=_SEED.user_a,
        project=_SEED.project_main,
        agents=(_SEED.builder, _SEED.reviewer),
        wp_tag="main",
        requires_independent_review=True,
    )
    _SEED.wp_main = wp
    _SEED.build_task = tasks[0]
    _SEED.review_task = tasks[1]
    _run2, wp2, tasks2, _slots2, _rev2 = await _materialize_wp(
        db_factory,
        tenant=_SEED.tenant_a,
        user=_SEED.user_a,
        project=_SEED.project_negwp,
        agents=(_SEED.builder,),  # single-candidate roster: no disjoint reviewer
        wp_tag="neg",
        requires_independent_review=True,
    )
    _SEED.wp_neg = wp2
    _SEED.build2_task = tasks2[0]
    _SEED.review2_task = tasks2[1]
    # Rework carrier task (U-3: a done todo task is TERMINAL — rework mints a
    # NEW task on the rework carrier project, never re-executes build_task).
    # The rework Run's execution + the v2 artifact/evidence rows ride on this
    # task; the G2/G3 provenance facts still walk build_task's ledger.
    # NOTE: this runs on its OWN committed session block — the session from
    # the main seeding block above is closed by the time we reach here, and
    # issuing the DAO create on it lost the row to rollback (the t_ab1fb86a
    # seed/DB desync: project_rework existed but its carrier task did not).
    rework_carrier = Task(
        agent_id=_SEED.builder.id,
        created_by=_SEED.user_a.id,
        tenant_id=_SEED.tenant_a.id,
        project_id=_SEED.project_rework.id,
        title="p4e2e rework build task",
        type="todo",
        status="pending",
        priority="medium",
    )
    with tenant_context(_SEED.tenant_a.id):
        async with db_factory() as rework_sess, rework_sess.begin():
            await task_provenance_dao.create_with_provenance(rework_carrier, db=rework_sess)
    _SEED.rework_build_task = rework_carrier
    _SEED.seeded = True


@pytest.fixture(scope="module")
def _live_db() -> None:
    """Skip the whole live tier when no reachable Postgres is configured."""
    if not _probe_live_db():
        pytest.skip(_SKIP_MESSAGE)
    _require_alembic_chain()


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncGenerator[None, None]:
    # The app engine's pool is bound to the event loop that first used it;
    # pytest-asyncio builds a fresh loop per test, so dispose between tests.
    yield
    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="module")
def db_factory(_live_db: None) -> async_sessionmaker:
    """One session factory over the app engine for the module."""
    from app.database import engine

    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Deterministic-model-tier port injection (the PROBED Phase-3 seam): the
# real worker builders take the completion port as a keyword default;
# rebinding those defaults before build_runtime_worker_components() swaps
# ONLY the LLM port — the whole frozen Phase-2F spine around it is
# untouched (no second runtime).  gate_raise=True drives the Root-§5
# fail-closed proof (the gate call itself errors → the gate fails CLOSED).
# ---------------------------------------------------------------------------
def _make_deterministic_port(
    *,
    path: str = "P4_E2E_OUT.md",
    label: str = "build",
    gate_raise: bool = False,
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
            # The semantic TaskCompletionGate call (no tool list).
            calls.append("gate")
            if gate_raise:
                # The gate call itself errors: provider outage mid-gate —
                # the fail-closed fix (t_56fbca2e) must NOT pass the task.
                raise LLMError("HTTP 503: completion gate provider outage (deterministic)")
            verdict = {
                "verdict": "pass",
                "missing_requirements": [],
                "next_actions": [],
                "evidence": [f"deterministic gate pass ({label})"],
            }
            return LLMCompletionStep(
                content=json.dumps(verdict),
                tool_calls=(),
                reasoning_content=None,
                retry_instruction=None,
                usage=TokenUsage(total_tokens=1),
                finish_reason="stop",
            )
        calls.append("model")
        state["business_calls"] += 1
        if state["business_calls"] == 1:
            # The first business call writes the run's real file (the tool
            # path is REAL: write_file against the local storage backend).
            args = json.dumps({"path": path, "content": f"{label} deterministic tool-path proof\n"})
            return LLMCompletionStep(
                content="",
                tool_calls=(
                    {
                        "id": f"p4-call-{state['business_calls']}",
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
            content=f"{path} written. Task complete (deterministic {label}).",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
            finish_reason="stop",
        )

    return port, calls


def _rebind_ports(comps: Any, port: Any) -> None:
    """Point the REAL worker component builders at the deterministic port."""
    router = comps.driver._node_executor
    agent_exec = router._agent_executor
    agent_exec._model_service._completion = port
    agent_exec._verifier._completion_gate._completion = port


def _build_worker(saver: Any, claimant: str) -> Any:
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


async def _drive_run(run_id: uuid.UUID, port: Any, claimant_tag: str) -> str | None:
    """Drive one enqueued Run to a terminal event through the REAL spine."""
    from app.services.agent_runtime.checkpointer import create_checkpointer

    async with create_checkpointer(get_settings()) as saver:
        await saver.setup()
        comps = _build_worker(saver, f"p4-{claimant_tag}-{uuid.uuid4().hex[:8]}")
        _rebind_ports(comps, port)
        return await _wait_terminal(run_id, comps.worker)


async def _wait_terminal(run_id: uuid.UUID, worker: Any, budget_s: int = 120) -> str | None:
    """Poll the shared command inbox until ``run_id`` emits a terminal event
    (the Phase-3 recipe: the terminal read uses a FRESH session each pass,
    never the worker's in-flight one)."""
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
# DB-free tier (no Postgres): the pure ledger reads that prove G2 (an old
# APPROVE over a superseded set drops out of the current-valid set
# automatically) and G3 (the rework provenance walk is ledger-queryable).
# In-memory rows duck-type the frozen ledger models the pure core reads.
# ---------------------------------------------------------------------------
def test_db_free_stale_approve_drops_out_and_rework_provenance_walks() -> None:
    """[DB-free] G2 + G3: the current-valid review over the CURRENT set only
    (the stale APPROVE + the REQUEST_CHANGES fail row both cite the now
    superseded v1 set → both drop out; only the re-review over v2 is
    current-valid), and rework_provenance walks the stored chain
    (fail row → supersession pair → new current set → re-review row with
    payload.rework_of) from the two ledgers alone."""
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord

    t = uuid.uuid4()
    v1 = ArtifactRecord(id=uuid.uuid4(), tenant_id=t, seal_status="SEALED", superseded_by=uuid.uuid4())
    v1_id, v2_id = v1.id, v1.superseded_by
    v2 = ArtifactRecord(id=v2_id, tenant_id=t, seal_status="SEALED", superseded_by=None)
    fail_row = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=t,
        kind="review",
        outcome="fail",
        artifact_id=v1_id,
        subject_ref=f"evidence://review/{uuid.uuid4()}",
        payload={"required_changes": ["fix x"], "critiques": ["v1 defect"]},
        created_by_agent=uuid.uuid4(),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    stale_approve = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=t,
        kind="review",
        outcome="pass",
        artifact_id=v1_id,  # cites the NOW-SUPERSEDED v1 set
        subject_ref=f"evidence://review/{uuid.uuid4()}",
        payload={"verdict": "ok (historical)"},
        created_by_agent=uuid.uuid4(),
        created_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    re_review = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=t,
        kind="review",
        outcome="pass",
        artifact_id=v2_id,
        subject_ref=f"evidence://review/{uuid.uuid4()}",
        payload={"verdict": "reworked", "rework_of": str(fail_row.id)},
        created_by_agent=uuid.uuid4(),
        created_at=datetime(2026, 10, 3, tzinfo=UTC),
    )
    proof = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=t,
        kind="file_revision",
        outcome="pass",
        artifact_id=v2_id,
        subject_ref=f"file://{uuid.uuid4()}",
        payload={"tests_total": 1, "tests_passed": 1},
        created_by_agent=uuid.uuid4(),
        created_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    from app.services.review_rework_service import current_valid_review, rework_provenance

    reviews = [stale_approve, fail_row, re_review]
    # G2 live-shape: over the current set (v2), ONLY the re-review row
    # qualifies — the stale APPROVE (citing superseded v1) and the fail row
    # (citing superseded v1) drop out automatically, never clobbering.
    current_valid = current_valid_review([v1, v2], reviews)
    assert current_valid is not None and current_valid.id == re_review.id

    # G3: the whole Rework -> REQUEST_CHANGES chain walks from the ledgers.
    prov = rework_provenance(
        fail_review=fail_row,
        artifacts=[v1, v2],
        reviews=reviews,
        evidence=[proof],
    )
    assert prov.fail_review is fail_row
    assert prov.required_changes == ["fix x"]
    assert (v1_id, v2_id) in prov.superseded_pairs, "the G2 supersession link is a stored edge"
    assert prov.current_artifact_ids == (v2_id,), "the NEW set is the current set"
    assert prov.re_review is not None and prov.re_review.id == re_review.id
    assert prov.new_evidence == (proof,), "the rework's new proof evidence is stored"


# ---------------------------------------------------------------------------
# The continuous chain's committed state (each live test 2-11 runs over the
# rows the previous test COMMITTED — one continuous execution, not isolated
# unit checks; pytest file order is the chain's order).
# ---------------------------------------------------------------------------
class _CHAINState:
    # hop 4 (Runtime -> Run -> Artifact/Evidence, G4):
    build_run_id: Any  # the builder run #1 id
    x1: Any  # the builder run #1's write_file AgentToolExecution
    rev1_id: Any  # the run #1 WorkspaceFileRevision id
    v1: Any  # the run #1 artifact row (execution-linked, superseded after rework)
    tool_result_ev: Any  # the G4 execution-evidence row (kind=tool_result)
    # hop 4b (the disjoint reviewer's OWN Run -> its task's artifact/evidence):
    reviewer_run_id: Any
    x_review: Any  # the reviewer run's write_file AgentToolExecution
    v_reviewer: Any  # the reviewer-task's execution-linked artifact row
    user_review_row: Any  # the user-captured approving verdict on the reviewer task
    # hop 5 (Review -> Rework -> Re-review, G2/G3/F1):
    r1_pass: Any
    r2_fail: Any
    x2: Any  # the rework run's write_file AgentToolExecution
    v2: Any
    rework_proof_ev: Any
    r3_rereview: Any
    # hop 7 (Completion):
    c5_task: Any
    c5_first: Any
    c5_second: Any

    def __init__(self) -> None:
        self.build_run_id = None
        self.x1 = None
        self.rev1_id = None
        self.v1 = None
        self.tool_result_ev = None
        self.reviewer_run_id = None
        self.x_review = None
        self.v_reviewer = None
        self.user_review_row = None
        self.r1_pass = None
        self.r2_fail = None
        self.x2 = None
        self.v2 = None
        self.rework_proof_ev = None
        self.r3_rereview = None
        self.c5_task = None
        self.c5_first = None
        self.c5_second = None


_CHAIN = _CHAINState()

# The main WP's builder set for the review/completion lanes' G1 mirror:
# the builder is the only slot agent that PRODUCES deliverables.
BUILDERS: frozenset[uuid.UUID] = frozenset()


async def _tenant_ctx_factory(db_factory: async_sessionmaker) -> None:
    """Seed the module facts once (idempotent) — the chain's precondition."""
    global BUILDERS
    await _seed(db_factory)
    BUILDERS = frozenset({_SEED.builder.id})


# ---------------------------------------------------------------------------
# HOP 2 (Assignment): the assignment lane picks a BUILDER + a DISJOINT
# REVIEWER (invariant-13 holds at assignment time, REV-1 deterministic
# pick over the multi-candidate roster).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assignment_picks_builder_and_disjoint_reviewer(db_factory: async_sessionmaker) -> None:
    """[DB-only + mock agent] Hop Task→Assignment: the REAL assignment lane
    (assignment_service.apply_assignment, the REV-1 deterministic solver)
    binds the build slot to the builder and the review slot to the
    DISJOINT reviewer on one flagged work package; the single-assignment
    fact is Task.agent_id (the lane's only write)."""
    await _tenant_ctx_factory(db_factory)
    from app.dao.base import tenant_context
    from app.models.task import Task
    from app.services.assignment_service import assignment_service

    tenant = _SEED.tenant_a.id
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=_SEED.wp_main.id, current_user=_SEED.user_a
            )
    assert outcome.state == "assigned", f"{outcome.state}: {outcome.code} {outcome.detail}"
    assert not outcome.constraint_report.get("fail_closed_codes"), outcome.constraint_report

    # The single assignment fact per slot: builder on the build task, the
    # DISJOINT reviewer on the review task (invariant-13 at assignment time).
    with tenant_context(tenant):
        async with db_factory() as sess:
            build_row = (await sess.execute(select(Task).where(Task.id == _SEED.build_task.id))).scalar_one()
            review_row = (await sess.execute(select(Task).where(Task.id == _SEED.review_task.id))).scalar_one()
            assert build_row.agent_id == _SEED.builder.id, "the build slot binds the builder"
            assert review_row.agent_id == _SEED.reviewer.id, "the review slot binds the disjoint reviewer"
            assert review_row.agent_id != build_row.agent_id, "REV-1: builder and reviewer are disjoint"


@pytest.mark.asyncio
async def test_assignment_review_independence_refuses_single_candidate_wp(db_factory: async_sessionmaker) -> None:
    """[DB-only + mock agent] Negative (invariant-13 at assignment time):
    a flagged package whose reviewer roster is the BUILDER's alone has no
    disjoint reviewer → PL_REVIEWER_NOT_INDEPENDENT BEFORE any write
    (the fail-closed assignment gate)."""
    await _tenant_ctx_factory(db_factory)
    from app.dao.base import tenant_context
    from app.models.task import Task
    from app.services.assignment_service import PL_REVIEWER_NOT_INDEPENDENT, assignment_service

    tenant = _SEED.tenant_a.id
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=_SEED.wp_neg.id, current_user=_SEED.user_a
            )
    assert outcome.state == "failed"
    assert outcome.code == PL_REVIEWER_NOT_INDEPENDENT, (outcome.code, outcome.detail)

    # Fail-closed before any write: both tasks keep the planner's default
    # binding (the single candidate), no fact was flipped.
    with tenant_context(tenant):
        async with db_factory() as sess:
            for row in (
                (await sess.execute(select(Task).where(Task.id == _SEED.build2_task.id))).scalar_one(),
                (await sess.execute(select(Task).where(Task.id == _SEED.review2_task.id))).scalar_one(),
            ):
                assert row.agent_id == _SEED.builder.id, "the single-candidate roster leaves the planner default"


# ---------------------------------------------------------------------------
# HOP 3+4 (Runtime -> Run -> Artifact/Evidence + file revisions, G4): the
# REAL Phase-2F worker spine drives the builder's Run; the tool path is
# REAL (write_file against the local storage backend); the ledger lane then
# registers the execution-linked artifact + execution evidence (guarantee
# #1).  DB-only + mock agent: the deterministic port is the only model.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_runtime_run_produces_artifact_and_evidence(db_factory: async_sessionmaker) -> None:
    """[DB-only + mock agent] Hops Runtime→Run→Artifact/Evidence: the
    frozen Phase-2F spine (build_runtime_worker_components + the shared
    command inbox) settles the builder's Run; the REAL write_file tool node
    persists an AgentToolExecution + a WorkspaceFileRevision; the review
    lane then registers the G4 execution-linked artifact (execution_id =
    the tool execution's id) + the G4 execution-evidence row.  The
    deterministic port's call log proves NO real LLM was involved."""
    await _tenant_ctx_factory(db_factory)
    from app.dao.artifact_evidence_dao import artifact_record_dao
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.agent_tool_execution import AgentToolExecution
    from app.models.artifact_evidence import ArtifactRecord
    from app.models.task import Task
    from app.models.user import User
    from app.models.workspace import WorkspaceFileRevision
    from app.services.review_rework_service import ReviewReworkService
    from app.services.task_execution_service import task_execution_service

    tenant = _SEED.tenant_a.id
    port, calls = _make_deterministic_port(path="P4_E2E_OUT.md", label="build")

    # Enqueue through the REAL Phase-2E gate (the frozen execution lane).
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            task_row = (await sess.execute(select(Task).where(Task.id == _SEED.build_task.id))).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.builder.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user_a.id))).scalar_one()
            out = await task_execution_service.execute(sess, task=task_row, agent=agent_row, current_user=user_row)
    assert out.run_id is not None, f"the gate must enqueue: {out.state} {out.derived_state}"
    _CHAIN.build_run_id = out.run_id

    # Drive the REAL spine to a terminal event (the deterministic port is
    # the only model step — the whole Phase-2F spine is untouched).
    terminal = await _drive_run(out.run_id, port, "build")
    assert terminal == "run_completed", f"builder run did not complete: {terminal}"
    assert "model" in calls and "gate" in calls, "the deterministic port was the model (no real LLM)"

    # Settlement: the real handler flipped the task to done.
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (await sess.execute(select(Task.status).where(Task.id == _SEED.build_task.id))).scalar_one() == "done"

    # The REAL tool path: one succeeded write_file execution + its revision.
    with tenant_context(tenant):
        async with db_factory() as sess:
            x1_rows = (
                await sess.execute(
                    select(AgentToolExecution)
                    .where(
                        AgentToolExecution.run_id == out.run_id,
                        AgentToolExecution.tool_name == "write_file",
                        AgentToolExecution.status == "succeeded",
                    )
                    .order_by(AgentToolExecution.started_at)
                )
            ).scalars().all()
            assert len(x1_rows) == 1, "the run's single write_file tool execution"
            _CHAIN.x1 = x1_rows[0]
            # Real spine semantics: a SMALL write_file result settles INLINE
            # (no blob archive → result_ref None, archive_status "inline");
            # only a result_summary over AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES
            # is archived under tool-result://<execution_id>. The G4
            # execution-link fact is the execution id itself (asserted below).
            _x1_meta = _CHAIN.x1.result_metadata or {}
            if _CHAIN.x1.result_ref is not None:
                assert _CHAIN.x1.result_ref.startswith("tool-result://")
            else:
                assert _x1_meta.get("archive_status") == "inline", _x1_meta
            rev_rows = (
                await sess.execute(
                    select(WorkspaceFileRevision)
                    .where(
                        WorkspaceFileRevision.scope_type == "agent",
                        WorkspaceFileRevision.scope_id == _SEED.builder.id,
                        WorkspaceFileRevision.path == "P4_E2E_OUT.md",
                    )
                    .order_by(WorkspaceFileRevision.created_at.desc())
                    .limit(1)
                )
            ).scalars().all()
            assert rev_rows, "the real tool path persisted a file revision"
            _CHAIN.rev1_id = rev_rows[0].id

    # The ledger lane (guarantee #1): the execution-linked artifact + the
    # G4 execution-evidence row, in one transaction.
    import hashlib

    content_hash = hashlib.sha256(b"build deterministic tool-path proof\n").hexdigest()
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            x1 = (await sess.execute(select(AgentToolExecution).where(AgentToolExecution.id == _CHAIN.x1.id))).scalar_one()
            v1 = ArtifactRecord(
                task_id=_SEED.build_task.id,
                project_id=_SEED.project_main.id,
                execution_id=x1.id,  # G4: the execution link (D5 XOR: the agent path)
                agent_id=_SEED.builder.id,
                type="file",
                title="P4_E2E_OUT.md",
                storage_scheme="workspace_path",
                storage_ref="P4_E2E_OUT.md",
                content_hash=content_hash,
                revision_ref=str(_CHAIN.rev1_id),
                tenant_id=tenant,
            )
            written_v1 = await artifact_record_dao.add_artifact(v1, tenant_id=tenant, db=sess)
            assert written_v1.seal_status == "DRAFT"
            _CHAIN.v1 = written_v1
            svc = ReviewReworkService()
            evs = await svc.record_execution_evidence(
                sess,
                tenant_id=tenant,
                execution=x1,
                task_id=_SEED.build_task.id,
                project_id=_SEED.project_main.id,
                kind="tool_result",
            )
            assert len(evs) == 1 and evs[0].execution_id == x1.id and evs[0].outcome == "pass"
            _CHAIN.tool_result_ev = evs[0]

    # Re-read committed: the G4 link is durable (execution -> artifact).
    with tenant_context(tenant):
        async with db_factory() as sess:
            v1_row = (await sess.execute(select(ArtifactRecord).where(ArtifactRecord.id == _CHAIN.v1.id))).scalar_one()
            assert v1_row.execution_id == _CHAIN.x1.id, "G4: the artifact is execution-linked"
            assert v1_row.superseded_by is None


# ---------------------------------------------------------------------------
# HOP 5 (Review -> Rework -> Re-review, G2/G3/F1): the DISJOINT reviewer's
# kind='review' verdicts; the REQUEST_CHANGES -> rework (NEW rows +
# superseded_by links, NO builder verdict) -> disjoint re-review chain.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_review_rework_rereview_chain(db_factory: async_sessionmaker) -> None:
    """[DB-only + mock agent] Hops Review→Rework→Re-review: the disjoint
    reviewer writes kind='review' verdicts (invariant-13 held at verdict
    time); a REQUEST_CHANGES (fail + critiques/required_changes) drives
    the builder's rework (NEW v2 artifact + file_revision proof + the
    superseded_by link, G2: the old rows go historical, never mutated,
    and the F1 boundary: NO builder-authored kind='review' row) and the
    disjoint reviewer's re-review (record_review(..., rework_of=fail.id),
    G3: payload.rework_of makes the provenance walkable)."""
    await _tenant_ctx_factory(db_factory)
    from app.dao.artifact_evidence_dao import artifact_record_dao
    from app.dao.base import tenant_context
    from app.models.artifact_evidence import ArtifactRecord
    from app.services.review_rework_service import RV_OK, ReviewReworkService

    tenant = _SEED.tenant_a.id
    svc = ReviewReworkService()
    builders = BUILDERS
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            current = await artifact_record_dao.list_by_task(_SEED.build_task.id, db=sess, current_only=True)
            assert [a.id for a in current] == [_CHAIN.v1.id]

            # R1 — the disjoint reviewer's first verdict: an early APPROVE
            # over v1 (seals it one-way, R3).  This pass row cites v1,
            # which becomes historical on rework — provenance of the
            # stale-set drop-out (G2).
            r1 = await svc.record_review(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                outcome="pass",
                reviewer_agent_id=_SEED.reviewer.id,
                reviewer_user_id=None,
                builder_agent_ids=builders,
                current_artifacts=current,
                cited_artifact_ids=[_CHAIN.v1.id],
                verdict="v1 acceptable — approving",
            )
            assert r1.code == RV_OK, r1
            _CHAIN.r1_pass = r1.evidence
            assert r1.sealed_artifact_ids == (_CHAIN.v1.id,), "APPROVE seals the current set one-way (R3)"

            # R2 — the REQUEST_CHANGES verdict (outcome=fail + the payload
            # the card requires: critiques + required_changes).
            r2 = await svc.record_review(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                outcome="fail",
                reviewer_agent_id=_SEED.reviewer.id,
                reviewer_user_id=None,
                builder_agent_ids=builders,
                current_artifacts=current,
                cited_artifact_ids=[_CHAIN.v1.id],
                verdict="v1 has the §7 defect — request changes",
                critiques=["missing the bounded read", "rework must add new proof evidence (I-4)"],
                required_changes=["register a new v2 revision + a file_revision proof"],
            )
            assert r2.code == RV_OK, r2
            _CHAIN.r2_fail = r2.evidence
            assert r2.evidence.outcome == "fail"
            assert r2.evidence.payload.get("required_changes"), "the fail verdict carries required_changes"
            assert r2.evidence.payload.get("critiques"), "the fail verdict carries critiques"

            # F1 + invariant-13 at verdict time: the BUILDER's own verdict
            # over the CURRENT set is rejected by the lane's guard (the
            # disjoint reviewer is the only verdict author).
            rogue_res = await svc.record_review(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                outcome="pass",
                reviewer_agent_id=_SEED.builder.id,
                reviewer_user_id=None,
                builder_agent_ids=builders,
                current_artifacts=current,
                cited_artifact_ids=[_CHAIN.v1.id],
                verdict="self-review must be rejected",
            )
            assert rogue_res.code == "RV_REVIEW_NOT_INDEPENDENT", rogue_res.code

            # I-4 negative: a rework with NO new proof evidence fails closed
            # (the plan guard, before any write — the card's "new artifact
            # + evidence rows" requirement).
            bare = ArtifactRecord(
                task_id=_SEED.build_task.id,
                execution_id=_CHAIN.x1.id,
                type="file",
                title="no-proof row",
                storage_scheme="workspace_path",
                storage_ref=f"P4_E2E_NO_PROOF_{uuid.uuid4().hex[:8]}.md",
                content_hash="0" * 64,
                tenant_id=tenant,
            )
            no_proof = await svc.record_rework(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                builder_agent_id=_SEED.builder.id,
                fail_review=_CHAIN.r2_fail,
                old_artifacts=current,
                new_artifacts=[bare],
                new_evidence=[],
            )
            assert no_proof.code == "RV_INVALID_INPUT", "a rework without new proof evidence fails closed (I-4)"


# ---------------------------------------------------------------------------
# HOP 5b (Rework Run -> NEW rows + superseded_by -> disjoint re-review):
# the builder's REWORK is a second real Run on the same frozen spine; its
# write_file execution mints the NEW v2 artifact + the I-4 proof evidence +
# the G2 superseded_by link — and the re-review verdict is the DISJOINT
# reviewer's separate act (payload.rework_of, G3).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rework_run_and_disjoint_rereview(db_factory: async_sessionmaker) -> None:
    """[DB-only + mock agent] Hop Rework→Re-review: a second real Run on
    the builder spine produces the rework's write_file execution; the
    ledger lane registers the NEW v2 artifact + the file_revision proof +
    the superseded_by link (G2: old rows historical, never mutated); the
    disjoint reviewer's re-review (record_review(rework_of=fail.id)) seals
    the NEW set and carries payload.rework_of (G3 walkable provenance).
    The rework writes NO kind='review' row (F1) — the only verdict author
    is the disjoint reviewer."""
    await _tenant_ctx_factory(db_factory)
    from app.dao.artifact_evidence_dao import artifact_record_dao, evidence_record_dao
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.agent_tool_execution import AgentToolExecution
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord
    from app.models.task import Task
    from app.models.user import User
    from app.services.review_rework_service import RV_OK, ReviewReworkService
    from app.services.task_execution_service import task_execution_service

    tenant = _SEED.tenant_a.id
    svc = ReviewReworkService()
    builders = BUILDERS
    port, calls = _make_deterministic_port(path="P4_E2E_OUT.md", label="rework")

    # The builder's rework: U-3 says a DONE todo task is terminal (never
    # re-Execute'd), so the rework mints a NEW task on the rework carrier
    # project (the seed's rework_build_task) — one continuous execution: the
    # rework rides the SAME frozen spine as run #1.
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            task_row = (
                await sess.execute(select(Task).where(Task.id == _SEED.rework_build_task.id))
            ).scalar_one()
            agent_row = (await sess.execute(select(Agent).where(Agent.id == _SEED.builder.id))).scalar_one()
            user_row = (await sess.execute(select(User).where(User.id == _SEED.user_a.id))).scalar_one()
            out2 = await task_execution_service.execute(sess, task=task_row, agent=agent_row, current_user=user_row)
    assert out2.run_id is not None and out2.run_id != _CHAIN.build_run_id, "the rework run is a distinct Run"
    terminal = await _drive_run(out2.run_id, port, "rework")
    assert terminal == "run_completed", f"rework run did not complete: {terminal}"
    assert "model" in calls, "the deterministic port drove the rework (no real LLM)"
    with tenant_context(tenant):
        async with db_factory() as sess:
            assert (
                await sess.execute(select(Task.status).where(Task.id == _SEED.rework_build_task.id))
            ).scalar_one() == "done", "the rework run's settlement flipped the carrier task to done"
            x2 = (
                await sess.execute(
                    select(AgentToolExecution)
                    .where(
                        AgentToolExecution.run_id == out2.run_id,
                        AgentToolExecution.tool_name == "write_file",
                        AgentToolExecution.status == "succeeded",
                    )
                )
            ).scalars().all()
            assert len(x2) == 1 and x2[0].id != _CHAIN.x1.id, "the rework's distinct write_file execution"
            _CHAIN.x2 = x2[0]

    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            x2 = (
                await sess.execute(select(AgentToolExecution).where(AgentToolExecution.id == _CHAIN.x2.id))
            ).scalar_one()
            current = await artifact_record_dao.list_by_task(_SEED.build_task.id, db=sess, current_only=True)
            assert [a.id for a in current] == [_CHAIN.v1.id]
            # Explicit id: SQLAlchemy would otherwise assign v2's UUID only at
            # INSERT time (default=uuid4), so the proof row below would cite
            # artifact_id=None (v2.id is still None at construction) and the
            # G3 walk's `proof.artifact_id in the current set` filter could
            # never match.  Minting the id up front keeps both rows carrying a
            # real, stable UUID from construction (the DB-free reference test
            # _artifact helper does the same).
            v2 = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=_SEED.build_task.id,
                project_id=_SEED.project_main.id,
                execution_id=x2.id,
                agent_id=_SEED.builder.id,
                type="file",
                title="P4_E2E_OUT.md (reworked)",
                storage_scheme="workspace_path",
                storage_ref="P4_E2E_OUT_v2.md",
                content_hash="f" * 64,
                revision_ref="v2",
                tenant_id=tenant,
            )
            proof = EvidenceRecord(
                task_id=_SEED.build_task.id,
                execution_id=x2.id,
                kind="file_revision",
                outcome="pass",
                subject_ref=f"file://{_SEED.build_task.id}/v2",
                payload={"tests_total": 2, "tests_passed": 2},
                created_by_agent=_SEED.builder.id,
                # The G3 stored-edge walk (rework_provenance) filters the
                # rework's new proof by `artifact_id in the current set` —
                # the proof MUST cite the NEW v2 artifact (the DB-free
                # reference test test_rework_provenance_walks_the_stored_chain
                # sets artifact_id=new.id on its new_proof row).
                artifact_id=v2.id,
                tenant_id=tenant,
            )
            rework = await svc.record_rework(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                builder_agent_id=_SEED.builder.id,
                fail_review=_CHAIN.r2_fail,
                old_artifacts=current,
                new_artifacts=[v2],
                new_evidence=[proof],
            )
            assert rework.code == RV_OK, rework.detail
            assert v2.id in rework.new_artifact_ids
            assert proof.id in rework.new_evidence_ids
            # G2: link the old row to the new current row (one stored edge).
            old_row = (await sess.execute(select(ArtifactRecord).where(ArtifactRecord.id == _CHAIN.v1.id))).scalar_one()
            await artifact_record_dao.supersede(old_row, new_id=v2.id, db=sess)
            _CHAIN.v2 = v2
            _CHAIN.rework_proof_ev = proof

            # R3 — the disjoint reviewer's RE-REVIEW (the rework_of link).
            current2 = await artifact_record_dao.list_by_task(_SEED.build_task.id, db=sess, current_only=True)
            assert [a.id for a in current2] == [v2.id], "the NEW set is the current set"
            r3 = await svc.record_review(
                sess,
                task_id=_SEED.build_task.id,
                tenant_id=tenant,
                outcome="pass",
                reviewer_agent_id=_SEED.reviewer.id,
                reviewer_user_id=None,
                builder_agent_ids=builders,
                current_artifacts=current2,
                cited_artifact_ids=[v2.id],
                verdict="v2 rework accepted",
                rework_of=_CHAIN.r2_fail.id,
            )
            assert r3.code == RV_OK, r3
            _CHAIN.r3_rereview = r3.evidence
            assert r3.evidence.payload.get("rework_of") == str(_CHAIN.r2_fail.id), "G3: the re-review carries rework_of"
            assert r3.sealed_artifact_ids == (v2.id,), "the re-review seals the NEW set (R3)"

    # Committed re-reads: F1 + G2 + G3 against the real ledger.
    with tenant_context(tenant):
        async with db_factory() as sess:
            verdicts = await evidence_record_dao.list_reviews_for_task(_SEED.build_task.id, db=sess)
            assert all(v.created_by_agent == _SEED.reviewer.id for v in verdicts), (
                "F1: every kind='review' row is the disjoint reviewer's — zero builder-authored"
            )
            assert len(verdicts) == 3, "pass(v1) + fail(v1) + pass(v2, rework_of)"
            v1_row = (await sess.execute(select(ArtifactRecord).where(ArtifactRecord.id == _CHAIN.v1.id))).scalar_one()
            assert v1_row.superseded_by == _CHAIN.v2.id, "G2: the old row is historical via superseded_by"
            assert v1_row.seal_status == "SEALED", "the old row keeps its seal state (never mutated)"
            cwr = await svc.current_valid_review(sess, task_id=_SEED.build_task.id)
            assert cwr is not None and cwr.id == _CHAIN.r3_rereview.id, "G2: the current-valid review is the re-review"
            prov = await svc.rework_provenance_for_task(
                sess, task_id=_SEED.build_task.id, fail_review_id=_CHAIN.r2_fail.id
            )
            assert prov is not None
            assert (_CHAIN.v1.id, _CHAIN.v2.id) in prov.superseded_pairs, "G3: the supersession link walks"
            assert prov.re_review is not None and prov.re_review.id == _CHAIN.r3_rereview.id, "G3: the re-review row walks"
            assert prov.new_evidence, "G3: the rework's proof evidence walks"


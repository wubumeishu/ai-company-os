"""Phase 3 Planning service — service-layer unit tests.

Per docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md §3/§4/§6 the
``PlanningService`` is the owned lane that turns a Project + a completed
Analysis run into a durable ProjectPlan (the ``PlanningRun`` revision) and
materializes it into the EXISTING Task Graph (frozen ``task_dependencies``,
via ``task_graph_service``) without forking a second graph model.  Two tiers,
mirroring the t_650ddd87 / t_b342df97 test split:

- **DB-free blueprint tier** (always run): the pure
  ``build_plan_blueprint`` core — goal / package / milestone / slot / edge
  derivation, the 1:1 finding -> goal provenance binding, the SECURITY
  review-slot rule, the serial-group inter-edge chain, the closed-set
  discipline, the 500-char title bound, the oversized-input fail-closed
  bound, and the deterministic ``plan_sha256`` content hash.

- **live-schema tier** (skipped when no reachable Postgres): the
  ``create_plan`` transactional write against the real f071 schema — the
  plan references existing Task + Analysis entities, materializes pending
  ``ANALYSIS_PLANNING`` Task rows (finding_id NULL, the full 2D provenance
  set), wires DAG edges through the frozen graph service, dedups a
  same-revision re-call as ``PL_PLAN_EXISTS``, never auto-enqueues a Run
  (G4), and the provenance chain
  AnalysisFinding -> PlanningGoal -> WorkPackage -> Task is queryable.

Running the live tier::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch> \
        uv run --extra dev pytest tests/test_planning_service.py
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.agent import Agent
from app.models.analysis import AnalysisFinding, AnalysisRun
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.user import User
from app.services.planning_service import (
    MAX_GOALS_PER_PLAN,
    PlanBlueprint,
    PlanningError,
    build_plan_blueprint,
    planning_service,
    replace_plan_sha,
)

# ---------------------------------------------------------------------------
# Helpers — in-memory model instances for the DB-free tier.
# ---------------------------------------------------------------------------

RUN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
AGENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
TENANT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
REV_A = "a" * 64


def _run(status: str = "AN_COMPLETED") -> AnalysisRun:
    return AnalysisRun(
        id=RUN_ID,
        project_id=PROJECT_ID,
        revision_sha=REV_A,
        status=status,
        tenant_id=TENANT_ID,
    )


def _finding(fid: str, category: str = "TECH_DEBT", severity: str = "WARN", tag: str = "FACT") -> AnalysisFinding:
    return AnalysisFinding(
        id=uuid.uuid5(uuid.NAMESPACE_URL, fid),
        analysis_run_id=RUN_ID,
        severity=severity,
        category=category,
        tag=tag,
        summary=f"{category.lower()}-{fid} summary",
        evidence={"anchors": [f"{fid}.py:1"]},
        tenant_id=TENANT_ID,
    )


def _project(status: str = "ANALYZING") -> Project:
    return Project(id=PROJECT_ID, name="plan-proj", status=status, created_by=USER_ID, tenant_id=TENANT_ID)


def _agent() -> Agent:
    return Agent(id=AGENT_ID, name="plan-agent", creator_id=USER_ID, tenant_id=TENANT_ID)


# ---------------------------------------------------------------------------
# DB-FREE BLUEPRINT TIER — the pure derivation core.
# ---------------------------------------------------------------------------


def test_blueprint_one_goal_per_finding_1to1_provenance() -> None:
    """(#5) the plan binds every goal to EXACTLY its own finding — a 1:1,
    queryable provenance chain, one goal per finding."""
    f1, f2 = _finding("f1"), _finding("f2", category="RISK")
    bp = build_plan_blueprint(run=_run(), findings=[f1, f2], project=_project(), agent=_agent())
    assert len(bp.goals) == 2
    assert bp.goals[0].analysis_finding_ids == (f1.id,)
    assert bp.goals[1].analysis_finding_ids == (f2.id,)
    # One work package per goal; every package points at its goal index.
    assert [p.goal_index for p in bp.packages] == [0, 1]


def test_blueprint_default_serial_delivery_group() -> None:
    """(#3/#4) the default shape is one delivery milestone bucketing all
    packages, with the packages wired serially: package i's head task depends
    on package i-1's head task (milestone / parallel-serial-group wiring)."""
    bp = build_plan_blueprint(
        run=_run(),
        findings=[_finding("f1"), _finding("f2"), _finding("f3")],
        project=_project(),
        agent=_agent(),
    )
    assert len(bp.milestones) == 1
    assert bp.milestones[0].kind == "delivery" and bp.milestones[0].seq == 0
    # Two inter-package serial edges: (1,0) and (2,1) — head-task chain.
    assert bp.inter_edges == ((1, 0), (2, 1)), bp.inter_edges
    assert len(bp.packages) == 3
    for p in bp.packages:
        assert p.execution_mode == "serial"


def test_blueprint_single_finding_has_no_inter_edges() -> None:
    bp = build_plan_blueprint(
        run=_run(), findings=[_finding("f1")], project=_project(), agent=_agent()
    )
    assert bp.inter_edges == ()
    # A single non-SECURITY finding -> one build slot, no intra edges.
    assert [s.slot for s in bp.packages[0].slots] == ["build"]
    assert bp.packages[0].intra_edges == ()


def test_blueprint_no_findings_yields_empty_plan() -> None:
    """A completed run with no findings still produces a valid (empty) plan —
    data, not an error: no goals, no packages, no milestone."""
    bp = build_plan_blueprint(run=_run(), findings=[], project=_project(), agent=_agent())
    assert bp.goals == () and bp.packages == () and bp.milestones == () and bp.inter_edges == ()


def test_blueprint_security_finding_adds_review_slot_and_intra_edge() -> None:
    """(#2/#4) a SECURITY finding's package gets an independent review slot
    that depends on its build slot (parent-child DAG edge), and the package
    declares requires_independent_review (design P11)."""
    bp = build_plan_blueprint(
        run=_run(),
        findings=[_finding("sec", category="SECURITY", severity="CRITICAL")],
        project=_project(),
        agent=_agent(),
    )
    pkg = bp.packages[0]
    assert [s.slot for s in pkg.slots] == ["build", "review"]
    assert pkg.intra_edges == (("review", "build"),)
    assert pkg.requires_independent_review is True
    # The review slot's closed capability input carries "review".
    review = next(s for s in pkg.slots if s.slot == "review")
    assert review.kind == "review" and review.required_capabilities == ("review",)
    assert review.is_head is False and next(s for s in pkg.slots if s.slot == "build").is_head is True


def test_blueprint_technical_debt_has_no_review_slot() -> None:
    bp = build_plan_blueprint(
        run=_run(), findings=[_finding("d", category="TECH_DEBT")], project=_project(), agent=_agent()
    )
    pkg = bp.packages[0]
    assert [s.slot for s in pkg.slots] == ["build"]
    assert pkg.requires_independent_review is False
    # TECH_DEBT capability input is a closed WORK_CAPABILITIES bundle.
    assert pkg.slots[0].required_capabilities == ("backend", "code")


def test_blueprint_closed_kind_and_capability_sets() -> None:
    """Every blueprint value the DAO write path validates against the closed
    sets in app/models/planning.py — no out-of-set value can reach a write."""
    from app.models.planning import (
        MILESTONE_KINDS,
        TASK_SCOPE_SLOT_KINDS,
        WORK_CAPABILITIES,
        WORK_PACKAGE_EXECUTION_MODES,
    )

    findings = [_finding(str(i), category=c) for i, c in enumerate(
        ("SECURITY", "TECH_DEBT", "RISK", "OPEN_QUESTION", "FACT")
    )]
    bp = build_plan_blueprint(
        run=_run(), findings=findings, project=_project(), agent=_agent(), execution_mode="parallel"
    )
    assert "parallel" in WORK_PACKAGE_EXECUTION_MODES
    assert bp.milestones[0].kind in MILESTONE_KINDS
    for p in bp.packages:
        assert p.execution_mode in WORK_PACKAGE_EXECUTION_MODES
        for s in p.slots:
            assert s.kind in TASK_SCOPE_SLOT_KINDS
            assert all(c in WORK_CAPABILITIES for c in s.required_capabilities)
            for g in bp.goals:
                assert all(c in WORK_CAPABILITIES for c in g.required_capabilities)


def test_blueprint_milestone_kind_overridable() -> None:
    bp = build_plan_blueprint(
        run=_run(),
        findings=[_finding("f1")],
        project=_project(),
        agent=_agent(),
        milestone_kind="gate",
        milestone_title="Gate A",
    )
    assert bp.milestones[0].kind == "gate" and bp.milestones[0].title == "Gate A"


def test_blueprint_title_truncation_boundary() -> None:
    """The materialized task title never exceeds the 500-char column width."""
    big = _finding("big")
    big.summary = "x" * 600
    bp = build_plan_blueprint(run=_run(), findings=[big], project=_project(), agent=_agent())
    title = bp.packages[0].slots[0].title
    assert len(title) == 500
    assert title == f"[TECH_DEBT] {'x' * 600}"[:500]


def test_blueprint_oversized_findings_fail_closed() -> None:
    """G5: a findings set above the plan bound refuses the whole plan."""
    findings = [_finding(f"f-{i}") for i in range(MAX_GOALS_PER_PLAN + 1)]
    with pytest.raises(PlanningError) as exc:
        build_plan_blueprint(run=_run(), findings=findings, project=_project(), agent=_agent())
    assert exc.value.code == "PL_INVALID_INPUT"


def test_blueprint_payload_and_sha_deterministic() -> None:
    """plan_sha256 is the content hash of the emitted (canonical) payload:
    same input -> same digest, different input -> different digest."""
    def _mk() -> PlanBlueprint:
        return build_plan_blueprint(
            run=_run(),
            findings=[_finding("f1"), _finding("f2")],
            project=_project(),
            agent=_agent(),
        )

    bp1, bp2 = _mk(), _mk()
    assert bp1.plan_sha256 == bp2.plan_sha256
    assert len(bp1.plan_sha256) == 64
    payload = bp1.payload()
    assert payload["goals"] == [
        {"title": g.title, "required_capabilities": list(g.required_capabilities),
         "analysis_finding_ids": [str(f) for f in g.analysis_finding_ids]}
        for g in bp1.goals
    ]
    # The sha is stamped from the payload via replace_plan_sha (stable).
    assert replace_plan_sha(bp1).plan_sha256 == bp1.plan_sha256
    different = build_plan_blueprint(
        run=_run(), findings=[_finding("f1")], project=_project(), agent=_agent()
    )
    assert different.plan_sha256 != bp1.plan_sha256


def test_blueprint_is_database_free_shape() -> None:
    """The blueprint is a plain value object: the ProjectPlan/translator input
    carries no ORM/session state (the pure half of the two-halves split)."""
    bp = build_plan_blueprint(
        run=_run(), findings=[_finding("f1")], project=_project(), agent=_agent()
    )
    assert isinstance(bp, PlanBlueprint)
    assert all(g.analysis_finding_ids for g in bp.goals)
    assert planning_service.build_blueprint(
        run=_run(), findings=[_finding("f1")], project=_project(), agent=_agent()
    ).plan_sha256 == bp.plan_sha256


# ---------------------------------------------------------------------------
# LIVE-SCHEMA TIER — the transactional write + Task Graph wiring (Postgres).
# ---------------------------------------------------------------------------

LIVE = pytest.mark.usefixtures("_live_db")


class _Seed:
    """Committed tenant graph, created once per pytest process (idempotent)."""

    def __init__(self) -> None:
        self.tenant = None
        self.user = None
        self.agent = None
        self.project = None
        self.seeded = False


_SEED = _Seed()


@pytest.fixture
def _live_db() -> None:
    """Skip the live tier when no reachable Postgres is configured."""
    import asyncio

    import asyncpg

    from app.config import get_settings

    if getattr(_live_db, "_result", None) is None:  # type: ignore[attr-defined]
        async def _probe() -> bool:
            dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
            try:
                conn = await asyncpg.connect(dsn)
                await conn.execute("select 1")
                await conn.close()
                return True
            except Exception:  # noqa: BLE001 - no DB / creds / schema all mean "skip"
                return False

        loop = asyncio.new_event_loop()
        try:
            _live_db._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    if not _live_db._result:  # type: ignore[attr-defined]
        pytest.skip(
            "no reachable Postgres for the planning service live tier; "
            "the DB-free blueprint tier carries the derivation logic"
        )


@pytest.fixture
async def db(_live_db) -> async_sessionmaker:
    from app.config import get_settings

    engine = create_async_engine(get_settings().DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(db_factory) -> None:
    """Seed one committed tenant graph + an ANALYZING project (idempotent).

    Each live test mints its OWN AN_COMPLETED run + findings under a fresh
    revision so committed rows never collide across the suite (re-runnable).
    """
    if _SEED.seeded:
        return
    from app.models.llm import LLMModel

    async with db_factory() as sess, sess.begin():
        _SEED.tenant = Tenant(name="plansvc", slug="plansvc-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant)
        await sess.flush()
        _SEED.user = User(
            tenant_id=_SEED.tenant.id,
            display_name="plansvc",
            role="member",
            is_active=True,
            email=f"plansvc-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user)
        await sess.flush()
        model = LLMModel(
            tenant_id=_SEED.tenant.id,
            provider="anthropic",
            model="claude-opus-4-6",
            api_key_encrypted="enc-test",
            label="plansvc-model",
        )
        sess.add(model)
        await sess.flush()
        _SEED.agent = Agent(
            name="plansvc-agent",
            creator_id=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
            access_mode="company",
            status="running",
            primary_model_id=model.id,
        )
        sess.add(_SEED.agent)
        await sess.flush()
        _SEED.project = Project(
            name="plansvc-proj",
            status="ANALYZING",
            created_by=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
        )
        sess.add(_SEED.project)
        await sess.flush()
        for obj in (_SEED.tenant, _SEED.user, _SEED.agent, _SEED.project):
            await sess.refresh(obj, attribute_names=["created_at"])
    _SEED.seeded = True


async def _new_run(db_factory, revision: str, status: str = "AN_COMPLETED"):
    """Create one AN_COMPLETED (or open) run + findings in a fresh revision.

    The run is flushed FIRST so ``run.id`` is populated before the findings
    that reference it are constructed (a UUID ``default`` is applied at
    flush, not construction).
    """
    from app.dao.base import tenant_context

    tenant = _SEED.tenant.id
    run = AnalysisRun(
        id=uuid.uuid4(),
        project_id=_SEED.project.id,
        revision_sha=revision,
        status=status,
        tenant_id=tenant,
    )
    with tenant_context(tenant):
        async with db_factory() as sess, sess.begin():
            sess.add(run)
            await sess.flush()
            findings = [
                AnalysisFinding(
                    id=uuid.uuid4(),
                    analysis_run_id=run.id,
                    severity=sev,
                    category=cat,
                    tag=tag,
                    summary=f"{label} finding",
                    evidence={"anchors": [f"{label}.py:1"]},
                    tenant_id=tenant,
                )
                for (cat, sev, tag, label) in (
                    ("TECH_DEBT", "WARN", "FACT", "tech-finding"),
                    ("SECURITY", "CRITICAL", "FACT", "sec-finding"),
                )
            ]
            for f in findings:
                sess.add(f)
            await sess.flush()
    return run, findings


def _revision(tag: str) -> str:
    """A well-formed 64-hex revision, unique per process."""
    return (f"{tag}-{uuid.uuid4().hex}" * 4)[:64]


@LIVE
async def test_create_plan_materializes_tasks_and_wires_existing_graph(db) -> None:
    """(#1/#2/#3/#4) create_plan produces a plan that references the existing
    Task + Analysis entities and wires the DAG through the existing Task Graph
    (task_dependencies) — no second graph model is ever created."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.task import Task, TaskDependency

    tag = uuid.uuid4().hex[:8]
    run, _findings = await _new_run(db, _revision(tag))
    tenant = _SEED.tenant.id

    with tenant_context(tenant):
        async with db() as sess:
            async with sess.begin():
                outcome = await planning_service.create_plan(
                    sess,
                    project_id=_SEED.project.id,
                    analysis_run_id=run.id,
                    agent=_SEED.agent,
                    current_user=_SEED.user,
                )
                assert outcome.state == "created", outcome
                plan = outcome.plan
                assert plan is not None and plan.run.status == "PL_COMPLETED"
                # Two findings -> two goals, two packages, one delivery milestone.
                assert len(plan.goal_ids) == 2 and len(plan.package_ids) == 2
                assert len(plan.milestone_ids) == 1
                # 2 build slots + 1 review slot (the SECURITY package) = 3 tasks.
                assert len(plan.materialized_task_ids) == 3, plan.materialized_task_ids
                # Every materialized Task is pending + ANALYSIS_PLANNING with the
                # full 2D provenance set (finding_id NULL by design §4).
                for tid in plan.materialized_task_ids:
                    t = await sess.get(Task, tid)
                    assert t.created_reason == "ANALYSIS_PLANNING"
                    assert t.status == "pending"  # G4: create != run
                    assert t.finding_id is None
                    assert t.analysis_run_id == run.id and t.project_id == _SEED.project.id
                    assert t.revision_sha == run.revision_sha
                # DAG edges landed in the EXISTING task_dependencies table:
                # 1 intra (review->build) + 1 inter (pkg2 build->pkg1 build).
                task_ids = set(plan.materialized_task_ids)
                edges = (
                    await sess.execute(
                        select(TaskDependency).where(
                            TaskDependency.task_id.in_(task_ids),
                            TaskDependency.depends_on_task_id.in_(task_ids),
                        )
                    )
                ).all()
                assert len(edges) == plan.edge_count == 2, [e for e in edges]
                assert plan.plan_sha256 and len(plan.plan_sha256) == 64
            await sess.commit()

    # G4 — no Run was auto-enqueued for any materialized task (the execution
    # lane is the separately-authorized Phase 2E service).
    from app.models.agent_run import AgentRun

    with tenant_context(tenant):
        async with db() as sess:
            rows = (
                await sess.execute(
                    select(AgentRun).where(
                        AgentRun.source_type == "task",
                        AgentRun.source_id.in_([str(t) for t in plan.materialized_task_ids]),
                    )
                )
            ).scalars().all()
            assert rows == [], "G4: a plan must NEVER auto-enqueue a Run"


@LIVE
async def test_create_plan_same_revision_reuses_existing_run(db) -> None:
    """(#4/§6.1) a second call at the SAME analysis revision is the
    append-only re-read path: PL_PLAN_EXISTS, no clobber, no duplicate tasks."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.task import Task

    tag = uuid.uuid4().hex[:8]
    run, _ = await _new_run(db, _revision(tag))
    tenant = _SEED.tenant.id
    first_tasks: list[uuid.UUID] = []

    with tenant_context(tenant):
        async with db() as sess:
            async with sess.begin():
                o1 = await planning_service.create_plan(
                    sess,
                    project_id=_SEED.project.id,
                    analysis_run_id=run.id,
                    agent=_SEED.agent,
                    current_user=_SEED.user,
                )
                assert o1.state == "created", o1
                first_tasks = list(o1.plan.materialized_task_ids)
            await sess.commit()

        async with db() as sess:
            with tenant_context(tenant):
                o2 = await planning_service.create_plan(
                    sess,
                    project_id=_SEED.project.id,
                    analysis_run_id=run.id,
                    agent=_SEED.agent,
                    current_user=_SEED.user,
                )
                assert o2.state == "existing" and o2.code == "PL_PLAN_EXISTS", o2
                assert o2.plan is not None and o2.plan.run.id == o1.plan.run.id
                # No second plan revision row, no duplicate tasks.
                from app.dao.planning_dao import planning_run_dao

                plans = await planning_run_dao.list_plans_for_project(_SEED.project.id, db=sess)
                matching = [p for p in plans if p.analysis_revision_sha == run.revision_sha]
                assert len(matching) == 1
                n_tasks = (
                    await sess.execute(
                        select(Task).where(
                            Task.analysis_run_id == run.id, Task.created_reason == "ANALYSIS_PLANNING"
                        )
                    )
                ).all()
                assert len(n_tasks) == len(first_tasks), "dedup: no duplicate materialized tasks"


@LIVE
async def test_create_plan_rejects_non_completed_run_and_bad_inputs(db) -> None:
    """G1 closed-code gates: an open run, a cross-project run, a missing
    project, and a non-executable project status each fail closed with the
    documented PL_* code and write nothing."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.project import Project as P

    tenant = _SEED.tenant.id

    # An open (AN_OPEN) run is refused.
    tag = uuid.uuid4().hex[:8]
    open_run, _ = await _new_run(db, _revision(tag), status="AN_OPEN")
    with tenant_context(tenant):
        async with db() as sess:
            o = await planning_service.create_plan(
                sess,
                project_id=_SEED.project.id,
                analysis_run_id=open_run.id,
                agent=_SEED.agent,
                current_user=_SEED.user,
            )
            assert o.state == "failed" and o.code == "PL_RUN_NOT_COMPLETED", o

    # A project in a non-executable status (RECEIVED) is refused: commit a
    # received project + a completed run against it, then call create_plan in a
    # fresh session so the service's own load path performs the gate.
    tag2 = uuid.uuid4().hex[:8]
    run2 = AnalysisRun(project_id=_SEED.project.id, revision_sha=_revision(tag2), status="AN_COMPLETED", tenant_id=tenant)
    with tenant_context(tenant):
        async with db() as sess, sess.begin():
            received = P(name="plansvc-received", status="RECEIVED", created_by=_SEED.user.id, tenant_id=tenant)
            sess.add(received)
            await sess.flush()
            run2.project_id = received.id
            sess.add(run2)
            await sess.flush()
        async with db() as sess:
            with tenant_context(tenant):
                o2 = await planning_service.create_plan(
                    sess,
                    project_id=received.id,
                    analysis_run_id=run2.id,
                    agent=_SEED.agent,
                    current_user=_SEED.user,
                )
            assert o2.state == "failed" and o2.code == "PL_PROJECT_NOT_EXECUTABLE", o2

    # A missing project / a missing run each fail closed as PL_INVALID_INPUT.
    with tenant_context(tenant):
        async with db() as sess:
            o3 = await planning_service.create_plan(
                sess,
                project_id=uuid.uuid4(),
                analysis_run_id=run2.id,
                agent=_SEED.agent,
                current_user=_SEED.user,
            )
            assert o3.state == "failed" and o3.code == "PL_INVALID_INPUT", o3
            o4 = await planning_service.create_plan(
                sess,
                project_id=_SEED.project.id,
                analysis_run_id=uuid.uuid4(),
                agent=_SEED.agent,
                current_user=_SEED.user,
            )
            assert o4.state == "failed" and o4.code == "PL_INVALID_INPUT", o4


@LIVE
async def test_provenance_chain_is_queryable(db) -> None:
    """(#5) the provenance chain AnalysisFinding -> PlanningGoal -> WorkPackage
    -> Task is queryable end to end via provenance_for_task."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.task import Task

    tag = uuid.uuid4().hex[:8]
    run, findings = await _new_run(db, _revision(tag))
    # Index into the created tasks by the goal finding they came from:
    # _new_run creates [TECH_DEBT, SECURITY] in that order.
    tenant = _SEED.tenant.id
    tech_task = sec_task = None
    with tenant_context(tenant):
        async with db() as sess:
            async with sess.begin():
                o = await planning_service.create_plan(
                    sess,
                    project_id=_SEED.project.id,
                    analysis_run_id=run.id,
                    agent=_SEED.agent,
                    current_user=_SEED.user,
                )
                assert o.state == "created", o
            await sess.commit()
            # Map each materialized task to its originating finding via the
            # task description's provenance footer (finding <id> <category>).
            t_rows = (
                await sess.execute(
                    select(Task).where(
                        Task.analysis_run_id == run.id, Task.created_reason == "ANALYSIS_PLANNING"
                    )
                )
            ).scalars().all()
            by_finding = {}
            for t in t_rows:
                for f in findings:
                    if str(f.id) in (t.description or ""):
                        by_finding[f.id] = t
            tech_task = by_finding[findings[0].id]
            sec_task = by_finding[findings[1].id]

    # Query the chain for the SECURITY task: goal cites the security finding,
    # the work package links it, and the run id is the plan revision.
    with tenant_context(tenant):
        async with db() as sess:
            view = await planning_service.provenance_for_task(sess, task_id=sec_task.id, current_user=_SEED.user)
            assert view is not None
            assert view["task"].id == sec_task.id
            assert view["goal"].analysis_finding_ids == [str(findings[1].id)]
            assert [f.id for f in view["findings"]] == [findings[1].id]
            assert view["work_package"].planning_run_id is not None
            assert view["required_capabilities"] == ["security", "review"]

            # The TECH_DEBT task's chain resolves to its own (distinct) finding
            # and its backend/code capability input.
            tech_view = await planning_service.provenance_for_task(sess, task_id=tech_task.id, current_user=_SEED.user)
            assert tech_view is not None
            assert tech_view["goal"].analysis_finding_ids == [str(findings[0].id)]
            assert [f.id for f in tech_view["findings"]] == [findings[0].id]
            assert tech_view["required_capabilities"] == ["backend", "code"]

    # A non-planning (MANUAL) task has no planning provenance chain: create it
    # in a fresh write session, then query it on its own read session.
    manual = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant, title="manual-x")
    with tenant_context(tenant):
        async with db() as sess, sess.begin():
            sess.add(manual)
            await sess.flush()
        async with db() as sess:
            assert (
                await planning_service.provenance_for_task(sess, task_id=manual.id, current_user=_SEED.user)
                is None
            )

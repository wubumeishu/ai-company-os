"""Phase 3 Agent Assignment + concurrency validation — test suite.

Per docs/architecture/PHASE_3_SQUAD_ORCHESTRATION_V1_DESIGN.md §8/§10 the
assignment lane is a pure predicate layer (CONF-1..5 + REV-1..3) over the
frozen WP data + DAG + live roster, with a single additive write path to the
one assignment fact ``Task.agent_id``.  Two tiers, mirroring the
``test_planning_service.py`` split (t_12874a76):

- **DB-free predicate tier** (always run): every closed code is asserted in
  isolation over synthetic ``WpContext`` data — PL_RESOURCE_CONFLICT
  (CONF-1 distinct-agent + CONF-4 same-agent variants), the DAG-serializes
  negative case, PL_REVIEWER_NOT_INDEPENDENT (no disjoint reviewer /
  missing blocking edge / no review slot), PL_NO_CANDIDATE_AGENT (empty +
  all-inactive), CONF-5 advisory-only, determinism of the pick (two
  evaluations converge on the same single agent — the "no two agents on one
  task simultaneously" property), and the §8 AssignmentPlan artifact shape.

- **live-schema tier** (skipped when no reachable Postgres): a real
  planning materialization (f071 planning tables + frozen Task graph) driven
  through ``AssignmentService.apply_assignment`` — every fail-closed code
  refuses the package BEFORE any ``Task.agent_id`` write, and the pass case
  writes the single fact + returns the report-only artifact, then re-apply is
  a zero-write no-op.

Running the live tier::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch> \
        uv run --extra dev pytest tests/test_assignment_service.py
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.dao.base import tenant_context
from app.services.assignment_service import (
    PL_NO_CANDIDATE_AGENT,
    PL_OK,
    PL_PARALLELISM_ADVISORY,
    PL_RESOURCE_CONFLICT,
    PL_REVIEWER_NOT_INDEPENDENT,
    PL_RUN_NOT_COMPLETED,
    SlotView,
    WpContext,
    _transitive_deps,
    assignment_service,
    check_resource_conflicts,
    check_review_dag,
    concurrent_possible,
    evaluate_wp,
    render_assignment_plan,
)

# ---------------------------------------------------------------------------
# Shared synthetic ids for the DB-free tier.
# ---------------------------------------------------------------------------
T1 = uuid.UUID("aaaaaaa1-0000-0000-0000-000000000001")
T2 = uuid.UUID("aaaaaaa2-0000-0000-0000-000000000002")
T3 = uuid.UUID("aaaaaaa3-0000-0000-0000-000000000003")
A1 = uuid.UUID("bbbbbbb1-0000-0000-0000-000000000001")
A2 = uuid.UUID("bbbbbbb2-0000-0000-0000-000000000002")
A3 = uuid.UUID("bbbbbbb3-0000-0000-0000-000000000003")
WP = uuid.UUID("ccccccc0-0000-0000-0000-000000000000")


def _slot(
    sid: str,
    task_id: uuid.UUID | None,
    kind: str = "build",
    cands: tuple[uuid.UUID, ...] = (A1,),
    res: dict[str, Any] | None = None,
) -> SlotView:
    return SlotView(
        slot=sid,
        task_id=task_id,
        kind=kind,
        candidate_agent_ids=tuple(cands),
        shared_resources=res,
    )


def _ctx(
    *,
    slots: tuple[SlotView, ...],
    edges: tuple[tuple[uuid.UUID, uuid.UUID], ...] = (),
    wp_shared: dict[str, Any] | None = None,
    requires_review: bool = False,
    goal_caps: tuple[str, ...] = (),
    max_par: int | None = None,
    mode: str = "parallel",
) -> WpContext:
    return WpContext(
        wp_id=WP,
        execution_mode=mode,
        wp_shared_resources=wp_shared,
        requires_independent_review=requires_review,
        max_parallel_tasks=max_par,
        goal_capabilities=goal_caps,
        slots=slots,
        task_agent={s.task_id: A1 for s in slots if s.task_id is not None},
        dag_edges=tuple(edges),
    )


# ---------------------------------------------------------------------------
# CONCURRENT-POSSIBLE / TRANSITIVE-DEP CORE
# ---------------------------------------------------------------------------


def test_concurrent_possible_true_when_no_edge() -> None:
    reach = _transitive_deps([])
    assert concurrent_possible(T1, T2, reach) is True
    assert concurrent_possible(T2, T1, reach) is True


def test_concurrent_false_when_edge_serializes() -> None:
    # T2 depends on T1  =>  edge (T2, T1).  T1 is an upstream of T2, so the
    # two cannot run concurrently.
    reach = _transitive_deps([(T2, T1)])
    assert concurrent_possible(T1, T2, reach) is False
    assert concurrent_possible(T2, T1, reach) is False


def test_transitive_deps_follows_chain() -> None:
    # T3 depends on T2, T2 depends on T1  =>  T3 transitively depends on T1.
    reach = _transitive_deps([(T3, T2), (T2, T1)])
    assert T1 in reach[T3]
    assert T2 in reach[T3]
    assert T3 not in reach[T1]  # T1 has no deps


# ---------------------------------------------------------------------------
# CONF-1 / CONF-4 — RESOURCE CONFLICT PREDICATE
# ---------------------------------------------------------------------------


def test_conf1_distinct_agent_overlap_fires() -> None:
    ctx = _ctx(
        slots=(
            _slot("s1", T1, cands=(A1,)),
            _slot("s2", T2, cands=(A2,)),
        ),
        wp_shared={"files": ["x.py"]},
    )
    chosen, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert report.fail_closed_codes() == (PL_RESOURCE_CONFLICT,)
    # The pair is distinct-agent => the rule tag is CONF-1.
    assert any(c.rule == "CONF-1" for c in report.conflicts)
    assert chosen[T1] == A1 and chosen[T2] == A2


def test_conf4_same_agent_overlap_fires() -> None:
    ctx = _ctx(
        slots=(
            _slot("s1", T1, cands=(A1,)),
            _slot("s2", T2, cands=(A1,)),
        ),
        wp_shared={"db": ["orders"]},
    )
    _, report = evaluate_wp(ctx, frozenset({A1}))
    assert report.fail_closed_codes() == (PL_RESOURCE_CONFLICT,)
    assert any(c.rule == "CONF-4" for c in report.conflicts)
    assert report.conflicts[0].resource == ("db", "orders")


def test_dag_serialization_clears_conflict() -> None:
    # Same resource, but the DAG makes the pair serial => no conflict.
    ctx = _ctx(
        slots=(
            _slot("s1", T1, cands=(A1,)),
            _slot("s2", T2, cands=(A2,)),
        ),
        edges=((T2, T1),),
        wp_shared={"files": ["x.py"]},
    )
    _, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert report.conflicts == ()
    assert report.fail_closed_codes() == ()


def test_observer_slots_never_conflict_with_mutator() -> None:
    # kind='review' is an observer: it shares the resource with a build slot
    # but must not fire CONF-1/CONF-4.
    ctx = _ctx(
        slots=(
            _slot("b", T1, kind="build", cands=(A1,)),
            _slot("r", T2, kind="review", cands=(A2,)),
        ),
        wp_shared={"files": ["x.py"]},
    )
    _, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert report.conflicts == ()


def test_slot_level_resources_override_wp_level() -> None:
    # Two build slots: WP declares files:x.py, but s1 narrows to files:y.py,
    # s2 keeps the WP default.  They share nothing => no conflict.
    ctx = _ctx(
        slots=(
            _slot("s1", T1, cands=(A1,), res={"files": ["y.py"]}),
            _slot("s2", T2, cands=(A2,)),
        ),
        wp_shared={"files": ["x.py"]},
    )
    _, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert report.conflicts == ()


# ---------------------------------------------------------------------------
# REV-1 / REV-2 — REVIEW-INDEPENDENCE PREDICATE
# ---------------------------------------------------------------------------


def test_rev1_no_disjoint_reviewer_fires() -> None:
    ctx = _ctx(
        slots=(
            _slot("b", T1, kind="build", cands=(A1,)),
            _slot("r", T2, kind="review", cands=(A1,)),  # only A1, not disjoint
        ),
        edges=((T2, T1),),
        requires_review=True,
    )
    _, report = evaluate_wp(ctx, frozenset({A1}))
    assert PL_REVIEWER_NOT_INDEPENDENT in report.fail_closed_codes()
    assert any(c.rule == "REV-1" for c in report.conflicts)


def test_rev2_missing_blocking_edge_fires() -> None:
    # Disjoint reviewer exists, but no DAG edge blocks the builder.
    ctx = _ctx(
        slots=(
            _slot("b", T1, kind="build", cands=(A1,)),
            _slot("r", T2, kind="review", cands=(A2,)),
        ),
        requires_review=True,  # no edges at all
    )
    _, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert PL_REVIEWER_NOT_INDEPENDENT in report.fail_closed_codes()
    assert any(c.rule == "REV-2" for c in report.conflicts)


def test_review_flagged_package_with_no_review_slot_fires() -> None:
    ctx = _ctx(
        slots=(_slot("b", T1, kind="build", cands=(A1,)),),
        requires_review=True,
    )
    _, report = evaluate_wp(ctx, frozenset({A1}))
    assert PL_REVIEWER_NOT_INDEPENDENT in report.fail_closed_codes()
    assert any(c.rule == "REV-1" for c in report.conflicts)


def test_independent_review_pass_yields_binding_and_edge() -> None:
    ctx = _ctx(
        slots=(
            _slot("b", T1, kind="build", cands=(A1,)),
            _slot("r", T2, kind="review", cands=(A2,)),
        ),
        edges=((T2, T1),),  # reviewer blocks the builder
        requires_review=True,
    )
    chosen, report = evaluate_wp(ctx, frozenset({A1, A2}))
    assert report.fail_closed_codes() == ()
    assert chosen[T1] == A1 and chosen[T2] == A2
    binding = report.review_bindings[0]
    assert binding.reviewer_task_id == T2
    assert binding.reviewer_agent_id == A2
    assert binding.reviewed_task_ids == (T1,)
    assert binding.blocking_edges == ((T2, T1),)


# ---------------------------------------------------------------------------
# §4.2 — NO-CANDIDATE PREDICATE
# ---------------------------------------------------------------------------


def test_no_candidate_empty_list_fires() -> None:
    ctx = _ctx(slots=(_slot("b", T1, kind="build", cands=()),))
    _, report = evaluate_wp(ctx, frozenset({A1}))
    assert report.fail_closed_codes() == (PL_NO_CANDIDATE_AGENT,)


def test_no_candidate_all_inactive_fires() -> None:
    # Candidates reference A3 but the roster only has A1 active.
    ctx = _ctx(slots=(_slot("b", T1, kind="build", cands=(A3,)),))
    _, report = evaluate_wp(ctx, frozenset({A1}))
    assert report.fail_closed_codes() == (PL_NO_CANDIDATE_AGENT,)


# ---------------------------------------------------------------------------
# CONF-5 — ADVISORY ONLY
# ---------------------------------------------------------------------------


def test_conf5_advisory_does_not_fail_closed() -> None:
    # Three concurrent mutator slots exceed the max_parallel_tasks hint of 1.
    ctx = _ctx(
        slots=(
            _slot("s1", T1, cands=(A1,)),
            _slot("s2", T2, cands=(A2,)),
            _slot("s3", T3, cands=(A3,)),
        ),
        max_par=1,
    )
    _, report = evaluate_wp(ctx, frozenset({A1, A2, A3}))
    assert report.fail_closed_codes() == ()  # advisory never refuses V1
    assert [a.rule for a in report.advisories] == ["CONF-5"]
    assert PL_PARALLELISM_ADVISORY == report.advisories[0].code


# ---------------------------------------------------------------------------
# DETERMINISM / SINGLE-FACT CONVERGENCE
# ---------------------------------------------------------------------------


def test_pick_is_deterministic_and_single_agent_per_task() -> None:
    ctx = _ctx(
        slots=(
            _slot("s1", T1, kind="build", cands=(A1, A2)),
            _slot("s2", T2, kind="build", cands=(A1, A2)),
        ),
    )
    chosen_a, _ = evaluate_wp(ctx, frozenset({A1, A2}))
    chosen_b, _ = evaluate_wp(ctx, frozenset({A1, A2}))
    assert chosen_a == chosen_b  # two evaluations converge on the same map
    # Each task binds to exactly ONE agent (the single-fact invariant).
    assert len(chosen_a) == 2
    assert all(isinstance(v, uuid.UUID) for v in chosen_a.values())
    # First survivor in planner-declared order wins deterministically.
    assert chosen_a[T1] == A1 and chosen_a[T2] == A1


def test_two_agents_cannot_bind_one_task() -> None:
    # A single slot with two candidates still resolves to exactly one agent.
    ctx = _ctx(slots=(_slot("s1", T1, kind="build", cands=(A1, A2)),))
    chosen, _ = evaluate_wp(ctx, frozenset({A1, A2}))
    assert chosen == {T1: A1}  # one pick, never two
    assert isinstance(chosen[T1], uuid.UUID)  # a scalar agent id, not a set


# ---------------------------------------------------------------------------
# §8 — ASSIGNMENTPLAN ARTIFACT SHAPE
# ---------------------------------------------------------------------------


def test_assignment_plan_artifact_shape() -> None:
    ctx = _ctx(
        slots=(
            _slot("b", T1, kind="build", cands=(A1,)),
            _slot("r", T2, kind="review", cands=(A2,)),
        ),
        edges=((T2, T1),),
        requires_review=True,
    )
    chosen, report = evaluate_wp(ctx, frozenset({A1, A2}))
    plan = render_assignment_plan(WP, ctx, chosen, report)
    assert plan["work_package_id"] == str(WP)
    assert {s["slot"] for s in plan["slots"]} == {"b", "r"}
    by_slot = {s["slot"]: s for s in plan["slots"]}
    assert by_slot["b"]["chosen_agent_id"] == str(A1)
    assert by_slot["r"]["chosen_agent_id"] == str(A2)
    assert "constraint_report" in plan
    assert "review_bindings" in plan
    assert plan["constraint_report"]["fail_closed"] == []
    assert plan["review_bindings"][0]["reviewer_task_id"] == str(T2)


# ---------------------------------------------------------------------------
# check_* helpers directly (narrow predicates)
# ---------------------------------------------------------------------------


def test_check_review_dag_noop_when_unflagged() -> None:
    ctx = _ctx(slots=(_slot("b", T1, kind="build", cands=(A1,)),))
    assert check_review_dag(ctx, {T1: A1}) == []


def test_check_resource_conflicts_noop_when_no_shared() -> None:
    ctx = _ctx(slots=(_slot("s1", T1, cands=(A1,)), _slot("s2", T2, cands=(A2,))))
    assert check_resource_conflicts(ctx, {T1: A1, T2: A2}) == []


# ===========================================================================
# LIVE-SCHEMA TIER — the transactional write + refusal-before-write (Postgres).
# ===========================================================================

LIVE = pytest.mark.usefixtures("_live_db")


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
            "no reachable Postgres for the assignment live tier; "
            "the DB-free predicate tier carries the constraint logic"
        )


@pytest.fixture
async def db(_live_db) -> Any:
    # Register the model modules whose tables the FK graph references but the
    # service under test does not import (e.g. Task.analysis_run_id ->
    # analysis_runs, TaskDependency -> tasks).  Importing the analysis +
    # planning + task model modules is enough to compile every relationship-
    # bearing query — the same light recipe the planning-service live tier
    # uses (do NOT import app.main: it drags in the full app/runtime and
    # hangs at import time in the test process).
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models.analysis
    import app.models.planning
    import app.models.task  # noqa: F401
    from app.config import get_settings

    engine = create_async_engine(get_settings().DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _Seed:
    """Committed tenant graph (tenant/user/agents/model/project), idempotent."""

    def __init__(self) -> None:
        self.tenant: Any = None
        self.user: Any = None
        self.model: Any = None
        self.agents: list[Any] = []
        self.project: Any = None
        self.seeded = False


_SEED = _Seed()


async def _seed(db_factory: Any) -> _Seed:
    """Seed one committed tenant graph + two agents + an ANALYZING project."""
    if _SEED.seeded:
        return _SEED
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.models.user import User

    async with db_factory() as sess, sess.begin():
        _SEED.tenant = Tenant(name="asgn", slug="asgn-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant)
        await sess.flush()
        _SEED.user = User(
            tenant_id=_SEED.tenant.id,
            display_name="asgn",
            role="member",
            is_active=True,
            email=f"asgn-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user)
        await sess.flush()
        _SEED.model = LLMModel(
            tenant_id=_SEED.tenant.id,
            provider="anthropic",
            model="claude-opus-4-6",
            api_key_encrypted="enc-test",
            label="asgn-model",
        )
        sess.add(_SEED.model)
        await sess.flush()
        for name in ("asgn-builder", "asgn-reviewer"):
            agent = Agent(
                name=name,
                creator_id=_SEED.user.id,
                tenant_id=_SEED.tenant.id,
                access_mode="company",
                status="running",
                primary_model_id=_SEED.model.id,
            )
            sess.add(agent)
            await sess.flush()
            _SEED.agents.append(agent)
        _SEED.project = Project(
            name="asgn-proj",
            status="ANALYZING",
            created_by=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
        )
        sess.add(_SEED.project)
        await sess.flush()
        for obj in (_SEED.tenant, _SEED.user, _SEED.project):
            await sess.refresh(obj, attribute_names=["created_at"])
    _SEED.seeded = True
    return _SEED


def _slot_json(
    slot: str,
    kind: str,
    title: str,
    cands: list[str],
    depends_on: list[str] | None = None,
    res: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slot": slot,
        "kind": kind,
        "title": title,
        "description": f"live {slot}",
        "is_head": kind == "build",
        "required_capabilities": ["code"],
        "candidate_agent_ids": cands,
    }
    if depends_on is not None:
        entry["depends_on_slots"] = depends_on
    if res is not None:
        entry["shared_resources"] = res
    return entry


class _Spec:
    """One planning materialization spec (pure data, no DB)."""

    def __init__(
        self,
        seed: _Seed,
        *,
        scope: list[dict[str, Any]],
        intra_edges: list[tuple[str, str]],
        wp_shared: dict[str, Any] | None,
        requires_review: bool,
        run_status: str = "PL_COMPLETED",
        mode: str = "parallel",
    ) -> None:
        self.seed = seed
        self.scope = scope
        self.intra_edges = intra_edges
        self.wp_shared = wp_shared
        self.requires_review = requires_review
        self.run_status = run_status
        self.mode = mode
        self.revision = ("asgn-" + uuid.uuid4().hex) * 4
        self.revision = self.revision[:64]
        self.wp_title = "asgn package " + uuid.uuid4().hex[:8]


async def _materialize(db_factory: Any, spec: _Spec) -> tuple[Any, dict[str, Any], _Seed]:
    """Seed a committed planning revision + its materialized tasks/edges.

    Returns ``(wp, {slot_id: task}, seed)``.  Every materialized Task starts
    bound to ``seed.agents[0]`` (the default); ``apply_assignment`` re-points
    the ones the constraint layer passes.  The run is left in
    ``spec.run_status``; for PL_COMPLETED the payload is locked so the
    assignment lane's PL_COMPLETED gate passes.
    """
    from app.dao.base import tenant_context
    from app.dao.planning_dao import (
        planning_goal_dao,
        planning_run_dao,
        work_package_dao,
        work_package_task_dao,
    )
    from app.dao.task_dao import task_provenance_dao
    from app.models.planning import (
        PlanningGoal,
        PlanningRun,
        WorkPackage,
        WorkPackageTask,
    )
    from app.models.task import Task
    from app.services.task_graph_service import task_graph_service

    seed = spec.seed
    tenant_id = seed.tenant.id
    default_agent = seed.agents[0]
    tasks: dict[str, Task] = {}

    with tenant_context(tenant_id):
        async with db_factory() as sess, sess.begin():
            run = PlanningRun(
                project_id=seed.project.id,
                analysis_revision_sha=spec.revision,
                planner_agent_id=default_agent.id,
                status="PL_OPEN",
                tenant_id=tenant_id,
            )
            await planning_run_dao.open_run(run, tenant_id=tenant_id, db=sess)

            goal = await planning_goal_dao.add_goal(
                PlanningGoal(
                    planning_run_id=run.id,
                    title="live goal",
                    required_capabilities=["code"],
                    analysis_finding_ids=[],
                    status="PL_PROPOSED",
                    tenant_id=tenant_id,
                ),
                tenant_id=tenant_id,
                db=sess,
            )

            wp = await work_package_dao.add_package(
                WorkPackage(
                    planning_run_id=run.id,
                    planning_goal_id=goal.id,
                    title=spec.wp_title,
                    task_scope=spec.scope,
                    execution_mode=spec.mode,
                    shared_resources=spec.wp_shared,
                    requires_independent_review=spec.requires_review,
                    tenant_id=tenant_id,
                ),
                tenant_id=tenant_id,
                db=sess,
            )

            task_ids_by_slot: dict[str, uuid.UUID] = {}
            for entry in spec.scope:
                task = Task(
                    agent_id=default_agent.id,
                    created_by=seed.user.id,
                    tenant_id=tenant_id,
                    project_id=seed.project.id,
                    created_reason="ANALYSIS_PLANNING",
                    title=entry["title"],
                    description=entry.get("description"),
                    type="todo",
                    status="pending",
                    priority="medium",
                )
                await task_provenance_dao.create_with_provenance(task, db=sess)
                link = WorkPackageTask(work_package_id=wp.id, tenant_id=tenant_id)
                await work_package_task_dao.add_slot(link, tenant_id=tenant_id, db=sess)
                await work_package_task_dao.materialize_slot(link, task_id=task.id, db=sess)
                tasks[entry["slot"]] = task
                task_ids_by_slot[entry["slot"]] = task.id

            for dependent, dep_on in spec.intra_edges:
                result = await task_graph_service.bulk_add_edges(
                    sess,
                    task_id=task_ids_by_slot[dependent],
                    depends_on_task_ids=[task_ids_by_slot[dep_on]],
                    tenant_id=tenant_id,
                )
                assert result.state in ("added", "blocked"), result.detail

            if spec.run_status == "PL_COMPLETED":
                await planning_run_dao.complete_run(
                    run, new_status="PL_COMPLETED", plan_sha256="asgn-sha", db=sess
                )
            await sess.commit()
    return wp, tasks, seed


async def _read_task_agents(db_factory: Any, tenant_id: uuid.UUID, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Re-read the authoritative Task.agent_id facts (single source of truth)."""
    from sqlalchemy import select

    from app.models.task import Task

    with tenant_context(tenant_id):
        async with db_factory() as sess:
            rows = (
                await sess.execute(
                    select(Task.id, Task.agent_id).where(
                        Task.id.in_(task_ids),
                        Task.tenant_id == tenant_id,
                    )
                )
            ).all()
    return {t: a for (t, a) in rows}


@LIVE
async def test_apply_pass_writes_single_fact_and_reapply_noop(db: Any) -> None:
    seed = await _seed(db)
    builders = [str(seed.agents[0].id), str(seed.agents[1].id)]
    scope = [
        _slot_json("b", "build", "asgn live build", builders),
        _slot_json("r", "review", "asgn live review", [str(seed.agents[1].id)], depends_on=["b"]),
    ]
    spec = _Spec(
        seed,
        scope=scope,
        intra_edges=[("r", "b")],
        wp_shared={"files": ["asgn.py"]},
        requires_review=True,
    )
    wp, tasks, seed = await _materialize(db, spec)

    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome.state == "assigned"
    assert outcome.code == PL_OK
    plan = outcome.assignment_plan
    assert plan is not None and plan["work_package_id"] == str(wp.id)
    by_slot = {s["slot"]: s for s in plan["slots"]}
    # builder picks agents[0], reviewer picks agents[1] (disjoint).
    assert by_slot["b"]["chosen_agent_id"] == str(seed.agents[0].id)
    assert by_slot["r"]["chosen_agent_id"] == str(seed.agents[1].id)
    assert plan["constraint_report"]["fail_closed"] == []

    # The single assignment fact was written to Task.agent_id (authoritative).
    facts = await _read_task_agents(db, seed.tenant.id, [tasks["b"].id, tasks["r"].id])
    assert facts[tasks["b"].id] == seed.agents[0].id
    assert facts[tasks["r"].id] == seed.agents[1].id

    # Re-apply is a zero-write no-op: the facts are unchanged and the second
    # evaluation converges on the same single agent (a task is never bound
    # to a second agent).
    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome2 = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome2.state == "assigned" and outcome2.code == PL_OK
    facts2 = await _read_task_agents(db, seed.tenant.id, [tasks["b"].id, tasks["r"].id])
    assert facts2 == facts


@LIVE
async def test_apply_refuses_resource_conflict_before_any_write(db: Any) -> None:
    seed = await _seed(db)
    # Two concurrent build slots share a file AND both bind to the same
    # default candidate => a same-agent mutator overlap (fail-closed).  No
    # serializing edge => the pair is concurrent-possible.
    only = [str(seed.agents[0].id)]
    scope = [
        _slot_json("b0", "build", "asgn conflict b0", only, res={"files": ["shared.py"]}),
        _slot_json("b1", "build", "asgn conflict b1", only, res={"files": ["shared.py"]}),
    ]
    spec = _Spec(
        seed,
        scope=scope,
        intra_edges=[],
        wp_shared=None,
        requires_review=False,
    )
    wp, tasks, seed = await _materialize(db, spec)
    assert tasks["b0"].agent_id == seed.agents[0].id  # pre-write default binding

    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome.state == "failed"
    assert outcome.code == PL_RESOURCE_CONFLICT

    # Fail-closed means NO Task.agent_id was re-pointed: both tasks still
    # carry their pre-write default binding (the writer never ran).
    facts = await _read_task_agents(db, seed.tenant.id, [tasks["b0"].id, tasks["b1"].id])
    assert facts[tasks["b0"].id] == seed.agents[0].id
    assert facts[tasks["b1"].id] == seed.agents[0].id


@LIVE
async def test_apply_refuses_non_independent_reviewer_before_write(db: Any) -> None:
    seed = await _seed(db)
    # requires_review but the reviewer's only candidate is the builder's
    # agent => no disjoint reviewer => PL_REVIEWER_NOT_INDEPENDENT.
    only = [str(seed.agents[0].id)]
    scope = [
        _slot_json("b", "build", "asgn rev b", only),
        _slot_json("r", "review", "asgn rev r", only, depends_on=["b"]),
    ]
    spec = _Spec(
        seed,
        scope=scope,
        intra_edges=[("r", "b")],
        wp_shared=None,
        requires_review=True,
    )
    wp, tasks, seed = await _materialize(db, spec)

    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome.state == "failed"
    assert outcome.code == PL_REVIEWER_NOT_INDEPENDENT
    # No write happened: the build task still carries its default binding.
    facts = await _read_task_agents(db, seed.tenant.id, [tasks["b"].id])
    assert facts[tasks["b"].id] == seed.agents[0].id


@LIVE
async def test_apply_refuses_missing_candidate_before_write(db: Any) -> None:
    seed = await _seed(db)
    # A candidate that does not exist in the live roster => PL_NO_CANDIDATE_AGENT.
    ghost = [str(uuid.uuid4())]
    scope = [_slot_json("b", "build", "asgn ghost b", ghost)]
    spec = _Spec(
        seed,
        scope=scope,
        intra_edges=[],
        wp_shared=None,
        requires_review=False,
    )
    wp, tasks, seed = await _materialize(db, spec)

    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome.state == "failed"
    assert outcome.code == PL_NO_CANDIDATE_AGENT
    facts = await _read_task_agents(db, seed.tenant.id, [tasks["b"].id])
    assert facts[tasks["b"].id] == seed.agents[0].id  # unchanged default


@LIVE
async def test_apply_refuses_open_run_before_write(db: Any) -> None:
    seed = await _seed(db)
    only = [str(seed.agents[0].id)]
    scope = [_slot_json("b", "build", "asgn open b", only)]
    spec = _Spec(
        seed,
        scope=scope,
        intra_edges=[],
        wp_shared=None,
        requires_review=False,
        run_status="PL_OPEN",  # run not yet completed => PL_RUN_NOT_COMPLETED
    )
    wp, tasks, seed = await _materialize(db, spec)

    with tenant_context(seed.tenant.id):
        async with db() as sess:
            outcome = await assignment_service.apply_assignment(
                sess, work_package_id=wp.id, current_user=seed.user
            )
            await sess.commit()
    assert outcome.state == "failed"
    assert outcome.code == PL_RUN_NOT_COMPLETED
    facts = await _read_task_agents(db, seed.tenant.id, [tasks["b"].id])
    assert facts[tasks["b"].id] == seed.agents[0].id  # unchanged default

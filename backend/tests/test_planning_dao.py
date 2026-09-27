"""Planning DAO — real-schema semantics (Postgres).

Companion to the DB-free migration contract in
``test_planning_persistence_migration.py``: that suite proves the f071 DDL is
guarded / idempotent / re-runnable without a database.  This module proves the
DAO layer (``app/dao/planning_dao.py``) behaves against the *resulting
schema*:

- the UNIQUE(project, analysis_revision_sha) guard rejects a duplicate
  (project, revision) pair (invariant §6.1, append-only)
- closed-set validation fails closed (ClosedCodeError) on every status,
  milestone-kind, execution-mode, and task_scope slot write (design D5/§6.7)
- ``get_plan_for_project`` / ``list_work_packages_by_milestone`` /
  ``list_milestones_for_run`` / ``get_assignment_candidates_for_work_package``
  (design §10) return the documented shapes against real rows
- the DB-enforced invariants §6.2/§6.3/§6.4 fire (uq_wp_tasks,
  ck_wp_tasks_materialized, uq_milestones_run_seq)
- the SET NULL / §6.3 CHECK interaction on the link table fails closed, and
  the re-open path preserves the intent
- tenant isolation: a second tenant's plan revision is invisible to the
  first tenant's scoped read
- goals CASCADE with their run

Session / transaction discipline: one session per test (the whole test body
is one transaction that commits on clean exit); every *expected*
database-level failure is contained in a ``db.begin_nested()`` savepoint —
a bare flush inside ``db.begin()`` would poison the asyncpg connection and
mask the real error (mirrors the design §4 contract that "the caller owns
the re-read recovery").  Revision strings are unique per process (``_rev``)
so the suite is re-runnable against the dedicated scratch DB.

It seeds one committed tenant graph through the real ORM models (so every
default is honored exactly as the app uses them) and points at a scratch
Postgres via ``DATABASE_URL`` (after ``alembic upgrade head``).  On a host
without a reachable Postgres the whole module skips via the autouse
``_db_available`` guard (the honest evidence boundary — the DB-free suite
carries the DDL logic, this one carries the live-schema proof).

Running this suite::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t_b342df97_f071 \
        uv run --extra dev pytest tests/test_planning_dao.py
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Register the full metadata graph (module side effects), not just the
# classes we instantiate: Task.finding_id FKs analysis_findings, so the
# Task insert above needs the analysis models in Base.metadata too.
import app.models.agent
import app.models.agent_run
import app.models.analysis
import app.models.planning
import app.models.project
import app.models.task
import app.models.tenant
import app.models.user  # noqa: F401
from app.config import get_settings
from app.dao.base import tenant_context
from app.dao.planning_dao import (
    ClosedCodeError,
    milestone_dao,
    planning_goal_dao,
    planning_run_dao,
    work_package_dao,
    work_package_task_dao,
)
from app.models.agent import Agent
from app.models.planning import Milestone, PlanningGoal, PlanningRun, WorkPackage, WorkPackageTask
from app.models.project import Project
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _db_available() -> None:
    """Skip the whole module when Postgres is unreachable on this host.

    One reachability probe per session on a dedicated raw asyncpg connection
    so the probe never leaks a pooled connection into the app engine (mirrors
    test_task_graph_provenance.py)."""
    if getattr(_db_available, "_result", None) is None:  # type: ignore[attr-defined]
        import asyncpg

        async def _probe() -> bool:
            dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
            try:
                conn = await asyncpg.connect(dsn)
                await conn.execute("select 1")
                await conn.close()
                return True
            except Exception:  # noqa: BLE001 - no DB / no creds / no schema all mean "skip"
                return False

        loop = asyncio.new_event_loop()
        try:
            _db_available._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    if not _db_available._result:  # type: ignore[attr-defined]
        pytest.skip(
            "no reachable Postgres for the planning DAO suite; the DB-free migration suite carries the DDL logic"
        )


class _Fixture:
    """Committed seed graph, created once per pytest process (idempotent)."""

    def __init__(self) -> None:
        self.tenant: Tenant | None = None
        self.other_tenant: Tenant | None = None
        self.user: User | None = None
        self.agent: Agent | None = None
        self.project: Project | None = None
        self.seeded = False


_FIXTURE = _Fixture()


@pytest.fixture
async def pl_db(_db_available) -> AsyncGenerator[async_sessionmaker, None]:
    """A fresh engine + session factory bound to the (scratch) DATABASE_URL.

    ``expire_on_commit=False``: sessions are passed around inside one
    ``db.begin()`` block (flush-only, committed by the context manager) and
    the tests keep reading ORM attributes after the flush — the same
    convention app/database.py uses for the request lifecycle."""
    engine = create_async_engine(get_settings().DATABASE_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _ensure_seeded(factory: async_sessionmaker) -> None:
    """Seed one committed tenant graph; a no-op if already seeded this run."""
    if _FIXTURE.seeded:
        return
    async with factory() as db, db.begin():
        _FIXTURE.tenant = Tenant(name="f071-pytest", slug="f071-" + uuid.uuid4().hex[:10])
        db.add(_FIXTURE.tenant)
        await db.flush()
        _FIXTURE.other_tenant = Tenant(name="f071-other", slug="f071other-" + uuid.uuid4().hex[:10])
        db.add(_FIXTURE.other_tenant)
        await db.flush()
        _FIXTURE.user = User(
            tenant_id=_FIXTURE.tenant.id,
            display_name="f071",
            role="member",
            is_active=True,
            email=f"f071-{uuid.uuid4().hex}@example.test",
        )
        db.add(_FIXTURE.user)
        await db.flush()
        _FIXTURE.agent = Agent(
            name="f071-agent",
            creator_id=_FIXTURE.user.id,
            tenant_id=_FIXTURE.tenant.id,
            access_mode="company",
            status="running",
        )
        db.add(_FIXTURE.agent)
        await db.flush()
        _FIXTURE.project = Project(
            name="f071-project",
            status="INITIALIZED",
            created_by=_FIXTURE.user.id,
            tenant_id=_FIXTURE.tenant.id,
        )
        db.add(_FIXTURE.project)
        await db.flush()
        # asyncpg does not fetch server-generated defaults (created_at etc.):
        # re-load them inside the session so the commit never triggers a
        # background SELECT (the f069 suite's exact gotcha).
        for obj in (_FIXTURE.tenant, _FIXTURE.other_tenant, _FIXTURE.user, _FIXTURE.agent, _FIXTURE.project):
            await db.refresh(obj, attribute_names=["created_at"])
        _FIXTURE.seeded = True


def _run(tenant_id: uuid.UUID, project_id: uuid.UUID, revision: str) -> PlanningRun:
    return PlanningRun(project_id=project_id, analysis_revision_sha=revision, tenant_id=tenant_id)


def _rev(name: str) -> str:
    """A process-unique revision string so committed per-test rows never collide."""
    return f"{name}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# PlanningRunDAO — versioning guard + closed status set
# ---------------------------------------------------------------------------
async def test_plan_open_complete_and_re_read(pl_db) -> None:
    await _ensure_seeded(pl_db)
    t, p = _FIXTURE.tenant, _FIXTURE.project  # type: ignore[union-attr]
    rev = _rev("sha-a")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            run = await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            found = await planning_run_dao.get_plan_for_project(p.id, rev, db=db)
            assert found is not None and found.id == run.id and found.status == "PL_OPEN"

            await planning_run_dao.complete_run(run, new_status="PL_COMPLETED", plan_sha256="abc123", db=db)
            assert run.status == "PL_COMPLETED" and run.plan_sha256 == "abc123" and run.finished_at is not None

            # Closed-set validation fails closed (design D5/§6.7): no back
            # transition, no unknown code.
            with pytest.raises(ClosedCodeError):
                await planning_run_dao.complete_run(run, new_status="PL_OPEN", db=db)
            with pytest.raises(ClosedCodeError):
                await planning_run_dao.complete_run(run, new_status="PL_BOGUS", db=db)

            # The run is in the committed set for this project (newest-first
            # listing puts it first).
            plans = await planning_run_dao.list_plans_for_project(p.id, db=db)
            assert any(pl.id == run.id for pl in plans)


async def test_revision_guard_rejects_duplicate_pair(pl_db) -> None:
    """The UNIQUE(project_id, analysis_revision_sha) invariant (§6.1)."""
    await _ensure_seeded(pl_db)
    t, p = _FIXTURE.tenant, _FIXTURE.project  # type: ignore[union-attr]
    rev = _rev("sha-guard")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            # A racing second launch at the SAME revision loses the insert
            # race: the UNIQUE guard raises IntegrityError on the flush,
            # contained in a savepoint; the re-read path returns the winner.
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    db.add(_run(t.id, p.id, rev))
                    await db.flush()
            winner = await planning_run_dao.get_plan_for_project(p.id, rev, db=db)
            assert winner is not None


async def test_tenant_isolation_on_plan_read(pl_db) -> None:
    """A revision on another tenant's project is invisible in-scope."""
    await _ensure_seeded(pl_db)
    t, ot, p = _FIXTURE.tenant, _FIXTURE.other_tenant, _FIXTURE.project  # type: ignore[union-attr]
    rev = _rev("sha-multi")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
    # The other tenant owns its own project + a same-named revision:
    with tenant_context(ot.id):
        async with pl_db() as db, db.begin():
            other_user = User(
                tenant_id=ot.id,
                display_name="f071-other",
                role="member",
                is_active=True,
                email=f"f071other-{uuid.uuid4().hex}@example.test",
            )
            db.add(other_user)
            await db.flush()
            other_project = Project(name="f071-other-project", status="INITIALIZED", created_by=other_user.id, tenant_id=ot.id)
            db.add(other_project)
            await db.flush()
            await planning_run_dao.open_run(_run(ot.id, other_project.id, rev), tenant_id=ot.id, db=db)
            # Under tenant B, tenant A's project has no plan rows: scoped read
            # returns None instead of leaking A's revision.
            assert await planning_run_dao.get_plan_for_project(p.id, rev, db=db) is None
            assert await planning_run_dao.get_plan_for_project(other_project.id, rev, db=db) is not None


# ---------------------------------------------------------------------------
# PlanningGoalDAO — closed status + capability input validation + CASCADE
# ---------------------------------------------------------------------------
async def test_goal_closed_sets_and_cascade(pl_db) -> None:
    await _ensure_seeded(pl_db)
    t, p = _FIXTURE.tenant, _FIXTURE.project  # type: ignore[union-attr]
    rev = _rev("sha-goals")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            run = await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            goal = PlanningGoal(
                planning_run_id=run.id,
                title="ship the refactor",
                required_capabilities=["backend", "db-migration"],
                analysis_finding_ids=["f1", "f2"],
                tenant_id=t.id,
            )
            added = await planning_goal_dao.add_goal(goal, tenant_id=t.id, db=db)
            assert added.status == "PL_PROPOSED"

            goals = await planning_goal_dao.list_goals_for_run(run.id, db=db)
            assert [g.id for g in goals] == [goal.id]

            # Closed-set validation on every write path (D5/§6.7-8).
            bad = PlanningGoal(planning_run_id=run.id, title="x", status="PL_WONKY", tenant_id=t.id)
            with pytest.raises(ClosedCodeError):
                await planning_goal_dao.add_goal(bad, tenant_id=t.id, db=db)
            cap = PlanningGoal(
                planning_run_id=run.id, title="y", required_capabilities=["teleportation"], tenant_id=t.id
            )
            with pytest.raises(ClosedCodeError):
                await planning_goal_dao.add_goal(cap, tenant_id=t.id, db=db)

            await planning_goal_dao.set_status(goal, new_status="PL_APPROVED", db=db)
            with pytest.raises(ClosedCodeError):
                await planning_goal_dao.set_status(goal, new_status="PL_BOGUS", db=db)

            # goals die with their run (ON DELETE CASCADE, design P2): delete
            # the run in the same transaction, the goal list is empty.
            await db.delete(run)
            await db.flush()
            assert await planning_goal_dao.list_goals_for_run(run.id, db=db) == []


# ---------------------------------------------------------------------------
# MilestoneDAO — closed kind + UNIQUE(run, seq)
# ---------------------------------------------------------------------------
async def test_milestone_closed_kind_and_seq_unique(pl_db) -> None:
    await _ensure_seeded(pl_db)
    t, p = _FIXTURE.tenant, _FIXTURE.project  # type: ignore[union-attr]
    rev = _rev("sha-ms")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            run = await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            m0 = await milestone_dao.add_milestone(
                Milestone(planning_run_id=run.id, kind="phase", seq=0, title="m0", tenant_id=t.id),
                tenant_id=t.id,
                db=db,
            )
            # P9: unknown kind fails closed.
            with pytest.raises(ClosedCodeError):
                await milestone_dao.add_milestone(
                    Milestone(planning_run_id=run.id, kind="checkmate", seq=1, title="bad", tenant_id=t.id),
                    tenant_id=t.id,
                    db=db,
                )
            # §6.4: two buckets at the same seq — contained in a savepoint.
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await milestone_dao.add_milestone(
                        Milestone(planning_run_id=run.id, kind="gate", seq=0, title="m0b", tenant_id=t.id),
                        tenant_id=t.id,
                        db=db,
                    )
            m1 = await milestone_dao.add_milestone(
                Milestone(planning_run_id=run.id, kind="gate", seq=1, title="gate-1", tenant_id=t.id),
                tenant_id=t.id,
                db=db,
            )
            buckets = await work_package_dao.list_milestones_for_run(run.id, db=db)
            assert [m.id for m in buckets] == [m0.id, m1.id]  # seq order


# ---------------------------------------------------------------------------
# WorkPackageDAO — execution_mode / task_scope slot contract + listing
# ---------------------------------------------------------------------------
def _wp(run_id: uuid.UUID, goal_id: uuid.UUID, tenant_id: uuid.UUID, **kw) -> WorkPackage:
    base = {
        "planning_run_id": run_id,
        "planning_goal_id": goal_id,
        "title": "wp",
        "execution_mode": "serial",
        "tenant_id": tenant_id,
    }
    base.update(kw)
    return WorkPackage(**base)


async def test_work_package_validation_and_milestone_listing(pl_db) -> None:
    await _ensure_seeded(pl_db)
    t, p, agent = _FIXTURE.tenant, _FIXTURE.project, _FIXTURE.agent  # type: ignore[union-attr]
    rev = _rev("sha-wp")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            run = await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            goal = PlanningGoal(planning_run_id=run.id, title="g", tenant_id=t.id)
            await planning_goal_dao.add_goal(goal, tenant_id=t.id, db=db)
            ms = await milestone_dao.add_milestone(
                Milestone(planning_run_id=run.id, kind="phase", seq=0, title="p1", tenant_id=t.id),
                tenant_id=t.id,
                db=db,
            )

            slots = [
                {
                    "slot": "s1",
                    "kind": "build",
                    "required_capabilities": ["backend", "code"],
                    "candidate_agent_ids": [str(agent.id)],
                    "depends_on_slots": [],
                    "shared_resources": {"files": ["a/b.py"]},
                },
                {"slot": "s2", "kind": "review", "candidate_agent_ids": [str(agent.id)]},
            ]
            wp = await work_package_dao.add_package(
                _wp(run.id, goal.id, t.id, milestone_id=ms.id, task_scope=slots),
                tenant_id=t.id,
                db=db,
            )
            assert wp.execution_mode == "serial" and wp.requires_independent_review is False

            # execution_mode closed set (P12).
            with pytest.raises(ClosedCodeError):
                await work_package_dao.add_package(
                    _wp(run.id, goal.id, t.id, title="bad-mode", execution_mode="chaos"),
                    tenant_id=t.id,
                    db=db,
                )
            # task_scope slot contract (squad §4.1).
            for bad_slots, why in (
                ([{"slot": "s1", "kind": "hack"}], "unknown slot kind"),
                ([{"slot": "s1"}, {"slot": "s1"}], "duplicate slot id"),
                ([{"slot": 7}], "non-string slot id"),
                ([{"slot": "s1", "kind": "build", "required_capabilities": ["quantum"]}], "unknown capability"),
                ([{"slot": "s1", "candidate_agent_ids": "not-a-list"}], "candidate ids not a list"),
            ):
                with pytest.raises(ClosedCodeError):
                    await work_package_dao.add_package(
                        _wp(run.id, goal.id, t.id, title=f"bad-{why}", task_scope=bad_slots),
                        tenant_id=t.id,
                        db=db,
                    )

            # A second, unbucketed WP: milestone listing distinguishes the two.
            wp2 = await work_package_dao.add_package(_wp(run.id, goal.id, t.id, title="wp-unbucketed"), tenant_id=t.id, db=db)
            by_ms = await work_package_dao.list_work_packages_by_milestone(ms.id, db=db)
            assert [w.id for w in by_ms] == [wp.id]
            by_run = await work_package_dao.list_work_packages_for_run(run.id, db=db)
            assert {w.id for w in by_run} == {wp.id, wp2.id}


# ---------------------------------------------------------------------------
# WorkPackageTaskDAO — invariants §6.2/§6.3 + assignment-candidate read
# ---------------------------------------------------------------------------
async def test_link_invariants_and_assignment_candidates(pl_db) -> None:

    await _ensure_seeded(pl_db)
    t, p, agent, user = _FIXTURE.tenant, _FIXTURE.project, _FIXTURE.agent, _FIXTURE.user  # type: ignore[union-attr]
    rev = _rev("sha-link")
    with tenant_context(t.id):
        async with pl_db() as db, db.begin():
            run = await planning_run_dao.open_run(_run(t.id, p.id, rev), tenant_id=t.id, db=db)
            goal = PlanningGoal(
                planning_run_id=run.id,
                title="g",
                required_capabilities=["backend"],
                tenant_id=t.id,
            )
            await planning_goal_dao.add_goal(goal, tenant_id=t.id, db=db)
            wp = await work_package_dao.add_package(_wp(run.id, goal.id, t.id), tenant_id=t.id, db=db)

            # open slot (task_id NULL, materialized_at NULL) is legal
            slot = WorkPackageTask(work_package_id=wp.id, tenant_id=t.id)
            await work_package_task_dao.add_slot(slot, tenant_id=t.id, db=db)

            # ck_wp_tasks_materialized: a timestamp without a task fails closed
            bad = WorkPackageTask(work_package_id=wp.id, materialized_at=datetime.now(UTC), tenant_id=t.id)
            with pytest.raises(ClosedCodeError):
                await work_package_task_dao.add_slot(bad, tenant_id=t.id, db=db)

            # materialize the slot onto a real task (frozen Task row)
            task = Task(
                agent_id=agent.id,
                created_by=user.id,
                tenant_id=t.id,
                project_id=p.id,
                title="f071 planned task",
                status="pending",
                created_reason="ANALYSIS_PLANNING",
                revision_sha=rev,
            )
            db.add(task)
            await db.flush()
            filled = await work_package_task_dao.materialize_slot(slot, task_id=task.id, db=db)
            assert filled.task_id == task.id and filled.materialized_at is not None
            # re-pointing a filled slot at a DIFFERENT task fails closed
            with pytest.raises(ClosedCodeError):
                other = Task(agent_id=agent.id, created_by=user.id, tenant_id=t.id, title="other", status="pending")
                db.add(other)
                await db.flush()
                await work_package_task_dao.materialize_slot(slot, task_id=other.id, db=db)

            # uq_wp_tasks (§6.2): a second slot cannot point at the same task
            second = WorkPackageTask(work_package_id=wp.id, task_id=task.id, tenant_id=t.id)
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await work_package_task_dao.add_slot(second, tenant_id=t.id, db=db)

            # SET NULL + §6.3 CHECK interaction (model docstring): a task
            # delete that a materialized slot references is rejected
            # fail-closed; the intent-preserving path re-opens the slot first.
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(delete(Task).where(Task.id == task.id))

            await work_package_task_dao.reopen_slot(slot, db=db)
            await db.execute(delete(Task).where(Task.id == task.id))
            slots_now = await work_package_task_dao.list_slots_for_package(wp.id, db=db)
            assert len(slots_now) == 1 and slots_now[0].task_id is None

            # re-link for the assignment-INPUT read: a fresh task on the goal.
            task2 = Task(
                agent_id=agent.id,
                created_by=user.id,
                tenant_id=t.id,
                project_id=p.id,
                title="f071 planned task 2",
                status="pending",
                created_reason="ANALYSIS_PLANNING",
                revision_sha=rev,
            )
            db.add(task2)
            await db.flush()
            await work_package_task_dao.materialize_slot(slot, task_id=task2.id, db=db)

            # the assignment INPUT read (design §10): WP + goal capabilities +
            # linked tasks + their single Agent binding (Task.agent_id, D2)
            view = await work_package_dao.get_assignment_candidates_for_work_package(wp.id, db=db)
            assert view is not None
            assert view["work_package"].id == wp.id
            assert view["goal"].id == goal.id
            assert view["required_capabilities"] == ["backend"]
            assert [tq.id for tq in view["linked_tasks"]] == [task2.id]
            assert [a.id for a in view["candidate_agents"]] == [agent.id]
            slots = await work_package_task_dao.list_slots_for_package(wp.id, db=db)
            assert [s.id for s in slots] == [slot.id]
            assert await work_package_dao.get_assignment_candidates_for_work_package(uuid.uuid4(), db=db) is None

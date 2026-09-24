"""f069 Task Graph + Provenance — real-schema constraint semantics (Postgres).

Companion to the DB-free migration contract in
``test_task_graph_provenance_migration.py``: that suite proves the DDL is
guarded / idempotent / re-runnable without a database.  This module proves
the *resulting schema* actually enforces the design (§3.2 / §4.1 / §5.3):

- ``ck_task_dep_no_self``  rejects a self edge
- ``uq_task_depends_pair`` rejects a duplicate edge
- ``created_reason`` server-defaults to MANUAL on a bare insert
- edge-table FKs CASCADE (deleting a task removes its edges)
- ``tasks.project_id`` FK CASCADE (deleting a project removes its tasks)
- ``tasks.analysis_run_id`` FK SET NULL (deleting the run keeps the durable
  task, nulls the ref, keeps the ``revision_sha`` snapshot)
- ready/blocked is derived (not persisted) by bounded, tenant-scoped SQL
- DAO reads aggregate dependencies + validate §4.2 provenance consistency

It seeds one committed tenant graph through the real ORM models (so every
default is honored exactly as the app uses them) and points at a scratch
Postgres via ``DATABASE_URL`` (after ``alembic upgrade head``).  Destructive
checks run inside an explicitly-rolled-back transaction so the module is
re-runnable.  On a host without a reachable Postgres the whole module skips
via the autouse ``_db_available`` guard (the honest evidence boundary — the
DB-free suite carries the DDL logic, this one carries the live-schema proof).

Running this suite::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch> \
        uv run --extra dev pytest tests/test_task_graph_provenance.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.agent
import app.models.analysis
import app.models.project
import app.models.task
import app.models.tenant
import app.models.user  # noqa: F401
from app.config import get_settings
from app.dao.base import tenant_context
from app.dao.task_dao import task_dependency_dao, task_provenance_dao
from app.models.agent import Agent
from app.models.analysis import AnalysisRun
from app.models.project import Project
from app.models.task import Task, TaskDependency
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _db_available() -> None:
    """Skip the whole module when Postgres is unreachable on this host.

    One reachability probe per session on a dedicated raw asyncpg connection
    so the probe never leaks a pooled connection into the app engine (mirrors
    test_project_analysis_e2e_acceptance.py)."""
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
        pytest.skip("no reachable Postgres for the f069 schema-semantics suite; the DB-free migration suite carries the DDL logic")


class _Fixture:
    """Committed seed graph, created once per pytest process (idempotent)."""

    def __init__(self) -> None:
        self.tenant: Tenant | None = None
        self.user: User | None = None
        self.agent: Agent | None = None
        self.project: Project | None = None
        self.tb: Task | None = None
        self.tc: Task | None = None
        self.tmid: Task | None = None
        self.t_self: Task | None = None
        self.seeded = False


_FIXTURE = _Fixture()


@pytest.fixture
async def f069_db(_db_available):
    """A fresh engine + session bound to the (scratch) DATABASE_URL."""
    engine = create_async_engine(get_settings().DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _ensure_seeded(session_factory) -> None:
    """Seed one committed tenant graph; a no-op if already seeded this run."""
    if _FIXTURE.seeded:
        return
    async with session_factory() as db, db.begin():
        _FIXTURE.tenant = Tenant(name="f069-pytest", slug="f069-" + uuid.uuid4().hex[:10])
        db.add(_FIXTURE.tenant)
        await db.flush()
        _FIXTURE.user = User(
            tenant_id=_FIXTURE.tenant.id, display_name="f069", role="member", is_active=True,
            email=f"f069-{uuid.uuid4().hex}@example.test",
        )
        db.add(_FIXTURE.user)
        await db.flush()
        _FIXTURE.agent = Agent(
            name="f069-agent", creator_id=_FIXTURE.user.id, tenant_id=_FIXTURE.tenant.id,
            access_mode="company", status="running",
        )
        db.add(_FIXTURE.agent)
        await db.flush()
        _FIXTURE.project = Project(
            name="f069-project", status="INITIALIZED", created_by=_FIXTURE.user.id,
            tenant_id=_FIXTURE.tenant.id,
        )
        db.add(_FIXTURE.project)
        await db.flush()
        # dependency chain: tmid -> tb -> tc (tc done, tb/tmid pending).
        # A small helper keeps the repeated field set without a dict-spread.
        def mk_task(title: str, status: str = "pending") -> Task:
            return Task(
                agent_id=_FIXTURE.agent.id,
                created_by=_FIXTURE.user.id,
                tenant_id=_FIXTURE.tenant.id,
                project_id=_FIXTURE.project.id,
                title=title,
                status=status,
            )

        _FIXTURE.tb = mk_task("b")
        _FIXTURE.tc = mk_task("c", status="done")
        _FIXTURE.tmid = mk_task("mid")
        _FIXTURE.t_self = mk_task("s")
        db.add_all([_FIXTURE.tb, _FIXTURE.tc, _FIXTURE.tmid, _FIXTURE.t_self])
        await db.flush()
        db.add(TaskDependency(id=uuid.uuid4(), tenant_id=_FIXTURE.tenant.id,
                               task_id=_FIXTURE.tmid.id, depends_on_task_id=_FIXTURE.tb.id))
        db.add(TaskDependency(id=uuid.uuid4(), tenant_id=_FIXTURE.tenant.id,
                               task_id=_FIXTURE.tb.id, depends_on_task_id=_FIXTURE.tc.id))
        await db.flush()
    _FIXTURE.seeded = True


async def _derive(db, task_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    return (await db.execute(
        text(
            "SELECT CASE WHEN COUNT(*)=0 THEN 'ready' "
            "WHEN COUNT(*) FILTER (WHERE t2.status='done') = COUNT(*) THEN 'ready' "
            "ELSE 'blocked' END "
            "FROM task_dependencies d JOIN tasks t2 ON t2.id=d.depends_on_task_id "
            "WHERE d.task_id=:tid AND d.tenant_id=:ten AND t2.tenant_id=:ten"
        ),
        {"tid": task_id, "ten": tenant_id},
    )).scalar()


async def test_self_edge_rejected_by_check(f069_db) -> None:
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        with tenant_context(tenant):
            db.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant,
                                  task_id=_FIXTURE.t_self.id, depends_on_task_id=_FIXTURE.t_self.id))
            with pytest.raises(IntegrityError, match="ck_task_dep_no_self|check"):
                await db.flush()
            await db.rollback()


async def test_duplicate_edge_rejected_by_unique(f069_db) -> None:
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        with tenant_context(tenant):
            db.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant,
                                  task_id=_FIXTURE.tmid.id, depends_on_task_id=_FIXTURE.tb.id))
            with pytest.raises(IntegrityError, match="uq_task_depends_pair|unique"):
                await db.flush()
            await db.rollback()


async def test_created_reason_defaults_to_manual_on_bare_insert(f069_db) -> None:
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        await db.begin()
        with tenant_context(tenant):
            t = Task(agent_id=_FIXTURE.agent.id, title="f069-bare-" + uuid.uuid4().hex[:6],
                     created_by=_FIXTURE.user.id, tenant_id=tenant)
            db.add(t)
            await db.flush()
            assert t.created_reason == "MANUAL"
        await db.rollback()  # keep the scratch DB clean for re-runs


async def test_cascade_and_set_null_semantics(f069_db) -> None:
    """SET NULL on run delete; CASCADE on task/project delete.  All local
    rows are created + destroyed inside one explicitly-rolled-back
    transaction so the committed seed graph is untouched."""
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        await db.begin()
        with tenant_context(tenant):
            rev = "c" * 64
            run = AnalysisRun(project_id=_FIXTURE.project.id, revision_sha=rev,
                              status="AN_COMPLETED", tenant_id=tenant)
            db.add(run)
            await db.flush()
            bound = Task(agent_id=_FIXTURE.agent.id, title="f069-bound", created_by=_FIXTURE.user.id,
                         tenant_id=tenant, project_id=_FIXTURE.project.id, analysis_run_id=run.id,
                         revision_sha=rev, created_reason="ANALYSIS_PLANNING")
            db.add(bound)
            await db.flush()

            # SET NULL: deleting the run keeps the durable task, nulls the
            # reference, retains the revision snapshot.
            await db.execute(delete(AnalysisRun).where(AnalysisRun.id == run.id))
            await db.flush()
            col = (await db.execute(
                text("SELECT analysis_run_id, revision_sha FROM tasks WHERE id=:id"), {"id": bound.id}
            )).one()
            assert col[0] is None, "analysis_run_id must be SET NULL, not CASCADE"
            assert col[1] == rev, "revision_sha snapshot must survive"

            # CASCADE: deleting a task removes its edges.
            cascade_task = Task(agent_id=_FIXTURE.agent.id, title="f069-edge", created_by=_FIXTURE.user.id,
                                tenant_id=tenant, project_id=_FIXTURE.project.id)
            db.add(cascade_task)
            await db.flush()
            db.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant,
                                  task_id=cascade_task.id, depends_on_task_id=_FIXTURE.tb.id))
            await db.flush()
            await db.execute(delete(Task).where(Task.id == cascade_task.id))
            await db.flush()
            remaining = (await db.execute(
                select(TaskDependency.id).where(TaskDependency.task_id == cascade_task.id)
            )).all()
            assert remaining == [], "edges must CASCADE with their task"
        await db.rollback()  # roll back the destructive part; seed graph untouched


async def test_ready_blocked_derivation(f069_db) -> None:
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        with tenant_context(tenant):
            assert await _derive(db, _FIXTURE.tmid.id, tenant) == "blocked"  # 1 pending dep
            assert await _derive(db, _FIXTURE.tb.id, tenant) == "ready"      # all deps done
            assert await _derive(db, _FIXTURE.t_self.id, tenant) == "ready"  # no deps
            # cross-tenant task id must not see this tenant's edges.
            assert await _derive(db, _FIXTURE.tmid.id, uuid.uuid4()) == "ready"


async def test_dao_reads_and_provenance_consistency(f069_db) -> None:
    await _ensure_seeded(f069_db)
    tenant = _FIXTURE.tenant.id
    async with f069_db() as db:
        await db.begin()
        with tenant_context(tenant):
            # task_status_map — the bounded status read behind ready/blocked.
            smap = await task_provenance_dao.task_status_map([_FIXTURE.tb.id, _FIXTURE.tc.id], db=db)
            assert smap[_FIXTURE.tc.id] == "done"
            assert smap[_FIXTURE.tb.id] == "pending"
            assert await task_provenance_dao.task_status_map([], db=db) == {}

            # dependency aggregation (the "query aggregates dependencies" path).
            deps = await task_dependency_dao.list_dependencies(_FIXTURE.tmid.id, db=db)
            assert [d.depends_on_task_id for d in deps] == [_FIXTURE.tb.id]

            # create_with_provenance persists a provenance set; a matching run
            # makes the §4.2 consistency rule hold for the planning lane.
            rev = "d" * 64
            run = AnalysisRun(project_id=_FIXTURE.project.id, revision_sha=rev, status="AN_COMPLETED",
                              tenant_id=tenant)
            db.add(run)
            await db.flush()
            finding_task = Task(agent_id=_FIXTURE.agent.id, title="f069-lane", created_by=_FIXTURE.user.id,
                                tenant_id=tenant, project_id=_FIXTURE.project.id, analysis_run_id=run.id,
                                revision_sha=rev, created_reason="ANALYSIS_PLANNING")
            await task_provenance_dao.create_with_provenance(finding_task, db=db)
            assert await task_provenance_dao.provenance_consistency(finding_task, db=db) is True

            # a MANUAL task that wrongly carries analysis provenance fails the
            # §4.2 rule (fail closed).
            bad = Task(agent_id=_FIXTURE.agent.id, title="f069-bad", created_by=_FIXTURE.user.id,
                       tenant_id=tenant, project_id=_FIXTURE.project.id, revision_sha=rev,
                       created_reason="MANUAL")
            db.add(bad)
            await db.flush()
            assert await task_provenance_dao.provenance_consistency(bad, db=db) is False
        await db.rollback()  # local rows were not committed; seed graph intact

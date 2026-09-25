"""Phase 2D Task Graph API + Analysis→Task conversion API (card t_b4a29991).

Two tiers, mirroring the repo's evidence discipline:

- **DB-free transport tier** (always runs): the handlers are pure adapters,
  so every transport mapping is asserted with fakes — a blocked ``GRAPH_*``
  outcome -> 404/409, the ``/graph`` payload carries Provenance + Status,
  a failed conversion outcome -> 409, a tenant violation -> 403, success ->
  the 201 shape with the G4 ``status="pending"`` invariant.
- **Live-schema tier** (Postgres-guarded, skips when no DB): a real ASGI
  client over the real app proves the E2E paths — edge authoring, the graph
  view, and the conversion **with the no-enqueue assertion** (G4: a
  converted Task never produces an ``AgentRun``; Task != Run).

Running the live tier::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch@f070> \
        uv run --extra dev pytest tests/test_task_graph_api.py
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# DB-FREE TRANSPORT TIER
# ---------------------------------------------------------------------------


def _task_ns(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "title": "t",
        "status": "pending",
        "created_reason": "MANUAL",
        "project_id": None,
        "analysis_run_id": None,
        "finding_id": None,
        "revision_sha": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user_ns() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")


def _agent_ns(tenant_id: uuid.UUID | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)


async def _add_dependencies_outcome(state: str, code: str | None, detail: str = ""):
    from unittest.mock import AsyncMock, patch

    from app.api.tasks import add_task_dependencies
    from app.schemas.task_graph import TaskDependenciesIn
    from app.services.task_graph_service import GraphEdgeOutcome

    outcome = GraphEdgeOutcome(state=state, code=code, detail=detail)
    dep_id = uuid.uuid4()
    task = _task_ns()
    user = _user_ns()

    class _FakeGraphService:
        async def bulk_add_edges(self, db, **kwargs: Any) -> GraphEdgeOutcome:
            assert kwargs["tenant_id"] == task.tenant_id
            return outcome

    async def _call():
        return await add_task_dependencies(
            agent_id=task.agent_id,
            task_id=task.id,
            data=TaskDependenciesIn(depends_on_task_ids=[dep_id]),
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )

    patches = [
        patch("app.api.tasks.check_agent_access", new=AsyncMock(return_value=(_agent_ns(task.tenant_id), "manage"))),
        patch("app.api.tasks.task_provenance_dao", new=SimpleNamespace(get_scoped=AsyncMock(return_value=task))),
        patch("app.api.tasks.task_graph_service", new=_FakeGraphService()),
    ]
    for p in patches:
        p.start()
    try:
        if state == "added":
            resp = await _call()
            assert resp == {"task_id": task.id, "added": [str(dep_id)], "state": "added"}
            return
        with pytest.raises(HTTPException) as exc:
            await _call()
    finally:
        for p in patches:
            p.stop()
    expected = 404 if code == "GRAPH_NOT_FOUND" else 409
    assert exc.value.status_code == expected, (code, exc.value.status_code)
    assert exc.value.detail == {"code": code, "message": detail}


@pytest.mark.parametrize(
    "code",
    [
        "GRAPH_NOT_FOUND",
        "GRAPH_SELF",
        "GRAPH_MISMATCH_TENANT",
        "GRAPH_MISMATCH_PROJECT",
        "GRAPH_SUPERVISION_NOT_ALLOWED",
        "GRAPH_CYCLE",
        "GRAPH_EXISTS",
        "GRAPH_INVALID",
    ],
)
async def test_add_dependencies_closed_codes_map_404_409(code: str) -> None:
    await _add_dependencies_outcome("blocked", code, detail=code.lower())
    if code == "GRAPH_NOT_FOUND":
        # covered in the 404 branch above; no extra assertion needed
        return
    await _add_dependencies_outcome("blocked", code, detail=code.lower())


async def test_add_dependencies_success_201() -> None:
    await _add_dependencies_outcome("added", None)


async def test_remove_missing_edge_maps_to_404() -> None:
    from unittest.mock import AsyncMock, patch

    from app.api.tasks import remove_task_dependency
    from app.services.task_graph_service import GraphEdgeOutcome

    dep_id = uuid.uuid4()
    task = _task_ns()
    user = _user_ns()

    class _FakeGraphService:
        async def remove_edge(self, db, **kwargs: Any) -> GraphEdgeOutcome:
            assert kwargs["tenant_id"] == task.tenant_id
            return GraphEdgeOutcome(state="blocked", code="GRAPH_NOT_FOUND", detail="edge not found")

    async def _call():
        return await remove_task_dependency(
            agent_id=task.agent_id,
            task_id=task.id,
            dep_task_id=dep_id,
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )

    patches = [
        patch("app.api.tasks.check_agent_access", new=AsyncMock(return_value=(_agent_ns(task.tenant_id), "manage"))),
        patch("app.api.tasks.task_provenance_dao", new=SimpleNamespace(get_scoped=AsyncMock(return_value=task))),
        patch("app.api.tasks.task_graph_service", new=_FakeGraphService()),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(HTTPException) as exc:
            await _call()
    finally:
        for p in patches:
            p.stop()
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "GRAPH_NOT_FOUND"


async def test_graph_view_carries_provenance_status_and_readiness() -> None:
    from unittest.mock import AsyncMock, patch

    from app.api.tasks import get_task_graph
    from app.schemas.schemas import TaskOut
    from app.services.task_graph_service import ReadinessOutcome

    task = _task_ns(
        project_id=uuid.uuid4(),
        analysis_run_id=uuid.uuid4(),
        finding_id=uuid.uuid4(),
        revision_sha="f" * 64,
        created_reason="ANALYSIS_FINDING",
        status="pending",
    )
    user = _user_ns()
    now = datetime.now(UTC)
    dep = uuid.uuid4()
    view = ReadinessOutcome(
        task_id=task.id,
        state="blocked",
        direct_dependencies=[{"id": dep, "status": "pending"}],
        blocking=[dep],
    )

    class _FakeGraphService:
        async def graph(self, db, **kwargs: Any) -> ReadinessOutcome:
            assert kwargs["tenant_id"] == task.tenant_id
            return view

    task_out = TaskOut(
        id=task.id,
        agent_id=task.agent_id,
        title=task.title,
        type="todo",
        status=task.status,
        priority="medium",
        assignee="self",
        created_by=user.id,
        created_at=now,
        updated_at=now,
        project_id=task.project_id,
        analysis_run_id=task.analysis_run_id,
        finding_id=task.finding_id,
        revision_sha=task.revision_sha,
        created_reason=task.created_reason,
    )

    with (
        patch("app.api.tasks.check_agent_access", new=AsyncMock(return_value=(_agent_ns(task.tenant_id), "manage"))),
        patch("app.api.tasks.task_provenance_dao", new=SimpleNamespace(get_scoped=AsyncMock(return_value=task))),
        patch("app.api.tasks.task_graph_service", new=_FakeGraphService()),
        patch("app.api.tasks._enrich_task_out", new=AsyncMock(return_value=task_out)),
    ):
        out = await get_task_graph(
            agent_id=task.agent_id,
            task_id=task.id,
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )
    # The response shape: readiness state + the full task (Provenance + Status).
    assert out.ready == "blocked"
    assert [e.id for e in out.direct_dependencies] == [dep]
    assert out.blocking == [dep]
    # Provenance + Status travel in the embedded TaskOut (design §4/§6): the
    # five provenance fields are present on the payload, not hidden.
    assert out.task.project_id == task.project_id
    assert out.task.analysis_run_id == task.analysis_run_id
    assert out.task.finding_id == task.finding_id
    assert out.task.revision_sha == task.revision_sha
    assert out.task.created_reason == task.created_reason
    assert out.task.status == "pending"


# --- conversion transport mapping ------------------------------------------


async def _conversion_fake(db: Any, project: Any, run: Any, agent: Any, current_user: Any):
    from app.services.task_decomposition_service import DecompositionOutcome

    return DecompositionOutcome(
        state="ok",
        code="TD_OK",
        detail="",
        converted_task_ids=[uuid.uuid4(), uuid.uuid4()],
        per_finding={uuid.uuid4(): "converted", uuid.uuid4(): "planning_only"},
        counts={"converted": 2, "skipped_duplicate": 0, "planning_only": 1},
    )


async def test_conversion_success_shape_201_pending() -> None:
    from unittest.mock import AsyncMock, patch

    from app.api.projects import convert_analysis_run_to_tasks
    from app.schemas.task_graph import TaskConversionRequest
    from app.services import task_decomposition_service as tds_module

    project = _task_ns(status="ANALYZING")
    run = SimpleNamespace(id=uuid.uuid4(), project_id=project.id, revision_sha="a" * 64)
    agent = _agent_ns(project.tenant_id)
    user = _user_ns()

    with (
        patch("app.api.projects._load_authorized_project", new=AsyncMock(return_value=project)),
        patch("app.api.projects._load_authorized_run", new=AsyncMock(return_value=run)),
        patch("app.api.projects.check_agent_access", new=AsyncMock(return_value=(agent, "manage"))),
        patch.object(tds_module.task_decomposition_service, "convert", new=AsyncMock(side_effect=_conversion_fake)),
    ):
        out = await convert_analysis_run_to_tasks(
            project_id=project.id,
            run_id=run.id,
            data=TaskConversionRequest(agent_id=agent.id),
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )
    assert out.code == "TD_OK" and out.state == "ok"
    assert out.status == "pending"  # G4 invariant: converted tasks are never enqueued
    assert out.project_id == project.id and out.analysis_run_id == run.id and out.agent_id == agent.id
    assert len(out.converted_task_ids) == 2
    assert out.counts["planning_only"] == 1


async def test_conversion_failed_gate_maps_409() -> None:
    from unittest.mock import AsyncMock, patch

    from app.api.projects import convert_analysis_run_to_tasks
    from app.schemas.task_graph import TaskConversionRequest
    from app.services import task_decomposition_service as tds_module
    from app.services.task_decomposition_service import TD_RUN_NOT_COMPLETED, DecompositionOutcome

    project = _task_ns(status="ANALYZING")
    run = SimpleNamespace(id=uuid.uuid4(), project_id=project.id, revision_sha="a" * 64)
    agent = _agent_ns(project.tenant_id)
    user = _user_ns()

    failed = DecompositionOutcome(state="failed", code=TD_RUN_NOT_COMPLETED, detail="run is AN_OPEN")

    async def _call():
        return await convert_analysis_run_to_tasks(
            project_id=project.id,
            run_id=run.id,
            data=TaskConversionRequest(agent_id=agent.id),
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )

    patches = [
        patch("app.api.projects._load_authorized_project", new=AsyncMock(return_value=project)),
        patch("app.api.projects._load_authorized_run", new=AsyncMock(return_value=run)),
        patch("app.api.projects.check_agent_access", new=AsyncMock(return_value=(agent, "manage"))),
        patch.object(tds_module.task_decomposition_service, "convert", new=AsyncMock(return_value=failed)),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(HTTPException) as exc:
            await _call()
    finally:
        for p in patches:
            p.stop()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == TD_RUN_NOT_COMPLETED


async def test_conversion_tenant_violation_maps_403() -> None:
    from unittest.mock import AsyncMock, patch

    from app.api.projects import convert_analysis_run_to_tasks
    from app.schemas.task_graph import TaskConversionRequest
    from app.services import task_decomposition_service as tds_module
    from app.services.task_decomposition_service import DecompositionSecurity

    project = _task_ns(status="ANALYZING")
    run = SimpleNamespace(id=uuid.uuid4(), project_id=project.id, revision_sha="a" * 64)
    agent = _agent_ns(uuid.uuid4())  # a DIFFERENT tenant
    user = _user_ns()

    async def _call():
        return await convert_analysis_run_to_tasks(
            project_id=project.id,
            run_id=run.id,
            data=TaskConversionRequest(agent_id=agent.id),
            current_user=user,
            db=None,  # type: ignore[arg-type]
        )

    patches = [
        patch("app.api.projects._load_authorized_project", new=AsyncMock(return_value=project)),
        patch("app.api.projects._load_authorized_run", new=AsyncMock(return_value=run)),
        patch("app.api.projects.check_agent_access", new=AsyncMock(return_value=(agent, "manage"))),
        patch.object(
            tds_module.task_decomposition_service,
            "convert",
            new=AsyncMock(side_effect=DecompositionSecurity("cross-tenant agent")),
        ),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(HTTPException) as exc:
            await _call()
    finally:
        for p in patches:
            p.stop()
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# LIVE-SCHEMA E2E TIER (Postgres-guarded; skip = the honest evidence boundary)
# ---------------------------------------------------------------------------

LIVE = pytest.mark.usefixtures("_e2e_db_available")


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Reset the app's shared engine between live tests.

    Each pytest test runs on a fresh event loop; pooled asyncpg connections
    bound to a closed loop would otherwise surface as "Event loop is closed"
    on the next request (the Phase 2C e2e suite carries the same fixture)."""
    from app.database import engine

    yield
    await engine.dispose()


@pytest.fixture
def _e2e_db_available() -> None:
    """Skip the live tier when no reachable Postgres is configured for it."""
    import asyncio

    import asyncpg

    from app.config import get_settings

    if getattr(_e2e_db_available, "_result", None) is None:  # type: ignore[attr-defined]
        async def _probe() -> bool:
            dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
            try:
                conn = await asyncpg.connect(dsn)
                await conn.execute("select 1")
                await conn.close()
                return True
            except Exception:  # noqa: BLE001
                return False

        loop = asyncio.new_event_loop()
        try:
            _e2e_db_available._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    if not _e2e_db_available._result:  # type: ignore[attr-defined]
        pytest.skip("no reachable Postgres for the task-graph API E2E; the DB-free tier carries the transport logic")


@pytest.fixture
def e2e_ac() -> Generator[httpx.AsyncClient, None, None]:
    """One ASGI transport over the real app (no lifespan: Redis-free here)."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


class _E2ESeed:
    """Committed tenant graph, created once per pytest process (idempotent)."""

    def __init__(self) -> None:
        self.tenant: Any = None
        self.user: Any = None
        self.agent: Any = None
        self.project: Any = None
        self.run_completed: Any = None
        self.run_open: Any = None
        self.findings: list[Any] = []
        self.seeded = False


_E2E = _E2ESeed()


async def _e2e_seed() -> None:
    """Seed one committed tenant + a project with an AN_COMPLETED run, an
    AN_OPEN run, and findings across the closed grid (the graph lane needs
    tenant-aligned todo tasks; the conversion lane needs executable/planning
    findings)."""
    from app.database import async_session as DB_SESSION

    if _E2E.seeded:
        return
    from app.models.agent import Agent
    from app.models.analysis import AnalysisFinding, AnalysisRun
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.models.user import User

    async with DB_SESSION() as s, s.begin():
        _E2E.tenant = Tenant(name="b4a29991", slug="b4a29991-" + uuid.uuid4().hex[:10])
        s.add(_E2E.tenant)
        await s.flush()
        _E2E.user = User(
            tenant_id=_E2E.tenant.id,
            display_name="b4a29991",
            role="member",
            is_active=True,
            email=f"b4a29991-{uuid.uuid4().hex}@example.test",
        )
        s.add(_E2E.user)
        await s.flush()
        _E2E.agent = Agent(
            name="b4a29991-agent",
            creator_id=_E2E.user.id,
            tenant_id=_E2E.tenant.id,
            access_mode="company",
            status="running",
        )
        s.add(_E2E.agent)
        await s.flush()
        _E2E.project = Project(
            name="b4a29991-proj",
            status="ANALYZING",
            created_by=_E2E.user.id,
            tenant_id=_E2E.tenant.id,
        )
        s.add(_E2E.project)
        await s.flush()
        _E2E.run_completed = AnalysisRun(
            project_id=_E2E.project.id,
            revision_sha="c" * 64,
            status="AN_COMPLETED",
            tenant_id=_E2E.tenant.id,
        )
        _E2E.run_open = AnalysisRun(
            project_id=_E2E.project.id,
            revision_sha="o" * 64,
            status="AN_OPEN",
            tenant_id=_E2E.tenant.id,
        )
        s.add_all([_E2E.run_completed, _E2E.run_open])
        await s.flush()
        for i, (cat, sev, tag, label) in enumerate(
            [
                ("TECH_DEBT", "WARN", "FACT", "e1-executable"),
                ("SECURITY", "CRITICAL", "FACT", "e2-executable"),
                ("RISK", "HIGH", "FACT", "risk-planning"),
                ("TECH_DEBT", "INFO", "FACT", "info-planning"),
            ]
        ):
            f = AnalysisFinding(
                analysis_run_id=_E2E.run_completed.id,
                severity=sev,
                category=cat,
                tag=tag,
                summary=f"{label} summary",
                evidence={"anchors": [f"{label}.py:{i}"]},
                tenant_id=_E2E.tenant.id,
            )
            s.add(f)
            _E2E.findings.append(f)
        await s.flush()
    _E2E.seeded = True


def _e2e_headers() -> dict[str, str]:
    return _auth_headers(_E2E.tenant.id, _E2E.user)


def _auth_headers(tenant_id: uuid.UUID, user: Any) -> dict[str, str]:
    from app.core.security import create_access_token

    token = create_access_token(user_id=str(user.id), role=user.role, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


async def _seed_tasks(
    titles: dict[str, str],
    *,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Seed todo Task rows (status values from ``titles`` keys) + flush/commit."""
    from app.database import async_session as DB_SESSION
    from app.models.task import Task

    created: dict[str, Any] = {}
    async with DB_SESSION() as s, s.begin():
        for key, status in titles.items():
            t = Task(
                agent_id=agent_id,
                created_by=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                title=f"b4a29991-{key}",
                status=status,
            )
            s.add(t)
            created[key] = t
            await s.flush()
    return created


@LIVE
async def test_graph_endpoints_e2e(e2e_ac: httpx.AsyncClient) -> None:
    """Edge authoring + the bounded graph view, over the real app."""
    await _e2e_seed()
    headers = _e2e_headers()
    tasks = await _seed_tasks(
        {"up-done": "done", "down": "pending"},
        project_id=_E2E.project.id,
        tenant_id=_E2E.tenant.id,
        agent_id=_E2E.agent.id,
        user_id=_E2E.user.id,
    )
    up, down = tasks["up-done"], tasks["down"]

    # 1) ready when the only dependency is done + provenance/status on the view.
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies",
        json={"depends_on_task_ids": [str(up.id)]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["added"] == [str(up.id)]

    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/graph", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] == "ready"
    assert body["blocking"] == []
    assert body["direct_dependencies"] == [{"id": str(up.id), "status": "done"}]
    # Provenance + Status on the embedded task (design §4/§6).
    assert body["task"]["project_id"] == str(_E2E.project.id)
    assert body["task"]["created_reason"] == "MANUAL"
    assert body["task"]["status"] == "pending"

    # 2) self-dependency -> 409 GRAPH_SELF (closed code, nothing written).
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies",
        json={"depends_on_task_ids": [str(down.id)]},
        headers=headers,
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GRAPH_SELF", r.text

    # 3) duplicate edge -> 409 GRAPH_EXISTS.
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies",
        json={"depends_on_task_ids": [str(up.id)]},
        headers=headers,
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GRAPH_EXISTS", r.text

    # 4) unknown dependency -> 404 GRAPH_NOT_FOUND (a referenced task absent
    #    for this tenant — the documented 404 mapping, never a disclosure).
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies",
        json={"depends_on_task_ids": [str(uuid.uuid4())]},
        headers=headers,
    )
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GRAPH_NOT_FOUND", r.text

    # 5) remove the edge -> 204; the graph view re-derives ready trivially.
    r = await e2e_ac.delete(f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies/{up.id}", headers=headers)
    assert r.status_code == 204, r.text
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/graph", headers=headers)
    assert r.json() == {
        "task": r.json()["task"],
        "ready": "ready",
        "direct_dependencies": [],
        "blocking": [],
    }, r.text
    # Removing a non-existent edge -> 404.
    r = await e2e_ac.delete(f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies/{up.id}", headers=headers)
    assert r.status_code == 404, r.text

    # 6) unknown task -> 404 on every graph endpoint.
    missing = str(uuid.uuid4())
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{missing}/graph", headers=headers)
    assert r.status_code == 404, r.text

    # 7) a blocked view: re-add the edge, keep the dep pending via a second
    #    not-done task.
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/dependencies",
        json={"depends_on_task_ids": [str(up.id)]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "added"  # up is done again -> still "ready"
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{down.id}/graph", headers=headers)
    assert r.json()["ready"] == "ready"  # up-done is a done dependency

    # 8) cycle: X -> Y then Y -> X.
    tasks2 = await _seed_tasks({"cx": "pending", "cy": "pending"}, **_seed_args())
    cx, cy = tasks2["cx"], tasks2["cy"]
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{cx.id}/dependencies",
        json={"depends_on_task_ids": [str(cy.id)]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    r = await e2e_ac.post(
        f"/api/agents/{_E2E.agent.id}/tasks/{cy.id}/dependencies",
        json={"depends_on_task_ids": [str(cx.id)]},
        headers=headers,
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GRAPH_CYCLE", r.text
    # The cycle was REFUSED (no cy->cx edge written).  The stored graph is
    # just cx->cy (cx depends on pending cy), so cx is blocked while cy —
    # with no stored dependencies — stays ready.  That blocked view is the
    # concrete evidence the cycle was not admitted.
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{cx.id}/graph", headers=headers)
    assert r.json()["ready"] == "blocked" and r.json()["blocking"] == [str(cy.id)], r.text
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{cy.id}/graph", headers=headers)
    assert r.json()["ready"] == "ready" and r.json()["direct_dependencies"] == [], r.text


def _seed_args() -> dict[str, Any]:
    return {
        "project_id": _E2E.project.id,
        "tenant_id": _E2E.tenant.id,
        "agent_id": _E2E.agent.id,
        "user_id": _E2E.user.id,
    }


@LIVE
async def test_conversion_e2e_pending_not_enqueued_and_idempotent(e2e_ac: httpx.AsyncClient) -> None:
    """G4 E2E: conversion creates pending, provenance-bearing Tasks and NEVER
    an AgentRun; a second invocation is all-skipped_duplicate."""
    await _e2e_seed()
    headers = _e2e_headers()

    r = await e2e_ac.post(
        f"/api/projects/{_E2E.project.id}/analysis/{_E2E.run_completed.id}/tasks",
        json={"agent_id": str(_E2E.agent.id)},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "TD_OK"
    assert body["status"] == "pending"  # G4: the created tasks are pending
    assert body["counts"] == {"converted": 2, "skipped_duplicate": 0, "planning_only": 2}, body
    assert len(body["converted_task_ids"]) == 2
    # The planning findings are reported, never converted.
    assert body["per_finding"][str(_E2E.findings[2].id)] == "planning_only"
    assert body["per_finding"][str(_E2E.findings[3].id)] == "planning_only"

    from app.database import async_session as DB_SESSION
    from app.models.agent_run import AgentRun
    from app.models.task import Task

    # The created rows: pending + full ANALYSIS_FINDING provenance + the agent
    # assignment (Task -> Assignment intent; Task != Run).
    async with DB_SESSION() as s:
        rows = (
            await s.execute(select(Task).where(Task.id.in_([uuid.UUID(x) for x in body["converted_task_ids"]])))
        ).scalars().all()
    assert len(rows) == 2
    for t in rows:
        assert t.status == "pending"
        assert t.created_reason == "ANALYSIS_FINDING"
        assert t.agent_id == _E2E.agent.id
        assert t.project_id == _E2E.project.id
        assert t.analysis_run_id == _E2E.run_completed.id
        assert t.finding_id is not None
        assert t.revision_sha == "c" * 64

    # THE no-enqueue assertion (G4, spec §7.4): no AgentRun/execution exists
    # for the converted tasks — conversion only produced the assignment intent.
    async with DB_SESSION() as s:
        runs = (
            await s.execute(
                select(AgentRun).where(
                    AgentRun.source_type == "task",
                    AgentRun.source_id.in_([str(t.id) for t in rows]),
                )
            )
        ).scalars().all()
    assert runs == [], "G4 violated: conversion must never enqueue a Run"

    # The graph view of a converted task carries the full provenance chain.
    first = body["converted_task_ids"][0]
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{first}/graph", headers=headers)
    assert r.status_code == 200, r.text
    g = r.json()["task"]
    assert g["created_reason"] == "ANALYSIS_FINDING"
    assert g["project_id"] == str(_E2E.project.id)
    assert g["analysis_run_id"] == str(_E2E.run_completed.id)
    assert g["revision_sha"] == "c" * 64
    assert r.json()["ready"] == "ready"  # no dependencies -> trivially ready

    # Idempotency (§4): a second invocation converts nothing.
    r = await e2e_ac.post(
        f"/api/projects/{_E2E.project.id}/analysis/{_E2E.run_completed.id}/tasks",
        json={"agent_id": str(_E2E.agent.id)},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body2 = r.json()
    assert body2["counts"]["skipped_duplicate"] == 2 and body2["counts"]["converted"] == 0, body2
    assert body2["converted_task_ids"] == []


@LIVE
async def test_conversion_run_not_completed_is_409(e2e_ac: httpx.AsyncClient) -> None:
    await _e2e_seed()
    headers = _e2e_headers()
    r = await e2e_ac.post(
        f"/api/projects/{_E2E.project.id}/analysis/{_E2E.run_open.id}/tasks",
        json={"agent_id": str(_E2E.agent.id)},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "TD_RUN_NOT_COMPLETED"


@LIVE
async def test_graph_supervision_not_applicable(e2e_ac: httpx.AsyncClient) -> None:
    """Supervision tasks carry no dependency edges -> not_applicable (§3.1)."""
    await _e2e_seed()
    headers = _e2e_headers()
    from app.database import async_session as DB_SESSION
    from app.models.task import Task

    async with DB_SESSION() as s, s.begin():
        t = Task(
            agent_id=_E2E.agent.id,
            created_by=_E2E.user.id,
            tenant_id=_E2E.tenant.id,
            project_id=_E2E.project.id,
            title="b4a29991-supervision",
            type="supervision",
            status="pending",
        )
        s.add(t)
        await s.flush()
    r = await e2e_ac.get(f"/api/agents/{_E2E.agent.id}/tasks/{t.id}/graph", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["ready"] == "not_applicable"

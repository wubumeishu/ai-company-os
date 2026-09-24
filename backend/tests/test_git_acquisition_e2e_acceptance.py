"""E2E acceptance test for Git Source Acquisition API (Phase 2B-4, card t_4874c3e7).

Design: ``docs/GIT_ACQ_DESIGN_V1.md`` §C.2 / §F.  Drives the NEW
``/api/projects/{project_id}/repositories/{repo_id}/acquire/{agent_id}``
POST + GET router through the **real FastAPI app over ASGI, real Postgres
(scratch DB, schema from the alembic 001→f067 chain), real local storage
backend** — no mocking of the app under test.  The service layer is already
covered DB-free by ``tests/test_git_acquisition_service.py`` (unit + real-git
local E2E); this file closes the *transport* surface that only the real
request path can exercise:

1. **Acquire happy path (local_git)**: a real ``git init`` repository is
   seeded as an UNVERIFIED ``local_git`` source row; POST acquire returns
   201 ``acquired`` + ``ACQ_OK`` + ``resolved_rev`` + the agent-scoped
   artifact key; the tar lands in the storage backend; the repository row is
   re-verified from the DB (``verified=True``, ``acq_result=ACQ_OK``, no
   credential in the locator); and — the card's core handoff — a later
   **materialize** call reads exactly that ONE artifact (no re-clone) and
   the original file bytes land under the agent's target subtree.
2. **GET status on a not-yet-attempted repo**: 200 ``pending``, no code,
   retryable — the closed transport state set reconstructs.
3. **Security rejection before any process**: a cloud-metadata-IP URL is a
   409 ``ACQ_SECURITY_REJECTED`` (permanent, never retried), the code is
   recorded in the locator, and no artifact is published.
4. **Tenant isolation**: a foreign-tenant target agent is a 403; an unknown
   repo / project id is a 404 (tenant invisibility, never a disclosure).
5. **The card's hard rule**: acquisition NEVER triggers a downstream Agent
   / Run / prompt — after a successful acquire there are zero new
   chat-session / task / schedule rows for the agent, and exactly ONE
   ``git_acquisition`` audit row.

Redis is the one dependency the materialize half of test 1 needs that this
host does not run; per the repo's established stub practice the lock layer's
``get_redis`` is pointed at the in-memory fake (same as
``test_materialization_e2e_acceptance.py``).

Running this suite: point ``DATABASE_URL`` at a scratch Postgres with the
alembic chain applied (``uv run alembic upgrade head``), e.g.::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_<card>_e2e \
        uv run --extra dev pytest tests/test_git_acquisition_e2e_acceptance.py

On a host without Postgres every test in this module skips (the autouse
``_db_available`` guard), so the suite is portable: the skip itself is the
evidence boundary, and the DB-free service suite carries the logic.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models.activity_log
import app.models.agent
import app.models.audit
import app.models.channel_config
import app.models.chat_session
import app.models.experience
import app.models.experience_reference
import app.models.gateway_message
import app.models.identity
import app.models.invitation_code
import app.models.llm
import app.models.notification
import app.models.onboarding
import app.models.org
import app.models.participant
import app.models.plaza
import app.models.project
import app.models.schedule
import app.models.session_context_state
import app.models.skill
import app.models.system_settings
import app.models.task
import app.models.tenant
import app.models.tenant_setting
import app.models.tool
import app.models.trigger
import app.models.trigger_execution
import app.models.user
import app.models.workspace
from app.core.security import create_access_token, get_current_user
from app.database import engine
from app.main import app
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.tenant import Tenant
from app.models.user import User
from app.services.storage_runtime import facade
from app.services.storage_runtime.local import LocalStorageBackend

DB_SESSION = async_sessionmaker(engine, expire_on_commit=False)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Redis fake (documented exception — no Redis server on this host)
# ---------------------------------------------------------------------------
class InMemoryWorkspaceRedis:
    """Implements exactly what ``workspace_locking`` uses: ``set nx`` and the
    owner-checked release ``eval`` script (the materialization E2E's fake)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.busy_keys: set[str] = set()

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx:
            if key in self.store or key in self.busy_keys:
                return False
            self.store[key] = value
            return True
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return int(self.store.pop(key, None) is not None)

    async def eval(self, _script: str, _numkeys: int, *keys_and_args: str, **_kw: object) -> object:
        key = keys_and_args[0]
        if self.store.get(key) in (None, ""):
            return 1
        self.store.pop(key, None)
        return 1

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch) -> InMemoryWorkspaceRedis:
    from app.services import workspace_locking

    fake = InMemoryWorkspaceRedis()

    async def _get_redis() -> InMemoryWorkspaceRedis:
        return fake

    monkeypatch.setattr(workspace_locking, "get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# Environment fixtures (mirrors the materialization E2E suite)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await engine.dispose()


@pytest.fixture
def _db_available() -> None:
    """Skip the whole module when Postgres is unreachable on this host.

    One reachability probe per session, done on a DEDICATED raw asyncpg
    connection (never the app engine) so the probe cannot leak a pooled,
    probe-loop-bound connection into the app engine that the test event
    loop later reuses (the "attached to a different loop" failure).  The
    skip is the honest evidence boundary — the DB-free service suite already
    proves the logic; this file proves the transport against a real stack."""
    if getattr(_db_available, "_result", None) is None:  # type: ignore[attr-defined]
        import asyncpg

        from app.config import get_settings

        async def _probe() -> bool:
            dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
            try:
                conn = await asyncpg.connect(dsn)
                await conn.execute("select 1")
                await conn.close()
                return True
            except Exception:  # noqa: BLE001 - any probe failure (no DB, no creds, no schema) means "skip"
                return False

        loop = asyncio.new_event_loop()
        try:
            _db_available._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    # Re-evaluate the skip on EVERY fixture call (the probe result is cached,
    # but the skip itself is not) so ALL tests in the module skip when the
    # DB is unreachable — not just the one whose call first ran the probe.
    if not _db_available._result:  # type: ignore[attr-defined]
        pytest.skip("no reachable Postgres for the API E2E acceptance; the DB-free service suite carries the logic evidence")


@pytest.fixture
def storage_backend(tmp_path, monkeypatch) -> LocalStorageBackend:
    """Real local storage backend rooted in the test's tmp_path."""
    root = tmp_path / "storage"
    root.mkdir()
    backend = LocalStorageBackend(str(root))
    monkeypatch.setattr(facade, "_storage_backend", backend)
    return backend


@pytest.fixture
def ac() -> httpx.AsyncClient:
    """One ASGI transport over the real app (no lifespan: Redis-free)."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client


def _auth_headers(tenant_id: uuid.UUID, user: User) -> dict[str, str]:
    """A Bearer JWT whose tenant claim the real TenantContextMiddleware
    decodes into the request's tenant ContextVar."""
    token = create_access_token(user_id=str(user.id), role=user.role, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


def _require_auth(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


# ---------------------------------------------------------------------------
# Seeding helpers (explicit row states — the gates are the units under test)
# ---------------------------------------------------------------------------
async def _seed_tenant_user(role: str = "member") -> tuple[Tenant, User]:
    async with DB_SESSION() as s:
        tenant = Tenant(name=f"gitacq-{uuid.uuid4().hex[:8]}", slug=f"gitacq-{uuid.uuid4().hex}")
        s.add(tenant)
        await s.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="GitAcq Prober",
            role=role,
            is_active=True,
            email=f"gitacq-{uuid.uuid4().hex[:10]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(tenant)
        return tenant, user


async def _seed_agent(tenant_id: uuid.UUID, user: User) -> Agent:
    async with DB_SESSION() as s:
        agent = Agent(
            name=f"gitacq-agent-{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=tenant_id,
            access_mode="company",
            status="running",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _seed_project_git_repo(
    tenant: Tenant,
    user: User,
    *,
    source_type: str,
    locator: dict,
) -> tuple[Project, Repository]:
    """Seed an INITIALIZED project with ONE unverified git source row."""
    async with DB_SESSION() as s:
        project = Project(
            name=f"gitacq-{uuid.uuid4().hex[:8]}",
            description="e2e",
            goal="e2e",
            status="INITIALIZED",
            created_by=user.id,
            tenant_id=tenant.id,
        )
        s.add(project)
        await s.flush()
        repo = Repository(
            project_id=project.id,
            source_type=source_type,
            locator=locator,
            display_name="git-src",
            verified=False,
            pending_verifier=False,
            tenant_id=tenant.id,
        )
        s.add(repo)
        await s.commit()
        await s.refresh(project)
        await s.refresh(repo)
        return project, repo


async def _repo_row(repo_id: uuid.UUID) -> Repository:
    async with DB_SESSION() as s:
        return (await s.execute(select(Repository).where(Repository.id == repo_id))).scalar_one()


def _make_local_git_repo(root: Path) -> str:
    """Create a REAL git repository under ``root`` (2 files, 1 commit).

    Returns the absolute path.  Uses ``-c`` config so it works on a host
    with no global git identity configured."""
    repo = root / "git-source"
    repo.mkdir(parents=True)
    (repo / "a.txt").write_text("alpha\n")
    (repo / "subdir").mkdir()
    (repo / "subdir" / "b.txt").write_text("beta\n")

    def _git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.email=e2e@example.test", "-c", "user.name=E2E", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("add", "a.txt", "subdir")
    _git("commit", "-q", "-m", "e2e: initial commit")
    return str(repo)


# ---------------------------------------------------------------------------
# 1. Acquire happy path: local_git -> 201 acquired -> materialize reads the ONE tar
# ---------------------------------------------------------------------------
async def test_acquire_local_git_then_materialize_reads_the_artifact(ac, storage_backend, fake_redis, _db_available, tmp_path) -> None:
    src = _make_local_git_repo(tmp_path / "srcdir")
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": src})
    _require_auth(user)
    headers = _auth_headers(tenant.id, user)
    try:
        # --- the POST acquire (the new Phase 2B-4 transport surface) -------
        resp = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["state"] == "acquired" and body["code"] == "ACQ_OK"
        assert body["retryable"] is False
        assert body["requested_ref"] is None  # the source declares no ref: the repo's own default branch
        assert body["resolved_rev"] and body["resolved_rev"] != ""
        expected_key = f"{agent.id}/.git-acq/{repo.id}/source.tar"
        assert body["artifact_key"] == expected_key
        # The artifact is a real bounded tar carrying ONLY the working tree.
        data = await storage_backend.read_bytes(expected_key)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
        assert {"a.txt", "subdir/b.txt"} <= set(names)
        assert not any(n.startswith(".git") for n in names)  # no metadata, no hooks

        # The repository row is verified, with the closed ACQ code recorded
        # and NO credential anywhere in the locator.
        row = await _repo_row(repo.id)
        assert row.verified is True and row.pending_verifier is False
        loc = row.locator or {}
        assert loc["acq_result"] == "ACQ_OK" and loc["acq_artifact"] == expected_key
        assert loc["resolved_rev"] == body["resolved_rev"]
        assert "token" not in str(loc).lower()

        # --- the GET status reconstructs the SAME closed state set --------
        g = await ac.get(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert g.status_code == 200, g.text
        gb = g.json()
        assert gb["state"] == "acquired" and gb["code"] == "ACQ_OK" and gb["artifact_key"] == expected_key

        # --- the card's core handoff: materialization reads the ONE tar,
        #     no re-clone, and the original bytes land in the target ------
        m = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert m.status_code == 201, m.text
        mrow = m.json()["repositories"][0]
        assert mrow["source_type"] == "local_git" and mrow["outcome"] == "SUCCESS"
        target = Path(storage_backend.root) / f"{agent.id}/projects/{project.id}/git-src/a.txt"
        assert target.read_text() == "alpha\n"
        sub = Path(storage_backend.root) / f"{agent.id}/projects/{project.id}/git-src/subdir/b.txt"
        assert sub.read_text() == "beta\n"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 2. GET status on a not-yet-attempted repo: 200 pending, no code, retryable
# ---------------------------------------------------------------------------
async def test_acquire_status_fresh_repo_is_pending_retryable(ac, _db_available) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="github", locator={"url": "https://github.com/octocat/Hello-World"})
    _require_auth(user)
    try:
        g = await ac.get(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=_auth_headers(tenant.id, user))
        assert g.status_code == 200, g.text
        body = g.json()
        assert body["state"] == "pending" and body["code"] is None and body["retryable"] is True
        assert body["artifact_key"] is None  # nothing was published
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 3. Security rejection: metadata-IP URL -> 409 ACQ_SECURITY_REJECTED, no I/O
# ---------------------------------------------------------------------------
async def test_acquire_metadata_ip_url_is_security_rejected_409(ac, storage_backend, _db_available) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(
        tenant,
        user,
        source_type="github",
        locator={"url": "https://169.254.169.254/latest/meta-data"},
    )
    _require_auth(user)
    try:
        resp = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=_auth_headers(tenant.id, user))
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["state"] == "failed" and body["code"] == "ACQ_SECURITY_REJECTED"
        assert body["retryable"] is False  # security findings are NEVER retried (card §21)
        assert body["artifact_key"] is None
        # The closed code is recorded in the locator; the repo stays unverified.
        row = await _repo_row(repo.id)
        assert row.verified is False
        assert (row.locator or {})["acq_result"] == "ACQ_SECURITY_REJECTED"
        # 0 I/O: the agent's staging subtree was never populated.
        staging = list((Path(storage_backend.root) / str(agent.id) / ".git-acq").rglob("*")) if (Path(storage_backend.root) / str(agent.id) / ".git-acq").exists() else []
        assert staging == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 4. Tenant isolation: foreign-tenant agent -> 404 (invisibility); unknown ids -> 404
# ---------------------------------------------------------------------------
async def test_acquire_foreign_tenant_agent_is_tenant_invisible_404(ac, _db_available) -> None:
    """A foreign-tenant agent is INVISIBLE to the tenant-scoped DAO, so the
    transport returns 404 — never a 403 that would disclose the agent's
    existence (the same tenant-invisibility rule the materialization E2E
    documents).  The service-level cross-tenant 403 (the M9 fourth gate) is
    covered DB-free in test_git_acquisition_service.py."""
    tenant_a, user_a = await _seed_tenant_user()
    agent_a = await _seed_agent(tenant_a.id, user_a)  # noqa: F841 (context: the caller's own agent exists)
    project, repo = await _seed_project_git_repo(tenant_a, user_a, source_type="local_git", locator={"path": "/tmp/never-visited"})
    tenant_b, user_b = await _seed_tenant_user()
    agent_b = await _seed_agent(tenant_b.id, user_b)  # foreign tenant's agent
    _require_auth(user_a)
    try:
        resp = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent_b.id}", headers=_auth_headers(tenant_a.id, user_a))
        assert resp.status_code == 404, resp.text  # tenant-invisibility: 404, not a 403 disclosure
        assert "Agent not found" in resp.json().get("detail", "")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_acquire_unknown_repo_or_project_is_404(ac, _db_available) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": "/tmp/never-visited"})
    _require_auth(user)
    headers = _auth_headers(tenant.id, user)
    try:
        other_repo = await ac.get(f"/api/projects/{project.id}/repositories/{uuid.uuid4()}/acquire/{agent.id}", headers=headers)
        assert other_repo.status_code == 404, other_repo.text
        other_project = await ac.get(f"/api/projects/{uuid.uuid4()}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert other_project.status_code == 404, other_project.text  # tenant-scoped invisibility
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 5. The card's hard rule: acquisition never starts a downstream Agent
# ---------------------------------------------------------------------------
async def test_acquisition_publishes_no_downstream_agent_execution(ac, storage_backend, _db_available, tmp_path) -> None:
    src = _make_local_git_repo(tmp_path / "srcdir5")
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": src})
    _require_auth(user)
    try:
        resp = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=_auth_headers(tenant.id, user))
        assert resp.status_code == 201, resp.text
        from app.models.chat_session import ChatSession
        from app.models.schedule import AgentSchedule
        from app.models.task import Task

        async with DB_SESSION() as s:
            # Plain row selects + len(): "zero downstream execution rows".
            sessions = (await s.execute(select(ChatSession).where(ChatSession.agent_id == agent.id))).scalars().all()
            tasks = (await s.execute(select(Task).where(Task.agent_id == agent.id))).scalars().all()
            schedules = (await s.execute(select(AgentSchedule).where(AgentSchedule.agent_id == agent.id))).scalars().all()
            audits = (
                await s.execute(select(AuditLog).where(AuditLog.agent_id == agent.id, AuditLog.action == "git_acquisition"))
            ).scalars().all()
        # Acquisition is an INDEPENDENT stage: the handoff is the artifact,
        # never a started Run / prompt / task / schedule (card §23).
        assert len(sessions) == 0 and len(tasks) == 0 and len(schedules) == 0
        assert len(audits) == 1  # exactly the one best-effort provenance row
        assert audits[0].details["result"] == "ACQ_OK"
        assert "ACQ" in resp.json()["code"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


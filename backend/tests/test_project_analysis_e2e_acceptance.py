"""Real-project E2E acceptance for the Phase 2C minimal Analysis persistence
(card t_37e2eb05, docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2).

Drives ONE real project through the entire card chain over the real FastAPI
app (ASGI) + real Postgres (scratch DB, schema from the alembic 001→f068
chain):

    Project (INITIALIZED)
      -> Source (a REAL local `git init` repo seeded as an UNVERIFIED
         local_git source)
      -> Revision (POST acquire writes locator.resolved_rev + verified=True)
      -> Analysis (POST analyze opens an AN_OPEN analysis_runs row bound to
         that revision_sha and moves the project to ANALYZING — OQ-6)
      -> Findings (POST .../findings records >=1 transient finding with a
         closed tag + path:line evidence, closes the run AN_COMPLETED)
      -> Stored Analysis (GET .../analysis reads back the CURRENT run + its
         findings and the HISTORY)
      -> Knowledge (POST .../promote writes a CONFIRMED project_knowledge row
         with provenance; GET .../knowledge reads it back)

The card's two hard rules are asserted here too:
- **Append-only versioning (OQ-5 / §9.2)**: a second commit at a NEW sha ->
  re-acquire -> re-launch produces a NEW analysis_runs row under
  UNIQUE(project_id, revision_sha); the read path returns the NEW row as
  "current" and the prior row in "history" (never a clobber).
- **Stage 11 (static-only, no project-code execution)**: after the full
  analysis + knowledge chain there are ZERO new chat-session / task /
  schedule rows for the target agent — the analysis path persists rows and
  stops; it never triggers a downstream Agent / Run / prompt / execution.

The test fixtures are copied from `test_git_acquisition_e2e_acceptance.py`
(the established E2E practice in this repo) so this module is self-contained
and portable: on a host without reachable Postgres every test here skips via
the autouse ``_db_available`` guard (skip = the honest evidence boundary; the
DB-free service suite carries the logic).

Running this suite (scratch Postgres with the chain applied)::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_2c_e2e \
        uv run --extra dev pytest tests/test_project_analysis_e2e_acceptance.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator
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
from app.models.project import Project, Repository
from app.models.tenant import Tenant
from app.models.user import User
from app.services.storage_runtime import facade
from app.services.storage_runtime.local import LocalStorageBackend

DB_SESSION = async_sessionmaker(engine, expire_on_commit=False)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Redis fake (documented exception — the acquire half of the chain touches
# the lock layer; the analysis half is static-only and never needs Redis).
# ---------------------------------------------------------------------------
class InMemoryWorkspaceRedis:
    """Implements exactly what ``workspace_locking`` uses: ``set nx`` and the
    owner-checked release ``eval`` script (copied from the git-acq E2E)."""

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
# Environment fixtures (mirror test_git_acquisition_e2e_acceptance.py)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncGenerator[None, None]:
    yield
    await engine.dispose()


@pytest.fixture
def _db_available() -> None:
    """Skip the whole module when Postgres is unreachable on this host.

    One reachability probe per session on a DEDICATED raw asyncpg connection
    so the probe never leaks a pooled connection into the app engine.  The
    skip is re-evaluated on EVERY call so all tests in the module skip
    together when the DB is down (skip = the honest evidence boundary)."""
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
            except Exception:  # noqa: BLE001 - no DB / no creds / no schema all mean "skip"
                return False

        loop = asyncio.new_event_loop()
        try:
            _db_available._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    if not _db_available._result:  # type: ignore[attr-defined]
        pytest.skip("no reachable Postgres for the analysis API E2E; the DB-free service suite carries the logic evidence")


@pytest.fixture
def storage_backend(tmp_path, monkeypatch) -> LocalStorageBackend:
    """Real local storage backend rooted in the test's tmp_path."""
    root = tmp_path / "storage"
    root.mkdir()
    backend = LocalStorageBackend(str(root))
    monkeypatch.setattr(facade, "_storage_backend", backend)
    return backend


@pytest.fixture
def ac() -> Generator[httpx.AsyncClient, None, None]:
    """One ASGI transport over the real app (no lifespan: Redis-free)."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client


def _auth_headers(tenant_id: uuid.UUID, user: User) -> dict[str, str]:
    token = create_access_token(user_id=str(user.id), role=user.role, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


def _require_auth(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


# ---------------------------------------------------------------------------
# Seeding helpers (explicit row states — the gates are the units under test)
# ---------------------------------------------------------------------------
async def _seed_tenant_user(role: str = "member") -> tuple[Tenant, User]:
    async with DB_SESSION() as s:
        tenant = Tenant(name=f"an2c-{uuid.uuid4().hex[:8]}", slug=f"an2c-{uuid.uuid4().hex}")
        s.add(tenant)
        await s.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="An2C Prober",
            role=role,
            is_active=True,
            email=f"an2c-{uuid.uuid4().hex[:10]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(tenant)
        return tenant, user


async def _seed_agent(tenant_id: uuid.UUID, user: User) -> Agent:
    async with DB_SESSION() as s:
        agent = Agent(
            name=f"an2c-agent-{uuid.uuid4().hex[:8]}",
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
            name=f"an2c-{uuid.uuid4().hex[:8]}",
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


async def _project_row(project_id: uuid.UUID) -> Project:
    async with DB_SESSION() as s:
        return (await s.execute(select(Project).where(Project.id == project_id))).scalar_one()


def _git_repo_with_commit(root: Path, files: dict[str, str]) -> Path:
    """A real `git init` repo under ``root`` with the given files committed.

    Returns the repo path.  Uses ``-c`` config so it works on a host with no
    global git identity configured.  ``files`` maps relative path -> bytes
    (a NEW commit when the content differs, so the versioning test can push
    a second sha).
    """
    import subprocess

    repo = root / "git-source"
    repo.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=e2e@example.test", "-c", "user.name=E2E", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    # `git init` when the dir is not already a repo (the first-commit case),
    # so a later call with a second file pushes a NEW commit (the versioning
    # case) — exit 128 otherwise.
    if not os.path.exists(repo / ".git"):
        _git("init", "-q")
    _git("add", "-A")
    _git("commit", "-q", "-m", f"an2c: {list(files)[-1]}")
    return repo


# ---------------------------------------------------------------------------
# 1. Full chain: Project -> Source -> Revision -> Analysis -> Findings ->
#    Stored Analysis -> Knowledge, with the card's two hard rules asserted.
# ---------------------------------------------------------------------------
async def test_analysis_full_chain_project_to_stored(
    ac,
    storage_backend,
    fake_redis,
    _db_available,
    tmp_path,
) -> None:
    src_root = _git_repo_with_commit(tmp_path / "src", {"a.txt": "alpha\n", "subdir/b.txt": "beta\n"})
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": str(src_root)})
    _require_auth(user)
    headers = _auth_headers(tenant.id, user)
    try:
        # --- Source -> Revision: a real acquire writes the typed revision --
        acq = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert acq.status_code == 201, acq.text
        resolved_rev = acq.json()["resolved_rev"]
        assert resolved_rev and resolved_rev != ""
        # Revision is the typed OQ-5 carrier: written back into the locator.
        loc = (await _repo_row(repo.id)).locator or {}
        assert loc["resolved_rev"] == resolved_rev and loc["acq_result"] == "ACQ_OK"

        # --- Revision -> Analysis: open a run bound to that sha ------------
        launched = await ac.post(
            f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent.id}", headers=headers
        )
        assert launched.status_code == 201, launched.text
        lb = launched.json()
        assert lb["state"] == "launched" and lb["code"] == "AN_OK"
        assert lb["revision_sha"] == resolved_rev  # the typed binding
        run_id = lb["run"]["id"]
        assert lb["run"]["status"] == "AN_OPEN"
        # OQ-6: Phase 2C OWNS the ANALYZING transition (decided, not inferred).
        assert (await _project_row(project.id)).status == "ANALYZING"

        # --- Analysis -> Findings: >=1 transient finding + path:line -------
        findings = await ac.post(
            f"/api/projects/{project.id}/analysis/{run_id}/findings",
            json={
                "findings": [
                    {
                        "category": "TECH_DEBT",
                        "tag": "OBSERVATION",
                        "summary": "a.txt is plaintext config; no schema declared",
                        "severity": "INFO",
                        "evidence": {"anchors": ["a.txt:1"], "provenance": {"source_card": "local_git"}},
                    }
                ]
            },
            headers=headers,
        )
        assert findings.status_code == 201, findings.text
        fb = findings.json()
        assert fb["run_status"] == "AN_COMPLETED"  # the open run closed
        assert len(fb["findings"]) == 1
        assert fb["findings"][0]["tag"] == "OBSERVATION"
        assert fb["findings"][0]["evidence"]["anchors"] == ["a.txt:1"]

        # --- Stored Analysis: read back the CURRENT run + findings + history
        read = await ac.get(f"/api/projects/{project.id}/analysis", headers=headers)
        assert read.status_code == 200, read.text
        rb = read.json()
        assert rb["current"]["id"] == run_id and rb["current"]["status"] == "AN_COMPLETED"
        assert rb["current"]["revision_sha"] == resolved_rev
        assert len(rb["current_findings"]) == 1
        assert rb["history"] == []  # this was the FIRST run

        # --- Knowledge: promote a CONFIRMED row with provenance ------------
        promoted = await ac.post(
            f"/api/projects/{project.id}/analysis/{run_id}/promote",
            json={"subject": "config layout", "statement": "plain-text config under a.txt, no schema"},
            headers=headers,
        )
        assert promoted.status_code == 201, promoted.text
        kb = promoted.json()
        assert kb["status"] == "CONFIRMED" and kb["source_analysis_run_id"] == run_id
        knowledge = await ac.get(f"/api/projects/{project.id}/knowledge", headers=headers)
        assert knowledge.status_code == 200, knowledge.text
        assert knowledge.json()[0]["id"] == kb["id"] and knowledge.json()[0]["source_analysis_run_id"] == run_id

        # --- Stage 11 (hard): the whole chain triggered ZERO downstream
        #     Agent / Run / prompt / task / schedule rows for the agent.
        from app.models.chat_session import ChatSession
        from app.models.schedule import AgentSchedule
        from app.models.task import Task

        async with DB_SESSION() as s:
            sessions = (await s.execute(select(ChatSession).where(ChatSession.agent_id == agent.id))).scalars().all()
            tasks = (await s.execute(select(Task).where(Task.agent_id == agent.id))).scalars().all()
            schedules = (await s.execute(select(AgentSchedule).where(AgentSchedule.agent_id == agent.id))).scalars().all()
        assert len(sessions) == 0 and len(tasks) == 0 and len(schedules) == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 2. Append-only versioning (OQ-5 / §9.2): a NEW commit sha -> NEW row,
#    the prior run moves to "history", never clobbered.
# ---------------------------------------------------------------------------
async def test_analysis_new_revision_appends_a_row_not_a_clobber(
    ac,
    storage_backend,
    fake_redis,
    _db_available,
    tmp_path,
) -> None:
    src_root = _git_repo_with_commit(tmp_path / "src2", {"a.txt": "alpha\n"})
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": str(src_root)})
    _require_auth(user)
    headers = _auth_headers(tenant.id, user)
    try:
        # Run 1 @ sha-1.
        acq1 = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert acq1.status_code == 201, acq1.text
        sha1 = acq1.json()["resolved_rev"]
        run1 = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent.id}", headers=headers)
        assert run1.status_code == 201, run1.text
        assert run1.json()["revision_sha"] == sha1

        # Push a NEW commit -> a NEW sha, re-acquire -> the locator now holds
        # the newer revision.  A distinct new file guarantees the commit's
        # tree differs from sha1 (a duplicate-key dict here would have kept
        # only the second value — F601).
        _git_repo_with_commit(src_root.parent, {"a.txt": "alpha\n", "note.txt": "a second, distinct commit\n"})
        acq2 = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=headers)
        assert acq2.status_code == 201, acq2.text
        sha2 = acq2.json()["resolved_rev"]
        assert sha2 != sha1

        # Run 2 @ sha-2 (the project is ANALYZING; the gate allows a NEW
        # revision while ANALYZING — append-only versioning, §9.2).
        run2 = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent.id}", headers=headers)
        assert run2.status_code == 201, run2.text
        assert run2.json()["revision_sha"] == sha2
        assert run2.json()["run"]["id"] != run1.json()["run"]["id"]

        # "current" = the NEW sha-2 run; "history" = the prior sha-1 run.
        read = (await ac.get(f"/api/projects/{project.id}/analysis", headers=headers)).json()
        assert read["current"]["revision_sha"] == sha2
        assert [r["revision_sha"] for r in read["history"]] == [sha1]
        # The prior row is intact, not clobbered.
        assert read["history"][0]["id"] == run1.json()["run"]["id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 3. Fail closed: a source with no stored revision cannot start an analysis.
# ---------------------------------------------------------------------------
async def test_analysis_launch_unverified_source_is_409_source_invalid(ac, _db_available) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    # Never acquired: locator has no resolved_rev, verified=False.
    project, repo = await _seed_project_git_repo(
        tenant, user, source_type="local_git", locator={"path": "/tmp/never-acquired"}
    )
    _require_auth(user)
    try:
        resp = await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent.id}", headers=_auth_headers(tenant.id, user))
        assert resp.status_code == 409, resp.text
        assert resp.json()["state"] == "failed" and resp.json()["code"] == "AN_SOURCE_INVALID"
        # No run was opened; the project stayed INITIALIZED (the gate is
        # fail-closed, nothing half-written).
        assert (await _project_row(project.id)).status == "INITIALIZED"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 4. A terminal run refuses further findings (the transient window is closed).
# ---------------------------------------------------------------------------
async def test_analysis_findings_on_closed_run_is_409_run_not_open(
    ac, storage_backend, fake_redis, _db_available, tmp_path
) -> None:
    src_root = _git_repo_with_commit(tmp_path / "src4", {"a.txt": "alpha\n"})
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    project, repo = await _seed_project_git_repo(tenant, user, source_type="local_git", locator={"path": str(src_root)})
    _require_auth(user)
    try:
        await ac.post(f"/api/projects/{project.id}/repositories/{repo.id}/acquire/{agent.id}", headers=_auth_headers(tenant.id, user))
        launched = await ac.post(
            f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent.id}", headers=_auth_headers(tenant.id, user)
        )
        run_id = launched.json()["run"]["id"]
        # First record closes the run (AN_COMPLETED).
        f1 = await ac.post(
            f"/api/projects/{project.id}/analysis/{run_id}/findings",
            json={"findings": [{"category": "FACT", "tag": "FACT", "summary": "repo has a.txt", "evidence": {"anchors": ["a.txt:1"]}}]},
            headers=_auth_headers(tenant.id, user),
        )
        assert f1.status_code == 201, f1.text
        # A second record against the now-terminal run is refused.
        f2 = await ac.post(
            f"/api/projects/{project.id}/analysis/{run_id}/findings",
            json={"findings": [{"category": "FACT", "tag": "FACT", "summary": "too late", "evidence": {"anchors": ["a.txt:1"]}}]},
            headers=_auth_headers(tenant.id, user),
        )
        assert f2.status_code == 409, f2.text
        # The closed code rides the 409 body's ``detail`` dict (the same
        # shape the Intake 409 SECURITY_REJECTED uses in this module).
        assert f2.json()["detail"]["code"] == "AN_RUN_NOT_OPEN"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 5. Tenant isolation: a foreign-tenant target agent is invisible (404, not
#    a 403 disclosure) — the M9 fourth gate.
# ---------------------------------------------------------------------------
async def test_analysis_foreign_tenant_agent_is_tenant_invisible_404(ac, _db_available) -> None:
    tenant_a, user_a = await _seed_tenant_user()
    await _seed_agent(tenant_a.id, user_a)
    project, repo = await _seed_project_git_repo(tenant_a, user_a, source_type="local_git", locator={"path": "/tmp/x"})
    tenant_b, user_b = await _seed_tenant_user()
    agent_b = await _seed_agent(tenant_b.id, user_b)  # foreign tenant's agent
    _require_auth(user_a)
    try:
        resp = await ac.post(
            f"/api/projects/{project.id}/repositories/{repo.id}/analyze/{agent_b.id}", headers=_auth_headers(tenant_a.id, user_a)
        )
        assert resp.status_code == 404, resp.text  # tenant-invisibility
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# 6. A never-analyzed project reads back as current=null + empty history
#    (data, not an error — 200 always).
# ---------------------------------------------------------------------------
async def test_analysis_read_never_analyzed_is_empty(ac, _db_available) -> None:
    tenant, user = await _seed_tenant_user()
    project, _repo = await _seed_project_git_repo(
        tenant, user, source_type="github", locator={"url": "https://github.com/octocat/Hello-World"}
    )
    _require_auth(user)
    try:
        read = await ac.get(f"/api/projects/{project.id}/analysis", headers=_auth_headers(tenant.id, user))
        assert read.status_code == 200, read.text
        assert read.json()["current"] is None and read.json()["history"] == [] and read.json()["current_findings"] == []
        knowledge = await ac.get(f"/api/projects/{project.id}/knowledge", headers=_auth_headers(tenant.id, user))
        assert knowledge.status_code == 200 and knowledge.json() == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)

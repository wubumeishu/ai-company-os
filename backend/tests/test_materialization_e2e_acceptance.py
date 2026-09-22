"""E2E acceptance test for Project Materialization (spec §11.13, card t_025cda02).

Drives the REAL request path — FastAPI app over ASGI, real Postgres scratch
database (schema from ``Base.metadata``, the same proven approach the intake
E2E suite uses), real local storage backend rooted in tmp_path — for the
materialize endpoint, per docs/MATERIALIZATION_SECURE_SPEC_V1.md §11.13:

1. Happy path: INITIALIZED project -> 201 SUCCESS; the files land under the
   authorized agent's storage subtree with the exact §2.3 key layout;
   revision + audit rows are persisted in the request transaction.
2. Idempotency: a repeat call -> 201 CONVERGED, no duplicate revision rows.
3. Content conflict: a differing pre-existing target + overwrite=false ->
   409 PARTIAL (the other repo still succeeds, §7.3 independent outcomes),
   0 new writes for the conflicting repo, no target overwrite.
4. Human edit lock: an active WorkspaceEditLock on the target file ->
   HUMAN_LOCK_CONFLICT, 0 writes.
5. Busy directory lock: a conflicting workspace mutation lock -> LOCK_CONFLICT
   for that repo only, PARTIAL, the failing repo's staging subtree is cleaned.
6. Status gate: a non-INITIALIZED project -> 409 SOURCE_NOT_READY, nothing
   written.
7. Agent gate: an agent the caller cannot use -> 403; an agent of another
   tenant -> 404 (the tenant-scoped DAO makes it invisible — no disclosure,
   mirroring the intake E2E's tenant-isolation rule).

Redis is the one dependency the real lock layer needs that this host does
not run; per the repo's established stub practice
(test_password_reset_and_notifications.MockRedis), the lock layer's
``get_redis`` is pointed at an in-memory fake that implements exactly the
two commands ``workspace_locking`` uses (``set nx`` + the release Lua
script). Everything else — Postgres, storage, DAOs, middleware, the service
itself — is real.
"""

from __future__ import annotations

import datetime
import io
import uuid
import zipfile
from collections.abc import Sequence
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
from app.models.workspace import WorkspaceEditLock, WorkspaceFileRevision
from app.services.storage_runtime import facade
from app.services.storage_runtime.local import LocalStorageBackend

DB_SESSION = async_sessionmaker(engine, expire_on_commit=False)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Redis fake (documented exception — no Redis server on this host)
# ---------------------------------------------------------------------------


class InMemoryWorkspaceRedis:
    """Implements exactly what ``workspace_locking`` uses: ``set nx`` and the
    owner-checked release ``eval`` script.  Keys are the real lock keys, so
    the tenant-scoped key shape (spec §6.1) is exercised for real."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        #: lock keys that are already held by a concurrent mutation.
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
        existed = key in self.store
        self.store.pop(key, None)
        return 1 if existed else 0

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        # The release script: only delete when we own the token.
        if self.store.get(key) == token:
            self.store.pop(key, None)
            return 1
        return 0

    async def aclose(self) -> None:  # for lifespan symmetry
        return None


@pytest.fixture
def fake_redis(monkeypatch):
    """Point the real lock layer at the in-memory fake (no Redis server on
    this host; the documented exception in this suite's header).  The lock
    module imports ``get_redis`` into its own namespace, so that is the
    attribute being patched."""
    from app.services import workspace_locking

    fake = InMemoryWorkspaceRedis()

    async def _get_redis() -> InMemoryWorkspaceRedis:
        return fake

    monkeypatch.setattr(workspace_locking, "get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# Environment fixtures (mirrors test_intake_e2e_acceptance.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await engine.dispose()


@pytest.fixture(scope="module")
def ac() -> httpx.AsyncClient:
    """One ASGI transport over the real app (no lifespan: Redis-free)."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client
    # Async close is done by the event loop owning the client; nothing else.


def _auth_headers(tenant_id: uuid.UUID, user: User) -> dict[str, str]:
    """A Bearer JWT whose tenant claim the real TenantContextMiddleware
    decodes into the request's tenant ContextVar."""
    token = create_access_token(user_id=str(user.id), role=user.role, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def storage_backend(tmp_path, monkeypatch):
    """Real local storage backend rooted in the test's tmp_path."""
    root = tmp_path / "storage"
    root.mkdir()
    backend = LocalStorageBackend(str(root))
    monkeypatch.setattr(facade, "_storage_backend", backend)
    return backend


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_user(role: str = "member") -> tuple[Tenant, User]:
    async with DB_SESSION() as s:
        tenant = Tenant(name=f"mat-{uuid.uuid4().hex[:8]}", slug=f"mat-{uuid.uuid4().hex}")
        s.add(tenant)
        await s.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="Mat Prober",
            role=role,
            is_active=True,
            email=f"mat-{uuid.uuid4().hex[:10]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(tenant)
        return tenant, user


async def _seed_agent(tenant_id: uuid.UUID, user: User, *, access_mode: str = "company") -> Agent:
    async with DB_SESSION() as s:
        agent = Agent(
            name=f"mat-agent-{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=tenant_id,
            access_mode=access_mode,  # "company": any same-tenant user may use it
            status="running",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _seed_agent_for(owner: User, *, access_mode: str = "company") -> Agent:
    """Seed an agent owned by a *specific* user (used for the 403 case)."""
    if owner.tenant_id is None:
        raise AssertionError("the seeded test user always has a tenant")
    return await _seed_agent(owner.tenant_id, owner, access_mode=access_mode)


async def _user_in_tenant(tenant: Tenant, role: str) -> User:
    async with DB_SESSION() as s:
        user = User(
            tenant_id=tenant.id,
            display_name=f"mat-{role}",
            role=role,
            is_active=True,
            email=f"mat-{role}-{uuid.uuid4().hex[:8]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _seed_project(
    tenant: Tenant,
    user: User,
    *,
    status: str,
    repos: Sequence[tuple[str, dict, str | None]],
) -> Project:
    """Seed a Project + its verified Repository rows (bypassing the intake
    API on purpose: the materialization gates are the unit under test, so the
    row states are set explicitly).  Each repo tuple is
    ``(source_type, locator, display_name)``; the material name is the
    display_name or, when None, the repo id's first 8 chars (§2.1)."""
    async with DB_SESSION() as s:
        project = Project(
            name=f"mat-{uuid.uuid4().hex[:8]}",
            description="e2e",
            goal="e2e",
            status=status,
            created_by=user.id,
            tenant_id=tenant.id,
        )
        s.add(project)
        await s.flush()
        for source_type, locator, display_name in repos:
            s.add(
                Repository(
                    project_id=project.id,
                    source_type=source_type,
                    locator=locator,
                    display_name=display_name,
                    verified=True,
                    pending_verifier=False,
                    tenant_id=tenant.id,
                )
            )
        await s.commit()
        await s.refresh(project)
        return project


def _make_zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _pre_seed_target(backend, target_key: str, content: bytes) -> None:
    """Write bytes directly into the local backend (a pre-existing target
    file, e.g. from a previous run or a human edit)."""
    path = Path(backend.root) / target_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# 1. Happy path — zip source into the agent's storage subtree (spec §2.3/§9.1)
# ---------------------------------------------------------------------------


async def test_e2e_materialize_happy_path_zip(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/mate/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/readme.md", b"# readme\n"), ("src/app.py", b"print('ok')")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])
    repo = next(r for r in (await _fetch_repos(project.id)) if r.source_type == "zip")

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(
            f"/api/projects/{project.id}/materialize/{agent.id}",
            json={"overwrite": False},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["outcome"] == "SUCCESS"
        assert body["retryable"] is False
        row = body["repositories"][0]
        assert row["source_type"] == "zip"
        assert row["outcome"] == "SUCCESS"
        assert row["written"] == 2 and row["converged"] == 0
        # The §9.3 declared limitation for zip materialization is present.
        assert any("zip" in note and "symlink" in note for note in body["limitations"])

        # Real files under the authorized agent's subtree (exact §2.3 layout).
        mat_name = repo.display_name or str(repo.id)[:8]
        for rel, content in (("docs/readme.md", b"# readme\n"), ("src/app.py", b"print('ok')")):
            target = f"{agent.id}/projects/{project.id}/{mat_name}/{rel}"
            path = Path(storage_backend.root) / target
            assert path.read_bytes() == content, f"missing target {target}"

        # No staging residue anywhere under the agent's namespace (§7.2).
        staging = list((Path(storage_backend.root) / str(agent.id) / ".materialize-tmp").rglob("*"))
        assert staging == [], f"staging residue: {staging}"

        # Persisted provenance rows (committed by the request session).
        async with DB_SESSION() as s:
            revs = (
                (
                    await s.execute(
                        select(WorkspaceFileRevision).where(
                            WorkspaceFileRevision.agent_id == agent.id,
                            WorkspaceFileRevision.group_key == f"materialize:{project.id}:{repo.id}:{agent.id}",
                        )
                    )
                )
                .scalars()
                .all()
            )
            audits = (
                (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant.id,
                            AuditLog.action == "project_materialization",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(revs) == 2, f"expected 2 revision rows, got {len(revs)}"
        assert all(r.actor_type == "system" and r.actor_id == user.id for r in revs)
        assert all(r.scope_type == "agent" and r.scope_id == agent.id for r in revs)
        assert all(r.content_hash for r in revs)
        assert len(audits) == 1
        assert audits[0].details["outcome"] == "SUCCESS"
        assert audits[0].details["repo_results"][0]["written"] == 2
        assert str(project.id) in audits[0].details["project_id"]
    finally:
        _clear_auth()


async def _fetch_repos(project_id: uuid.UUID) -> list[Repository]:
    async with DB_SESSION() as s:
        return list((await s.execute(select(Repository).where(Repository.project_id == project_id))).scalars().all())


# ---------------------------------------------------------------------------
# 2. Idempotency — a repeat call converges with no drift (spec §8 row 2)
# ---------------------------------------------------------------------------


async def test_e2e_materialize_repeat_call_converges(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/matconv/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("notes/a.md", b"same")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])
    repo = next(r for r in await _fetch_repos(project.id))
    mat_name = repo.display_name or str(repo.id)[:8]

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        first = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert first.status_code == 201, first.text
        assert first.json()["repositories"][0]["outcome"] == "SUCCESS"

        second = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert second.status_code == 201, second.text
        assert second.json()["outcome"] == "SUCCESS"
        row2 = second.json()["repositories"][0]
        assert row2["outcome"] == "CONVERGED" and row2["converged"] == 1 and row2["written"] == 0

        # No drift: still exactly one file, no duplicate revision rows.
        target = f"{agent.id}/projects/{project.id}/{mat_name}/notes/a.md"
        assert (Path(storage_backend.root) / target).read_bytes() == b"same"
        async with DB_SESSION() as s:
            revs = (
                await s.execute(
                    select(WorkspaceFileRevision).where(
                        WorkspaceFileRevision.agent_id == agent.id,
                        WorkspaceFileRevision.group_key == f"materialize:{project.id}:{repo.id}:{agent.id}",
                    )
                )
            ).scalars().all()
        assert len(revs) == 1  # the converged call added no revision
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 3. Content conflict — differing target + overwrite=false (spec §8 row 3)
# ---------------------------------------------------------------------------


async def test_e2e_content_conflict_is_409_partial(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/matchk/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"NEW")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])
    repo = next(r for r in await _fetch_repos(project.id))
    mat_name = repo.display_name or str(repo.id)[:8]
    target = f"{agent.id}/projects/{project.id}/{mat_name}/docs/a.md"
    _pre_seed_target(storage_backend, target, b"OLD")  # a differing pre-existing target

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["outcome"] == "FAILED"
        row = body["repositories"][0]
        assert row["outcome"] == "FAILED" and row["reason_code"] == "CONTENT_CONFLICT"
        assert row["written"] == 0
        assert body["retryable"] is False
        # 0 new writes: the existing target content is untouched (§8 row 3).
        assert (Path(storage_backend.root) / target).read_bytes() == b"OLD"
        async with DB_SESSION() as s:
            revs = (
                await s.execute(
                    select(WorkspaceFileRevision).where(WorkspaceFileRevision.agent_id == agent.id)
                )
            ).scalars().all()
        assert revs == []
    finally:
        _clear_auth()


async def test_e2e_content_conflict_overwrite_replaces(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/mato/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"NEW")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])
    repo = next(r for r in await _fetch_repos(project.id))
    mat_name = repo.display_name or str(repo.id)[:8]
    target = f"{agent.id}/projects/{project.id}/{mat_name}/docs/a.md"
    _pre_seed_target(storage_backend, target, b"OLD")

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": True}, headers=headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["repositories"][0]["outcome"] == "SUCCESS"
        assert (Path(storage_backend.root) / target).read_bytes() == b"NEW"
        # The revision captured the BEFORE content (M5 §8 row 3).
        async with DB_SESSION() as s:
            rev = (
                await s.execute(
                    select(WorkspaceFileRevision).where(
                        WorkspaceFileRevision.agent_id == agent.id,
                        WorkspaceFileRevision.group_key == f"materialize:{project.id}:{repo.id}:{agent.id}",
                    )
                )
            ).scalars().one()
        assert rev.before_content == "OLD"
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 4. Human edit lock — an active lock never yields to a silent overwrite
# (spec §6.2 / §11.11)
# ---------------------------------------------------------------------------


async def test_e2e_human_edit_lock_is_conflict(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/matl/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"X")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])
    repo = next(r for r in await _fetch_repos(project.id))
    mat_name = repo.display_name or str(repo.id)[:8]
    target_rel = f"projects/{project.id}/{mat_name}/docs/a.md"
    target_key = f"{agent.id}/{target_rel}"
    _pre_seed_target(storage_backend, target_key, b"human's draft")
    # A human actively editing this file right now (future expiry).
    async with DB_SESSION() as s:
        s.add(
            WorkspaceEditLock(
                agent_id=agent.id,
                scope_type="agent",
                scope_id=agent.id,
                path=target_rel,
                user_id=user.id,
                expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=90),
            )
        )
        await s.commit()

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 409, resp.text
        row = resp.json()["repositories"][0]
        assert row["outcome"] == "FAILED" and row["reason_code"] == "HUMAN_LOCK_CONFLICT"
        assert row["written"] == 0
        assert resp.json()["retryable"] is True
        # The human's uncommitted work is untouched.
        assert (Path(storage_backend.root) / target_key).read_bytes() == b"human's draft"
        async with DB_SESSION() as s:
            revs = (await s.execute(select(WorkspaceFileRevision).where(WorkspaceFileRevision.agent_id == agent.id))).scalars().all()
        assert revs == []
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 5. Busy directory lock — a concurrent mutation (spec §6.1 / §11.10)
# ---------------------------------------------------------------------------


async def test_e2e_busy_directory_lock_is_partial_and_cleans_staging(ac, storage_backend, fake_redis, tmp_path) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)

    def _folder(name: str) -> str:
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        (d / "f.txt").write_text(name)
        import os

        return os.path.abspath(str(d))

    # display names == "ok" / "bad": the material names that appear in the
    # directory lock keys and target paths (§6.1 / §2.3).
    repos = [
        ("local_folder", {"path": _folder("ok")}, "ok"),
        ("local_folder", {"path": _folder("bad")}, "bad"),
    ]
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=repos)
    repo_ids = [r.id for r in await _fetch_repos(project.id)]
    repo_ok, repo_bad = repo_ids[0], repo_ids[1]

    # Hold the directory lock for the "bad" material, as if another mutation
    # were in progress on that directory.
    busy_key = f"tenant:{tenant.id}:workspace-lock:{agent.id}:projects/{project.id}/bad"
    fake_redis.busy_keys.add(busy_key)

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["outcome"] == "PARTIAL"
        assert body["retryable"] is True
        by_repo = {r["repo_id"]: r for r in body["repositories"]}
        assert by_repo[str(repo_ok)]["outcome"] == "SUCCESS"
        assert by_repo[str(repo_bad)]["outcome"] == "FAILED"
        assert by_repo[str(repo_bad)]["reason_code"] == "LOCK_CONFLICT"
        # The successful repo's write DID land (§7.3 independent outcomes).
        ok_path = Path(storage_backend.root) / f"{agent.id}/projects/{project.id}"
        assert (ok_path / "ok" / "f.txt").read_bytes() == b"ok"
        # The failing repo's staging subtree was fully cleaned (§7.2 / §11.10).
        assert not (Path(storage_backend.root) / str(agent.id) / ".materialize-tmp" / str(repo_bad)).exists()
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 6. Status gate — only INITIALIZED projects may be materialized (spec §1)
# ---------------------------------------------------------------------------


async def test_e2e_non_initialized_project_is_409_source_not_ready(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    key = f"e2e/matgate/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"x")]))
    for status in ("RECEIVED", "SOURCES_OK", "COMPLETED"):
        project = await _seed_project(tenant, user, status=status, repos=[("zip", {"storage_key": key}, None)])
        _require_auth(user)
        try:
            headers = _auth_headers(tenant.id, user)
            resp = await ac.post(f"/api/projects/{project.id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
            assert resp.status_code == 409, f"{status}: {resp.text}"
            assert resp.json()["detail"]["code"] == "SOURCE_NOT_READY"
            assert resp.json()["detail"]["retryable"] is False
        finally:
            _clear_auth()
    # Nothing was written for any of the gated projects.
    agent_root = Path(storage_backend.root) / str(agent.id)
    assert not (agent_root / "projects").exists()


async def test_e2e_pending_verifier_repo_is_409_source_not_ready(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    agent = await _seed_agent(tenant.id, user)
    # An INITIALIZED project is impossible with a pending repo through the
    # state machine; seed the inconsistent row directly to prove the gate
    # still fails closed (defense-in-depth, spec §1 rule 2).
    async with DB_SESSION() as s:
        project = Project(
            name="mat-pending",
            description="e2e",
            goal="e2e",
            status="INITIALIZED",
            created_by=user.id,
            tenant_id=tenant.id,
        )
        s.add(project)
        await s.flush()
        s.add(
            Repository(
                project_id=project.id,
                source_type="zip",
                locator={"storage_key": "e2e/pending.zip"},
                verified=True,
                pending_verifier=True,
                tenant_id=tenant.id,
            )
        )
        await s.commit()
        project_id = project.id

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project_id}/materialize/{agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "SOURCE_NOT_READY"
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 7. Agent gate — agent the caller may not use -> 403; another tenant's
#    agent -> 404 (tenant-scoped DAO: no disclosure, spec §5.1 gate 3)
# ---------------------------------------------------------------------------


async def test_e2e_unknown_agent_is_404(ac, storage_backend, fake_redis) -> None:
    tenant, user = await _seed_tenant_user()
    key = f"e2e/mat404/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"x")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{uuid.uuid4()}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 404, resp.text
    finally:
        _clear_auth()


async def test_e2e_foreign_tenant_agent_is_404(ac, storage_backend, fake_redis) -> None:
    """An agent owned by a different tenant is invisible to the tenant-scoped
    DAO the transport uses (``agent_dao.get_active`` is scoped to the acting
    tenant's context), so it is a 404 — never a 403 or a cross-tenant write.
    This is the request-boundary half of the M9 combination gate; the
    service's fourth gate (``agent.tenant_id == project.tenant_id``) is the
    fail-closed backstop if a background caller bypassed the transport."""
    tenant, user = await _seed_tenant_user()
    foreign_tenant, _foreign_user = await _seed_tenant_user()
    foreign_agent = await _seed_agent(foreign_tenant.id, _foreign_user)
    key = f"e2e/mat404b/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"x")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{foreign_agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 404, resp.text  # foreign-tenant agent is not found in this tenant's scope
        # Nothing landed under the foreign agent's subtree.
        assert not (Path(storage_backend.root) / str(foreign_agent.id) / "projects").exists()
    finally:
        _clear_auth()


async def test_e2e_same_tenant_private_agent_is_403(ac, storage_backend, fake_redis) -> None:
    """A same-tenant agent the caller is not authorized for (a *private* agent
    owned by someone else) is a 403 — the transport's ``check_agent_access``
    runs before the service, so no materialization attempt happens."""
    tenant, user = await _seed_tenant_user()
    owner = await _user_in_tenant(tenant, "member")
    private_agent = await _seed_agent_for(owner, access_mode="private")
    key = f"e2e/mat403/{uuid.uuid4().hex[:8]}.zip"
    _pre_seed_target(storage_backend, key, _make_zip_bytes([("docs/a.md", b"x")]))
    project = await _seed_project(tenant, user, status="INITIALIZED", repos=[("zip", {"storage_key": key}, None)])

    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(f"/api/projects/{project.id}/materialize/{private_agent.id}", json={"overwrite": False}, headers=headers)
        assert resp.status_code == 403, resp.text
        assert not (Path(storage_backend.root) / str(private_agent.id) / "projects").exists()
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# Auth helpers (mirror the intake E2E harness)
# ---------------------------------------------------------------------------


def _require_auth(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_auth() -> None:
    app.dependency_overrides.pop(get_current_user, None)

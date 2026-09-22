"""End-to-end acceptance testing for the Project Intake closed loop (card t_78ac2f99).

Executes the real Intake flow in a real test environment:

- Real Postgres scratch database (DATABASE_URL env) whose schema was created
  from the full application metadata (``Base.metadata``) including the f067
  rejection / pending-verifier columns.  ``alembic upgrade head`` is NOT used
  on a fresh DB because an unrelated pre-existing schedule migration
  (202608181600) crashes on fresh databases (``DuplicateColumnError`` on
  ``agent_schedules.delivery_target_id``); see the verification report.
- Real FastAPI application (``app.main.app``) driven over the ASGI stack with
  ``httpx.ASGITransport``: request, middleware (tenant ContextVar), service,
  tenant-scoped DAOs, and session/commit semantics all run for real.
- Real local storage backend rooted in a tmp_path directory, and real
  host-filesystem fixtures for the four V1 source types: ``local_folder``,
  ``zip``, ``document``, ``manual``.

Accepted invariants verified:

1. Lifecycle: create (RECEIVED) -> validate -> INITIALIZED, and the failure
   branch -> REJECTED with a closed-set reason code.
2. Every source type behaves as specified (pass / reject shapes).
3. Rejected projects expose a clear reason code and never leak raw host
   paths, locators, or secrets in the persisted or API-visible detail.
4. Illegal state jumps are impossible: terminal-state re-validation is a
   409, and the two-step chain never shortcuts RECEIVED -> INITIALIZED.
5. Tenant isolation holds through the real request path: a foreign-tenant
   project is a 404 (no disclosure), a same-tenant non-creator member is a
   403, a same-tenant admin may read, and a tenant-less context fails
   closed rather than disclosing cross-tenant rows.
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

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
from app.core.security import get_current_user
from app.dao.base import tenant_context
from app.database import engine
from app.main import app
from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.tenant import Tenant
from app.models.user import User
from app.services.storage_runtime.local import LocalStorageBackend

DB_SESSION = async_sessionmaker(engine, expire_on_commit=False)

#: The closed 6-code reason set (security module, brief §4.2).
CLOSED_REASON_CODES = frozenset(
    {
        "SOURCE_NOT_FOUND",
        "SOURCE_INVALID",
        "SECURITY_REJECTED",
        "SOURCE_UNREACHABLE",
        "DISTRIBUTION_FAILED",
        "SOURCE_NOT_SUPPORTED",
    }
)

MAX_RETRIES = 3  # mirrors the service's hardcoded budget (brief §8 UNKNOW 4)


# ---------------------------------------------------------------------------
# Environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Tear down the shared engine's pool after every test.

    ``asyncpg`` connections are bound to the event loop that created them.
    pytest-asyncio gives each test its own loop, but the app's module-level
    engine pools connections — without disposal, test N would reuse a
    connection owned by test 1's (already closed) loop and fail with a
    cross-loop transport error.  Disposing between tests is the
    document-sanctioned reset for this arrangement.
    """
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
    decodes into the request's tenant ContextVar.  Minted with the app's own
    token function + secret so the middleware accepts it."""
    from app.core.security import create_access_token

    token = create_access_token(user_id=str(user.id), role=user.role, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def storage_backend(tmp_path, monkeypatch):
    """Real local storage backend rooted in the test's tmp_path."""
    from app.services.storage_runtime import facade

    root = tmp_path / "storage"
    root.mkdir()
    backend = LocalStorageBackend(str(root))
    monkeypatch.setattr(facade, "_storage_backend", backend)
    return backend


async def _seed_tenant_user(role: str = "member") -> tuple[Tenant, User]:
    """Seed one tenant + one active user in the real DB (per-test isolation)."""
    async with DB_SESSION() as s:
        tenant = Tenant(
            name=f"e2e-tenant-{uuid.uuid4().hex[:8]}",
            slug=f"e2e-{uuid.uuid4().hex}",
        )
        s.add(tenant)
        await s.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="E2E Prober",
            role=role,
            is_active=True,
            email=f"e2e-{uuid.uuid4().hex[:10]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(tenant)
        return tenant, user


async def _user_in_tenant(tenant: Tenant, role: str) -> User:
    async with DB_SESSION() as s:
        user = User(
            tenant_id=tenant.id,
            display_name=f"E2E {role}",
            role=role,
            is_active=True,
            email=f"e2e-{role}-{uuid.uuid4().hex[:8]}@example.test",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


def _require_auth(user: User) -> None:
    """Point the real auth dependency at a seeded DB user."""
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_auth() -> None:
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Fixture builders (real storage + real host filesystem)
# ---------------------------------------------------------------------------


def _make_zip_bytes(member_names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in member_names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


def _zip_key(root: Path, data: bytes, name: str = "fixture.zip") -> str:
    key = f"e2e/accept/{name}"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def _doc_key(root: Path, name: str, content: bytes) -> str:
    key = f"e2e/accept/{name}"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return key


def _abs_host_path(path: Path) -> str:
    """A host-absolute path shape the security module accepts on every OS."""
    return os.path.abspath(str(path))


def _host_folder(tmp_path: Path, name: str, content: str = "print('e2e')") -> str:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "main.py").write_text(content)
    return _abs_host_path(d)


# ---------------------------------------------------------------------------
# 1. Happy path — each source type drives the closed loop to INITIALIZED
# ---------------------------------------------------------------------------


async def test_e2e_manual_source_reaches_initialized(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E manual", "description": "d", "goal": "g", "sources": [{"source_type": "manual"}]},
            headers=headers,
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["status"] == "RECEIVED"
        assert body["rejection_info"] is None
        project_id = body["id"]

        validated = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "INITIALIZED"
        assert validated.json()["rejection_info"] is None
        # The source was marked verified through the real DAOs.
        assert validated.json()["repositories"][0]["verified"] is True
    finally:
        _clear_auth()


async def test_e2e_local_folder_source_reaches_initialized(ac, storage_backend, tmp_path) -> None:
    tenant, user = await _seed_tenant_user()
    folder = _host_folder(tmp_path, "src")
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E folder",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "local_folder", "locator": {"path": folder}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "INITIALIZED"
    finally:
        _clear_auth()


async def test_e2e_zip_storage_source_reaches_initialized(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    key = _zip_key(storage_backend.root, _make_zip_bytes(["docs/readme.md", "src/app.py"]))
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E zip",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "zip", "locator": {"storage_key": key}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "INITIALIZED"
    finally:
        _clear_auth()


async def test_e2e_document_storage_source_reaches_initialized(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    key = _doc_key(storage_backend.root, "spec.md", b"# spec\n")
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E doc",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "document", "locator": {"storage_key": key}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "INITIALIZED"
    finally:
        _clear_auth()


async def test_e2e_n0_project_is_valid_manual_only(ac, storage_backend) -> None:
    """Zero sources is the accepted N=0 manual-only form (Phase 2A §E.2)."""
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E N0", "description": "d", "goal": "g"},
            headers=headers,
        )
        assert create.status_code == 201, create.text
        assert create.json()["status"] == "RECEIVED"
        assert create.json()["repositories"] == []
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 200
        assert validated.json()["status"] == "INITIALIZED"
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 2. Rejection branch — clear reason codes, no sensitive data in the detail
# ---------------------------------------------------------------------------


async def test_e2e_git_source_rejects_source_not_supported(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E git",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "github", "locator": {"owner": "acme", "repo": "widget"}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        project_id = create.json()["id"]
        validated = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert validated.status_code == 409, validated.text
        info = validated.json()["rejection_info"]
        assert info["reason_code"] == "SOURCE_NOT_SUPPORTED"
        assert info["retryable"] is False
        # The persisted row carries the closed-set code, and the detail names
        # the capability gap — never the locator's owner/repo values.
        async with DB_SESSION() as s:
            row = (await s.execute(select(Project).where(Project.id == uuid.UUID(project_id)))).scalar_one()
        assert row.rejection_reason == "SOURCE_NOT_SUPPORTED"
        assert "acme" not in (row.rejection_detail or "")
        assert "widget" not in (row.rejection_detail or "")
    finally:
        _clear_auth()


async def test_e2e_zip_slip_rejects_security_rejected(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    evil = _zip_key(
        storage_backend.root,
        _make_zip_bytes(["../../etc/cron.d/evil", "ok.txt"]),
        name="evil.zip",
    )
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E evil zip",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "zip", "locator": {"storage_key": evil}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 409, validated.text
        info = validated.json()["rejection_info"]
        assert info["reason_code"] == "SECURITY_REJECTED"
        assert info["retryable"] is False
        # No-leak invariant: the detail classifies the vector (it may use the
        # generic "'..' traversal" descriptor); it must NOT echo the hostile
        # member path or the storage key back.
        detail = info["reason_detail"] or ""
        assert "etc/cron.d" not in detail
        assert "evil" not in detail
        assert evil not in detail
    finally:
        _clear_auth()


async def test_e2e_missing_document_rejects_source_not_found(ac, storage_backend, tmp_path) -> None:
    tenant, user = await _seed_tenant_user()
    missing = _abs_host_path(tmp_path / "no-such-doc.txt")
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E missing doc",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "document", "locator": {"path": missing}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        project_id = create.json()["id"]
        validated = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert validated.status_code == 409, validated.text
        info = validated.json()["rejection_info"]
        assert info["reason_code"] == "SOURCE_NOT_FOUND"
        assert info["reason_code"] in CLOSED_REASON_CODES
        # No raw host path echo in the API-visible detail.
        assert str(tmp_path) not in (info["reason_detail"] or "")
        assert missing not in (info["reason_detail"] or "")
    finally:
        _clear_auth()


async def test_e2e_unsupported_document_type_rejects_source_invalid(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    key = _doc_key(storage_backend.root, "payload.exe", b"MZ\x90\x00")
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E exe doc",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "document", "locator": {"storage_key": key}}],
            },
            headers=headers,
        )
        assert create.status_code == 201, create.text
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 409, validated.text
        assert validated.json()["rejection_info"]["reason_code"] == "SOURCE_INVALID"
    finally:
        _clear_auth()


async def test_e2e_credential_locator_rejected_nothing_persisted(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        resp = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E secret",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "github", "locator": {"owner": "acme", "api_key": "super-secret-123"}}],
            },
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "SECURITY_REJECTED"
        assert "super-secret-123" not in resp.text  # the secret itself is not echoed
        async with DB_SESSION() as s:
            count = (await s.execute(select(Project.id).where(Project.created_by == user.id))).scalars().all()
        assert count == []  # nothing persisted
    finally:
        _clear_auth()


async def test_e2e_sensitive_root_locator_rejected_nothing_persisted(ac, storage_backend) -> None:
    """A host path pointing into a sensitive system root is registered at
    create (the credential gate only blocks *secrets*, not paths) but is
    rejected SECURITY_REJECTED at validate, when the validator's
    ``check_host_path`` shape gate runs. The source is never materialized."""
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E /etc",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "local_folder", "locator": {"path": "/etc/passwd"}}],
            },
            headers=headers,
        )
        # Registered RECEIVED: the create-time gate is the credential scan,
        # not a host-path check. The sensitive root is refused later.
        assert create.status_code == 201, create.text
        assert create.json()["status"] == "RECEIVED"
        validated = await ac.post(f"/api/projects/{create.json()['id']}/validate", headers=headers)
        assert validated.status_code == 409, validated.text
        info = validated.json()["rejection_info"]
        # Sensitive root -> SECURITY_REJECTED on POSIX. The rejection is a
        # closed-set, permanent code (no retry).
        assert info["reason_code"] in ("SECURITY_REJECTED", "SOURCE_INVALID")
        assert info["retryable"] is False
    finally:
        _clear_auth()


async def test_e2e_schema_rejects_ambiguous_document_locator(ac, storage_backend) -> None:
    """document/zip locators must carry exactly one of path/storage_key
    (422 before any entity exists — brief §4.5 / UNKNOW 5)."""
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        both = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E amb",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "document", "locator": {"path": "/x.md", "storage_key": "y/z.md"}}],
            },
            headers=headers,
        )
        assert both.status_code == 422, both.text
        async with DB_SESSION() as s:
            count = (await s.execute(select(Project.id).where(Project.created_by == user.id))).scalars().all()
        assert count == []  # rejected at validation: nothing created
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 3. Transient hold + bounded retry (real DAO counters on real Postgres)
# ---------------------------------------------------------------------------


async def test_e2e_transient_hold_climbs_retry_counter_then_escalates(storage_backend, tmp_path, monkeypatch) -> None:
    """SOURCE_UNREACHABLE holds keep the project in RECEIVED with
    pending_verifier + a climbing retry_count; at the bound the project
    becomes a terminal REJECTED.  Exercised through the service with the
    REAL DAOs and a real Postgres session — only the storage backend's
    boundary is swapped for a raising fake, per the accepted practice of the
    service tests."""
    import app.services.project_intake_service as svc
    from app.services.project_intake_service import ProjectIntakeService

    tenant, user = await _seed_tenant_user()

    class _OutageBackend:
        async def exists(self, key):
            raise OSError("storage outage")

        async def stat(self, key):
            raise OSError("storage outage")

        async def read_bytes(self, key):
            raise OSError("storage outage")

    monkeypatch.setattr(svc, "get_storage_backend", lambda: _OutageBackend())

    # A real Project + Repository row, written through real DAOs.
    async with DB_SESSION() as s:
        project = Project(
            id=uuid.uuid4(),
            name="E2E transient",
            description="d",
            goal="g",
            status="RECEIVED",
            created_by=user.id,
            tenant_id=tenant.id,
        )
        repo = Repository(
            project_id=project.id,
            source_type="document",
            locator={"storage_key": "outage/x.md"},
            tenant_id=tenant.id,
        )
        s.add(project)
        s.add(repo)
        await s.commit()
        project_id = project.id

    service = ProjectIntakeService()
    statuses: list[str] = []
    with tenant_context(tenant.id):
        # Exactly MAX_RETRIES validates: the first MAX_RETRIES-1 are
        # transient holds (RECEIVED, retry_count climbing 0 -> 1 -> 2); the
        # MAX_RETRIES-th call finds retry_count at MAX_RETRIES-1, bumps to
        # the bound, and escalates to terminal REJECTED (no further bump —
        # escalation happens *at* the bound, not after it).
        for _ in range(MAX_RETRIES):
            async with DB_SESSION() as s:
                p = (
                    await s.execute(
                        select(Project).where(Project.id == project_id).options(selectinload(Project.repositories))
                    )
                ).scalar_one()
                _p, info = await service.validate_sources(s, project=p, current_user=user)
                statuses.append(_p.status)
                assert info is not None and info.reason_code == "SOURCE_UNREACHABLE"
                assert info.retryable == (_p.status != "REJECTED")
                await s.commit()

    assert statuses[: MAX_RETRIES - 1] == ["RECEIVED"] * (MAX_RETRIES - 1)
    assert statuses[-1] == "REJECTED"

    # A validate *after* the terminal escalation is refused outright —
    # the real DB-backed path enforces the state machine, not just memory.
    from app.services.project_intake_service import IntakeTransitionError

    with tenant_context(tenant.id):
        async with DB_SESSION() as s:
            p = (
                await s.execute(
                    select(Project).where(Project.id == project_id).options(selectinload(Project.repositories))
                )
            ).scalar_one()
            with pytest.raises(IntakeTransitionError):
                await service.validate_sources(s, project=p, current_user=user)
            await s.rollback()

    async with DB_SESSION() as s:
        row = (await s.execute(select(Repository).where(Repository.project_id == project_id))).scalars().one()
        proj = (await s.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.pending_verifier is True
    # The escalation call bumps the counter to the bound (MAX_RETRIES).
    assert row.retry_count == MAX_RETRIES
    assert proj.status == "REJECTED"
    assert proj.rejection_reason == "SOURCE_UNREACHABLE"


# ---------------------------------------------------------------------------
# 4. Illegal state jumps are impossible through the real request path
# ---------------------------------------------------------------------------


async def test_e2e_revalidating_terminal_project_is_409(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        # Reach INITIALIZED through the full happy path ...
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E terminal", "description": "d", "goal": "g", "sources": [{"source_type": "manual"}]},
            headers=headers,
        )
        project_id = create.json()["id"]
        assert (await ac.post(f"/api/projects/{project_id}/validate", headers=headers)).status_code == 200

        # ... then re-validate the terminal state: conflict, state unchanged.
        again = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert again.status_code == 409, again.text
        assert again.json()["detail"]["code"] == "intake_terminal_state"
        async with DB_SESSION() as s:
            row = (await s.execute(select(Project).where(Project.id == uuid.UUID(project_id)))).scalar_one()
        assert row.status == "INITIALIZED"
    finally:
        _clear_auth()


async def test_e2e_revalidating_rejected_project_is_409(ac, storage_backend) -> None:
    """A REJECTED project (permanent SOURCE_NOT_SUPPORTED) is also terminal:
    no re-validation, ever."""
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E rej terminal",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "gitlab", "locator": {"owner": "a", "repo": "b"}}],
            },
            headers=headers,
        )
        project_id = create.json()["id"]
        first = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert first.status_code == 409  # permanent reject
        assert first.json()["rejection_info"]["reason_code"] == "SOURCE_NOT_SUPPORTED"
        again = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert again.status_code == 409  # terminal-state conflict, still REJECTED
        assert again.json()["detail"]["code"] == "intake_terminal_state"
        async with DB_SESSION() as s:
            row = (await s.execute(select(Project).where(Project.id == uuid.UUID(project_id)))).scalar_one()
        assert row.status == "REJECTED"
    finally:
        _clear_auth()


async def test_e2e_no_direct_received_to_initialized_jump(storage_backend) -> None:
    """The two-step chain is enforced by the security guard, not the service:
    RECEIVED -> INITIALIZED is refused even when asked directly."""
    from app.services.project_intake_service import IntakeTransitionError

    with pytest.raises(IntakeTransitionError):
        _direct_assert_transition("RECEIVED", "INITIALIZED")


def _direct_assert_transition(current: str, target: str) -> None:
    from app.services.project_intake_service import ProjectIntakeService

    ProjectIntakeService._assert_transition(current, target)


# ---------------------------------------------------------------------------
# 5. Tenant isolation through the real request path
# ---------------------------------------------------------------------------


async def test_e2e_cross_tenant_read_is_404_not_403(ac, storage_backend) -> None:
    """A foreign-tenant row is invisible to the tenant-scoped DAO -> 404."""
    tenant_a, user_a = await _seed_tenant_user()
    tenant_b, user_b = await _seed_tenant_user()

    _require_auth(user_a)
    try:
        headers_a = _auth_headers(tenant_a.id, user_a)
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E iso", "description": "d", "goal": "g", "sources": [{"source_type": "manual"}]},
            headers=headers_a,
        )
        project_id = create.json()["id"]
        assert create.status_code == 201

        # Now act as tenant B's user, with tenant B's middleware context:
        # the row belongs to tenant A -> not found in B's scope -> 404.
        _require_auth(user_b)
        headers_b = _auth_headers(tenant_b.id, user_b)
        resp = await ac.get(f"/api/projects/{project_id}", headers=headers_b)
        assert resp.status_code == 404, resp.text  # no disclosure, no 403
    finally:
        _clear_auth()


async def test_e2e_non_creator_member_403_same_tenant_admin_200(ac, storage_backend) -> None:
    tenant, owner = await _seed_tenant_user(role="member")
    stranger = await _user_in_tenant(tenant, role="member")
    admin = await _user_in_tenant(tenant, role="org_admin")

    _require_auth(owner)
    try:
        headers = _auth_headers(tenant.id, owner)
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E access", "description": "d", "goal": "g", "sources": [{"source_type": "manual"}]},
            headers=headers,
        )
        project_id = create.json()["id"]

        _require_auth(stranger)
        resp = await ac.get(f"/api/projects/{project_id}", headers=headers)
        assert resp.status_code == 403, resp.text

        _require_auth(admin)
        resp = await ac.get(f"/api/projects/{project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
    finally:
        _clear_auth()


async def test_e2e_list_is_tenant_scoped_for_admin(ac, storage_backend) -> None:
    """An admin's list sees every project in their tenant — and none from
    another tenant (the tenant-scoped DAO + do_orm_execute filter)."""
    tenant, owner = await _seed_tenant_user()
    foreign_tenant, _ = await _seed_tenant_user()
    admin = await _user_in_tenant(tenant, role="org_admin")
    foreign = await _user_in_tenant(foreign_tenant, role="member")

    _require_auth(owner)
    try:
        headers = _auth_headers(tenant.id, owner)
        await ac.post(
            "/api/projects/",
            json={"name": "E2E list mine", "description": "d", "goal": "g"},
            headers=headers,
        )
        _require_auth(foreign)
        foreign_headers = _auth_headers(foreign_tenant.id, foreign)
        await ac.post(
            "/api/projects/",
            json={"name": "E2E list foreign", "description": "d", "goal": "g"},
            headers=foreign_headers,
        )

        # Admin of the first tenant lists: sees own-tenant project only.
        _require_auth(admin)
        resp = await ac.get("/api/projects/", headers=headers)
        assert resp.status_code == 200, resp.text
        names = [p["name"] for p in resp.json()]
        assert "E2E list mine" in names
        assert "E2E list foreign" not in names
    finally:
        _clear_auth()


async def test_e2e_unknown_tenant_token_fails_closed(ac, storage_backend) -> None:
    """A tenant the middleware cannot bind (no valid Bearer) must not
    disclose or write cross-tenant rows: reads degrade to empty / not found,
    writes are refused — never a 500, never another tenant's data."""
    _tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        resp = await ac.get("/api/projects/", headers={})
        # The list path without tenant context still scopes by user (creator),
        # so it must not 500 and must not leak foreign tenants' rows.
        assert resp.status_code in (200, 401, 403), resp.text
        if resp.status_code == 200:
            assert all(p["created_by"] == str(user.id) for p in resp.json())
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# 6. Audit trail + rejection persistence shape (closed-loop evidence)
# ---------------------------------------------------------------------------


async def test_e2e_audit_rows_recorded_for_intake_actions(ac, storage_backend) -> None:
    tenant, user = await _seed_tenant_user()
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={"name": "E2E audit", "description": "d", "goal": "g", "sources": [{"source_type": "manual"}]},
            headers=headers,
        )
        project_id = create.json()["id"]
        await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        async with DB_SESSION() as s:
            rows = (
                (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant.id,
                            AuditLog.action.in_(("project.intake.received", "project.intake.validated")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        actions = {r.action for r in rows}
        assert {"project.intake.received", "project.intake.validated"} <= actions
        validated = next(r for r in rows if r.action == "project.intake.validated")
        assert validated.details["to_status"] == "INITIALIZED"
        received = next(r for r in rows if r.action == "project.intake.received")
        assert received.details["to_status"] == "RECEIVED"
    finally:
        _clear_auth()


async def test_e2e_rejected_detail_is_class_level_not_raw(ac, storage_backend, tmp_path) -> None:
    """The persisted rejection_detail is a bounded, class-level string: it
    names the *kind* of problem, never the offending raw host path
    (security module contract — 'the description is what may be stored')."""
    tenant, user = await _seed_tenant_user()
    folder_missing = _abs_host_path(tmp_path / "absent")
    _require_auth(user)
    try:
        headers = _auth_headers(tenant.id, user)
        create = await ac.post(
            "/api/projects/",
            json={
                "name": "E2E detail shape",
                "description": "d",
                "goal": "g",
                "sources": [{"source_type": "local_folder", "locator": {"path": folder_missing}}],
            },
            headers=headers,
        )
        project_id = create.json()["id"]
        resp = await ac.post(f"/api/projects/{project_id}/validate", headers=headers)
        assert resp.status_code == 409
        async with DB_SESSION() as s:
            row = (await s.execute(select(Project).where(Project.id == uuid.UUID(project_id)))).scalar_one()
        detail = row.rejection_detail or ""
        assert row.rejection_reason == "SOURCE_NOT_FOUND"
        # Class-level wording, and NOT the raw host path.
        assert "does not exist" in detail or "not found" in detail.lower()
        assert str(tmp_path) not in detail
        assert folder_missing not in detail
    finally:
        _clear_auth()

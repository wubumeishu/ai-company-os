"""Service tests for Project Materialization (Phase 2B-3, card t_025cda02).

Covers docs/MATERIALIZATION_SECURE_SPEC_V1.md §11 handover items 1-12
(non-E2E) WITHOUT a live DB: the storage backend, directory locks, human
edit locks, and the db session are stubbed; source readers run against real
``tmp_path`` filesystem fixtures so path / zip / budget checks are real,
not string-matching.  Spec §11.13 (real Postgres + real storage) lives in
the E2E acceptance suite, not here.

Invariants asserted (spec / card):
- Status gate: only INITIALIZED; every other status → SOURCE_NOT_READY.
- Zip Slip: real malicious archives → SECURITY_REJECTED, 0 writes.
- Path traversal / sensitive roots on local_folder → rejected.
- Workspace boundary: reserved first-segment names rejected; the target-key
  formula is asserted on the KEY STRING (spec §2.3/§2.5).
- Tenant isolation: cross-tenant agent → MaterializationSecurity (M9 gate).
- Existing same-name file: differing content + overwrite=false →
  CONTENT_CONFLICT with 0 new writes; overwrite=true replaces and the
  revision records before_content.  Repeat call → CONVERGED, hash stable.
- Partial failure: a busy publish lock → PARTIAL + the failing repo's
  staging subtree is fully cleaned; the other repo's write survives.
- Human edit lock: an active lock → HUMAN_LOCK_CONFLICT, 0 writes.
- Unready source: pending_verifier / git source → SOURCE_NOT_READY.
"""

from __future__ import annotations

import contextlib
import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest

import app.services.project_materialization_service as svc
from app.models.agent import Agent
from app.models.project import Project
from app.models.user import User
from app.models.workspace import WorkspaceFileRevision
from app.services.intake_security import TenantScopeViolation
from app.services.project_materialization_service import (
    GIT_SOURCE_TYPES,
    MaterializationNotReady,
    MaterializationSecurity,
    ProjectMaterializationService,
    content_hash_bytes,
    make_plan,
    make_repo,
)

# ---------------------------------------------------------------------------
# Builders / fakes
# ---------------------------------------------------------------------------


def make_user(*, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), tenant_id=tenant_id, role="member")


def _agent(*, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None) -> Agent:
    """A real Agent row (no DB) — the intake test's ORM-object pattern."""
    return Agent(id=agent_id or uuid.uuid4(), name="mat-agent", creator_id=uuid.uuid4(), tenant_id=tenant_id)


def _project(
    *,
    tenant_id: uuid.UUID,
    status: str = "INITIALIZED",
    project_id: uuid.UUID | None = None,
    repositories: list | None = None,
) -> Project:
    """A real Project row (no DB), with its repositories collection set in
    memory (``# type: ignore`` — no relationship backref without a DB)."""
    p = Project(
        id=project_id or uuid.uuid4(),
        name="mat",
        description="d",
        goal="g",
        status=status,
        created_by=uuid.uuid4(),
        tenant_id=tenant_id,
    )
    p.repositories = list(repositories or [])  # type: ignore[attr-defined]
    return p


def _make_backend(monkeypatch, *, files: dict | None = None, directories: set | None = None) -> svc._MemoryStorage:
    backend = svc._MemoryStorage(files=files or {}, directories=directories or set())
    monkeypatch.setattr(svc, "get_storage_backend", lambda: backend)
    return backend


class FakeWorkspaceLocks:
    """Stands in for ``workspace_locks`` (spec §6.1 recipe); records the
    (agent_id, paths, tenant_id) triple so §6.1 (agent-tenant domain) is
    asserted on the call, and can mark individual lock paths busy to
    simulate a directory-level lock collision."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, list[str], object]] = []
        self.busy_paths: set[str] = set()

    def __call__(self, agent_id, paths, *, ttl_seconds=None, tenant_id=None):
        self.calls.append((agent_id, list(paths), tenant_id))

        @contextlib.asynccontextmanager
        async def _cm():
            for p in paths:
                if p in self.busy_paths:
                    raise RuntimeError(f"Workspace lock busy: {p}")
            yield

        return _cm()


@pytest.fixture
def workspace_locks(monkeypatch):
    fake = FakeWorkspaceLocks()
    monkeypatch.setattr(svc, "workspace_locks", fake)
    return fake


@pytest.fixture
def active_lock(monkeypatch):
    """Stands in for ``workspace_collaboration.get_active_lock``: returns an
    active lock exactly for the paths placed in ``state['locked']``."""
    state: dict[str, set] = {"locked": set()}

    async def _fake_get_active_lock(db, *, agent_id, path):
        if path in state["locked"]:
            return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), agent_id=agent_id, path=path)
        return None

    monkeypatch.setattr(svc, "get_active_lock", _fake_get_active_lock)
    return state


class FakeSession:
    """Records ``add`` / ``flush``; no real DB (spec §11.1-12 are DB-free)."""

    def __init__(self) -> None:
        self.added: list = []
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    def revisions(self) -> list:
        return [o for o in self.added if isinstance(o, WorkspaceFileRevision)]

    def audits(self) -> list:
        from app.models.audit import AuditLog

        return [o for o in self.added if isinstance(o, AuditLog)]


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


def _make_zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _service() -> ProjectMaterializationService:
    return svc.project_materialization_service


# ---------------------------------------------------------------------------
# 1. Status gate — only INITIALIZED passes (spec §1 / §11.1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["RECEIVED", "SOURCES_OK", "ANALYZING", "EXECUTING", "BLOCKED", "COMPLETED", "ARCHIVED", "REJECTED"])
async def test_non_initialized_status_is_source_not_ready(status, session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("manual", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, status=status, project_id=project_id, repositories=[repo])
    with pytest.raises(MaterializationNotReady) as exc:
        await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert exc.value.code == "SOURCE_NOT_READY"
    assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# 2. Zip Slip — a real malicious archive is rejected with 0 writes (spec §11.2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "members",
    [
        [("../escape.txt", b"x"), ("a/b.txt", b"ok")],
        [("/etc/cron.d/evil", b"x")],
        [("C:\\evil\\x", b"x")],
        [("a/../../b", b"x")],
    ],
)
async def test_zip_slip_is_security_rejected_with_zero_writes(members, session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    backend.files["e2e/evil.zip"] = _make_zip_bytes(members)
    repo = make_repo("zip", {"storage_key": "e2e/evil.zip"}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SECURITY_REJECTED"
    assert row.written == 0 and row.converged == 0
    assert out.outcome == "FAILED"
    # No target write and no residual staging (spec §7.2 cleanup invariant).
    assert not any(k.startswith(f"{agent.id}/projects/") for k in backend.files)
    assert not any(k.startswith(f"{agent.id}/.materialize-tmp/") for k in backend.files)


# ---------------------------------------------------------------------------
# 3. Path traversal / sensitive roots on local_folder host paths (spec §11.3).
# ---------------------------------------------------------------------------


async def test_local_folder_traversal_is_rejected(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("local_folder", {"path": f"{tmp_path}\\.."}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SECURITY_REJECTED"
    assert row.written == 0


async def test_local_folder_sensitive_root_is_rejected(session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("local_folder", {"path": "/etc/passwd"}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SECURITY_REJECTED"
    assert out.repositories[0].written == 0


# ---------------------------------------------------------------------------
# 4. Workspace boundary — reserved names + target-key formula (spec §11.4 / §11.6).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["skills", "tasks.json", "workspace", ".materialize-tmp", "memory", "soul.md", "HEARTBEAT.md", "projects", "focus.md", ".git"],
)
async def test_reserved_top_level_member_is_rejected(name, session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    backend.files["e2e/reserved.zip"] = _make_zip_bytes([(f"{name}/inner.txt", b"nope")])
    repo = make_repo("zip", {"storage_key": "e2e/reserved.zip"}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SECURITY_REJECTED"
    assert row.written == 0
    # Zero writes: the backend holds no target / staging keys.
    assert set(backend.files) == {"e2e/reserved.zip"}


async def test_reserved_material_name_is_source_invalid(session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    backend.files["e2e/doc.md"] = b"# doc\n"
    # A content-bearing source whose material_name is a reserved storage name
    # fails the §2.1 shape check → SOURCE_INVALID (manual sources are an
    # explicit skip instead, which is covered separately).
    repo = make_repo("document", {"storage_key": "e2e/doc.md"}, display_name="skills", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_INVALID"
    assert out.repositories[0].written == 0
    assert set(backend.files) == {"e2e/doc.md"}


def test_target_key_formula_is_the_authorized_agent() -> None:
    """Spec §2.3/§2.5: the key string ALWAYS carries the named agent's prefix —
    asserted on the formula, not just that some write happened."""
    agent_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    project_id = uuid.uuid4()
    plan = make_plan(agent_id=agent_id, project_id=project_id, material_name="docs", repo_id=repo_id)
    key = plan.target_key("a/b.txt")
    assert key == f"{agent_id}/projects/{project_id}/docs/a/b.txt"
    assert key.startswith(f"{agent_id}/projects/")
    # Another agent's prefix can never be produced by the same formula.
    other = uuid.uuid4()
    assert not key.startswith(f"{other}/projects/")
    sk = plan.staging_key_for("a/b.txt")
    assert sk == f"{agent_id}/.materialize-tmp/{repo_id}/a/b.txt"
    assert sk.startswith(f"{agent_id}/.materialize-tmp/")


def test_normalize_rel_rejects_traversal() -> None:
    norm = ProjectMaterializationService._normalize_rel
    assert norm("a/./b.txt") == "a/b.txt"
    assert norm("a\\b.txt") == "a/b.txt"
    assert norm("../x") is None
    assert norm("a/../../x") is None
    assert norm("a\x00b") is None
    assert norm("") is None
    assert norm(".") is None


# ---------------------------------------------------------------------------
# 5. Tenant isolation — M9 fourth gate (spec §11.5).
# ---------------------------------------------------------------------------


async def test_cross_tenant_agent_is_materialization_security(session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user = make_user(tenant_id=tenant_a)
    agent_b = _agent(tenant_id=tenant_b)  # a Tenant-B agent
    project_id = uuid.uuid4()
    repo = make_repo("manual", project_id=project_id, tenant_id=tenant_a)
    project = _project(tenant_id=tenant_a, project_id=project_id, repositories=[repo])
    with pytest.raises(MaterializationSecurity):
        await _service().materialize(session, project=project, agent=agent_b, overwrite=False, current_user=user)


async def test_cross_tenant_user_is_materialization_security(session, workspace_locks, active_lock, monkeypatch) -> None:
    """The entry re-check (verify_tenant_scope) fails before the M9 gate: a
    user from another tenant may not even address the project row.  This
    raises the *narrow* TenantScopeViolation (a SecurityError subclass), which
    the transport maps like any security finding — never a silent cross-tenant
    write."""
    _make_backend(monkeypatch)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user = make_user(tenant_id=tenant_b)
    agent = _agent(tenant_id=tenant_a)
    project_id = uuid.uuid4()
    repo = make_repo("manual", project_id=project_id, tenant_id=tenant_a)
    project = _project(tenant_id=tenant_a, project_id=project_id, repositories=[repo])
    with pytest.raises(TenantScopeViolation):
        await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)


# ---------------------------------------------------------------------------
# 6. Happy path — target-key landing + provenance (spec §11.6 / §9.1).
# ---------------------------------------------------------------------------


async def test_local_folder_happy_path_writes_target_keys_and_revisions(
    tmp_path, session, workspace_locks, active_lock, monkeypatch
) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    (folder / "sub").mkdir(parents=True)
    (folder / "main.py").write_text("print('hi')")
    (folder / "sub" / "child.txt").write_text("kid")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS"
    assert row.written == 2 and row.converged == 0
    assert out.outcome == "SUCCESS"
    expected_a = f"{agent.id}/projects/{project_id}/docs/main.py"
    expected_b = f"{agent.id}/projects/{project_id}/docs/sub/child.txt"
    assert backend.files[expected_a] == b"print('hi')"
    assert backend.files[expected_b] == b"kid"
    # §6.1 lock recipe: the directory lock is taken on the agent's tenant.
    assert workspace_locks.calls[0] == (agent.id, [f"projects/{project_id}/docs"], tenant)
    # Provenance: one revision row per written file, stable group_key (§9.1).
    revs = session.revisions()
    assert len(revs) == 2
    gk = f"materialize:{project_id}:{repo.id}:{agent.id}"
    assert all(r.group_key == gk for r in revs)
    assert all(r.actor_type == "system" and r.actor_id == user.id for r in revs)
    assert {r.content_hash for r in revs} == {
        content_hash_bytes(backend.files[expected_a]),
        content_hash_bytes(backend.files[expected_b]),
    }
    # Audit row recorded (§9.2) with the per-repo result detail.
    audits = session.audits()
    assert len(audits) == 1
    assert audits[0].action == "project_materialization"
    assert audits[0].details["outcome"] == "SUCCESS"
    assert audits[0].details["repo_results"][0]["repo_id"] == str(repo.id)
    # Staging cleaned on the success path (§7.2).
    assert not any(k.startswith(f"{agent.id}/.materialize-tmp/") for k in backend.files)


# ---------------------------------------------------------------------------
# 7. Idempotency — a repeat call converges with no drift (spec §11.8).
# ---------------------------------------------------------------------------


async def test_repeat_call_converges_with_no_drift(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("same")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    first = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert first.repositories[0].outcome == "SUCCESS"
    assert first.repositories[0].written == 1
    target = f"{agent.id}/projects/{project_id}/docs/a.txt"
    snapshot_hash = content_hash_bytes(backend.files[target])
    file_count = len(backend.files)
    revision_count = len(session.revisions())

    second = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row2 = second.repositories[0]
    assert row2.outcome == "CONVERGED"
    assert row2.converged == 1 and row2.written == 0
    assert second.outcome == "SUCCESS"
    # No drift: identical bytes, no new file, no duplicate revision row.
    assert content_hash_bytes(backend.files[target]) == snapshot_hash
    assert len(backend.files) == file_count
    assert len(session.revisions()) == revision_count


# ---------------------------------------------------------------------------
# 8. Existing same-name file, differing content (spec §11.9).
# ---------------------------------------------------------------------------


async def test_existing_different_content_no_overwrite_is_conflict(
    tmp_path, session, workspace_locks, active_lock, monkeypatch
) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("NEW")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    existing_key = f"{agent.id}/projects/{project_id}/docs/a.txt"
    backend.files[existing_key] = b"OLD"

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "CONTENT_CONFLICT"
    assert row.written == 0
    # Spec §8 row 3: a conflicting repo has 0 NEW writes — the existing
    # content was not touched.
    assert backend.files[existing_key] == b"OLD"
    assert out.retryable is False


async def test_existing_different_content_overwrite_replaces_and_records_before(
    tmp_path, session, workspace_locks, active_lock, monkeypatch
) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("NEW")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    existing_key = f"{agent.id}/projects/{project_id}/docs/a.txt"
    backend.files[existing_key] = b"OLD"

    out = await _service().materialize(session, project=project, agent=agent, overwrite=True, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS"
    assert row.written == 1
    assert backend.files[existing_key] == b"NEW"
    # The revision captured the BEFORE content (§8 row 3 / §9.1).
    revs = [r for r in session.revisions() if r.path.endswith("/a.txt")]
    assert revs and revs[0].before_content == "OLD"
    assert revs[0].content_hash == content_hash_bytes(b"NEW")


# ---------------------------------------------------------------------------
# 10. Partial failure — a mid-publish lock conflict (spec §11.10).
# ---------------------------------------------------------------------------


async def test_busy_publish_lock_is_partial_and_cleans_failing_staging(
    tmp_path, session, workspace_locks, active_lock, monkeypatch
) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()

    def _folder(name: str) -> str:
        d = tmp_path / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        return str(d)

    repo_ok = make_repo("local_folder", {"path": _folder("ok")}, display_name="ok", project_id=project_id, tenant_id=tenant)
    repo_bad = make_repo("local_folder", {"path": _folder("bad")}, display_name="bad", project_id=project_id, tenant_id=tenant)
    workspace_locks.busy_paths.add(f"projects/{project_id}/bad")
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo_ok, repo_bad])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    ok_row = next(r for r in out.repositories if r.repo_id == repo_ok.id)
    bad_row = next(r for r in out.repositories if r.repo_id == repo_bad.id)
    assert ok_row.outcome == "SUCCESS" and ok_row.written == 1
    assert bad_row.outcome == "FAILED" and bad_row.reason_code == "LOCK_CONFLICT"
    assert out.outcome == "PARTIAL"
    assert out.retryable is True
    # The failing repo's staging subtree was fully cleaned (§7.2 / §11.10).
    assert not any(k.startswith(f"{agent.id}/.materialize-tmp/{repo_bad.id}/") for k in backend.files)
    # The successful repo's target write DID land (§7.3 independent outcomes).
    assert backend.files[f"{agent.id}/projects/{project_id}/ok/f.txt"] == b"ok"


# ---------------------------------------------------------------------------
# 11. Human edit lock — an active lock wins, never silently overwritten
# (spec §11.11).
# ---------------------------------------------------------------------------


async def test_active_human_lock_is_conflict_not_overwrite(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    active_lock["locked"].add(f"projects/{project_id}/docs/a.txt")
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "HUMAN_LOCK_CONFLICT"
    assert row.written == 0
    assert f"{agent.id}/projects/{project_id}/docs/a.txt" not in backend.files
    assert out.retryable is True


# ---------------------------------------------------------------------------
# 12. Unready source — pending_verifier / git source (spec §11.12).
# ---------------------------------------------------------------------------


async def test_pending_verifier_repo_is_source_not_ready(session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("local_folder", {"path": "/x"}, pending_verifier=True, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    with pytest.raises(MaterializationNotReady) as exc:
        await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert exc.value.code == "SOURCE_NOT_READY"


@pytest.mark.parametrize("stype", sorted(GIT_SOURCE_TYPES))
async def test_git_source_repo_is_source_not_ready(stype, session, workspace_locks, active_lock, monkeypatch) -> None:
    """Spec §3.5: a git repo can never pass intake, so under the INITIALIZED
    gate it is unreachable; the gate still fails it closed with
    SOURCE_NOT_READY (explicit unknown-value behavior, never a fake success)."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo(stype, {"owner": "o", "repo": "r"}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    with pytest.raises(MaterializationNotReady) as exc:
        await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert exc.value.code == "SOURCE_NOT_READY"


# ---------------------------------------------------------------------------
# manual source, document source, budgets (spec §3.1 / §3.3 / §3).
# ---------------------------------------------------------------------------


async def test_manual_source_is_explicit_skip_zero_writes(session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("manual", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SKIPPED_NO_MATERIAL"
    assert row.reason_code == "SKIPPED_NO_MATERIAL"
    assert row.skipped == 1 and row.written == 0
    assert out.outcome == "SUCCESS"
    assert not backend.files
    assert any("manual" in note for note in out.limitations)


async def test_document_storage_key_happy_path(session, workspace_locks, active_lock, monkeypatch) -> None:
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    doc_key = "e2e/spec.md"
    backend.files[doc_key] = b"# spec\n"
    repo = make_repo("document", {"storage_key": doc_key}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS" and row.written == 1
    target = f"{agent.id}/projects/{project_id}/{str(repo.id)[:8]}/spec.md"
    assert backend.files[target] == b"# spec\n"
    # The source object was only READ, never modified (§3 invariant).
    assert backend.files[doc_key] == b"# spec\n"


async def test_document_unsupported_extension_is_source_invalid(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    bad = tmp_path / "blob.exe"
    bad.write_bytes(b"MZ")
    repo = make_repo("document", {"path": str(bad)}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SOURCE_INVALID"


async def test_missing_source_is_source_not_found(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo = make_repo("local_folder", {"path": str(tmp_path / "does-not-exist")}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SOURCE_NOT_FOUND"
    assert out.retryable is True


async def test_empty_folder_is_source_invalid(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    """Spec §3.2/§3.4: a content-bearing source that enumerates to zero files
    is SOURCE_INVALID — never a fake SUCCESS with nothing written."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "empty"
    folder.mkdir()
    repo = make_repo("local_folder", {"path": str(folder)}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SOURCE_INVALID"
    assert row.written == 0


async def test_over_single_file_budget_is_source_size_limit(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "big"
    folder.mkdir()
    (folder / "big.bin").write_bytes(b"x" * (svc.MAX_MATERIALIZE_FILE_BYTES + 1))
    repo = make_repo("local_folder", {"path": str(folder)}, project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED"
    assert row.reason_code == "SOURCE_SIZE_LIMIT"
    assert row.written == 0


# ---------------------------------------------------------------------------
# Source-tree invariants (spec §3).
# ---------------------------------------------------------------------------


async def test_source_tree_is_never_mutated(tmp_path, session, workspace_locks, active_lock, monkeypatch) -> None:
    """§3 invariant: materialization only READS the source — no host file is
    moved, renamed, or deleted by a successful call."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user = make_user(tenant_id=tenant)
    agent = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    (folder / "sub").mkdir(parents=True)
    (folder / "main.py").write_text("print('hi')")
    (folder / "sub" / "child.txt").write_text("kid")
    snapshot = sorted(
        (str(p.relative_to(folder)), content_hash_bytes(p.read_bytes())) for p in folder.rglob("*") if p.is_file()
    )

    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert out.outcome == "SUCCESS"

    after = sorted(
        (str(p.relative_to(folder)), content_hash_bytes(p.read_bytes())) for p in folder.rglob("*") if p.is_file()
    )
    assert after == snapshot  # the source tree is byte-for-byte untouched

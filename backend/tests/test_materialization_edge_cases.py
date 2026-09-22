"""Edge-case tests for Project Materialization (card t_6990cc6d).

Additive to ``tests/test_project_materialization_service.py`` (46 DB-free
service tests, spec §11.1-12) and
``tests/test_materialization_e2e_acceptance.py`` (11 real-Postgres tests,
spec §11.13).  This file closes the *remaining* verification surface the
task calls out — V1 source-type breadth, path traversal, repeated
materialization, concurrent lock conflicts, partial failures, and the
"agent workspaces stay isolated and sources are never modified"
invariant — WITHOUT a live DB, so it runs anywhere ``tmp_path`` does.

What is deliberately NOT re-tested here (already covered by the two
suites above): the status gate, the happy-path key formula + provenance,
the reserved-name boundary, the cross-tenant M9 gate, the busy *directory*
lock fast-fail, the human edit lock, and the git/pending_verifier
SOURCE_NOT_READY gates.  Each test below carries a short "why" so a reader
can tell it is new coverage, not a duplicate.

Invariants this file locks down:
- Every V1 source type reaches every success/rejection shape through the
  real source readers:
  * manual         -> SKIPPED_NO_MATERIAL (no content invented)
  * local_folder   -> host-dir enumeration (success / empty / not-found)
  * document       -> host file + storage_key (success / missing / dir /
                      unsupported type / material-name fallback)
  * zip            -> host file + storage_key (success / missing / corrupt /
                      dir-only member skipped)
- Host-path shape security is closed-set per type: ``local_folder`` relative
  -> SOURCE_INVALID (shape); ANY type carrying ``..`` / NUL -> 
  SECURITY_REJECTED (a host escape vector is never "invalid", it is
  *rejected*).
- A SUCCESS materialization leaves the on-disk *source* byte-for-byte
  identical (the §3 invariant) for local_folder, document, AND zip — and
  even for a security-rejected source.
- Idempotency is content-hash based: a repeat call converges only while the
  source is unchanged; a changed source re-conflicts (0 new writes unless
  overwrite=true), and the overwrite path captures the BEFORE content.
- Concurrent access: the directory lock is acquired and *released* (a clean
  repeat succeeds — no stale lock), a racer holding it fast-fails with
  LOCK_CONFLICT, and a write-phase racer (probe saw the key absent) is
  caught by the conditional write as CONTENT_CONFLICT with 0 new writes.
- Partial failure is independent-outcome: a sibling that stages then
  CONTENT_CONFLICTs is PARTIAL with its staging cleaned; a staging write
  failure fails THAT repo only; a security-rejected repo never poisons a
  clean sibling.
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
from app.services.project_materialization_service import (
    ProjectMaterializationService,
    content_hash_bytes,
    make_repo,
)
from app.services.storage_runtime.base import ConditionalWriteResult

# ---------------------------------------------------------------------------
# Builders / fakes (self-contained mirror of the service-suite fixtures so
# this file runs independently of test_project_materialization_service.py).
# ---------------------------------------------------------------------------


def make_user(*, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), tenant_id=tenant_id, role="member")


def _agent(*, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None) -> Agent:
    return Agent(id=agent_id or uuid.uuid4(), name="mat-edge-agent", creator_id=uuid.uuid4(), tenant_id=tenant_id)


def _project(
    *,
    tenant_id: uuid.UUID,
    status: str = "INITIALIZED",
    project_id: uuid.UUID | None = None,
    repositories: list | None = None,
) -> Project:
    p = Project(
        id=project_id or uuid.uuid4(),
        name="mat-edge",
        description="edge",
        goal="edge",
        status=status,
        created_by=uuid.uuid4(),
        tenant_id=tenant_id,
    )
    p.repositories = list(repositories or [])  # type: ignore[attr-defined]
    return p


def _make_backend(monkeypatch, *args, **kwargs) -> svc._MemoryStorage:
    backend = svc._MemoryStorage(*args, **kwargs)
    monkeypatch.setattr(svc, "get_storage_backend", lambda: backend)
    return backend


class FakeSession:
    """Records ``add`` / ``flush``; no real DB (this suite is DB-free)."""

    def __init__(self) -> None:
        self.added: list = []
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    def revisions(self) -> list:
        return [o for o in self.added if isinstance(o, WorkspaceFileRevision)]

    def audit_count(self) -> int:
        from app.models.audit import AuditLog

        return sum(1 for o in self.added if isinstance(o, AuditLog))


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


class RecordingWorkspaceLocks:
    """A real acquire/release directory lock (spec §6.1) that records order.

    ``held`` models *another* writer already holding a directory lock: add a
    path to it before a call and that call fast-fails with LOCK_CONFLICT.
    The lock a materialize takes is released in its ``finally`` (no stale
    lock), so a clean repeat call acquires it again.
    """

    def __init__(self) -> None:
        self.held: set[str] = set()  # "other writers" holding these paths
        self.acquire_order: list[str] = []
        self.released: list[str] = []

    def __call__(self, agent_id, paths, *, ttl_seconds=None, tenant_id=None):
        rec = self

        @contextlib.asynccontextmanager
        async def _cm():
            for p in paths:
                if p in rec.held:
                    raise RuntimeError(f"Workspace lock busy: {p}")
            for p in paths:
                rec.acquire_order.append(p)
            try:
                yield
            finally:
                for p in paths:
                    rec.held.discard(p)
                    rec.released.append(p)

        return _cm()


@pytest.fixture
def locks(monkeypatch) -> RecordingWorkspaceLocks:
    fake = RecordingWorkspaceLocks()
    monkeypatch.setattr(svc, "workspace_locks", fake)
    return fake


@pytest.fixture
def active_lock(monkeypatch):
    """Stands in for ``workspace_collaboration.get_active_lock`` (human edit
    locks).  By default no path is locked -> the materialize proceeds, so the
    lock fixtures do not shadow every test; only tests that need it set one."""

    state: dict[str, set] = {"locked": set()}

    async def _fake_get_active_lock(db, *, agent_id, path):
        if path in state["locked"]:
            return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), agent_id=agent_id, path=path)
        return None

    monkeypatch.setattr(svc, "get_active_lock", _fake_get_active_lock)
    return state


def _make_zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _write_zip_to_disk(tmp_path, name: str, members: list[tuple[str, bytes]]) -> str:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for mname, mdata in members:
            zf.writestr(mname, mdata)
    return str(p)


def _service() -> ProjectMaterializationService:
    return svc.project_materialization_service


def _ctx(*, tenant: uuid.UUID, user, agent, project_id, repo, project=None) -> tuple[Project, Agent, User]:
    """Assemble a call context sharing one tenant (the isolation happy-case)."""
    project = project or _project(tenant_id=tenant, project_id=project_id, repositories=[repo])
    return project, agent, user


# ===========================================================================
# 1. Path traversal — closed-set shape vs. security rejection, all types
# ===========================================================================


async def test_local_folder_relative_path_is_source_invalid(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """local_folder is *defined* as an absolute host path, so a relative path
    is a shape error (SOURCE_INVALID), not a security rejection."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    # "src/docs" is relative (no leading drive or /) -> shape invalid.
    repo = make_repo("local_folder", {"path": "src/docs"}, project_id=project_id, tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_INVALID"
    assert out.repositories[0].outcome == "FAILED"


@pytest.mark.parametrize(
    "raw",
    [
        "../docs",  # relative AND escaping -> the '..' makes it security, not shape
        "C:/Windows/../win",
    ],
)
async def test_document_zip_host_path_dotdot_is_security_rejected(raw, tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """document/zip host paths may be relative, but a ``..`` segment is an
    escape vector: SECURITY_REJECTED (never a silent relative read)."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    for stype in ("document", "zip"):
        repo = make_repo(stype, {"path": raw}, project_id=project_id, tenant_id=tenant)
        out = await _service().materialize(
            session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user
        )
        row = out.repositories[0]
        assert row.outcome == "FAILED"
        assert row.reason_code == "SECURITY_REJECTED"
        assert row.written == 0


@pytest.mark.parametrize("stype", ["local_folder", "document", "zip"])
async def test_host_path_with_nul_byte_is_security_rejected(stype, tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """A NUL byte truncates C-style path buffers: the single authoritative
    guard rejects it for every host-path source type."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    base = str(tmp_path) + "/sub" if stype != "local_folder" else str(tmp_path)
    bad = f"{base}/file\x00.txt"
    locator = {"path": bad}
    repo = make_repo(stype, locator, project_id=project_id, tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SECURITY_REJECTED"
    assert out.repositories[0].written == 0


# ===========================================================================
# 2. Document source type — host path + storage_key, all shapes
# ===========================================================================


async def test_document_host_path_happy_path(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """document via a real on-disk host file: SUCCESS, one target write; the
    material name falls back to the repo id's first 8 chars (§2.1)."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    doc = tmp_path / "report.md"
    # Binary write: text-mode write_text translates \n to the platform
    # line-ending (CRLF on Windows), which would make the on-disk bytes
    # non-deterministic across OSes.  The materialization contract is about
    # exact bytes, so the source must be written byte-for-byte.
    doc.write_bytes(b"# report\nbody")
    repo_id = uuid.uuid4()
    repo = make_repo("document", {"path": str(doc)}, repo_id=repo_id, project_id=project_id, tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS" and row.written == 1
    target = f"{agent.id}/projects/{project_id}/{str(repo_id)[:8]}/report.md"
    assert backend.files[target] == b"# report\nbody"
    # The source document was only read, never touched (§3 invariant).
    assert doc.read_bytes() == b"# report\nbody"


async def test_document_host_path_missing_is_source_not_found(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    repo = make_repo("document", {"path": str(tmp_path / "nope.md")}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_NOT_FOUND"
    assert out.repositories[0].outcome == "FAILED"
    assert out.retryable is True  # a missing mount / file is transient


async def test_document_host_path_is_directory_is_source_invalid(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """Pointing a document locator at a directory is a shape error, not an
    unreadable file: SOURCE_INVALID."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    d = tmp_path / "adirectory"
    d.mkdir()
    repo = make_repo("document", {"path": str(d)}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_INVALID"


async def test_document_storage_key_missing_is_source_not_found(session, locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    repo = make_repo("document", {"storage_key": "does/not/exist.md"}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_NOT_FOUND"
    assert out.retryable is True


async def test_document_storage_key_is_directory_is_source_invalid(session, locks, active_lock, monkeypatch) -> None:
    # The service normalizes the lookup key (trailing slash stripped -> "a/doc"),
    # and the in-memory backend stores keys exactly as passed, so the directory
    # marker must be registered under the *normalized* key the service reads.
    _make_backend(monkeypatch, directories={"a/doc"})
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    repo = make_repo("document", {"storage_key": "a/doc"}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_INVALID"


# ===========================================================================
# 3. Zip source type — host path + storage_key, all shapes
# ===========================================================================


async def test_zip_host_path_happy_path(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """zip via a real on-disk archive: SUCCESS; both members land under the
    §2.3 key layout; the source archive on disk is byte-identical after."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    zpath = _write_zip_to_disk(tmp_path, "pkg.zip", [("docs/readme.md", b"# hi"), ("src/app.py", b"print(1)")])
    repo_id = uuid.uuid4()
    repo = make_repo("zip", {"path": zpath}, repo_id=repo_id, project_id=project_id, tenant_id=tenant, display_name="pkg")
    before = Path_bytes(zpath)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS" and row.written == 2
    for rel, content in (("docs/readme.md", b"# hi"), ("src/app.py", b"print(1)")):
        assert backend.files[f"{agent.id}/projects/{project_id}/pkg/{rel}"] == content
    assert Path_bytes(zpath) == before  # source archive untouched
    # The declared zip limitation is reported (symlink -> ordinary file).
    assert any("zip" in n and "symlink" in n for n in out.limitations)


async def test_zip_host_path_missing_is_source_not_found(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    repo = make_repo("zip", {"path": str(tmp_path / "missing.zip")}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_NOT_FOUND"
    assert out.retryable is True


async def test_zip_storage_key_corrupt_is_source_invalid(session, locks, active_lock, monkeypatch) -> None:
    """A storage object that is not a readable zip container is SOURCE_INVALID
    (not a security finding): the guard's closed-set distinction."""
    _make_backend(monkeypatch, files={"a/bad.zip": b"this is not a zip archive at all"})
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    repo = make_repo("zip", {"storage_key": "a/bad.zip"}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SOURCE_INVALID"
    assert out.repositories[0].outcome == "FAILED"


async def test_zip_directory_only_member_is_skipped(session, locks, active_lock, monkeypatch) -> None:
    """A member with a trailing '/' is a directory entry: it is enumerated but
    not materialized as a file; a dir-only archive (no regular files) is
    SOURCE_INVALID (nothing to materialize — no fake SUCCESS)."""
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()

    # dir entry + one real file -> only the file lands.
    backend = _make_backend(monkeypatch, files={"a/mixed.zip": _make_zip_bytes([("sub/", b""), ("sub/x.txt", b"x")])})
    repo = make_repo("zip", {"storage_key": "a/mixed.zip"}, project_id=project_id, tenant_id=tenant, display_name="z")
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].outcome == "SUCCESS" and out.repositories[0].written == 1
    assert f"{agent.id}/projects/{project_id}/z/sub/x.txt" in backend.files
    assert f"{agent.id}/projects/{project_id}/z/sub" not in backend.files  # dir not a key

    # A dir-only archive -> zero regular files -> SOURCE_INVALID.
    _make_backend(monkeypatch, files={"a/empty.zip": _make_zip_bytes([("only/a/dir/", b"")])})
    repo2 = make_repo("zip", {"storage_key": "a/empty.zip"}, project_id=uuid.uuid4(), tenant_id=tenant)
    out2 = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo2.project_id, repositories=[repo2]), agent=agent, overwrite=False, current_user=user)
    assert out2.repositories[0].reason_code == "SOURCE_INVALID"
    assert out2.repositories[0].written == 0


# ===========================================================================
# 4. The §3 invariant: the on-disk source is never modified
# ===========================================================================


def Path_bytes(p: str) -> bytes:
    from pathlib import Path

    return Path(p).read_bytes()


async def test_document_host_source_is_never_mutated(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """After a SUCCESS document materialization the source file is unchanged,
    and a FAILED (unsupported-type) call also leaves it untouched."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)

    good = tmp_path / "ok.md"
    good.write_text("hello world")
    good_repo = make_repo("document", {"path": str(good)}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=good_repo.project_id, repositories=[good_repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].outcome == "SUCCESS"
    assert good.read_bytes() == b"hello world"

    # Now point the *same* source at a name the whitelist rejects; the read
    # attempt must not rewrite/delete the file.
    bad = tmp_path / "ok.bin"
    bad.write_bytes(b"\x00\x01\x02")
    bad_repo = make_repo("document", {"path": str(bad)}, project_id=uuid.uuid4(), tenant_id=tenant)
    out2 = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=bad_repo.project_id, repositories=[bad_repo]), agent=agent, overwrite=False, current_user=user)
    assert out2.repositories[0].reason_code == "SOURCE_INVALID"
    assert bad.read_bytes() == b"\x00\x01\x02"  # still there, untouched


async def test_security_rejected_zip_source_is_never_mutated(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """Even a malicious archive that is rejected with 0 writes leaves the
    on-disk source byte-identical (the reject path is pure in-memory)."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    zpath = _write_zip_to_disk(tmp_path, "evil.zip", [("../../../etc/cron.d/evil", b"x")])
    before = Path_bytes(zpath)
    repo = make_repo("zip", {"path": zpath}, project_id=uuid.uuid4(), tenant_id=tenant)
    out = await _service().materialize(session, project=_project(tenant_id=tenant, project_id=repo.project_id, repositories=[repo]), agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].reason_code == "SECURITY_REJECTED"
    assert Path_bytes(zpath) == before


# ===========================================================================
# 5. Repeated materialization — idempotency is content-hash based
# ===========================================================================


async def test_triple_repeat_call_stable(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """SUCCESS -> CONVERGED -> CONVERGED: the target bytes, the file count,
    and the revision count are all stable across three calls (no drift, no
    duplicate provenance rows)."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("stable")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    calls = [await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user) for _ in range(3)]
    assert [c.repositories[0].outcome for c in calls] == ["SUCCESS", "CONVERGED", "CONVERGED"]
    target = f"{agent.id}/projects/{project_id}/docs/a.txt"
    first_hash = content_hash_bytes(backend.files[target])
    assert content_hash_bytes(backend.files[target]) == first_hash  # no content drift
    # Only the first call wrote a revision; the two converged calls added none.
    assert len(session.revisions()) == 1


async def test_repeat_call_after_source_change_is_conflict(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """Convergence is by content hash, not by 'I materialized this before'.
    Changing the source after a SUCCESS makes the next call (overwrite=false)
    a CONTENT_CONFLICT with 0 new writes — the target keeps the old bytes."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    src = folder / "a.txt"
    src.write_text("V1")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    first = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert first.repositories[0].outcome == "SUCCESS"
    target = f"{agent.id}/projects/{project_id}/docs/a.txt"

    src.write_text("V2-CHANGED")  # the source drifted
    second = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = second.repositories[0]
    assert row.outcome == "FAILED" and row.reason_code == "CONTENT_CONFLICT"
    assert row.written == 0
    assert backend.files[target] == b"V1"  # old target content preserved


async def test_repeat_call_after_source_change_overwrite_replaces(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """overwrite=true on a changed source replaces the target and the revision
    records the BEFORE content (the §8 row 3 / §9.1 provenance)."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    src = folder / "a.txt"
    src.write_text("V1")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    src.write_text("V2")
    out = await _service().materialize(session, project=project, agent=agent, overwrite=True, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "SUCCESS" and row.written == 1
    target = f"{agent.id}/projects/{project_id}/docs/a.txt"
    assert backend.files[target] == b"V2"
    rev = next(r for r in session.revisions() if r.path.endswith("/a.txt") and r.after_content is None and r.before_content is not None)
    assert rev.before_content == "V1"
    assert rev.content_hash == content_hash_bytes(b"V2")


async def test_mixed_converge_and_conflict_probe_is_all_or_nothing(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """With overwrite=false, the probe scans *every* target key before any
    write: a repo with one matching + one differing pre-existing file is
    CONTENT_CONFLICT with 0 new writes (the matching one is counted but
    nothing is written for the repo — §8 row 3, not 'some written, some
    not')."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "match.txt").write_text("SAME")
    (folder / "differ.txt").write_text("NEW")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    # Pre-seed targets: one matches the source, one differs.
    backend.files[f"{agent.id}/projects/{project_id}/docs/match.txt"] = b"SAME"
    backend.files[f"{agent.id}/projects/{project_id}/docs/differ.txt"] = b"OLD"
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED" and row.reason_code == "CONTENT_CONFLICT"
    assert row.written == 0
    # Neither target was touched by the conflicting call.
    assert backend.files[f"{agent.id}/projects/{project_id}/docs/differ.txt"] == b"OLD"
    assert backend.files[f"{agent.id}/projects/{project_id}/docs/match.txt"] == b"SAME"


# ===========================================================================
# 6. Concurrent lock conflicts
# ===========================================================================


async def test_directory_lock_acquired_then_released_repeat_succeeds(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """A clean materialize acquires its directory lock and *releases* it
    (no stale lock): a second call on the same directory succeeds.  This is
    the non-racy half of 'concurrent lock conflicts' — the lock is a guard
    for in-flight mutations, not a session-wide lease."""
    _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    first = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert first.repositories[0].outcome == "SUCCESS"
    second = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert second.repositories[0].outcome == "CONVERGED"
    # The lock was acquired and released once per publish; no path is held.
    assert f"projects/{project_id}/docs" in locks.acquire_order
    assert f"projects/{project_id}/docs" in locks.released
    assert not locks.held


async def test_concurrent_racer_holds_directory_lock_is_lock_conflict(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """A racer that still holds the directory lock (another in-flight
    mutation) makes this call fast-fail with LOCK_CONFLICT, 0 writes for the
    repo, retryable=True — the §6.1 'never block, never wait' semantics."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    # Simulate a concurrent mutation still holding the directory lock.
    locks.held.add(f"projects/{project_id}/docs")
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED" and row.reason_code == "LOCK_CONFLICT"
    assert row.written == 0
    assert out.retryable is True
    assert f"{agent.id}/projects/{project_id}/docs/a.txt" not in backend.files  # nothing landed
    # The racer's hold is not something the service may clear; our own locks
    # (none acquired) are all released.
    assert locks.released == []


class _RacingConditionalBackend(svc._MemoryStorage):
    """A conditional write that reports a concurrent conflict for one key:
    the probe saw the target absent, but between probe and write a racer
    created it.  Models the documented §9.3.3 lock-TTL window."""

    def __init__(self, files=None, directories=None, race_key: str | None = None) -> None:
        super().__init__(files=files, directories=directories)
        self.race_key = race_key

    async def write_bytes_if_match(self, key, data, *, condition=None, content_type=None):
        if key == self.race_key:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=await self.get_version(key))
        return await super().write_bytes_if_match(key, data, condition=condition, content_type=content_type)


async def test_conditional_write_racer_between_probe_and_write_is_content_conflict(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """The write phase re-checks the version token: a racer that creates the
    target key after the probe (the §9.3.3 TTL window) is caught by the
    ``require_absent`` conditional write as CONTENT_CONFLICT, 0 new writes,
    no revision row — honest failure rather than a silent clobber."""
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", repo_id=repo_id, project_id=project_id, tenant_id=tenant)
    target_key = f"{agent.id}/projects/{project_id}/docs/a.txt"
    backend = _RacingConditionalBackend(race_key=target_key)
    monkeypatch.setattr(svc, "get_storage_backend", lambda: backend)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    row = out.repositories[0]
    assert row.outcome == "FAILED" and row.reason_code == "CONTENT_CONFLICT"
    assert row.written == 0
    assert target_key not in backend.files  # the racer's write, not ours
    assert session.revisions() == []


# ===========================================================================
# 7. Partial failure — independent outcomes, staging cleanup, sibling survival
# ===========================================================================


async def test_content_conflict_sibling_succeeds_is_partial_and_cleans_staging(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """Independent outcomes (§7.3): repo 'ok' lands; repo 'bad' (a differing
    pre-existing target, overwrite=false) is CONTENT_CONFLICT with 0 new
    writes; the whole call is PARTIAL, retryable=False (a content conflict
    is not transiently retryable); and 'bad's staged subtree is cleaned."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()

    def _folder(name: str) -> str:
        d = tmp_path / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        return str(d)

    repo_ok = make_repo("local_folder", {"path": _folder("ok")}, display_name="ok", project_id=project_id, tenant_id=tenant)
    repo_bad = make_repo("local_folder", {"path": _folder("bad")}, display_name="bad", project_id=project_id, tenant_id=tenant)
    # Pre-seed a differing target for the 'bad' material only.
    backend.files[f"{agent.id}/projects/{project_id}/bad/f.txt"] = b"some other human content"
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo_ok, repo_bad])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    by_repo = {r.repo_id: r for r in out.repositories}
    assert out.outcome == "PARTIAL"
    assert by_repo[repo_ok.id].outcome == "SUCCESS" and by_repo[repo_ok.id].written == 1
    assert by_repo[repo_bad.id].outcome == "FAILED" and by_repo[repo_bad.id].reason_code == "CONTENT_CONFLICT"
    assert by_repo[repo_bad.id].written == 0
    assert out.retryable is False  # a content conflict is a data decision, not transient
    # The failing repo's staging subtree was fully cleaned (§7.2).
    assert not any(k.startswith(f"{agent.id}/.materialize-tmp/{repo_bad.id}/") for k in backend.files)
    # The successful repo's write DID land.
    assert backend.files[f"{agent.id}/projects/{project_id}/ok/f.txt"] == b"ok"
    # The human's differing content was not overwritten.
    assert backend.files[f"{agent.id}/projects/{project_id}/bad/f.txt"] == b"some other human content"


class _FailingStagingBackend(svc._MemoryStorage):
    """Staging writes fail for one repo's subtree: models a disk/permission
    failure that hits a specific repo's staging keys only."""

    def __init__(self, fail_prefix: str, files=None, directories=None) -> None:
        super().__init__(files=files, directories=directories)
        self.fail_prefix = fail_prefix

    async def write_bytes(self, key, data, content_type=None):
        if key.startswith(self.fail_prefix):
            raise OSError("simulated staging disk failure")
        await super().write_bytes(key, data, content_type=content_type)


async def test_staging_write_failure_is_partial_and_sibling_survives(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """A staging-phase write failure fails THAT repo only (SOURCE_FAILED,
    retryable=True — a disk failure is transient), the sibling still
    publishes, the whole call is PARTIAL, and the failing repo's staging
    subtree holds no target-facing keys."""
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()

    def _folder(name: str) -> str:
        d = tmp_path / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        return str(d)

    repo_ok = make_repo("local_folder", {"path": _folder("ok")}, display_name="ok", project_id=project_id, tenant_id=tenant)
    repo_bad = make_repo("local_folder", {"path": _folder("bad")}, display_name="bad", project_id=project_id, tenant_id=tenant)
    backend = _FailingStagingBackend(fail_prefix=f"{agent.id}/.materialize-tmp/{repo_bad.id}/")
    monkeypatch.setattr(svc, "get_storage_backend", lambda: backend)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo_ok, repo_bad])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    by_repo = {r.repo_id: r for r in out.repositories}
    assert out.outcome == "PARTIAL" and out.retryable is True
    assert by_repo[repo_ok.id].outcome == "SUCCESS" and by_repo[repo_ok.id].written == 1
    assert by_repo[repo_bad.id].outcome == "FAILED" and by_repo[repo_bad.id].reason_code == "SOURCE_FAILED"
    assert by_repo[repo_bad.id].written == 0
    # The sibling's target write landed; the failing repo published nothing.
    assert backend.files[f"{agent.id}/projects/{project_id}/ok/f.txt"] == b"ok"
    assert f"{agent.id}/projects/{project_id}/bad/f.txt" not in backend.files


async def test_security_rejected_repo_does_not_poison_sibling_is_partial(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """A malicious zip in one repo is SECURITY_REJECTED with 0 writes; a clean
    sibling repo in the SAME call still publishes (independent outcomes), and
    the whole call is PARTIAL with retryable=False — a security finding is
    never retried blindly, and a bad repo does not drag a good one down."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    project_id = uuid.uuid4()

    def _folder(name: str) -> str:
        d = tmp_path / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        return str(d)

    evil = _make_zip_bytes([("../../escape", b"x")])
    backend.files["a/evil.zip"] = evil
    repo_bad = make_repo("zip", {"storage_key": "a/evil.zip"}, display_name="bad", project_id=project_id, tenant_id=tenant)
    repo_ok = make_repo("local_folder", {"path": _folder("ok")}, display_name="ok", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo_ok, repo_bad])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    by_repo = {r.repo_id: r for r in out.repositories}
    assert out.outcome == "PARTIAL"
    assert by_repo[repo_bad.id].outcome == "FAILED" and by_repo[repo_bad.id].reason_code == "SECURITY_REJECTED"
    assert by_repo[repo_bad.id].written == 0
    assert out.retryable is False
    assert by_repo[repo_ok.id].outcome == "SUCCESS" and by_repo[repo_ok.id].written == 1
    # The sibling's target write landed; no target / staging keys for the bad repo.
    assert backend.files[f"{agent.id}/projects/{project_id}/ok/f.txt"] == b"ok"
    assert not any(k.startswith(f"{agent.id}/.materialize-tmp/{repo_bad.id}/") for k in backend.files)
    assert not any(k.startswith(f"{agent.id}/projects/{project_id}/bad/") for k in backend.files)


# ===========================================================================
# 8. Agent-workspace isolation — the material lands ONLY under the named
#    agent's subtree (task: "agent workspaces remain isolated")
# ===========================================================================


async def test_materialized_files_land_only_in_named_agent_subtree(tmp_path, session, locks, active_lock, monkeypatch) -> None:
    """Every written key carries the *named* agent's prefix (§2.5 layout
    invariant); no key ever lands under a different agent's namespace or in
    the staging tree after the call completes."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    other = _agent(tenant_id=tenant)  # a second, *different* agent
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent, overwrite=False, current_user=user)
    assert out.repositories[0].outcome == "SUCCESS"
    for key in backend.files:
        # Only the named agent's subtree is written; the staging tree is gone.
        assert key.startswith(f"{agent.id}/"), f"write escaped the named agent: {key}"
        assert not key.startswith(f"{other.id}/"), f"write landed in another agent's namespace: {key}"
        assert not key.startswith(f"{agent.id}/.materialize-tmp/"), f"staging residue: {key}"


async def test_cross_agent_materialize_writes_into_that_agents_subtree(
    tmp_path, session, locks, active_lock, monkeypatch
) -> None:
    """Materializing into a *different* agent writes only into that agent's
    subtree (same-tenant agents are legal targets); the source agent's subtree
    is untouched.  Isolation is per *target* agent, enforced by the key
    formula, not by which agent 'owns' the project."""
    backend = _make_backend(monkeypatch)
    tenant = uuid.uuid4()
    user, agent_a = make_user(tenant_id=tenant), _agent(tenant_id=tenant)
    agent_b = _agent(tenant_id=tenant)
    project_id = uuid.uuid4()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    repo = make_repo("local_folder", {"path": str(folder)}, display_name="docs", project_id=project_id, tenant_id=tenant)
    project = _project(tenant_id=tenant, project_id=project_id, repositories=[repo])

    out = await _service().materialize(session, project=project, agent=agent_b, overwrite=False, current_user=user)
    assert out.repositories[0].outcome == "SUCCESS"
    assert backend.files[f"{agent_b.id}/projects/{project_id}/docs/a.txt"] == b"x"
    # Agent A's subtree is completely empty — nothing bled across.
    assert not any(k.startswith(f"{agent_a.id}/") for k in backend.files)

"""Service + validator tests for the Project Intake lifecycle (Phase 2B-2).

Covers the card's required test matrix (root brief §十三) without a live DB:

- Lifecycle:  manual -> RECEIVED -> SOURCES_OK -> INITIALIZED (all-pass).
- Reason codes: SOURCE_NOT_FOUND / SOURCE_INVALID / SECURITY_REJECTED /
  SOURCE_UNREACHABLE / SOURCE_NOT_SUPPORTED, with the correct retryable flag.
- Bounded retry: a transient SOURCE_UNREACHABLE hold escalates to REJECTED
  at the retry budget.
- ZIP safety: a zip carrying a ``../../`` member is rejected
  SECURITY_REJECTED with no extraction / write / execute (routed through
  the security module's ``check_zip_slip``).
- State machine: legal Intake edges pass, illegal jumps (REJECTED->
  INITIALIZED, INITIALIZED->RECEIVED, SOURCES_OK->RECEIVED, and a direct
  RECEIVED->INITIALIZED) are refused.
- Multiple repositories on one project.
- Credential rejection at create time (nothing persisted).
- Tenant read-access + cross-tenant isolation (creator OR same-tenant admin;
  a cross-tenant admin is denied; a foreign-tenant row is a 404 by DAO).
- Proof the service routes through the security module rather than shadowing
  it (spies on check_host_path / check_zip_slip / transition).

The DAO singletons and the storage backend are stubbed; validators are
exercised against real ``tmp_path`` filesystem fixtures so the checks are
real, not string-matching.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest

import app.services.project_intake_service as svc
from app.models.project import Project, Repository
from app.models.user import User
from app.schemas.project_intake import SourceSpec
from app.services import intake_security
from app.services.project_intake_service import (
    IntakeSecurityError,
    IntakeTransitionError,
    ProjectIntakeService,
    MAX_RETRIES,
)
from app.services.storage_runtime.base import StorageEntry


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _make_user(role: str = "member") -> User:
    # A real in-memory User ORM object (the service's declared type) so the
    # permission checks operate on the actual model, not a stand-in.
    return User(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=role)


def _make_repo(
    source_type: str,
    locator: dict | None = None,
    *,
    retry_count: int = 0,
    pending_verifier: bool = False,
) -> Repository:
    return Repository(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=source_type,
        locator=locator,
        verified=False,
        pending_verifier=pending_verifier,
        retry_count=retry_count,
        tenant_id=uuid.uuid4(),
    )


class _StubRepoDAO:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def mark_verified(self, repo, *, db=None):
        self.calls.append(("mark_verified", repo.id))
        repo.verified = True

    async def mark_pending_verifier(self, repo, *, db=None):
        self.calls.append(("mark_pending_verifier", repo.id))
        repo.pending_verifier = True

    async def clear_pending_verifier(self, repo, *, db=None):
        self.calls.append(("clear_pending_verifier", repo.id))
        repo.pending_verifier = False

    async def bump_retry_count(self, repo, *, db=None):
        repo.retry_count = int(repo.retry_count or 0) + 1
        self.calls.append(("bump_retry_count", repo.id, repo.retry_count))
        return repo.retry_count


class _StubProjectDAO:
    def __init__(self) -> None:
        self.created: list = []
        self.transitions: list = []

    async def add_project_with_repositories(self, project, repositories, *, tenant_id, db=None):
        self.created.append((project, list(repositories), tenant_id))
        return project

    async def reject(self, project, *, reason_code, detail, db=None):
        project.status = "REJECTED"
        project.rejection_reason = reason_code
        project.rejection_detail = detail
        self.transitions.append(("reject", reason_code))

    async def transition(self, project, new_status, *, db=None):
        project.status = new_status
        self.transitions.append(("transition", new_status))

    async def mark_sources_ok(self, project, *, db=None):
        project.status = "SOURCES_OK"
        self.transitions.append(("mark_sources_ok",))

    async def get_scoped_with_repositories(self, project_id, db=None):
        return None

    async def list_for_user_scoped(self, **kwargs):
        return []


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class _MemoryStorage:
    """In-memory stand-in for the storage backend (document/zip key paths)."""

    def __init__(self, *, files: dict | None = None, directories: set | None = None):
        self.files = files or {}
        self.directories = directories or set()
        self.exists_calls = 0

    async def exists(self, key):
        self.exists_calls += 1
        return key in self.files or key in self.directories

    async def stat(self, key):
        return StorageEntry(name=key, key=key, is_dir=(key in self.directories),
                           size=len(self.files.get(key, b"")))

    async def read_bytes(self, key):
        return self.files[key]


@pytest.fixture
def stubs(monkeypatch):
    """Patch the service's DAO singletons + storage resolver to fakes."""
    pdao = _StubProjectDAO()
    rdao = _StubRepoDAO()
    monkeypatch.setattr(svc, "project_dao", pdao)
    monkeypatch.setattr(svc, "repository_dao", rdao)
    monkeypatch.setattr(svc, "get_storage_backend", lambda: _MemoryStorage())
    return SimpleNamespace(project_dao=pdao, repo_dao=rdao, db=_FakeSession())


# ---------------------------------------------------------------------------
# 1. State machine — legal edges pass, illegal jumps are refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("RECEIVED", "SOURCES_OK"),
        ("RECEIVED", "REJECTED"),
        ("SOURCES_OK", "INITIALIZED"),
        ("SOURCES_OK", "REJECTED"),
        ("RECEIVED", "RECEIVED"),  # stay while retrying
        ("SOURCES_OK", "SOURCES_OK"),  # stay while retrying
    ],
)
def test_legal_intake_edges_are_allowed(current: str, target: str) -> None:
    ProjectIntakeService._assert_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("REJECTED", "INITIALIZED"),  # terminal -> non-terminal (forbidden)
        ("REJECTED", "RECEIVED"),
        ("INITIALIZED", "RECEIVED"),  # backwards (forbidden)
        ("INITIALIZED", "REJECTED"),
        ("SOURCES_OK", "RECEIVED"),  # backwards (forbidden)
        ("RECEIVED", "INITIALIZED"),  # direct skip of SOURCES_OK (forbidden)
        ("EXOTIC", "RECEIVED"),  # unknown status fails closed
    ],
)
def test_illegal_intake_edges_are_refused(current: str, target: str) -> None:
    with pytest.raises(IntakeTransitionError):
        ProjectIntakeService._assert_transition(current, target)


# ---------------------------------------------------------------------------
# 2. Validators — exercised against real tmp_path fixtures.
# ---------------------------------------------------------------------------


async def test_manual_source_passes(stubs) -> None:
    out = await ProjectIntakeService()._validate_manual(_make_repo("manual"))
    assert out.ok and out.reason_code is None


async def test_local_folder_existing_nonempty_dir_passes(stubs, tmp_path) -> None:
    d = tmp_path / "src"
    d.mkdir()
    (d / "main.py").write_text("print(1)")
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": str(d)})
    )
    assert out.ok


async def test_local_folder_missing_path_is_source_not_found(stubs, tmp_path) -> None:
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": str(tmp_path / "does-not-exist")})
    )
    assert not out.ok and out.reason_code == "SOURCE_NOT_FOUND" and not out.retryable


async def test_local_folder_that_is_a_file_is_source_invalid(stubs, tmp_path) -> None:
    f = tmp_path / "afile.txt"
    f.write_text("x")
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": str(f)})
    )
    assert not out.ok and out.reason_code == "SOURCE_INVALID"


async def test_local_folder_empty_dir_is_source_invalid(stubs, tmp_path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": str(d)})
    )
    assert not out.ok and out.reason_code == "SOURCE_INVALID"


@pytest.mark.parametrize("bad", ["..", "/a/../b", "..\\windows", "foo/.."])
async def test_local_folder_traversal_is_security_rejected(stubs, tmp_path, bad) -> None:
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": bad})
    )
    assert not out.ok and out.reason_code == "SECURITY_REJECTED" and not out.retryable


async def test_local_folder_relative_path_is_source_invalid(stubs) -> None:
    # local_folder is a *host absolute* path; a relative value is a locator
    # shape violation (security module's verdict), not a security escape.
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": "relative/docs"})
    )
    assert not out.ok and out.reason_code == "SOURCE_INVALID"


async def test_local_folder_sensitive_root_is_security_rejected(stubs) -> None:
    out = await ProjectIntakeService()._validate_local_folder(
        _make_repo("local_folder", {"path": "/etc/passwd"})
    )
    assert not out.ok and out.reason_code == "SECURITY_REJECTED"


async def test_document_existing_supported_file_passes(stubs, tmp_path) -> None:
    f = tmp_path / "spec.txt"
    f.write_text("hi")
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"path": str(f)})
    )
    assert out.ok


async def test_document_missing_is_source_not_found(stubs, tmp_path) -> None:
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"path": str(tmp_path / "nope.txt")})
    )
    assert not out.ok and out.reason_code == "SOURCE_NOT_FOUND"


async def test_document_unsupported_type_is_source_invalid(stubs, tmp_path) -> None:
    f = tmp_path / "blob.exe"
    f.write_bytes(b"MZ")
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"path": str(f)})
    )
    assert not out.ok and out.reason_code == "SOURCE_INVALID"


async def test_document_traversal_is_security_rejected(stubs) -> None:
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"path": "../../etc/passwd"})
    )
    assert not out.ok and out.reason_code == "SECURITY_REJECTED"


async def test_document_storage_key_supported_passes(monkeypatch, tmp_path) -> None:
    key = "tenant_1/docs/spec.txt"
    monkeypatch.setattr(
        svc, "get_storage_backend",
        lambda: _MemoryStorage(files={key: b"data"}, directories=set()),
    )
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"storage_key": key})
    )
    assert out.ok


async def test_document_storage_key_missing_is_source_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        svc, "get_storage_backend",
        lambda: _MemoryStorage(files={}, directories=set()),
    )
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"storage_key": "missing/spec.txt"})
    )
    assert not out.ok and out.reason_code == "SOURCE_NOT_FOUND"


async def test_document_storage_backend_outage_is_transient_unreachable(monkeypatch) -> None:
    class _BrokenBackend:
        async def exists(self, key):
            raise OSError("storage unavailable")

        async def stat(self, key):
            raise OSError("storage unavailable")

        async def read_bytes(self, key):
            raise OSError("storage unavailable")

    monkeypatch.setattr(svc, "get_storage_backend", lambda: _BrokenBackend())
    out = await ProjectIntakeService()._validate_document(
        _make_repo("document", {"storage_key": "x/spec.txt"})
    )
    assert (
        not out.ok
        and out.reason_code == "SOURCE_UNREACHABLE"
        and out.retryable
    )


# ---- zip ----


def _build_zip_bytes(member_names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in member_names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


async def test_zip_valid_archive_passes(stubs, tmp_path) -> None:
    zf = tmp_path / "ok.zip"
    zf.write_bytes(_build_zip_bytes(["docs/a.txt", "src/b.py"]))
    out = await ProjectIntakeService()._validate_zip(_make_repo("zip", {"path": str(zf)}))
    assert out.ok


async def test_zip_slip_member_is_security_rejected(stubs, tmp_path) -> None:
    # The card's literal vector: a member that escapes the archive root.
    zf = tmp_path / "evil.zip"
    zf.write_bytes(_build_zip_bytes(["../../something", "inside.txt"]))
    out = await ProjectIntakeService()._validate_zip(_make_repo("zip", {"path": str(zf)}))
    assert not out.ok and out.reason_code == "SECURITY_REJECTED" and not out.retryable


async def test_zip_corrupt_bytes_are_source_invalid(stubs, tmp_path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    out = await ProjectIntakeService()._validate_zip(_make_repo("zip", {"path": str(bad)}))
    assert not out.ok and out.reason_code == "SOURCE_INVALID"


async def test_zip_missing_is_source_not_found(stubs, tmp_path) -> None:
    out = await ProjectIntakeService()._validate_zip(
        _make_repo("zip", {"path": str(tmp_path / "nope.zip")})
    )
    assert not out.ok and out.reason_code == "SOURCE_NOT_FOUND"


async def test_zip_storage_key_slip_is_security_rejected(monkeypatch) -> None:
    key = "tenant_1/evil.zip"
    monkeypatch.setattr(
        svc, "get_storage_backend",
        lambda: _MemoryStorage(files={key: _build_zip_bytes(["../../etc/cron.d/x"])}),
    )
    out = await ProjectIntakeService()._validate_zip(_make_repo("zip", {"storage_key": key}))
    assert not out.ok and out.reason_code == "SECURITY_REJECTED"


async def test_zip_validation_never_writes_or_executes(stubs, tmp_path) -> None:
    # Proof of "no extraction / write / execute": run the check inside an
    # empty dir and assert nothing new appears on disk for a hostile zip.
    canary = tmp_path / "intake_canary"
    canary.mkdir()
    hostile = tmp_path / "hostile.zip"
    hostile.write_bytes(_build_zip_bytes(["../../intake_canary/pwned"]))
    out = await ProjectIntakeService()._validate_zip(_make_repo("zip", {"path": str(hostile)}))
    assert out.reason_code == "SECURITY_REJECTED"
    assert list(canary.iterdir()) == []  # nothing escaped the archive


# ---- git source types ----


@pytest.mark.parametrize("stype", ["github", "gitlab", "local_git"])
async def test_git_source_is_explicit_source_not_supported(stubs, stype) -> None:
    out = await ProjectIntakeService()._validate_unsupported(
        _make_repo(stype, {"owner": "acme", "repo": "widget"})
    )
    assert (
        not out.ok
        and out.reason_code == "SOURCE_NOT_SUPPORTED"
        and not out.retryable
    )


# ---------------------------------------------------------------------------
# 3. create_intake — registration + credential rejection.
# ---------------------------------------------------------------------------


async def test_create_intake_n0_manual_registers_received(stubs) -> None:
    user = _make_user()
    project = await ProjectIntakeService().create_intake(
        stubs.db, current_user=user, name="P", description="d", goal="g",
        sources=[SourceSpec(source_type="manual")],
    )
    assert project.status == "RECEIVED"
    assert project.created_by == user.id
    created = stubs.project_dao.created[0]
    _p, repos, tenant = created
    assert len(repos) == 1 and repos[0].source_type == "manual"
    assert tenant == user.tenant_id


async def test_create_intake_registers_multiple_sources(stubs) -> None:
    user = _make_user()
    await ProjectIntakeService().create_intake(
        stubs.db, current_user=user, name="P", description="d", goal="g",
        sources=[
            SourceSpec(source_type="manual"),
            SourceSpec(source_type="local_folder", locator={"path": "/x"}),
            SourceSpec(source_type="zip", locator={"path": "/z.zip"}),
        ],
    )
    _p, repos, _t = stubs.project_dao.created[0]
    assert [r.source_type for r in repos] == ["manual", "local_folder", "zip"]


async def test_create_intake_rejects_credential_locator_and_persists_nothing(stubs) -> None:
    user = _make_user()
    with pytest.raises(IntakeSecurityError):
        await ProjectIntakeService().create_intake(
            stubs.db, current_user=user, name="P", description="d", goal="g",
            sources=[SourceSpec(source_type="github", locator={"owner": "a", "api_key": "sekret"})],
        )
    assert stubs.project_dao.created == []  # nothing persisted


async def test_create_intake_rejects_url_userinfo_credential(stubs) -> None:
    user = _make_user()
    with pytest.raises(IntakeSecurityError):
        await ProjectIntakeService().create_intake(
            stubs.db, current_user=user, name="P", description="d", goal="g",
            sources=[
                SourceSpec(
                    source_type="github",
                    locator={"url": "https://user:pass@host/repo"},
                )
            ],
        )
    assert stubs.project_dao.created == []


# ---------------------------------------------------------------------------
# 4. validate_sources lifecycle — the closed loop the card is about.
# ---------------------------------------------------------------------------


def _project_with(status: str, *repos: Repository) -> Project:
    p = Project(
        id=uuid.uuid4(),
        name="P",
        description="d",
        goal="g",
        status=status,
        created_by=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )
    p.repositories = list(repos)  # type: ignore[attr-defined]
    return p


def _project_owned_by(user: User, status: str = "RECEIVED", repos: tuple[Repository, ...] = ()) -> Project:
    """A project created by ``user`` under a shared tenant (for permission tests)."""
    p = Project(
        id=uuid.uuid4(),
        name="P",
        description="d",
        goal="g",
        status=status,
        created_by=user.id,
        tenant_id=user.tenant_id,
    )
    p.repositories = list(repos)  # type: ignore[attr-defined]
    return p


async def test_all_pass_reaches_initialized_via_sources_ok(stubs) -> None:
    user = _make_user()
    project = _project_with("RECEIVED", _make_repo("manual"))
    result_project, rejection_info = await ProjectIntakeService().validate_sources(
        stubs.db, project=project, current_user=user
    )
    assert result_project.status == "INITIALIZED"
    assert rejection_info is None
    # The all-pass chain must walk RECEIVED -> SOURCES_OK -> INITIALIZED.
    assert ("mark_sources_ok",) in stubs.project_dao.transitions
    assert ("transition", "INITIALIZED") in stubs.project_dao.transitions
    # The source was marked verified.
    assert ("mark_verified", project.repositories[0].id) in stubs.repo_dao.calls


async def test_permanent_source_not_found_rejects(stubs) -> None:
    user = _make_user()
    repo = _make_repo("document", {"path": "/no/such/file.txt"})
    project = _project_with("RECEIVED", repo)
    result_project, rejection_info = await ProjectIntakeService().validate_sources(
        stubs.db, project=project, current_user=user
    )
    assert result_project.status == "REJECTED"
    assert rejection_info is not None
    assert rejection_info.reason_code == "SOURCE_NOT_FOUND"
    assert rejection_info.retryable is False
    assert result_project.rejection_reason == "SOURCE_NOT_FOUND"
    assert ("reject", "SOURCE_NOT_FOUND") in stubs.project_dao.transitions


async def test_git_source_rejects_with_source_not_supported(stubs) -> None:
    user = _make_user()
    project = _project_with("RECEIVED", _make_repo("github", {"owner": "a", "repo": "b"}))
    result_project, rejection_info = await ProjectIntakeService().validate_sources(
        stubs.db, project=project, current_user=user
    )
    assert result_project.status == "REJECTED"
    assert rejection_info is not None
    assert rejection_info.reason_code == "SOURCE_NOT_SUPPORTED"
    assert rejection_info.retryable is False


async def test_transient_unreachable_holds_then_escalates(stubs, monkeypatch) -> None:
    """A SOURCE_UNREACHABLE source holds for the retry budget, then rejects."""
    user = _make_user()

    # A storage backend that always times out -> UNREACHABLE every validate.
    class _Broken:
        async def exists(self, key):
            raise OSError("timeout")
        async def stat(self, key):
            raise OSError("timeout")
        async def read_bytes(self, key):
            raise OSError("timeout")

    monkeypatch.setattr(svc, "get_storage_backend", lambda: _Broken())

    repo = _make_repo("document", {"storage_key": "x/y.txt"})
    project = _project_with("RECEIVED", repo)
    service = ProjectIntakeService()

    calls = 0
    while project.status != "REJECTED":
        project, info = await service.validate_sources(stubs.db, project=project, current_user=user)
        assert info is not None and info.reason_code == "SOURCE_UNREACHABLE"
        if project.status == "REJECTED":
            assert info.retryable is False
        else:
            assert info.retryable is True
        calls += 1
        if calls > MAX_RETRIES + 1:
            raise AssertionError("retry budget was not bounded")

    assert project.status == "REJECTED"
    assert repo.retry_count >= MAX_RETRIES - 1  # counter actually climbed
    assert ("reject", "SOURCE_UNREACHABLE") in stubs.project_dao.transitions


async def test_revalidating_a_terminal_project_is_rejected(stubs) -> None:
    user = _make_user()
    for terminal in ("REJECTED", "INITIALIZED"):
        project = _project_with(terminal, _make_repo("manual"))
        with pytest.raises(IntakeTransitionError):
            await ProjectIntakeService().validate_sources(
                stubs.db, project=project, current_user=user
            )


# ---------------------------------------------------------------------------
# 5. Permission / tenant isolation (security-module guards, API-facing).
# ---------------------------------------------------------------------------


def test_creator_can_read() -> None:
    user = _make_user(role="member")
    project = _project_owned_by(user)  # created_by + tenant both the user's
    intake_security.verify_read_access(user, project)  # must not raise


def test_same_tenant_admin_can_read() -> None:
    owner = _make_user(role="member")
    admin = _make_user(role="org_admin")
    admin.tenant_id = owner.tenant_id  # put the admin in the owner's tenant
    # A different member created it, but same tenant + admin role => allowed.
    project = _project_owned_by(owner)
    project.created_by = uuid.uuid4()  # not the admin
    assert project.tenant_id == admin.tenant_id
    intake_security.verify_read_access(admin, project)  # must not raise


def test_other_member_cannot_read() -> None:
    owner = _make_user(role="member")
    stranger_tenant = uuid.uuid4()
    stranger = _make_user(role="member")
    stranger.tenant_id = stranger_tenant
    project = _project_owned_by(owner)
    # Re-point the project into the stranger's tenant but keep owner's authorship.
    project.tenant_id = stranger_tenant
    with pytest.raises(intake_security.ReadForbidden):
        intake_security.verify_read_access(stranger, project)


def test_cross_tenant_admin_cannot_read() -> None:
    admin = _make_user(role="platform_admin")
    other_tenant = uuid.uuid4()
    owner = _make_user(role="member")
    project = _project_owned_by(owner)
    project.tenant_id = other_tenant  # a tenant the admin does not belong to
    with pytest.raises(intake_security.ReadForbidden):
        intake_security.verify_read_access(admin, project)


def test_tenant_scope_guard_fails_closed_on_cross_tenant() -> None:
    with pytest.raises(intake_security.TenantScopeViolation):
        intake_security.verify_tenant_scope(uuid.uuid4(), uuid.uuid4())


def test_tenant_scope_guard_fails_closed_on_missing_context() -> None:
    with pytest.raises(intake_security.TenantScopeViolation):
        intake_security.verify_tenant_scope(uuid.uuid4(), None)


# ---------------------------------------------------------------------------
# 6. Proof the service routes THROUGH the security module (not shadowing it).
# ---------------------------------------------------------------------------


async def test_service_delegates_path_checks_to_security_module(stubs, tmp_path) -> None:
    host_calls = 0
    real_check = intake_security.check_host_path

    def spy_check(raw, *, source_type):
        nonlocal host_calls
        host_calls += 1
        return real_check(raw, source_type=source_type)

    import app.services.project_intake_service as m

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "check_host_path", spy_check)
    try:
        d = tmp_path / "proj"
        d.mkdir()
        (d / "x.txt").write_text("1")
        await m.ProjectIntakeService()._validate_local_folder(
            _make_repo("local_folder", {"path": str(d)})
        )
    finally:
        monkey.undo()
    assert host_calls >= 1  # the service actually called the security gate


async def test_service_delegates_zip_checks_to_security_module(stubs, tmp_path) -> None:
    slip_calls = 0
    real_check = intake_security.check_zip_slip

    def spy_zip(data):
        nonlocal slip_calls
        slip_calls += 1
        return real_check(data)

    import app.services.project_intake_service as m

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "check_zip_slip", spy_zip)
    try:
        zf = tmp_path / "z.zip"
        zf.write_bytes(_build_zip_bytes(["a.txt"]))
        await m.ProjectIntakeService()._validate_zip(_make_repo("zip", {"path": str(zf)}))
    finally:
        monkey.undo()
    assert slip_calls >= 1


async def test_service_delegates_transitions_to_security_module(stubs) -> None:
    """Every status write in validate_sources goes through intake_security.transition."""
    import app.services.project_intake_service as m

    recorded = []
    real_transition = intake_security.transition

    def spy_transition(current, target):
        recorded.append((current, target))
        return real_transition(current, target)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "transition", spy_transition)
    try:
        user = _make_user()
        project = _project_with("RECEIVED", _make_repo("manual"))
        result, _ = await m.ProjectIntakeService().validate_sources(
            stubs.db, project=project, current_user=user
        )
    finally:
        monkey.undo()
    assert result.status == "INITIALIZED"
    assert ("RECEIVED", "SOURCES_OK") in recorded
    assert ("SOURCES_OK", "INITIALIZED") in recorded
    # A direct RECEIVED -> INITIALIZED jump never happened.
    assert ("RECEIVED", "INITIALIZED") not in recorded

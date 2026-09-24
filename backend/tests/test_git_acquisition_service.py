"""Git Source Acquisition tests (Phase 2B-4, card t_4874c3e7).

Design: ``docs/GIT_ACQ_DESIGN_V1.md`` (card t_82ac3524).  Three layers:

1. **Pure / unit** — the security gates must hold WITHOUT any process, DB,
   or network: the ref shape-gate (no injection / option-shaped / bare-
   integer refs reach a git child), the URL gate (https-only, no userinfo,
   no private / metadata hosts — SSRF), the local-path gate, the failure
   classifier, the secret-free stderr sanitizer, the credential resolve
   (token only in-process), the retry-budget finalizer, the post-checks
   (submodules / reserved names), the artifact publisher (agent-scoped
   key, .git pruned), and the status reconstruction (the GET's closed
   state set).
2. **Local git E2E** — a real ``git init`` repository on ``tmp_path``
   acquired through the real service + real git binary: default-branch
   resolution (never hardcoded), a branch ref, a commit-SHA pin, a
   missing ref (ACQ_REF_NOT_FOUND — "clone succeeded is NOT success"),
   and the submodule refusal.  Storage is an in-memory fake; no DB.
3. **Remote git E2E** (skip-guarded on reachability) — a public GitHub
   repository through the full pipeline, and GitLab's anonymous-auth
   posture asserted as ACQ_AUTH_FAILED (fail-closed, never retried).

The invariants asserted throughout (card §10/§17/§19, design §A/§C):
- no credential ever appears in a locator, log, audit row, or outcome;
- a security rejection happens BEFORE any process is spawned;
- tenant isolation: a cross-tenant / null-tenant acquisition raises the
  403-class ``AcquisitionSecurity`` (or the re-asserted
  ``TenantScopeViolation``), never a 409-class ACQ code;
- the acquisition never triggers a downstream Agent / Run / prompt —
  the only session writes are the repository row + one audit row;
- the published tar is agent-scoped, byte-bounded, and carries ONLY the
  working tree (no ``.git``, no hooks).
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.git_acquisition_service as gasvc
import app.services.project_materialization_service as mat_svc
from app.config import get_settings as real_get_settings
from app.core.security import encrypt_data
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.user import User
from app.schemas.project_intake import (
    ACQ_AUTH_FAILED,
    ACQ_OK,
    ACQ_REF_NOT_FOUND,
    ACQ_SECURITY_REJECTED,
    ACQ_SOURCE_INVALID,
    ACQ_SOURCE_UNREACHABLE,
    ACQ_TIMEOUT,
    SUBMODULES_UNSUPPORTED,
    AcquisitionOut,
    acq_code_is_retryable,
)
from app.services import intake_security
from app.services.git_acquisition_service import (
    AcquisitionError,
    AcquisitionSecurity,
    GitAcquisitionService,
)
from app.services.intake_security import SecurityError, TenantScopeViolation
from app.services.project_intake_service import ProjectIntakeService
from app.services.project_materialization_service import (
    _RepoRejected,
    _RepoUnreachable,
    make_plan,
)

GITHUB_E2E_URL = "https://github.com/octocat/Hello-World"
GITLAB_E2E_URL = "https://gitlab.com/gitlab-org/api-playground"
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Builders / fakes
# ---------------------------------------------------------------------------


def _user(tenant: uuid.UUID) -> User:
    return User(id=uuid.uuid4(), tenant_id=tenant, role="member")


def _agent(tenant: uuid.UUID) -> Agent:
    return Agent(id=uuid.uuid4(), name="acq-agent", creator_id=uuid.uuid4(), tenant_id=tenant)


def _project(tenant: uuid.UUID) -> Project:
    return Project(
        id=uuid.uuid4(),
        name="acq-proj",
        description="d",
        goal="g",
        status="RECEIVED",
        created_by=uuid.uuid4(),
        tenant_id=tenant,
    )


def _repo(
    source_type: str,
    locator: dict | None,
    tenant: uuid.UUID,
    *,
    verified: bool = False,
    pending_verifier: bool = False,
    retry_count: int = 0,
) -> Repository:
    return Repository(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=source_type,
        locator=locator,
        verified=verified,
        pending_verifier=pending_verifier,
        retry_count=retry_count,
        tenant_id=tenant,
    )


class _FakeDB:
    """Records ``add`` / ``flush``; no real DB (the card's evidence rule:
    the acquisition writes exactly the repo row + one audit row, nothing
    that could start an Agent / Run)."""

    def __init__(self) -> None:
        self.added: list = []
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


class _AcqStorage:
    """In-memory stand-in for the storage facade subset the service uses:
    ``write_bytes`` (the artifact publish) + ``delete_tree`` (the failure
    cleanup invariant) + ``exists`` / ``read_bytes`` (the reader side)."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})

    async def write_bytes(self, key: str, data: bytes) -> None:
        self.files[key] = bytes(data)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for k in [k for k in self.files if k == key or k.startswith(prefix)]:
            self.files.pop(k, None)

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def read_bytes(self, key: str) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]


def _patch_storage(monkeypatch, fake: _AcqStorage) -> None:
    monkeypatch.setattr(gasvc, "get_storage_backend", lambda: fake)


def _patch_no_credentials(monkeypatch) -> None:
    async def _rows(agent_id: uuid.UUID) -> list:
        return []

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))


def _git_run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    return proc.stdout.decode().strip()


def make_local_repo(base: Path, *, branch: str = "feature/x") -> dict:
    """A real git repository on disk: two commits (``rev1`` on the default
    branch, ``rev2`` on ``branch``) + a submodule marker variant.  The
    default branch is named explicitly (``acqmain``) so no test depends on
    a host-level init.defaultBranch config."""
    repo_dir = base / "upstream"
    repo_dir.mkdir()
    _git_run(["init", "-q", "-b", "acqmain"], repo_dir)
    _git_run(["config", "user.email", "acq@example.com"], repo_dir)
    _git_run(["config", "user.name", "acq-test"], repo_dir)
    (repo_dir / "a.txt").write_text("alpha\n")
    _git_run(["add", "a.txt"], repo_dir)
    _git_run(["commit", "-q", "-m", "one"], repo_dir)
    rev1 = _git_run(["rev-parse", "HEAD"], repo_dir)
    _git_run(["checkout", "-q", "-b", branch], repo_dir)
    (repo_dir / "b.txt").write_text("beta\n")
    _git_run(["add", "b.txt"], repo_dir)
    _git_run(["commit", "-q", "-m", "two"], repo_dir)
    rev2 = _git_run(["rev-parse", "HEAD"], repo_dir)
    return {"dir": repo_dir, "rev1": rev1, "rev2": rev2, "branch": branch}


@pytest.fixture
def svc() -> GitAcquisitionService:
    return GitAcquisitionService()


# ---------------------------------------------------------------------------
# 1. Entry gates — tenant isolation before ANY I/O (design §C.5 / card §19)
# ---------------------------------------------------------------------------


def test_entry_gate_passes_within_one_tenant(svc) -> None:
    tenant = uuid.uuid4()
    svc._entry_gates(_project(tenant), _repo("github", {"url": "https://github.com/o/r"}, tenant), _agent(tenant), _user(tenant))
    # no exception


def test_entry_gate_cross_tenant_agent_raises_security(svc) -> None:
    # The M9 fourth gate: the acting user and the project share a tenant,
    # but the TARGET agent belongs to another tenant -> the 403-class
    # isolation finding (NOT a closed ACQ code, NOT a 409).
    tenant = uuid.uuid4()
    with pytest.raises(AcquisitionSecurity):
        svc._entry_gates(_project(tenant), _repo("github", {"url": GITHUB_E2E_URL}, tenant), _agent(uuid.uuid4()), _user(tenant))


def test_entry_gate_null_agent_tenant_raises_security(svc) -> None:
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    agent.tenant_id = None
    with pytest.raises(AcquisitionSecurity):
        svc._entry_gates(_project(tenant), _repo("github", {"url": GITHUB_E2E_URL}, tenant), agent, _user(tenant))


def test_entry_gate_missing_project_tenant_fails_closed() -> None:
    # verify_tenant_scope re-asserts first: a null object tenant is a
    # TenantScopeViolation (403-class), not a guessed pass.
    tenant = uuid.uuid4()
    project = _project(tenant)
    project.tenant_id = None
    with pytest.raises(TenantScopeViolation):
        gasvc.GitAcquisitionService()._entry_gates(project, _repo("github", {"url": GITHUB_E2E_URL}, tenant), _agent(tenant), _user(tenant))


def test_entry_gate_cross_tenant_user_fails_closed() -> None:
    with pytest.raises(TenantScopeViolation):
        gasvc.GitAcquisitionService()._entry_gates(
            _project(uuid.uuid4()),
            _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4()),
            _agent(uuid.uuid4()),
            _user(uuid.uuid4()),
        )


def test_entry_gate_non_git_source_is_closed_acq_code(svc) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(AcquisitionError) as exc_info:
        svc._entry_gates(_project(tenant), _repo("manual", {}, tenant), _agent(tenant), _user(tenant))
    assert exc_info.value.code == ACQ_SOURCE_INVALID
    # a programming error, NOT an isolation finding: the 409-class closed
    # code, not AcquisitionSecurity.


def test_acquisition_security_is_a_security_error() -> None:
    # One narrow 403 mapping at the transport: AcquisitionSecurity joins
    # the security module's SecurityError family (the materialization
    # handler's MaterializationSecurity precedent).
    assert issubclass(AcquisitionSecurity, SecurityError)


# ---------------------------------------------------------------------------
# 2. Ref shape-gate — the git child NEVER sees an unvalidated ref (card §7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref, expect",
    [
        ("a" * 40, "sha"),  # full commit pin
        ("abc1234", "sha"),  # 7-hex commit prefix (fetched, verified)
        ("main", "ref"),
        ("release/v2.1-hotfix", "ref"),
        ("feature/x", "ref"),
        ("42", "reject"),  # bare integer — not a branch/tag worth passing to git
        ("-m", "reject"),  # option-shaped: git parses a leading-dash argv as a flag
        ("-upload-pack=evil", "reject"),
        ("x" * 129, "reject"),  # over the 128-char bound
        ("a\x00b", "reject"),  # NUL byte
        ("", "none"),
    ],
)
def test_ref_shape_gate(svc, ref: str, expect: str) -> None:
    agent = _agent(uuid.uuid4())
    if ref == "":
        out = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4())
    else:
        out = _repo("github", {"url": GITHUB_E2E_URL, "branch": ref}, uuid.uuid4())
    if expect == "reject":
        with pytest.raises(AcquisitionError) as exc_info:
            svc._resolve_ref_and_provider(out, agent)
        assert exc_info.value.code == ACQ_SECURITY_REJECTED
    else:
        requested, provider, is_sha = svc._resolve_ref_and_provider(out, agent)
        assert provider == "github"
        if expect == "none":
            assert requested is None and not is_sha
        elif expect == "sha":
            assert requested == ref and is_sha
        else:
            assert requested == ref and not is_sha


def test_ref_shape_gate_tag_locator_key(svc) -> None:
    repo = _repo("github", {"url": GITHUB_E2E_URL, "tag": "v1.0"}, uuid.uuid4())
    requested, _provider, is_sha = svc._resolve_ref_and_provider(repo, _agent(uuid.uuid4()))
    assert requested == "v1.0" and not is_sha


# ---------------------------------------------------------------------------
# 3. URL gate — https-only, no userinfo, no private/metadata hosts (SSRF)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, detail_substring",
    [
        ("file:///etc/passwd", "scheme"),  # local-file scheme: SSRF-adjacent, forbidden
        ("ssh://git@github.com/o/r.git", "scheme"),
        ("ftp://github.com/o/r", "scheme"),
        ("http://github.com/o/r", "scheme"),  # plain http: no https guarantee
        ("https://user:pass@github.com/o/r", "userinfo"),  # credentials in the URL
        ("https://169.254.169.254/latest/meta-data", "private / reserved ip literal"),  # cloud metadata (link-local)
        ("https://192.168.0.1/x", "private / reserved ip literal"),  # RFC1918 private
        ("https://10.0.0.5/x", "private / reserved ip literal"),
    ],
)
def test_remote_url_gate_rejects_unsafe_urls(svc, url: str, detail_substring: str) -> None:
    repo = _repo("github", {"url": url}, uuid.uuid4())
    with pytest.raises(AcquisitionError) as exc_info:
        svc._validate_remote_url(repo)
    assert exc_info.value.code == ACQ_SECURITY_REJECTED
    assert detail_substring in exc_info.value.detail.lower()


def test_remote_url_gate_rejects_missing_url(svc) -> None:
    repo = _repo("github", {"url": ""}, uuid.uuid4())
    with pytest.raises(AcquisitionError) as exc_info:
        svc._validate_remote_url(repo)
    assert exc_info.value.code == ACQ_SOURCE_INVALID


def test_remote_url_gate_accepts_public_https_url(svc) -> None:
    # A genuine public https URL passes the shape gate (the reachability of
    # the host is a later, retryable concern — the gate is about SHAPE).
    assert svc._validate_remote_url(_repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4())) == GITHUB_E2E_URL


def test_local_git_path_gate_rejects_sensitive_root(svc) -> None:
    # The async local-path gate runs the shared host-path rule: a sensitive
    # root (e.g. /etc) is SECURITY_REJECTED before any process is spawned —
    # the closed ACQ code, not a transient hold.
    repo = _repo("local_git", {"path": "/etc/passwd"}, uuid.uuid4())
    with pytest.raises(AcquisitionError) as exc_info:
        _run_sync(svc._validate_local_git(repo, _agent(repo.tenant_id)))
    assert exc_info.value.code == ACQ_SECURITY_REJECTED
    assert intake_security.sensitive_root_detail("/etc/passwd") is not None


def test_local_git_path_gate_rule_via_security_module() -> None:
    verdict = intake_security.check_host_path("/etc/passwd", source_type="local_folder")
    assert not verdict.ok and verdict.reason_code == "SECURITY_REJECTED"
    verdict = intake_security.check_host_path("relative/path", source_type="local_folder")
    assert not verdict.ok and verdict.reason_code == "SOURCE_INVALID"  # local_git is a host-absolute path


# ---------------------------------------------------------------------------
# 4. Failure classification + secret-free stderr (design §B.1 / §A.3)
# ---------------------------------------------------------------------------


def _failure(svc: GitAcquisitionService, stderr: bytes) -> AcquisitionError:
    exc = gasvc._GitFailure(["clone"], 128, stderr)
    return svc._classify_git_failure(exc)


@pytest.mark.parametrize(
    "stderr, expected_code",
    [
        (b"fatal: could not read Username for 'https://github.com': terminal prompts disabled", ACQ_AUTH_FAILED),
        (b"fatal: Authentication failed for 'https://github.com/' (HTTP 401)", ACQ_AUTH_FAILED),
        (b"fatal: remote repository not accessible (HTTP 403): access denied", ACQ_AUTH_FAILED),
        (b"fatal: couldn't find remote ref v9.9", ACQ_REF_NOT_FOUND),
        (b"error: revision unknown revision abc1234", ACQ_REF_NOT_FOUND),
        (b"fatal: unable to access 'https://x/y': Could not resolve host: x", ACQ_SOURCE_UNREACHABLE),
        (b"fatal: early EOF: connection closed", ACQ_SOURCE_UNREACHABLE),
        (b"git: unknown option", ACQ_SOURCE_INVALID),
    ],
)
def test_failure_classification_maps_stderr_class_to_closed_code(svc, stderr: bytes, expected_code: str) -> None:
    err = _failure(svc, stderr)
    assert err.code == expected_code


def test_failure_retryability_is_closed(svc) -> None:
    assert acq_code_is_retryable(ACQ_SOURCE_UNREACHABLE)
    assert acq_code_is_retryable(ACQ_TIMEOUT)
    for code in (ACQ_AUTH_FAILED, ACQ_REF_NOT_FOUND, ACQ_SOURCE_INVALID, ACQ_SECURITY_REJECTED, SUBMODULES_UNSUPPORTED):
        assert not acq_code_is_retryable(code), code


def test_stderr_sanitizer_redacts_userinfo_and_bounds(svc) -> None:
    _ = svc
    secret_url = b"fatal: auth failed for https://bob:sekrit-token@github.com/o/r"
    sanitized = gasvc._sanitize_git_stderr(secret_url)
    assert "sekrit-token" not in sanitized
    assert "redacted" in sanitized
    big = b"x" * 5000
    assert len(gasvc._sanitize_git_stderr(big)) <= 4000 + 3
    assert gasvc._sanitize_git_stderr(b"") == ""


# ---------------------------------------------------------------------------
# 5. Retry-budget finalizer (card §21: bounded, transient-only)
# ---------------------------------------------------------------------------


def test_transient_failure_within_budget_stays_pending(svc) -> None:
    tenant = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, tenant)
    outcome = gasvc.AcquisitionOutcome(state="pending")
    out = svc._finalize_failure(outcome, AcquisitionError(ACQ_SOURCE_UNREACHABLE, "net down"), repo, _agent(tenant))
    assert out.state == "pending" and out.retryable
    assert repo.pending_verifier is True  # sibling gates stay fail-closed
    assert repo.retry_count == 1
    assert repo.verified is False
    assert repo.locator["acq_result"] == ACQ_SOURCE_UNREACHABLE


def test_transient_failure_exhausted_budget_terminates(svc) -> None:
    tenant = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, tenant, retry_count=MAX_RETRIES)
    out = svc._finalize_failure(gasvc.AcquisitionOutcome(state="pending"), AcquisitionError(ACQ_TIMEOUT, "wall"), repo, _agent(tenant))
    assert out.state == "failed" and not out.retryable
    assert repo.locator["acq_result"] == ACQ_TIMEOUT


def test_permanent_failure_never_retries(svc) -> None:
    tenant = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, tenant, retry_count=0)
    out = svc._finalize_failure(gasvc.AcquisitionOutcome(state="pending"), AcquisitionError(ACQ_AUTH_FAILED, "no token"), repo, _agent(tenant))
    assert out.state == "failed" and not out.retryable
    assert repo.pending_verifier is False
    assert repo.retry_count == 0  # permanent codes do not consume the budget


def test_failure_detail_is_bounded(svc) -> None:
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4())
    svc._record_failure(repo, ACQ_AUTH_FAILED, "x" * 900, transient=False)
    assert len(repo.locator["acq_detail"]) <= 300


# ---------------------------------------------------------------------------
# 6. Credential resolve — token only in-process (design §A.3)
# ---------------------------------------------------------------------------


def test_credential_resolve_returns_decrypted_token_for_matching_platform(svc, monkeypatch) -> None:
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    key = real_get_settings().SECRET_KEY
    row = SimpleNamespace(
        status="active",
        credential_type="api_key",
        platform="github.com",
        cookies_json=encrypt_data("ghp_secret", key),
    )

    async def _rows(agent_id: uuid.UUID) -> list:
        assert agent_id == agent.id
        return [row]

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))
    assert _run_sync(svc._resolve_credential(agent, _repo("github", {"url": GITHUB_E2E_URL}, tenant), GITHUB_E2E_URL)) == "ghp_secret"


def test_credential_resolve_skips_inactive_mismatched_and_absent(svc, monkeypatch) -> None:
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    key = real_get_settings().SECRET_KEY
    rows = [
        SimpleNamespace(status="expired", credential_type="api_key", platform="github.com", cookies_json=encrypt_data("x", key)),
        SimpleNamespace(status="active", credential_type="website", platform="github.com", cookies_json=encrypt_data("x", key)),
        SimpleNamespace(status="active", credential_type="api_key", platform="gitlab.com", cookies_json=encrypt_data("x", key)),
        SimpleNamespace(status="active", credential_type="api_key", platform="github.com", cookies_json=None),
    ]

    async def _rows(agent_id: uuid.UUID) -> list:
        return rows

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))
    assert _run_sync(svc._resolve_credential(agent, _repo("github", {"url": GITHUB_E2E_URL}, tenant), GITHUB_E2E_URL)) is None


def test_credential_resolve_undecryptable_is_permanent_auth_failure(svc, monkeypatch) -> None:
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    row = SimpleNamespace(status="active", credential_type="api_key", platform="github.com", cookies_json="garbage-not-encrypted")

    async def _rows(agent_id: uuid.UUID) -> list:
        return [row]

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))
    with pytest.raises(AcquisitionError) as exc_info:
        _run_sync(svc._resolve_credential(agent, _repo("github", {"url": GITHUB_E2E_URL}, tenant), GITHUB_E2E_URL))
    assert exc_info.value.code == ACQ_AUTH_FAILED


def test_credential_resolve_local_git_never_touches_the_store(svc, monkeypatch) -> None:
    calls: list = []

    async def _rows(agent_id: uuid.UUID) -> list:
        calls.append(agent_id)
        return []

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))
    assert _run_sync(svc._resolve_credential(_agent(uuid.uuid4()), _repo("local_git", {"path": "/x"}, uuid.uuid4()), None)) is None
    assert calls == []


def test_credential_store_outage_degrades_to_public(svc, monkeypatch) -> None:
    async def _rows(agent_id: uuid.UUID) -> list:
        raise RuntimeError("store down")

    monkeypatch.setattr(gasvc, "agent_credential_dao", SimpleNamespace(list_by_agent=_rows))
    assert _run_sync(svc._resolve_credential(_agent(uuid.uuid4()), _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4()), GITHUB_E2E_URL)) is None


def _run_sync(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 7. Git child env — prompt disabled, token only in the child's env
# ---------------------------------------------------------------------------


def test_git_env_never_prompts_and_strips_overridable_config(svc, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/u/.gitconfig")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = svc._git_env(None, None)
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # a hang is the one failure that would keep a child alive
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_COUNT" not in env


def test_git_env_injects_token_as_bearer_header_not_url(svc) -> None:
    env = svc._git_env("tok-abc", GITHUB_E2E_URL)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer tok-abc"
    # the token is in NO argv-relevant place and NOT in the URL:
    assert "tok-abc" not in GITHUB_E2E_URL


def test_git_env_without_token_carries_no_credential(svc) -> None:
    env = svc._git_env(None, GITHUB_E2E_URL)
    assert "GIT_CONFIG_COUNT" not in env
    assert "Authorization" not in str(env)


# ---------------------------------------------------------------------------
# 8. Post-checks on the acquired tree (card §16/§18)
# ---------------------------------------------------------------------------


def test_post_checks_refuse_submodule_declaration(svc, tmp_path) -> None:
    (tmp_path / ".gitmodules").write_text("[submodule \"s\"]\n\tpath = s\n")
    with pytest.raises(AcquisitionError) as exc_info:
        svc._post_check_tree(tmp_path)
    assert exc_info.value.code == SUBMODULES_UNSUPPORTED


def test_post_checks_allow_submodule_marker_in_subdir(svc, tmp_path) -> None:
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / ".gitmodules").write_text("x")
    svc._post_check_tree(tmp_path)  # only a ROOT .gitmodules declares submodules


def test_post_checks_refuse_reserved_first_segment(svc, tmp_path) -> None:
    reserved = next(iter(mat_svc.RESERVED_STORAGE_NAMES - {".git", ".git-acq", ".materialize-tmp"}))
    member = tmp_path / reserved / "x.txt"
    member.parent.mkdir(parents=True)
    member.write_text("x")
    with pytest.raises(AcquisitionError) as exc_info:
        svc._post_check_tree(tmp_path)
    assert exc_info.value.code == ACQ_SECURITY_REJECTED


def test_post_checks_pass_on_a_clean_tree(svc, tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print(1)")
    svc._post_check_tree(tmp_path)


# ---------------------------------------------------------------------------
# 9. Artifact publishing — agent-scoped key, working tree only, bounded
# ---------------------------------------------------------------------------


def test_artifact_key_is_agent_scoped(svc) -> None:
    agent_id, repo_id = uuid.uuid4(), uuid.uuid4()
    assert gasvc.GitAcquisitionService._artifact_key(agent_id, repo_id) == f"{agent_id}/.git-acq/{repo_id}/source.tar"


def test_publish_tars_working_tree_and_prunes_git_metadata(svc, monkeypatch, tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("[core]\n")  # must NOT enter the artifact

    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    agent_id, repo_id = uuid.uuid4(), uuid.uuid4()
    key = gasvc.GitAcquisitionService._artifact_key(agent_id, repo_id)
    _run_sync(svc._publish_artifact(tmp_path, agent_id, repo_id, key))

    assert key in fake.files
    with tarfile.open(fileobj=io.BytesIO(fake.files[key]), mode="r") as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
    assert "src/a.py" in names
    assert not any(n.startswith(".git") for n in names)  # no metadata, no hooks


def test_publish_enforces_byte_budgets(svc, monkeypatch, tmp_path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 64)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    monkeypatch.setattr(mat_svc, "MAX_MATERIALIZE_FILE_BYTES", 32)
    agent_id, repo_id = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(AcquisitionError) as exc_info:
        _run_sync(svc._publish_artifact(tmp_path, agent_id, repo_id, gasvc.GitAcquisitionService._artifact_key(agent_id, repo_id)))
    assert exc_info.value.code == "ACQ_SIZE_LIMIT"


# ---------------------------------------------------------------------------
# 10. status() — reconstructs the closed state set from the locator
# ---------------------------------------------------------------------------


def test_status_fresh_repo_is_pending_retryable(svc) -> None:
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4())
    out = _run_sync(svc.status(_FakeDB(), repo=repo, agent=_agent(repo.tenant_id)))
    assert out.state == "pending" and out.retryable and out.code is None


def test_status_transient_within_budget_is_pending(svc) -> None:
    # A transient outcome still inside the bounded retry budget is PENDING,
    # not a terminal failure (design §C.2 closed state set).
    repo = _repo(
        "github",
        {"url": GITHUB_E2E_URL, "acq_result": ACQ_TIMEOUT, "acq_detail": "wall"},
        uuid.uuid4(),
        retry_count=1,
    )
    out = _run_sync(svc.status(_FakeDB(), repo=repo, agent=_agent(repo.tenant_id)))
    assert out.state == "pending" and out.retryable and out.code == ACQ_TIMEOUT


def test_status_permanent_code_is_failed(svc) -> None:
    repo = _repo(
        "github",
        {"url": GITHUB_E2E_URL, "acq_result": ACQ_AUTH_FAILED, "acq_detail": "no token"},
        uuid.uuid4(),
    )
    out = _run_sync(svc.status(_FakeDB(), repo=repo, agent=_agent(repo.tenant_id)))
    assert out.state == "failed" and not out.retryable and out.code == ACQ_AUTH_FAILED


def test_status_verified_artifact_is_acquired(svc) -> None:
    repo = _repo(
        "github",
        {"url": GITHUB_E2E_URL, "acq_artifact": "k", "acq_result": ACQ_OK, "resolved_rev": "r" * 40},
        uuid.uuid4(),
        verified=True,
    )
    out = _run_sync(svc.status(_FakeDB(), repo=repo, agent=_agent(repo.tenant_id)))
    assert out.state == "acquired" and out.code == ACQ_OK and out.artifact_key == "k" and out.resolved_rev == "r" * 40


@pytest.mark.parametrize(
    "bogus",
    [
        "TOTALLY_BOGUS",  # out-of-set free-form string (the audit's repro value)
        "",  # empty string: no result recorded yet, not a terminal failure
        42,  # non-string JSON value a caller could persist
        {"a": 1},  # a non-string object
    ],
)
def test_status_out_of_set_acq_result_is_a_safe_read_never_500(svc, bogus) -> None:
    # F1 (audit t_31f91B3A): acq_result is free-form intake user input for git
    # sources, so a value outside the closed ACQ set (or empty / non-string)
    # crosses the status boundary as data, not a programming error.  The old
    # path fed it straight into acq_code_is_retryable -> ValueError -> an
    # unhandled 500 on the client-reachable GET route.  It must now degrade to
    # a safe read: a not-yet-attempted pending (code=None, retryable) — at the
    # transport that is a 200/409 body, NEVER a 500.
    repo = _repo("github", {"url": GITHUB_E2E_URL, "acq_result": bogus}, uuid.uuid4())
    out = _run_sync(svc.status(_FakeDB(), repo=repo, agent=_agent(repo.tenant_id)))
    assert out.state == "pending"
    assert out.code is None
    assert out.retryable


# ---------------------------------------------------------------------------
# 11. Materialization git reader — reads the ONE tar, no re-clone (card §11)
# ---------------------------------------------------------------------------


def _git_plan(agent_id: uuid.UUID, repo: Repository, locator: dict) -> mat_svc._RepoPlan:
    plan = make_plan(agent_id=agent_id, project_id=repo.project_id, material_name="m", repo_id=repo.id, source_type="github")
    plan.repo = repo
    repo.locator = locator
    return plan


def _tar_bytes(members: dict[str, bytes], *, symlinks: dict[str, str] | None = None, dirs: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for d in dirs or []:
            ti = tarfile.TarInfo(d + "/")
            ti.type = tarfile.DIRTYPE
            tar.addfile(ti)
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tar.addfile(ti)
    return buf.getvalue()


def test_git_reader_extracts_verified_artifact(svc, monkeypatch) -> None:
    _ = svc
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    data = _tar_bytes({"a/b.txt": b"hello", "c.txt": b"world"}, dirs=["a/"])
    fake = _AcqStorage({key: data})
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    _run_sync(mat._plan_git(plan))
    assert plan.payload == {"a/b.txt": b"hello", "c.txt": b"world"}
    assert set(plan.rels) == {"a/b.txt", "c.txt"}


def test_git_reader_skips_symlink_members(svc, monkeypatch) -> None:
    _ = svc
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    data = _tar_bytes({"ok.txt": b"x"}, symlinks={"sneaky": "../etc/passwd"})
    fake = _AcqStorage({key: data})
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    _run_sync(mat._plan_git(plan))
    assert plan.payload == {"ok.txt": b"x"}  # the symlink member was never followed


def test_git_reader_rejects_traversal_member(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    data = _tar_bytes({"../../evil.txt": b"pwn"})
    fake = _AcqStorage({key: data})
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoRejected) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SECURITY_REJECTED"
    assert plan.payload == {}  # 0 writes


def test_git_reader_rejects_reserved_first_segment(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    reserved = next(iter(mat_svc.RESERVED_STORAGE_NAMES - {".git", ".git-acq", ".materialize-tmp"}))
    data = _tar_bytes({f"{reserved}/x.txt": b"x"})
    fake = _AcqStorage({key: data})
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoRejected) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SECURITY_REJECTED"


def test_git_reader_missing_locator_key_is_source_not_found(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    fake = _AcqStorage()
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoUnreachable) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SOURCE_NOT_FOUND"


def test_git_reader_missing_object_in_storage_is_source_not_found(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    fake = _AcqStorage()  # key absent
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoUnreachable) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SOURCE_NOT_FOUND"


def test_git_reader_corrupt_tar_is_unreachable_not_500(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key
    fake = _AcqStorage({key: b"definitely not a tar"})
    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: fake)
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoUnreachable) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SOURCE_UNREACHABLE"


def test_git_reader_storage_outage_is_unreachable(svc, monkeypatch) -> None:
    mat = mat_svc.ProjectMaterializationService()
    agent_id = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL}, uuid.uuid4(), verified=True)
    key = f"{agent_id}/.git-acq/{repo.id}/source.tar"
    repo.locator["acq_artifact"] = key

    class _BrokenStorage(_AcqStorage):
        async def read_bytes(self, key: str) -> bytes:
            raise RuntimeError("backend down")

    monkeypatch.setattr(mat_svc, "get_storage_backend", lambda: _BrokenStorage({key: b"x"}))
    plan = _git_plan(agent_id, repo, repo.locator)
    with pytest.raises(_RepoUnreachable) as exc_info:
        _run_sync(mat._plan_git(plan))
    assert exc_info.value.code == "SOURCE_UNREACHABLE"


def test_git_artifact_verified_gate_helper() -> None:
    mat = mat_svc.ProjectMaterializationService()
    assert not mat._git_artifact_verified(_repo("github", {}, uuid.uuid4(), verified=True))
    assert not mat._git_artifact_verified(_repo("github", {"acq_artifact": "k"}, uuid.uuid4(), verified=False))
    assert not mat._git_artifact_verified(_repo("github", {"acq_artifact": "k"}, uuid.uuid4(), verified=True, pending_verifier=True))
    assert mat._git_artifact_verified(_repo("github", {"acq_artifact": "k"}, uuid.uuid4(), verified=True))


# ---------------------------------------------------------------------------
# 12. Intake flip — a git source validates OK only with a verified artifact
# ---------------------------------------------------------------------------


def test_intake_git_source_validates_only_when_artifact_verified() -> None:
    intake = ProjectIntakeService()
    tenant = uuid.uuid4()
    ok_repo = _repo("github", {"url": GITHUB_E2E_URL, "acq_artifact": "k"}, tenant, verified=True)
    out = _run_sync(intake._validate_git(ok_repo))
    assert out.ok

    fresh = _repo("github", {"url": GITHUB_E2E_URL}, tenant)
    out = _run_sync(intake._validate_git(fresh))
    assert not out.ok and out.reason_code == "SOURCE_NOT_SUPPORTED" and not out.retryable  # permanent, fail-closed

    unverified = _repo("github", {"url": GITHUB_E2E_URL, "acq_artifact": "k"}, tenant, verified=False, pending_verifier=True)
    out = _run_sync(intake._validate_git(unverified))
    assert not out.ok and out.reason_code == "SOURCE_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# 13. Real-git E2E — the service against a real git binary (no network)
# ---------------------------------------------------------------------------


def _no_spawn(svc, monkeypatch) -> None:
    """Ensure NO git process is spawned (a shape / gate rejection must not
    reach the child — card §7: the gate is before ANY process)."""

    async def _boom(*_a, **_kw):
        raise AssertionError("git child spawned despite a gate rejection")

    monkeypatch.setattr(gasvc.asyncio, "create_subprocess_exec", _boom)


def test_e2e_local_git_default_branch_ref_is_not_main(svc, monkeypatch, tmp_path) -> None:
    up = make_local_repo(tmp_path)
    tenant = uuid.uuid4()
    agent, project, user = _agent(tenant), _project(tenant), _user(tenant)
    repo = _repo("local_git", {"path": str(up["dir"])}, tenant)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    _patch_no_credentials(monkeypatch)

    db = _FakeDB()
    outcome = _run_sync(svc.acquire(db, project=project, repo=repo, agent=agent, current_user=user))

    assert outcome.state == "acquired", outcome.message
    assert outcome.code == ACQ_OK
    assert outcome.resolved_rev == up["rev2"]  # the default branch's tip — read, never assumed
    key = f"{agent.id}/.git-acq/{repo.id}/source.tar"
    assert outcome.artifact_key == key and key in fake.files
    assert repo.verified and not repo.pending_verifier
    assert repo.locator["acq_result"] == ACQ_OK and repo.locator["resolved_rev"] == up["rev2"]
    with tarfile.open(fileobj=io.BytesIO(fake.files[key])) as tar:
        names = {m.name for m in tar.getmembers() if m.isfile()}
    assert "a.txt" in names and not any(n.startswith(".git") for n in names)
    # the ONLY session writes: the repo row + one audit row — nothing that
    # could start an Agent / Run / prompt (card §3).
    assert len(db.added) == 2 and isinstance(db.added[1], AuditLog)
    assert db.flushes >= 2
    # the per-call staging work dir is deleted on the success path too
    # (lifecycle verification: the owned resource reached the removed state).
    residue = [p for p in Path(os.environ.get("TEMP", "/tmp")).glob(f"git-acq-{agent.id}-{repo.id}-*")]
    assert not residue, f"staging work dir survived: {residue}"


def test_e2e_local_git_branch_ref_resolves_and_verifies(svc, monkeypatch, tmp_path) -> None:
    up = make_local_repo(tmp_path)
    tenant = uuid.uuid4()
    repo = _repo("local_git", {"path": str(up["dir"]), "branch": up["branch"]}, tenant)
    _patch_storage(monkeypatch, _AcqStorage())
    _patch_no_credentials(monkeypatch)
    db = _FakeDB()
    outcome = _run_sync(svc.acquire(db, project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "acquired"
    assert outcome.resolved_rev == up["rev2"]  # "clone succeeded" is verified, not assumed
    assert repo.locator["requested_ref"] == up["branch"]


def test_e2e_local_git_commit_pin(svc, monkeypatch, tmp_path) -> None:
    up = make_local_repo(tmp_path)
    tenant = uuid.uuid4()
    repo = _repo("local_git", {"path": str(up["dir"]), "commit": up["rev1"]}, tenant)
    _patch_storage(monkeypatch, _AcqStorage())
    _patch_no_credentials(monkeypatch)
    outcome = _run_sync(
        svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant))
    )
    assert outcome.state == "acquired" and outcome.resolved_rev == up["rev1"]


def test_e2e_local_git_missing_ref_is_ref_not_found(svc, monkeypatch, tmp_path) -> None:
    up = make_local_repo(tmp_path)
    tenant = uuid.uuid4()
    repo = _repo("local_git", {"path": str(up["dir"]), "branch": "no-such-branch"}, tenant)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    _patch_no_credentials(monkeypatch)
    outcome = _run_sync(svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "failed" and outcome.code == ACQ_REF_NOT_FOUND and not outcome.retryable
    assert repo.locator["acq_result"] == ACQ_REF_NOT_FOUND
    assert fake.files == {}  # the staging subtree was deleted on failure
    assert repo.pending_verifier is False  # a missing ref is permanent, not a transient hold


def test_e2e_local_git_submodule_declaration_refused(svc, monkeypatch, tmp_path) -> None:
    up = make_local_repo(tmp_path)
    (up["dir"] / ".gitmodules").write_text('[submodule "s"]\n\tpath = s\n\turl = https://example.com/s\n')
    _git_run(["add", ".gitmodules"], up["dir"])
    _git_run(["commit", "-q", "-m", "sub"], up["dir"])
    tenant = uuid.uuid4()
    repo = _repo("local_git", {"path": str(up["dir"])}, tenant)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    _patch_no_credentials(monkeypatch)
    outcome = _run_sync(svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "failed" and outcome.code == SUBMODULES_UNSUPPORTED and not outcome.retryable
    assert fake.files == {}


def test_e2e_local_git_sensitive_path_rejected_before_any_process(svc, monkeypatch, tmp_path) -> None:
    _ = tmp_path
    tenant = uuid.uuid4()
    repo = _repo("local_git", {"path": "/etc/hosts"}, tenant)
    _patch_storage(monkeypatch, _AcqStorage())
    _no_spawn(svc, monkeypatch)
    outcome = _run_sync(svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "failed" and outcome.code == ACQ_SECURITY_REJECTED and not outcome.retryable


def test_e2e_option_shaped_ref_rejected_before_any_process(svc, monkeypatch) -> None:
    # A leading-dash ref would be parsed as a git flag even in value
    # position: the shape gate refuses it BEFORE any child is spawned.
    tenant = uuid.uuid4()
    repo = _repo("github", {"url": GITHUB_E2E_URL, "branch": "-m"}, tenant)
    _patch_storage(monkeypatch, _AcqStorage())
    _patch_no_credentials(monkeypatch)
    _no_spawn(svc, monkeypatch)
    outcome = _run_sync(svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "failed" and outcome.code == ACQ_SECURITY_REJECTED and not outcome.retryable


def test_e2e_url_rejection_spawns_no_process(svc, monkeypatch) -> None:
    tenant = uuid.uuid4()
    repo = _repo("github", {"url": "file:///etc/passwd"}, tenant)
    _patch_storage(monkeypatch, _AcqStorage())
    _patch_no_credentials(monkeypatch)
    _no_spawn(svc, monkeypatch)
    outcome = _run_sync(svc.acquire(_FakeDB(), project=_project(tenant), repo=repo, agent=_agent(tenant), current_user=_user(tenant)))
    assert outcome.state == "failed" and outcome.code == ACQ_SECURITY_REJECTED and not outcome.retryable


def _make_default_repo(base: Path, default: str = "acqmain") -> Path:
    """A real git repo whose HEAD symref is a NON-main default branch.

    Unlike ``make_local_repo`` (which then moves HEAD to a second branch),
    this repo is left exactly where ``git init -b <default>`` put it: one
    commit, HEAD -> ``refs/heads/<default>``.  That is the remote's OWN
    default branch, the thing ``ls-remote --symref <url> HEAD`` reports —
    and it is deliberately not "main".
    """
    repo_dir = base / "upstream-default"
    repo_dir.mkdir()
    _git_run(["init", "-q", "-b", default], repo_dir)
    _git_run(["config", "user.email", "acq@example.com"], repo_dir)
    _git_run(["config", "user.name", "acq-test"], repo_dir)
    (repo_dir / "a.txt").write_text("alpha\n")
    _git_run(["add", "a.txt"], repo_dir)
    _git_run(["commit", "-q", "-m", "one"], repo_dir)
    return repo_dir


def test_default_branch_resolves_a_non_main_default(svc, tmp_path) -> None:
    # F2 (audit t_31f91B3A): _default_branch is the no-ref remote path's way
    # of reading the remote's OWN default branch (card §6/§14 — never hardcoded
    # "main").  Drive it against a REAL local git repo whose default branch is
    # "acqmain" (explicit -b acqmain, no host init.defaultBranch dependency):
    # assert the returned name EXACTLY equals that repo's actual default
    # branch read via git symbolic-ref.  Both the ls-remote argv order and the
    # tab-delimited ref-line parse must be right, since neither bug is masked
    # by a "main" default.
    repo_dir = _make_default_repo(tmp_path)
    actual_default = _git_run(["symbolic-ref", "--short", "HEAD"], repo_dir)
    assert actual_default == "acqmain"  # the test premise: a non-"main" default

    env = svc._git_env(None, None)
    deadline = time.monotonic() + 60
    resolved = _run_sync(svc._default_branch(str(repo_dir), env, deadline))
    assert resolved == actual_default
    assert resolved == "acqmain"


# ---------------------------------------------------------------------------
# 14. Remote E2E — real GitHub / GitLab (skip-guarded on reachability)
# ---------------------------------------------------------------------------


def _git_probe(url: str) -> str:
    """One bounded git-egress probe: 'ok' / 'auth' / 'unreachable'.

    'auth' means the network reached the host but anonymous access was
    refused (401/403 / credential prompt) — posture, not connectivity.  The
    probe sets GIT_TERMINAL_PROMPT=0 + GIT_CONFIG_COUNT=0 exactly like the
    service's child env, so the result mirrors what the acquisition sees."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "0"
    env.pop("GIT_ASKPASS", None)
    try:
        proc = subprocess.run(["git", "ls-remote", "--symref", url, "HEAD"], capture_output=True, timeout=30, env=env, check=False)
    except Exception:  # noqa: BLE001 - a probe: any failure (git missing, timeout, any OSError) is a simple "unreachable" verdict
        return "unreachable"
    if proc.returncode == 0:
        return "ok"
    err = proc.stderr.decode("utf-8", "replace").lower()
    if any(m in err for m in ("could not read username", "authentication", "401", "403", "does not exist or you do not have access")):
        return "auth"
    return "unreachable"


def test_e2e_github_public_repo_default_branch(svc, monkeypatch) -> None:
    if _git_probe(GITHUB_E2E_URL) != "ok":
        pytest.skip("GitHub is not anonymously reachable from this host; the remote E2E evidence is recorded in the acceptance doc")
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    repo = _repo("github", {"url": GITHUB_E2E_URL}, tenant)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    _patch_no_credentials(monkeypatch)
    db = _FakeDB()
    outcome = _run_sync(svc.acquire(db, project=_project(tenant), repo=repo, agent=agent, current_user=_user(tenant)))
    assert outcome.state == "acquired", outcome.message
    assert outcome.provider == "github"
    assert len(outcome.resolved_rev) == 40  # the remote's OWN default branch (master for this repo), read via ls-remote — never hardcoded
    key = f"{agent.id}/.git-acq/{repo.id}/source.tar"
    assert outcome.artifact_key == key and key in fake.files
    with tarfile.open(fileobj=io.BytesIO(fake.files[key])) as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
    assert names and not any(n.startswith(".git") for n in names)  # working tree only — no metadata, no hooks
    assert db.flushes >= 2 and isinstance(db.added[1], AuditLog)


def test_e2e_gitlab_anonymous_access_fails_closed_as_auth(svc, monkeypatch) -> None:
    # Without a stored credential the GitLab source must never hang on a
    # prompt and never report a retryable failure: either anonymous access
    # is allowed (acquired, agent-scoped artifact) or the refusal maps to
    # the permanent ACQ_AUTH_FAILED with the staging subtree cleaned.
    if _git_probe(GITLAB_E2E_URL) == "unreachable":
        pytest.skip("no outbound git egress to GitLab on this host; posture check deferred")
    tenant = uuid.uuid4()
    agent = _agent(tenant)
    repo = _repo("gitlab", {"url": GITLAB_E2E_URL}, tenant)
    fake = _AcqStorage()
    _patch_storage(monkeypatch, fake)
    _patch_no_credentials(monkeypatch)
    db = _FakeDB()
    outcome = _run_sync(svc.acquire(db, project=_project(tenant), repo=repo, agent=agent, current_user=_user(tenant)))
    if outcome.state == "acquired":
        assert outcome.artifact_key in fake.files
    else:
        assert outcome.code == ACQ_AUTH_FAILED, outcome.message
        assert not outcome.retryable  # permanent: a missing token cannot "clear"
        assert "ghp" not in outcome.message.lower()
        assert fake.files == {}  # the staging subtree is deleted on every failure exit


# ---------------------------------------------------------------------------
# 15. Transport schema — the closed state set maps 1:1
# ---------------------------------------------------------------------------


def test_acquisition_out_from_outcome_maps_closed_fields() -> None:
    agent_id, repo_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    out = AcquisitionOut.from_outcome(
        gasvc.AcquisitionOutcome(
            state="acquired",
            code=ACQ_OK,
            requested_ref="v1",
            resolved_rev="r",
            provider="github",
            artifact_key="k",
        ),
        project_id=project_id,
        repo_id=repo_id,
        agent_id=agent_id,
    )
    assert out.state == "acquired" and out.code == ACQ_OK and out.artifact_key == "k"
    assert out.project_id == project_id and out.repo_id == repo_id and out.agent_id == agent_id

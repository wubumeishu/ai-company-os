"""Security-hardening tests for the Phase 2B-2 Project Intake flow (t_3fdac523).

These tests exercise the authoritative rules in
``app/services/intake_security.py`` — the *security* slice of the Intake card
that the sibling implementation card (t_7d2ae798) builds its service on top
of.  They are import-safe and database-free: the module under test is pure
stdlib + the ``normalize_storage_key`` helper, so no FastAPI/Postgres fixtures
are required (the f066 regression test uses the same DB-free convention).

Coverage maps to the card body, one section each:

- tenant isolation (a modified id cannot cross tenant boundaries);
- credentials never persist into a Project/Repository locator;
- path traversal (``../``) and absolute-path attacks are rejected;
- state-machine integrity (``REJECTED -> INITIALIZED`` is impossible, among
  other illegal jumps);
- zip validation detects Zip Slip without extracting, writing, or executing.
"""

from __future__ import annotations

import io
import os
import socket
import uuid
import zipfile
from types import SimpleNamespace

from app.services.intake_security import (
    INTAKE_TRANSITIONS,
    PERMANENT_REASON_CODES,
    REASON_CODES,
    TRANSIENT_REASON_CODES,
    CredentialLeakError,
    InvalidTransition,
    PathSecurityError,
    ReadForbidden,
    TenantScopeViolation,
    can_transition,
    check_host_path,
    check_locator_security,
    check_zip_slip,
    git_url_detail,
    is_terminal_intake_status,
    is_unsafe_host,
    normalize_rel,
    path_traversal_detail,
    reason_code_is_retryable,
    scan_locator_for_credentials,
    transition,
    verify_read_access,
    verify_tenant_scope,
)

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# 1. Tenant isolation
# ---------------------------------------------------------------------------


def test_verify_tenant_scope_allows_matching_tenants() -> None:
    verify_tenant_scope(TENANT_A, TENANT_A)  # no raise


def test_verify_tenant_scope_rejects_cross_tenant_object() -> None:
    # A caller in tenant B fetches a record owned by tenant A by handing the
    # record's real id through — the guard must refuse before any read.
    try:
        verify_tenant_scope(object_tenant_id=TENANT_A, context_tenant_id=TENANT_B)
    except TenantScopeViolation:
        return
    raise AssertionError("cross-tenant scope check did not fail closed")


def test_verify_tenant_scope_fails_closed_on_missing_context() -> None:
    for object_tenant, context_tenant in [(TENANT_A, None), (None, TENANT_A), (None, None)]:
        try:
            verify_tenant_scope(object_tenant, context_tenant)
        except TenantScopeViolation:
            continue
        raise AssertionError(f"missing tenant context did not fail closed: {object_tenant} / {context_tenant}")


def test_reject_by_modified_id_is_captured_by_scope_guard() -> None:
    # The card's "cannot be accessed across tenants via modified ids" is
    # enforced by requiring object.tenant == context.tenant.  A record owned by
    # tenant A, fetched while the acting context is tenant B (e.g. the caller
    # supplied tenant A's project id but is a tenant B user), must be refused
    # regardless of the id's shape.
    same_tenant = uuid.uuid4()
    verify_tenant_scope(same_tenant, same_tenant)  # a record claiming the active tenant passes
    try:
        verify_tenant_scope(TENANT_A, TENANT_B)  # record claims A, caller context is B: rejected
    except TenantScopeViolation:
        return
    raise AssertionError("a cross-tenant record was not rejected")


# ---------------------------------------------------------------------------
# 2. Credentials must not persist into a Project/Repository locator
# ---------------------------------------------------------------------------


def test_plain_credential_field_is_detected() -> None:
    for key in ("api_key", "token", "password", "secret", "private_key", "client_secret"):
        assert scan_locator_for_credentials({key: "s3cr3t-value"}) is not None


def test_url_userinfo_credential_is_detected() -> None:
    assert scan_locator_for_credentials({"url": "https://user:pass@github.com/org/repo.git"}) is not None


def test_safe_git_locator_is_not_flagged() -> None:
    # A public git locator with no userinfo and no credential keys must pass.
    assert (
        scan_locator_for_credentials(
            {"owner": "acme", "repo": "widget", "branch": "main", "url": "https://github.com/acme/widget.git"}
        )
        is None
    )


def test_empty_credential_value_is_ignored() -> None:
    # An empty value is not a stored secret; only non-empty credential values
    # are flagged (avoids false-positives on optional blank fields).
    assert scan_locator_for_credentials({"api_key": ""}) is None


def test_check_locator_security_combines_credential_and_path() -> None:
    verdict = check_locator_security({"path": "/etc/passwd", "token": "abc"}, source_type="local_folder")
    assert verdict.ok is False and verdict.reason_code == "SECURITY_REJECTED"


# ---------------------------------------------------------------------------
# 3. Path traversal and absolute-path attacks
# ---------------------------------------------------------------------------


def test_dotdot_traversal_is_detected() -> None:
    for raw in ("../../etc/passwd", "..\\..\\Windows\\System32", "a/../../x", "C:\\..\\secret"):
        assert path_traversal_detail(raw) is not None, raw


def test_null_byte_is_detected() -> None:
    assert path_traversal_detail("legit\x00/../../etc") is not None


def test_benign_paths_are_not_flagged() -> None:
    assert path_traversal_detail("/opt/projects/acme") is None
    assert path_traversal_detail("C:\\projects\\acme") is None
    assert path_traversal_detail("relative/docs") is None


def test_local_folder_requires_absolute_host_path() -> None:
    verdict = check_host_path("relative/docs", source_type="local_folder")
    assert verdict.ok is False and verdict.reason_code == "SOURCE_INVALID"


def test_sensitive_host_root_is_rejected() -> None:
    for raw in ("/etc/passwd", "/proc/self/cmdline", "C:\\Windows\\System32\\drivers\\etc\\hosts"):
        verdict = check_host_path(raw, source_type="local_folder")
        assert verdict.ok is False and verdict.reason_code == "SECURITY_REJECTED", raw


def test_ordinary_project_directory_passes() -> None:
    assert check_host_path("/opt/data/projects/acme", source_type="local_folder").ok is True


# ---------------------------------------------------------------------------
# 4. State-machine integrity
# ---------------------------------------------------------------------------


def test_legal_intake_transitions() -> None:
    assert can_transition("RECEIVED", "SOURCES_OK")
    assert can_transition("RECEIVED", "REJECTED")
    assert can_transition("SOURCES_OK", "INITIALIZED")
    assert can_transition("SOURCES_OK", "REJECTED")
    # stay-while-retrying is a no-op, not an edge:
    assert can_transition("RECEIVED", "RECEIVED")
    assert can_transition("SOURCES_OK", "SOURCES_OK")


def test_rejected_to_initialized_is_illegal() -> None:
    assert can_transition("REJECTED", "INITIALIZED") is False
    try:
        transition("REJECTED", "INITIALIZED")
    except InvalidTransition:
        return
    raise AssertionError("REJECTED -> INITIALIZED was allowed")


def test_initialized_to_received_is_illegal() -> None:
    assert can_transition("INITIALIZED", "RECEIVED") is False
    try:
        transition("INITIALIZED", "RECEIVED")
    except InvalidTransition:
        return
    raise AssertionError("INITIALIZED -> RECEIVED was allowed")


def test_sources_ok_to_received_is_illegal() -> None:
    assert can_transition("SOURCES_OK", "RECEIVED") is False


def test_unknown_status_fails_closed() -> None:
    for current, target in [("GALACTIC", "RECEIVED"), ("RECEIVED", "GALACTIC"), ("X", "Y")]:
        try:
            transition(current, target)
        except InvalidTransition:
            continue
        raise AssertionError(f"unknown status pair was not rejected: {current} -> {target}")


def test_terminal_statuses_are_terminal() -> None:
    assert is_terminal_intake_status("REJECTED")
    assert is_terminal_intake_status("INITIALIZED")
    assert not is_terminal_intake_status("RECEIVED")
    assert not is_terminal_intake_status("SOURCES_OK")


def test_transition_graph_is_closed_set() -> None:
    assert set(INTAKE_TRANSITIONS) == {"RECEIVED", "SOURCES_OK"}
    assert INTAKE_TRANSITIONS["RECEIVED"] == frozenset({"SOURCES_OK", "REJECTED"})
    assert INTAKE_TRANSITIONS["SOURCES_OK"] == frozenset({"INITIALIZED", "REJECTED"})


# ---------------------------------------------------------------------------
# 5. Reason-code closed set
# ---------------------------------------------------------------------------


def test_reason_codes_are_six_closed() -> None:
    assert len(REASON_CODES) == 6
    assert "SOURCE_NOT_SUPPORTED" in REASON_CODES
    assert PERMANENT_REASON_CODES == {"SOURCE_NOT_FOUND", "SOURCE_INVALID", "SECURITY_REJECTED", "SOURCE_NOT_SUPPORTED"}
    assert TRANSIENT_REASON_CODES == {"SOURCE_UNREACHABLE"}


def test_unknown_reason_code_is_rejected() -> None:
    from app.services.intake_security import assert_reason_code

    try:
        assert_reason_code("MY_INVENTED_CODE")
    except ValueError:
        return
    raise AssertionError("an invented reason code slipped through the closed set")


def test_retryable_classification() -> None:
    assert reason_code_is_retryable("SOURCE_UNREACHABLE") is True
    for code in PERMANENT_REASON_CODES:
        assert reason_code_is_retryable(code) is False


# ---------------------------------------------------------------------------
# 6. Zip validation — no extraction, no write, no execute
# ---------------------------------------------------------------------------


def _build_zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return buf.getvalue()


def test_zip_with_dotdot_is_rejected_as_security() -> None:
    # The card's literal example: a member named ``../../something``.
    verdict = check_zip_slip(_build_zip(["../../something", "inside.txt"]))
    assert verdict.ok is False
    assert verdict.reason_code == "SECURITY_REJECTED"


def test_zip_absolute_and_drive_entries_are_rejected() -> None:
    for names in (["/etc/cron.d/evil"], (["C:\\evil.txt"]), (["a/../../b/../../c.txt"])):
        verdict = check_zip_slip(_build_zip(names))
        assert verdict.ok is False and verdict.reason_code == "SECURITY_REJECTED", names


def test_benign_zip_is_accepted() -> None:
    verdict = check_zip_slip(_build_zip(["docs/readme.md", "src/main.py", "a/b/../../top.txt"]))
    # top-level and depth-bounded names are fine; only root-escaping is not.
    assert verdict.ok is True


def test_invalid_zip_is_source_invalid_not_security() -> None:
    verdict = check_zip_slip(b"this is definitely not a zip archive")
    assert verdict.ok is False and verdict.reason_code == "SOURCE_INVALID"


def test_zip_check_writes_nothing_to_disk() -> None:
    # The card body requires: "zip validation does not ... overwrite user
    # files."  Run the check inside a fresh temp dir and prove no entry is
    # created there, for both a benign and a hostile archive.
    import tempfile

    marker_dir = "intake_zip_no_write_canary"
    with tempfile.TemporaryDirectory() as tmp:
        canary = os.path.join(tmp, marker_dir)
        before = os.listdir(tmp)
        hostile = _build_zip(["../../" + marker_dir, "data.txt"])
        benign = _build_zip(["data.txt"])
        check_zip_slip(hostile)
        check_zip_slip(benign)
        after = os.listdir(tmp)
        assert before == after == []
        assert not os.path.exists(canary)


def test_zip_check_does_not_execute_embedded_scripts() -> None:
    # A hostile zip may contain a shell/python "script" as a plain member.
    # The validator must refuse it as unsafe naming AND must never run it —
    # asserted indirectly: the process's environment is untouched and no
    # side-effect file appears.
    import tempfile

    script_body = b"#!/bin/sh\necho pwned > /tmp/should_not_exist_marker\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../run.sh", script_body)  # escapes root -> unsafe
    verdict = check_zip_slip(buf.getvalue())
    assert verdict.ok is False and verdict.reason_code == "SECURITY_REJECTED"
    marker = os.path.join(tempfile.gettempdir(), "should_not_exist_marker")
    assert not os.path.exists(marker)


# ---------------------------------------------------------------------------
# 7. Read-access permission model (brief §3.3)
# ---------------------------------------------------------------------------


def _user(user_id, role, tenant_id, active=True):
    return SimpleNamespace(id=user_id, role=role, tenant_id=tenant_id, is_active=active)


def _project(creator, tenant_id):
    return SimpleNamespace(created_by=creator, tenant_id=tenant_id)


def test_creator_can_read() -> None:
    user = _user("u1", "member", TENANT_A)
    verify_read_access(user, _project("u1", TENANT_A))  # no raise


def test_same_tenant_admin_can_read() -> None:
    admin = _user("u-admin", "org_admin", TENANT_A)
    verify_read_access(admin, _project("u1", TENANT_A))


def test_other_member_cannot_read() -> None:
    stranger = _user("u2", "member", TENANT_A)
    try:
        verify_read_access(stranger, _project("u1", TENANT_A))
    except ReadForbidden:
        return
    raise AssertionError("a non-creator, non-admin was granted read access")


def test_cross_tenant_admin_cannot_read() -> None:
    # An admin of tenant B must NOT read tenant A's project (brief §3.3:
    # "不做跨租户 admin").
    cross_admin = _user("u-admin-b", "platform_admin", TENANT_B)
    try:
        verify_read_access(cross_admin, _project("u1", TENANT_A))
    except ReadForbidden:
        return
    raise AssertionError("a cross-tenant admin was granted read access")


def test_platform_admin_same_tenant_can_read() -> None:
    plat = _user("u-plat", "platform_admin", TENANT_A)
    verify_read_access(plat, _project("u1", TENANT_A))


# ---------------------------------------------------------------------------
# 8. Git source acquisition security guards (Phase 2B-4, card t_4874c3e7)
#
# The three shared, stdlib-only guards the Git acquisition service and the
# Materialization git-reader MUST call (one authoritative rule set, design
# GIT_ACQ_DESIGN_V1.md §D): the https-only URL gate, the cross-product SSRF
# host rule, and the single member-path normalizer.
# ---------------------------------------------------------------------------


def test_git_url_detail_rejects_non_https_schemes() -> None:
    # file:// / ssh:// / http:// / ftp:// are all unsafe protocols — only
    # https is a git remote in V1.
    assert git_url_detail("file:///etc/passwd") is not None
    assert git_url_detail("ssh://git@github.com/o/r.git") is not None
    assert git_url_detail("http://github.com/o/r") is not None
    assert git_url_detail("ftp://github.com/o/r") is not None
    assert git_url_detail("javascript:alert(1)") is not None


def test_git_url_detail_rejects_embedded_userinfo() -> None:
    # A credential in the URL violates the "token never in the locator /
    # URL userinfo" invariant (design §A.3) — reject before any process.
    assert git_url_detail("https://user:pass@github.com/o/r") is not None
    assert git_url_detail("https://user@github.com/o/r") is not None


def test_git_url_detail_rejects_empty_and_non_string() -> None:
    assert git_url_detail("") is not None
    assert git_url_detail(None) is not None
    assert git_url_detail(123) is not None  # type: ignore[arg-type]


def test_git_url_detail_delegates_host_to_is_unsafe_host() -> None:
    # The host decision is owned by is_unsafe_host: a public host passes
    # (no DNS on IP literals), a private IP literal fails closed.
    assert is_unsafe_host("https://8.8.8.8/x") is None  # public IP literal
    assert is_unsafe_host("https://127.0.0.1/x") is not None  # loopback
    assert is_unsafe_host("https://10.0.0.5/x") is not None  # RFC1918
    assert is_unsafe_host("https://169.254.169.254/meta") is not None  # link-local / metadata
    assert is_unsafe_host("https://192.168.1.1/x") is not None
    assert is_unsafe_host("https://0.0.0.0/x") is not None
    # userinfo is rejected by the URL gate, and a bare public IP passes.
    assert git_url_detail("https://8.8.8.8/x") is None
    assert git_url_detail("https://192.168.0.1/x") is not None


def test_is_unsafe_host_fails_closed_on_unresolvable(monkeypatch) -> None:
    # A hostname that cannot be resolved is UNPROVABLE-safe -> unsafe
    # (the fail-closed-on-exception rule, design §A.5).  Monkeypatch the
    # resolver so the test is deterministic and needs no network.
    def _boom(*_a, **_k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert is_unsafe_host("https://does-not-exist.example/") is not None


def test_normalize_rel_rejects_traversal_and_reserved() -> None:
    # The shared normalizer: backslash->slash, drop '.'/empty, and ANY '..'
    # or NUL rejects the whole path (never popped).  Empty -> None.
    assert normalize_rel("a/b/c") == "a/b/c"
    assert normalize_rel("a\\b\\c") == "a/b/c"
    assert normalize_rel("./a/./b") == "a/b"
    assert normalize_rel("a/../../x") is None
    assert normalize_rel("..") is None
    assert normalize_rel("a\x00b") is None
    assert normalize_rel("") is None
    assert normalize_rel(".") is None


# ---------------------------------------------------------------------------
# 9. Module surface sanity
# ---------------------------------------------------------------------------


def test_expected_symbol_names_present() -> None:
    # Guard the exact public surface the sibling service is written against.
    import importlib

    module = importlib.import_module("app.services.intake_security")
    for name in (
        "PathSecurityError",
        "CredentialLeakError",
        "SecurityVerdict",
        "check_host_path",
        "check_zip_slip",
        "git_url_detail",
        "is_unsafe_host",
        "normalize_rel",
        "scan_locator_for_credentials",
        "transition",
        "verify_tenant_scope",
        "verify_read_access",
    ):
        assert hasattr(module, name), f"public surface lost its {name!r} export"
        assert name in module.__all__
    # exception hierarchy is shared
    assert issubclass(PathSecurityError, Exception)
    assert issubclass(CredentialLeakError, Exception)
    assert issubclass(InvalidTransition, Exception)
    assert issubclass(TenantScopeViolation, Exception)

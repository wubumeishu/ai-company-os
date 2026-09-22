"""Security layer for the Phase 2B-2 Project Intake flow (card t_3fdac523).

This module is the single authoritative home for the *security* slice of the
Intake card.  The sibling implementation card (t_7d2ae798) owns the Intake
service, the API, the state-machine driver, and the f067 rejection/retry
columns; this card owns the hardening rules the service must call so that the
Intake entry point cannot be bypassed.

Architect brief: ``docs/INTAKE_ARCHITECTURE_BRIEF_V1.md`` §4.4/§4.5/§7/§8 —
path traversal, Zip Slip, credential-not-stored, cross-tenant 403, and
state-machine integrity.

Design constraints honored here (and nowhere else):

- **Import-safe and side-effect free.**  The module imports only the standard
  library — no database, no FastAPI, no settings, no app-level import chain.
  (Deliberately it does *not* import ``normalize_storage_key``: that helper
  silently pops ``..`` segments, which would mask an escaping traversal; this
  module instead detects and rejects escapes, and keeping the stdlib-only
  surface means the rules are unit-testable on every platform the app
  targets.)  The sibling service can call these functions from a unit test or
  from the live request path with identical behavior.
- **Fail closed.**  Every check returns a verdict; an unknown or suspicious
  input is rejected rather than guessed.  A broad, "maybe safe" pass is never
  returned — see AGENTS.md "Ignored failures are narrow and explained".
- **No extraction, no execution.**  Zip checks read archive *names* only;
  they never ``extract`` a member, never write a file, and never run a
  script.  This is the "safe source validation only" rule from brief §八.

The service/API layer maps the exceptions raised here to HTTP status codes:

======  ===================  ===============================
module  raised when         API mapping (sibling's card)
======  ===================  ===============================
PathSecurityError     a locator host path is unsafe      409 SECURITY_REJECTED
CredentialLeakError   a locator would store a secret      409 SECURITY_REJECTED
InvalidTransition     an illegal status jump was requested 409 (or 422 on create)
TenantScopeViolation  a record's tenant != acting tenant   403/404
ReadForbidden         caller is neither creator nor same-tenant admin 403
======  ===================  ===============================
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

__all__ = [
    "INTAKE_TRANSITIONS",
    "PERMANENT_REASON_CODES",
    "REASON_CODES",
    "TERMINAL_INTAKE_STATUSES",
    "TRANSIENT_REASON_CODES",
    "CredentialLeakError",
    "InvalidTransition",
    "PathSecurityError",
    "ReadForbidden",
    "SecurityError",
    "SecurityVerdict",
    "TenantScopeViolation",
    "assert_reason_code",
    "can_transition",
    "check_host_path",
    "check_zip_slip",
    "is_terminal_intake_status",
    "path_traversal_detail",
    "reason_code_is_retryable",
    "scan_locator_for_credentials",
    "sensitive_root_detail",
    "transition",
    "verify_read_access",
    "verify_tenant_scope",
]


# ---------------------------------------------------------------------------
# Reason-code closed set (brief §4.2 — six codes, no private additions)
# ---------------------------------------------------------------------------

#: The closed set of Intake rejection reason codes (brief §4.2).  A validator
#: may only ever emit one of these; anything else is a programming error and
#: is rejected by :func:`assert_reason_code`.
REASON_CODES = frozenset(
    {
        "SOURCE_NOT_FOUND",  # source does not exist (permanent)
        "SOURCE_INVALID",  # exists but type/structure/content is wrong (permanent)
        "SECURITY_REJECTED",  # a security policy was triggered (permanent)
        "SOURCE_UNREACHABLE",  # temporarily unreachable, bounded retry
        "DISTRIBUTION_FAILED",  # defined-but-not-triggered in Intake (brief §4.3)
        "SOURCE_NOT_SUPPORTED",  # no V1 verifier exists for this source_type
    }
)

#: Reason codes that mean "never retry, the project is terminal".
PERMANENT_REASON_CODES = frozenset(
    {
        "SOURCE_NOT_FOUND",
        "SOURCE_INVALID",
        "SECURITY_REJECTED",
        "SOURCE_NOT_SUPPORTED",
    }
)

#: Reason codes that mean "retry is meaningful; escalate to REJECTED at the
#: retry bound" (brief §4.4 / §4.5).
TRANSIENT_REASON_CODES = frozenset({"SOURCE_UNREACHABLE"})


def assert_reason_code(code: str) -> None:
    """Raise ``ValueError`` if ``code`` is not one of the six closed codes.

    This is the guard against a validator silently minting a new, undocumented
    reason string (brief §4.2 "实现侧不得私加").
    """
    if code not in REASON_CODES:
        raise ValueError(
            f"{code!r} is not in the closed Intake reason-code set {sorted(REASON_CODES)}"
        )


def reason_code_is_retryable(code: str) -> bool:
    """Return whether re-invoking validate can change the outcome for ``code``.

    Transient codes are retryable; permanent codes (and DISTRIBUTION_FAILED,
    which is not triggered in Intake) are not.  An unknown code is a
    programming error and is rejected rather than guessed.
    """
    assert_reason_code(code)
    return code in TRANSIENT_REASON_CODES


# ---------------------------------------------------------------------------
# Path security (local_folder / document / zip host paths) — brief §4.5, §八
# ---------------------------------------------------------------------------

#: Host-path roots that an Intake locator must never point at.  These are the
#: conventional system/sensitive directories on Linux and Windows.  The list is
#: a *denial* allowlist-complement: anything under one of these roots is
#: rejected as ``SECURITY_REJECTED`` (brief §4.5 "不得指向已知的敏感目录").
_SENSITIVE_PATH_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/run",
    "/var/run",
    "/var/lib",
    "/usr/lib/systemd",
    "c:\\windows",
    "c:\\winnt",
    "\\windows",
    "system32",
)

_NUL_RE = re.compile(r"\x00")
# A URL/scheme value carrying embedded userinfo:  https://user:pass@host/...
_URL_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+@")


@dataclass(frozen=True)
class SecurityVerdict:
    """Outcome of a single security check.

    ``ok`` is the boolean decision; ``reason_code`` and ``detail`` are only
    populated on a rejection and carry a closed-set code plus a safe, bounded
    human description (the description is what may be stored in
    ``projects.rejection_detail`` — it must never contain the offending secret
    or full locator, only the *class* of problem).
    """

    ok: bool
    reason_code: str | None = None
    detail: str | None = None

    @staticmethod
    def accept() -> SecurityVerdict:
        return SecurityVerdict(ok=True)

    @staticmethod
    def reject(reason_code: str, detail: str) -> SecurityVerdict:
        assert_reason_code(reason_code)
        return SecurityVerdict(ok=False, reason_code=reason_code, detail=detail)


def path_traversal_detail(raw: str) -> str | None:
    """Return a detail string if ``raw`` contains a traversal vector, else None.

    Detected vectors:
    - a NUL byte (a classic path-truncation attack);
    - any ``..`` path segment (normalized across ``/`` and ``\\``);
    - a percent-encoded traversal is *not* decoded here — the locator is the
      trusted, already-parsed value, so raw ``..`` is the attack surface.
    """
    if not isinstance(raw, str):
        return "path is not a string"
    if not raw:
        return "path is empty"
    if _NUL_RE.search(raw):
        return "path contains a NUL byte"
    normalized = raw.replace("\\", "/")
    for segment in normalized.split("/"):
        if segment == "..":
            return "path contains a '..' segment"
    return None


def sensitive_root_detail(raw: str) -> str | None:
    """Return a detail string if ``raw`` lands under a sensitive host root."""
    if not isinstance(raw, str) or not raw:
        return None
    lowered = raw.lower()
    for prefix in _SENSITIVE_PATH_PREFIXES:
        p = prefix.lower()
        if not lowered.startswith(p):
            continue
        # Match on a path boundary so ``/etc`` catches ``/etc/passwd`` but not
        # ``/etcure``.  Bare-marker prefixes (no leading slash, e.g. the
        # Windows ``system32`` marker and drive prefixes) match on their own.
        is_marker = not p.startswith("/")
        if is_marker:
            return f"path points into sensitive directory {prefix!r}"
        boundary = lowered[len(p):1]
        if boundary in ("/", "\\", ":") or boundary == "":
            return f"path points into sensitive directory {prefix!r}"
    return None


def check_host_path(raw: str, *, source_type: str) -> SecurityVerdict:
    """Validate a host-path locator (``local_folder``, ``document``, ``zip``).

    Rules (brief §4.5 + §八):
    - reject any traversal or NUL (``SECURITY_REJECTED``);
    - reject a path that escapes into a sensitive host root
      (``SECURITY_REJECTED``);
    - ``local_folder`` is defined as a *host absolute* path, so a relative
      path is ``SOURCE_INVALID`` (it is not the locator shape this source type
      accepts);
    - ``document`` / ``zip`` host paths may be absolute or relative, but never
      traverse.

    Reachability (does the path actually exist on this host?) is deliberately
    *not* decided here — that is the validator's concern and maps to
    ``SOURCE_NOT_FOUND`` / ``SOURCE_UNREACHABLE`` (brief UNKNOW 1: host-path
    reachability in a containerized deploy is an ops contract, not a system
    assumption).  This function only enforces the *shape safety* of the value.
    """
    traversal = path_traversal_detail(raw)
    if traversal is not None:
        return SecurityVerdict.reject("SECURITY_REJECTED", traversal)
    sensitive = sensitive_root_detail(raw)
    if sensitive is not None:
        return SecurityVerdict.reject("SECURITY_REJECTED", sensitive)
    if source_type == "local_folder" and not _is_absolute_host_path(raw):
        return SecurityVerdict.reject(
            "SOURCE_INVALID", "local_folder.path must be an absolute host path"
        )
    return SecurityVerdict.accept()


def _is_absolute_host_path(raw: str) -> bool:
    """A host absolute path: POSIX ``/...`` or a Windows drive ``X:\\...``/``X:/...``."""
    if raw.startswith("/"):
        return True
    return re.match(r"^[A-Za-z]:[\\/]", raw) is not None


# ---------------------------------------------------------------------------
# Zip Slip — brief §7 / §八 ("no extract, no write, no execute")
# ---------------------------------------------------------------------------

# Absolute entry (leading /), a drive-letter entry, or a '..' that escapes the
# archive root.  Depth tracking makes ``a/b/../../x`` (stays inside) allowed
# while ``../../x`` (escapes) is rejected — matching the real Zip-Slip bug.
_DRIVE_ENTRY_RE = re.compile(r"^[A-Za-z]:([/\\].*)?$")


def _zip_entry_is_dangerous(name: str) -> str | None:
    """Return a detail string if a single archive member name is unsafe."""
    if not isinstance(name, str) or name == "":
        return "empty member name"
    if _NUL_RE.search(name):
        return "member name contains a NUL byte"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return "member path is absolute"
    if _DRIVE_ENTRY_RE.match(normalized):
        return "member path carries a drive-letter prefix"
    depth = 0
    for segment in normalized.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            depth -= 1
            if depth < 0:
                return "member path escapes the archive root ('..' traversal)"
        else:
            depth += 1
    return None


def check_zip_slip(data: bytes) -> SecurityVerdict:
    """Safely inspect a zip archive for Zip-Slip / path-traversal entries.

    Reads only the central-directory member *names* (``namelist``); it never
    extracts a member, never writes to disk, and never executes embedded
    content.  A zip that cannot be opened as a container is ``SOURCE_INVALID``
    (it is not a readable archive); a valid container carrying an unsafe
    member name is ``SECURITY_REJECTED``.
    """
    if not isinstance(data, (bytes, bytearray)):
        return SecurityVerdict.reject("SOURCE_INVALID", "zip source is not readable bytes")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile:
        return SecurityVerdict.reject("SOURCE_INVALID", "not a valid zip archive")
    with archive:
        names = archive.namelist()
        # A hard bound on the name count stops a name-only zip bomb from
        # forcing an unbounded scan (brief "complete-operation bounds").
        if len(names) > 100_000:
            return SecurityVerdict.reject(
                "SECURITY_REJECTED", "archive member count exceeds the Intake bound"
            )
        for name in names:
            danger = _zip_entry_is_dangerous(name)
            if danger is not None:
                # detail names the *class* of problem, not the raw entry, so a
                # stored rejection_detail can never become a write vector.
                return SecurityVerdict.reject(
                    "SECURITY_REJECTED", f"unsafe archive member: {danger}"
                )
    return SecurityVerdict.accept()


# ---------------------------------------------------------------------------
# Credential guard — brief §八 "credential 明文不进 Project / Repository"
# ---------------------------------------------------------------------------

#: Locator keys that, when present with a non-empty value, unambiguously carry
#: a credential.  Matched on the exact key or a trailing ``_<marker>`` so that
#: ordinary locator fields (path, storage_key, owner, repo, url, branch,
#: commit, display_name) never false-positive.
_CREDENTIAL_KEY_MARKERS = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "secret",
        "passwd",
        "password",
        "credential",
        "credentials",
        "authorization",
        "bearer",
        "access_key",
        "private_key",
        "ssh_key",
        "cookie",
        "jwt",
        "client_secret",
    }
)


def _key_is_credential(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_").replace(" ", "_")
    return normalized in _CREDENTIAL_KEY_MARKERS or any(
        normalized.endswith("_" + marker) for marker in _CREDENTIAL_KEY_MARKERS
    )


def scan_locator_for_credentials(locator: object) -> str | None:
    """Return a detail string if a locator would persist a sensitive credential.

    Two narrow, high-confidence signals are checked (fail-safe at intake):

    - a locator *key* that names a credential field and holds a non-empty
      value (a stored ``api_key`` / ``token`` / ``password`` / ...); and
    - a locator *string value* that is a URL carrying embedded userinfo
      (``https://user:pass@host``), the classic leaked-clone-credential shape
      for git sources.

    A locator that is not a dict (``None`` for a ``manual`` source) is safe by
    construction.  The returned detail names the *class* of the leak, never
    the value, so it is safe to store in ``repositories``/``projects``.
    """
    if not isinstance(locator, dict):
        return None
    for key, value in locator.items():
        if _key_is_credential(key) and value not in (None, ""):
            return f"locator field {str(key)!r} carries a credential value"
        if isinstance(value, str) and _URL_USERINFO_RE.match(value):
            return f"locator field {str(key)!r} contains a credential in its URL userinfo"
    return None


def check_locator_security(locator: object, *, source_type: str) -> SecurityVerdict:
    """Full locator gate: credential scan + host-path shape safety.

    Runs the credential guard for every source type, and the host-path shape
    check when the locator carries a host ``path``.  Returns the first
    rejection (credential leaks and path escapes are both ``SECURITY_REJECTED``).
    """
    leak = scan_locator_for_credentials(locator)
    if leak is not None:
        return SecurityVerdict.reject("SECURITY_REJECTED", leak)
    if isinstance(locator, dict):
        host_path = locator.get("path")
        if isinstance(host_path, str) and host_path:
            return check_host_path(host_path, source_type=source_type)
    return SecurityVerdict.accept()


# ---------------------------------------------------------------------------
# State-machine integrity — brief §5 / §十 ("non-legal jumps must not succeed")
# ---------------------------------------------------------------------------

#: The only Intake-owned transition edges (brief §5, "本卡只实现上表 4 类迁移").
#: The transient retry is a *stay* (a no-op, not an edge), so it is not listed
#: here.  Nothing in Intake leaves ``INITIALIZED`` or ``REJECTED``:
#: ``INITIALIZED`` hands off to the analysis/execution phase (out of scope), and
#: ``REJECTED`` is terminal (brief §十 "REJECTED -> INITIALIZED" forbidden).
INTAKE_TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"SOURCES_OK", "REJECTED"}),
    "SOURCES_OK": frozenset({"INITIALIZED", "REJECTED"}),
}

#: Statuses with no Intake-owned outbound edge.
TERMINAL_INTAKE_STATUSES = frozenset({"INITIALIZED", "REJECTED"})


class SecurityError(Exception):
    """Base class for Intake security-policy violations."""


class PathSecurityError(SecurityError):
    """Raised when a host-path locator is unsafe (traversal / sensitive root)."""


class CredentialLeakError(SecurityError):
    """Raised when a locator would persist a sensitive credential."""


class InvalidTransition(SecurityError):
    """Raised when an illegal Project status jump is requested."""


class TenantScopeViolation(SecurityError):
    """Raised when an object's tenant does not match the acting tenant context."""


class ReadForbidden(SecurityError):
    """Raised when the caller is neither the creator nor a same-tenant admin."""


def can_transition(current: str, target: str) -> bool:
    """Return whether ``current -> target`` is a legal Intake edge.

    A "stay" (``current == target``) is legal only while the status is not
    terminal; terminal statuses have no outbound Intake edge.  Every other
    pair is illegal unless it is one of the :data:`INTAKE_TRANSITIONS` edges.
    """
    if current in INTAKE_TRANSITIONS and target in INTAKE_TRANSITIONS[current]:
        return True
    if current == target:
        return current not in TERMINAL_INTAKE_STATUSES
    return False


def transition(current: str, target: str) -> str:
    """Assert a legal Intake transition and return ``target``.

    Raises :class:`InvalidTransition` for every illegal jump — in particular
    ``REJECTED -> INITIALIZED`` and ``INITIALIZED -> RECEIVED`` (brief §十).
    The check is closed-set: an unknown ``current`` or ``target`` status is
    itself a transition violation.
    """
    assert_reason_code_free_status(current, target)
    if not can_transition(current, target):
        raise InvalidTransition(
            f"illegal Intake transition {current!r} -> {target!r}"
        )
    return target


def assert_reason_code_free_status(current: str, target: str) -> None:
    """Reject status values outside the Project enum (defence in depth).

    The Project model enforces the 10-value enum at the DB boundary; this is a
    second, cheap check at the service boundary so a bad status string fails
    closed before any state is written.
    """
    known = frozenset(INTAKE_TRANSITIONS) | TERMINAL_INTAKE_STATUSES | {
        "ANALYZING",
        "PENDING_CONFIRMATION",
        "EXECUTING",
        "BLOCKED",
        "COMPLETED",
        "ARCHIVED",
    }
    if current not in known or target not in known:
        raise InvalidTransition(
            f"unknown Intake status in transition {current!r} -> {target!r}"
        )


def is_terminal_intake_status(status: str) -> bool:
    """Return whether ``status`` has no further Intake-owned transition."""
    return status in TERMINAL_INTAKE_STATUSES


# ---------------------------------------------------------------------------
# Tenant / permission guard — brief §3.3, §十一 (reuse the existing model)
# ---------------------------------------------------------------------------


def verify_tenant_scope(object_tenant_id, context_tenant_id) -> None:
    """Fail closed when a record's tenant does not match the acting context.

    This is a *second* check layered on the authoritative ``do_orm_execute``
    tenant filter (``app/dao/base.py``) and :func:`TenantScopedBaseDAO.add_scoped`.
    It exists so that a bug which loses tenant context (a null context, a
    cross-tenant fetch) fails the *narrow* Intake path instead of silently
    disclosing or writing another tenant's Project/Repository (brief §十一:
    "不能通过修改 project_id / repository_id 绕过 tenant 隔离").
    """
    if object_tenant_id is None or context_tenant_id is None:
        raise TenantScopeViolation("a tenant-scoped Intake operation has no tenant context")
    if object_tenant_id != context_tenant_id:
        raise TenantScopeViolation("record tenant does not match the acting tenant context")


def verify_read_access(user, project) -> None:
    """Enforce the read/validate permission model from brief §3.3.

    Access is granted to:
    - the creator (``project.created_by == user.id``); or
    - a same-tenant admin (``platform_admin`` / ``org_admin`` whose tenant
      matches the project's tenant).

    Anything else raises :class:`ReadForbidden` (mapped to 403).  This mirrors
    ``check_agent_access``'s agent-dimension model; it deliberately does not
    allow a cross-tenant admin to read another tenant's Project.
    """
    if user is None or project is None:
        raise ReadForbidden("missing user or project for the access check")
    if project.created_by == user.id:
        return
    if user.role in ("platform_admin", "org_admin") and project.tenant_id == user.tenant_id:
        return
    raise ReadForbidden(
        "caller is neither the project creator nor a same-tenant admin"
    )

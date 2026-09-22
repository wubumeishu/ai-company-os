"""Project Intake lifecycle service (Phase 2B-2).

Owns the Intake state machine for a Project and its source/asset registry:

    RECEIVED ──(validate: all pass)──► SOURCES_OK ──► INITIALIZED
        │
        ├──(permanent failure)────────► REJECTED (reason code in closed set)
        └──(transient unreachable, within retry budget)──► stay RECEIVED /
             SOURCES_OK (retry_count++); budget exhausted → REJECTED

Per docs/INTAKE_ARCHITECTURE_BRIEF_V1.md §3–§7:
- API handlers call only the two public methods here; no business
  orchestration lives in the transport layer.
- All persistence goes through the domain DAOs (``project_dao`` /
  ``repository_dao``); the service never runs raw ``select(...)``.
- Every status write is asserted through ``intake_security.transition`` —
  the security module (card t_3fdac523) is the single guard against illegal
  Intake jumps (REJECTED → INITIALIZED, INITIALIZED → RECEIVED, ...).
  Terminal-state re-validation surfaces as :class:`IntakeTransitionError`
  (a subclass of :class:`SecurityError`) so the API maps it to one 409.
- Every host-path / zip / credential check is delegated to
  ``intake_security`` (``check_host_path`` / ``check_zip_slip`` /
  ``scan_locator_for_credentials``); this service does not re-invent a
  security rule. A credential-leaking locator is rejected at create time,
  so no secret ever reaches a Repository row.
- Reason codes come from the security module's closed 6-code set; the
  service only emits a code the security module accepts
  (``reason_code_is_retryable`` asserts membership on every use).
- The service never extracts a zip, executes project code, creates a
  workspace, or creates a Task (Materialization / execution are out of
  scope here).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiofiles
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the upload document/office whitelist (brief §7 UNKNOW 2): the set is
# defined once in app.api.upload and must not be re-invented here.
from app.api.upload import OFFICE_EXTENSIONS, TEXT_EXTENSIONS
from app.dao.project_intake_dao import project_dao, repository_dao
from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.user import User
from app.schemas.project_intake import RejectionInfo, SourceSpec
from app.services import intake_security
from app.services.intake_security import (
    InvalidTransition,
    SecurityError,
    check_host_path,
    check_zip_slip,
    scan_locator_for_credentials,
    transition,
)
from app.services.storage_runtime.utils import normalize_storage_key

__all__ = [
    "IntakeSecurityError",
    "IntakeTransitionError",
    "ProjectIntakeService",
    "ValidationOutcome",
    "get_storage_backend",
]


def get_storage_backend():
    """Resolve the configured storage backend (lazy, platform-safe import).

    The storage_runtime facade pulls in the platform-specific lock module at
    package import time; resolving it here on first use keeps this service
    module importable on every host. Tests patch this module attribute to a
    memory backend.
    """
    from app.services.storage_runtime.facade import get_storage_backend as _resolve

    return _resolve()


# ---------------------------------------------------------------------------
# Reason codes — single-sourced from the security module's closed set.
# Extending the set requires the integration card + consistency review; the
# service must not add codes (brief §4.2 "实现侧不得私加").
# ---------------------------------------------------------------------------
REASON_SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
REASON_SOURCE_INVALID = "SOURCE_INVALID"
REASON_SECURITY_REJECTED = "SECURITY_REJECTED"
REASON_SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
#: Defined in the closed set but NOT triggered by Intake — Materialization
#: owns it (brief §4.3). Kept so the closed set is complete.
REASON_DISTRIBUTION_FAILED = "DISTRIBUTION_FAILED"
REASON_SOURCE_NOT_SUPPORTED = "SOURCE_NOT_SUPPORTED"

#: git source types carry no V1 verifier (brief §4.4): they explicitly return
#: SOURCE_NOT_SUPPORTED — a permanent rejection in the security module's
#: classification — and the project goes straight to REJECTED with that
#: reason code. The source_type value is preserved so the "enum first,
#: capability later" record needs no rework.
GIT_SOURCE_TYPES = frozenset(
    {
        "github",
        "gitlab",
        "local_git",
    }
)

#: Hardcoded bounded-retry budget for transient failures (brief §8 UNKNOW 4:
#: hardcode 3; config-ization is explicitly deferred to a future card).
MAX_RETRIES = 3

_STATUS_RECEIVED = "RECEIVED"
_STATUS_SOURCES_OK = "SOURCES_OK"
_STATUS_INITIALIZED = "INITIALIZED"
_STATUS_REJECTED = "REJECTED"

#: The only Intake-owned outgoing edges are the security module's
#: ``INTAKE_TRANSITIONS``; terminal states are its ``TERMINAL_INTAKE_STATUSES``.
#: RECEIVED -> SOURCES_OK -> INITIALIZED is therefore a two-step chain,
#: never a direct jump (brief §5).
_INCOME_TERMINAL_STATES = intake_security.TERMINAL_INTAKE_STATUSES

#: Document type whitelist (brief §8 UNKNOW 2: reuse the upload set, never
#: re-invent it).
_DOCUMENT_EXTENSIONS = frozenset(TEXT_EXTENSIONS | OFFICE_EXTENSIONS)


class IntakeSecurityError(SecurityError):
    """Service-level security policy violation in the Intake flow.

    Raised when a request would persist a secret or otherwise violate a
    security policy before / during an Intake operation. Subclasses
    :class:`SecurityError` so the API's single transport mapping (409
    SECURITY_REJECTED) covers every security exception in one place.
    """


class IntakeTransitionError(IntakeSecurityError):
    """Raised when a Project status jump is not a legal Intake move.

    Terminal states (REJECTED / INITIALIZED) have no outgoing Intake edges,
    so re-validation of a terminal project is a conflict, not an error. The
    API maps it to 409 (``intake_terminal_state``).
    """


@dataclass
class ValidationOutcome:
    """Result of validating one source.

    ``ok=True`` means the source passed; ``reason_code`` / ``reason_detail``
    are populated only when ``ok=False`` and always carry a code from the
    security module's closed set. ``retryable`` mirrors
    ``intake_security.reason_code_is_retryable`` for that code.
    """

    ok: bool
    reason_code: str | None = None
    reason_detail: str | None = None
    retryable: bool = False


@dataclass
class _ValidateDecision:
    """Aggregated outcome of one validate pass, before persistence."""

    target_status: str
    ok: bool
    permanent: bool
    rejected_reason_code: str | None = None
    rejected_detail: str | None = None
    rejected_source_id: uuid.UUID | None = None
    retryable: bool = False
    retries_remaining: int | None = None
    verified_repo_ids: list[uuid.UUID] = field(default_factory=list)


class ProjectIntakeService:
    """Intake lifecycle owner. API handlers call only ``create_intake`` and
    ``validate_sources``; everything else is internal."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_intake(
        self,
        db: AsyncSession,
        *,
        current_user: User,
        name: str,
        description: str,
        goal: str,
        sources: Sequence[SourceSpec],
    ) -> Project:
        """Accept + register a project. Returns the created Project (RECEIVED).

        A project with zero sources is valid (the N=0 manual-only form). Each
        source becomes a Repository row with the raw source_type preserved
        (git types included — they are registered but rejected on the first
        validate). Before any row is written, every locator is scanned by
        the credential guard: a secret-bearing locator is rejected with
        ``IntakeSecurityError`` (409 SECURITY_REJECTED) and nothing is
        persisted (brief §八 "credential 明文不进 Project / Repository").
        """
        for spec in sources:
            leak = scan_locator_for_credentials(spec.locator)
            if leak is not None:
                raise IntakeSecurityError(leak)

        tenant_id = self._resolve_tenant(current_user)
        now = datetime.now(UTC)

        # Generate the PK client-side BEFORE the child Repository rows are
        # constructed: the id default (uuid.uuid4) applies at INSERT, but the
        # children's NOT NULL ``project_id`` must be concrete in the same
        # flush.  Pre-assigning mirrors the model default and is a no-op for
        # servers that would generate it anyway.
        project = Project(
            id=uuid.uuid4(),
            name=name,
            description=description,
            goal=goal,
            status=_STATUS_RECEIVED,
            created_by=current_user.id,
            tenant_id=tenant_id,
            status_changed_at=now,
        )

        repositories: list[Repository] = []
        for spec in sources:
            repositories.append(
                Repository(
                    project_id=project.id,
                    source_type=spec.source_type,
                    locator=(spec.locator if spec.locator else None),
                    display_name=spec.display_name,
                    tenant_id=tenant_id,
                )
            )

        # One atomic write of the project + all its source rows in tenant
        # scope.  The PK is pre-assigned in the constructor (client-side
        # ``uuid.uuid4``) so that every child Repository row can carry a
        # concrete ``project_id`` in the same single flush — a deferred
        # INSERT-time id default would leave the children's NOT NULL
        # ``project_id`` unset.
        created = await project_dao.add_project_with_repositories(project, repositories, tenant_id=tenant_id, db=db)
        await self._audit(
            db,
            "project.intake.received",
            current_user,
            {
                "project_id": str(created.id),
                "from_status": None,
                "to_status": _STATUS_RECEIVED,
                "source_count": len(repositories),
            },
        )
        return created

    async def validate_sources(
        self,
        db: AsyncSession,
        *,
        project: Project,
        current_user: User,
    ) -> tuple[Project, RejectionInfo | None]:
        """Validate every source of ``project`` and advance the state machine.

        ``project.repositories`` must be eager-loaded by the caller. Returns
        ``(project, rejection_info)``: ``rejection_info`` is None on the
        all-pass path and populated on every reject/hold path. The caller
        maps the decision to the transport response (200 vs 409).

        All-pass chain (brief §5): RECEIVED → SOURCES_OK → INITIALIZED in
        one call — both edges asserted through ``intake_security.transition``.
        """
        if project.status in _INCOME_TERMINAL_STATES:
            raise IntakeTransitionError(
                f"project {project.id} is in terminal state {project.status}; Intake validation is not applicable"
            )

        pre_status = project.status
        repositories = list(getattr(project, "repositories", None) or [])
        results: list[tuple[Repository, ValidationOutcome]] = []
        for repo in repositories:
            results.append((repo, await self._dispatch_validator(repo)))

        decision = self._decide_outcome(project, results)

        # Persist per-repository marks first, then the project status move.
        for repo, outcome in results:
            if outcome.ok:
                await repository_dao.mark_verified(repo, db=db)
                if getattr(repo, "pending_verifier", False):
                    await repository_dao.clear_pending_verifier(repo, db=db)
            elif outcome.retryable:
                # Transient hold: pending-verifier mark + bounded counter.
                await repository_dao.mark_pending_verifier(repo, db=db)
                await repository_dao.bump_retry_count(repo, db=db)

        if decision.permanent:
            # Terminal reject (permanent code, or retry budget exhausted).
            self._assert_transition(pre_status, _STATUS_REJECTED)
            await project_dao.reject(
                project,
                reason_code=decision.rejected_reason_code or REASON_SOURCE_INVALID,
                detail=decision.rejected_detail,
                db=db,
            )
            rejection_info = RejectionInfo(
                reason_code=decision.rejected_reason_code or REASON_SOURCE_INVALID,
                reason_detail=decision.rejected_detail,
                failed_source_id=decision.rejected_source_id,
                retryable=False,
                retries_remaining=None,
            )
        elif decision.ok:
            # All sources verified: RECEIVED → SOURCES_OK → INITIALIZED,
            # two asserted edges (never a direct jump, brief §5).
            self._assert_transition(pre_status, _STATUS_SOURCES_OK)
            await project_dao.mark_sources_ok(project, db=db)
            self._assert_transition(_STATUS_SOURCES_OK, _STATUS_INITIALIZED)
            await project_dao.transition(project, _STATUS_INITIALIZED, db=db)
            rejection_info = None
        else:
            # Transient hold: stay RECEIVED (nothing verified yet) or move to
            # SOURCES_OK (some sources verified) — monotonic, never backwards.
            target = decision.target_status
            if target != pre_status:
                self._assert_transition(pre_status, target)
                if target == _STATUS_SOURCES_OK:
                    await project_dao.mark_sources_ok(project, db=db)
            assert decision.rejected_reason_code is not None  # set by every transient decision
            rejection_info = RejectionInfo(
                reason_code=decision.rejected_reason_code,
                reason_detail=decision.rejected_detail,
                failed_source_id=decision.rejected_source_id,
                retryable=True,
                retries_remaining=decision.retries_remaining,
            )

        await self._audit(
            db,
            "project.intake.validated",
            current_user,
            {
                "project_id": str(project.id),
                "from_status": pre_status,
                "to_status": project.status,
                "reason_code": decision.rejected_reason_code,
                "failed_source_id": (str(decision.rejected_source_id) if decision.rejected_source_id else None),
                "retryable": decision.retryable,
            },
        )
        return project, rejection_info

    # ------------------------------------------------------------------
    # Validation outcome aggregation (pure state-machine logic).
    # ------------------------------------------------------------------

    @staticmethod
    def _decide_outcome(
        project: Project,
        results: list[tuple[Repository, ValidationOutcome]],
    ) -> _ValidateDecision:
        verified_ids = [repo.id for repo, oc in results if oc.ok]

        # 1) A permanent failure dominates everything → terminal REJECTED.
        # "Permanent" here is exactly "not retryable" per the security
        # module's classification (SOURCE_NOT_FOUND / SOURCE_INVALID /
        # SECURITY_REJECTED / SOURCE_NOT_SUPPORTED, or a transient that has
        # exhausted its retry budget — handled in step 2).
        for repo, oc in results:
            if not oc.ok and not oc.retryable:
                return _ValidateDecision(
                    target_status=_STATUS_REJECTED,
                    ok=False,
                    permanent=True,
                    rejected_reason_code=oc.reason_code,
                    rejected_detail=oc.reason_detail,
                    rejected_source_id=repo.id,
                    retryable=False,
                    verified_repo_ids=verified_ids,
                )

        # 2) Transient / unreachable sources: bounded retry, then escalate.
        transient = [(repo, oc) for repo, oc in results if not oc.ok and oc.retryable]
        if transient:
            # The binding retry counter is the highest one the project has
            # already climbed; the persistence step bumps it afterwards.
            max_retries_used = max(int(getattr(repo, "retry_count", 0) or 0) for repo, _ in transient)
            lead_repo, lead_oc = transient[0]
            if max_retries_used + 1 >= MAX_RETRIES:
                # Budget exhausted: escalate to REJECTED, reason unchanged
                # (the code the validator emitted, e.g. SOURCE_UNREACHABLE).
                return _ValidateDecision(
                    target_status=_STATUS_REJECTED,
                    ok=False,
                    permanent=True,
                    rejected_reason_code=lead_oc.reason_code,
                    rejected_detail=lead_oc.reason_detail,
                    rejected_source_id=lead_repo.id,
                    retryable=False,
                    retries_remaining=0,
                    verified_repo_ids=verified_ids,
                )
            # Monotonic hold: RECEIVED stays RECEIVED while nothing has
            # verified; SOURCES_OK is the "some sources ok" milestone.
            target = _STATUS_SOURCES_OK if verified_ids else _STATUS_RECEIVED
            remaining = max(0, MAX_RETRIES - (max_retries_used + 1))
            return _ValidateDecision(
                target_status=target,
                ok=False,
                permanent=False,
                rejected_reason_code=lead_oc.reason_code,
                rejected_detail=lead_oc.reason_detail,
                rejected_source_id=lead_repo.id,
                retryable=True,
                retries_remaining=remaining,
                verified_repo_ids=verified_ids,
            )

        # 3) All sources verified (vacuously true for N=0) → INITIALIZED.
        return _ValidateDecision(
            target_status=_STATUS_INITIALIZED,
            ok=True,
            permanent=False,
            verified_repo_ids=verified_ids,
        )

    @staticmethod
    def _assert_transition(current: str, target: str) -> None:
        """Assert a legal Intake move through the security module's graph.

        ``intake_security.transition`` is the single guard: it enforces the
        closed edge set (RECEIVED → {SOURCES_OK, REJECTED}, SOURCES_OK →
        {INITIALIZED, REJECTED}) and refuses every illegal jump — in
        particular REJECTED → INITIALIZED and INITIALIZED → RECEIVED
        (brief §十). ``current == target`` stays are no-ops while
        non-terminal.
        """
        if current == target:
            return
        try:
            transition(current, target)
        except InvalidTransition as exc:
            raise IntakeTransitionError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Validators (dispatched by source_type).
    # ------------------------------------------------------------------

    async def _dispatch_validator(self, repo: Repository) -> ValidationOutcome:
        source_type = repo.source_type
        if source_type == "manual":
            return await self._validate_manual(repo)
        if source_type == "local_folder":
            return await self._validate_local_folder(repo)
        if source_type == "document":
            return await self._validate_document(repo)
        if source_type == "zip":
            return await self._validate_zip(repo)
        if source_type in GIT_SOURCE_TYPES:
            return await self._validate_unsupported(repo)
        # Unknown source_type (the schema layer should have blocked this):
        # treat as unsupported rather than inventing a validator.
        return self._not_supported_outcome(f"source_type {source_type!r} has no V1 verifier")

    @staticmethod
    def _not_supported_outcome(detail: str) -> ValidationOutcome:
        # Permanent in the security module's classification: a missing
        # verifier is a capability gap, not a transient condition.
        return ValidationOutcome(
            ok=False,
            reason_code=REASON_SOURCE_NOT_SUPPORTED,
            reason_detail=detail,
            retryable=intake_security.reason_code_is_retryable(REASON_SOURCE_NOT_SUPPORTED),
        )

    async def _validate_manual(self, repo: Repository) -> ValidationOutcome:
        # Pure registration, no external dependency (brief §2).
        return ValidationOutcome(ok=True)

    @staticmethod
    def _security_gate(raw_path: object, *, source_type: str) -> ValidationOutcome | None:
        """Return a rejection outcome if the host path is unsafe, else None.

        Routed through ``intake_security.check_host_path`` — the authoritative
        path-shape rule (traversal/NUL → SECURITY_REJECTED, sensitive roots →
        SECURITY_REJECTED, local_folder relative → SOURCE_INVALID). An
        explicitly absent path is the validator's own miss, not a security
        finding.
        """
        if not isinstance(raw_path, str) or not raw_path:
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_NOT_FOUND,
                reason_detail=f"{source_type} source requires a non-empty host path",
                retryable=False,
            )
        verdict = check_host_path(raw_path, source_type=source_type)
        if verdict.ok:
            return None
        assert verdict.reason_code is not None  # populated by every SecurityVerdict.reject()
        return ValidationOutcome(
            ok=False,
            reason_code=verdict.reason_code,
            reason_detail=verdict.detail,
            retryable=intake_security.reason_code_is_retryable(verdict.reason_code),
        )

    async def _validate_local_folder(self, repo: Repository) -> ValidationOutcome:
        locator = repo.locator or {}
        rejection = self._security_gate(locator.get("path"), source_type="local_folder")
        if rejection is not None:
            return rejection
        path = locator["path"]
        if not os.path.exists(path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_NOT_FOUND,
                reason_detail=f"path {path!r} does not exist",
                retryable=False,
            )
        if not os.path.isdir(path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"path {path!r} is not a directory",
                retryable=False,
            )
        if not os.access(path, os.R_OK | os.X_OK):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"directory {path!r} is not readable",
                retryable=False,
            )
        try:
            non_empty = any(os.scandir(path))
        except OSError as exc:
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"directory {path!r} could not be listed ({exc})",
                retryable=False,
            )
        if not non_empty:
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"directory {path!r} is empty",
                retryable=False,
            )
        return ValidationOutcome(ok=True)

    async def _validate_document(self, repo: Repository) -> ValidationOutcome:
        locator = repo.locator or {}
        if "storage_key" in locator:
            return await self._validate_document_storage_key(locator["storage_key"])
        rejection = self._security_gate(locator.get("path"), source_type="document")
        if rejection is not None:
            return rejection
        host_path = locator["path"]
        if not os.path.exists(host_path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_NOT_FOUND,
                reason_detail=f"document {host_path!r} does not exist",
                retryable=False,
            )
        if not os.path.isfile(host_path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"document {host_path!r} is not a file",
                retryable=False,
            )
        if not os.access(host_path, os.R_OK):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"document {host_path!r} is not readable",
                retryable=False,
            )
        if not _is_supported_document(host_path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=(
                    f"document type not supported for {host_path!r} (allowed: {sorted(_DOCUMENT_EXTENSIONS)})"
                ),
                retryable=False,
            )
        return ValidationOutcome(ok=True)

    async def _validate_document_storage_key(self, raw_key: object) -> ValidationOutcome:
        key = normalize_storage_key(str(raw_key))
        try:
            backend = get_storage_backend()
            if not await backend.exists(key):
                return ValidationOutcome(
                    ok=False,
                    reason_code=REASON_SOURCE_NOT_FOUND,
                    reason_detail=f"storage key {key!r} not found",
                    retryable=False,
                )
            entry = await backend.stat(key)
            if entry.is_dir:
                return ValidationOutcome(
                    ok=False,
                    reason_code=REASON_SOURCE_INVALID,
                    reason_detail=f"storage key {key!r} is a directory",
                    retryable=False,
                )
        except SecurityError:
            # A security exception raised by a storage layer is still a
            # security finding — let it surface as 409, never as "unreachable".
            raise
        except Exception as exc:  # storage outage → transient hold (brief §4.5)
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_UNREACHABLE,
                reason_detail=f"document storage backend unreachable ({exc.__class__.__name__})",
                retryable=intake_security.reason_code_is_retryable(REASON_SOURCE_UNREACHABLE),
            )
        if not _is_supported_document(key):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=(f"document type not supported for {key!r} (allowed: {sorted(_DOCUMENT_EXTENSIONS)})"),
                retryable=False,
            )
        return ValidationOutcome(ok=True)

    async def _validate_zip(self, repo: Repository) -> ValidationOutcome:
        locator = repo.locator or {}
        if "storage_key" in locator:
            return await self._validate_zip_storage_key(locator["storage_key"])
        rejection = self._security_gate(locator.get("path"), source_type="zip")
        if rejection is not None:
            return rejection
        host_path = locator["path"]
        if not os.path.exists(host_path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_NOT_FOUND,
                reason_detail=f"zip {host_path!r} does not exist",
                retryable=False,
            )
        if not os.path.isfile(host_path):
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_INVALID,
                reason_detail=f"zip {host_path!r} is not a file",
                retryable=False,
            )
        try:
            async with aiofiles.open(host_path, "rb") as fh:
                data = await fh.read()
        except OSError as exc:
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_UNREACHABLE,
                reason_detail=f"zip {host_path!r} could not be read ({exc})",
                retryable=intake_security.reason_code_is_retryable(REASON_SOURCE_UNREACHABLE),
            )
        return self._zip_slip_outcome(data)

    async def _validate_zip_storage_key(self, raw_key: object) -> ValidationOutcome:
        key = normalize_storage_key(str(raw_key))
        try:
            backend = get_storage_backend()
            if not await backend.exists(key):
                return ValidationOutcome(
                    ok=False,
                    reason_code=REASON_SOURCE_NOT_FOUND,
                    reason_detail=f"storage key {key!r} not found",
                    retryable=False,
                )
            entry = await backend.stat(key)
            if entry.is_dir:
                return ValidationOutcome(
                    ok=False,
                    reason_code=REASON_SOURCE_INVALID,
                    reason_detail=f"storage key {key!r} is a directory, not a file",
                    retryable=False,
                )
            data = await backend.read_bytes(key)
        except SecurityError:
            raise
        except Exception as exc:  # storage outage → transient hold (brief §4.5)
            return ValidationOutcome(
                ok=False,
                reason_code=REASON_SOURCE_UNREACHABLE,
                reason_detail=f"zip storage backend unreachable ({exc.__class__.__name__})",
                retryable=intake_security.reason_code_is_retryable(REASON_SOURCE_UNREACHABLE),
            )
        return self._zip_slip_outcome(data)

    @staticmethod
    def _zip_slip_outcome(data: bytes) -> ValidationOutcome:
        # ``check_zip_slip`` inspects member names only — no extraction, no
        # write, no execute (brief §七/§八). Unsafe member → SECURITY_REJECTED;
        # an unreadable container → SOURCE_INVALID.
        verdict = check_zip_slip(data)
        if not verdict.ok:
            assert verdict.reason_code is not None  # populated by every SecurityVerdict.reject()
            return ValidationOutcome(
                ok=False,
                reason_code=verdict.reason_code,
                reason_detail=verdict.detail,
                retryable=intake_security.reason_code_is_retryable(verdict.reason_code),
            )
        return ValidationOutcome(ok=True)

    async def _validate_unsupported(self, repo: Repository) -> ValidationOutcome:
        # No V1 verifier for git sources (brief §4.4): explicit
        # SOURCE_NOT_SUPPORTED, permanent — the project is rejected, never
        # "validated successfully".
        return self._not_supported_outcome(
            f"{repo.source_type} source: no V1 verifier (Phase 2B git acquisition pending)"
        )

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    def _resolve_tenant(self, current_user: User) -> uuid.UUID:
        tenant_id = getattr(current_user, "tenant_id", None)
        if tenant_id is None:
            raise ValueError("current_user has no tenant_id; Intake is tenant-scoped")
        return tenant_id

    async def _audit(self, db, action: str, user: User, details: dict) -> None:
        """Best-effort audit trail for each state advance (brief §3.4).

        Mirrors the existing audit-logging convention: a failure to write
        the audit row is logged but never breaks the primary Intake
        operation (the ignored failure is narrow and explained).
        """
        try:
            row = AuditLog(
                tenant_id=getattr(user, "tenant_id", None),
                user_id=user.id,
                action=action,
                details=details,
            )
            db.add(row)
            await db.flush()
        except Exception as exc:
            logger.error(f"[project_intake] audit write failed for {action}: {exc}")


def _is_supported_document(name: str) -> bool:
    ext = os.path.splitext(str(name))[1].lower()
    return ext in _DOCUMENT_EXTENSIONS


project_intake_service = ProjectIntakeService()

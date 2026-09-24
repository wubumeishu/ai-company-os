"""Project Materialization service (Phase 2B-3, card t_025cda02).

Owns the *materialization* stage of a formally-accepted Project: copying a
Project's already-verified source material into a target agent's storage
subtree so a later Run can consume it through the existing TempWorkspace /
storage read surface.  Intake (Phase 2B-2) is the state machine that brings a
Project to ``INITIALIZED``; this service is the independent stage that moves
material into an agent.  It is neither the Project entity nor an Agent Run.

Spec: ``docs/MATERIALIZATION_SECURE_SPEC_V1.md`` (card t_c672b2c2, committed
on ``wt/t_c672b2c2`` @ 10d16f4f — the single adjudicated text the builder and
the reviewer both consume).  Every security decision below re-uses the
existing authoritative guards and invents none of its own (spec §4 routing
rule):

- host-path shape               -> ``intake_security.check_host_path``
- zip member names (Zip Slip)   -> ``intake_security.check_zip_slip``
- tenant / read access gates    -> ``intake_security.verify_tenant_scope`` /
                                   ``verify_read_access``
- agent authorization + tenant  -> ``core.permissions.check_agent_access``
- target/staging key normalize  -> ``storage_runtime.utils.normalize_storage_key``
- human edit locks              -> ``workspace_collaboration.get_active_lock``
- directory-level mutation lock -> ``workspace_locking.workspace_locks``
- conditional writes            -> ``storage_runtime.base.WriteCondition`` /
                                   ``write_bytes_if_match``
- document type whitelist       -> ``project_intake_service._DOCUMENT_EXTENSIONS``
                                   (the intake constant, re-imported — never
                                   copied, spec §3.3)

What this stage does NOT do (card §1 hard rules, spec §1.3): it never changes
Project status, creates a Task, starts an Agent Run, sends a prompt, executes
code, launches a Squad, or mutates the source (host) files.  Its only durable
effect is that files land inside the *named* agent's storage subtree; a later
Run sees them automatically through TempWorkspace materialization
(``agent_tools.py`` default-path semantics — no runtime change required).
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.user import User
from app.models.workspace import WorkspaceFileRevision
from app.schemas.project_intake import MaterializationOut, MaterializationRepoResult
from app.services import intake_security
from app.services.intake_security import SecurityError, check_host_path, check_zip_slip
from app.services.project_intake_service import _DOCUMENT_EXTENSIONS, get_storage_backend
from app.services.storage_runtime.base import StorageVersion, WriteCondition, content_hash_bytes
from app.services.storage_runtime.utils import normalize_storage_key
from app.services.workspace_collaboration import get_active_lock, normalize_workspace_path
from app.services.workspace_locking import workspace_locks

if TYPE_CHECKING:
    from app.models.agent import Agent

__all__ = [
    "GIT_SOURCE_TYPES",
    "MATERIALIZED_RESERVATION_NAME_SET",
    "RESERVED_STORAGE_NAMES",
    "MaterializationNotReady",
    "MaterializationSecurity",
    "ProjectMaterializationService",
    "content_hash_bytes",
    "make_plan",
    "make_repo",
    "project_materialization_service",
]

#: git source types carry no V1 acquisition path (spec §3.5): they can never
#: bring a Project to INITIALIZED under the intake state machine, so under the
#: INITIALIZED gate they are unreachable; a materialization that ever sees one
#: fails closed with SOURCE_NOT_READY (explicit unknown-value behavior for the
#: extensible source_type enum, card §13 / spec §3.5).
GIT_SOURCE_TYPES = frozenset({"github", "gitlab", "local_git"})

#: Reserved first-segment names (spec §2.4): neither a material_name nor the
#: first segment of any source-relative path may collide with these.  They are
#: either protected Run paths (tasks.json), the TempWorkspace default
#: materialize set (skills/memory/workspace/focus.md/soul.md/HEARTBEAT.md),
#: the self-namespace this stage writes into (projects), or the staging root
#: (materialize-tmp).  Hard-coded per the spec so the set is one source of
#: truth; a collision is SECURITY_REJECTED for the whole repo (0 writes).
RESERVED_STORAGE_NAMES = frozenset(
    {
        ".materialize-tmp",
        ".git",
        ".skill",
        "skills",
        "memory",
        "tasks.json",
        "soul.md",
        "focus.md",
        "HEARTBEAT.md",
        "workspace",
        "projects",
    }
)
MATERIALIZED_RESERVATION_NAME_SET = RESERVED_STORAGE_NAMES

#: Single-file and whole-call byte budgets (spec §3), aligned with the
#: Runtime's own materialize budget constants
#: (``agent_tools.TOOL_MATERIALIZE_MAX_FILE_BYTES`` / ``_TOTAL_BYTES``): a
#: file that would exceed the Run's 50MB / 500MB materialize budget is rejected
#: here before it is ever staged.
MAX_MATERIALIZE_FILE_BYTES = 50 * 1024 * 1024
MAX_MATERIALIZE_TOTAL_BYTES = 500 * 1024 * 1024

#: material_name shape bound (spec §2.1): length 1..64.
_MAX_MATERIAL_NAME_LEN = 64


# ---------------------------------------------------------------------------
# Service-level transport-mappable errors (narrow; the API maps them — no
# business in the transport layer, backend AGENTS.md).
# ---------------------------------------------------------------------------


class MaterializationSecurity(SecurityError):
    """A hard isolation violation (M9 fourth gate / entry tenant re-check).

    Subclasses :class:`SecurityError` so the transport layer keeps one narrow
    mapping (403) for every security exception, intake-style.
    """


class MaterializationNotReady(SecurityError):
    """Pre-call status / source-readiness gate (spec §1 / §10.2): 409.

    Carries a closed-set ``code`` + ``retryable`` + a bounded message.  Used
    before ANY I/O: a non-INITIALIZED project, a repository still marked
    pending-verifier, or a git-source repo under the gate.
    """

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Internal plan / failure carriers (private; never crossed into the API).
# ---------------------------------------------------------------------------


@dataclass
class _RepoPlan:
    """One repository's read -> stage -> publish plan.

    ``payload`` (rel -> bytes) is filled in the read phase; outcome counters
    are filled by the publish phase.  ``agent_id`` / ``actor_id`` are stamped
    by the caller so the key formulas and the provenance rows never read the
    target from a mutable global.
    """

    repo: Repository
    source_type: str
    agent_id: uuid.UUID
    actor_id: uuid.UUID
    material_name: str = ""
    agent_tenant_id: uuid.UUID | None = None
    rels: list[str] = field(default_factory=list)
    payload: dict[str, bytes] = field(default_factory=dict)
    # Terminal outcome fields (closed sets per spec §10.1).
    outcome: str = "FAILED"
    reason_code: str | None = None
    detail: str | None = None
    written: int = 0
    converged: int = 0
    skipped: int = 0
    staging_key: str | None = None
    staging_enabled: bool = False

    # -- key formulas (spec §2.3: the single authoritative concatenation) --

    def target_key(self, rel: str) -> str:
        key = normalize_storage_key(
            f"{self.agent_id}/projects/{self.repo.project_id}/{self.material_name}/{rel}"
        )
        # Defense-in-depth: the prefix must equal the *named* agent's projects
        # subtree (spec §2.5 layout invariant); nothing else feeds the key.
        assert key.startswith(f"{self.agent_id}/projects/"), f"target key escaped agent subtree: {key!r}"
        return key

    def staging_key_for(self, rel: str) -> str:
        key = normalize_storage_key(f"{self.agent_id}/.materialize-tmp/{self.repo.id}/{rel}")
        assert key.startswith(f"{self.agent_id}/.materialize-tmp/"), f"staging key escaped: {key!r}"
        return key


class _RepoRejected(Exception):
    """A source reader's closed-set rejection (security / shape / size)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _RepoUnreachable(Exception):
    """Source unavailability (NOT a security finding): transient / not-found.

    Distinct from :class:`_RepoRejected` so a missing/locked source reports
    SOURCE_NOT_FOUND / SOURCE_UNREACHABLE while a genuinely unsafe one reports
    SECURITY_REJECTED / SOURCE_INVALID — the closed-set reason codes keep the
    "why" inspectable (backend AGENTS.md independent-outcomes rule).
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ProjectMaterializationService:
    """Materialization owner.  API handlers call only ``materialize``;
    everything else is internal."""

    # ------------------------------------------------------------------
    # Public entry point (spec §12).
    # ------------------------------------------------------------------

    async def materialize(
        self,
        db: AsyncSession,
        *,
        project: Project,
        agent: Agent,
        overwrite: bool,
        current_user: User,
    ) -> MaterializationOut:
        """Materialize ``project``'s verified sources into ``agent``'s subtree.

        ``project.repositories`` must be eager-loaded by the caller.  The
        target ``agent`` must already have been authorized by the transport
        layer (``check_agent_access``); this method re-asserts the isolation
        gates fail-closed so a background caller that skips the API cannot
        write cross-tenant (spec §5.1 / M9).  Background/queue callers must
        wrap the call in ``tenant_context(tenant_id)`` (V1 rule, spec §5.1).

        Never changes Project status, never creates a Task / Run / prompt /
        code execution / Squad.  Returns a :class:`MaterializationOut` whose
        ``outcome`` drives the transport status code (SUCCESS -> 201;
        PARTIAL / FAILED -> 409 with the full per-repo detail).
        """
        self._entry_gates(project, agent, current_user)
        plans = [await self._plan_repo(project, repo, agent, current_user) for repo in list(project.repositories)]
        total_bytes = sum(len(data) for plan in plans for data in plan.payload.values())
        if total_bytes > MAX_MATERIALIZE_TOTAL_BYTES:
            # Call-wide budget exceeded (spec §3): every content-bearing repo
            # fails with SOURCE_SIZE_LIMIT.  This runs AFTER the read phase so
            # no I/O happened yet (staging is not enabled), and BEFORE staging
            # so nothing is written.
            for plan in plans:
                if plan.payload:
                    self._fail(plan, "SOURCE_SIZE_LIMIT", f"call-wide material exceeds {MAX_MATERIALIZE_TOTAL_BYTES} bytes")
        outcome = await self._run_pipeline(db, plans, project, agent, overwrite, current_user)
        return outcome

    def _entry_gates(self, project: Project, agent: Agent, current_user: User) -> None:
        """Fail-closed pre-I/O gates, in the spec §5.1 gate order.

        1. ``verify_tenant_scope(project, current_user)`` — the entry re-check
           (a bare-PK fetch with no tenant context must not disclose / write
           another tenant's row, spec §5.1 FACT ``dao/base.py`` null-context
           degeneration).
        2. M9 fourth gate: ``agent.tenant_id == project.tenant_id`` (hard,
           fail-closed) — closes the "Tenant-A project -> Tenant-B agent"
           combination gap ``check_agent_access`` alone leaves open.
        3. Status gate: only ``INITIALIZED`` (spec §1 rule 1, before ANY I/O).
        4. Per-repo minimum source gate (spec §1 rule 2 / card §3): a
           repository that is not ``verified`` or is still ``pending_verifier``
           makes the call SOURCE_NOT_READY (defense-in-depth; a bad row fails
           closed, sibling repos are not blamed for it).
        5. Git-source gate (design GIT_ACQ_DESIGN_V1.md §C.3): a git repo
           whose acquisition artifact is missing or not yet verified still
           fails closed with SOURCE_NOT_READY (the Phase 2B-3 behavior — the
           "enum first, capability later" marker is preserved by construction:
           the absence of a verified ``locator.acq_artifact`` is exactly the
           pre-acquisition state).  A git repo that HAS a verified acquisition
           artifact passes the gate and is read by the git source reader
           (``_plan_git``) — the ONE bounded tar, no re-clone.
        """
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        agent_tenant = getattr(agent, "tenant_id", None)
        if agent_tenant is None or project.tenant_id is None:
            raise MaterializationSecurity("materialization has no tenant context; refusing to write")
        if agent_tenant != project.tenant_id:
            raise MaterializationSecurity("target agent tenant does not match the project tenant")
        if project.status != "INITIALIZED":
            raise MaterializationNotReady(
                code="SOURCE_NOT_READY",
                message=f"project {project.id} is in status {project.status}; only INITIALIZED projects may be materialized",
            )
        for repo in list(getattr(project, "repositories", None) or []):
            if not repo.verified or repo.pending_verifier:
                raise MaterializationNotReady(
                    code="SOURCE_NOT_READY",
                    message=f"repository {repo.id} is not verified-ready (verified={repo.verified}, pending_verifier={repo.pending_verifier})",
                )
            if repo.source_type in GIT_SOURCE_TYPES and not self._git_artifact_verified(repo):
                # The ONE gate the acquisition work flips (design §C.3 / hook #2):
                # a git repo passes pre-I/O ONLY when its acquisition produced
                # a verified artifact.  The absence of the key, an unverified
                # mark, or a pending verifier is the pre-acquisition state and
                # keeps the Phase 2B-3 fail-closed behavior by construction.
                raise MaterializationNotReady(
                    code="SOURCE_NOT_READY",
                    message=f"repository {repo.id} ({repo.source_type}) has no verified acquisition artifact",
                )

    # ------------------------------------------------------------------
    # Phase 1 — read / validate / enumerate one repo (0 writes, spec §7.1 [1]).
    # ------------------------------------------------------------------

    async def _plan_repo(self, project: Project, repo: Repository, agent: Agent, current_user: User) -> _RepoPlan:
        material_name = self._material_name(repo)
        plan = _RepoPlan(
            repo=repo,
            source_type=repo.source_type,
            agent_id=agent.id,
            actor_id=current_user.id,
            material_name=material_name or "",
            agent_tenant_id=getattr(agent, "tenant_id", None),
        )
        if repo.source_type == "manual":
            # Explicit skip, zero content invention (M4② / spec §3.1).
            plan.outcome = "SKIPPED_NO_MATERIAL"
            plan.reason_code = "SKIPPED_NO_MATERIAL"
            plan.detail = "manual source carries no material; explicitly skipped"
            plan.skipped = 1
            return plan
        if material_name is None:
            self._fail(plan, "SOURCE_INVALID", "material_name fails the shape / reserved-name checks")
            return plan
        try:
            if repo.source_type == "local_folder":
                await self._plan_local_folder(plan)
            elif repo.source_type == "document":
                await self._plan_document(plan)
            elif repo.source_type == "zip":
                await self._plan_zip(plan)
            elif repo.source_type in GIT_SOURCE_TYPES:
                await self._plan_git(plan)
            else:
                self._fail(plan, "SOURCE_INVALID", f"unsupported source_type {repo.source_type!r}")
                return plan
        except _RepoRejected as exc:
            self._fail(plan, exc.code, exc.detail)
            return plan
        except _RepoUnreachable as exc:
            plan.outcome = "FAILED"
            plan.reason_code = exc.code
            plan.detail = exc.detail
            return plan
        except SecurityError:
            # A storage-layer security exception is a security finding
            # (spec §3.3): it must surface as 409, never degrade to
            # "unreachable".
            raise
        except Exception as exc:  # storage outage / unreadable source
            plan.outcome = "FAILED"
            plan.reason_code = "SOURCE_UNREACHABLE"
            plan.detail = f"{repo.source_type} source could not be read ({exc.__class__.__name__})"
            return plan
        for rel, data in plan.payload.items():
            if len(data) > MAX_MATERIALIZE_FILE_BYTES:
                self._fail(plan, "SOURCE_SIZE_LIMIT", f"file {rel!r} exceeds {MAX_MATERIALIZE_FILE_BYTES} bytes")
                return plan
        # A content-bearing source that enumerated to ZERO files is
        # SOURCE_INVALID for every non-manual type (spec §3.2 "为空" →
        # SOURCE_INVALID; §3.4 a directory-only / empty archive has no
        # material).  Nothing is materialized and a fake SUCCESS is refused.
        if not plan.payload:
            self._fail(plan, "SOURCE_INVALID", f"{repo.source_type} source enumerated to zero files; nothing to materialize")
            return plan
        plan.outcome = "SUCCESS"
        plan.reason_code = None
        return plan

    # ---- source readers (rel -> bytes, enforcing spec §2.2 / §2.4 / §3) ----

    async def _plan_local_folder(self, plan: _RepoPlan) -> None:
        raw_path = (plan.repo.locator or {}).get("path")
        rejection = self._host_gate(raw_path, source_type="local_folder")
        if rejection is not None:
            raise _RepoRejected(*rejection)
        root = Path(os.path.abspath(str(raw_path)))
        if not root.exists():
            raise _RepoUnreachable("SOURCE_NOT_FOUND", f"folder {root} does not exist")
        if not root.is_dir():
            raise _RepoUnreachable("SOURCE_INVALID", f"folder {root} is not a directory")
        if not os.access(root, os.R_OK | os.X_OK):
            raise _RepoUnreachable("SOURCE_INVALID", f"folder {root} is not readable")
        # Recursive enumeration (spec §3 invariant): os.walk; symlinks and
        # non-regular files are SKIPPED — a host symlink is an escape vector.
        # The source tree is only ever READ here: no source file is moved,
        # renamed, or deleted.
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    data = await _read_host_file_bytes(full)
                except OSError as exc:
                    raise _RepoUnreachable("SOURCE_UNREACHABLE", f"file {full.name} could not be read ({exc})")
                rel = self._normalize_rel(os.path.relpath(full, root).replace(os.sep, "/"))
                if rel is None:
                    raise _RepoRejected("SECURITY_REJECTED", "source-relative path contains a traversal segment")
                first = rel.split("/", 1)[0]
                if first in RESERVED_STORAGE_NAMES:
                    raise _RepoRejected("SECURITY_REJECTED", f"reserved first-segment name {first!r}")
                plan.payload[rel] = data
                plan.rels.append(rel)

    async def _plan_document(self, plan: _RepoPlan) -> None:
        locator = plan.repo.locator or {}
        if "storage_key" in locator:
            await self._plan_document_storage_key(plan, locator["storage_key"])
            return
        raw_path = locator.get("path")
        rejection = self._host_gate(raw_path, source_type="document")
        if rejection is not None:
            raise _RepoRejected(*rejection)
        path = Path(os.path.abspath(str(raw_path)))
        if not path.exists():
            raise _RepoUnreachable("SOURCE_NOT_FOUND", f"document {path} does not exist")
        if not path.is_file():
            raise _RepoUnreachable("SOURCE_INVALID", f"document {path} is not a file")
        if not os.access(path, os.R_OK):
            raise _RepoUnreachable("SOURCE_INVALID", f"document {path} is not readable")
        filename = path.name
        if not self._is_supported_document(filename):
            raise _RepoUnreachable("SOURCE_INVALID", f"document type not supported for {filename!r}")
        rel = self._check_rel_shape(plan, self._normalize_rel(filename), filename)
        plan.payload[rel] = await _read_host_file_bytes(path)
        plan.rels.append(rel)

    async def _plan_document_storage_key(self, plan: _RepoPlan, raw_key: object) -> None:
        backend = get_storage_backend()
        key = normalize_storage_key(str(raw_key))
        try:
            if not await backend.exists(key):
                raise _RepoUnreachable("SOURCE_NOT_FOUND", f"storage key {key!r} not found")
            entry = await backend.stat(key)
            if entry.is_dir:
                raise _RepoUnreachable("SOURCE_INVALID", f"storage key {key!r} is a directory")
            data = await backend.read_bytes(key)
        except SecurityError:
            raise  # storage-layer security finding -> 409, never unreachable
        except _RepoUnreachable:
            raise
        except Exception as exc:  # storage outage -> transient hold (intake parity)
            raise _RepoUnreachable("SOURCE_UNREACHABLE", f"document storage backend unreachable ({exc.__class__.__name__})")
        filename = key.rsplit("/", 1)[-1]
        if not self._is_supported_document(filename):
            raise _RepoUnreachable("SOURCE_INVALID", f"document type not supported for {filename!r}")
        rel = self._check_rel_shape(plan, self._normalize_rel(filename), filename)
        plan.payload[rel] = data
        plan.rels.append(rel)

    async def _plan_zip(self, plan: _RepoPlan) -> None:
        locator = plan.repo.locator or {}
        if "storage_key" in locator:
            backend = get_storage_backend()
            key = normalize_storage_key(str(locator["storage_key"]))
            try:
                if not await backend.exists(key):
                    raise _RepoUnreachable("SOURCE_NOT_FOUND", f"storage key {key!r} not found")
                entry = await backend.stat(key)
                if entry.is_dir:
                    raise _RepoUnreachable("SOURCE_INVALID", f"storage key {key!r} is a directory")
                data = await backend.read_bytes(key)
            except SecurityError:
                raise
            except _RepoUnreachable:
                raise
            except Exception as exc:
                raise _RepoUnreachable("SOURCE_UNREACHABLE", f"zip storage backend unreachable ({exc.__class__.__name__})")
        else:
            raw_path = locator.get("path")
            rejection = self._host_gate(raw_path, source_type="zip")
            if rejection is not None:
                raise _RepoRejected(*rejection)
            path = Path(os.path.abspath(str(raw_path)))
            if not path.exists():
                raise _RepoUnreachable("SOURCE_NOT_FOUND", f"zip {path} does not exist")
            if not path.is_file():
                raise _RepoUnreachable("SOURCE_INVALID", f"zip {path} is not a file")
            try:
                data = await _read_host_file_bytes(path)
            except OSError as exc:
                raise _RepoUnreachable("SOURCE_UNREACHABLE", f"zip {path} could not be read ({exc})")
        # Pre-unpack single guard (spec §3.4 / §4): the intake-time pass is NOT
        # evidence of safety now — the host file / storage object may have been
        # replaced.  Unsafe member -> SECURITY_REJECTED (whole repo, 0 writes);
        # unreadable container -> SOURCE_INVALID.
        verdict = check_zip_slip(data)
        if not verdict.ok:
            assert verdict.reason_code is not None
            raise _RepoRejected(verdict.reason_code, verdict.detail or "unsafe archive member")
        self._extract_zip_members(plan, data)

    def _extract_zip_members(self, plan: _RepoPlan, data: bytes) -> None:
        """Pure in-memory unpack (spec §3.4): no member is extracted to disk,
        no script is executed, no host symlink is created.  A symlink entry
        (external 0xA000) materializes its target-path string as an ordinary
        file — a declared V1 limitation (spec §9.3.4), not a host link.
        """
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                name = info.filename
                if name.endswith("/"):  # directory entry -> skip
                    continue
                # Second-depth guard (spec §2.2 / §3.4): a passing
                # check_zip_slip is not a trust credential — re-normalize the
                # member name and assert its prefix before it becomes a write
                # target.
                rel = self._normalize_rel(name)
                if rel is None:
                    raise _RepoRejected("SECURITY_REJECTED", "archive member path contains a traversal segment")
                first = rel.split("/", 1)[0]
                if first in RESERVED_STORAGE_NAMES:
                    raise _RepoRejected("SECURITY_REJECTED", f"reserved archive member {name!r}")
                plan.payload[rel] = archive.read(name)
                plan.rels.append(rel)

    # ------------------------------------------------------------------
    # Git source reader (Phase 2B-4, design GIT_ACQ_DESIGN_V1.md §C.3):
    # reads the ONE bounded tar the acquisition stage published.  No
    # re-clone, no hooks, no install (card §17) — the artifact is the
    # handoff object (card §11).
    # ------------------------------------------------------------------

    def _git_artifact_verified(self, repo: Repository) -> bool:
        """Whether a git repo's acquisition artifact is present + verified.

        The gate helper (design §C.3 hook #2): the locator must carry a
        non-empty ``acq_artifact`` storage key AND the row must be marked
        ``verified`` with the pending-verifier mark cleared.  This is the
        ONLY condition that turns the git hard-gate fail-open; anything
        short of it is the pre-acquisition state and stays
        SOURCE_NOT_READY (the Phase 2B-3 behavior, preserved by
        construction).
        """
        loc = repo.locator or {}
        artifact = loc.get("acq_artifact")
        if not isinstance(artifact, str) or not artifact:
            return False
        return bool(repo.verified) and not repo.pending_verifier

    async def _plan_git(self, plan: _RepoPlan) -> None:
        """Read the acquisition tar into the plan payload (git source reader).

        Reads the ONE bounded object via the storage facade (no re-clone —
        card §11), re-runs the SAME shared member-path normalizer +
        reserved-name check the other readers use (design §18 "one rule,
        never a second contradicting set"), and follows the exact read
        discipline of ``_plan_local_folder`` (symlinks never followed,
        source never modified).  A missing artifact is SOURCE_NOT_FOUND
        (the gate already fails this pre-I/O; this is the per-repo path),
        an unreadable object is SOURCE_UNREACHABLE, and an unsafe member
        is SECURITY_REJECTED (0 writes).
        """
        loc = plan.repo.locator or {}
        raw_key = loc.get("acq_artifact")
        if not isinstance(raw_key, str) or not raw_key:
            raise _RepoUnreachable(
                "SOURCE_NOT_FOUND",
                "git acquisition artifact is not present in the repository locator",
            )
        key = normalize_storage_key(raw_key)
        backend = get_storage_backend()
        try:
            if not await backend.exists(key):
                raise _RepoUnreachable("SOURCE_NOT_FOUND", f"git artifact {key!r} not found in storage")
            data = await backend.read_bytes(key)
        except SecurityError:
            raise  # storage-layer security finding -> 409, never unreachable
        except _RepoUnreachable:
            raise
        except Exception as exc:  # noqa: BLE001 - storage outage / unreadable object -> transient hold (local-folder parity)
            raise _RepoUnreachable("SOURCE_UNREACHABLE", f"git artifact could not be read ({exc.__class__.__name__})")
        # Pure in-memory unpack (the acquisition tar carries only the
        # working tree — no .git metadata, no hooks, card §17).  A tar
        # member is never extracted to disk; bytes go straight to the
        # payload like the zip reader.  A corrupt / truncated object is a
        # transient unreadable source (SOURCE_UNREACHABLE, local-folder
        # parity), NOT a 500: the unpack is guarded, security rejections
        # raised inside it still propagate.
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
                for member in archive.getmembers():
                    if member.isdir():
                        continue
                    if member.issym() or member.islnk():
                        # A symlink / hardlink inside the artifact is an
                        # escape vector: recorded and skipped, never
                        # followed (card §18, the same discipline as
                        # _plan_local_folder).
                        continue
                    if not member.isfile():
                        continue
                    # Second-depth guard (spec §2.2): the intake-time
                    # acquisition post-checks are not a trust credential —
                    # re-normalize the member path and assert its prefix
                    # before it becomes a write target.
                    rel = self._normalize_rel(member.name)
                    if rel is None:
                        raise _RepoRejected("SECURITY_REJECTED", "git artifact member path contains a traversal segment")
                    first = rel.split("/", 1)[0]
                    if first in RESERVED_STORAGE_NAMES:
                        raise _RepoRejected("SECURITY_REJECTED", f"reserved git artifact member {member.name!r}")
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    plan.payload[rel] = handle.read()
                    plan.rels.append(rel)
        except tarfile.TarError as exc:
            raise _RepoUnreachable(
                "SOURCE_UNREACHABLE", f"git artifact is not a readable tar ({exc.__class__.__name__})"
            ) from None

    # ------------------------------------------------------------------
    # Key / name / rel rules (spec §2).
    # ------------------------------------------------------------------

    @staticmethod
    def _material_name(repo: Repository) -> str | None:
        raw = (repo.display_name or "").strip()
        if not raw:
            raw = str(repo.id)[:8]
        if not (1 <= len(raw) <= _MAX_MATERIAL_NAME_LEN):
            return None
        if any(ch in raw for ch in ("/", "\\", "\x00")):
            return None
        if raw.startswith("."):
            return None
        if raw in RESERVED_STORAGE_NAMES:
            return None
        return raw

    @staticmethod
    def _normalize_rel(rel: str) -> str | None:
        """Normalize a source-relative path (spec §2.2); None when unsafe.

        Delegates to the ONE shared rule,
        :func:`app.services.intake_security.normalize_rel` (design
        GIT_ACQ_DESIGN_V1.md §D "path safety" / card §18 "never a second,
        contradicting rule set"): backslash -> slash; empty and ``.``
        segments are dropped; ANY ``..`` segment or NUL byte is a traversal
        vector and rejects the whole repo (never popped, unlike the
        permissive storage-key normalizer).  Keeping the shared function as
        the single owner means the acquisition post-checks and the
        materialization readers can never drift into two rule sets.
        """
        return intake_security.normalize_rel(rel)

    def _check_rel_shape(self, plan: _RepoPlan, rel: str | None, display: str) -> str:
        """Validate a source name / first-segment and return the safe ``rel``.

        Raises :class:`_RepoRejected` (SECURITY_REJECTED) when the name is
        unsafe or reserved; returns the normalized ``rel`` on success so the
        caller can use it as a payload key without a ``None``.
        """
        if rel is None:
            raise _RepoRejected("SECURITY_REJECTED", f"unsafe source name {display!r}")
        first = rel.split("/", 1)[0]
        if first in RESERVED_STORAGE_NAMES:
            raise _RepoRejected("SECURITY_REJECTED", f"reserved first-segment name {first!r}")
        return rel

    @staticmethod
    def _host_gate(raw_path: object, *, source_type: str) -> tuple[str, str] | None:
        """Closed-set host-path shape gate via the single authoritative guard.

        ``intake_security.check_host_path`` is the one rule: traversal / NUL /
        sensitive root -> SECURITY_REJECTED; local_folder relative ->
        SOURCE_INVALID.  This service re-invents none of it (spec §4).
        """
        if not isinstance(raw_path, str) or not raw_path:
            return ("SOURCE_INVALID", f"{source_type} source requires a non-empty host path")
        verdict = check_host_path(raw_path, source_type=source_type)
        if verdict.ok:
            return None
        assert verdict.reason_code is not None
        return (verdict.reason_code, verdict.detail or "host path rejected")

    @staticmethod
    def _fail(plan: _RepoPlan, code: str, detail: str) -> None:
        plan.outcome = "FAILED"
        plan.reason_code = code
        plan.detail = detail

    @staticmethod
    def _is_supported_document(name: str) -> bool:
        # Reuses the intake constant (spec §3.3): the whitelist is defined once
        # in app.api.upload and re-imported by project_intake_service; this
        # stage must import the same set, never copy the literals.
        ext = os.path.splitext(str(name))[1].lower()
        return ext in _DOCUMENT_EXTENSIONS

    # ------------------------------------------------------------------
    # Three-phase pipeline (spec §7.1): read -> staging -> publish, with the
    # staging-cleanup invariant on every exit path (§7.2).
    # ------------------------------------------------------------------

    async def _run_pipeline(
        self,
        db: AsyncSession,
        plans: list[_RepoPlan],
        project: Project,
        agent: Agent,
        overwrite: bool,
        current_user: User,
    ) -> MaterializationOut:
        backend = get_storage_backend()
        enabled: list[_RepoPlan] = []
        try:
            # [2] Staging: every content-bearing repo writes its bytes to its
            # exclusive staging key ({agent_id}/.materialize-tmp/{repo_id}/)
            # with unconditional writes (staging keys are repo-private).  A
            # mid-stage failure (disk / permission) fails THAT repo only; the
            # read phase already enforced the per-file and call-wide budgets.
            for plan in plans:
                if not plan.payload:
                    continue
                try:
                    plan.staging_key = plan.staging_key_for(plan.rels[0])  # repo-scoped root
                    for rel in plan.rels:
                        await backend.write_bytes(plan.staging_key_for(rel), plan.payload[rel])
                    plan.staging_enabled = True
                    enabled.append(plan)
                except SecurityError:
                    raise
                except Exception as exc:
                    self._fail(plan, "SOURCE_FAILED", f"staging write failed: {exc.__class__.__name__}")

            # [3] Publish: under the directory-level lock, per target key, with
            # the human-lock + conditional-write second layer (spec §6 / §8).
            for plan in plans:
                if not plan.payload:
                    continue
                await self._publish_repo(db, plan, overwrite=overwrite)
        finally:
            # [4] Cleanup invariant: staging subtrees are deleted on EVERY exit
            # path (success / failure / exception / cancel).  A residual
            # dot-prefixed, repo-scoped key never reads as target-keyspace, so
            # a failure cannot leave a visible half-product — but we still
            # delete so the next run starts clean.  A cleanup failure is
            # recorded, never swallowed (card §9).
            for plan in enabled:
                assert plan.staging_key is not None
                try:
                    await backend.delete_tree(f"{plan.agent_id}/.materialize-tmp/{plan.repo.id}")
                except Exception as exc:
                    logger.error(f"[materialization] staging cleanup failed for {plan.staging_key}: {exc}")

        # Per-repo conflict surface (spec §7.3 / §10.2): the service ALWAYS
        # returns the assembled :class:`MaterializationOut` (SUCCESS, PARTIAL,
        # or FAILED) so the caller's session commits the revisions + audit of
        # every repo that succeeded (§7.3 independent outcomes).  The
        # *transport* maps PARTIAL / FAILED to 409 "with the full repositories[]
        # detail" and never to a 2xx; this service invents no HTTP semantics
        # (backend AGENTS.md: no business in the transport, transport does the
        # mapping).
        outcome = self._build_out(plans, project, agent)
        await self._audit(db, outcome, project, current_user, agent)
        return outcome

    async def _publish_repo(self, db: AsyncSession, plan: _RepoPlan, *, overwrite: bool) -> None:
        """Publish one repo's staged files into its target subtree (spec §6/§8).

        Two phases, so a CONTENT_CONFLICT guarantees 0 NEW writes for the repo
        (spec §8 row 3, not "some written, some not"):

        [a] Probe — read-only: human edit locks + target-key versions for
            every rel.  Any conflict here fails the repo before a single
            write.  (The write phase still re-checks the version; a racing
            writer is caught by the conditional write, spec §6.3 / §8 note.)
        [b] Write — only when the probe passed: converge / conditional writes /
            overwrite, each with its provenance revision row.
        """
        lock_path = f"projects/{plan.repo.project_id}/{plan.material_name}"
        try:
            # The directory-level lock's tenant domain is the AGENT's tenant
            # (spec §6.1 recipe: ``tenant_id=agent.tenant_id``) — the lock
            # guards the target agent's storage subtree, so it lives in that
            # agent's tenant namespace, not the source's.
            async with workspace_locks(plan.agent_id, [lock_path], tenant_id=plan.agent_tenant_id):
                await self._probe_conflicts(db, plan, overwrite=overwrite)
                if plan.outcome != "FAILED":
                    await self._write_published(db, plan, overwrite=overwrite)
        except RuntimeError as exc:  # "Workspace lock busy: ..." (fast-fail)
            self._fail(plan, "LOCK_CONFLICT", f"workspace lock busy for {lock_path}: {exc}")

    async def _probe_conflicts(self, db: AsyncSession, plan: _RepoPlan, *, overwrite: bool) -> None:
        """Read-only pre-write scan of every target key (spec §8 "before the
        call" decision table + §6.2 human locks).  First conflict wins."""
        backend = get_storage_backend()
        target_dir = f"projects/{plan.repo.project_id}/{plan.material_name}"
        for rel in plan.rels:
            data = plan.payload[rel]
            target_key = plan.target_key(rel)
            target_rel = f"{target_dir}/{rel}"

            # Human edit lock (spec §6.2): a human actively editing this file
            # (90s TTL) wins — never silently overwrite their uncommitted work.
            human_lock = await get_active_lock(db, agent_id=plan.agent_id, path=target_rel)
            if human_lock is not None:
                self._fail(plan, "HUMAN_LOCK_CONFLICT", f"human is editing {target_rel}")
                return

            source_hash = content_hash_bytes(data)
            version = await backend.get_version(target_key)
            if version.exists and version.content_hash and version.content_hash == source_hash:
                plan.converged += 1  # probe phase still counts converges (0 writes)
                continue
            if version.exists and not overwrite:
                # Content differs, no overwrite requested (spec §8 row 3): the
                # repo conflicts with 0 NEW writes — this is why the whole
                # probe runs before any write happens.
                self._fail(plan, "CONTENT_CONFLICT", f"existing {target_rel} differs; set overwrite=true to replace")
                return

    async def _write_published(self, db: AsyncSession, plan: _RepoPlan, *, overwrite: bool) -> None:
        """The write phase: only reached when the probe found no conflict.

        Converged keys are skipped (0 writes, no revision, spec §8 row 2); the
        rest are written — new keys via a ``require_absent`` conditional write
        (a concurrent writer racing the possibly-expired lock is still caught
        as a CONTENT_CONFLICT, spec §6.3 / §8), overwrites unconditionally
        inside the lock (the move tool's overwrite semantics).  Each written
        file gets its provenance revision row in the same transaction (§9.1).
        """
        backend = get_storage_backend()
        target_dir = f"projects/{plan.repo.project_id}/{plan.material_name}"
        converged_rels = set()
        # Recompute the converged set against the same rule the probe used so
        # the write phase and the probe can never disagree.
        for rel in plan.rels:
            data = plan.payload[rel]
            source_hash = content_hash_bytes(data)
            version = await backend.get_version(plan.target_key(rel))
            if version.exists and version.content_hash and version.content_hash == source_hash:
                converged_rels.add(rel)
        for rel in plan.rels:
            if rel in converged_rels:
                continue
            data = plan.payload[rel]
            target_key = plan.target_key(rel)
            target_rel = f"{target_dir}/{rel}"
            version = await backend.get_version(target_key)
            before_text = await self._read_text_best_effort(backend, target_key) if version.exists else None
            source_hash = content_hash_bytes(data)
            condition = WriteCondition(require_absent=True) if not overwrite else None
            result = await backend.write_bytes_if_match(target_key, data, condition=condition)
            if not result.ok:
                # A version token shifted between probe and write (a racer the
                # lock did not catch — the documented §9.3.3 TTL window): the
                # conflicting key is reported; earlier writes of this repo are
                # honest counts (§7.3).
                self._fail(plan, "CONTENT_CONFLICT", f"concurrent write conflict on {target_rel}")
                return
            try:
                revision = WorkspaceFileRevision(
                    agent_id=plan.agent_id,
                    scope_type="agent",
                    scope_id=plan.agent_id,
                    path=normalize_workspace_path(target_rel),
                    operation="write",
                    actor_type="system",  # materialization is a system action
                    actor_id=plan.actor_id,  # traceable to the triggering user
                    before_content=before_text,
                    after_content=None,  # large / binary content stays out of the column (§9.3.2)
                    content_hash=source_hash,
                    group_key=f"materialize:{plan.repo.project_id}:{plan.repo.id}:{plan.agent_id}",
                )
                db.add(revision)
                await db.flush()
            except Exception as exc:
                self._fail(plan, "SOURCE_FAILED", f"revision record failed for {target_rel}: {exc.__class__.__name__}")
                return
            plan.written += 1
        plan.outcome = "CONVERGED" if (plan.converged and plan.written == 0) else "SUCCESS"

    @staticmethod
    async def _read_text_best_effort(backend, key: str) -> str | None:
        """Best-effort pre-existing text for the revision's before_content
        (spec §9.1/§9.3.2): binary (NUL-bearing) or unreadable -> None."""
        try:
            raw = await backend.read_bytes(key)
        except Exception:
            return None
        if b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # ------------------------------------------------------------------
    # Result / audit assembly (spec §9 / §10).
    # ------------------------------------------------------------------

    @staticmethod
    def _build_out(plans: list[_RepoPlan], project: Project, agent: Agent) -> MaterializationOut:
        results: list[MaterializationRepoResult] = []
        any_success = False
        any_failed = False
        for plan in plans:
            if plan.outcome in ("SUCCESS", "CONVERGED", "SKIPPED_NO_MATERIAL"):
                any_success = True
            elif plan.outcome == "FAILED":
                any_failed = True
            results.append(
                MaterializationRepoResult(
                    repo_id=plan.repo.id,
                    source_type=plan.source_type,
                    outcome=plan.outcome,
                    reason_code=plan.reason_code,
                    written=plan.written,
                    converged=plan.converged,
                    skipped=plan.skipped,
                )
            )
        if any_failed and any_success:
            outcome = "PARTIAL"
        elif any_failed:
            outcome = "FAILED"
        else:
            outcome = "SUCCESS"
        # retryable = the call can meaningfully be re-invoked: a retryable
        # conflict (lock / human-lock) or a transient source failure caused a
        # FAILED repo.  _RepoUnreachable's family — NOT_FOUND / UNREACHABLE /
        # SOURCE_FAILED staging — is transient by definition (a missing mount
        # or an out storage backend may clear); CONTENT_CONFLICT / SECURITY /
        # SOURCE_INVALID are not.
        retryable = any(
            plan.outcome == "FAILED" and plan.reason_code in ("LOCK_CONFLICT", "HUMAN_LOCK_CONFLICT", "SOURCE_NOT_FOUND", "SOURCE_UNREACHABLE", "SOURCE_FAILED")
            for plan in plans
        )
        limitations = _declared_limitations(plans)
        return MaterializationOut(
            project_id=project.id,
            agent_id=agent.id,
            outcome=outcome,
            retryable=retryable,
            repositories=results,
            limitations=limitations,
        )

    async def _audit(self, db: AsyncSession, out: MaterializationOut, project: Project, current_user: User, agent: Agent) -> None:
        """One AuditLog row per call (spec §9.2), best-effort like intake's.

        A failure to write the audit row is logged, never raised — the
        primary materialization result is preserved (narrow, explained, card
        §9).  The row carries the per-repo result rows + the declared
        limitations so a later inspection answers "which Project / Repository
        did these files come from?" without a new evidence table (M6).
        """
        try:
            row = AuditLog(
                tenant_id=project.tenant_id,
                user_id=current_user.id,
                agent_id=getattr(agent, "id", None),
                action="project_materialization",
                details={
                    "project_id": str(out.project_id),
                    "agent_id": str(out.agent_id),
                    "outcome": out.outcome,
                    "retryable": out.retryable,
                    "limitations": out.limitations,
                    "repo_results": [
                        {
                            "repo_id": str(r.repo_id),
                            "source_type": r.source_type,
                            "outcome": r.outcome,
                            "reason_code": r.reason_code,
                            "written": r.written,
                            "converged": r.converged,
                            "skipped": r.skipped,
                        }
                        for r in out.repositories
                    ],
                },
            )
            db.add(row)
            await db.flush()
        except Exception as exc:
            logger.error(f"[materialization] audit write failed: {exc}")


# ---------------------------------------------------------------------------
# Module-level helpers (private to this stage).
# ---------------------------------------------------------------------------


def _declared_limitations(plans: list[_RepoPlan]) -> list[str]:
    """The spec §9.3 LIMITATIONS, reported when a call actually exhibits them
    so a clean call carries an empty list (never pretend unified evidence)."""
    notes: list[str] = []
    if any(plan.outcome == "SKIPPED_NO_MATERIAL" for plan in plans):
        notes.append("manual source explicitly skipped; no content was invented (M4)")
    if any(plan.source_type == "zip" and plan.outcome in ("SUCCESS", "CONVERGED", "FAILED") for plan in plans):
        notes.append("zip symlink entries materialize as ordinary files; no host symlink is created (§9.3.4)")
    if any(plan.outcome in ("SUCCESS", "CONVERGED") for plan in plans):
        notes.append(
            "provenance = call result + per-call AuditLog + revision rows aggregated by group_key "
            "materialize:{project_id}:{repo_id}:{agent_id} (no unified evidence table, §9.3.1)"
        )
    if any(plan.reason_code == "SOURCE_FAILED" or plan.outcome == "FAILED" for plan in plans):
        notes.append(
            "before_content is best-effort text only; binary content is not persisted in revision columns (§9.3.2)"
        )
    return notes


async def _read_host_file_bytes(path: Path) -> bytes:
    """Read one host source file read-only (spec §3 invariant: sources are
    never modified / moved / deleted by this stage)."""
    async with aiofiles.open(path, "rb") as fh:
        return await fh.read()


# ---------------------------------------------------------------------------
# Test-support helpers (used by the unit suite; the intake service exposes the
# same shape of a shared instance + small constructors for DB-free tests).
# ---------------------------------------------------------------------------


def make_repo(
    source_type: str,
    locator: dict | None = None,
    *,
    display_name: str | None = None,
    verified: bool = True,
    pending_verifier: bool = False,
    tenant_id: uuid.UUID | None = None,
    repo_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> Repository:
    """Build a bare Repository row for service-level tests (no DB)."""
    return Repository(
        id=repo_id or uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        source_type=source_type,
        locator=locator,
        display_name=display_name,
        verified=verified,
        pending_verifier=pending_verifier,
        retry_count=0,
        tenant_id=tenant_id or uuid.uuid4(),
    )


def make_plan(
    *,
    agent_id: uuid.UUID,
    project_id: uuid.UUID,
    material_name: str,
    repo_id: uuid.UUID,
    source_type: str = "local_folder",
) -> _RepoPlan:
    """Build a bare :class:`_RepoPlan` for the key-formula unit tests
    (spec §2.3 / §2.5) without running the full pipeline."""
    return _RepoPlan(
        repo=make_repo(source_type, repo_id=repo_id, project_id=project_id),
        source_type=source_type,
        agent_id=agent_id,
        actor_id=uuid.uuid4(),
        material_name=material_name,
    )


class _MemoryStorage:
    """In-memory storage backend stand-in for the materialization service.

    Mirrors the subset of the storage facade the service uses: ``exists`` /
    ``stat`` / ``read_bytes`` / ``write_bytes`` / ``get_version`` /
    ``write_bytes_if_match`` / ``delete_tree``.  Keys are stored exactly as
    passed (no normalization) so the tests can assert the *actual* target /
    staging key strings, and ``content_hash`` is populated on every write so
    the converge / conflict decision table is exercised for real.
    """

    def __init__(self, files: dict[str, bytes] | None = None, directories: set[str] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.directories: set[str] = set(directories or set())

    # -- reads --

    async def exists(self, key: str) -> bool:
        return key in self.files or key in self.directories

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def is_dir(self, key: str) -> bool:
        return key in self.directories

    async def stat(self, key: str):
        if key in self.files:
            return _MemoryEntry(key, b"file", len(self.files[key]), content_hash_bytes(self.files[key]))
        if key in self.directories:
            return _MemoryEntry(key, b"dir", 0, "")
        raise FileNotFoundError(key)

    async def read_bytes(self, key: str) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    async def get_version(self, key: str) -> StorageVersion:
        if key in self.files:
            return StorageVersion(
                key=key,
                exists=True,
                is_dir=False,
                size=len(self.files[key]),
                content_hash=content_hash_bytes(self.files[key]),
            )
        if key in self.directories:
            return StorageVersion(key=key, exists=True, is_dir=True)
        return StorageVersion(key=key, exists=False, is_dir=False)

    # -- writes --

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = bytes(data)

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.directories.discard(key)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for k in [k for k in self.files if k.startswith(prefix) or k == key]:
            self.files.pop(k, None)
        for k in [k for k in list(self.directories) if k.startswith(prefix) or k == key]:
            self.directories.discard(k)

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ):
        from app.services.storage_runtime.base import ConditionalWriteResult

        current = await self.get_version(key)
        if condition and condition.require_absent and current.exists:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        self.files[key] = bytes(data)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


class _MemoryEntry:
    def __init__(self, key: str, kind: bytes, size: int, content_hash: str) -> None:
        self.name = key.rsplit("/", 1)[-1]
        self.key = key
        self.is_dir = kind == b"dir"
        self.size = size
        self.content_hash = content_hash


#: A single shared service instance (the intake service uses the same shape).
project_materialization_service = ProjectMaterializationService()

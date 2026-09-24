"""Git Source Acquisition service (Phase 2B-4, card t_4874c3e7).

Design: ``docs/GIT_ACQ_DESIGN_V1.md`` (card t_82ac3524).  This is the ONE new
service module the design ruled (§B "What is net-new"): it turns a registered
git source (github / gitlab / local_git) into a verified, byte-bounded
artifact a later Materialization call can read — without re-cloning, without
running any project code, and without ever triggering a downstream Agent.

Acquisition is an INDEPENDENT stage between Intake and Materialization:

    Repository (registered, unverified)
        ->  GitAcquisitionService.acquire()   <- THIS module
        ->  verified artifact + resolved revision
        ->  ProjectMaterializationService (reads the ONE tar, no re-clone)

The hard rules (design §C / card §10/§17): the acquisition writes ONLY into
an isolated, agent-scoped staging area ``{agent_id}/.git-acq/{repo_id}/`` and
publishes a single bounded tar through the storage facade; it NEVER writes
into the agent's workspace / project dir, NEVER executes project code, hooks,
installers or submodules, and NEVER starts an Agent / Run / prompt.

Everything security-relevant is REUSED, not re-invented (design §D "Reuse
strategy summary" — the table the builder must honor):

- URL / SSRF         -> ``intake_security.git_url_detail`` + ``is_unsafe_host``
                        (https-only scheme gate; private/loopback/metadata
                        host -> ACQ_SECURITY_REJECTED, never retried)
- local path shape   -> ``intake_security.check_host_path``
- member-path rel    -> ``intake_security.normalize_rel`` (the ONE shared
                        traversal rule, reused by materialization too)
- subprocess         -> ``asyncio.create_subprocess_exec`` argument lists
                        (never ``shell=True``, never string concat) + the
                        two-stage process-group reap recipe copied from
                        ``sandbox/local/subprocess_backend.py:221-245``
- timeout            -> ``asyncio.wait_for`` on the bounded wall
                        ``config.GIT_ACQUISITION_MAX_SECONDS``
- credentials        -> ``core.security.decrypt_data`` on an
                        ``agent_credentials`` row (platform github/gitlab,
                        credential_type api_key), decrypted at the use
                        boundary and injected into the git child's env ONLY —
                        the token value never enters a locator, a model
                        field, a log line, or an audit row
- staging / artifact -> ``project_intake_service.get_storage_backend`` facade;
                        the "delete the staging tree on EVERY exit" invariant
                        from ``project_materialization_service``

Closed result codes: this service owns the ACQ_* set defined in
``schemas/project_intake.py`` (distinct from the Intake 6-code set).  Only
``ACQ_SOURCE_UNREACHABLE`` and ``ACQ_TIMEOUT`` are retryable, bounded by
``repositories.retry_count`` + the intake ``MAX_RETRIES`` budget.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data
from app.dao.agent_credential_dao import agent_credential_dao
from app.models.audit import AuditLog
from app.models.project import Project, Repository
from app.models.user import User
from app.schemas.project_intake import (
    ACQ_AUTH_FAILED,
    ACQ_OK,
    ACQ_REF_NOT_FOUND,
    ACQ_RESULT_CODES,
    ACQ_SECURITY_REJECTED,
    ACQ_SIZE_LIMIT,
    ACQ_SOURCE_INVALID,
    ACQ_SOURCE_NOT_FOUND,
    ACQ_SOURCE_UNREACHABLE,
    ACQ_TIMEOUT,
    SUBMODULES_UNSUPPORTED,
    acq_code_is_retryable,
)
from app.services import intake_security
from app.services.intake_security import SecurityError
from app.services.project_intake_service import MAX_RETRIES, get_storage_backend
from app.services.project_materialization_service import (
    GIT_SOURCE_TYPES,
    RESERVED_STORAGE_NAMES,
)
from app.services.storage_runtime.utils import normalize_storage_key

if TYPE_CHECKING:
    from app.models.agent import Agent

__all__ = [
    "AcquisitionError",
    "AcquisitionOutcome",
    "AcquisitionSecurity",
    "GitAcquisitionService",
    "git_acquisition_service",
]

#: Bounded capture for the git child's stderr (the
#: ``MAX_EXEC_STDERR_CAPTURE_BYTES``-style rule, agent_tools.py:12295-12309):
#: only the tail of a large output is kept, and it is sanitized before it may
#: ever be logged or returned (a failed clone's stderr names the URL, so a raw
#: echo would leak any userinfo; the sanitizer strips it).
MAX_GIT_STDERR_CAPTURE_BYTES = 4000

#: The two-stage reap grace window, mirroring the sandbox recipe (SIGTERM ->
#: grace -> SIGKILL, sandbox/local/subprocess_backend.py:221-245).
_PROCESS_TERMINATION_GRACE_SECONDS = 10

#: The ref-shape gates (design §B.1).  branch/tag -> one pattern, a commit
#: -> the 40-hex-SHA pattern.  Everything else (injection vectors, a bare
#: integer, a too-long name) is rejected before ANY process is spawned.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

#: The one storage name the acquisition artifact publishes (kept out of the
#: reserved set so it can never collide with a TempWorkspace default name).
_ARTIFACT_NAME = "source.tar"


class AcquisitionError(Exception):
    """A service-level acquisition failure carrying a closed ACQ code.

    The API maps ``code`` to the transport body; the ``detail`` is always
    bounded and secret-free (a security-rejection detail names the CLASS of
    the problem — e.g. "URL scheme 'file' is not allowed" — never the token
    or full URL userinfo, per the credential-not-in-locator invariant).
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class AcquisitionSecurity(SecurityError):
    """A hard tenant-isolation / scope violation at the entry gate.

    Subclasses the security module's :class:`SecurityError` so the
    transport layer keeps one narrow 403 mapping for every isolation
    finding, exactly mirroring the materialization handler's
    ``MaterializationSecurity -> 403`` / ``MaterializationNotReady -> 409``
    split (design §C.5: the M9 fourth gate is a 403-class finding, not a
    closed 409 code).  Raised by ``_entry_gates`` before ANY I/O; the
    sibling ``TenantScopeViolation`` (from the re-asserted
    ``verify_tenant_scope``) is caught alongside it.
    """


@dataclass
class AcquisitionOutcome:
    """The assembled result of one acquire call.

    ``state`` is the transport-facing closed set (acquired / pending /
    failed); ``code`` is the closed ACQ_* set (None until an attempt ran);
    ``retryable`` mirrors whether re-invocation can change the outcome;
    ``message`` is the bounded, secret-free detail.  The metadata fields are
    stamped only on ACQ_OK.
    """

    state: str
    code: str | None = None
    retryable: bool = False
    message: str = ""
    requested_ref: str | None = None
    resolved_rev: str | None = None
    provider: str | None = None
    artifact_key: str | None = None
    acquired_at: datetime | None = None
    #: Internal: the agent-scoped staging subtree to clean on every exit.
    _staging_key: str | None = field(default=None, repr=False)
    #: Internal: whether a transient failure still has retry budget left.
    _retries_remaining: int | None = field(default=None, repr=False)

    def is_ok(self) -> bool:
        return self.state == "acquired"


def _sanitize_git_stderr(stderr: bytes | None) -> str:
    """Return a bounded, secret-free tail of a git child's stderr.

    A failed ``git clone``'s stderr contains the full URL (and any
    ``user:pass@`` the attacker or config placed there).  Before it may be
    logged or returned to a caller the output is capped to the bounded tail
    and the entire ``user:pass`` userinfo is replaced by ``<redacted>`` so
    the secret half is gone.  This is the "no token / raw URL userinfo in
    logs or audit" rule (design §A.3).
    """
    if not stderr:
        return ""
    text = stderr.decode("utf-8", errors="replace")
    if len(text) > MAX_GIT_STDERR_CAPTURE_BYTES:
        text = "..." + text[-MAX_GIT_STDERR_CAPTURE_BYTES:]
    # Redact userinfo in a URL: user:pass@ -> user@ (drop the secret half).
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//<redacted>@", text)
    return text.strip()


class GitAcquisitionService:
    """The Git acquisition owner.  API handlers call only ``acquire`` and
    ``status``; everything else is internal."""

    # ------------------------------------------------------------------
    # Public entry points (design §B.1 / §C.2).
    # ------------------------------------------------------------------

    async def acquire(
        self,
        db: AsyncSession,
        *,
        project: Project,
        repo: Repository,
        agent: Agent,
        current_user: User,
    ) -> AcquisitionOutcome:
        """Acquire one git source into a verified, bounded artifact.

        Pipeline (design §B.1, all bounded and fail-closed):

          1. entry gates (tenant scope + source_type) ;
          2. URL / ref / local-path validation (pure, no I/O) ;
          3. credential resolve (process-only, injected into the child env) ;
          4. the bounded git run (clone / fetch, two-stage reap on timeout) ;
          5. acquisition-area post-checks (reserved names, symlinks,
             submodules, traversal) ;
          6. artifact publish (single bounded tar) + staging cleanup
             (``delete_tree`` on EVERY exit path) ;
          7. record the locator metadata + verified mark + audit row.

        Only ``ACQ_SOURCE_UNREACHABLE`` / ``ACQ_TIMEOUT`` are retryable, and
        only while ``repositories.retry_count`` is under the intake budget.
        """
        settings = get_settings()
        budget = int(settings.GIT_ACQUISITION_MAX_SECONDS)
        artifact_key = self._artifact_key(agent.id, repo.id)

        outcome = AcquisitionOutcome(state="pending")
        work_dir: Path | None = None  # assigned below; the finally must not NameError
        remote_url: str | None = None
        local_dir: str | None = None
        try:
            # [0] Entry + shape gates (fail-closed, before ANY I/O / process):
            # a tenant-isolation finding (AcquisitionSecurity / the re-asserted
            # verify_tenant_scope) is a 403-class exception and propagates to
            # the transport as-is; a closed ACQ code (a non-git source_type,
            # a rejected ref) becomes a 409-class outcome below.  Both gate
            # steps run on CLIENT-REACHABLE inputs (repo_id / locator), so
            # they must never surface as a 500.
            self._entry_gates(project, repo, agent, current_user)
            requested_ref, provider, requested_sha = self._resolve_ref_and_provider(repo, agent)
            outcome.requested_ref = requested_ref
            outcome.provider = provider
            # [1] Shape validation (pure, no I/O, no process yet): remote URL
            # vs local git dir, plus the ref.  Exactly one of remote_url /
            # local_dir is set, chosen by source_type (typed so neither is
            # read unbound on the other branch).
            if repo.source_type == "local_git":
                local_dir = await self._validate_local_git(repo, agent)
            else:
                remote_url = self._validate_remote_url(repo)
            source = local_dir if local_dir is not None else remote_url
            if source is None:
                raise AcquisitionError(ACQ_SOURCE_INVALID, "git source resolved to neither a URL nor a local dir")
            # [2] Credential resolve (process-only; None when public/absent).
            # local_git carries no remote auth, so it passes a None URL.
            token = await self._resolve_credential(agent, repo, remote_url)
            # [3] The bounded git run + ref verify, into the staging area.
            # The work dir is a UNIQUE host dir (agent-scoped so two tenants
            # can never share a git working dir, + a per-call nonce so
            # concurrent acquires of the same repo cannot interleave).
            work_dir = Path(os.environ.get("TEMP", "/tmp")) / f"git-acq-{agent.id}-{repo.id}-{uuid.uuid4().hex[:8]}"
            resolved_rev = await self._run_git_acquire(
                repo,
                source,
                requested_ref,
                requested_sha,
                work_dir,
                token,
                budget,
            )
            outcome.resolved_rev = resolved_rev
            # [4] Acquisition-area post-checks before any publish.
            self._post_check_tree(work_dir)
            # [5] Artifact publish (one bounded tar) + staging cleanup.
            await self._publish_artifact(work_dir, agent.id, repo.id, artifact_key)
            # [6] Record locator metadata + verified mark + audit (best-effort).
            await self._record_success(db, repo, agent, artifact_key, resolved_rev, requested_ref, provider)
            outcome.state = "acquired"
            outcome.code = ACQ_OK
            outcome.artifact_key = artifact_key
            outcome.acquired_at = datetime.now(UTC)
            outcome.message = "acquired"
            return outcome
        except AcquisitionError as exc:
            # Map the closed code to the transport state + retry rule.
            return self._finalize_failure(outcome, exc, repo, agent)
        finally:
            # The staging-cleanup invariant (design §A.4 / §D): delete the
            # host work dir on EVERY exit path and, on a failure, the partial
            # staging storage subtree too — a residual dot-prefixed, agent-
            # scoped key must never read as a target-keyspace half-product.
            await self._cleanup(work_dir=work_dir, outcome=outcome, agent_id=agent.id, repo_id=repo.id)

    async def status(self, db: AsyncSession, *, repo: Repository, agent: Agent) -> AcquisitionOutcome:
        """Reconstruct the stored acquisition result (design §C.2 GET).

        Reads the ONE locator JSON (``acq_artifact`` / ``acq_result`` /
        ``resolved_rev`` ...) and the repo's verified / pending_verifier /
        retry_count marks; it performs no git work.  A repo with no stored
        result is a not-yet-attempted ``pending`` (retryable, no code).
        """
        loc = repo.locator or {}
        code = loc.get("acq_result")
        # F1 (audit t_31f91B3A): ``acq_result`` is INTAKE USER INPUT for git
        # sources (the locator is a free-form dict; the create-time credential
        # scan rejects only credential-shaped values, so ``"TOTALLY_BOGUS"``
        # or ``""`` can be persisted).  The closed-set guard is meant to
        # catch a programming error, but this value crosses the status
        # boundary as trusted user data — so it is validated here against
        # ``ACQ_RESULT_CODES`` and an out-of-set / empty / non-string value
        # degrades to a safe read (``code=None`` -> the not-yet-attempted
        # pending reconstruction below), NEVER a raised ValueError.  The
        # GET is a client-reachable route that must never surface a 500
        # (the acquire() docstring contract, design §C.2).
        if not isinstance(code, str) or code not in ACQ_RESULT_CODES:
            code = None
        artifact_key = loc.get("acq_artifact")
        resolved_rev = loc.get("resolved_rev")
        requested_ref = loc.get("requested_ref")
        provider = loc.get("provider")
        acquired_at = loc.get("acquired_at")
        if isinstance(acquired_at, str):
            try:
                acquired_at = datetime.fromisoformat(acquired_at)
            except ValueError:
                acquired_at = None
        message = loc.get("acq_detail") or ""
        if code is None and not artifact_key:
            return AcquisitionOutcome(
                state="pending",
                requested_ref=requested_ref,
                provider=provider,
                retryable=True,
                message="not yet acquired",
                _retries_remaining=self._retries_remaining(repo),
            )
        ok = bool(artifact_key) and repo.verified and not repo.pending_verifier
        if ok:
            state, retryable = "acquired", False
            code = code or ACQ_OK
        else:
            retryable = bool(code) and acq_code_is_retryable(code) and (int(repo.retry_count or 0) < MAX_RETRIES)
            # A transient outcome still inside the bounded retry budget is a
            # PENDING result (the POST's closed state, "re-invocation can
            # still reach an outcome"), not a terminal failure — the GET must
            # reconstruct the same state set, not report a retryable hold as
            # terminal (design §C.2).
            state = "pending" if retryable else "failed"
        return AcquisitionOutcome(
            state=state,
            code=code or None,
            retryable=retryable,
            message=message,
            requested_ref=requested_ref,
            resolved_rev=resolved_rev,
            provider=provider,
            artifact_key=artifact_key,
            acquired_at=acquired_at,
        )

    # ------------------------------------------------------------------
    # Entry gates (fail-closed, before ANY I/O — design §C.5 / card §19).
    # ------------------------------------------------------------------

    def _entry_gates(self, project: Project, repo: Repository, agent: Agent, current_user: User) -> None:
        """Tenant scope + source_type guard, run before any I/O.

        - ``verify_tenant_scope`` re-asserts the record's tenant equals the
          acting context (a null context / cross-tenant fetch fails closed);
        - a non-git source_type is ACQ_SOURCE_INVALID (this endpoint exists
          for git sources; a manual / document row reaching it is a
          programming error, not a transient condition);
        - the M9 fourth gate: the target agent's tenant must equal the
          project's tenant (the isolation gap ``check_agent_access`` alone
          leaves open for background callers).
        """
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        agent_tenant = getattr(agent, "tenant_id", None)
        if agent_tenant is None or project.tenant_id is None:
            # A tenant-isolation finding (not a source problem): the caller
            # may not run acquisition against these tenants.  Maps to 403 via
            # the transport's ``AcquisitionSecurity`` handler, the same split
            # the materialization gate uses (M9 fourth gate, design §C.5).
            raise AcquisitionSecurity("acquisition has no tenant context; refusing to run")
        if agent_tenant != project.tenant_id:
            raise AcquisitionSecurity("target agent tenant does not match the project tenant")
        if repo.source_type not in GIT_SOURCE_TYPES:
            # A programming error, not an isolation finding: this endpoint
            # exists for git sources only, so a manual / document / zip row
            # reaching it is the closed ACQ code (409-class, never retried).
            raise AcquisitionError(ACQ_SOURCE_INVALID, f"source_type {repo.source_type!r} is not a git source")

    def _retries_remaining(self, repo: Repository) -> int | None:
        return max(0, MAX_RETRIES - int(repo.retry_count or 0))

    def _finalize_failure(self, outcome: AcquisitionOutcome, exc: AcquisitionError, repo: Repository, agent: Agent) -> AcquisitionOutcome:
        """Map a closed ACQ code to the transport state + retry budget.

        Permanent codes (AUTH_FAILED / SECURITY_REJECTED / REF_NOT_FOUND /
        SOURCE_NOT_FOUND / SOURCE_INVALID / SIZE_LIMIT / SUBMODULES_
        UNSUPPORTED) are ``failed`` and never retried.  Transient codes
        (SOURCE_UNREACHABLE / TIMEOUT) stay ``pending`` while the repo's
        retry budget remains, and escalate to a terminal ``failed`` once it
        is exhausted (bounded, card §21).  The code + a bounded, secret-free
        detail are written to the locator so ``status`` can reconstruct the
        terminal outcome; the transient path ALSO sets the existing
        pending-verifier mark so the intake dispatch and the materialization
        gate keep failing this repo closed until it is re-acquired.
        """
        outcome.code = exc.code
        outcome.message = exc.detail
        remaining = self._retries_remaining(repo)
        transient = acq_code_is_retryable(exc.code)
        if transient and remaining and remaining > 0:
            # Transient, within budget: stay pending, record the hold, bump
            # the counter so the NEXT call is the last one (bounded by the
            # intake MAX_RETRIES budget — card §21 / design §D "retries").
            outcome.state = "pending"
            outcome.retryable = True
            outcome._retries_remaining = remaining - 1
            self._record_failure(repo, exc.code, exc.detail, transient=True)
            return outcome
        # Permanent, or transient-with-budget-exhausted: terminal failure.
        outcome.state = "failed"
        outcome.retryable = False
        self._record_failure(repo, exc.code, exc.detail, transient=False)
        return outcome

    def _record_failure(self, repo: Repository, code: str, detail: str, *, transient: bool) -> None:
        """Write the terminal/pending result into the locator + repo marks.

        No schema change (design §C.4): the result rides in
        ``repositories.locator`` JSON (``acq_result`` / ``acq_detail``),
        bounded and secret-free — never a token, never raw URL userinfo.
        A permanent failure clears the pending mark; a transient hold keeps
        it set so the sibling gates stay fail-closed (design §C.3: the
        "enum first, capability later" marker is unchanged).
        """
        loc = dict(repo.locator or {})
        loc["acq_result"] = code
        loc["acq_detail"] = detail[:300] if detail else code
        repo.locator = loc
        if not transient:
            repo.pending_verifier = False
        repo.verified = False
        # In-memory only: the API caller's session commits (flush there);
        # the bump + mark are the same session's writes, no autocommit.
        if transient:
            repo.pending_verifier = True
            repo.retry_count = int(repo.retry_count or 0) + 1
        repo.updated_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Ref / provider resolution (design §6: never assume "main").
    # ------------------------------------------------------------------

    def _resolve_ref_and_provider(self, repo: Repository, agent: Agent) -> tuple[str | None, str, bool]:
        """Return ``(requested_ref, provider, is_sha)`` from the locator.

        The provider is the source_type's host label (github / gitlab /
        local_git); ``requested_ref`` is the locator's branch/tag/commit when
        present and None when the source declares only a URL (the remote's
        own default branch is then used via ``git ls-remote --symref``).  A
        full 40-hex value is a commit pin; a shorter 7-39 hex value is
        treated as a commit prefix (fetched, then verified against the
        resolved full SHA, design §13).  Everything else is a branch/tag
        name — and it must pass the ref shape-gate (card §7): no injection
        vectors, no bare integers, no over-long names, no leading ``-``
        (a git argv item starting with a dash is parsed by git as a flag
        even in the value position — the argument-list isolation does not
        protect against an option-shaped value).  A ref that passes
        NEITHER gate is rejected ACQ_SECURITY_REJECTED before ANY process is
        spawned (the git child never sees an unvalidated ref).
        """
        loc = repo.locator or {}
        ref = loc.get("branch") or loc.get("tag") or loc.get("commit") or loc.get("ref")
        provider = repo.source_type
        if not isinstance(ref, str) or not ref:
            return None, provider, False
        is_sha = bool(_SHA_RE.fullmatch(ref))
        # Branch/tag names: char-class safe AND no leading dash (a git argv
        # item starting with a dash is parsed as a flag even in value
        # position — the argument-list isolation does not protect against an
        # option-shaped value) AND not a bare integer (card §7 "no bare
        # integers": digit-only names are not branch/tag names worth
        # passing to git; a commit pin must be 7-40 hex via the SHA gate).
        is_ref_name = bool(_REF_RE.fullmatch(ref)) and not ref.startswith("-") and not ref.isdigit()
        if not is_sha and not is_ref_name:
            raise AcquisitionError(
                ACQ_SECURITY_REJECTED,
                "requested ref fails the shape gate (branch/tag/commit); refusing to pass it to git",
            )
        return ref, provider, is_sha

    # ------------------------------------------------------------------
    # Shape validation (pure, no I/O, no process yet — design §B.1 step 1).
    # ------------------------------------------------------------------

    def _validate_remote_url(self, repo: Repository) -> str:
        """Validate a github / gitlab URL through the shared security gate.

        ``intake_security.git_url_detail`` is the ONE rule (https-only
        scheme, no userinfo, host not private/loopback/metadata).  Any
        rejection is ACQ_SECURITY_REJECTED (permanent, never retried — card
        §21) with a bounded, secret-free detail (the CLASS of the problem).
        """
        url = (repo.locator or {}).get("url")
        if not isinstance(url, str) or not url:
            raise AcquisitionError(ACQ_SOURCE_INVALID, f"{repo.source_type} source requires a non-empty locator.url")
        detail = intake_security.git_url_detail(url)
        if detail is not None:
            raise AcquisitionError(ACQ_SECURITY_REJECTED, detail)
        return url

    async def _validate_local_git(self, repo: Repository, agent: Agent) -> str:
        """Validate a local_git host path is a real, safe git directory.

        ``check_host_path`` is the authoritative path-shape rule (traversal /
        NUL / sensitive root -> SECURITY_REJECTED; a relative path ->
        SOURCE_INVALID).  A path that exists but is not a git repository
        (no readable ``.git``) is SOURCE_INVALID — a plain directory must not
        be mistaken for a git source (card §8 "must really verify").
        """
        raw = (repo.locator or {}).get("path")
        if not isinstance(raw, str) or not raw:
            raise AcquisitionError(ACQ_SOURCE_INVALID, "local_git source requires a non-empty locator.path")
        verdict = intake_security.check_host_path(raw, source_type="local_folder")
        if not verdict.ok:
            assert verdict.reason_code is not None
            if verdict.reason_code == "SECURITY_REJECTED":
                raise AcquisitionError(ACQ_SECURITY_REJECTED, verdict.detail or "local_git path rejected")
            raise AcquisitionError(ACQ_SOURCE_INVALID, verdict.detail or "local_git path is invalid")
        root = Path(os.path.abspath(raw))
        if not root.exists() or not root.is_dir():
            raise AcquisitionError(ACQ_SOURCE_NOT_FOUND, f"local git path {root.name!r} is not a directory")
        # A git dir has a readable ``.git`` (a dir in a worktree, a file in a
        # shallow / bare-ish layout).  Without it the path is not a git
        # repository: the local_git vs local_folder boundary (card §9).
        if not (root / ".git").exists():
            raise AcquisitionError(ACQ_SOURCE_INVALID, f"local git path {root.name!r} is not a git repository")
        return str(root)

    # ------------------------------------------------------------------
    # Credential resolve (design §A.3: process-only, env-injected).
    # ------------------------------------------------------------------

    async def _resolve_credential(self, agent: Agent, repo: Repository, url: str | None) -> str | None:
        """Return a decrypted token for the source host, or None (public).

        Resolution is ``agent_credentials`` scoped to the target materialization
        agent: pick the active ``api_key`` row whose ``platform`` matches the
        source host, decrypt it with the app SECRET_KEY, and return it ONLY in
        the process.  The token is NEVER written to the locator, a model field,
        a log, or an audit row.  A missing token for a public source is fine
        (return None); a present-but-undecryptable credential is ACQ_AUTH_
        FAILED (permanent — a token cannot "clear" like a network blip).
        """
        if url is None:
            return None  # local_git: no remote auth
        host = (urlparse(url).hostname or "").lower()
        try:
            rows = await agent_credential_dao.list_by_agent(agent.id)
        except Exception as exc:  # noqa: BLE001 - credential store unreachable; any DAO error is a "no token" miss, not a security finding
            logger.warning(f"[git_acq] credential lookup failed for {agent.id}: {exc.__class__.__name__}")
            return None
        for row in rows:
            if row.status != "active":
                continue
            if row.credential_type != "api_key":
                continue
            platform = (row.platform or "").lower()
            # The credential's platform must be a dot-delimited host suffix
            # (github.com matches "github"; "mygithub.example.com" must NOT
            # match a "github" credential) — the token is only ever injected
            # for a host the credential was stored against.
            if not (platform and (host == platform or host.endswith("." + platform))):
                continue
            # The encrypted payload lives in the model's cookies_json column
            # (credential_type="api_key" rows store the token here; no
            # schema change — design §A.3 "reuse the model").
            cipher = row.cookies_json
            if not cipher:
                continue
            try:
                return decrypt_data(cipher, get_settings().SECRET_KEY)
            except ValueError as exc:
                raise AcquisitionError(ACQ_AUTH_FAILED, "stored credential could not be decrypted") from exc
        return None

    # ------------------------------------------------------------------
    # The bounded git run (design §B.1 step 3 / §A.2 two-stage reap).
    # ------------------------------------------------------------------

    async def _run_git_acquire(
        self,
        repo: Repository,
        source: str,
        requested_ref: str | None,
        requested_sha: bool,
        work_dir: Path,
        token: str | None,
        budget: int,
    ) -> str:
        """Clone / fetch into ``work_dir`` and VERIFY the resolved revision.

        Argument-list subprocess only (never shell=True / string concat) and
        a bounded TOTAL wall of ``budget`` seconds shared by every git call
        in this acquire (card §15: a clone may not run unbounded — the
        config key bounds the whole call, not a single command).  A
        two-stage process-group reap fires on any timeout.  "clone
        succeeded" is NOT success (design §13/§14): the checkout is
        cross-checked against the requested ref / SHA, and a remote source
        with no ref of its own resolves the remote's default branch via
        ``git ls-remote --symref`` (never hardcoded ``main``).
        """
        env = self._git_env(token, source if repo.source_type != "local_git" else None)
        deadline = time.monotonic() + budget
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            if repo.source_type == "local_git":
                # Local: full clone from the validated dir (no network).  A
                # FULL clone (not --depth 1) so ANY requested branch/tag /
                # commit SHA is present and the checkout below is verifiable
                # (card §13 "clone succeeded is NOT success").
                await self._git(["clone", "--no-recurse-submodules", source, "."], work_dir, env, deadline)
                if requested_ref:
                    try:
                        await self._git(["checkout", requested_ref], work_dir, env, deadline)
                    except _GitFailure:
                        # A ref absent from a FULL local clone is a missing
                        # ref, not a malformed source (design §13): map it to
                        # the closed ACQ_REF_NOT_FOUND, never the generic
                        # classification's SOURCE_INVALID.
                        raise AcquisitionError(
                            ACQ_REF_NOT_FOUND, f"requested ref {requested_ref!r} is not in the local repository"
                        ) from None
            elif requested_ref and requested_sha:
                # Commit pin: a shallow clone of the default branch may not
                # contain the SHA, so fetch it explicitly then verify.
                ref = requested_ref
                await self._git(
                    ["clone", "--depth", "1", "--no-recurse-submodules", source, "."],
                    work_dir,
                    env,
                    deadline,
                )
                try:
                    await self._git(
                        ["fetch", "--depth", "1", "origin", ref], work_dir, env, deadline
                    )
                except _GitFailure:
                    # The depth-limited SHA fetch failed: fall back to a
                    # full (unshallow) fetch of the same SHA — documented
                    # in the design ("big repos pay once").  The deadline
                    # still bounds the total wall (card §15).
                    await self._git(["fetch", "origin", ref], work_dir, env, deadline)
                await self._git(["checkout", ref], work_dir, env, deadline)
            elif requested_ref:
                # Branch / tag: a shallow clone of the default branch may
                # not contain the ref, so fetch it explicitly (works for
                # tags too, where ``clone --branch`` is finicky) then
                # check it out — the cross-check below proves the
                # checkout actually landed on that ref.
                ref = requested_ref
                await self._git(
                    ["clone", "--depth", "1", "--no-recurse-submodules", source, "."],
                    work_dir,
                    env,
                    deadline,
                )
                await self._git(["fetch", "--depth", "1", "origin", ref], work_dir, env, deadline)
                await self._git(["checkout", ref], work_dir, env, deadline)
            else:
                # No ref: use the remote's own default branch (ls-remote
                # --symref HEAD -> refs/heads/<default>), never "main".
                default = await self._default_branch(source, env, deadline)
                await self._git(
                    ["clone", "--depth", "1", "--no-recurse-submodules", source, "."],
                    work_dir,
                    env,
                    deadline,
                )
                if default:
                    await self._git(["checkout", default], work_dir, env, deadline)
            rev = (
                await self._git(
                    ["rev-parse", "HEAD"],
                    work_dir,
                    env,
                    deadline,
                )
            ).strip()
            if not rev:
                raise AcquisitionError(ACQ_SOURCE_INVALID, "could not resolve the checkout revision")
            if requested_sha and requested_ref:
                # A pin (full 40-hex or 7-39 hex prefix) must be the revision
                # actually checked out — "clone succeeded" is NOT success.
                if not rev.lower().startswith(requested_ref.lower()):
                    raise AcquisitionError(
                        ACQ_REF_NOT_FOUND,
                        f"checkout revision {rev[:12]} does not match the requested commit {requested_ref[:12]}",
                    )
            elif requested_ref:
                # Cross-check: the revision the ref resolves to must be the
                # one we checked out (design §14 "实际验证 checkout").
                resolved = (
                    await self._git(
                        ["rev-parse", f"{requested_ref}^{{commit}}"],
                        work_dir,
                        env,
                        deadline,
                    )
                ).strip()
                if resolved and resolved.lower() != rev.lower():
                    raise AcquisitionError(
                        ACQ_REF_NOT_FOUND,
                        f"checkout revision {rev[:12]} does not match requested ref {requested_ref!r} ({resolved[:12]})",
                    )
            return rev
        except AcquisitionError:
            raise
        except TimeoutError:
            raise AcquisitionError(ACQ_TIMEOUT, f"acquisition exceeded the {budget}s total budget") from None
        except _GitFailure as exc:
            raise self._classify_git_failure(exc) from None

    async def _default_branch(self, url: str, env: dict[str, str], deadline: float) -> str | None:
        """Read the remote's own default branch via ``ls-remote --symref``.

        The line ``ref: refs/heads/<name>\\tHEAD`` carries the remote's
        default; when absent (empty repo / unusual remote) return None and
        let the plain clone use its HEAD.  Never a hardcoded ``main``.

        F2 (audit t_31f91B3A) — two independent fixes, both required:

        1. argv order: git's grammar is ``ls-remote [--symref] <repository>
           [<refs>]``, so the URL is the repository position and ``HEAD``
           the ref filter.  The previous inversion (``HEAD`` first, URL
           last) made git parse repository="HEAD" and ALWAYS exit 128,
           which ``allow_failure=True`` swallowed — so this read was dead
           code that "failed" silently on every call.
        2. parse: the symref ref line is tab-delimited
           (``refs/heads/<name>\\tHEAD``); the branch name is the segment
           BEFORE the tab.  The old ``split("refs/heads/",1)[-1].strip()``
           returned ``<name>\\tHEAD`` (the mid-string tab survives
           ``strip()``), which fails ``_REF_RE`` and would make the later
           ``git checkout`` fail with "invalid refname".
        """
        out = await self._git(["ls-remote", "--symref", url, "HEAD"], None, env, deadline, allow_failure=True)
        for line in out.splitlines():
            if not line.startswith("ref:"):
                continue
            # Symref line: ``ref: <full-ref>\tHEAD`` — the ref name is the
            # tab-delimited first field; strip the ``refs/heads/`` prefix.
            ref_field = line[len("ref:"):].split("\t", 1)[0].strip()
            name = ref_field.removeprefix("refs/heads/")
            if not name:
                continue
            if not _REF_RE.fullmatch(name):
                # A ref-name outside the shape gate is not a branch we may
                # hand to git: degrade to the clone's own HEAD (never a
                # hardcoded "main").
                return None
            return name
        return None

    def _git_env(self, token: str | None, url: str | None) -> dict[str, str]:
        """Build the git child's env: inherit PATH, strip config paths that
        could override the acquisition discipline, and — for a remote source
        with a token — inject the credential via git's env-config so the
        token lives ONLY in the child's environment (design §A.3 "decrypt
        immediately before spawning git, inject into the child's env, never
        log/return it").

        ``GIT_TERMINAL_PROMPT=0`` is set for EVERY child (card §15 network
        safety): a private repo with no usable token fails fast with an
        auth error instead of hanging on an interactive credential prompt
        (a hang would only end at the timeout wall, and a prompt is the
        one failure mode that would otherwise keep the child alive).

        The token is delivered as an ``http.<url>.extraheader`` Authorization
        header through the ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_0``/
        ``GIT_CONFIG_VALUE_0`` env mechanism git reads at startup — NOT
        embedded in a clone URL / argv (which would surface in the process
        table).  This is provider-agnostic: GitHub and GitLab both accept
        ``Authorization: Bearer *** over HTTPS.
        """
        env = dict(os.environ)
        for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_SSL_NO_VERIFY", "GIT_ASKPASS"):
            env.pop(var, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if token and url:
            host = (urlparse(url).hostname or "").lower()
            scheme = (urlparse(url).scheme or "https").lower()
            target = f"{scheme}://{host}/"
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = f"http.{target}.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
        return env

    async def _git(
        self,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str],
        deadline: float,
        *,
        allow_failure: bool = False,
    ) -> str:
        """Run one bounded ``git <argv...>`` call (argument list only).

        ``deadline`` is the acquire call's TOTAL monotonic wall
        (design §B.1 / card §15): the per-call timeout is what remains of it,
        so the whole sequence of git calls — never one command — is bounded
        by the config value.  Returns stdout; on a non-zero exit raises
        :class:`_GitFailure` (carrying the sanitized stderr tail) unless
        ``allow_failure`` — a failed call is never silently treated as
        success.  A call that would start past the deadline raises
        ``TimeoutError`` immediately (the reap + ACQ_TIMEOUT mapping in the
        caller is still reachable, and no child is spawned for a zero
        budget).
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        full = ["git", *argv]
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise AcquisitionError(ACQ_SOURCE_UNREACHABLE, "git executable not found on this host") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=remaining)
        except TimeoutError:
            await self._terminate_and_reap_process(proc)
            raise
        if proc.returncode is not None and proc.returncode != 0 and not allow_failure:
            raise _GitFailure(argv, proc.returncode, err)
        return out.decode("utf-8", errors="replace") if out else ""

    async def _terminate_and_reap_process(self, proc: asyncio.subprocess.Process) -> None:
        """Two-stage process-group reap (the sandbox recipe, §A.2).

        POSIX: SIGTERM the group, grace, then SIGKILL the group.  On a
        platform without process-group control (Windows — ``os.killpg`` /
        ``SIGKILL`` are absent) the whole thing degrades to a single
        ``proc.kill()`` on the direct child (design §E "Windows
        start_new_session degrades to proc.kill()"; the bounded wall still
        holds, only the group reap is lost).
        """
        if proc.returncode is not None:
            await proc.wait()
            return
        # Platform-conditional group-control primitives, read via getattr so
        # the module type-checks on Windows (where ``os.killpg`` /
        # ``os.getpgid`` / ``signal.SIGKILL`` do not exist) while keeping the
        # POSIX two-stage group reap intact.  A missing primitive degrades to
        # the direct-child ``proc.kill()`` (design §E).
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        sigterm = getattr(signal, "SIGTERM", None)
        sigkill = getattr(signal, "SIGKILL", None)

        def _group_term() -> None:
            if killpg is not None and getpgid is not None and sigterm is not None:
                try:
                    killpg(getpgid(proc.pid), sigterm)
                    return
                except (ProcessLookupError, PermissionError, OSError, TypeError):
                    pass
            proc.kill()

        _group_term()
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
            return
        except TimeoutError:
            pass
        if killpg is not None and getpgid is not None and sigkill is not None:
            try:
                killpg(getpgid(proc.pid), sigkill)
            except (ProcessLookupError, PermissionError, OSError, TypeError):
                proc.kill()
        else:
            proc.kill()
        await proc.wait()

    @staticmethod
    def _classify_git_failure(exc: _GitFailure) -> AcquisitionError:
        """Map a failed git call to a closed ACQ code.

        Only the stderr CLASS drives the code (the value stays secret):
        auth failures -> ACQ_AUTH_FAILED (never retried), a missing ref /
        "not found" -> ACQ_REF_NOT_FOUND, "could not resolve host" /
        connection errors -> ACQ_SOURCE_UNREACHABLE (retryable), anything
        else -> ACQ_SOURCE_INVALID.  The detail is a bounded, redacted
        summary, never the raw token-bearing stderr.
        """
        err = _sanitize_git_stderr(exc.stderr).lower()
        if "authentication" in err or "could not read username" in err or "401" in err or "403" in err or "access denied" in err:
            return AcquisitionError(ACQ_AUTH_FAILED, f"git {exc.argv[0]}: authentication/permission failed (see sanitized detail)")
        if "couldn't find remote ref" in err or "not found" in err or "unknown revision" in err or "invalid ref" in err:
            return AcquisitionError(ACQ_REF_NOT_FOUND, f"git {exc.argv[0]}: the requested ref could not be found")
        if "could not resolve host" in err or "connection" in err or "timed out" in err or "failed to connect" in err or "remote host" in err:
            return AcquisitionError(ACQ_SOURCE_UNREACHABLE, f"git {exc.argv[0]}: the remote is unreachable (transient)")
        detail = err[:200] if err else "git command failed"
        return AcquisitionError(ACQ_SOURCE_INVALID, f"git {exc.argv[0]} failed: {detail}")

    # ------------------------------------------------------------------
    # Post-checks + artifact publish (design §B.1 steps 4-5).
    # ------------------------------------------------------------------

    def _post_check_tree(self, work_dir: Path) -> None:
        """Bounded post-checks on the acquired tree BEFORE any publish.

        - submodules: ``.gitmodules`` present -> SUBMODULES_UNSUPPORTED
          (fail-closed, no ``--recurse`` — card §16);
        - reserved first-segment names / ``..`` on every member path via the
          shared ``normalize_rel`` + the materialization reserved set (the
          SAME rule, design §18 — a collision is SECURITY_REJECTED);
        - symlinks: recorded and never followed (an escape vector, card §18).
        """
        for root, dirs, files in os.walk(work_dir):
            for name in files:
                full = Path(root) / name
                rel = full.relative_to(work_dir).as_posix()
                if full.is_symlink():
                    continue  # recorded, never followed
                if name == ".gitmodules" and full.parent == work_dir:
                    raise AcquisitionError(
                        SUBMODULES_UNSUPPORTED,
                        "source declares submodules; V1 refuses them (fail-closed)",
                    )
                normalized = intake_security.normalize_rel(rel)
                if normalized is None:
                    raise AcquisitionError(ACQ_SECURITY_REJECTED, "acquired tree carries a path-traversal segment")
                first = normalized.split("/", 1)[0]
                if first in RESERVED_STORAGE_NAMES:
                    raise AcquisitionError(ACQ_SECURITY_REJECTED, f"acquired member {first!r} collides with a reserved name")
            # Prune the .git metadata dir out of the walk (it is not source
            # material — the tar carries only the working tree).
            if ".git" in dirs:
                dirs.remove(".git")

    async def _publish_artifact(self, work_dir: Path, agent_id: uuid.UUID, repo_id: uuid.UUID, artifact_key: str) -> None:
        """Tar the working tree into the single bounded artifact.

        The tar carries ONLY the working tree (no ``.git`` metadata, no hooks
        — card §17), is bounded by the materialization byte budgets, and is
        written through the storage facade so a later materialization reads
        ONE object rather than re-cloning.  An over-budget tree is
        ACQ_SIZE_LIMIT (permanent, not a retry).
        """
        import io

        from app.services.project_materialization_service import (
            MAX_MATERIALIZE_FILE_BYTES,
            MAX_MATERIALIZE_TOTAL_BYTES,
        )

        buf = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for root, dirs, files in os.walk(work_dir):
                # Prune .git metadata (the SAME rule the post-checks use):
                # the artifact carries ONLY the working tree — no .git, no
                # hooks, no objects (card §17).  Unpruned .git members would
                # also collide with the materialization reader's reserved
                # first-segment check (".git" is reserved) and reject the
                # artifact downstream.
                if ".git" in dirs:
                    dirs.remove(".git")
                for name in files:
                    full = Path(root) / name
                    if full.is_symlink():
                        continue
                    size = full.stat().st_size
                    if size > MAX_MATERIALIZE_FILE_BYTES:
                        raise AcquisitionError(ACQ_SIZE_LIMIT, f"member {full.name!r} exceeds the per-file byte budget")
                    total += size
                    if total > MAX_MATERIALIZE_TOTAL_BYTES:
                        raise AcquisitionError(ACQ_SIZE_LIMIT, "acquired tree exceeds the total byte budget")
                    tar.add(full, arcname=full.relative_to(work_dir).as_posix())
        backend = get_storage_backend()
        await backend.write_bytes(artifact_key, buf.getvalue())

    # ------------------------------------------------------------------
    # Record / audit / cleanup.
    # ------------------------------------------------------------------

    async def _record_success(
        self,
        db: AsyncSession,
        repo: Repository,
        agent: Agent,
        artifact_key: str,
        resolved_rev: str,
        requested_ref: str | None,
        provider: str,
    ) -> None:
        """Stamp the locator with the acquisition metadata + verified mark.

        No schema change (design §C.4): ``repositories.locator`` JSON carries
        ``acq_artifact`` / ``resolved_rev`` / ``requested_ref`` / ``provider``
        / ``acquired_at`` / ``acq_result``.  A token never enters the locator
        (the credential guard already forbids it at intake; this path adds
        none).  The audit row is best-effort, bounded, and secret-free.
        """
        now = datetime.now(UTC)
        loc = dict(repo.locator or {})
        loc["acq_artifact"] = artifact_key
        loc["resolved_rev"] = resolved_rev
        loc["requested_ref"] = requested_ref
        loc["provider"] = provider
        loc["acquired_at"] = now.isoformat()
        loc["acq_result"] = ACQ_OK
        loc["acq_detail"] = "acquired"
        repo.locator = loc
        repo.verified = True
        repo.pending_verifier = False
        repo.verified_at = now
        db.add(repo)
        await db.flush()
        # Belt-and-braces: the credential guard must still pass on the
        # updated locator (no token / userinfo leaked into the JSON).
        leak = intake_security.scan_locator_for_credentials(repo.locator)
        if leak is not None:
            raise AcquisitionError(ACQ_SECURITY_REJECTED, "acquisition would persist a credential: " + leak)
        try:
            row = AuditLog(
                tenant_id=repo.tenant_id,
                user_id=None,
                agent_id=agent.id,
                action="git_acquisition",
                details={
                    "project_id": str(repo.project_id),
                    "repo_id": str(repo.id),
                    "result": ACQ_OK,
                    "provider": provider,
                    "requested_ref": requested_ref,
                    "resolved_rev": resolved_rev,
                    "artifact_key": artifact_key,
                },
            )
            db.add(row)
            await db.flush()
        except Exception as exc:  # noqa: BLE001 - audit is best-effort; the artifact + locator metadata are the primary outcome
            logger.error(f"[git_acq] audit write failed: {exc}")

    async def _cleanup(self, *, work_dir: Path | None, outcome: AcquisitionOutcome, agent_id: uuid.UUID, repo_id: uuid.UUID) -> None:
        """Delete the host work dir on every exit; on a failure also drop the
        partial staging storage subtree so a half-product is never visible.

        ``work_dir`` is None when the call failed before the git run (a shape
        / validation rejection) — then there is only the storage subtree (and
        even that only once a publish happened; a pure validation failure
        wrote nothing, so the delete_tree is a harmless no-op).
        """
        import shutil
        import stat

        # Delete the host work dir on EVERY exit — with bounded retry: a
        # just-finished git child may still hold file handles (antivirus /
        # indexer on Windows), and git marks .git/objects/pack/*.pack
        # READ-ONLY, which blocks unlink() on Windows.  Each attempt makes
        # the members writable first, so one rmtree is a reliable terminal
        # state; a surviving residue is logged LOUD (it is the staged,
        # agent-scoped prefix, never target-keyspace material) instead of
        # silently ignored — the "delete on every exit" invariant (design
        # §A.4) is not met by pretending the delete happened.
        if work_dir is not None and work_dir.exists():
            for _attempt in range(3):
                try:
                    for root, _dirs, files in os.walk(work_dir):
                        for name in files:
                            try:
                                os.chmod(Path(root) / name, stat.S_IRUSR | stat.S_IWUSR)
                            except OSError:
                                pass  # already gone / unreadable: the attempt below observes it
                    shutil.rmtree(work_dir)
                    break
                except OSError:
                    if _attempt == 2:
                        logger.error(f"[git_acq] work-dir cleanup left residue for {work_dir} (staged prefix, not target-keyspace)")
                    else:
                        await asyncio.sleep(0.5 * (_attempt + 1))
        if outcome.state != "acquired":
            try:
                backend = get_storage_backend()
                await backend.delete_tree(f"{agent_id}/.git-acq/{repo_id}")
            except Exception as exc:  # noqa: BLE001 - staging cleanup is best-effort on the failure path; the artifact was never published
                logger.error(f"[git_acq] staging cleanup failed for {agent_id}/.git-acq/{repo_id}: {exc}")

    @staticmethod
    def _artifact_key(agent_id: uuid.UUID, repo_id: uuid.UUID) -> str:
        """The ONE bounded tar's storage key (design §A.4 / §B.1 step 5).

        Agent-scoped (``{agent_id}/.git-acq/{repo_id}/source.tar``) so two
        tenants can never share a git artifact, and normalized through the
        single authoritative storage-key rule.  A later materialization reads
        exactly this object — no re-clone.
        """
        return normalize_storage_key(f"{agent_id}/.git-acq/{repo_id}/{_ARTIFACT_NAME}")


class _GitFailure(Exception):
    """A non-zero git call (carrying the sanitized stderr tail)."""

    def __init__(self, argv: list[str], returncode: int, stderr: bytes | None) -> None:
        super().__init__(f"git {argv[0]} exited {returncode}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


#: A single shared service instance (the intake / materialization services
#: use the same shape).
git_acquisition_service = GitAcquisitionService()

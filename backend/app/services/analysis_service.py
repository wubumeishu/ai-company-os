"""Phase 2C Analysis service — the minimal Analysis persistence owner.

Per docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2 this service OWNS the four
minimal paths that let the OS "reliably store one Project Analysis + its
sources":

- ``launch``            — open an ``analysis_runs`` row bound to the source
                          revision (``repositories.locator.resolved_rev``,
                          written back by GitAcquisitionService) and set
                          ``project.status -> ANALYZING`` (Phase 2C owns the
                          ANALYZING transition, decision OQ-6).
- ``record_findings``   — write the transient ``analysis_findings`` of the
                          open run (every finding carries a closed
                          provenance tag + bounded traceable evidence;
                          README-as-truth is forbidden) and close the run.
- ``promote_finding``   — on human/company confirmation, insert a DURABLE
                          ``project_knowledge`` row with the provenance
                          (``source_analysis_run_id``) copied, not owned.
- ``read_current_and_history`` / ``knowledge`` — return the CURRENT run +
                          its FINDINGS and the HISTORY (all prior runs);
                          knowledge rows are revision-independent.

Settled decisions honored (card t_37e2eb05, design §9.2):
- OQ-5: the revision carrier is the typed ``analysis_runs.revision_sha``
  (indexed commit hash), decoupled from the free-form
  ``repositories.locator`` JSON.
- OQ-6: ``analysis_runs.status`` is a CLOSED result-code enum (the AN_*
  set below, mirroring the ACQ_* pattern) — NOT a new step-by-step
  workflow state machine (root AGENTS.md §2).  ``PENDING_CONFIRMATION``
  stays INERT: no code path in this lane sets it (the confirmation UI does
  not exist yet); promotion happens through ``promote_finding`` while the
  project remains ANALYZING.
- Hard transient-vs-durable boundary: findings die with their run
  (``ON DELETE CASCADE``); a promoted knowledge row is NOT invalidated by
  a later analysis at a different commit.
- Versioning: ``UNIQUE(project_id, revision_sha)`` makes the table
  append-only — a re-analysis at a NEW commit is a NEW row, history is
  never clobbered.  A racing launch at the SAME revision loses the insert
  race and RE-READS the existing run (the concurrency guard).
- M9 fourth gate: every read is tenant-scoped via
  ``TenantScopedBaseDAO``; ``verify_tenant_scope`` re-asserts the record's
  tenant equals the acting context, and the target agent's tenant must
  equal the project's tenant (fail closed, 403-class).
- Stage 11 (hard): this path is STATIC-ONLY.  It reads DB rows and the
  bounded locator JSON written by acquisition; it NEVER executes the
  target project's code (no subprocess, no interpreter, no build, no
  ``pip``/``npm`` install, no service start).  Dynamic analysis is
  deferred to a future Execution/Analysis sandbox.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.dao.analysis_dao import analysis_finding_dao, analysis_run_dao, project_knowledge_dao
from app.dao.project_intake_dao import project_dao
from app.models.agent import Agent
from app.models.analysis import (
    ANALYSIS_CATEGORY_VALUES,
    ANALYSIS_FACING_TAGS,
    ANALYSIS_RUN_STATUSES,
    ANALYSIS_SEVERITY_VALUES,
    AnalysisFinding,
    AnalysisRun,
    ProjectKnowledge,
)
from app.models.project import Project, Repository
from app.models.user import User
from app.services import intake_security
from app.services.project_materialization_service import GIT_SOURCE_TYPES

# ---------------------------------------------------------------------------
# Closed result-code set (mirrors the ACQ_* pattern: the service validates
# against it before any write; the DAO persists the decided value).
# ---------------------------------------------------------------------------

AN_OK = "AN_OK"
AN_NOT_INITIALIZED = "AN_NOT_INITIALIZED"
AN_SOURCE_INVALID = "AN_SOURCE_INVALID"
AN_INVALID_FINDING = "AN_INVALID_FINDING"
AN_RUN_NOT_OPEN = "AN_RUN_NOT_OPEN"
AN_KNOWLEDGE_INVALID = "AN_KNOWLEDGE_INVALID"

#: The closed transport code set.  A code outside it is a programming error;
#: readers of persisted / status data re-validate against it (F1 pattern).
ANALYSIS_RESULT_CODES = frozenset(
    {AN_OK, AN_NOT_INITIALIZED, AN_SOURCE_INVALID, AN_INVALID_FINDING, AN_RUN_NOT_OPEN, AN_KNOWLEDGE_INVALID}
)


class AnalysisSecurity(RuntimeError):
    """A tenant-isolation finding (403-class, never retried).

    Raised when the acting context / target agent crosses a tenant
    security boundary (the M9 fourth gate).  Maps to 403 at the
    transport, mirroring ``AcquisitionSecurity``.
    """


class AnalysisError(Exception):
    """A closed AN_* code outcome (409-class) with a stable code + detail.

    Client-reachable gate failures (a non-INITIALIZED project, a source
    without a stored revision, an invalid finding / knowledge input) become
    a closed code carried by the outcome — they must never surface as a
    500 (the same fail-closed contract the ACQ codes carry).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if code not in ANALYSIS_RESULT_CODES:
            raise ValueError(f"analysis code {code!r} is not in the closed result-code set")
        self.code = code
        self.message = message


# Bounded-input limits (stage-16: every client-reachable input is bounded
# before it reaches a DB write; mirrors the bounded pattern of intake /
# acquisition).
MAX_SUMMARY_CHARS = 4000
MAX_SUBJECT_CHARS = 200  # matches the String(200) column width
MAX_STATEMENT_CHARS = 10_000
MAX_EVIDENCE_ANCHORS = 100
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_FINDINGS_PER_RECORD = 100

#: A well-formed resolved revision: 7..64 hex chars (short or full commit
#: sha).  Anything else in the locator JSON is a programming error, not a
#: revision (fail closed — the revision carrier is TYPED, design OQ-5).
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass
class AnalysisLaunchOutcome:
    """The transport shape of a launch attempt (mirrors AcquisitionOutcome).

    state:
      - ``launched`` — a new AN_OPEN run was opened at the source revision
        and the project moved to ANALYZING (the 201 case).
      - ``existing`` — the revision already had a run (the UNIQUE
        invariant's re-read path); nothing was clobbered (the 200 case).
      - ``failed``  — a closed AN_* code (the 409 case).
    """

    state: str
    code: str | None = None
    message: str = ""
    run: AnalysisRun | None = None
    revision_sha: str | None = None


@dataclass
class FindingRecord:
    """A validated finding input, ready for persistence (bounded + tagged)."""

    severity: str
    category: str
    tag: str
    summary: str
    evidence: dict = field(default_factory=dict)


def _sha_is_wellformed(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


class AnalysisService:
    """The Analysis persistence owner.  API handlers call ``launch``,
    ``record_findings``, ``promote_finding``, ``read_current_and_history``
    and ``knowledge``; everything else is internal."""

    # ------------------------------------------------------------------
    # Public entry points.
    # ------------------------------------------------------------------

    async def launch(
        self,
        db,
        *,
        project: Project,
        repo: Repository,
        agent: Agent,
        current_user: User,
    ) -> AnalysisLaunchOutcome:
        """Open one analysis run bound to the source's resolved revision.

        Pipeline (fail-closed, all bounded):
          1. tenant entry gate (record scope + target-agent tenant);
          2. status gate — the project may start analysis from INITIALIZED
             (the Phase 2C-owned INITIALIZED -> ANALYZING transition) or,
             for a NEW revision, while still ANALYZING (append-only
             versioning, design §9.2: a re-analysis at a new commit is a
             NEW row).  Any terminal / pre-analysis status is refused.
          3. revision gate — the source must be a verified git source with
             a well-formed ``locator.resolved_rev`` (the OQ-5 typed
             revision carrier, written back by GitAcquisitionService);
          4. open the AN_OPEN run (the UNIQUE invariant's insert race is
             recovered by a re-read, never a clobber) and set
             project.status -> ANALYZING.

        Static-only (stage 11): step 3 reads the locator JSON — no git
        work, no execution of the target project.
        """
        outcome = AnalysisLaunchOutcome(state="pending")
        try:
            self._entry_gates(project, repo, agent, current_user)
            if project.status not in ("INITIALIZED", "ANALYZING"):
                raise AnalysisError(
                    AN_NOT_INITIALIZED,
                    f"project is in status {project.status!r}; analysis launch requires INITIALIZED (or a still-ANALYZING project for a new revision)",
                )
            revision_sha, requested_ref = self._revision_from_locator(repo)
            outcome.revision_sha = revision_sha
            outcome.message = "launched"

            run = AnalysisRun(
                project_id=project.id,
                agent_id=agent.id,
                revision_sha=revision_sha,
                requested_ref=requested_ref,
                resolved_at=datetime.now(UTC),
                status=ANALYSIS_RUN_STATUSES[0],  # AN_OPEN
                tenant_id=project.tenant_id,
            )
            try:
                await analysis_run_dao.open_run(run, tenant_id=project.tenant_id, db=db)
            except IntegrityError:
                # The UNIQUE(project_id, revision_sha) invariant: a racing
                # launch at the SAME revision already won the insert.  The
                # failed INSERT must be rolled back before the session can
                # re-read (its only in-transaction write at this point is
                # the failed row itself); the re-read returns the existing
                # run — the versioning invariant is never clobbered.
                await db.rollback()
                existing = await analysis_run_dao.get_for_project_and_revision(
                    project.id, revision_sha, db=db
                )
                if existing is not None:
                    outcome.state = "existing"
                    outcome.code = AN_OK
                    outcome.run = existing
                    outcome.message = "an analysis run already exists for this revision"
                    return outcome
                raise AnalysisError(AN_SOURCE_INVALID, "revision binding could not be opened") from None
            # Phase 2C OWNS the ANALYZING transition (OQ-6): the legality
            # was decided above (the INITIALIZED gate); the DAO persists.
            await project_dao.transition(project, "ANALYZING", db=db)
            outcome.state = "launched"
            outcome.code = AN_OK
            outcome.run = run
            return outcome
        except AnalysisError as exc:
            outcome.state = "failed"
            outcome.code = exc.code
            outcome.message = exc.message
            return outcome

    async def record_findings(
        self,
        db,
        *,
        project: Project,
        run: AnalysisRun,
        findings_in: list[dict],
        current_user: User,
    ) -> tuple[list[AnalysisFinding], AnalysisRun]:
        """Record >=1 transient findings for the OPEN run, then close it.

        Every finding carries a closed provenance tag + bounded traceable
        evidence (path:line anchors; README-as-truth is forbidden).  A
        terminal run refuses findings (AN_RUN_NOT_OPEN): findings belong to
        one open analysis window.  The run closes as AN_COMPLETED.
        """
        self._scope_gates(project, run, current_user)
        if run.status != ANALYSIS_RUN_STATUSES[0]:
            raise AnalysisError(
                AN_RUN_NOT_OPEN, f"run {run.id} is {run.status!r}; findings require an open run"
            )
        if not findings_in or len(findings_in) > MAX_FINDINGS_PER_RECORD:
            raise AnalysisError(AN_INVALID_FINDING, f"expected 1..{MAX_FINDINGS_PER_RECORD} findings per record")
        records = [self._validate_finding(f, project.tenant_id, run.id) for f in findings_in]
        persisted = list(await analysis_finding_dao.add_findings(records, tenant_id=project.tenant_id, db=db))
        await analysis_run_dao.close_run(run, new_status=ANALYSIS_RUN_STATUSES[1], db=db)  # AN_COMPLETED
        return persisted, run

    async def promote_finding(
        self,
        db,
        *,
        project: Project,
        run: AnalysisRun,
        subject: str,
        statement: str,
        current_user: User,
    ) -> ProjectKnowledge:
        """Promote a confirmed finding into DURABLE project knowledge.

        The confirmation step (the PENDING_CONFIRMATION lane stays INERT —
        no code path sets it): on human/company confirmation the knowledge
        row is written CONFIRMED with the provenance copied
        (``source_analysis_run_id``).  The row is revision-independent — a
        later analysis at a different commit never invalidates it.
        """
        self._scope_gates(project, run, current_user)
        subject = (subject or "").strip()
        statement = (statement or "").strip()
        if not (1 <= len(subject) <= MAX_SUBJECT_CHARS):
            raise AnalysisError(AN_KNOWLEDGE_INVALID, f"knowledge subject must be 1..{MAX_SUBJECT_CHARS} chars")
        if not (1 <= len(statement) <= MAX_STATEMENT_CHARS):
            raise AnalysisError(AN_KNOWLEDGE_INVALID, f"knowledge statement must be 1..{MAX_STATEMENT_CHARS} chars")
        knowledge = ProjectKnowledge(
            project_id=project.id,
            subject=subject,
            statement=statement,
            source_analysis_run_id=run.id,
            status="CONFIRMED",
            tenant_id=project.tenant_id,
        )
        await project_knowledge_dao.add_knowledge(knowledge, tenant_id=project.tenant_id, db=db)
        return knowledge

    async def read_current_and_history(
        self,
        db,
        *,
        project: Project,
        current_user: User,
    ) -> dict:
        """Return the CURRENT run + its findings, and the HISTORY (prior runs).

        "current" = the newest run; "history" = all prior rows (append-only
        versioning, design §9.2).  A project that was never analyzed reads
        back as current=None + empty history (data, not an error).
        """
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        runs = list(await analysis_run_dao.list_for_project(project.id, db=db))
        current = runs[0] if runs else None
        current_findings = (
            list(await analysis_finding_dao.list_for_run(current.id, db=db)) if current is not None else []
        )
        return {
            "current": current,
            "current_findings": current_findings,
            "history": runs[1:],
        }

    async def knowledge(self, db, *, project: Project, current_user: User) -> list[ProjectKnowledge]:
        """All durable knowledge rows for a project (newest first)."""
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        return list(await project_knowledge_dao.list_for_project(project.id, db=db))

    # ------------------------------------------------------------------
    # Gates + validation (fail closed, before any write).
    # ------------------------------------------------------------------

    def _entry_gates(self, project: Project, repo: Repository, agent: Agent, current_user: User) -> None:
        """Tenant scope + source_type guard, run before any write.

        Mirrors the GitAcquisitionService entry gate (M9 fourth gate):
        ``verify_tenant_scope`` re-asserts the record's tenant equals the
        acting context, and the target agent's tenant must equal the
        project's tenant.
        """
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        agent_tenant = getattr(agent, "tenant_id", None)
        if agent_tenant is None or project.tenant_id is None:
            raise AnalysisSecurity("analysis has no tenant context; refusing to run")
        if agent_tenant != project.tenant_id:
            raise AnalysisSecurity("target agent tenant does not match the project tenant")
        if repo.source_type not in GIT_SOURCE_TYPES:
            raise AnalysisError(
                AN_SOURCE_INVALID, f"source_type {repo.source_type!r} is not a git source"
            )

    def _scope_gates(self, project: Project, run: AnalysisRun, current_user: User) -> None:
        """Re-assert the run belongs to this project + tenant before a write."""
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        if run.project_id != project.id:
            raise AnalysisError(AN_RUN_NOT_OPEN, "analysis run does not belong to this project")
        if run.tenant_id != project.tenant_id:
            raise AnalysisSecurity("analysis run tenant does not match the project tenant")

    def _revision_from_locator(self, repo: Repository) -> tuple[str, str | None]:
        """Read the typed revision carrier from the verified locator.

        The source of ``revision_sha`` is ``repositories.locator.resolved_rev``
        — written back by GitAcquisitionService on a successful acquire.
        A source that was never verified / never stored a revision cannot
        start an analysis (fail closed: AN_SOURCE_INVALID, not a guess).
        """
        if not repo.verified:
            raise AnalysisError(AN_SOURCE_INVALID, "the source has not been verified; acquire it first")
        loc = repo.locator or {}
        revision_sha = loc.get("resolved_rev")
        # isinstance narrows to str for the static type-checker; the helper
        # additionally enforces the closed sha shape (7..64 hex chars, OQ-5).
        if not isinstance(revision_sha, str) or not _sha_is_wellformed(revision_sha):
            raise AnalysisError(
                AN_SOURCE_INVALID, "the verified source carries no resolved revision; acquire it first"
            )
        requested_ref = loc.get("requested_ref")
        requested_ref = requested_ref if isinstance(requested_ref, str) and requested_ref else None
        return revision_sha, requested_ref

    def _validate_finding(self, finding_in: dict, tenant_id: uuid.UUID, analysis_run_id: uuid.UUID) -> AnalysisFinding:
        """Validate one finding against the closed sets + bounds (pre-write)."""
        tag = finding_in.get("tag")
        if tag not in ANALYSIS_FACING_TAGS:
            raise AnalysisError(AN_INVALID_FINDING, f"finding tag {tag!r} is not in the closed set")
        category = finding_in.get("category")
        if category not in ANALYSIS_CATEGORY_VALUES:
            raise AnalysisError(AN_INVALID_FINDING, f"finding category {category!r} is not in the closed set")
        severity = finding_in.get("severity") or "INFO"
        if severity not in ANALYSIS_SEVERITY_VALUES:
            raise AnalysisError(AN_INVALID_FINDING, f"finding severity {severity!r} is not in the closed set")
        summary = finding_in.get("summary")
        if not isinstance(summary, str) or not (1 <= len(summary.strip()) <= MAX_SUMMARY_CHARS):
            raise AnalysisError(AN_INVALID_FINDING, f"finding summary must be 1..{MAX_SUMMARY_CHARS} chars")
        evidence = finding_in.get("evidence")
        if not isinstance(evidence, dict):
            # README-as-truth is forbidden: a finding without traceable
            # evidence is not a finding — fail closed before the write.
            raise AnalysisError(AN_INVALID_FINDING, "finding evidence must be a JSON object")
        anchors = evidence.get("anchors")
        if not isinstance(anchors, list) or not (1 <= len(anchors) <= MAX_EVIDENCE_ANCHORS):
            raise AnalysisError(
                AN_INVALID_FINDING, f"finding evidence must carry 1..{MAX_EVIDENCE_ANCHORS} path:line anchors"
            )
        for anchor in anchors:
            if not isinstance(anchor, str) or not anchor.strip():
                raise AnalysisError(AN_INVALID_FINDING, "each evidence anchor must be a path:line string")
        if len(json.dumps(evidence, default=str)) > MAX_EVIDENCE_BYTES:
            raise AnalysisError(AN_INVALID_FINDING, f"finding evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
        return AnalysisFinding(
            analysis_run_id=analysis_run_id,
            severity=severity,
            category=category,
            tag=tag,
            summary=summary.strip(),
            evidence=evidence,
            tenant_id=tenant_id,
        )


analysis_service = AnalysisService()

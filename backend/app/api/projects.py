"""Project Intake API (Phase 2B-2).

Transport adapters for the Project Intake lifecycle. Per
backend/AGENTS.md the handlers parse + validate the request, establish the
authenticated + authorized caller, hand explicit inputs to the owning
service (``project_intake_service``), and map the outcome to the transport
response. No business orchestration, ORM query, or state transition lives
here.

Routes (brief §3.1):
    POST   /api/projects                          create Intake
    GET    /api/projects                          list (current tenant)
    GET    /api/projects/{project_id}             detail (repositories + reason)
    POST   /api/projects/{project_id}/validate     explicit source validation
    POST   /api/projects/{project_id}/materialize/{agent_id}   materialize
    POST   /api/projects/{project_id}/repositories/{repo_id}/acquire/{agent_id}
                                                 acquire a git source (Phase 2B-4)
    GET    /api/projects/{project_id}/repositories/{repo_id}/acquire/{agent_id}
                                                 acquisition status

Status-code mapping (brief §3.2 + security module's mapping table):
    create 201 on success; request validation fails with 422 before any
    entity is created; a credential-leaking locator fails with 409
    SECURITY_REJECTED and persists nothing.
    validate 200 on all-pass (INITIALIZED) or a retryable hold; 409 on a
    permanent rejection (REJECTED) or when re-validating a terminal state.
    read/detail: 404 when the row is absent from the acting tenant scope,
    403 when the caller is neither creator nor same-tenant admin — both
    decided by the security module's guards, not re-invented here.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.analysis_dao import analysis_run_dao
from app.dao.project_intake_dao import project_dao
from app.database import get_db
from app.models.analysis import AnalysisRun
from app.models.project import Project, Repository
from app.models.user import User
from app.schemas.analysis import (
    AnalysisLaunchOut,
    AnalysisReadOut,
    AnalysisRunOut,
    FindingOut,
    FindingsIn,
    FindingsOut,
    KnowledgeIn,
    KnowledgeOut,
)
from app.schemas.project_intake import (
    AcquisitionOut,
    MaterializationOut,
    MaterializeRequest,
    ProjectIntakeCreate,
    ProjectOut,
    RejectionInfo,
)
from app.services.analysis_service import (
    AnalysisError,
    AnalysisSecurity,
    analysis_service,
)
from app.services.git_acquisition_service import (
    AcquisitionSecurity,
    git_acquisition_service,
)
from app.services.intake_security import (
    ReadForbidden,
    SecurityError,
    TenantScopeViolation,
    verify_read_access,
    verify_tenant_scope,
)
from app.services.project_intake_service import project_intake_service
from app.services.project_materialization_service import (
    MaterializationNotReady,
    MaterializationSecurity,
    project_materialization_service,
)

router = APIRouter(prefix="/projects", tags=["projects"])

_ADMIN_ROLES = ("platform_admin", "org_admin")


# ---------------------------------------------------------------------------
# Permission + error mapping (brief §3.3, §3.4; security module's table):
# the guards below are the security module's authoritative checks — this
# module only translates their verdicts into transport responses.
# ---------------------------------------------------------------------------
async def _load_authorized_project(
    db: AsyncSession,
    project_id: uuid.UUID,
    current_user: User,
) -> Project:
    project = await project_dao.get_scoped_with_repositories(project_id, db=db)
    if project is None:
        # Foreign-tenant rows are invisible to the tenant-scoped DAO + the
        # do_orm_execute tenant filter, so "not found in my tenant" is a 404
        # by construction (never a cross-tenant disclosure).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    try:
        verify_read_access(current_user, project)
        verify_tenant_scope(project.tenant_id, current_user.tenant_id)
    except ReadForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this project",
        ) from None
    return project


def _rejection_info_from_persisted(project: Project) -> RejectionInfo | None:
    """Reconstruct a rejection view from the persisted REJECTED fields."""
    if project.status != "REJECTED" or not project.rejection_reason:
        return None
    return RejectionInfo(
        reason_code=project.rejection_reason,
        reason_detail=project.rejection_detail,
        retryable=False,
    )


def _project_out(project: Project, rejection_info: RejectionInfo | None = None) -> ProjectOut:
    return ProjectOut.from_project(project, rejection_info)


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_intake(
    data: ProjectIntakeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept + register an Intake: create the Project in RECEIVED state with
    its source rows. Request validation (422) runs before this handler, so no
    entity is created from an invalid body (brief §3.2 / Phase 2A §E.2 rule).
    A credential-leaking locator fails with 409 SECURITY_REJECTED and
    persists nothing (brief §八; security module mapping table)."""
    try:
        project = await project_intake_service.create_intake(
            db,
            current_user=current_user,
            name=data.name,
            description=data.description,
            goal=data.goal,
            sources=data.sources,
        )
    except SecurityError as exc:
        # Credential leak (IntakeSecurityError) or tenant-scope violation:
        # one narrow transport mapping, nothing persisted.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SECURITY_REJECTED",
                "message": str(exc),
                "retryable": False,
            },
        ) from None
    return _project_out(project, None)


@router.get("/", response_model=list[ProjectOut])
async def list_intakes(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the current tenant's Intakes the user may read.

    Creators see their own projects; admins see every project in their
    tenant (same-tenant only — no cross-tenant admin, brief §3.3).
    """
    is_admin = current_user.role in _ADMIN_ROLES
    projects = await project_dao.list_for_user_scoped(
        user_id=current_user.id,
        is_admin=is_admin,
        skip=skip,
        limit=limit,
        db=db,
    )
    return [_project_out(p, _rejection_info_from_persisted(p)) for p in projects]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_intake(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one Intake's detail, including its repositories and reason code."""
    project = await _load_authorized_project(db, project_id, current_user)
    return _project_out(project, _rejection_info_from_persisted(project))


@router.post("/{project_id}/validate", response_model=ProjectOut)
async def validate_intake(
    project_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly validate the project's sources and advance the state machine.

    200 on all-pass (INITIALIZED) or a retryable hold; 409 on a permanent
    rejection (REJECTED, rejection_info populated) or when re-validating a
    terminal state. The chosen status is set on the shared ``Response`` so
    the same ``ProjectOut`` body serves both outcomes.
    """
    project = await _load_authorized_project(db, project_id, current_user)
    try:
        project, rejection_info = await project_intake_service.validate_sources(
            db, project=project, current_user=current_user
        )
    except SecurityError:
        # Terminal-state re-validation (IntakeTransitionError) or any other
        # security finding during the validate pass: one 409 mapping.
        response.status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "intake_terminal_state",
                "message": "Project is in a terminal Intake state; validation is not applicable",
                "retryable": False,
            },
        ) from None

    if rejection_info is not None and not rejection_info.retryable:
        # Permanent rejection → terminal REJECTED, reported as a 409 conflict.
        response.status_code = status.HTTP_409_CONFLICT
    return _project_out(project, rejection_info)


@router.post("/{project_id}/materialize/{agent_id}", response_model=MaterializationOut, status_code=status.HTTP_201_CREATED)
async def materialize_project(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: MaterializeRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Materialize a verified Project's source material into a target agent.

    Per docs/MATERIALIZATION_SECURE_SPEC_V1.md §10 the handler is a pure
    transport adapter: it loads the authorized project, authorizes the target
    agent (``check_agent_access``), hands explicit inputs to the owning
    service, and maps the outcome to the transport response (spec §10.2).

    Status mapping:
      - 201 on SUCCESS — the material is in the agent's storage subtree.
      - 409 on PARTIAL / FAILED, carrying the FULL ``MaterializationOut``
        body (per-repo detail) — never a 2xx that would misread partial work
        as success (spec §7.3 / §10.2).
      - 409 SOURCE_NOT_READY when the project is not INITIALIZED (pre-I/O
        gate; retryable=False).
      - 403 when the caller may not read the project or has no access to the
        target agent (read gate / agent gate, spec §5.1 gate 3).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)  # 404/403 propagate
    try:
        result = await project_materialization_service.materialize(
            db,
            project=project,
            agent=agent,
            overwrite=data.overwrite,
            current_user=current_user,
        )
    except MaterializationNotReady as exc:
        # Pre-I/O gate (spec §1 / §10.2): the project is not INITIALIZED, or a
        # repository is not verified-ready / is a git source.  One narrow 409
        # with the intake-style detail body; nothing was written.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from None
    except (MaterializationSecurity, TenantScopeViolation):
        # A tenant-isolation security finding: the M9 fourth gate (a Tenant-A
        # project -> Tenant-B agent, the combination gap ``check_agent_access``
        # alone leaves open for background callers) or the entry tenant
        # re-check.  403 (spec §5.1 gate 3 / §10.2 读门禁/租户).  No write.
        # Through the API the transport's prior gates already make these
        # unreachable; catching them keeps the mapping correct if a background
        # caller or a future reorder ever reaches them.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access: this materialization crosses a tenant security boundary",
        ) from None
    except SecurityError:
        # Residual storage-layer security finding re-raised by the source
        # readers (spec §3.3 / §10.2: a storage SecurityError maps to 409 and
        # must NOT be downgraded to "unreachable").  Intake-style 409 body.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SECURITY_REJECTED", "message": "storage security finding", "retryable": False},
        ) from None
    if result.outcome in ("PARTIAL", "FAILED"):
        # PARTIAL / FAILED is a 409 carrying the FULL per-repo body (spec
        # §7.3 / §10.2) — never a 2xx.  Set the status on the shared Response
        # (the validate_intake pattern) so the same MaterializationOut body
        # serves the 409 while the successful repos' detail is preserved.
        response.status_code = status.HTTP_409_CONFLICT
    return result


# ---------------------------------------------------------------------------
# Git Source Acquisition (Phase 2B-4, design GIT_ACQ_DESIGN_V1.md §C.2)
#
# Both routes live under the same router + the existing
# ``_load_authorized_project`` read gate; the target agent is authorized by
# ``check_agent_access`` (404/403 propagate) exactly like the materialize
# route, and the service re-asserts every isolation gate fail-closed so a
# background caller that skips the transport cannot cross tenants.  The
# transport is a pure adapter (backend AGENTS.md): all policy — the closed
# ACQ_* codes, the retry budget, the tenant gates, the artifact publish —
# lives in the service; this module only maps the outcome to the status.
# ---------------------------------------------------------------------------


def _find_repo(project: Project, repo_id: uuid.UUID) -> "Repository":
    """The repository row inside the authorized project, else a 404.

    ``_load_authorized_project`` eager-loads the repositories, so a repo id
    that belongs to another tenant / project is simply not in the list —
    the same tenant-invisibility-404 the project load relies on, never a
    cross-tenant disclosure.
    """
    for repo in list(getattr(project, "repositories", None) or []):
        if repo.id == repo_id:
            return repo
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")


@router.post(
    "/{project_id}/repositories/{repo_id}/acquire/{agent_id}",
    response_model=AcquisitionOut,
    status_code=status.HTTP_201_CREATED,
)
async def acquire_repository(
    project_id: uuid.UUID,
    repo_id: uuid.UUID,
    agent_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Acquire a registered git source into a verified, bounded artifact.

    The target agent (``agent_id``) supplies the credential scope, the
    tenant-isolation gate, and the artifact's agent-scoped storage key —
    the same authorization pattern the materialize route uses.  The ref
    context is the repository's locator as registered (no body).

    Status mapping (design §C.2):
      - 201 on ``acquired`` — artifact + verified metadata written; the
        later materialization reads exactly this object (no re-clone).
      - 409 on ``pending`` (a transient failure still inside the bounded
        retry budget; ``retryable=True``) or ``failed`` (a permanent
        outcome or an exhausted transient budget; ``retryable=False``) —
        never a 2xx that would misread an un-acquired source as ready.
      - 403 when the caller crosses a tenant security boundary
        (``AcquisitionSecurity``); 404/403 from ``check_agent_access``.

    The handler NEVER triggers a downstream Agent / Run / prompt /
    execution: it returns the acquisition result and stops (card §3 /
    card §23 — Acquisition is its own stage, the handoff is the artifact).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    repo = _find_repo(project, repo_id)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)  # 404/403 propagate
    try:
        outcome = await git_acquisition_service.acquire(
            db,
            project=project,
            repo=repo,
            agent=agent,
            current_user=current_user,
        )
    except (AcquisitionSecurity, TenantScopeViolation):
        # A tenant-isolation / scope violation at the entry gate (the M9
        # fourth gate or the re-asserted verify_tenant_scope): one narrow
        # 403, mirroring the materialization handler's
        # MaterializationSecurity + TenantScopeViolation -> 403 mapping.
        # No write happened.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access: this acquisition crosses a tenant security boundary",
        ) from None
    result = AcquisitionOut.from_outcome(outcome, project_id=project.id, repo_id=repo.id, agent_id=agent.id)
    if outcome.state != "acquired":
        # pending / failed: a 409 carrying the same AcquisitionOut body,
        # the way materialize serves PARTIAL / FAILED (design §C.2).
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.get("/{project_id}/repositories/{repo_id}/acquire/{agent_id}", response_model=AcquisitionOut)
async def acquire_repository_status(
    project_id: uuid.UUID,
    repo_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reconstruct a git repository's stored acquisition result.

    Reads the locator metadata (``acq_artifact`` / ``acq_result`` /
    ``resolved_rev`` ...) and the repo's verified / pending-verifier /
    retry marks; it performs no git work.  A not-yet-attempted repo is a
    ``pending`` with no code.  200 always — status reads have no conflict
    semantics (a ``failed`` state is data, not a transport error).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    repo = _find_repo(project, repo_id)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)  # 404/403 propagate
    outcome = await git_acquisition_service.status(db, repo=repo, agent=agent)
    return AcquisitionOut.from_outcome(outcome, project_id=project.id, repo_id=repo.id, agent_id=agent.id)


# ---------------------------------------------------------------------------
# Project Analysis (Phase 2C, docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2)
#
# The minimal Analysis persistence surface.  The target agent (launch only)
# is authorized by ``check_agent_access`` + the service's tenant gate, and
# the owning service (``analysis_service``) carries ALL policy: the closed
# AN_* codes, the revision binding, the append-only versioning invariant,
# and the stage-11 static-only boundary.  The handlers are pure transport
# adapters that map the outcome to a status (the acquire-route pattern).
# ---------------------------------------------------------------------------


async def _load_authorized_run(
    db: AsyncSession,
    project: Project,
    run_id: uuid.UUID,
) -> AnalysisRun:
    """Fetch a run row tenant-scoped, confirming it belongs to the project.

    A run that does not exist in the acting tenant, or that belongs to a
    different project, is a 404 (tenant invisibility, never a disclosure).
    """
    run = await analysis_run_dao.get_scoped(run_id, db=db)
    if run is None or run.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis run not found")
    return run


@router.post(
    "/{project_id}/repositories/{repo_id}/analyze/{agent_id}",
    response_model=AnalysisLaunchOut,
    status_code=status.HTTP_201_CREATED,
)
async def launch_analysis(
    project_id: uuid.UUID,
    repo_id: uuid.UUID,
    agent_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Open one analysis run bound to the source's resolved revision.

    Status mapping (design §9.2):
      - 201 on ``launched`` — a new AN_OPEN run + the ANALYZING transition;
      - 200 on ``existing`` — the revision already has a run (the UNIQUE
        invariant's re-read; nothing clobbered);
      - 409 on ``failed`` — a closed AN_* code (not-INITIALIZED, a source
        without a stored revision, ...).

    The handler NEVER triggers a downstream Agent / Run / prompt /
    execution: it persists the revision binding + findings and stops
    (stage 11: the analysis path is static-only).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    repo = _find_repo(project, repo_id)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)  # 404/403 propagate
    try:
        outcome = await analysis_service.launch(
            db,
            project=project,
            repo=repo,
            agent=agent,
            current_user=current_user,
        )
    except AnalysisSecurity:
        # A tenant-isolation / scope violation at the entry gate (the M9
        # fourth gate or the re-asserted verify_tenant_scope): one narrow
        # 403, mirroring the acquisition handler's mapping.  No write.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access: this analysis crosses a tenant security boundary",
        ) from None
    result = AnalysisLaunchOut.from_outcome(outcome, project_id=project.id, repo_id=repo.id, agent_id=agent.id)
    if outcome.state == "existing":
        response.status_code = status.HTTP_200_OK
    elif outcome.state == "failed":
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.post(
    "/{project_id}/analysis/{analysis_run_id}/findings",
    response_model=FindingsOut,
    status_code=status.HTTP_201_CREATED,
)
async def record_analysis_findings(
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    data: FindingsIn,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record the transient findings of an open analysis run, then close it.

    201 on success (the run is stamped AN_COMPLETED); 409 on a closed AN_*
    code (a terminal run refuses findings; an out-of-set / unbounded /
    evidence-less finding is rejected before any write — README-as-truth is
    forbidden).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    run = await _load_authorized_run(db, project, analysis_run_id)
    try:
        findings, run = await analysis_service.record_findings(
            db,
            project=project,
            run=run,
            findings_in=[f.model_dump() for f in data.findings],
            current_user=current_user,
        )
    except AnalysisError as exc:
        response.status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message, "retryable": False},
        ) from None
    return FindingsOut(
        run_id=run.id,
        run_status=run.status,
        findings=[FindingOut.from_finding(f) for f in findings],
    )


@router.post(
    "/{project_id}/analysis/{analysis_run_id}/promote",
    response_model=KnowledgeOut,
    status_code=status.HTTP_201_CREATED,
)
async def promote_analysis_finding(
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    data: KnowledgeIn,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote a confirmed finding into durable project knowledge.

    On human/company confirmation the knowledge row is written CONFIRMED
    with the provenance copied (``source_analysis_run_id``); the row is
    revision-independent (a later analysis at a different commit never
    invalidates it).  201 on success; 409 on a closed AN_* code.
    """
    project = await _load_authorized_project(db, project_id, current_user)
    run = await _load_authorized_run(db, project, analysis_run_id)
    try:
        knowledge = await analysis_service.promote_finding(
            db,
            project=project,
            run=run,
            subject=data.subject,
            statement=data.statement,
            current_user=current_user,
        )
    except AnalysisError as exc:
        response.status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message, "retryable": False},
        ) from None
    return KnowledgeOut.from_knowledge(knowledge)


@router.get("/{project_id}/analysis", response_model=AnalysisReadOut)
async def read_analysis(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The read path: the CURRENT run + its findings, and the HISTORY.

    "current" = the newest run; "history" = all prior rows (append-only
    versioning — a re-analysis at a new commit is a new row, never a
    clobber).  A never-analyzed project reads back as current=null + empty
    history (data, not an error — 200 always).
    """
    project = await _load_authorized_project(db, project_id, current_user)
    view = await analysis_service.read_current_and_history(db, project=project, current_user=current_user)
    return AnalysisReadOut(
        current=AnalysisRunOut.from_run(view["current"]) if view["current"] is not None else None,
        current_findings=[FindingOut.from_finding(f) for f in view["current_findings"]],
        history=[AnalysisRunOut.from_run(run) for run in view["history"]],
    )


@router.get("/{project_id}/knowledge", response_model=list[KnowledgeOut])
async def read_project_knowledge(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """All durable, revision-independent knowledge rows for a project."""
    project = await _load_authorized_project(db, project_id, current_user)
    rows = await analysis_service.knowledge(db, project=project, current_user=current_user)
    return [KnowledgeOut.from_knowledge(row) for row in rows]



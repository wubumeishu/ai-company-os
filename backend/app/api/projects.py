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
from app.dao.project_intake_dao import project_dao
from app.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.project_intake import (
    MaterializationOut,
    MaterializeRequest,
    ProjectIntakeCreate,
    ProjectOut,
    RejectionInfo,
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

"""Tenant-scoped DAOs for the Project Intake domain.

Phase 2B-2 Intake persistence, per docs/INTAKE_ARCHITECTURE_BRIEF_V1.md §3.3:
``ProjectDAO`` / ``RepositoryDAO`` inherit ``TenantScopedBaseDAO`` so that the
active tenant (bound by ``TenantContextMiddleware`` on every request) filters
all reads automatically and ``add_scoped`` forces tenant alignment on writes.

Layering rules honored (backend/app/dao/AGENTS.md):
- No business logic in this module — only DB reads, writes, and scoped joins
  within the Project <-> Repository domain boundary.
- ``status`` is mutated exclusively by ``ProjectIntakeService`` (via
  ``transition``), so no bare status-assignment path exists in the DAO layer
  (state-machine invariant: status may only move through the owning service).
- DAOs flush; the owning service/transaction commits.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dao.base import TenantScopedBaseDAO
from app.models.project import Project, Repository


class ProjectDAO(TenantScopedBaseDAO[Project]):
    """Tenant-scoped DAO for Project rows.

    Reads are scoped to the active tenant via the ContextVar; for
    platform-admin cross-tenant queries use the parent ``BaseDAO`` methods and
    annotate the call site with ``# arch-guard: allow (platform_admin cross-tenant)``.
    """

    def __init__(self) -> None:
        super().__init__(Project)

    async def get_scoped_with_repositories(self, project_id: uuid.UUID, db=None) -> Project | None:
        """Fetch a project with its repositories eagerly loaded (selectinload, no N+1)."""
        tenant_id = self._require_tenant_id()
        if tenant_id is None:
            # No tenant context: fall back to an unscoped fetch (tests / tooling).
            if db is None:
                async with self.session(readonly=True) as session_db:
                    stmt = select(Project).where(Project.id == project_id)
                    return (await session_db.execute(stmt)).scalar_one_or_none()
            stmt = select(Project).where(Project.id == project_id)
            return (await db.execute(stmt)).scalar_one_or_none()
        async with self.session(db=db, readonly=True) as session_db:
            stmt = (
                select(Project)
                .where(Project.id == project_id, Project.tenant_id == tenant_id)
                .options(selectinload(Project.repositories))
            )
            return (await session_db.execute(stmt)).scalar_one_or_none()

    async def list_for_user_scoped(
        self,
        *,
        user_id: uuid.UUID,
        is_admin: bool = False,
        skip: int = 0,
        limit: int = 100,
        db=None,
    ) -> Sequence[Project]:
        """List projects the user may read.

        Creators always see their own projects; admins see every project in
        their tenant. Non-admins are limited to projects they created.
        """
        tenant_id = self._require_tenant_id()
        stmt = select(Project)
        if tenant_id is not None:
            stmt = stmt.where(Project.tenant_id == tenant_id)
        if not is_admin:
            stmt = stmt.where(Project.created_by == user_id)
        stmt = (
            stmt.options(selectinload(Project.repositories))
            .order_by(Project.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def add_project_with_repositories(
        self,
        project: Project,
        repositories: Sequence[Repository],
        *,
        tenant_id: uuid.UUID,
        db=None,
    ) -> Project:
        """Register a new project and its source rows atomically in one tenant scope.

        ``project`` and every repository row are written with the same tenant
        and flushed together so a create is never half-persisted. When ``db``
        is the caller's request session the flush rides that transaction
        (committed by the request exit path); when it is None the DAO opens
        its own session and auto-commits (background/worker callers).

        The returned project's ``repositories`` relationship and every
        server-side column are re-fetched in the session's async context, so
        API views that serialize it never trigger a lazy load or an expired
        attribute refresh (an AsyncSession would raise ``MissingGreenlet``).
        """
        async with self.session(db=db) as session_db:
            self.add_scoped(session_db, project, tenant_id=tenant_id)
            for repo in repositories:
                self._add_repository_scoped(session_db, repo, tenant_id=tenant_id)
            await session_db.flush()
            # The INSERT flush expires every server-generated column
            # (created_at / updated_at) on the in-memory objects.  Re-load
            # them so the API view carries concrete values; a plain read of
            # an expired attribute on an AsyncSession would otherwise raise
            # MissingGreenlet on the request greenlet.
            await session_db.refresh(project)
            # Then load the one-to-many relationship in the session's async
            # context (same eager shape get_scoped_with_repositories returns);
            # the API view must never lazy-load it.
            await session_db.refresh(project, attribute_names=["repositories"])
        return project

    def _add_repository_scoped(self, db, repo: Repository, *, tenant_id: uuid.UUID) -> None:
        if getattr(repo, "tenant_id", None) is not None and repo.tenant_id != tenant_id:
            raise RuntimeError("Object tenant_id does not match the write tenant scope")
        repo.tenant_id = tenant_id
        db.add(repo)

    async def transition(self, project: Project, new_status: str, *, db=None) -> None:
        """Apply a status transition to a project, stamping status_changed_at.

        Transition *legality* is the service's responsibility — the DAO
        persists whatever status the service decided. This is the ONLY status
        write path the DAO exposes.
        """
        async with self.session(db=db) as session_db:
            project.status = new_status
            project.status_changed_at = datetime.now(UTC)
            session_db.add(project)
            await session_db.flush()
            # ``updated_at`` is a server-side onupdate: the flush above expires
            # it on the in-memory object, and a later plain read (e.g. the
            # API view) would then refresh outside the greenlet (MissingGreenlet
            # on an AsyncSession).  Re-load it here, inside the session.
            await session_db.refresh(project, attribute_names=["updated_at"])

    async def reject(
        self,
        project: Project,
        *,
        reason_code: str,
        detail: str | None,
        db=None,
    ) -> None:
        """Persist a terminal REJECTED transition with its reason code + detail.

        reason_code must come from the service's closed set; the DAO does not
        re-validate that contract.
        """
        async with self.session(db=db) as session_db:
            project.status = "REJECTED"
            project.status_changed_at = datetime.now(UTC)
            project.rejection_reason = reason_code
            project.rejection_detail = detail
            session_db.add(project)
            await session_db.flush()
            # Re-load the server-side onupdate timestamp inside the session
            # (see transition): avoids a lazy refresh on the request greenlet.
            await session_db.refresh(project, attribute_names=["updated_at"])

    async def mark_sources_ok(self, project: Project, *, db=None) -> None:
        """Persist the RECEIVED/SOURCES_OK -> SOURCES_OK transition."""
        async with self.session(db=db) as session_db:
            project.status = "SOURCES_OK"
            project.status_changed_at = datetime.now(UTC)
            session_db.add(project)
            await session_db.flush()
            # Re-load the server-side onupdate timestamp inside the session
            # (see transition): avoids a lazy refresh on the request greenlet.
            await session_db.refresh(project, attribute_names=["updated_at"])


class RepositoryDAO(TenantScopedBaseDAO[Repository]):
    """Tenant-scoped DAO for Repository rows (the source/asset registry)."""

    def __init__(self) -> None:
        super().__init__(Repository)

    async def list_by_project_scoped(self, project_id: uuid.UUID, db=None) -> Sequence[Repository]:
        """Fetch all repositories for a project, tenant-scoped (single query)."""
        tenant_id = self._require_tenant_id()
        stmt = select(Repository).where(Repository.project_id == project_id)
        if tenant_id is not None:
            stmt = stmt.where(Repository.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def mark_verified(self, repo: Repository, *, db=None) -> None:
        """Flag a repository row as verified, stamping verified_at (UTC)."""
        async with self.session(db=db) as session_db:
            repo.verified = True
            repo.verified_at = datetime.now(UTC)
            session_db.add(repo)
            await session_db.flush()
            # The flush expires every repo attribute (incl. the server-side
            # updated_at); reload inside the session so the API view that
            # serializes this row never triggers a lazy refresh.
            await session_db.refresh(repo)

    async def mark_pending_verifier(self, repo: Repository, *, db=None) -> None:
        """Set the pending-verifier mark on a repository (git-source hold, §4.4)."""
        async with self.session(db=db) as session_db:
            repo.pending_verifier = True
            session_db.add(repo)
            await session_db.flush()
            # Reload the row's attributes (incl. the server-side updated_at)
            # so the API view can serialize it without a lazy refresh.
            await session_db.refresh(repo)

    async def clear_pending_verifier(self, repo: Repository, *, db=None) -> None:
        """Clear the pending-verifier mark once a source is fully verified."""
        async with self.session(db=db) as session_db:
            repo.pending_verifier = False
            session_db.add(repo)
            await session_db.flush()
            # Reload the row's attributes (incl. the server-side updated_at)
            # so the API view can serialize it without a lazy refresh.
            await session_db.refresh(repo)

    async def bump_retry_count(self, repo: Repository, *, db=None) -> int:
        """Increment the bounded retry counter and return the new value."""
        async with self.session(db=db) as session_db:
            repo.retry_count = int(repo.retry_count or 0) + 1
            session_db.add(repo)
            await session_db.flush()
            # Reload the row's attributes (incl. the server-side updated_at)
            # so the API view can serialize it without a lazy refresh.
            await session_db.refresh(repo)
        return int(repo.retry_count or 0)


project_dao = ProjectDAO()
repository_dao = RepositoryDAO()

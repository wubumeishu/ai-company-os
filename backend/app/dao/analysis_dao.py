"""Tenant-scoped DAOs for the Phase 2C Analysis persistence domain.

Per docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2 the three Analysis tables
(``analysis_runs`` / ``analysis_findings`` / ``project_knowledge``) are
reached ONLY through ``TenantScopedBaseDAO`` + ``verify_tenant_scope``
(the M9 fourth gate).  All DAOs here inherit ``TenantScopedBaseDAO``: the
active tenant (bound by ``TenantContextMiddleware`` on every request)
filters reads automatically, and ``add_scoped`` forces tenant alignment on
writes (``app/dao/AGENTS.md``).

Layering rules honored (backend/app/dao/AGENTS.md):
- No business logic in this module — only DB reads, writes, and scoped
  queries within the Project -> AnalysisRun -> Finding/Knowledge boundary.
- ``AnalysisRun.status`` and ``Project.status`` (the ANALYZING transition)
  are mutated exclusively by ``analysis_service`` (the owning service); the
  DAO persists the decided value and stamps the owning timestamp.
- DAOs flush; the owning service/transaction commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.dao.base import TenantScopedBaseDAO
from app.models.analysis import AnalysisFinding, AnalysisRun, ProjectKnowledge


class AnalysisRunDAO(TenantScopedBaseDAO[AnalysisRun]):
    """Tenant-scoped DAO for analysis_runs rows (versioning + revision binding).

    Reads are scoped to the active tenant via the ContextVar; for
    platform-admin cross-tenant queries use the parent ``BaseDAO`` methods
    and annotate the call site with ``# arch-guard: allow (platform_admin cross-tenant)``.
    """

    def __init__(self) -> None:
        super().__init__(AnalysisRun)

    async def get_for_project_and_revision(
        self,
        project_id: uuid.UUID,
        revision_sha: str,
        db=None,
    ) -> AnalysisRun | None:
        """Fetch the (unique) run bound to one (project, revision) pair.

        The ``UNIQUE(project_id, revision_sha)`` invariant (design §9.2)
        makes this lookup a single-row read; it is the concurrency guard's
        re-read path when a racing launch loses the insert race.
        """
        tenant_id = self._require_tenant_id()
        if tenant_id is None:
            if db is None:
                async with self.session(readonly=True) as session_db:
                    stmt = select(AnalysisRun).where(
                        AnalysisRun.project_id == project_id,
                        AnalysisRun.revision_sha == revision_sha,
                    )
                    return (await session_db.execute(stmt)).scalar_one_or_none()
            stmt = select(AnalysisRun).where(
                AnalysisRun.project_id == project_id,
                AnalysisRun.revision_sha == revision_sha,
            )
            return (await db.execute(stmt)).scalar_one_or_none()
        async with self.session(db=db, readonly=True) as session_db:
            stmt = select(AnalysisRun).where(
                AnalysisRun.project_id == project_id,
                AnalysisRun.revision_sha == revision_sha,
                AnalysisRun.tenant_id == tenant_id,
            )
            return (await session_db.execute(stmt)).scalar_one_or_none()

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[AnalysisRun]:
        """All runs for a project, newest first (single query).

        "current" = the first row; "history" = all prior rows (design §9.2
        versioning: the table is append-only, history is never clobbered).
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(AnalysisRun)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.started_at.desc(), AnalysisRun.created_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(AnalysisRun.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()

    async def open_run(
        self,
        run: AnalysisRun,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> AnalysisRun:
        """Register one open run in the caller's tenant scope + transaction.

        The flush rides the caller's session (committed by the owning
        service's transaction).  The flush may raise ``IntegrityError`` on
        the ``UNIQUE(project_id, revision_sha)`` invariant (a racing
        launch); the caller owns the re-read recovery.
        """
        self.add_scoped(db, run, tenant_id=tenant_id)
        await db.flush()
        # The INSERT flush expires every server-generated column
        # (created_at / updated_at / started_at) on the in-memory object; a
        # later plain read on the request greenlet would raise MissingGreenlet
        # on an AsyncSession.  Re-load them inside the session (the
        # project_dao.create contract).
        await db.refresh(run, attribute_names=["created_at", "updated_at", "started_at"])
        return run

    async def close_run(
        self,
        run: AnalysisRun,
        *,
        new_status: str,
        db=None,
    ) -> None:
        """Stamp a terminal status + the finished_at window bound together."""
        from datetime import UTC, datetime

        async with self.session(db=db) as session_db:
            run.status = new_status
            run.finished_at = datetime.now(UTC)
            session_db.add(run)
            await session_db.flush()
            await session_db.refresh(run, attribute_names=["updated_at"])


class AnalysisFindingDAO(TenantScopedBaseDAO[AnalysisFinding]):
    """Tenant-scoped DAO for analysis_findings rows (transient, run-owned).

    Findings die with their run (``ON DELETE CASCADE`` on analysis_run_id)
    — the transient-vs-durable boundary of design §9.2.
    """

    def __init__(self) -> None:
        super().__init__(AnalysisFinding)

    async def add_findings(
        self,
        findings: Sequence[AnalysisFinding],
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> Sequence[AnalysisFinding]:
        """Register findings for one open run in the caller's transaction."""
        for finding in findings:
            self.add_scoped(db, finding, tenant_id=tenant_id)
        await db.flush()
        # The INSERT flush expires the server-generated columns on the
        # in-memory objects; re-load them inside the session so the caller's
        # view (the record-findings outcome) never triggers a greenlet
        # refresh on the request greenlet.
        for finding in findings:
            await db.refresh(finding, attribute_names=["created_at", "updated_at"])
        return list(findings)

    async def list_for_run(
        self,
        analysis_run_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[AnalysisFinding]:
        """All findings owned by one run, oldest first (single query)."""
        tenant_id = self._require_tenant_id()
        stmt = (
            select(AnalysisFinding)
            .where(AnalysisFinding.analysis_run_id == analysis_run_id)
            .order_by(AnalysisFinding.created_at.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(AnalysisFinding.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()


class ProjectKnowledgeDAO(TenantScopedBaseDAO[ProjectKnowledge]):
    """Tenant-scoped DAO for project_knowledge rows (durable, confirmed).

    A knowledge row is NOT invalidated by a later analysis at a different
    commit (design §9.2): its provenance (``source_analysis_run_id``) is
    copied, not owned.
    """

    def __init__(self) -> None:
        super().__init__(ProjectKnowledge)

    async def add_knowledge(
        self,
        knowledge: ProjectKnowledge,
        *,
        tenant_id: uuid.UUID,
        db,
    ) -> ProjectKnowledge:
        """Register one confirmed knowledge row in the caller's transaction."""
        self.add_scoped(db, knowledge, tenant_id=tenant_id)
        await db.flush()
        # Re-load the server-generated timestamps inside the session (same
        # contract as add_findings): the promote outcome view must never
        # trigger a greenlet refresh on the request greenlet.
        await db.refresh(knowledge, attribute_names=["created_at", "updated_at"])
        return knowledge

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        db=None,
        limit: int = 100,
    ) -> Sequence[ProjectKnowledge]:
        """All durable knowledge for a project, newest first (single query)."""
        tenant_id = self._require_tenant_id()
        stmt = (
            select(ProjectKnowledge)
            .where(ProjectKnowledge.project_id == project_id)
            .order_by(ProjectKnowledge.created_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(ProjectKnowledge.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            return (await session_db.execute(stmt)).scalars().all()


analysis_run_dao = AnalysisRunDAO()
analysis_finding_dao = AnalysisFindingDAO()
project_knowledge_dao = ProjectKnowledgeDAO()

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB

from app.dao.base import TenantScopedBaseDAO
from app.models.audit import AuditLog


class AuditLogDAO(TenantScopedBaseDAO[AuditLog]):
    """Tenant-scoped reads over audit rows.

    Phase 2E V1 (docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md §6.3/§8): the
    intake service owns the AuditLog writes (one row per Execute outcome);
    the §6.3 soft retry cap ("V1 cap = 3 retries per Task per day, recorded
    in audit, enforced by the API audit count — NOT by queue machinery")
    needs exactly one bounded, tenant-scoped COUNT read.  This module gives
    the owning service that read; it carries no policy (the 3-cap and the
    closed code set live in the service layer).

    The ``details`` column is a plain PG ``json``; the JSONB cast makes
    ``jsonb_extract_path_text`` available for the bounded string extract.
    """

    def __init__(self) -> None:
        super().__init__(AuditLog)

    async def count_task_audit(
        self,
        task_id: uuid.UUID,
        *,
        action: str,
        since: datetime,
        db=None,
    ) -> int:
        """One bounded COUNT: audit rows for one task + action since ``since``.

        The §6.3 transient-retry cap reads the per-task, per-window count of
        ``task_execute_retried`` rows this way — a single indexed COUNT
        (``action`` + ``created_at`` + tenant predicate), never a sweep of
        other tenants' rows.
        """
        tenant_id = self._require_tenant_id()
        stmt = (
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == action,
                AuditLog.created_at >= since,
                func.jsonb_extract_path_text(AuditLog.details.cast(JSONB), "task_id") == str(task_id),
            )
        )
        if tenant_id is not None:
            stmt = stmt.where(AuditLog.tenant_id == tenant_id)
        async with self.session(db=db, readonly=True) as session_db:
            result = await session_db.execute(stmt)
            return int(result.scalar_one() or 0)


audit_log_dao = AuditLogDAO()

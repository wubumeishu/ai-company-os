"""Project and Repository persistence models (Phase 2A R1 birth model).

Project = the business object of one whole thing the company has taken on
(a single order), per docs/PHASE2A_PROJECT_DESIGN_V1.md §A. Repository is
its source/asset registry object (where the material lives and how to reach
it) and is NOT guaranteed to be a Git repository (design §F.1a naming
boundary). One Project owns N (>=0) Repository rows via a real FK.

Design constraints honored here (and nowhere else in this card):
- Project fields are exactly §D.1 Must Have; the §D.3 excluded list
  (clone_url/branch/commit, workspace paths, analysis JSON, task lists,
  Run refs, budget/OKR) must not be added to this model.
- Repository is the minimal §F.1 shape: source_type + per-type locator
  JSON + verification result. No branch/commit/credential/provider
  columns (minimalism rule, audit §B.2).
- Both models carry a non-nullable ``tenant_id`` so they are tenant-owned
  by schema and are automatically picked up by the ``do_orm_execute``
  tenant filter in app/dao/base.py (``_is_tenant_scoped_model``) without
  any opt-in flag.
- Task/Agent/Workspace/Execution/Artifact/Review/Scheduler models stay
  untouched (§G.1: the V1 task graph does not add project fields to Task).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Project(Base):
    """A project the company has formally taken on (business-level, not runtime).

    status is the §C lifecycle: the entity is born at Intake acceptance
    (RECEIVED); BLOCKED is an execution state only (EXECUTING -> BLOCKED);
    COMPLETED / ARCHIVED / REJECTED are the terminal groups.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Enum(
            "RECEIVED",
            "SOURCES_OK",
            "INITIALIZED",
            "ANALYZING",
            "PENDING_CONFIRMATION",
            "EXECUTING",
            "BLOCKED",
            "COMPLETED",
            "ARCHIVED",
            "REJECTED",
            name="project_status_enum",
            create_constraint=False,
        ),
        default="RECEIVED",
        nullable=False,
    )

    # Ownership
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Repository(Base):
    """Source/asset registry row belonging to exactly one Project.

    ``Repository`` is a registry of where a project's material lives and how
    to reach it; it does not imply the underlying source is a Git repository
    (design §F.1a). The source_type values github/gitlab/local_git are
    reserved for the Phase 2B git-fetch batch (design §F.3): registering such
    sources is allowed, but their reachability verifiers do not exist yet, so
    V1 projects carrying them stay in RECEIVED/SOURCES_OK with a
    pending-verifier mark.
    """

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "manual",
            "local_folder",
            "document",
            "zip",
            "github",
            "gitlab",
            "local_git",
            name="repository_source_type_enum",
            create_constraint=False,
        ),
        default="manual",
        nullable=False,
    )
    # Per source_type structured locator, e.g. {"path": "..."} for
    # local_folder/document/zip, {"owner": "...", "repo": "..."} for
    # github/gitlab. Free-form dict on purpose: adding a new source type
    # must not require a schema change (design §F.1).
    locator: Mapped[dict | None] = mapped_column(JSON, default=None)
    display_name: Mapped[str | None] = mapped_column(String(200))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="repositories")


# Resolve forward refs
from app.models.user import User

"""Task models for digital employees.

Phase 2D (docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §3/§4) extends the
legacy Task row with a five-column provenance set (``project_id`` /
``analysis_run_id`` / ``finding_id`` / ``revision_sha`` /
``created_reason``) and adds the V1 Task Graph edge table
``task_dependencies`` so a Task's origin and its explicit dependencies are
persisted.  The edge table follows the Phase 2B/2C physical-FK precedent
(f066–f068, ADR-1): real ``ForeignKeyConstraint`` + guarded idempotent DDL.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

#: tasks.created_reason — a CLOSED 3-value set (design §4.1).  Persisted as the
#: PG enum ``task_created_reason_enum`` by the f069 migration; the service layer
#: re-validates against this closed set so a bad value fails closed before any
#: row is written.  Answers "why was this Task created?": MANUAL (a user/agent
#: made it directly), ANALYSIS_FINDING (derived from one finding — requires the
#: full analysis provenance), ANALYSIS_PLANNING (derived from an analysis
#: context, no single finding — finding_id may be NULL).
TASK_CREATED_REASONS = ("MANUAL", "ANALYSIS_FINDING", "ANALYSIS_PLANNING")


class Task(Base):
    """Task assigned to or managed by a digital employee."""

    __tablename__ = "tasks"
    __tenant_scoped__ = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(
        Enum("todo", "supervision", name="task_type_enum", create_constraint=False),
        default="todo",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "doing", "done", name="task_status_enum"),
        default="pending",
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        Enum("low", "medium", "high", "urgent", name="task_priority_enum"),
        default="medium",
        nullable=False,
    )
    assignee: Mapped[str] = mapped_column(String(50), default="self")  # "self" or user_id
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Supervision specific fields
    supervision_target_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    supervision_target_name: Mapped[str | None] = mapped_column(String(100))
    supervision_channel: Mapped[str | None] = mapped_column(String(50))
    remind_schedule: Mapped[str | None] = mapped_column(String(100))

    # Phase 2D provenance (design §4): where/why this Task came from.  All
    # nullable so the legacy manual path is untouched (created_reason defaults
    # to MANUAL with the analysis columns NULL).
    # project_id — CASCADE: a Task is subordinated to its Project context
    # (design §4.1; deleting a Project removes its Tasks, mirroring findings
    # CASCADE on runs in f068).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # analysis_run_id — SET NULL: a run row may be deleted/re-analyzed without
    # destroying a durable execution intent; provenance is "copied, not owned"
    # (f068 knowledge source_analysis_run_id precedent).
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # finding_id — SET NULL: findings are transient (die with their run); a
    # Task is a durable intent and must not be reverse-destroyed.
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_findings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # revision_sha — denormalized snapshot of the analysis run's revision
    # (design §4.1): the highest-frequency traceability query ("which Git rev
    # was this against?") is join-free, and it survives a SET NULL on the
    # upstream run/finding rows.
    revision_sha: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # created_reason — closed 3-value enum.  server_default (not just a
    # Python-side default) so the create_all-provisioned fresh-DB path and the
    # f069 migration path agree on the DDL: every existing/new Task row is
    # MANUAL by definition with no data backfill (f068 index-lockstep lesson
    # applied to the column default).
    created_reason: Mapped[str] = mapped_column(
        Enum(*TASK_CREATED_REASONS, name="task_created_reason_enum", create_constraint=False),
        default="MANUAL",
        server_default="MANUAL",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="tasks")
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    logs: Mapped[list["TaskLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskDependency(Base):
    """One directed edge of the V1 Task Graph: ``task_id`` depends on
    ``depends_on_task_id`` (arrow points at the dependency / upstream).

    Phase 2D minimum model (docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md
    §3): explicit dependencies, self-edge prevention (DB CHECK + service),
    bounded blocked/ready computation.  No edge attributes, no workflow
    semantics.  Both ends CASCADE so the graph never dangles.
    """

    __tablename__ = "task_dependencies"
    __tenant_scoped__ = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # Dependee (downstream) / depended-on (upstream).  Both CASCADE: deleting a
    # Task removes every edge referencing it.
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_depends_pair"),
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dep_no_self"),
    )


class TaskLog(Base):
    """Progress log entry for a task."""

    __tablename__ = "task_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="logs")


# Resolve forward refs
from app.models.agent import Agent  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401

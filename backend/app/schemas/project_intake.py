"""Pydantic schemas for the Project Intake API (Phase 2B-2).

Per docs/INTAKE_ARCHITECTURE_BRIEF_V1.md §3.2 the Intake schema follows the
same conventions as the rest of ``app/schemas``: Pydantic v2 ``BaseModel``
subclasses, ``from_attributes`` output models, and ``Field`` constraints on
create models. The ``locator`` shape is validated per ``source_type`` so that
the host-path vs storage-key ambiguity is rejected at request-validation time
(422), not at storage time (brief §4.5 / UNKNOW 5).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

# ─── Locator source types ───────────────────────────────────────────────────

SUPPORTED_SOURCE_TYPES = ("manual", "local_folder", "document", "zip")
# github / gitlab / local_git are reserved for the Phase 2B git-fetch batch;
# V1 keeps them registerable (the enum already has all 7 values) and the
# validate endpoint returns SOURCE_NOT_SUPPORTED for them (brief §4.4).
GIT_SOURCE_TYPES = ("github", "gitlab", "local_git")
KNOWN_SOURCE_TYPES = SUPPORTED_SOURCE_TYPES + GIT_SOURCE_TYPES


class SourceSpec(BaseModel):
    """One Intake source declaration.

    ``locator`` is the structured per-type locator (brief §3.2 table):
      - manual:        ``{}`` or ``None``
      - local_folder:  ``{"path": "<absolute host path>"}``
      - document/zip:  ``{"path": "..."}`` XOR ``{"storage_key": "..."}``
      - git types:     free-form (``{"owner": "...", "repo": "..."}``),
                       never dereferenced by V1 validators.
    """

    source_type: str = Field(description="manual | local_folder | document | zip | github | gitlab | local_git")
    locator: dict | None = Field(default=None, description="Structured locator for the source type")
    display_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_locator_shape(self) -> "SourceSpec":
        if self.source_type not in KNOWN_SOURCE_TYPES:
            raise ValueError(
                f"Unknown source_type {self.source_type!r}; expected one of {list(KNOWN_SOURCE_TYPES)}"
            )

        if self.source_type == "manual":
            # A manual source is a pure registration: no locator is required,
            # but if one is present it must be an empty dict (nothing else is
            # meaningful). The stored row keeps locator None/{} as-is.
            if self.locator not in (None, {}):
                raise ValueError("manual source must not carry a locator payload")
            return self

        if self.source_type == "local_folder":
            path = self._locator_str("path")
            if path is None:
                raise ValueError("local_folder source requires locator.path")
            if not path:
                raise ValueError("local_folder locator.path must be a non-empty string")
            return self

        if self.source_type in ("document", "zip"):
            # Host path XOR storage key: both-present or both-absent is an
            # ambiguous locator and is rejected at 422 (brief §4.5). The
            # locator may be None, so read both fields through the helper.
            path = self._locator_str("path")
            storage_key = self._locator_str("storage_key")
            has_path = path is not None
            has_key = storage_key is not None
            if has_path and has_key:
                raise ValueError(
                    "document/zip source must specify either locator.path or "
                    "locator.storage_key, not both"
                )
            if not has_path and not has_key:
                raise ValueError(
                    "document/zip source requires exactly one of "
                    "locator.path or locator.storage_key"
                )
            if has_path and not path:
                raise ValueError("document/zip locator.path must be a non-empty string")
            if has_key and not storage_key:
                raise ValueError(
                    "document/zip locator.storage_key must be a non-empty string"
                )
            return self

        # Git types: locator is free-form and never dereferenced by V1.
        return self

    def _locator_str(self, field: str) -> str | None:
        if not isinstance(self.locator, dict):
            return None
        value = self.locator.get(field)
        return value if isinstance(value, str) else None


class ProjectIntakeCreate(BaseModel):
    """Create-Intake request body (brief §3.2).

    name / description / goal are all mandatory (Phase 2A §E.2: the N=0
    source form is still a registered project; an Intake with zero sources
    is a valid "manual only" project).
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    sources: list[SourceSpec] = Field(default_factory=list)


# ─── Output models ──────────────────────────────────────────────────────────


class RepositoryOut(BaseModel):
    """API view of a source/asset registry row (brief §3.2 response shape)."""

    id: uuid.UUID
    source_type: str
    locator: dict | None = None
    display_name: str | None = None
    verified: bool
    verified_at: datetime | None = None
    pending_verifier: bool
    retry_count: int

    model_config = {"from_attributes": True}


class RejectionInfo(BaseModel):
    """The persisted rejection payload of a REJECTED project, or the
    transient rejection view of a pending-verifier / retryable outcome."""

    reason_code: str
    reason_detail: str | None = None
    failed_source_id: uuid.UUID | None = None
    retryable: bool = False
    retries_remaining: int | None = None


class ProjectOut(BaseModel):
    """API view of a Project (brief §3.2)."""

    id: uuid.UUID
    name: str
    description: str | None = None
    goal: str | None = None
    status: str
    repositories: list[RepositoryOut] = []
    rejection_info: RejectionInfo | None = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    status_changed_at: datetime | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_project(cls, project, rejection_info: RejectionInfo | None = None) -> "ProjectOut":
        """Build the out model from an ORM Project with eager repositories."""
        repositories = [
            RepositoryOut(
                id=repo.id,
                source_type=repo.source_type,
                locator=repo.locator,
                display_name=repo.display_name,
                verified=repo.verified,
                verified_at=repo.verified_at,
                pending_verifier=getattr(repo, "pending_verifier", False),
                retry_count=getattr(repo, "retry_count", 0),
            )
            for repo in getattr(project, "repositories", None) or []
        ]
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            goal=project.goal,
            status=project.status,
            repositories=repositories,
            rejection_info=rejection_info,
            created_by=project.created_by,
            created_at=project.created_at,
            updated_at=project.updated_at,
            status_changed_at=project.status_changed_at,
        )

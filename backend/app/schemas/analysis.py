"""Phase 2C Analysis transport schemas.

Transport validation models for the minimal Analysis persistence surface
(docs/PHASE_2C_PROJECT_ANALYSIS.md §9.2).  Request models bound every
client-reachable input BEFORE it reaches the owning service (the service
re-validates the closed sets — the same double-gate pattern the
project_intake schemas use); response models serialize the persisted rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Findings (transient, run-owned; every finding carries a closed tag +
# bounded traceable evidence — README-as-truth is forbidden).
# ---------------------------------------------------------------------------


class EvidenceIn(BaseModel):
    """The bounded traceable-evidence shape of one finding.

    ``anchors``: 1..100 ``path:line`` strings pointing at the source the
    finding is about (the provenance contract: every finding is
    traceable).  ``provenance``: an optional bounded source-card dict.
    """

    anchors: list[str] = Field(min_length=1, max_length=100)
    provenance: dict | None = None


class FindingIn(BaseModel):
    """One transient finding, recorded against an open analysis run."""

    category: str = Field(description="Closed set: SECURITY/RISK/TECH_DEBT/OPEN_QUESTION/FACT")
    tag: str = Field(description="Closed set: FACT/OBSERVATION/INFERENCE/UNKNOWN")
    summary: str = Field(min_length=1, max_length=4000)
    evidence: EvidenceIn
    severity: str = Field(default="INFO", description="Closed set: INFO/WARN/HIGH/CRITICAL")


class FindingsIn(BaseModel):
    """The record-findings request body (>=1 finding per record)."""

    findings: list[FindingIn] = Field(min_length=1, max_length=100)


class FindingOut(BaseModel):
    """A persisted transient finding of one analysis run."""

    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    analysis_run_id: uuid.UUID
    severity: str
    category: str
    tag: str
    summary: str
    evidence: dict | None
    created_at: datetime | None = None

    @classmethod
    def from_finding(cls, finding) -> FindingOut:
        return cls(
            id=finding.id,
            analysis_run_id=finding.analysis_run_id,
            severity=finding.severity,
            category=finding.category,
            tag=finding.tag,
            summary=finding.summary,
            evidence=finding.evidence,
            created_at=finding.created_at,
        )


class FindingsOut(BaseModel):
    """The record-findings outcome: the run's closed state + the findings."""

    run_id: uuid.UUID
    run_status: str
    findings: list[FindingOut]


# ---------------------------------------------------------------------------
# Runs (versioning + Git-revision binding).
# ---------------------------------------------------------------------------


class AnalysisRunOut(BaseModel):
    """A persisted analysis run, bound to exactly one revision."""

    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID | None
    revision_sha: str
    requested_ref: str | None = None
    resolved_at: datetime | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_run(cls, run) -> AnalysisRunOut:
        return cls(
            id=run.id,
            project_id=run.project_id,
            agent_id=run.agent_id,
            revision_sha=run.revision_sha,
            requested_ref=run.requested_ref,
            resolved_at=run.resolved_at,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class AnalysisLaunchOut(BaseModel):
    """The launch outcome (closed transport state set, design §9.2).

    ``state``:
      - ``launched`` — a new AN_OPEN run was opened + the project moved to
        ANALYZING (201);
      - ``existing`` — this revision already has a run (the UNIQUE
        invariant's re-read; nothing clobbered, 200);
      - ``failed``   — a closed AN_* code (409).
    """

    project_id: uuid.UUID
    repo_id: uuid.UUID
    agent_id: uuid.UUID
    state: str
    code: str | None = None
    message: str = ""
    revision_sha: str | None = None
    run: AnalysisRunOut | None = None

    @classmethod
    def from_outcome(cls, outcome, *, project_id: uuid.UUID, repo_id: uuid.UUID, agent_id: uuid.UUID) -> AnalysisLaunchOut:
        return cls(
            project_id=project_id,
            repo_id=repo_id,
            agent_id=agent_id,
            state=outcome.state,
            code=outcome.code,
            message=outcome.message,
            revision_sha=outcome.revision_sha,
            run=AnalysisRunOut.from_run(outcome.run) if outcome.run is not None else None,
        )


class AnalysisReadOut(BaseModel):
    """The read path: CURRENT run + its findings, and HISTORY (prior runs).

    Append-only versioning (design §9.2): ``current`` = the newest run;
    ``history`` = every prior run, never clobbered.  A project that was
    never analyzed reads back as current=null + empty history.
    """

    current: AnalysisRunOut | None
    current_findings: list[FindingOut]
    history: list[AnalysisRunOut]


# ---------------------------------------------------------------------------
# Knowledge (durable, confirmed, revision-independent).
# ---------------------------------------------------------------------------


class KnowledgeIn(BaseModel):
    """The promote-on-confirmation request body."""

    subject: str = Field(min_length=1, max_length=200, description='e.g. "backend framework"')
    statement: str = Field(min_length=1, max_length=10000, description='e.g. "backend uses FastAPI"')


class KnowledgeOut(BaseModel):
    """A durable project-knowledge row with its copied provenance."""

    id: uuid.UUID
    project_id: uuid.UUID
    subject: str
    statement: str
    source_analysis_run_id: uuid.UUID | None
    status: str
    created_at: datetime | None = None

    @classmethod
    def from_knowledge(cls, row) -> KnowledgeOut:
        return cls(
            id=row.id,
            project_id=row.project_id,
            subject=row.subject,
            statement=row.statement,
            source_analysis_run_id=row.source_analysis_run_id,
            status=row.status,
            created_at=row.created_at,
        )

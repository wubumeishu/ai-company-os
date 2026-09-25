"""Phase 2D Task Graph + Analysis→Task transport schemas.

The request/response models for the API lane (card t_b4a29991):

- ``TaskDependenciesIn``      — the bounded edge-authoring body (V1: a task's
  direct upstream dependencies, ≤ 100 per invocation, the service's
  ``MAX_BATCH_EDGES``).
- ``TaskEdgeOut`` / ``TaskGraphOut`` — the GET /graph payload.  The graph view
  carries the bounded dependency states **plus the Task's provenance** (design
  §4/§6: every graph read answers "why/where does this Task come from?").
- ``TaskConversionRequest`` / ``TaskConversionOut`` — the explicit
  Analysis→Task conversion body/result (PHASE_2D_ANALYSIS_TASK_MAPPING.md §5:
  the closed ``TD_*`` code + per-finding outcome data, never free text).

Transport-only: no business policy lives here.  The owning services
(``task_graph_service`` / ``task_decomposition_service``) produce the outcome
objects; these models just shape the response.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.schemas import TaskOut


class TaskDependenciesIn(BaseModel):
    """The bounded edge-authoring body for one task (design §6 API draft).

    One item = a single dependency (the POST serves add/remove-free bulk
    authoring); more = a bulk add.  The service enforces the 100-edge bound
    and every §5.1 closed-code gate (self / tenant / project / supervision /
    cycle / exists) — an oversized list is a GRAPH_INVALID rejection, not a
    truncated write.
    """

    depends_on_task_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class TaskEdgeOut(BaseModel):
    """One direct dependency of the graph's task (id + live status)."""

    id: uuid.UUID
    status: str | None


class TaskGraphOut(BaseModel):
    """GET /tasks/{task_id}/graph — the bounded per-task graph view.

    ``ready`` is the design §5.3 closed state: ``"ready"`` (every direct
    dependency is done, or there are none), ``"blocked"`` (some dependency
    unmet — see ``blocking``), ``"not_applicable"`` (supervision tasks carry
    no dependency edges, design §3.1).  ``task`` embeds the full TaskOut, so
    the response always carries Provenance + Status (task fields
    ``project_id`` / ``analysis_run_id`` / ``finding_id`` / ``revision_sha`` /
    ``created_reason`` / ``status``).
    """

    task: TaskOut
    ready: str
    direct_dependencies: list[TaskEdgeOut]
    blocking: list[uuid.UUID]


class TaskConversionRequest(BaseModel):
    """POST /projects/{project_id}/analysis/{run_id}/tasks — the explicit
    conversion invocation body (mapping spec §2 G1 / §3.3).

    ``agent_id`` is the mandatory executing agent: a converted Task lands in
    ``pending`` and only runs through the existing, separately-authorized
    manual trigger.  V1 has no implicit agent (TD_AGENT_REQUIRED) — the
    executing agent is a human decision, never a silent default.
    """

    agent_id: uuid.UUID


class TaskConversionOut(BaseModel):
    """The conversion result (mapping spec §5: closed code + data, no 2xx on
    failure; 201 on success with the created Task ids + per-finding statuses).

    - ``code`` is the closed ``TD_*`` set; ``state`` is ``"ok"`` / ``"failed"``.
    - ``per_finding`` values are the closed set
      ``converted | skipped_duplicate | planning_only`` — data, never prose.
    - ``status`` on a converted Task is ALWAYS ``"pending"`` (G4: conversion
      never enqueues; execution is a separate human act).
    """

    code: str
    state: str
    detail: str = ""
    project_id: uuid.UUID
    analysis_run_id: uuid.UUID
    agent_id: uuid.UUID
    status: str = "pending"  # G4 invariant of every created Task row
    converted_task_ids: list[uuid.UUID] = Field(default_factory=list)
    per_finding: dict[uuid.UUID, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)

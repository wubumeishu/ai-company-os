"""Phase 2D Analysis → Task decomposition service (the mapping boundary).

Per docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md this service is the SINGLE owned
boundary between the two layers Phase 2C deliberately kept apart:

    Analysis (understanding: analysis_runs + transient findings)
        │  ← boundary owner: TaskDecompositionService, invoked EXPLICITLY
        ▼
    Task (execution intent: a bounded unit owned by exactly one Agent)

Settled decisions honored (card t_3867a0f9, spec §1/§2/§3):
- **Conversion is explicit.** Nothing auto-converts on ``record_findings``.
  The owning lane (the API endpoint, t_b4a29991) calls ``convert`` as a
  separate human act; analysis stays a read/understanding lane.
- **Conversion never executes (G4).** A converted Task lands in
  ``status="pending"`` and is NEVER passed to ``enqueue_task_runtime``.
  "Create" and "run" are decoupled — that decoupling IS the V1 safety gate.
  Execution stays on the existing, separately-authorized manual trigger.
- **One owner.** Only this service may create analysis-provenance Task rows
  (``created_reason="ANALYSIS_FINDING"``).  The manual task API keeps creating
  ``MANUAL`` tasks with null provenance — no bypass path.
- **Fail closed.** Every gate failure returns a closed ``TD_*`` code (409-class,
  mirroring the AN_* pattern in ``analysis_service``); nothing is written.
  An unknown finding shape is NOT converted (default → planning class).
- **Flat task set (V1).** Conversion produces NO dependency edges — a Task
  depending on another converted Task is the Task Graph lane's concern.  This
  service fills the provenance columns (the §4.2 ``ANALYSIS_FINDING`` shape:
  project_id + analysis_run_id + finding_id + revision_sha, all mandatory).

The two halves are deliberately separated so each is independently verifiable:
``classify`` is a pure function over the closed 5×4×4 grid (unit-testable with
no database); ``convert`` is the bounded, transactional, tenant-scoped write
that consumes ``classify`` and the persistence lane's DAO primitives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.dao.analysis_dao import analysis_finding_dao
from app.dao.task_dao import task_provenance_dao
from app.models.agent import Agent
from app.models.analysis import AnalysisFinding, AnalysisRun
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.services import intake_security

# ---------------------------------------------------------------------------
# Closed result-code set (mirrors the AN_* closed-code pattern of
# analysis_service.py; the spec §5 codes).  A code outside the set is a
# programming error and is rejected at construction.
# ---------------------------------------------------------------------------

TD_OK = "TD_OK"
TD_RUN_NOT_COMPLETED = "TD_RUN_NOT_COMPLETED"
TD_INVALID_INPUT = "TD_INVALID_INPUT"
TD_AGENT_REQUIRED = "TD_AGENT_REQUIRED"
TD_AGENT_TENANT_MISMATCH = "TD_AGENT_TENANT_MISMATCH"

#: The closed transport code set (spec §5).  Read-back of persisted / status
#: data re-validates against it (the F1 pattern).
DECOMPOSITION_RESULT_CODES = frozenset(
    {TD_OK, TD_RUN_NOT_COMPLETED, TD_INVALID_INPUT, TD_AGENT_REQUIRED, TD_AGENT_TENANT_MISMATCH}
)

#: Per-finding outcome closed set (spec §5: "never free text").
PER_FINDING_OUTCOMES = frozenset({"converted", "skipped_duplicate", "planning_only"})

#: The V1 closed severity → priority map (spec §3.3).  INFO never reaches a
#: Task (rule P4 excludes it), so it maps to "low" only for completeness.
SEVERITY_TO_PRIORITY = {"CRITICAL": "urgent", "HIGH": "high", "WARN": "medium", "INFO": "low"}

#: G5 — the bounded output of one invocation, aligned to the analysis lane's
#: MAX_FINDINGS_PER_RECORD=100 (a run's findings are capped at 100, so a
#: whole-run conversion is naturally ≤ 100; this is the defensive guard).
MAX_TASKS_PER_INVOCATION = 100
#: The task title column width (``tasks.title`` is String(500)).
MAX_TASK_TITLE_CHARS = 500

#: The executable-class inclusion rules (spec §3.2) as closed data.
_EXECUTABLE_RULES = (
    ("TECH_DEBT", "FACT", {"WARN", "HIGH", "CRITICAL"}),  # E1
    ("SECURITY", "FACT", {"HIGH", "CRITICAL"}),  # E2
)
_PLANNING_CATEGORIES = frozenset({"OPEN_QUESTION", "RISK"})  # P1, P2
_LOW_CONFIDENCE_TAGS = frozenset({"INFERENCE", "UNKNOWN"})  # P3


class DecompositionSecurity(RuntimeError):
    """A tenant-isolation finding (403-class, never retried) — the M9 gate.

    Raised when the acting context / target agent crosses the tenant security
    boundary; maps to 403 at the transport (mirrors ``AnalysisSecurity``).
    """


class DecompositionError(Exception):
    """A closed TD_* outcome (409-class) with a stable code + detail.

    Client-reachable gate failures become a closed code carried by the
    outcome — they must never surface as a 500 (the fail-closed contract the
    AN_* codes carry).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if code not in DECOMPOSITION_RESULT_CODES:
            raise ValueError(f"decomposition code {code!r} is not in the closed result-code set")
        self.code = code
        self.message = message


@dataclass
class DecompositionOutcome:
    """The transport shape of a conversion attempt (mirrors AN_* outcomes).

    - ``state="ok"``   — the invocation ran; per-finding results in
      ``per_finding`` (each ``converted`` / ``skipped_duplicate`` /
      ``planning_only``), and the created Task ids.
    - ``state="failed"`` — a closed TD_* gate rejected the whole invocation
      before any write; ``code`` carries the reason.
    """

    state: str
    code: str | None = None
    detail: str = ""
    converted_task_ids: list[uuid.UUID] = field(default_factory=list)
    per_finding: dict[uuid.UUID, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def classify(category: str, severity: str, tag: str) -> str:
    """Pure, database-free classification of one finding (spec §3).

    Evaluates exclusions first, inclusions second, default = planning (fail
    closed): an unmapped shape NEVER becomes a Task on its own.  Returns the
    closed class ``"executable"`` or ``"planning"``.  The 5×4×4 closed grid
    yields EXACTLY the E1/E2 rows as executable (5 of 200):
    - E1: TECH_DEBT ∧ FACT ∧ severity ∈ {WARN, HIGH, CRITICAL}
    - E2: SECURITY ∧ FACT ∧ severity ∈ {HIGH, CRITICAL}
    """
    # Exclusions (P1–P4) — any match is planning-only.
    if category in _PLANNING_CATEGORIES:
        return "planning"
    if tag in _LOW_CONFIDENCE_TAGS:
        return "planning"
    if severity == "INFO":
        return "planning"
    # Inclusions (E1/E2) — the closed allow-list.
    for cat, face_tag, sev_set in _EXECUTABLE_RULES:
        if category == cat and tag == face_tag and severity in sev_set:
            return "executable"
    return "planning"  # P5 default (fail-closed)


def build_task_fields(
    finding: AnalysisFinding,
    run: AnalysisRun,
    *,
    agent_id: uuid.UUID,
    created_by: uuid.UUID,
) -> dict:
    """The §3.3 finding → Task field mapping for one executable finding.

    Returns the Task column values (NOT a persisted Task) so the mapping is
    pure + unit-testable: title (500-char truncation), description (summary +
    evidence anchors + provenance footer), the fixed type/status, the closed
    severity→priority map, the executing agent, and the mandatory
    ``ANALYSIS_FINDING`` provenance set.
    """
    title = f"[{finding.category}] {finding.summary}"
    if len(title) > MAX_TASK_TITLE_CHARS:
        title = title[:MAX_TASK_TITLE_CHARS]

    parts = [f"Analysis finding: {finding.summary}"]
    anchors = (finding.evidence or {}).get("anchors")
    if isinstance(anchors, list) and anchors:
        parts.append("Evidence: " + ", ".join(str(a) for a in anchors))
    parts.append(f"Provenance: analysis run {run.id} @ revision {run.revision_sha}")

    return {
        "agent_id": agent_id,
        "title": title,
        "description": "\n".join(parts),
        "type": "todo",
        "status": "pending",  # G4: NEVER enqueued at conversion
        "priority": SEVERITY_TO_PRIORITY[finding.severity],
        "created_by": created_by,
        "created_reason": "ANALYSIS_FINDING",
        "project_id": run.project_id,
        "analysis_run_id": run.id,
        "finding_id": finding.id,
        "revision_sha": run.revision_sha,
        "tenant_id": run.tenant_id,
    }


class TaskDecompositionService:
    """The Analysis→Task boundary owner.  The API lane calls ``convert``;
    ``classify`` / ``build_task_fields`` are the pure, testable cores."""

    @staticmethod
    def classify_finding(category: str, severity: str, tag: str) -> str:
        """Public alias of the pure :func:`classify` for the test suite."""
        return classify(category, severity, tag)

    async def convert(
        self,
        db,
        *,
        project: Project,
        run: AnalysisRun,
        agent: Agent,
        current_user: User,
    ) -> DecompositionOutcome:
        """Convert this run's executable findings into pending Task rows.

        Pipeline (fail-closed, all bounded):
          G1  run must be ``AN_COMPLETED`` (else TD_RUN_NOT_COMPLETED);
              tenant scope re-asserted; target agent's tenant == project tenant
              (else 403-class, raised as ``DecompositionSecurity``).
          G3  each executable-class finding -> a pending Task with provenance
              filled (``build_task_fields``); planning-class findings are
              reported ``planning_only`` (never a Task).
          §4  already-converted findings are skipped (``skipped_duplicate``)
              via the bounded ``converted_finding_ids`` pre-check; the last-
              resort guard is the f070 UNIQUE(analysis_run_id, finding_id).
          G4  NO ``enqueue_task_runtime`` call — a converted task runs only
              through the existing manual trigger.
          G5  the findings set is bounded to MAX_TASKS_PER_INVOCATION.
        """
        outcome = DecompositionOutcome(state="pending")

        # G1 — the terminal-run gate + tenant entry gate (mirrors
        # AnalysisService._entry_gates / _scope_gates).
        intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        agent_tenant = getattr(agent, "tenant_id", None)
        if project.tenant_id is None:
            raise DecompositionSecurity("decomposition has no tenant context; refusing to run")
        if agent_tenant is None:
            raise DecompositionSecurity("target agent has no tenant; refusing to run")
        if agent_tenant != project.tenant_id:
            raise DecompositionSecurity("target agent tenant does not match the project tenant")
        if run.status != "AN_COMPLETED":
            outcome.state = "failed"
            outcome.code = TD_RUN_NOT_COMPLETED
            outcome.detail = f"run {run.id} is {run.status!r}; conversion requires AN_COMPLETED"
            return outcome
        if run.project_id != project.id:
            outcome.state = "failed"
            outcome.code = TD_INVALID_INPUT
            outcome.detail = "analysis run does not belong to this project"
            return outcome

        # G1 — agent_id is MANDATORY in V1 (spec §3.3: TD_AGENT_REQUIRED; the
        # executing agent is a human decision, never silently inherited).
        if agent.id is None:
            outcome.state = "failed"
            outcome.code = TD_AGENT_REQUIRED
            outcome.detail = "an executing agent_id is required to convert findings"
            return outcome

        # Load the run's findings (bounded to the analysis-lane cap of 100).
        findings = list(await analysis_finding_dao.list_for_run(run.id, db=db, limit=MAX_TASKS_PER_INVOCATION))
        # G5 — defensive: a run whose findings exceed the bound is not
        # converted (fail closed rather than write an unbounded batch).
        if len(findings) > MAX_TASKS_PER_INVOCATION:
            outcome.state = "failed"
            outcome.code = TD_INVALID_INPUT
            outcome.detail = f"run has {len(findings)} findings, exceeding the {MAX_TASKS_PER_INVOCATION} invocation bound"
            return outcome

        # §4 — dedup pre-check: which findings of this run are already converted?
        converted = await task_provenance_dao.converted_finding_ids(run.id, db=db)

        outcome.state = "ok"
        outcome.code = TD_OK
        for finding in findings:
            if classify(finding.category, finding.severity, finding.tag) != "executable":
                outcome.per_finding[finding.id] = "planning_only"
                continue
            if finding.id in converted:
                outcome.per_finding[finding.id] = "skipped_duplicate"
                continue
            task = Task(**build_task_fields(finding, run, agent_id=agent.id, created_by=current_user.id))
            await task_provenance_dao.create_with_provenance(task, db=db)
            outcome.per_finding[finding.id] = "converted"
            outcome.converted_task_ids.append(task.id)

        outcome.counts = {
            "converted": sum(1 for v in outcome.per_finding.values() if v == "converted"),
            "skipped_duplicate": sum(1 for v in outcome.per_finding.values() if v == "skipped_duplicate"),
            "planning_only": sum(1 for v in outcome.per_finding.values() if v == "planning_only"),
        }
        return outcome


task_decomposition_service = TaskDecompositionService()

__all__ = [
    "DECOMPOSITION_RESULT_CODES",
    "MAX_TASKS_PER_INVOCATION",
    "PER_FINDING_OUTCOMES",
    "SEVERITY_TO_PRIORITY",
    "TD_AGENT_REQUIRED",
    "TD_AGENT_TENANT_MISMATCH",
    "TD_INVALID_INPUT",
    "TD_OK",
    "TD_RUN_NOT_COMPLETED",
    "DecompositionError",
    "DecompositionOutcome",
    "DecompositionSecurity",
    "TaskDecompositionService",
    "build_task_fields",
    "classify",
    "task_decomposition_service",
]

"""Phase 3 Planning service — the Project + Analysis → ProjectPlan owner.

Per docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md §1/§3/§4 this
service is the SINGLE owned lane that turns a Project Analysis (the Phase 2C
durable ``AnalysisRun`` + its findings) into a durable, materializable
``ProjectPlan`` (the design's P1 decision: the plan *is* the
``PlanningRun`` revision concept, no separate table).  It is the producer the
design's §5 reserved slot names: it fills ``Task.created_reason =
ANALYSIS_PLANNING`` (the finding-lane stays on ``ANALYSIS_FINDING``) without
touching the frozen Task / Analysis / graph models.

The two halves are deliberately separated so each is independently
verifiable, mirroring the Phase 2D decomposition service's
``classify`` / ``build_task_fields`` (pure) + ``convert`` (transactional)
split:

- :func:`build_plan_blueprint` is a PURE, database-free derivation over
  bounded input data (the run + its findings + a small set of closed knobs).
  It emits the goals, work packages, milestones, the materialization
  task slots, and the dependency edge spec.  No ORM, no session, no DB —
  unit-testable like the ``classify`` grid.

- :meth:`PlanningService.create_plan` is the bounded, transactional,
  tenant-scoped WRITE that persists the blueprint's goals/WPs/milestones,
  materializes each slot into a real ``Task`` row through the frozen
  ``task_provenance_dao.create_with_provenance`` (the §4 materialization
  contract, ``finding_id`` NULL), links it through the planning-side
  ``work_package_tasks`` link, and wires the DAG edges through the FROZEN
  ``task_graph_service.bulk_add_edges`` (design D2/§7 rule 2: the existing
  ``task_dependencies`` graph is the single authority — no second graph,
  no second readiness model, no second edge table is ever created).

Settled decisions honored:
- **G4 — create ≠ run.** A materialized Task lands ``status="pending"`` and
  is NEVER enqueued (the execution lane is the separately-authorized
  Phase 2E ``TaskExecutionService``).  "Create" and "run" stay decoupled.
- **No second authority.** The plan reuses ``Task`` + ``TaskDependency`` +
  ``Task.agent_id``; the only new link is ``work_package_tasks`` (design P5).
- **Fail closed.** Every gate returns a closed ``PL_*`` code before any
  write; a terminal run / mismatched project / missing agent refuses the
  whole plan (mirrors the AN_* / TD_* closed-code pattern).
- **Append-only revisions.** ``UNIQUE(project_id, analysis_revision_sha)``
  (§6.1) makes a re-plan at a new revision a new row; a same-revision call
  re-reads the existing run (the concurrency guard) and never clobbers.
- **Provenance chain (#5).** The chain AnalysisFinding → PlanningGoal
  (``analysis_finding_ids``) → WorkPackage → Task (the ``work_package_tasks``
  link + the Task's own ``ANALYSIS_PLANNING`` provenance set) is queryable
  end to end via :meth:`PlanningService.provenance_for_task`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.dao.analysis_dao import analysis_finding_dao, analysis_run_dao
from app.dao.base import tenant_context
from app.dao.planning_dao import (
    milestone_dao,
    planning_goal_dao,
    planning_run_dao,
    work_package_dao,
    work_package_task_dao,
)
from app.dao.project_intake_dao import project_dao
from app.dao.task_dao import task_provenance_dao
from app.models.agent import Agent
from app.models.analysis import AnalysisFinding, AnalysisRun
from app.models.planning import (
    PLANNING_GOAL_STATUSES,
    PLANNING_RUN_STATUSES,
    Milestone,
    PlanningGoal,
    PlanningRun,
    WorkPackage,
    WorkPackageTask,
)
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.services import intake_security
from app.services.task_graph_service import task_graph_service

# ---------------------------------------------------------------------------
# Closed result-code set (mirrors the AN_* / TD_* closed-code pattern).  A
# code outside the set is a programming error, rejected at construction.
# ---------------------------------------------------------------------------

PL_OK = "PL_OK"
PL_PLAN_EXISTS = "PL_PLAN_EXISTS"
PL_RUN_NOT_COMPLETED = "PL_RUN_NOT_COMPLETED"
PL_INVALID_INPUT = "PL_INVALID_INPUT"
PL_PROJECT_NOT_EXECUTABLE = "PL_PROJECT_NOT_EXECUTABLE"
PL_AGENT_REQUIRED = "PL_AGENT_REQUIRED"
PL_TENANT_MISMATCH = "PL_TENANT_MISMATCH"

#: The closed transport code set.  Read-back of persisted / status data
#: re-validates against it (the F1 pattern the AN_/TD_ lanes use).
PLANNING_RESULT_CODES = frozenset(
    {
        PL_OK,
        PL_PLAN_EXISTS,
        PL_RUN_NOT_COMPLETED,
        PL_INVALID_INPUT,
        PL_PROJECT_NOT_EXECUTABLE,
        PL_AGENT_REQUIRED,
        PL_TENANT_MISMATCH,
    }
)

#: G5 — the bounded output of one plan (the analysis lane caps a run's
#: findings at 100; one goal per finding, so a whole-run plan is naturally
#: <= 100.  This is the defensive guard: an oversized run fails closed
#: rather than writing an unbounded batch of goals/tasks/edges).
MAX_GOALS_PER_PLAN = 100

#: The task title column width (``tasks.title`` is String(500)).
MAX_TASK_TITLE_CHARS = 500

#: Required-capability input per finding category (design P5 / S2).  A bounded,
#: CLOSED mapping: a capability bundle is an INPUT to the assignment step,
#: never the assignment fact (the fact stays ``Task.agent_id``, design D2).
_CAPABILITY_BY_CATEGORY: dict[str, list[str]] = {
    "SECURITY": ["security", "review"],
    "TECH_DEBT": ["backend", "code"],
    "RISK": ["code"],
    "OPEN_QUESTION": ["code"],
    "FACT": ["code"],
}
_DEFAULT_CAPABILITIES = ["code"]

#: Finding categories whose goals REQUIRE an independent reviewer slot
#: (design P11 / REV-1).  A review slot is materialized alongside the build
#: slot, ``depends_on`` the build, and carries ``requires_independent_review``
#: on the owning work package.  Enforcement of reviewer != executor is the
#: assignment lane's concern (deferred, design D-3); here we only DECLARE it.
_REVIEW_REQUIRED_CATEGORIES = frozenset({"SECURITY"})


class PlanningError(Exception):
    """A closed PL_* outcome (409-class) with a stable code + detail.

    Client-reachable gate failures become a closed code carried by the
    outcome — they must never surface as a 500 (the fail-closed contract the
    AN_* / TD_* codes carry).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if code not in PLANNING_RESULT_CODES:
            raise ValueError(f"planning code {code!r} is not in the closed result-code set")
        self.code = code
        self.message = message


class PlanningSecurity(RuntimeError):
    """A tenant-isolation finding (403-class, never retried) — the M9 gate.

    Raised when the acting context / target agent crosses the tenant
    security boundary; maps to 403 at the transport (mirrors
    ``AnalysisSecurity`` / ``DecompositionSecurity``).
    """


# ---------------------------------------------------------------------------
# Pure blueprint — the database-free derivation core.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlueprintSlot:
    """One materializable task slot of a work package (squad design §4.1).

    ``slot`` is the stable string id the edge spec references; ``kind`` is a
    closed member of the task_scope slot kinds (build / review / gate /
    other); ``is_head`` marks the package's head task — the one the
    inter-package ordering edges attach to.
    """

    slot: str
    kind: str
    title: str
    description: str | None
    is_head: bool
    required_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlueprintMilestone:
    """One ordering / phase bucket the plan's packages join (design P9)."""

    kind: str
    seq: int
    title: str


@dataclass(frozen=True)
class BlueprintPackage:
    """One work package: a goal's task slots + execution shape (design P3).

    ``intra_edges`` are (dependent_slot, dependency_slot) pairs WITHIN this
    package (parent-child ordering, e.g. review depends on build).
    ``shared_resources`` is the conflict-detection declaration (P10);
    enforcement stays in the existing Redis workspace locks (design §6.12).
    """

    title: str
    goal_index: int
    execution_mode: str
    shared_resources: dict[str, Any] | None
    requires_independent_review: bool
    slots: tuple[BlueprintSlot, ...]
    intra_edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BlueprintGoal:
    """One derived operational goal, traceable to its motivating findings."""

    title: str
    description: str | None
    required_capabilities: tuple[str, ...]
    analysis_finding_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class PlanBlueprint:
    """The pure, DB-free shape of one plan (the translator's input).

    ``inter_edges`` are (from_package_index, to_package_index) head-task
    ordering pairs across packages (the parallel/serial + milestone group
    wiring, design P12/P13).  ``plan_sha256`` is the content hash the run
    locks onto (the materialization idempotency key, design §4).
    """

    goals: tuple[BlueprintGoal, ...]
    packages: tuple[BlueprintPackage, ...]
    milestones: tuple[BlueprintMilestone, ...]
    inter_edges: tuple[tuple[int, int], ...]
    plan_sha256: str

    def payload(self) -> dict[str, Any]:
        """The bounded, canonical plan body the run's ``plan_payload`` carries."""
        return {
            "goals": [
                {
                    "title": g.title,
                    "required_capabilities": list(g.required_capabilities),
                    "analysis_finding_ids": [str(f) for f in g.analysis_finding_ids],
                }
                for g in self.goals
            ],
            "packages": [
                {
                    "title": p.title,
                    "execution_mode": p.execution_mode,
                    "slots": [s.slot for s in p.slots],
                    "requires_independent_review": p.requires_independent_review,
                }
                for p in self.packages
            ],
            "milestones": [f"{m.kind}:{m.seq}" for m in self.milestones],
            "inter_edges": [f"{a}->{b}" for a, b in self.inter_edges],
        }


def _capabilities_for(category: str) -> tuple[str, ...]:
    """Closed capability input per category (design P5 / S2); fail to a default."""
    return tuple(_CAPABILITY_BY_CATEGORY.get(category, _DEFAULT_CAPABILITIES))


def _slot_title(category: str, summary: str) -> str:
    """The materialized task title: ``[category] summary`` truncated to 500."""
    title = f"[{category}] {summary}"
    return title[:MAX_TASK_TITLE_CHARS]


def _slot_description(
    goal_index: int,
    finding: AnalysisFinding,
    run: AnalysisRun,
    *,
    head: bool,
) -> str:
    """The materialized task description: summary + evidence + provenance footer."""
    parts = [
        f"Planning goal {goal_index + 1}: {finding.summary}",
    ]
    anchors = (finding.evidence or {}).get("anchors")
    if isinstance(anchors, list) and anchors:
        parts.append("Evidence: " + ", ".join(str(a) for a in anchors))
    role = "build" if head else "independent review"
    parts.append(
        f"Provenance: analysis run {run.id} @ revision {run.revision_sha}; "
        f"finding {finding.id} ({finding.category}); {role} slot"
    )
    return "\n".join(parts)


def build_plan_blueprint(
    *,
    run: AnalysisRun,
    findings: list[AnalysisFinding],
    project: Project,
    agent: Agent,
    execution_mode: str = "serial",
    milestone_kind: str = "delivery",
    milestone_title: str = "Deliver the plan",
) -> PlanBlueprint:
    """PURE, database-free derivation of the plan from a run's findings.

    One goal per finding (the 1:1 provenance chain the card requires); each
    goal owns one work package whose slots materialize into Tasks.  The
    default shape is a serial delivery group:

    - intra-package: a SECURITY finding's package gets a ``review`` slot that
      depends on its ``build`` slot (parent-child ordering + review
      independence, design P11); every other category is a single build slot.
    - inter-package: within the (single) delivery milestone the packages form
      a serial group — package ``i``'s head task depends on package ``i-1``'s
      head task (the milestone / parallel-serial-group wiring, design P12/P13).

    Fail closed: the package / slot ``kind`` + capability values are drawn
    only from the closed sets so the blueprint can never carry an out-of-set
    value into the DAO write path (which would raise ``ClosedCodeError``).
    Bounded: a findings list above ``MAX_GOALS_PER_PLAN`` is refused.
    """
    if len(findings) > MAX_GOALS_PER_PLAN:
        raise PlanningError(
            PL_INVALID_INPUT,
            f"run has {len(findings)} findings, exceeding the {MAX_GOALS_PER_PLAN} plan bound",
        )

    goals: list[BlueprintGoal] = []
    packages: list[BlueprintPackage] = []
    for index, finding in enumerate(findings):
        caps = _capabilities_for(finding.category)
        needs_review = finding.category in _REVIEW_REQUIRED_CATEGORIES
        build_slot = BlueprintSlot(
            slot="build",
            kind="build",
            title=_slot_title(finding.category, finding.summary),
            description=_slot_description(index, finding, run, head=True),
            is_head=True,
            required_capabilities=caps,
        )
        slots = [build_slot]
        intra: list[tuple[str, str]] = []
        requires_review_flag = needs_review
        if needs_review:
            review_slot = BlueprintSlot(
                slot="review",
                kind="review",
                title=f"Independent review: {build_slot.title}",
                description=_slot_description(index, finding, run, head=False),
                is_head=False,
                required_capabilities=("review",),
            )
            slots.append(review_slot)
            intra.append(("review", "build"))

        goals.append(
            BlueprintGoal(
                title=f"[{finding.category}] {finding.summary}",
                description=f"Derived from analysis finding {finding.id} @ revision {run.revision_sha}",
                required_capabilities=caps,
                analysis_finding_ids=(finding.id,),
            )
        )
        packages.append(
            BlueprintPackage(
                title=build_slot.title,
                goal_index=index,
                execution_mode=execution_mode,
                shared_resources=None,
                requires_independent_review=requires_review_flag,
                slots=tuple(slots),
                intra_edges=tuple(intra),
            )
        )

    milestones = (
        (BlueprintMilestone(kind=milestone_kind, seq=0, title=milestone_title),)
        if packages
        else ()
    )

    # Serial delivery group: package i's head depends on package i-1's head.
    inter_edges: list[tuple[int, int]] = [
        (i, i - 1) for i in range(1, len(packages))
    ]

    blueprint = PlanBlueprint(
        goals=tuple(goals),
        packages=tuple(packages),
        milestones=milestones,
        inter_edges=tuple(inter_edges),
        plan_sha256="",
    )
    blueprint = replace_plan_sha(blueprint)
    return blueprint


def replace_plan_sha(blueprint: PlanBlueprint) -> PlanBlueprint:
    """Stamp the blueprint with the content hash of its emitted payload."""
    canonical = json.dumps(blueprint.payload(), sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PlanBlueprint(
        goals=blueprint.goals,
        packages=blueprint.packages,
        milestones=blueprint.milestones,
        inter_edges=blueprint.inter_edges,
        plan_sha256=digest,
    )


# ---------------------------------------------------------------------------
# The transactional writer.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    """The durable plan (the P1 ``PlanningRun`` revision + its materialization).

    This is the card's ``ProjectPlan`` return type: the run row plus the ids
    of everything the plan materialized (goals, packages, milestones, Tasks,
    DAG edges).  The plan references the existing Task + Analysis entities,
    never a second graph or assignment fact.
    """

    run: PlanningRun
    goal_ids: tuple[uuid.UUID, ...] = ()
    package_ids: tuple[uuid.UUID, ...] = ()
    milestone_ids: tuple[uuid.UUID, ...] = ()
    materialized_task_ids: tuple[uuid.UUID, ...] = ()
    edge_count: int = 0
    plan_sha256: str = ""
    state: str = "created"


@dataclass
class PlanOutcome:
    """The transport shape of a create_plan attempt (mirrors AN_/TD_ outcomes).

    - ``state="created"``   — a new plan revision was materialized; ``plan``
      carries the durable ProjectPlan.
    - ``state="existing"`` — the (project, revision) already had a plan
      revision (the append-only re-read path, §6.1); nothing clobbered.
    - ``state="failed"``   — a closed PL_* gate rejected before any write.
    """

    state: str
    code: str | None = None
    detail: str = ""
    plan: ProjectPlan | None = None


class PlanningService:
    """The Project + Analysis → ProjectPlan owner.  API lanes call
    ``create_plan`` and ``provenance_for_task``; the blueprint builder is the
    pure, testable core."""

    # ------------------------------------------------------------------
    # Pure derivation core (DB-free, unit-testable).
    # ------------------------------------------------------------------
    @staticmethod
    def build_blueprint(
        *,
        run: AnalysisRun,
        findings: list[AnalysisFinding],
        project: Project,
        agent: Agent,
        **kwargs: Any,
    ) -> PlanBlueprint:
        """Public alias of the pure :func:`build_plan_blueprint` for tests."""
        return build_plan_blueprint(run=run, findings=findings, project=project, agent=agent, **kwargs)

    # ------------------------------------------------------------------
    # create_plan — the bounded, transactional, tenant-scoped write.
    # ------------------------------------------------------------------
    async def create_plan(
        self,
        db,
        *,
        project_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        agent: Agent,
        current_user: User,
        execution_mode: str = "serial",
        milestone_kind: str = "delivery",
    ) -> PlanOutcome:
        """Materialize a durable ProjectPlan from a completed Analysis run.

        Pipeline (fail-closed, all bounded, one transaction):
          G1  load project + run (both tenant-scoped); run must belong to the
              project and be ``AN_COMPLETED`` (else PL_RUN_NOT_COMPLETED);
              tenant entry gate (M9).
          G2  the executing agent is MANDATORY + same-tenant (PL_AGENT_REQUIRED
              / PL_TENANT_MISMATCH) — a Task's agent_id is non-null and never
              silently inherited.
          R   re-read: a plan revision for (project, run.revision_sha) already
              exists -> return it as ``existing`` (the §6.1 append-only guard;
              a racing launch never clobbers).
          W   persist the blueprint: open the PL_OPEN run, write goals
              (PL_PROPOSED), the delivery milestone, the work packages; then
              materialize every slot into a real Task row
              (``created_reason=ANALYSIS_PLANNING``, ``finding_id`` NULL, the
              full 2D provenance set) through the frozen task provenance DAO,
              link each slot through ``work_package_tasks``, and wire the DAG
              through the frozen ``task_graph_service.bulk_add_edges`` (NO
              second graph model).  Move goals PL_APPROVED -> PL_MATERIALIZED
              and the run PL_OPEN -> PL_COMPLETED with the plan_sha256 lock.
          G4  NO ``enqueue_task_runtime`` — a materialized Task is NEVER run
              here (execution stays on the separately-authorized Phase 2E
              lane).
          G5  the findings set is bounded to MAX_GOALS_PER_PLAN.
        """
        outcome = PlanOutcome(state="pending")

        # G1 — tenant entry gate + load (mirrors AnalysisService._entry_gates).
        project = await project_dao.get_scoped(project_id, db=db)
        if project is None:
            outcome.state = "failed"
            outcome.code = PL_INVALID_INPUT
            outcome.detail = "project not found in this tenant"
            return outcome
        run = await analysis_run_dao.get(analysis_run_id, db=db)
        if run is None:
            outcome.state = "failed"
            outcome.code = PL_INVALID_INPUT
            outcome.detail = "analysis run not found in this tenant"
            return outcome
        try:
            intake_security.verify_tenant_scope(project.tenant_id, current_user.tenant_id)
        except Exception as exc:  # narrow: the tenant-scope assertion only
            raise PlanningSecurity("plan has no matching tenant context; refusing to run") from exc
        if run.project_id != project.id:
            outcome.state = "failed"
            outcome.code = PL_INVALID_INPUT
            outcome.detail = "analysis run does not belong to this project"
            return outcome
        if run.status != "AN_COMPLETED":
            outcome.state = "failed"
            outcome.code = PL_RUN_NOT_COMPLETED
            outcome.detail = f"run {run.id} is {run.status!r}; planning requires AN_COMPLETED"
            return outcome

        # G2 — the executing agent is mandatory + same-tenant (TD_AGENT_REQUIRED).
        if agent.id is None:
            outcome.state = "failed"
            outcome.code = PL_AGENT_REQUIRED
            outcome.detail = "an executing agent_id is required to materialize the plan"
            return outcome
        agent_tenant = getattr(agent, "tenant_id", None)
        if agent_tenant is None:
            outcome.state = "failed"
            outcome.code = PL_AGENT_REQUIRED
            outcome.detail = "executing agent has no tenant; refusing to run"
            return outcome
        if agent_tenant != project.tenant_id:
            outcome.state = "failed"
            outcome.code = PL_TENANT_MISMATCH
            outcome.detail = "executing agent tenant does not match the project tenant"
            return outcome

        # G-EXEC — a plan materializes alongside execution (design §4
        # PL_PROJECT_NOT_EXECUTABLE), reusing the Phase 2E executable set.
        from app.services.task_execution_service import PROJECT_EXECUTABLE_STATUSES

        if project.status not in PROJECT_EXECUTABLE_STATUSES:
            outcome.state = "failed"
            outcome.code = PL_PROJECT_NOT_EXECUTABLE
            outcome.detail = (
                f"project is in status {project.status!r}; planning materialization "
                f"requires one of {sorted(PROJECT_EXECUTABLE_STATUSES)}"
            )
            return outcome

        tenant_id = project.tenant_id
        with tenant_context(tenant_id):
            # R — the append-only revision guard (§6.1): a plan at this exact
            # revision already exists -> re-read it, never clobber.
            existing = await planning_run_dao.get_plan_for_project(
                project.id, run.revision_sha, db=db
            )
            if existing is not None:
                outcome.state = "existing"
                outcome.code = PL_PLAN_EXISTS
                outcome.detail = "a plan revision already exists for this analysis revision"
                outcome.plan = ProjectPlan(run=existing, state="existing")
                return outcome

            # Load the run's findings (bounded to the analysis-lane cap).
            findings = list(
                await analysis_finding_dao.list_for_run(run.id, db=db, limit=MAX_GOALS_PER_PLAN)
            )

            # Pure derivation (DB-free; may fail closed on an oversized set).
            blueprint = build_plan_blueprint(
                run=run,
                findings=findings,
                project=project,
                agent=agent,
                execution_mode=execution_mode,
                milestone_kind=milestone_kind,
            )

            # W — open the run + persist goals, milestones, packages.
            run_row = PlanningRun(
                project_id=project.id,
                analysis_revision_sha=run.revision_sha,
                planner_agent_id=agent.id,
                status=PLANNING_RUN_STATUSES[0],  # PL_OPEN
                plan_payload=blueprint.payload(),
                tenant_id=tenant_id,
            )
            await planning_run_dao.open_run(run_row, tenant_id=tenant_id, db=db)

            goal_rows: list[PlanningGoal] = []
            for g in blueprint.goals:
                goal_rows.append(
                    await planning_goal_dao.add_goal(
                        PlanningGoal(
                            planning_run_id=run_row.id,
                            title=g.title,
                            description=g.description,
                            required_capabilities=list(g.required_capabilities),
                            analysis_finding_ids=[str(f) for f in g.analysis_finding_ids],
                            status=PLANNING_GOAL_STATUSES[0],  # PL_PROPOSED
                            tenant_id=tenant_id,
                        ),
                        tenant_id=tenant_id,
                        db=db,
                    )
                )

            milestone_rows: list[Milestone] = []
            for m in blueprint.milestones:
                milestone_rows.append(
                    await milestone_dao.add_milestone(
                        Milestone(
                            planning_run_id=run_row.id,
                            kind=m.kind,
                            seq=m.seq,
                            title=m.title,
                            tenant_id=tenant_id,
                        ),
                        tenant_id=tenant_id,
                        db=db,
                    )
                )
            # One delivery milestone -> its id buckets every package below.
            milestone_id = milestone_rows[0].id if milestone_rows else None

            package_rows: list[WorkPackage] = []
            # (wp_index, slot_name) -> Task, filled during materialization.
            slot_tasks: dict[tuple[int, str], Task] = {}
            edge_count = 0
            task_ids: list[uuid.UUID] = []

            for p_index, package in enumerate(blueprint.packages):
                goal = blueprint.goals[p_index]
                task_scope = [
                    {
                        "slot": s.slot,
                        "kind": s.kind,
                        "title": s.title,
                        "description": s.description,
                        "is_head": s.is_head,
                        "required_capabilities": list(s.required_capabilities),
                        # Candidate agents ride the slot for the assignment
                        # lane (t_9820b3d3); here the single executing agent
                        # is the candidate (reviewer != builder is enforced
                        # at assignment, not here — design D-3).
                        "candidate_agent_ids": [str(agent.id)],
                        # depends_on_slots: this slot's in-package dependency
                        # (e.g. the review slot depends on build); the DAG
                        # edges are emitted through the frozen graph service.
                        "depends_on_slots": [
                            dep for (dependent, dep) in package.intra_edges if dependent == s.slot
                        ],
                    }
                    for s in package.slots
                ]
                package_rows.append(
                    await work_package_dao.add_package(
                        WorkPackage(
                            planning_run_id=run_row.id,
                            planning_goal_id=goal_rows[p_index].id,
                            milestone_id=milestone_id,
                            title=package.title,
                            task_scope=task_scope,
                            execution_mode=package.execution_mode,
                            shared_resources=package.shared_resources,
                            requires_independent_review=package.requires_independent_review,
                            tenant_id=tenant_id,
                        ),
                        tenant_id=tenant_id,
                        db=db,
                    )
                )
                pkg = package_rows[p_index]

                # Materialize every slot into a real Task row (finding_id NULL,
                # the full 2D provenance set) + link it through work_package_tasks.
                for s in package.slots:
                    task = Task(
                        agent_id=agent.id,
                        created_by=current_user.id,
                        tenant_id=tenant_id,
                        project_id=project.id,
                        analysis_run_id=run.id,
                        revision_sha=run.revision_sha,
                        created_reason="ANALYSIS_PLANNING",
                        title=s.title,
                        description=s.description,
                        type="todo",
                        status="pending",  # G4: NEVER enqueued here
                        priority="medium",
                    )
                    await task_provenance_dao.create_with_provenance(task, db=db)
                    slot_tasks[(p_index, s.slot)] = task
                    task_ids.append(task.id)
                    slot_link = WorkPackageTask(work_package_id=pkg.id, tenant_id=tenant_id)
                    await work_package_task_dao.add_slot(slot_link, tenant_id=tenant_id, db=db)
                    await work_package_task_dao.materialize_slot(
                        slot_link, task_id=task.id, db=db
                    )

                # Intra-package edges (parent-child, e.g. review -> build).
                for dependent_slot, dependency_slot in package.intra_edges:
                    from_task = slot_tasks[(p_index, dependent_slot)]
                    to_task = slot_tasks[(p_index, dependency_slot)]
                    edge_result = await task_graph_service.bulk_add_edges(
                        db,
                        task_id=from_task.id,
                        depends_on_task_ids=[to_task.id],
                        tenant_id=tenant_id,
                    )
                    if edge_result.state == "added":
                        edge_count += 1

            # Inter-package edges (the serial delivery group, milestone P13):
            # package i's head task depends on package i-1's head task.
            for from_index, to_index in blueprint.inter_edges:
                from_task = slot_tasks[(from_index, "build")]
                to_task = slot_tasks[(to_index, "build")]
                edge_result = await task_graph_service.bulk_add_edges(
                    db,
                    task_id=from_task.id,
                    depends_on_task_ids=[to_task.id],
                    tenant_id=tenant_id,
                )
                if edge_result.state == "added":
                    edge_count += 1

            # Human-gate the goal ladder (PL_APPROVED) then materialize it.
            for goal in goal_rows:
                await planning_goal_dao.set_status(goal, new_status="PL_APPROVED", db=db)
                await planning_goal_dao.set_status(goal, new_status="PL_MATERIALIZED", db=db)
            await planning_run_dao.complete_run(
                run_row,
                new_status="PL_COMPLETED",
                plan_sha256=blueprint.plan_sha256,
                db=db,
            )

        outcome.state = "created"
        outcome.code = PL_OK
        outcome.plan = ProjectPlan(
            run=run_row,
            goal_ids=tuple(g.id for g in goal_rows),
            package_ids=tuple(p.id for p in package_rows),
            milestone_ids=tuple(m.id for m in milestone_rows),
            materialized_task_ids=tuple(task_ids),
            edge_count=edge_count,
            plan_sha256=blueprint.plan_sha256,
        )
        return outcome

    # ------------------------------------------------------------------
    # Provenance query (#5): Analysis finding -> Task, end to end.
    # ------------------------------------------------------------------
    async def provenance_for_task(
        self,
        db,
        *,
        task_id: uuid.UUID,
        current_user: User,
    ) -> dict[str, Any] | None:
        """Query the full provenance chain for one planning-materialized Task.

        Returns ``None`` when the Task has no planning link (it is not
        ``ANALYSIS_PLANNING`` or no work_package_tasks row points at it).  The
        chain, in queryable form:

        - ``task``            — the Task row (created_reason, project/run/revision);
        - ``work_package``    — the WP whose slot materialized it (the link);
        - ``goal``            — the owning PlanningGoal;
        - ``findings``        — the AnalysisFinding rows named by
          ``goal.analysis_finding_ids`` (the originating understanding);
        - ``planning_run``    — the durable plan revision it was born of.

        This is the "provenance chain is queryable" acceptance (card #5).
        """
        task = await task_provenance_dao.get_scoped(task_id, db=db)
        if task is None:
            return None
        try:
            intake_security.verify_tenant_scope(task.tenant_id, current_user.tenant_id)
        except Exception as exc:  # narrow: the tenant-scope assertion only
            raise PlanningSecurity("provenance query has no matching tenant context") from exc
        if task.created_reason != "ANALYSIS_PLANNING":
            return None  # a non-planning task has no planning provenance chain
        return await _provenance_view(db, task=task)


async def _provenance_view(db, *, task: Task) -> dict[str, Any] | None:
    """Assemble the provenance chain through bounded, tenant-scoped reads.

    Uses the owning DAOs (no raw ORM in the service, per the DAO layering
    rule): the ``work_package_tasks`` link that materialized this task, the
    assignment-INPUT view for that work package (goal + capabilities), and
    the findings the goal cites.
    """
    link_row = await work_package_task_dao.get_link_for_task(task.id, db=db)
    if link_row is None:
        return None
    view = await work_package_dao.get_assignment_candidates_for_work_package(
        link_row.work_package_id, db=db
    )
    if view is None:
        return None
    goal = view["goal"]
    finding_ids = [uuid.UUID(f) for f in (goal.analysis_finding_ids or []) if f]
    findings: list[Any] = []
    if finding_ids:
        findings = list(await analysis_finding_dao.get_by_ids(finding_ids, db=db))
    return {
        "task": task,
        "work_package": view["work_package"],
        "goal": goal,
        "findings": findings,
        "planning_run_id": view["work_package"].planning_run_id,
        "required_capabilities": view["required_capabilities"],
    }


planning_service = PlanningService()

__all__ = [
    "PLANNING_RESULT_CODES",
    "PL_AGENT_REQUIRED",
    "PL_INVALID_INPUT",
    "PL_OK",
    "PL_PLAN_EXISTS",
    "PL_PROJECT_NOT_EXECUTABLE",
    "PL_RUN_NOT_COMPLETED",
    "PL_TENANT_MISMATCH",
    "BlueprintGoal",
    "BlueprintMilestone",
    "BlueprintPackage",
    "BlueprintSlot",
    "PlanBlueprint",
    "PlanOutcome",
    "PlanningError",
    "PlanningSecurity",
    "PlanningService",
    "ProjectPlan",
    "build_plan_blueprint",
    "planning_service",
    "replace_plan_sha",
]

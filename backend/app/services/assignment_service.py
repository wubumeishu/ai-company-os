"""Phase 3 Agent Assignment + concurrency validation (Squad Orchestration V1 §8).

Per docs/architecture/PHASE_3_SQUAD_ORCHESTRATION_V1_DESIGN.md §8 this service
is the assignment lane that a materialized ProjectPlan (``PlanningRun`` + its
work packages, produced by the owning ``PlanningService`` lane) feeds:

1. **Candidate resolution (§4.2, S3).** For each ``task_scope`` slot the
   planner-emitted ``candidate_agent_ids`` are re-validated against the live
   roster (active, not expired, same tenant — the exact availability gate the
   Phase 2E execution lane uses, ``task_execution_service`` P5) and EXACTLY
   ONE survivor is picked deterministically (first in planner-declared
   order).  The pick is written to the single frozen assignment fact
   ``Task.agent_id`` (S5) through one additive DAO method; an empty or
   fully-inactive candidate set fails closed with ``PL_NO_CANDIDATE_AGENT``.

2. **Conflict + review constraints (§6/§7, S4).** The pure, DB-free
   predicates CONF-1..CONF-5 + REV-1..REV-3 run over the WP's slots, its
   frozen ``task_dependencies`` subgraph, and the declared
   ``shared_resources``.  Fail-closed closed codes:
   ``PL_RESOURCE_CONFLICT`` (CONF-1/CONF-4), ``PL_REVIEWER_NOT_INDEPENDENT``
   (REV-1/REV-2), ``PL_NO_CANDIDATE_AGENT`` (§4.2).  CONF-5 (global
   parallelism bound) is ADVISORY only — it never fails a V1 plan (the
   project-level cap is the deferred runtime change D-2).

3. **The AssignmentPlan artifact (§8).** A computed, JSON-serializable
   report linking agents to tasks with the constraint results
   (``{work_package_id, slots, constraint_report, review_bindings}``).  It is
   RETURNED/LOGGED, never persisted: no ``TaskAssignment`` / ``Squad`` table
   exists, and the only authoritative facts stay where the design pins them —
   order in ``task_dependencies``, assignment in ``Task.agent_id``,
   enforcement in the existing Redis workspace locks + ORM tenant filter
   (S4/S5, "planning detects, runtime enforces").

Concurrency validation (the card's hard requirement): "a task may not be
assigned to two agents simultaneously" holds two ways at once —

- **Structurally:** one non-null ``Task.agent_id`` column, one task per
  (WP, slot) via ``uq_wp_tasks`` (a task belongs to exactly one work
  package), so a second agent can never bind the same task row.
- **Deterministically:** the pick is a pure function of
  (candidates, active roster, builder picks).  Two racing
  ``apply_assignment`` calls on the same WP read the same context and
  converge on the same single writer value — there is no second lock
  (S4: the Redis workspace lock + DAG remain the enforcers), and the
  "two active mutators of one resource for one Agent" check is exactly
  CONF-1 + CONF-4 at plan-validation time.

Settled decisions honored (no new organizational layer — acceptance):
- The squad stays a DERIVATION (S1): distinct ``Task.agent_id`` over a
  package's tasks + the computed constraint report.  No Squad/Team/Role
  entity, no new table, no new column, no new state machine.
- DAG is the authority (S4, design §5): a task may run only when its
  dependencies are done; on conflict the DAG wins.  The
  ``requires_independent_review`` flag propagates to execution scheduling
  purely through the blocking ``builder_task -> reviewer_task`` edge
  (REV-2) — no new scheduler.
- Capability→Agent matching is the planner's job via
  ``candidate_agent_ids`` (D-1 deferred: no structured capability column in
  V1); the lane verifies liveness + disjointness, not capability text.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.permissions import is_agent_expired
from app.dao.agent_dao import agent_dao
from app.dao.base import tenant_context
from app.dao.planning_dao import (
    planning_run_dao,
    work_package_dao,
)
from app.dao.task_dao import task_dependency_dao, task_provenance_dao
from app.models.planning import WorkPackage
from app.models.task import Task
from app.models.user import User

# ---------------------------------------------------------------------------
# Closed result-code set for the assignment lane (mirrors the PL_* pattern the
# planning lane owns).  A code outside the set is a programming error.
# ---------------------------------------------------------------------------

PL_OK = "PL_OK"
PL_INVALID_INPUT = "PL_INVALID_INPUT"
PL_RUN_NOT_COMPLETED = "PL_RUN_NOT_COMPLETED"
#: §4.2 — a build/review slot has no live, active, same-tenant candidate agent.
PL_NO_CANDIDATE_AGENT = "PL_NO_CANDIDATE_AGENT"
#: CONF-1 / CONF-4 — concurrent-possible mutator slots share a resource id.
PL_RESOURCE_CONFLICT = "PL_RESOURCE_CONFLICT"
#: REV-1 / REV-2 — a requires_independent_review WP has no disjoint active
#: reviewer, or the reviewer task does not block the builders via the DAG.
PL_REVIEWER_NOT_INDEPENDENT = "PL_REVIEWER_NOT_INDEPENDENT"
#: CONF-5 — advisory only, never fail-closes V1 (the global cap is deferred D-2).
PL_PARALLELISM_ADVISORY = "PL_PARALLELISM_ADVISORY"

ASSIGNMENT_RESULT_CODES = frozenset(
    {
        PL_OK,
        PL_INVALID_INPUT,
        PL_RUN_NOT_COMPLETED,
        PL_NO_CANDIDATE_AGENT,
        PL_RESOURCE_CONFLICT,
        PL_REVIEWER_NOT_INDEPENDENT,
        PL_PARALLELISM_ADVISORY,
    }
)

#: The three FAIL-CLOSED codes of the constraint layer (squad design §6/§7/§8).
ASSIGNMENT_FAIL_CODES = frozenset(
    {PL_NO_CANDIDATE_AGENT, PL_RESOURCE_CONFLICT, PL_REVIEWER_NOT_INDEPENDENT}
)

#: slot ``kind`` that MUTATES its declared resources (squad design §6:
#: kind ∈ {build, gate} mutates; kind ∈ {review, other} observes read-only
#: and never conflicts over the same resource).
MUTATOR_SLOT_KINDS = ("build", "gate")

#: Closed shared-resource shape (squad design §6, parent P10):
#: ``{"files": [...], "db": [...], "api": [...], "workspace": [...]}``.
RESOURCE_CATEGORIES = ("files", "db", "api", "workspace")

#: Bounded inputs (a pathological package fails closed rather than scanning
#: an unbounded graph — mirrors MAX_PROJECT_EDGES in task_graph_service).
MAX_WP_SLOTS = 200
MAX_WP_EDGES = 1000


class AssignmentSecurity(RuntimeError):
    """A tenant-isolation finding (403-class, never retried).

    Raised when the acting user and the work package cross the tenant
    security boundary; maps to 403 at the transport (mirrors
    ``PlanningSecurity`` / ``AnalysisSecurity``).
    """


# ---------------------------------------------------------------------------
# Pure context — the database-free input of every predicate.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotView:
    """One materialized ``task_scope`` slot of a work package (squad §4.1)."""

    slot: str
    task_id: uuid.UUID  # materialized: build_wp_context pairs slots to linked tasks
    kind: str
    candidate_agent_ids: tuple[uuid.UUID, ...]
    shared_resources: dict[str, Any] | None


@dataclass(frozen=True)
class WpContext:
    """The complete, bounded input of one work package's assignment evaluation.

    ``task_agent`` is the CURRENT ``Task.agent_id`` fact per task (the single
    authoritative assignment fact before this lane writes); ``dag_edges``
    are the frozen ``task_dependencies`` rows of the package's tasks as
    ``(task_id, depends_on_task_id)`` pairs — the arrow points at the
    upstream dependency.  Everything is plain data: the predicates below
    are pure functions of this context (design §10: DB-only / mock
    unit-testable, no LLM, no second lock on the test path).
    """

    wp_id: uuid.UUID
    execution_mode: str
    wp_shared_resources: dict[str, Any] | None
    requires_independent_review: bool
    max_parallel_tasks: int | None
    goal_capabilities: tuple[str, ...]
    slots: tuple[SlotView, ...]
    task_agent: dict[uuid.UUID, uuid.UUID | None]
    dag_edges: tuple[tuple[uuid.UUID, uuid.UUID], ...]

    def builder_task_ids(self) -> list[uuid.UUID]:
        """The non-review materialized tasks of the package, in task order."""
        return [
            s.task_id
            for s in self.slots
            if s.kind != "review" and s.task_id is not None
        ]


@dataclass(frozen=True)
class ConflictEntry:
    """One detected constraint violation (or its advisory counterpart)."""

    rule: str  # CONF-1 / CONF-4 / REV-1 / REV-2
    code: str  # closed code from ASSIGNMENT_FAIL_CODES / advisory
    detail: str
    resource: tuple[str, str] | None = None  # (category, resource id)
    slots: tuple[str, ...] = ()
    task_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ReviewBinding:
    """REV-1/REV-2 result for one reviewer task (design §8 ``review_bindings``)."""

    reviewer_task_id: uuid.UUID
    reviewer_agent_id: uuid.UUID
    reviewed_task_ids: tuple[uuid.UUID, ...]
    blocking_edges: tuple[tuple[uuid.UUID, uuid.UUID], ...]


@dataclass
class ConstraintReport:
    """Per-WP constraint results: fail-closed conflicts + advisories.

    ``conflicts`` carries the three fail-closed codes in deterministic order;
    ``advisories`` carries CONF-5 warnings that never refuse the package.
    """

    conflicts: tuple[ConflictEntry, ...] = ()
    advisories: tuple[ConflictEntry, ...] = ()
    review_bindings: tuple[ReviewBinding, ...] = ()

    def fail_closed_codes(self) -> tuple[str, ...]:
        """The distinct fail-closed codes present (empty tuple = package passes)."""
        seen: list[str] = []
        for c in self.conflicts:
            if c.code in ASSIGNMENT_FAIL_CODES and c.code not in seen:
                seen.append(c.code)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        def _entry(e: ConflictEntry) -> dict[str, Any]:
            return {
                "rule": e.rule,
                "code": e.code,
                "detail": e.detail,
                "resource": (
                    {"category": e.resource[0], "id": e.resource[1]}
                    if e.resource is not None
                    else None
                ),
                "slots": list(e.slots),
                "task_ids": [str(t) for t in e.task_ids],
            }

        return {
            "fail_closed": list(self.fail_closed_codes()),
            "conflicts": [_entry(c) for c in self.conflicts],
            "advisories": [_entry(a) for a in self.advisories],
            "review_bindings": [
                {
                    "reviewer_task_id": str(b.reviewer_task_id),
                    "reviewer_agent_id": str(b.reviewer_agent_id),
                    "reviewed_task_ids": [str(t) for t in b.reviewed_task_ids],
                    "blocking_edges": [
                        [str(a), str(b)] for a, b in b.blocking_edges
                    ],
                }
                for b in self.review_bindings
            ],
        }


# ---------------------------------------------------------------------------
# Pure derivation core (DB-free, unit-testable).
# ---------------------------------------------------------------------------


def _transitive_deps(
    edges: Sequence[tuple[uuid.UUID, uuid.UUID]], limit: int = MAX_WP_EDGES
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """Task -> its transitive dependency set (bounded BFS, fail-closed).

    Mirrors the bounded-reachability discipline of
    ``task_graph_service.upstream_reachable``: a package whose dep closure
    exceeds ``limit`` is refused, never truncated.
    """
    dep_of: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for task_id, dep_on in edges:
        dep_of[task_id].add(dep_on)
    memo: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    for start in set(dep_of) | {d for targets in dep_of.values() for d in targets}:
        if start in memo:
            continue
        stack = list(dep_of.get(start, ()))
        seen: set[uuid.UUID] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if len(seen) > limit:
                raise ValueError(
                    f"work-package dependency closure exceeds {limit} edges; refusing to evaluate"
                )
            stack.extend(dep_of.get(node, ()))
        memo[start] = frozenset(seen)
    return memo


def concurrent_possible(
    task_a: uuid.UUID,
    task_b: uuid.UUID,
    reach: dict[uuid.UUID, frozenset[uuid.UUID]],
) -> bool:
    """Two slots may run in parallel iff neither transitively depends on the other."""
    return task_b not in reach.get(task_a, frozenset()) and task_a not in reach.get(
        task_b, frozenset()
    )


def effective_resources(
    slot: SlotView, wp_shared: dict[str, Any] | None
) -> set[tuple[str, str]]:
    """The slot's declared resource set: per-slot override, else the WP-level
    declaration (squad design §6).  Only the closed category shape is read;
    unknown keys are ignored (the write path validates the slot shape)."""
    src = slot.shared_resources if slot.shared_resources is not None else wp_shared
    out: set[tuple[str, str]] = set()
    if not isinstance(src, dict):
        return out
    for category in RESOURCE_CATEGORIES:
        values = src.get(category)
        if isinstance(values, list):
            for value in values:
                out.add((category, str(value)))
    return out


def _mutator_slots(ctx: WpContext) -> list[SlotView]:
    return [s for s in ctx.slots if s.kind in MUTATOR_SLOT_KINDS and s.task_id is not None]


def resolve_candidates(
    ctx: WpContext, active_agent_ids: frozenset[uuid.UUID]
) -> tuple[dict[uuid.UUID, uuid.UUID], list[ConflictEntry]]:
    """§4.2 + REV-1: pick EXACTLY one active agent per materialized task.

    Builders resolve first (first survivor in planner-declared order); in a
    ``requires_independent_review`` package a review slot may only pick a
    candidate DISJOINT from every builder's pick (REV-1).  Returns
    (task_id -> chosen agent, fail-closed conflicts).  Deterministic: the
    same context + roster always yields the same mapping (concurrency
    convergence, see module docstring).
    """
    conflicts: list[ConflictEntry] = []
    chosen: dict[uuid.UUID, uuid.UUID] = {}
    builder_picks: dict[uuid.UUID, uuid.UUID] = {}

    def _survivors(slot: SlotView) -> list[uuid.UUID]:
        return [c for c in slot.candidate_agent_ids if c in active_agent_ids]

    # Phase 1 — every non-review (builder / gate / other) slot first, so the
    # review disjointness check in phase 2 sees ALL builder picks regardless
    # of the slots' declared order.
    for slot in ctx.slots:
        if slot.task_id is None or slot.kind == "review":
            continue  # not yet materialized: the translator materializes,
            # this lane only writes facts — open slots are not assignable.
        survivors = _survivors(slot)
        if not survivors:
            conflicts.append(
                ConflictEntry(
                    rule="§4.2",
                    code=PL_NO_CANDIDATE_AGENT,
                    detail=(
                        f"slot {slot.slot!r} has no live active same-tenant candidate "
                        f"agent (candidates={[str(c) for c in slot.candidate_agent_ids]})"
                    ),
                    slots=(slot.slot,),
                    task_ids=(slot.task_id,),
                )
            )
            continue
        pick = survivors[0]
        chosen[slot.task_id] = pick
        builder_picks[slot.task_id] = pick

    # Phase 2 — review slots: a flagged package's reviewer must be disjoint
    # from every builder pick (REV-1); unflagged review slots take their
    # first survivor like any other slot.
    for slot in ctx.slots:
        if slot.task_id is None or slot.kind != "review":
            continue
        survivors = _survivors(slot)
        if not survivors:
            conflicts.append(
                ConflictEntry(
                    rule="§4.2",
                    code=PL_NO_CANDIDATE_AGENT,
                    detail=(
                        f"slot {slot.slot!r} has no live active same-tenant candidate "
                        f"agent (candidates={[str(c) for c in slot.candidate_agent_ids]})"
                    ),
                    slots=(slot.slot,),
                    task_ids=(slot.task_id,),
                )
            )
            continue
        if ctx.requires_independent_review:
            blocked = set(builder_picks.values())
            disjoint = [c for c in survivors if c not in blocked]
            if not disjoint:
                conflicts.append(
                    ConflictEntry(
                        rule="REV-1",
                        code=PL_REVIEWER_NOT_INDEPENDENT,
                        detail=(
                            f"review slot {slot.slot!r} has no active candidate disjoint from the "
                            f"builder agents {sorted(str(a) for a in blocked)}"
                        ),
                        slots=(slot.slot,),
                        task_ids=(slot.task_id,),
                    )
                )
                continue
            pick = disjoint[0]
        else:
            pick = survivors[0]
        chosen[slot.task_id] = pick
    return chosen, conflicts


def check_review_dag(ctx: WpContext, chosen: dict[uuid.UUID, uuid.UUID]) -> list[ConflictEntry]:
    """REV-2: the reviewer task must block every builder task via the DAG.

    ``requires_independent_review`` also REQUIRES at least one review slot; a
    flagged package without one is not independently reviewable (REV-1).
    A missing blocking edge is reported under the same closed code — the
    reviewer cannot be said independent of the work it does not gate.
    """
    conflicts: list[ConflictEntry] = []
    if not ctx.requires_independent_review:
        return conflicts
    review_slots = [s for s in ctx.slots if s.kind == "review" and s.task_id is not None]
    builders = [t for t in ctx.builder_task_ids()]
    if not review_slots:
        conflicts.append(
            ConflictEntry(
                rule="REV-1",
                code=PL_REVIEWER_NOT_INDEPENDENT,
                detail="work package requires independent review but declares no review slot",
            )
        )
        return conflicts
    reach = _transitive_deps(ctx.dag_edges)
    for builder_task in builders:
        gated = [
            r.task_id
            for r in review_slots
            if builder_task in reach.get(r.task_id, frozenset())
        ]
        if not gated:
            conflicts.append(
                ConflictEntry(
                    rule="REV-2",
                    code=PL_REVIEWER_NOT_INDEPENDENT,
                    detail=(
                        f"builder task {builder_task} is not blocked by any review task in the DAG"
                    ),
                    slots=tuple(s.slot for s in review_slots),
                    task_ids=(builder_task,),
                )
            )
    return conflicts


def check_resource_conflicts(ctx: WpContext, chosen: dict[uuid.UUID, uuid.UUID]) -> list[ConflictEntry]:
    """CONF-1 / CONF-4: concurrent-possible mutators sharing a resource id.

    A pair of the SAME chosen agent is tagged CONF-4 (the G4 consequence:
    without a lane key two ready tasks of one agent can only be serialized
    by the DAG); a pair of distinct agents is tagged CONF-1 (the runtime
    Redis lock serializes the write but not the intent collision).  Both
    fail closed with ``PL_RESOURCE_CONFLICT``.
    """
    conflicts: list[ConflictEntry] = []
    mutators = _mutator_slots(ctx)
    reach = _transitive_deps(ctx.dag_edges)
    resources = {
        s.task_id: effective_resources(s, ctx.wp_shared_resources) for s in mutators
    }
    agents = {s.task_id: chosen.get(s.task_id) for s in mutators}
    for i, a in enumerate(mutators):
        for b in mutators[i + 1 :]:
            if not concurrent_possible(a.task_id, b.task_id, reach):
                continue  # the DAG serializes the pair — no concurrency risk
            shared = resources[a.task_id] & resources[b.task_id]
            if not shared:
                continue
            for category, resource_id in sorted(shared):
                rule = (
                    "CONF-4"
                    if agents[a.task_id] is not None
                    and agents[a.task_id] == agents[b.task_id]
                    else "CONF-1"
                )
                conflicts.append(
                    ConflictEntry(
                        rule=rule,
                        code=PL_RESOURCE_CONFLICT,
                        detail=(
                            f"concurrent-possible mutator slots {a.slot!r} and {b.slot!r} both "
                            f"mutate {category}:{resource_id}; add a serializing DAG edge or "
                            f"split the resource"
                        ),
                        resource=(category, resource_id),
                        slots=(a.slot, b.slot),
                        task_ids=(a.task_id, b.task_id),
                    )
                )
    return conflicts


def check_parallelism_advisory(ctx: WpContext) -> list[ConflictEntry]:
    """CONF-5 (advisory): a concurrency fan-out beyond the WP's hint bound.

    Counted = mutator slots that are concurrent-possible with at least one
    other mutator slot.  A breach is reported, never refused (the global cap
    is the deferred runtime change D-2; V1 keeps the predicate visible but
    non-failing, per squad design §6 CONF-5).
    """
    advisories: list[ConflictEntry] = []
    if ctx.max_parallel_tasks is None:
        return advisories
    mutators = _mutator_slots(ctx)
    reach = _transitive_deps(ctx.dag_edges)
    concurrent_count = sum(
        1
        for s in mutators
        if any(
            s.task_id != o.task_id
            and concurrent_possible(s.task_id, o.task_id, reach)
            for o in mutators
        )
    )
    if concurrent_count > ctx.max_parallel_tasks:
        advisories.append(
            ConflictEntry(
                rule="CONF-5",
                code=PL_PARALLELISM_ADVISORY,
                detail=(
                    f"{concurrent_count} concurrent-possible mutator slots exceed the "
                    f"max_parallel_tasks hint {ctx.max_parallel_tasks} (advisory only in V1)"
                ),
                slots=tuple(s.slot for s in mutators),
                task_ids=tuple(s.task_id for s in mutators),
            )
        )
    return advisories


def evaluate_wp(
    ctx: WpContext, active_agent_ids: frozenset[uuid.UUID]
) -> tuple[dict[uuid.UUID, uuid.UUID], ConstraintReport]:
    """Run the full constraint layer over one package context.

    Returns ``(chosen_agents, report)`` where ``chosen_agents`` maps each
    materialized task to its single pick (deterministic) and ``report``
    carries every conflict + advisory + review binding.  The caller refuses
    the package when ``report.fail_closed_codes()`` is non-empty.
    """
    conflicts: list[ConflictEntry] = []
    chosen, resolution_conflicts = resolve_candidates(ctx, active_agent_ids)
    conflicts.extend(resolution_conflicts)
    conflicts.extend(check_review_dag(ctx, chosen))
    conflicts.extend(check_resource_conflicts(ctx, chosen))
    advisories = check_parallelism_advisory(ctx)

    bindings: list[ReviewBinding] = []
    if ctx.requires_independent_review:
        reach = _transitive_deps(ctx.dag_edges)
        direct = {
            (task_id, dep_on) for task_id, dep_on in ctx.dag_edges
        }
        for slot in ctx.slots:
            if slot.kind != "review" or slot.task_id is None:
                continue
            agent = chosen.get(slot.task_id)
            if agent is None:
                continue
            reviewed = tuple(
                t for t in ctx.builder_task_ids() if t in reach.get(slot.task_id, frozenset())
            )
            bindings.append(
                ReviewBinding(
                    reviewer_task_id=slot.task_id,
                    reviewer_agent_id=agent,
                    reviewed_task_ids=reviewed,
                    blocking_edges=tuple(
                        (slot.task_id, t) for t in reviewed if (slot.task_id, t) in direct
                    ),
                )
            )

    report = ConstraintReport(
        conflicts=tuple(conflicts),
        advisories=tuple(advisories),
        review_bindings=tuple(bindings),
    )
    return chosen, report


# ---------------------------------------------------------------------------
# Context construction from persisted rows (pure over ORM objects).
# ---------------------------------------------------------------------------


def build_wp_context(
    *,
    wp: WorkPackage,
    goal_capabilities: list[str] | None,
    linked_tasks: Sequence[Task],
    dag_edges: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> WpContext:
    """Assemble the evaluation context from one work package's persisted data.

    Slot <-> task pairing is by TITLE, not by position: the translator
    materializes each slot into a Task carrying the slot's ``title`` (the
    planning service sets ``Task.title`` from the slot entry), and the
    ``work_package_tasks`` link rows have random UUID primary keys, so the
    DAO's link-row ordering is NOT a stable positional contract.  Within one
    work package slot titles are unique (the translator derives them per
    slot, and the DAO's slot-shape validation keeps the ids closed), so the
    title is the recoverable join key.  A linked task whose title matches no
    slot is a data defect and fails closed at the caller.
    """
    scope = wp.task_scope or []
    tasks_by_title: dict[str, Task] = {t.title: t for t in linked_tasks}
    slots: list[SlotView] = []
    used_tasks: set[uuid.UUID] = set()
    for i, entry in enumerate(scope):
        title = str(entry.get("title", ""))
        task = tasks_by_title.get(title)
        if task is None:
            raise ValueError(
                f"slot {entry.get('slot', f'slot-{i}')!r} (title {title!r}) matches no linked "
                "task in this work package"
            )
        if task.id in used_tasks:
            raise ValueError(
                f"task {task.id} is linked to more than one slot in this work package"
            )
        used_tasks.add(task.id)
        raw_candidates = entry.get("candidate_agent_ids") or []
        candidates: list[uuid.UUID] = []
        for raw in raw_candidates:
            candidates.append(uuid.UUID(str(raw)))
        slots.append(
            SlotView(
                slot=str(entry.get("slot", f"slot-{i}")),
                task_id=task.id,
                kind=str(entry.get("kind", "other")),
                candidate_agent_ids=tuple(candidates),
                shared_resources=entry.get("shared_resources"),
            )
        )
    return WpContext(
        wp_id=wp.id,
        execution_mode=wp.execution_mode,
        wp_shared_resources=wp.shared_resources,
        requires_independent_review=bool(wp.requires_independent_review),
        max_parallel_tasks=wp.max_parallel_tasks,
        goal_capabilities=tuple(goal_capabilities or ()),
        slots=tuple(slots),
        task_agent={t.id: t.agent_id for t in linked_tasks},
        dag_edges=tuple(dag_edges),
    )


def render_assignment_plan(
    wp_id: uuid.UUID,
    ctx: WpContext,
    chosen: dict[uuid.UUID, uuid.UUID],
    report: ConstraintReport,
) -> dict[str, Any]:
    """The §8 AssignmentPlan artifact — a computed report, NOT a table.

    Shape per squad design §8: ``{work_package_id, slots:
    [{slot, task_id, chosen_agent_id, kind}], constraint_report,
    review_bindings}``.  The authoritative assignments live only in
    ``Task.agent_id``; this dict is returned/logged for the planner and
    the review lane.
    """
    slots_payload: list[dict[str, Any]] = []
    for slot in ctx.slots:
        agent = chosen.get(slot.task_id) if slot.task_id is not None else None
        slots_payload.append(
            {
                "slot": slot.slot,
                "task_id": str(slot.task_id) if slot.task_id is not None else None,
                "chosen_agent_id": str(agent) if agent is not None else None,
                "kind": slot.kind,
            }
        )
    return {
        "work_package_id": str(wp_id),
        "slots": slots_payload,
        "constraint_report": report.to_dict(),
        "review_bindings": report.to_dict()["review_bindings"],
    }


# ---------------------------------------------------------------------------
# The transactional writer.
# ---------------------------------------------------------------------------


@dataclass
class AssignmentOutcome:
    """The transport shape of an apply_assignment attempt.

    - ``state="assigned"`` — the constraint layer passed (or was advisory-only);
      the picks are written to the single ``Task.agent_id`` fact and the
      ``assignment_plan`` artifact is returned.
    - ``state="failed"``  — a closed code refused the package BEFORE any
      write (fail-closed: the planner is re-invoked or a human resolves;
      nothing silently proceeds).
    """

    state: str
    code: str | None = None
    detail: str = ""
    assignment_plan: dict[str, Any] | None = None
    constraint_report: dict[str, Any] = field(default_factory=dict)


class AssignmentService:
    """The Agent-Assignment lane over a materialized ProjectPlan (design §8)."""

    async def apply_assignment(
        self,
        db,
        *,
        work_package_id: uuid.UUID,
        current_user: User,
    ) -> AssignmentOutcome:
        """Resolve + validate + bind one work package's tasks to single agents.

        Pipeline (fail-closed, one transaction, no new persistence):
          G0  the acting user must carry a tenant (403-class
              ``AssignmentSecurity``, never a silent cross-tenant 404).
          M9  every read then runs inside the user's ``tenant_context``:
              the scoped assignment-INPUT view (WP + goal + linked tasks)
              resolves a cross-tenant work-package id to ``None`` (the ORM
              tenant-injection filter — no leaked foreign row), and the
              live-roster batch read can never return a foreign tenant's
              agents.
          G1  the owning planning run must be terminal ``PL_COMPLETED``
              (assignment runs after materialization, never mid-transaction;
              ``PL_RUN_NOT_COMPLETED`` otherwise).
          R   re-validate every candidate against the live roster (active,
              not expired, same tenant — the Phase 2E P5 availability gate);
          C   run the pure constraint layer (CONF-1..5 + REV-1..3) over the
              context; ANY fail-closed code refuses the whole package with
              the report attached;
          W   write each pick to ``Task.agent_id`` (one writer, one fact;
              unchanged facts are not re-written — a re-apply is a zero-write
              no-op, the concurrency convergence the module docstring states);
          P   return the AssignmentPlan artifact (§8, report only).
        """
        outcome = AssignmentOutcome(state="pending")

        target_tenant = current_user.tenant_id
        if target_tenant is None:
            raise AssignmentSecurity("assignment has no tenant context; refusing to run")

        # M9 — every read below runs inside the acting user's tenant context:
        # the ORM tenant-injection hook filters the scoped reads so a
        # cross-tenant work package id resolves to ``None`` (not a leaked
        # foreign row, no 403-vs-404 disclosure) and the live-roster batch
        # read can never return a foreign tenant's agents.
        with tenant_context(target_tenant):
            view = await work_package_dao.get_assignment_candidates_for_work_package(
                work_package_id, db=db
            )
            if view is None:
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = "work package not found in this tenant"
                return outcome
            wp: WorkPackage = view["work_package"]
            linked_tasks: list[Task] = list(view["linked_tasks"])

            run = await planning_run_dao.get_scoped(wp.planning_run_id, db=db)
            if run is None:
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = f"planning run {wp.planning_run_id} is missing in this tenant"
                return outcome
            if run.status != "PL_COMPLETED":
                outcome.state = "failed"
                outcome.code = PL_RUN_NOT_COMPLETED
                outcome.detail = (
                    f"planning run {wp.planning_run_id} is {run.status!r}; "
                    "assignment requires PL_COMPLETED"
                )
                return outcome

            scope = wp.task_scope or []
            if len(scope) != len(linked_tasks):
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = (
                    f"task_scope has {len(scope)} slots but {len(linked_tasks)} linked tasks; "
                    "the materialization contract is broken"
                )
                return outcome
            if len(scope) > MAX_WP_SLOTS:
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = f"work package carries {len(scope)} slots, exceeding the {MAX_WP_SLOTS} bound"
                return outcome

            task_ids = [t.id for t in linked_tasks]
            edges = await task_dependency_dao.list_edges_for(
                task_ids, db=db, limit=MAX_WP_EDGES
            )
            if len(edges) >= MAX_WP_EDGES:
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = (
                    f"work package carries {len(edges)}+ dependency edges, exceeding the "
                    f"{MAX_WP_EDGES} bounded-read cap; refusing to evaluate a truncated DAG"
                )
                return outcome
            dag_edges: tuple[tuple[uuid.UUID, uuid.UUID], ...] = tuple(
                (e.task_id, e.depends_on_task_id) for e in edges
            )

            # Re-validate candidates against the LIVE roster (one bounded batch
            # read, no N+1): availability = row exists (not soft-deleted) +
            # primary model bound + not expired — the Phase 2E P5 gate.  The
            # read is tenant-injected, so foreign-tenant agents are dropped.
            candidate_ids = sorted(
                {
                    c
                    for entry in scope
                    for c in (uuid.UUID(str(x)) for x in (entry.get("candidate_agent_ids") or []))
                }
            )
            active: frozenset[uuid.UUID] = frozenset()
            if candidate_ids:
                roster = list(await agent_dao.list_by_ids(candidate_ids, db=db))
                active = frozenset(
                    a.id
                    for a in roster
                    if a.primary_model_id is not None and not is_agent_expired(a)
                )

            try:
                ctx = build_wp_context(
                    wp=wp,
                    goal_capabilities=view["required_capabilities"],
                    linked_tasks=linked_tasks,
                    dag_edges=dag_edges,
                )
            except ValueError as exc:
                # Slot/task title-pairing defect (a data bug in the persisted
                # plan) — fail closed with a closed code, never a 500.
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = str(exc)[:500]
                return outcome
            try:
                chosen, report = evaluate_wp(ctx, active)
            except ValueError as exc:
                # A dependency closure beyond MAX_WP_EDGES (bounded-graph
                # discipline, mirrors the task graph service's fail-closed cap).
                outcome.state = "failed"
                outcome.code = PL_INVALID_INPUT
                outcome.detail = str(exc)[:500]
                return outcome

            fail_codes = report.fail_closed_codes()
            plan = render_assignment_plan(wp.id, ctx, chosen, report)
            outcome.constraint_report = report.to_dict()

            if fail_codes:
                outcome.state = "failed"
                outcome.code = fail_codes[0]
                outcome.detail = "; ".join(
                    f"[{c.rule}] {c.detail}" for c in report.conflicts if c.code in fail_codes
                )[:1000]
                outcome.assignment_plan = plan
                return outcome

            # W — write the single assignment fact; an unchanged binding is a
            # no-op, so a racing second apply never flips the fact to a second
            # agent (the deterministic-pick convergence, module docstring).
            task_by_id = {t.id: t for t in linked_tasks}
            for task_id, agent_id in chosen.items():
                task = task_by_id.get(task_id)
                if task is None:
                    continue
                if task.agent_id != agent_id:
                    await task_provenance_dao.update_agent_binding(
                        task, agent_id=agent_id, db=db
                    )

        outcome.state = "assigned"
        outcome.code = PL_OK
        outcome.detail = ""
        outcome.assignment_plan = plan
        return outcome


assignment_service = AssignmentService()

__all__ = [
    "ASSIGNMENT_FAIL_CODES",
    "ASSIGNMENT_RESULT_CODES",
    "MAX_WP_EDGES",
    "MAX_WP_SLOTS",
    "MUTATOR_SLOT_KINDS",
    "PL_INVALID_INPUT",
    "PL_NO_CANDIDATE_AGENT",
    "PL_OK",
    "PL_PARALLELISM_ADVISORY",
    "PL_RESOURCE_CONFLICT",
    "PL_REVIEWER_NOT_INDEPENDENT",
    "PL_RUN_NOT_COMPLETED",
    "RESOURCE_CATEGORIES",
    "AssignmentOutcome",
    "AssignmentSecurity",
    "AssignmentService",
    "ConflictEntry",
    "ConstraintReport",
    "ReviewBinding",
    "SlotView",
    "WpContext",
    "assignment_service",
    "build_wp_context",
    "check_parallelism_advisory",
    "check_resource_conflicts",
    "check_review_dag",
    "concurrent_possible",
    "effective_resources",
    "evaluate_wp",
    "render_assignment_plan",
    "resolve_candidates",
]

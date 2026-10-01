"""The Phase 4 Completion lane — derived CT / CW / CP predicates + C5 decision rows.

Implements the V1 completion contract of design card ``t_4185daed``
(docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md §3–§5)
on top of the two parent ledger tables owned by card ``t_436ddafb``
(``artifact_records`` / ``evidence_records``) and the frozen Phase 2C–3 task
graph + planning link tables.  This card owns the completion-lane *decision*
only; the delivery *record* (``delivery_records``) belongs to ``t_fa30ea5d``.

Design decisions honored (design §1 C0–C8):

- **C0** — no new table / column / persisted status.  Completion at Task /
  Work Package / Project level is a *derived predicate* computed live; the
  only durable trace is one append-only ``evidence_records kind='structured'``
  decision row per evaluation (C5).
- **C1** — ``CT(T)`` (§3.1), first failing term in stack order wins (§3.5):
  ``T.type='todo'`` (C7) → ``|A_c(T)|>=1`` → ``A_c(T) ⊆ SEALED`` → a
  current-valid review over ``A_c(T)`` with ``outcome='pass'`` → the reviewer
  is disjoint from the WP's builders (G1 read-side mirror, invariant 13) → no
  open REQUEST_CHANGES / unconsumed inconclusive on the current set →
  ``deps_done(T)`` (every *transitive* dependency satisfies CT recursively,
  memoized; a cycle is unreadable → the catch-all) → all terms hold.
  ``Task.status`` / ``final_answer`` / the gate verdict are NEVER inputs
  (R-A, Root §5).
- **C2** — ``CW(W)`` (§4), the §4 code-stack order: ``CP_NO_WORK`` → the CT
  code of the first failing materialized task (named in ``failing_task_id``)
  → ``CP_OPEN_SLOT`` → ``CP_TENANT_MISMATCH`` → ``CP_EVAL_ERROR``.  The
  ``requires_independent_review`` term lives INSIDE each CT (invariant 13),
  never re-checked at WP level (one-authority rule).
- **C3** — ``CP(P)`` (§5): every in-scope ``type='todo'`` task satisfies CT
  AND every ``kind='delivery'`` milestone WP of the LATEST PL_COMPLETED
  planning run satisfies CW (gate/phase milestones are ordering buckets,
  never completion state; an empty delivery set is a satisfied term) AND the
  tenant boundary holds.  ``CP_OK`` licenses the lane's single owning write
  site: ``Project.status='COMPLETED'`` (today zero write sites — audit Q10),
  published only on a FRESH recomputed ``CP_OK`` + executable status + tenant
  match, idempotent by construction (§9).
- **C5 / design-conflict adjudication (card t_e399386f, live f072 probe)** —
  the parent's invariant-5 partial-unique index ``uq_evidence_records_
  reverify`` (``WHERE NOT payload ? 'reverify_of'``) empirically blocks a 2nd
  decision row on the same ``subject_ref``.  The resolution is additive: C5
  rows chain via ``payload.reverify_of`` = the previous decision row's id for
  the same subject (``None`` / omitted for the first), which EXCLUDES them
  from the partial index (the AgentRunEvent precedent) — repeated evaluations
  append freely while staying chained, and the only correct read is the
  LATEST row per scope (§9 staleness note).  The bounded read is
  ``EvidenceRecordDAO.latest_decision_row_for_subject``.
- **C6** — closed decision-code sets ``CP_*`` / ``CD_*`` (§3.5 / §6.2),
  fail-closed: unknown or unreadable inputs fall through to the catch-all and
  never to the OK code.
- **C7** — the completion scope is ``type='todo'`` execution tasks only;
  supervision tasks keep their frozen re-pend owner.
- **C8** — the ``TaskCompletionGate._fail_open`` defect stays OUT (separate
  card, Root §5); CT reads ledger proof rows, never the gate's LLM call.

Structure (Phase 3 discipline: pure core + bounded transactional writes):

- The **pure, DB-free predicate core** (module-level functions) implements the
  CT / CW / CP stacks over in-memory bundles so every ``CP_*`` path is
  unit-testable without Postgres (cycle → ``CP_EVAL_ERROR``, open slot,
  tenant mismatch, unexpected outcome, ...).
- The **DB-bound service** (:class:`CompletionService`) binds the caller's
  tenant (D6 scope-inject), reads through the tenant-scoped DAOs (every read
  re-asserts the tenant — the I-7 shape), runs the pure core, writes the ONE
  ``C5`` decision row per evaluation (chained), and — on a fresh ``CP_OK``
  project — publishes ``COMPLETED`` exactly once through the single owning
  write site (C3).  The lane writes NOTHING else: no Task.status, no seal, no
  artifact row, no delivery record (those belong to the review / delivery
  lanes).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.dao.artifact_evidence_dao import (
    ArtifactEvidenceClosedError,
    artifact_record_dao,
    evidence_record_dao,
)
from app.dao.base import tenant_context
from app.dao.planning_dao import (
    planning_run_dao,
    work_package_dao,
    work_package_task_dao,
)
from app.dao.project_intake_dao import project_dao
from app.dao.task_dao import task_dependency_dao, task_provenance_dao
from app.models.artifact_evidence import (
    MAX_EVIDENCE_PAYLOAD_BYTES,
    ArtifactRecord,
    EvidenceRecord,
)
from app.models.planning import WorkPackage, WorkPackageTask
from app.models.project import Project
from app.models.task import Task
from app.services.review_rework_service import current_valid_review

# ---------------------------------------------------------------------------
# Closed decision-code sets (design §3.5 / §6.2, decision C6).  Service-side
# only: a closed vocabulary, not a persisted state machine (the f069
# "String, closed" + service re-validation precedent).  Unknown / unreadable
# inputs fail closed to the catch-all, NEVER to the OK code (Root §5).
# ---------------------------------------------------------------------------

#: Task / Work Package / Project completion codes (design §3.5).
CP_OK = "CP_OK"
#: |A_c(T)| = 0 — no artifacts to complete (WP: no materialized task;
#: Project: no in-scope task).
CP_NO_WORK = "CP_NO_WORK"
#: Some current artifact is still DRAFT (sealing is the APPROVE-driven
#: review lane's write — not the completion lane's).
CP_NOT_SEALED = "CP_NOT_SEALED"
#: No current-valid review row over A_c(T) exists yet.
CP_NO_APPROVING_REVIEW = "CP_NO_APPROVING_REVIEW"
#: The current-valid pass row's reviewer is a builder on the WP (read-side
#: mirror of invariant 13; fail-closed even if a bad row ever reached storage).
CP_REVIEW_NOT_INDEPENDENT = "CP_REVIEW_NOT_INDEPENDENT"
#: An open REQUEST_CHANGES on the current set (current-valid row 'fail').
CP_OPEN_REQUEST_CHANGES = "CP_OPEN_REQUEST_CHANGES"
#: An unconsumed 'inconclusive' row on the current set (parked; it can never
#: satisfy completion — review card I-6).
CP_INCONCLUSIVE_REVIEW = "CP_INCONCLUSIVE_REVIEW"
#: >= 1 transitive dependency fails CT recursively.
CP_DEPS_NOT_DONE = "CP_DEPS_NOT_DONE"
#: A task, artifact, or review row crosses tenants (read-side re-assertion;
#: the DAO scope-inject makes it unreachable, not merely prevented — I-7).
CP_TENANT_MISMATCH = "CP_TENANT_MISMATCH"
#: A WorkPackage has an open slot (task_id NULL) — work not fully
#: materialized; completion impossible (WP-level, §4).
CP_OPEN_SLOT = "CP_OPEN_SLOT"
#: Catch-all: unreadable input / unexpected ledger shape — fail closed,
#: never CP_OK.
CP_EVAL_ERROR = "CP_EVAL_ERROR"

#: The closed CP_* set (every named completion code).
CP_RESULT_CODES = frozenset(
    {
        CP_OK,
        CP_NO_WORK,
        CP_NOT_SEALED,
        CP_NO_APPROVING_REVIEW,
        CP_REVIEW_NOT_INDEPENDENT,
        CP_OPEN_REQUEST_CHANGES,
        CP_INCONCLUSIVE_REVIEW,
        CP_DEPS_NOT_DONE,
        CP_TENANT_MISMATCH,
        CP_OPEN_SLOT,
        CP_EVAL_ERROR,
    }
)

#: Delivery decision codes (design §6.2, decision C4) — the closed delivery
#: contract the ``t_fa30ea5d`` record lane consumes.  This card fixes the
#: contract; it does NOT own the delivery record / table / DAO.
CD_OK = "CD_OK"
#: CW/CP (as applicable) is not CP_OK — there is no input to build a
#: delivery from (the structural root of "Agent says done → Delivery
#: impossible").
CD_NOT_COMPLETED = "CD_NOT_COMPLETED"
#: A delivery cites an artifact that is DRAFT, or whose approving review row
#: is not current-valid over the cited set (superseded ⇒ stale).
CD_NO_SEALED_APPROVED = "CD_NO_SEALED_APPROVED"
#: Destination outside the closed V1 vocabulary (C-D2).
CD_DESTINATION_INVALID = "CD_DESTINATION_INVALID"
#: Catch-all; fail closed.
CD_EVAL_ERROR = "CD_EVAL_ERROR"

#: The closed CD_* set (the delivery decision contract, §6.2).
CD_RESULT_CODES = frozenset(
    {
        CD_OK,
        CD_NOT_COMPLETED,
        CD_NO_SEALED_APPROVED,
        CD_DESTINATION_INVALID,
        CD_EVAL_ERROR,
    }
)

#: The closed V1 delivery destination kinds (C-D2, design §6.2).  No external
#: publish platform in V1 (Root §6): adding a kind is a reviewable closed-set
#: extension, not a code path.
DELIVERY_DESTINATION_KINDS = ("channel", "published_page", "project_record")

#: The C5 decision row's closed provenance vocabulary (design §3.3 / §5.3, C5).
C5_DECISION = "completion_evaluated"
#: The C5 subject namespaces, one per evaluation scope.
C5_SCOPES = ("task", "wp", "project")

#: The Project status group that may receive the COMPLETED projection — the
#: frozen ``PROJECT_EXECUTABLE_STATUSES`` mirror (task_execution_service.py
#: §4 P2: this lane depends on the set, it does not own the Project SM).
PROJECT_EXECUTABLE_STATUSES = frozenset({"ANALYZING", "PENDING_CONFIRMATION", "EXECUTING"})

#: The lane's bounded read caps (complete-or-absent: an over-cap graph is
#: unreadable → the catch-all, never a silent partial).
_MAX_SCOPE_TASKS = 1000
_MAX_DEP_HOPS = 32
_MAX_EDGES_PER_BATCH = 200


class CompletionEvaluationError(Exception):
    """A fail-closed gate of the completion lane.

    Carries a ``code`` (a member of :data:`CP_RESULT_CODES` for the predicate
    / write path, or :data:`CD_RESULT_CODES` for the delivery contract) so the
    owning transport maps it without re-parsing the message (mirrors
    ``ReviewReworkError`` / ``ArtifactEvidenceClosedError``).
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}" + (f": {message}" if message else ""))


# ---------------------------------------------------------------------------
# Pure, DB-free evaluation inputs (the read-side rows the predicates consume).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskEvalInput:
    """The read-side bundle for one Task's CT evaluation (design §3.3 step 1).

    ``artifacts`` is the task's *entire* artifact set (current + historical)
    so the core can recompute ``A_c(T)`` (the non-superseded set) and the
    current-valid review over it — the review card §3.4 shape.  ``reviews``
    are the ``kind='review'`` verdict rows touching the task's artifacts.
    ``builder_agent_ids`` is the read-side independence fact (invariant 13
    mirror): the builder agent ids on the owning WorkPackage.  ``None`` means
    the set is UNKNOWN — with an agent reviewer the G1 mirror cannot be
    evaluated and the term fails closed to the catch-all (never CP_OK).

    The DB-free core types these as the *frozen* ledger models so it can call
    :func:`current_valid_review` (which requires the real types); the pure
    unit tests pass in-memory stand-in rows whose attributes duck-type the
    same way (``getattr`` access, no isinstance check).
    """

    task: Task
    artifacts: Sequence[ArtifactRecord] = ()
    reviews: Sequence[EvidenceRecord] = ()
    builder_agent_ids: frozenset[uuid.UUID] | None = None

    def current_artifacts(self) -> list[object]:
        """The current (non-superseded) artifact set A_c(T)."""
        return [a for a in self.artifacts if getattr(a, "superseded_by", None) is None]


@dataclass(frozen=True)
class TaskEvalContext:
    """What one CT evaluation reads: a task→bundle map + the frozen
    direct-dependency graph (task id → its direct dependency ids).

    ``tasks`` must contain the bundle for every task reachable as a
    *transitive* dependency of the one being checked (the recursion reads
    each dep's bundle).  A missing bundle for a reachable dep is an
    unreadable input → ``CP_EVAL_ERROR`` (fail-closed, Root §5); a cycle is
    the same.
    """

    tasks: Mapping[uuid.UUID, TaskEvalInput]
    direct_deps: Mapping[uuid.UUID, Sequence[uuid.UUID]]


@dataclass(frozen=True)
class WorkPackageEvalInput:
    """The read-side bundle for one WorkPackage's CW evaluation (design §4).

    ``task_bundles`` is the full closure map (the materialized tasks plus all
    their transitive dep targets); ``wp_task_ids`` is the set of materialized
    task ids the WP-level term actually checks (the open slots are excluded
    and fail ``CP_OPEN_SLOT`` separately).
    """

    work_package: WorkPackage
    slots: Sequence[WorkPackageTask] = ()
    wp_task_ids: Sequence[uuid.UUID] = ()
    task_bundles: Mapping[uuid.UUID, TaskEvalInput] = field(default_factory=dict)
    direct_deps: Mapping[uuid.UUID, Sequence[uuid.UUID]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectEvalInput:
    """The read-side bundle for one Project's CP evaluation (design §5).

    ``in_scope_task_ids`` are the project's ``type='todo'`` tasks (the term
    the CP predicate checks); ``task_bundles`` is the full closure map those
    ids + all their transitive deps read against.
    """

    project: Project
    in_scope_task_ids: Sequence[uuid.UUID] = ()
    task_bundles: Mapping[uuid.UUID, TaskEvalInput] = field(default_factory=dict)
    direct_deps: Mapping[uuid.UUID, Sequence[uuid.UUID]] = field(default_factory=dict)
    delivery_packages: Sequence[WorkPackageEvalInput] = ()


@dataclass(frozen=True)
class EvaluationResult:
    """The outcome of one predicate evaluation: the CP code + the cited inputs
    the owning service stamps into the C5 decision row."""

    code: str
    cited: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CompletionDecision:
    """The C5 decision-row shape (one payload per evaluation, §3.3 / §5.3).

    ``code`` is exactly one member of :data:`CP_RESULT_CODES`; ``subject_ref``
    is the C5 subject locator (``task://{id}`` / ``wp://{id}`` /
    ``project://{id}``); ``cited`` names the inputs the evaluation read: the
    cited artifact ids, the current-valid review row id, the dependency task
    ids, and — at WP / project level — the failing task id that drove the
    failure.  ``reverify_of`` is the previous decision row's id for the same
    subject (``None`` for the first) — the chain link that excludes the row
    from the invariant-5 partial-unique index.
    """

    code: str
    subject_ref: str
    scope: str
    cited: dict[str, object] = field(default_factory=dict)
    reverify_of: uuid.UUID | None = None

    def payload(self) -> dict[str, Any]:
        """The C5 decision-row payload (design §3.3 / §5.3, bounded)."""
        payload: dict[str, Any] = {
            "decision": C5_DECISION,
            "outcome": self.code,
            "scope": self.scope,
            "subject_ref": self.subject_ref,
            "cited": dict(self.cited),
        }
        if self.reverify_of is not None:
            payload["reverify_of"] = str(self.reverify_of)
        return payload


@dataclass(frozen=True)
class EvaluationActor:
    """Who triggers the evaluation: exactly one of ``agent_id`` /
    ``user_id`` (D5 XOR).  The C5 row's source fields mirror it."""

    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.agent_id is None) == (self.user_id is None):
            raise CompletionEvaluationError(
                CP_EVAL_ERROR, "an evaluation actor is exactly one of agent_id / user_id (D5 XOR)"
            )


@dataclass(frozen=True)
class CompletionResult:
    """The outcome of one DB-bound evaluation (+ optional COMPLETED
    publication).

    ``code`` is the CP code; ``decision`` is the C5 row appended (None when
    the row could not be written, e.g. a payload overrun — the evaluation
    code is still returned, complete-or-absent); ``project_completed`` is
    True only when this call published ``Project.status='COMPLETED'``;
    ``project_status`` is the status after the call (on a re-call over an
    already-terminal project it is the existing terminal value, unchanged).
    """

    code: str
    decision: EvidenceRecord | None = None
    project_completed: bool = False
    project_status: str | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# The pure CT predicate (design §3.1 / §3.5), memoized recursion over the
# frozen dependency graph.
# ---------------------------------------------------------------------------
def _ct_terms(
    ctx: TaskEvalContext,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    memo: dict[uuid.UUID, EvaluationResult],
    on_stack: set[uuid.UUID],
) -> EvaluationResult:
    """One task's CT term stack (design §3.1 order), memoized.

    Cycle-safe: a task reached while already on the recursion stack is an
    unreadable (circular) graph → the catch-all, never CP_OK.

    ONLY ``CP_OK`` results are cached: success is context-independent (all
    own terms hold AND all transitive deps are CP_OK, so it stays true no
    matter who asked).  A failure code is NOT cached — a task that failed in
    one context (e.g. as the far node of a cycle) may pass in another (a
    diamond whose failing sibling does not feed it), so failures recompute
    on every visit; the catch-all (cycle detection, missing bundle) stays
    sound either way because it is re-derived from the live stack.
    """
    if task_id in memo:
        return memo[task_id]
    if task_id in on_stack:
        return EvaluationResult(CP_EVAL_ERROR, {"cycle_at": str(task_id)})
    on_stack.add(task_id)
    try:
        result = _ct_terms_uncached(ctx, task_id, tenant_id, memo, on_stack)
    finally:
        on_stack.discard(task_id)
    if result.code == CP_OK:
        memo[task_id] = result
    return result


def _ct_terms_uncached(
    ctx: TaskEvalContext,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    memo: dict[uuid.UUID, EvaluationResult],
    on_stack: set[uuid.UUID],
) -> EvaluationResult:
    """The CT term stack, first failing term wins (design §3.1 / §3.5)."""
    inp = ctx.tasks.get(task_id)
    if inp is None or getattr(inp, "task", None) is None:
        # A dependency id with no readable bundle — the dep target is missing
        # / unreadable inside the tenant: fail closed, never CP_OK.
        return EvaluationResult(CP_EVAL_ERROR, {"unresolved_task_id": str(task_id)})
    task = inp.task

    # C7 scope: the predicate governs execution tasks only; a supervision /
    # out-of-scope row reaching this lane is unreadable input to it (its
    # completion stays in the frozen re-pend owner).
    if getattr(task, "type", "todo") != "todo":
        return EvaluationResult(CP_EVAL_ERROR, {"out_of_scope_task_id": str(task_id)})

    # tenant_ok(T): the task itself (read-side re-assertion, I-7 shape).
    task_tenant = getattr(task, "tenant_id", None)
    if tenant_id is not None and task_tenant is not None and task_tenant != tenant_id:
        return EvaluationResult(CP_TENANT_MISMATCH, {"task_id": str(task_id)})

    # Term (a): |A_c(T)| >= 1 (required artifacts exist, DRAFT or SEALED).
    current = inp.current_artifacts()
    if not current:
        return EvaluationResult(CP_NO_WORK, {"task_id": str(task_id)})

    # Terms (a/b): every current artifact is SEALED, in the same tenant.
    for artifact in current:
        art_tenant = getattr(artifact, "tenant_id", None)
        if tenant_id is not None and art_tenant is not None and art_tenant != tenant_id:
            return EvaluationResult(
                CP_TENANT_MISMATCH, {"artifact_id": str(getattr(artifact, "id", None))}
            )
        if getattr(artifact, "seal_status", None) != "SEALED":
            return EvaluationResult(
                CP_NOT_SEALED,
                {"artifact_ids": [str(getattr(a, "id", None)) for a in current]},
            )

    # Terms (b/c): the current-valid review over A_c(T) — its outcome, its
    # tenant, its reviewer independence (G1 read-side mirror, invariant 13).
    review = current_valid_review(inp.artifacts, inp.reviews)
    review_id = str(getattr(review, "id", None)) if review is not None else None
    if review is None:
        return EvaluationResult(CP_NO_APPROVING_REVIEW, {"task_id": str(task_id), "review_id": None})
    review_tenant = getattr(review, "tenant_id", None)
    if tenant_id is not None and review_tenant is not None and review_tenant != tenant_id:
        return EvaluationResult(CP_TENANT_MISMATCH, {"review_id": review_id})
    outcome = getattr(review, "outcome", None)
    if outcome == "fail":
        return EvaluationResult(CP_OPEN_REQUEST_CHANGES, {"review_id": review_id})
    if outcome == "inconclusive":
        return EvaluationResult(CP_INCONCLUSIVE_REVIEW, {"review_id": review_id})
    if outcome != "pass":
        # A verdict outside the closed EVIDENCE_OUTCOMES set — unexpected
        # ledger shape, fail closed, never CP_OK.
        return EvaluationResult(CP_EVAL_ERROR, {"review_id": review_id, "outcome": str(outcome)})
    reviewer = getattr(review, "created_by_agent", None)
    if reviewer is not None:
        builders = inp.builder_agent_ids
        if builders is None:
            # The G1 mirror needs the builder set; unknown set + an agent
            # reviewer = an unevaluable independence term → catch-all.  (A
            # user-captured review row has no agent reviewer: the term is
            # vacuously held — a user can never be in an agent builder set.)
            return EvaluationResult(CP_EVAL_ERROR, {"review_id": review_id, "builder_set_unknown": True})
        if reviewer in builders:
            return EvaluationResult(
                CP_REVIEW_NOT_INDEPENDENT, {"review_id": review_id, "reviewer": str(reviewer)}
            )

    # Term (d): deps_done(T) — every transitive dependency satisfies CT
    # recursively (memoized; a cycle in the frozen graph is the catch-all).
    cited: dict[str, object] = {
        "task_id": str(task_id),
        "artifact_ids": [str(getattr(a, "id", None)) for a in current],
        "review_id": review_id,
        "dep_task_ids": [str(d) for d in sorted(ctx.direct_deps.get(task_id, ()), key=str)],
    }
    for dep_id in ctx.direct_deps.get(task_id, ()):
        dep_result = _ct_terms(ctx, dep_id, tenant_id, memo, on_stack)
        if dep_result.code == CP_OK:
            continue
        if dep_result.code in (CP_DEPS_NOT_DONE, CP_EVAL_ERROR):
            # A deeper dependency does not complete, OR the graph itself is
            # unreadable (a cycle is detected at the dep and the catch-all is
            # its named code) — both surface as-is: the *named* failure is the
            # right one to cite, not a blanket "deps not done".
            return EvaluationResult(dep_result.code, {**cited, "failing_dep": str(dep_id)})
        # A readable dep that simply does not satisfy its own CT stack.
        return EvaluationResult(CP_DEPS_NOT_DONE, {**cited, "failing_dep": str(dep_id)})

    return EvaluationResult(CP_OK, cited)


def evaluate_ct(ctx: TaskEvalContext, task_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None) -> EvaluationResult:
    """The pure, DB-free ``CT(T)`` predicate (design §3.1) over one context.

    Returns the first failing code in stack order (§3.5) plus the cited
    inputs for the C5 decision row.  NEVER reads Task.status / final_answer /
    the gate verdict (R-A, Root §5).
    """
    return _ct_terms(ctx, task_id, tenant_id, {}, set())


# ---------------------------------------------------------------------------
# The pure CW predicate (design §4 code-stack order).
# ---------------------------------------------------------------------------
def evaluate_cw(inp: WorkPackageEvalInput, *, tenant_id: uuid.UUID | None = None) -> EvaluationResult:
    """The pure, DB-free ``CW(W)`` predicate (design §4).

    Code stack: ``CP_NO_WORK`` → the CT code of the first failing
    materialized task (named in ``failing_task_id``) → ``CP_OPEN_SLOT`` →
    ``CP_TENANT_MISMATCH`` → ``CP_EVAL_ERROR``.  The
    ``requires_independent_review`` term lives INSIDE each CT (invariant 13),
    never re-checked at WP level (one-authority rule).
    """
    wp_id = str(getattr(inp.work_package, "id", None))

    # CP_NO_WORK — no materialized task at all (every slot still open).
    materialized = list(inp.wp_task_ids)
    if not materialized:
        return EvaluationResult(CP_NO_WORK, {"wp_id": wp_id})

    # ∀ materialized task: CT holds (first failure reported + named).  The
    # context carries the full transitive closure so the recursion reads.
    task_ctx = TaskEvalContext(tasks=inp.task_bundles, direct_deps=inp.direct_deps)
    for task_id in sorted(materialized, key=str):
        result = evaluate_ct(task_ctx, task_id, tenant_id=tenant_id)
        if result.code != CP_OK:
            cited = dict(result.cited)
            cited["failing_task_id"] = str(task_id)
            cited["wp_id"] = wp_id
            return EvaluationResult(result.code, cited)

    # No open slot: any slot with task_id NULL is an UNRESOLVED BLOCKER
    # (fail-closed; open-slot policy is the planning lane's, design §4/§8).
    if any(s.task_id is None for s in inp.slots):
        return EvaluationResult(CP_OPEN_SLOT, {"wp_id": wp_id})

    # tenant_ok(W) — the WP row itself (I-7 read-side re-assertion; the
    # §4 stack order places it after the per-task codes and open slot).
    wp_tenant = getattr(inp.work_package, "tenant_id", None)
    if tenant_id is not None and wp_tenant is not None and wp_tenant != tenant_id:
        return EvaluationResult(CP_TENANT_MISMATCH, {"wp_id": wp_id})

    return EvaluationResult(CP_OK, {"wp_id": wp_id})


# ---------------------------------------------------------------------------
# The pure CP predicate (design §5 term order).
# ---------------------------------------------------------------------------
def evaluate_cp(inp: ProjectEvalInput, *, tenant_id: uuid.UUID | None = None) -> EvaluationResult:
    """The pure, DB-free ``CP(P)`` predicate (design §5).

    ``|in_scope_tasks(P)| >= 1`` → ∀ in-scope task CT → ∀ delivery-milestone
    WP CW (gate/phase milestones are ordering buckets, never completion
    state; an empty delivery set satisfies the term) → tenant_ok(P) → CP_OK.
    """
    project_id = str(getattr(inp.project, "id", None))

    in_scope = sorted(inp.in_scope_task_ids, key=str)
    if not in_scope:
        return EvaluationResult(CP_NO_WORK, {"project_id": project_id})

    # ∀ in-scope task: CT (deterministic order; the failing task is named).
    task_ctx = TaskEvalContext(tasks=inp.task_bundles, direct_deps=inp.direct_deps)
    for task_id in in_scope:
        result = evaluate_ct(task_ctx, task_id, tenant_id=tenant_id)
        if result.code != CP_OK:
            cited = dict(result.cited)
            cited["failing_task_id"] = str(task_id)
            cited["project_id"] = project_id
            return EvaluationResult(result.code, cited)

    # ∀ delivery-milestone WP: CW (the first failing WP's code, its failing
    # task named).  Empty delivery set = the term is trivially satisfied.
    for wp_inp in inp.delivery_packages:
        result = evaluate_cw(wp_inp, tenant_id=tenant_id)
        if result.code != CP_OK:
            cited = dict(result.cited)
            cited["wp_id"] = str(getattr(wp_inp.work_package, "id", None))
            cited["project_id"] = project_id
            return EvaluationResult(result.code, cited)

    # tenant_ok(P) — the project row itself (I-7 read-side re-assertion).
    p_tenant = getattr(inp.project, "tenant_id", None)
    if tenant_id is not None and p_tenant is not None and p_tenant != tenant_id:
        return EvaluationResult(CP_TENANT_MISMATCH, {"project_id": project_id})

    return EvaluationResult(CP_OK, {"project_id": project_id})


# ---------------------------------------------------------------------------
# The CD_* delivery contract (design §6.2 / C-D1..C-D3): the pure gate the
# t_fa30ea5d record lane consumes.  This card fixes the contract; it never
# writes a delivery record.
# ---------------------------------------------------------------------------
def delivery_destination_code(kind: str | None) -> str:
    """The closed V1 destination gate (C-D2): the closed-set membership check.

    A kind inside :data:`DELIVERY_DESTINATION_KINDS` passes the gate; anything
    else (incl. None / unknown) is ``CD_DESTINATION_INVALID``.
    """
    if kind in DELIVERY_DESTINATION_KINDS:
        return CD_OK
    return CD_DESTINATION_INVALID


def delivery_decision_code(
    *,
    scope_code: str,
    destination_kind: str | None,
    sealed_and_approved: bool,
) -> str:
    """The C-D1..C-D3 gate stack for one delivery (fail-closed, first failing).

    1. C-D1 (gate): the scope's current evaluation must be ``CP_OK`` —
       otherwise there is NO input to build a delivery from
       (``CD_NOT_COMPLETED``);
    2. C-D2 (destination): the destination kind must be inside the closed V1
       vocabulary (``CD_DESTINATION_INVALID``);
    3. C-D1 (cited set): every cited artifact must be SEALED and covered by a
       current-valid approving review row (``CD_NO_SEALED_APPROVED``).

    ``sealed_and_approved`` is the (pure) input check the record lane
    computes over its cited set; an unexpected / unreadable input is the
    caller's ``CD_EVAL_ERROR`` catch-all, not a gate of this function.
    """
    if scope_code not in CP_RESULT_CODES:
        return CD_EVAL_ERROR
    if scope_code != CP_OK:
        return CD_NOT_COMPLETED
    if destination_kind not in DELIVERY_DESTINATION_KINDS:
        return CD_DESTINATION_INVALID
    if not sealed_and_approved:
        return CD_NO_SEALED_APPROVED
    return CD_OK


# ---------------------------------------------------------------------------
# C5 decision-row builders (design §3.3 step 3) — DB-free payload shapes.
# ---------------------------------------------------------------------------
def task_decision(subject_id: uuid.UUID, result: EvaluationResult, reverify_of: uuid.UUID | None = None) -> CompletionDecision:
    """The task-scope C5 decision row (design §3.3 / §5.3)."""
    cited = dict(result.cited)
    cited.setdefault("task_id", str(subject_id))
    return CompletionDecision(code=result.code, subject_ref=f"task://{subject_id}", scope="task", cited=cited, reverify_of=reverify_of)


def wp_decision(subject_id: uuid.UUID, result: EvaluationResult, reverify_of: uuid.UUID | None = None) -> CompletionDecision:
    """The WP-scope C5 decision row (the failing task is named, §4)."""
    cited = dict(result.cited)
    cited.setdefault("wp_id", str(subject_id))
    return CompletionDecision(code=result.code, subject_ref=f"wp://{subject_id}", scope="wp", cited=cited, reverify_of=reverify_of)


def project_decision(subject_id: uuid.UUID, result: EvaluationResult, reverify_of: uuid.UUID | None = None) -> CompletionDecision:
    """The project-scope C5 decision row (design §5.3)."""
    cited = dict(result.cited)
    cited.setdefault("project_id", str(subject_id))
    return CompletionDecision(code=result.code, subject_ref=f"project://{subject_id}", scope="project", cited=cited, reverify_of=reverify_of)


def _decision_payload_bytes(decision: CompletionDecision) -> int:
    """The encoded payload size (complete-or-absent bound, invariant 11)."""
    return len(json.dumps(decision.payload(), ensure_ascii=False, default=str).encode("utf-8"))


# ---------------------------------------------------------------------------
# The DB-bound service: bounded reads + ONE append + the single COMPLETED
# write site (design §3.3 / §5 / §9).
# ---------------------------------------------------------------------------
class CompletionService:
    """The DB-bound completion lane over the two parent ledger tables.

    Each evaluation binds the caller's tenant (D6 scope-inject), reads
    through the tenant-scoped DAOs (every read re-asserts the tenant — the
    I-7 shape), runs the pure ``CT`` / ``CW`` / ``CP`` core, appends the ONE
    ``C5`` decision row (``kind='structured'``, chained via
    ``payload.reverify_of``), and — on a FRESH ``CP_OK`` project evaluation —
    publishes ``COMPLETED`` through the single owning write site (C3).  The
    lane writes NOTHING else: no Task.status, no seal, no artifact row, no
    delivery record (those belong to the review / delivery lanes).
    """

    # --- task scope (CT) ----------------------------------------------------
    async def evaluate_task(
        self,
        db,
        *,
        task_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor: EvaluationActor,
        builder_agent_ids: frozenset[uuid.UUID] | None = None,
    ) -> CompletionResult:
        """Evaluate one Task's CT and append its C5 decision row (§3.3)."""
        with tenant_context(tenant_id):
            task = await task_provenance_dao.get_scoped(task_id, db=db)
            if task is None:
                # The subject is not readable inside the caller's tenant — the
                # cross-tenant fact (or a deleted task): fail closed, BEFORE
                # any dependent read, and no C5 row for an unseen subject.
                return CompletionResult(
                    CP_TENANT_MISMATCH, detail=f"task {task_id} not readable in tenant {tenant_id}"
                )
            bundles, direct_deps, load_error = await self._dep_closure_and_inputs(
                db, [task_id], builder_agent_ids=builder_agent_ids
            )
            if load_error is not None:
                code, cited, detail = load_error
            else:
                ctx = TaskEvalContext(tasks=bundles, direct_deps=direct_deps)
                result = evaluate_ct(ctx, task_id, tenant_id=tenant_id)
                code, cited = result.code, result.cited
                detail = f"CT {code} for task {task_id}"
            previous = await self._latest_decision(db, f"task://{task_id}")
            written = await self._append_decision(
                db,
                task_decision(
                    task_id,
                    EvaluationResult(code, cited),
                    reverify_of=previous.id if previous is not None else None,
                ),
                actor=actor,
                tenant_id=tenant_id,
                task_id=task_id,
            )
            return CompletionResult(code, decision=written, detail=detail)

    # --- work-package scope (CW) --------------------------------------------
    async def evaluate_work_package(
        self,
        db,
        *,
        wp_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor: EvaluationActor,
        builder_agent_ids: frozenset[uuid.UUID] | None = None,
    ) -> CompletionResult:
        """Evaluate one WorkPackage's CW and append its C5 decision row (§4)."""
        with tenant_context(tenant_id):
            wp = await work_package_dao.get_scoped(wp_id, db=db)
            if wp is None:
                return CompletionResult(
                    CP_TENANT_MISMATCH, detail=f"work package {wp_id} not readable in tenant {tenant_id}"
                )
            slots = await work_package_task_dao.list_slots_for_package(wp_id, db=db)
            materialized = sorted({s.task_id for s in slots if s.task_id is not None}, key=str)
            bundles, direct_deps, load_error = await self._dep_closure_and_inputs(
                db, materialized, builder_agent_ids=builder_agent_ids
            )
            if load_error is not None:
                code, cited, detail = load_error
            else:
                inp = WorkPackageEvalInput(
                    work_package=wp,
                    slots=slots,
                    wp_task_ids=materialized,
                    task_bundles=bundles,
                    direct_deps=direct_deps,
                )
                result = evaluate_cw(inp, tenant_id=tenant_id)
                code, cited = result.code, result.cited
                detail = f"CW {code} for wp {wp_id}"
            previous = await self._latest_decision(db, f"wp://{wp_id}")
            written = await self._append_decision(
                db,
                wp_decision(
                    wp_id, EvaluationResult(code, cited), reverify_of=previous.id if previous is not None else None
                ),
                actor=actor,
                tenant_id=tenant_id,
            )
            return CompletionResult(code, decision=written, detail=detail)

    # --- project scope (CP) + the single COMPLETED write site ---------------
    async def evaluate_project(
        self,
        db,
        *,
        project_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor: EvaluationActor,
        builder_agent_ids: frozenset[uuid.UUID] | None = None,
    ) -> CompletionResult:
        """Evaluate one Project's CP and append its C5 decision row (§5).

        On a FRESH ``CP_OK`` evaluation where the project is in an
        executable status, publishes ``Project.status='COMPLETED'`` through
        the single owning write site (C3, audit Q10).  A re-call on an
        already-terminal project returns without a second write (idempotent
        by construction, design §9).
        """
        with tenant_context(tenant_id):
            project = await project_dao.get_scoped(project_id, db=db)
            if project is None:
                # The cross-tenant call: fail CP_TENANT_MISMATCH BEFORE any
                # read of the project's tasks / WPs, and no C5 row for an
                # unseen subject.
                return CompletionResult(
                    CP_TENANT_MISMATCH, detail=f"project {project_id} not readable in tenant {tenant_id}"
                )

            in_scope_ids, scope_error = await self._in_scope_task_ids(db, project_id)
            if scope_error is not None:
                code, cited, detail = scope_error
            else:
                delivery_pkgs, delivery_ids, delivery_error = await self._delivery_packages(
                    db, project, builder_agent_ids=builder_agent_ids
                )
                if delivery_error is not None:
                    code, cited, detail = delivery_error
                else:
                    needed = sorted(set(in_scope_ids) | set(delivery_ids), key=str)
                    bundles, direct_deps, load_error = await self._dep_closure_and_inputs(
                        db, needed, builder_agent_ids=builder_agent_ids
                    )
                    if load_error is not None:
                        code, cited, detail = load_error
                    else:
                        # Fill the shared closure map into each delivery WP's
                        # context (frozen dataclasses: rebuild, never mutate).
                        full_pkgs = [
                            WorkPackageEvalInput(
                                work_package=p.work_package,
                                slots=p.slots,
                                wp_task_ids=p.wp_task_ids,
                                task_bundles=bundles,
                                direct_deps=direct_deps,
                            )
                            for p in delivery_pkgs
                        ]
                        inp = ProjectEvalInput(
                            project=project,
                            in_scope_task_ids=in_scope_ids,
                            task_bundles=bundles,
                            direct_deps=direct_deps,
                            delivery_packages=full_pkgs,
                        )
                        result = evaluate_cp(inp, tenant_id=tenant_id)
                        code, cited = result.code, result.cited
                        detail = f"CP {code} for project {project_id}"

            previous = await self._latest_decision(db, f"project://{project_id}")
            written = await self._append_decision(
                db,
                project_decision(
                    project_id, EvaluationResult(code, cited), reverify_of=previous.id if previous is not None else None
                ),
                actor=actor,
                tenant_id=tenant_id,
                project_id=project_id,
            )

            # --- The single owning write site (C3, design §5 / §9). ---
            # Preconditions, all re-checked live on this call:
            #   1. a FRESH CP_OK evaluation (recomputed above, NOT the
            #      decision row read back);
            #   2. the project is in an executable status (the frozen
            #      PROJECT_EXECUTABLE_STATUSES — an already-terminal project
            #      is outside the set, so a re-call never rewrites);
            #   3. the tenant matches (get_scoped above already asserted it).
            project_completed = False
            if code == CP_OK and project.status in PROJECT_EXECUTABLE_STATUSES:
                await project_dao.transition(project, "COMPLETED", db=db)
                project.status = "COMPLETED"
                project_completed = True
            return CompletionResult(
                code,
                decision=written,
                project_completed=project_completed,
                project_status=project.status,
                detail=detail + ("; COMPLETED published" if project_completed else ""),
            )

    # --- reads (tenant-scoped, bounded) --------------------------------------
    async def _dep_closure_and_inputs(
        self,
        db,
        root_ids: list[uuid.UUID],
        *,
        builder_agent_ids: frozenset[uuid.UUID] | None,
    ) -> tuple[
        dict[uuid.UUID, TaskEvalInput],
        dict[uuid.UUID, list[uuid.UUID]],
        tuple[str, dict[str, object], str] | None,
    ]:
        """The bounded transitive dependency closure of ``root_ids`` + each
        member's CT read bundle + the direct-dependency map.

        One frontier walk over the frozen ``task_dependencies`` graph (each
        hop a single batched edge read, no N+1): the readable set is the roots
        plus every edge target that joins it.  A cycle or an over-cap frontier
        is an unreadable graph → the fail-closed catch-all triple (Root §5;
        the pure core would surface it as ``CP_EVAL_ERROR`` too — catching it
        at the read keeps the error *named* and bounded).  Returns
        ``(bundles, direct_deps, load_error)``.
        """
        seen: set[uuid.UUID] = set()
        closure: list[uuid.UUID] = []
        for rid in root_ids:
            if rid not in seen:
                seen.add(rid)
                closure.append(rid)
        pending = list(closure)
        direct_deps: dict[uuid.UUID, list[uuid.UUID]] = {tid: [] for tid in closure}
        hops = 0
        while pending:
            if hops >= _MAX_DEP_HOPS:
                # Hops exhausted with an un-expanded frontier — a cycle the
                # reader cannot settle: fail closed.
                return (
                    {},
                    {},
                    (
                        CP_EVAL_ERROR,
                        {"dep_frontier": [str(b) for b in sorted(pending, key=str)]},
                        "dependency graph unsettled within the bounded hops",
                    ),
                )
            edges = await task_dependency_dao.list_edges_for(pending, db=db, limit=_MAX_EDGES_PER_BATCH)
            if len(edges) >= _MAX_EDGES_PER_BATCH:
                # A single batch hit its cap: the graph is over-bounded and
                # unreadable (complete-or-absent — never a silent partial).
                return (
                    {},
                    {},
                    (
                        CP_EVAL_ERROR,
                        {"edges_at_cap": _MAX_EDGES_PER_BATCH},
                        "dependency frontier exceeds the bounded caps",
                    ),
                )
            next_batch: list[uuid.UUID] = []
            for e in edges:
                direct_deps.setdefault(e.task_id, []).append(e.depends_on_task_id)
                target = e.depends_on_task_id
                if target not in seen:
                    seen.add(target)
                    closure.append(target)
                    next_batch.append(target)
            pending = next_batch
            hops += 1
        if len(closure) > _MAX_SCOPE_TASKS:
            return (
                {},
                {},
                (
                    CP_EVAL_ERROR,
                    {"unreadable_task_count": len(closure)},
                    "task closure exceeds the bounded cap",
                ),
            )

        bundles: dict[uuid.UUID, TaskEvalInput] = {}
        for tid in closure:
            task = await task_provenance_dao.get_scoped(tid, db=db)
            if task is None:
                # A reachable dep row left the tenant (or was never in it):
                # the cross-tenant re-assertion (I-7 shape), not a hole.
                return (
                    {},
                    {},
                    (
                        CP_TENANT_MISMATCH,
                        {"missing_task_ids": [str(tid)]},
                        "a task row is unreadable inside the caller's tenant",
                    ),
                )
            artifacts = await artifact_record_dao.list_by_task(tid, db=db, current_only=False)
            reviews = await evidence_record_dao.list_reviews_for_task(tid, db=db)
            bundles[tid] = TaskEvalInput(
                task=task,
                artifacts=artifacts,
                reviews=reviews,
                builder_agent_ids=builder_agent_ids,
            )
        return bundles, direct_deps, None

    async def _in_scope_task_ids(
        self, db, project_id: uuid.UUID
    ) -> tuple[list[uuid.UUID], tuple[str, dict[str, object], str] | None]:
        """The in-scope ``type='todo'`` task ids of one project (C7, §3.2)."""
        tasks = await task_provenance_dao.list_scoped(
            extra_filters=[Task.project_id == project_id, Task.type == "todo"],
            db=db,
            limit=_MAX_SCOPE_TASKS,
        )
        if len(tasks) >= _MAX_SCOPE_TASKS:
            return [], (
                CP_EVAL_ERROR,
                {"unreadable_task_count": len(tasks)},
                "in-scope task set exceeds the bounded cap",
            )
        return sorted(t.id for t in tasks), None

    async def _delivery_packages(
        self,
        db,
        project,
        *,
        builder_agent_ids: frozenset[uuid.UUID] | None,
    ) -> tuple[
        list[WorkPackageEvalInput],
        list[uuid.UUID],
        tuple[str, dict[str, object], str] | None,
    ]:
        """Every delivery-milestone WP of the LATEST PL_COMPLETED planning run
        (design §5), as skeleton ``WorkPackageEvalInput`` rows (``task_bundles``
        / ``direct_deps`` are filled in by the caller from the shared closure
        map).  Also returns the union of their materialized task ids.  Gate /
        phase milestones are ordering buckets — never completion state; an
        empty delivery set satisfies the term."""
        runs = await planning_run_dao.list_plans_for_project(project.id, db=db)
        completed = [r for r in runs if r.status == "PL_COMPLETED"]
        if not completed:
            return [], [], None
        latest = completed[0]  # newest first (started_at / created_at DESC).
        milestones = await work_package_dao.list_milestones_for_run(latest.id, db=db)
        packages: list[WorkPackageEvalInput] = []
        all_materialized: list[uuid.UUID] = []
        for milestone in milestones:
            if milestone.kind != "delivery":
                continue
            wps = await work_package_dao.list_work_packages_by_milestone(milestone.id, db=db)
            for wp in wps:
                slots = await work_package_task_dao.list_slots_for_package(wp.id, db=db)
                materialized = sorted({s.task_id for s in slots if s.task_id is not None}, key=str)
                all_materialized.extend(materialized)
                packages.append(
                    WorkPackageEvalInput(work_package=wp, slots=slots, wp_task_ids=materialized)
                )
        return packages, all_materialized, None

    # --- C5 write (ONE append per evaluation) --------------------------------
    async def _latest_decision(self, db, subject_ref: str) -> EvidenceRecord | None:
        """The LATEST decision row for one subject (design §9: the only
        correct read is the newest per scope — the chain's tail)."""
        return await evidence_record_dao.latest_decision_row_for_subject(subject_ref, db=db)

    async def _append_decision(
        self,
        db,
        decision: CompletionDecision,
        *,
        actor: EvaluationActor,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
    ) -> EvidenceRecord | None:
        """Append the ONE C5 decision row for this evaluation (design §3.3 / C5).

        The row is ``kind='structured'`` with the closed C5 payload + the
        ``payload.reverify_of`` chain link (the previous decision row's id for
        the same subject, or omitted for the first).  The chain EXCLUDES the
        row from the invariant-5 partial-unique index so repeated evaluations
        of one subject append freely while staying chained (the live-DB
        adjudication of card t_e399386f).  The lane's own provenance rides the
        D5 XOR source fields (agent XOR user).  A payload overrun or a
        rejected append leaves the evaluation code intact (complete-or-absent
        — a half-proof is worse than none).
        """
        if _decision_payload_bytes(decision) > MAX_EVIDENCE_PAYLOAD_BYTES:
            return None
        row = EvidenceRecord(
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            kind="structured",
            # outcome is the CLOSED EVIDENCE_OUTCOMES verdict column; a
            # completion decision carries no ledger verdict, so 'pass' rides
            # here and the real result lives in payload.outcome (the C5
            # contract's named code).  The decision is re-derived on every
            # re-read (§9) — the payload is the cited decision, the column
            # is a closed-set placeholder, never the source of truth.
            outcome="pass",
            subject_ref=decision.subject_ref,
            payload=decision.payload(),
            created_by_agent=actor.agent_id,
            created_by_user=actor.user_id,
        )
        try:
            return await evidence_record_dao.add_evidence(row, tenant_id=tenant_id, db=db)
        except (ArtifactEvidenceClosedError, IntegrityError):
            # A rejected append (bounded-payload overrun at flush, or the
            # invariant-5 index in the unexpected): the decision is computed
            # and returned; the row append is the only write, so a failure
            # here leaves the evaluation code intact (fail-closed).
            return None


completion_service = CompletionService()


__all__ = [
    "C5_DECISION",
    "C5_SCOPES",
    "CD_DESTINATION_INVALID",
    "CD_EVAL_ERROR",
    "CD_NOT_COMPLETED",
    "CD_NO_SEALED_APPROVED",
    "CD_OK",
    "CD_RESULT_CODES",
    "CP_DEPS_NOT_DONE",
    "CP_EVAL_ERROR",
    "CP_INCONCLUSIVE_REVIEW",
    "CP_NOT_SEALED",
    "CP_NO_APPROVING_REVIEW",
    "CP_NO_WORK",
    "CP_OK",
    "CP_OPEN_REQUEST_CHANGES",
    "CP_OPEN_SLOT",
    "CP_RESULT_CODES",
    "CP_REVIEW_NOT_INDEPENDENT",
    "CP_TENANT_MISMATCH",
    "DELIVERY_DESTINATION_KINDS",
    "PROJECT_EXECUTABLE_STATUSES",
    "CompletionDecision",
    "CompletionEvaluationError",
    "CompletionResult",
    "CompletionService",
    "EvaluationActor",
    "EvaluationResult",
    "ProjectEvalInput",
    "TaskEvalContext",
    "TaskEvalInput",
    "WorkPackageEvalInput",
    "completion_service",
    "delivery_decision_code",
    "delivery_destination_code",
    "evaluate_cp",
    "evaluate_ct",
    "evaluate_cw",
    "project_decision",
    "task_decision",
    "wp_decision",
]

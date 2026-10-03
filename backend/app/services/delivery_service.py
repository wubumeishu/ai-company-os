"""The Phase 4 Delivery record lane — CD_* decision + the delivery_records table.

Implements the V1 delivery contract of design card ``t_4185daed``
(docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md §6,
C-D1..C-D4 + the closed ``CD_*`` set §6.2).  This card owns the delivery
*record* (the design-reserved ``delivery_records`` table + its DAO + this
service lane) — the ONE bounded new schema piece of Phase 4 (card
``t_af586c02``).  The CD_* contract, the CT / CW / CP evaluators, and the
two f072 ledger tables are CONSUMED from the completion lane
(``completion_service``) and the parent domain (``artifact_evidence``), never
re-invented.

Guarantees honored (Root §6 / design §6):

- **C-D1 (gate, hard requirement 1).**  A delivery record may be opened ONLY
  against a scope (WP or Project) whose *current* evaluation is ``CP_OK``.
  The gate runs the completion lane's own evaluator on the live call (the C5
  decision row it appends is the cited provenance row); any non-``CP_OK``
  outcome is ``CD_NOT_COMPLETED`` — there is structurally NO input to build a
  delivery from, and **no record is written**.  "Agent says done ->
  Delivery" is impossible at every hop: the gate's inputs are the ledger
  rows (SEALED artifacts + a current-valid ``outcome='pass'`` review row by a
  disjoint reviewer + deps + tenant), never ``Task.status`` / ``final_answer``
  / the gate verdict (C-D4 / R-A extended).
- **Cited set (hard requirement 2).**  The delivered ``artifact_ids`` may be
  ONLY ``SEALED`` artifacts covered by a current-valid approving review row
  (the completion lane's ``current_valid_review`` read, consumed — not
  re-implemented).  An artifact that is DRAFT / superseded / not covered by
  the current-valid pass row over its task is ``CD_NO_SEALED_APPROVED`` and
  nothing is written (a rework that supersedes between decision and write
  fails the re-check at write time, design §6.3 C-D3).
- **C-D2 (destination).**  ``destination_kind`` is the closed V1 vocabulary
  ``channel`` / ``published_page`` / ``project_record`` — the closed set the
  completion lane owns (``DELIVERY_DESTINATION_KINDS``), validated here
  against that owner (C-D2 "adding a kind is a reviewable closed-set
  extension, not a code path").  No external publish platform in V1; the
  record only *references* the destination fact (a channel-delivery fact / a
  ``PublishedPage`` short id / the ledger itself for ``project_record``).
- **C-D3 (record).**  One append-only record carrying the delivery provenance
  (the citing C5 decision row + cited artifact ids + the citing current-valid
  review row ids + the destination + the state + the timestamps + the D5 XOR
  source) — the row is its own audit record (C-D4 of the §6.3 field list).
  ``project_record`` is terminal at the decision (the record IS the
  destination); ``channel`` / ``published_page`` stay PENDING until the
  owning transport drives ``DeliveryRecordDAO.transition_state`` (the ONE
  post-append write path; recall / second transitions are out of V1).
- **C-D4 (independence).**  a builder is not a disjoint reviewer, so a
  builder-captured approving row cannot exist (invariant 13); the lane
  re-asserts the reviewer disjointness at the read side (fail-closed mirror,
  same boundary as the completion lane's CP_REVIEW_NOT_INDEPENDENT) — a
  builder delivering a scope it built is structurally impossible.

Structure (Phase 3 discipline: pure core + bounded transactional writes):

- The **pure, DB-free gate core** (module-level functions) implements the
  C-D1..C-D3 decision stack over the lane's own ``delivery_decision_code``
  contract + the current-valid-approving read, so every ``CD_*`` path is
  unit-testable without Postgres (non-CP_OK scope, unknown destination,
  uncited / superseded artifact, empty cited set, ...).
- The **DB-bound service** (:class:`DeliveryService`) binds the caller's
  tenant (D6 scope-inject), reads the bounded scope ledger state through the
  tenant-scoped DAOs (every read re-asserts the tenant — the I-7 shape), runs
  the completion lane's CP gate (which appends the citing C5 row), re-checks
  the cited set against the current-valid SEALED+approved ledger state, and —
  on ``CD_OK`` only — appends the ONE delivery record.  No other write.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.dao.artifact_evidence_dao import artifact_record_dao, evidence_record_dao
from app.dao.base import tenant_context
from app.dao.delivery_record_dao import delivery_record_dao
from app.dao.planning_dao import planning_run_dao, work_package_dao, work_package_task_dao
from app.dao.task_dao import task_provenance_dao
from app.models.delivery_record import DELIVERY_STATES, DeliveryRecord
from app.models.task import Task
from app.services.completion_service import (
    CD_EVAL_ERROR,
    CD_NO_SEALED_APPROVED,
    CD_NOT_COMPLETED,
    CD_OK,
    CP_OK,
    EvaluationActor,
    TaskEvalInput,
    completion_service,
    delivery_decision_code,
)
from app.services.review_rework_service import current_valid_review

# ---------------------------------------------------------------------------
# The closed delivery scope vocabulary (C-D1: a delivery is opened against a
# WP or Project scope — never a bare task; the C5 subject namespaces,
# delivery-scoped subset).  The record's provenance + idempotent read ride on
# it.
# ---------------------------------------------------------------------------
DELIVERY_SCOPES = ("wp", "project")

#: Bounded delivery-lane read caps (complete-or-absent: an over-cap scope is
#: unreadable -> the catch-all, never a silent partial).  Aligned with the
#: completion lane's reads (``list_by_task`` / ``list_reviews_for_task``
#: default limit 200) so the cited-set check sees EXACTLY the ledger state
#: the CP gate evaluated — a wider read would re-derive a second authority.
_MAX_DELIVERY_SCOPE_TASKS = 1000
_MAX_DELIVERY_ARTIFACTS_PER_TASK = 200
_MAX_DELIVERY_REVIEWS_PER_TASK = 200

#: The terminal state the lane itself owns for the ``project_record``
#: destination (C-D3: "terminal states set by ... the lane on
#: project_record" — the record IS the destination, so the decision is the
#: delivery).  ``channel`` / ``published_page`` stay PENDING until the
#: owning transport drives the transition (no external publish platform in
#: V1, C-D2 / Root §6).
_PROJECT_RECORD_TERMINAL_STATE = "DELIVERED"


# ---------------------------------------------------------------------------
# The pure, DB-free C-D1..C-D3 gate (consumes the lane's CD_* contract).
# ---------------------------------------------------------------------------
def resolve_cited_artifact_ids(
    requested: Sequence[str] | None,
    approved_artifact_ids: Mapping[str, object],
) -> tuple[list[str], str | None]:
    """Resolve the delivered artifact set for the record + a fail-closed
    reason (design §6.3 C-D3, the "bounded list of SEALED+approved ids").

    - ``requested=None`` -> deliver the FULL current-valid approved set of
      the scope (the ``project_record`` natural case: the verified-and-
      approved artifact set, no external handover).
    - ``requested`` given -> the exact ids, each re-checked against the
      current-valid ledger state (an id that is not SEALED+approved is the
      fail-closed ``CD_NO_SEALED_APPROVED``; the *cited set* check, not a
      record write).
    - an EMPTY explicit list is a malformed input -> the catch-all
      ``CD_EVAL_ERROR`` (a delivery must name what it delivers; complete-or-
      absent, never a silent empty delivery).
    - duplicated ids are the same malformed-input class -> the catch-all
      (a silent dedup would hide the caller's error, fail-open).

    Returns ``(cited_ids, failure_code)`` — ``failure_code`` is ``None`` on
    success, a CD_* code otherwise (the caller writes NOTHING on failure).
    """
    if requested is None:
        return sorted(approved_artifact_ids, key=str), None
    if len(requested) == 0 or len(set(requested)) != len(requested):
        return sorted(set(requested), key=str), CD_EVAL_ERROR
    for art_id in requested:
        if art_id not in approved_artifact_ids:
            # C-D1 cited-set term: the artifact is DRAFT, superseded, or its
            # approving review row is not current-valid over the cited set
            # (superseded => stale, review card §3.4) — fail closed, nothing
            # delivered.
            return sorted(requested, key=str), CD_NO_SEALED_APPROVED
    return sorted(requested, key=str), None


def sealed_and_approved_check(
    requested: Sequence[str] | None,
    approved_artifact_ids: Mapping[str, object],
) -> bool:
    """The C-D1 cited-set term for the lane's gate stack (the pure input
    check ``delivery_decision_code`` consumes as ``sealed_and_approved``).

    True iff the resolved cited set is non-empty and every cited id is in
    the current-valid SEALED+approved set.  An empty resolved set (no
    approved artifact at all, or an explicit empty list) is False — a
    delivery with no approved input is the ``CD_NO_SEALED_APPROVED`` term,
    never the OK code.
    """
    cited, failure = resolve_cited_artifact_ids(requested, approved_artifact_ids)
    if failure is not None:
        return False
    return len(cited) > 0


def delivery_gate(
    *,
    scope_code: str,
    destination_kind: str | None,
    requested_artifact_ids: Sequence[str] | None,
    approved_artifact_ids: Mapping[str, object],
) -> tuple[str, list[str] | None]:
    """The C-D1..C-D3 decision stack for one delivery (fail-closed, first
    failing term wins — CONSUMES the completion lane's
    ``delivery_decision_code`` contract, the CD_* closed set §6.2).

    A malformed explicit cited set (empty or duplicated ids) is the catch-
    all ``CD_EVAL_ERROR`` BEFORE the lane's stack: a delivery must name a
    well-formed bounded set; silent dedup / a silent empty delivery are
    both fail-open, and neither is allowed (complete-or-absent, Root §5).

    Returns ``(code, cited_ids)``: the CD code + the resolved cited ids
    (``None`` when the gate failed — the caller writes no record).
    """
    if requested_artifact_ids is not None and (
        len(requested_artifact_ids) == 0 or len(set(requested_artifact_ids)) != len(requested_artifact_ids)
    ):
        return CD_EVAL_ERROR, None
    sealed_ok = sealed_and_approved_check(requested_artifact_ids, approved_artifact_ids)
    code = delivery_decision_code(
        scope_code=scope_code,
        destination_kind=destination_kind,
        sealed_and_approved=sealed_ok,
    )
    if code != CD_OK:
        return code, None
    cited, _ = resolve_cited_artifact_ids(requested_artifact_ids, approved_artifact_ids)
    return CD_OK, cited


# ---------------------------------------------------------------------------
# The current-valid SEALED+approved set read (C-D1 / C-D2 cited-set input).
# ---------------------------------------------------------------------------
def approved_artifact_ids_for_bundles(
    task_bundles: Mapping[uuid.UUID, TaskEvalInput],
    *,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, uuid.UUID]:
    """The current-valid SEALED + approving-coverage artifact ids of a scope's
    in-scope tasks, as a ``{str(artifact_id): task_id}`` map (the cited-set
    authority for the C-D1 term).

    An artifact qualifies ONLY when (a) it is in the task's current
    (non-superseded) set, (b) it is ``SEALED``, (c) it is in the same tenant
    (D6 re-assertion, I-7 shape), and (d) the task's current-valid review
    (the completion lane's ``current_valid_review`` read, consumed — the
    latest non-superseded ``kind='review'`` row over the current set) is
    ``outcome='pass'`` with a disjoint reviewer (C-D4 / invariant 13 read-
    side mirror: a reviewer inside the builder set, or an unknown builder
    set + an agent reviewer, fails closed — the approving coverage does not
    hold).  This never reads ``Task.status`` / ``final_answer`` / the gate
    verdict (C-D4 / R-A).
    """
    approved: dict[str, uuid.UUID] = {}
    for task_id, bundle in task_bundles.items():
        current = bundle.current_artifacts()
        if not current:
            continue
        review = current_valid_review(bundle.artifacts, bundle.reviews)
        if review is None:
            continue
        outcome = getattr(review, "outcome", None)
        if outcome != "pass":
            # An open REQUEST_CHANGES / unconsumed inconclusive / unexpected
            # verdict on the current set: no approving coverage — fail
            # closed (the same read-side rejections the completion lane's CT
            # applies, never re-implemented as a second authority).
            continue
        reviewer = getattr(review, "created_by_agent", None)
        if reviewer is not None:
            builders = bundle.builder_agent_ids
            if builders is None:
                continue  # unknown builder set + an agent reviewer: unevaluable
            if reviewer in builders:
                continue  # C-D4 / invariant 13 mirror: not disjoint
        for artifact in current:
            art_id = getattr(artifact, "id", None)
            if art_id is None:
                continue  # an artifact row without an identity: unreadable ledger shape
            art_tenant = getattr(artifact, "tenant_id", None)
            if tenant_id is not None and art_tenant is not None and art_tenant != tenant_id:
                continue
            if getattr(artifact, "seal_status", None) != "SEALED":
                continue
            approved[str(art_id)] = task_id
    return approved


# ---------------------------------------------------------------------------
# The DB-bound service.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one delivery call (C-D1..C-D4).

    ``code`` is exactly one member of the closed CD_* set (fail-closed);
    ``record`` is the appended ``delivery_records`` row (``None`` on every
    non-``CD_OK`` — nothing is written when the gate fails); ``cited_ids``
    is the resolved delivered artifact set (``None`` on failure);
    ``already_delivered`` marks the idempotent re-delivery (an existing
    terminal record of the same cited set was returned, no new row);
    ``decision_row`` is the citing C5 decision row (the gate evaluation's
    audit trace, C-D3 provenance).
    """

    code: str
    record: DeliveryRecord | None = None
    decision_row: object | None = None
    cited_ids: list[str] | None = None
    already_delivered: bool = False
    detail: str = ""


def _parse_scope_ref(scope_ref: str) -> tuple[str, uuid.UUID] | None:
    """The closed delivery scope locator: ``wp://{uuid}`` | ``project://{uuid}``.

    Returns ``(kind, id)`` or ``None`` when the shape is outside the closed
    vocabulary (fail-closed catch-all, Root §5).
    """
    for kind in DELIVERY_SCOPES:
        prefix = f"{kind}://"
        if scope_ref.startswith(prefix):
            raw = scope_ref[len(prefix):]
            try:
                return kind, uuid.UUID(raw)
            except ValueError:
                return None
    return None


class DeliveryService:
    """The DB-bound delivery record lane (design §6).

    Each call binds the caller's tenant (D6 scope-inject), reads the scope
    through the tenant-scoped DAOs (every read re-asserts the tenant — the
    I-7 shape), runs the completion lane's CP gate (the citing C5 row is
    appended by that evaluator), re-checks the cited set against the
    current-valid SEALED+approved ledger state, and — on ``CD_OK`` only —
    appends the ONE delivery record (terminal at the decision for
    ``project_record``; PENDING + owning-transport transition for
    ``channel`` / ``published_page``).  The lane writes NOTHING else: no
    Task.status, no seal, no artifact row, no second C5 row.
    """

    async def deliver(
        self,
        db,
        *,
        scope_ref: str,
        tenant_id: uuid.UUID,
        actor: EvaluationActor,
        destination_kind: str | None,
        destination_ref: str | None = None,
        artifact_ids: Sequence[str] | None = None,
        builder_agent_ids: frozenset[uuid.UUID] | None = None,
    ) -> DeliveryResult:
        """Open one delivery against a WP / Project scope (C-D1..C-D4).

        ``artifact_ids=None`` delivers the full current-valid approved set;
        an explicit list re-checks each id against that set at write time.
        On any non-``CD_OK`` code NO record is written (fail-closed).
        """
        with tenant_context(tenant_id):
            parsed = _parse_scope_ref(scope_ref)
            if parsed is None:
                # Outside the closed scope vocabulary: unreadable input, the
                # catch-all — never the OK code, no read, no write.
                return DeliveryResult(CD_EVAL_ERROR, detail=f"scope_ref {scope_ref!r} outside {DELIVERY_SCOPES}")
            kind, scope_id = parsed

            # --- C-D1 gate input: the scope's CURRENT CP evaluation (the
            # completion lane's evaluator + its C5 decision row, consumed).
            # Cross-tenant / unseen subject fails the lane's read BEFORE any
            # C5 row (CP_TENANT_MISMATCH) -> CD_NOT_COMPLETED below, no write.
            if kind == "wp":
                gate = await completion_service.evaluate_work_package(
                    db, wp_id=scope_id, tenant_id=tenant_id, actor=actor, builder_agent_ids=builder_agent_ids
                )
            else:
                gate = await completion_service.evaluate_project(
                    db, project_id=scope_id, tenant_id=tenant_id, actor=actor, builder_agent_ids=builder_agent_ids
                )
            if gate.code != CP_OK:
                # C-D1 (gate): the scope is not CP_OK — no input to build a
                # delivery from (CD_NOT_COMPLETED).  The gate's C5 row (the
                # citing decision row) is the audit trace of the rejection.
                return DeliveryResult(
                    CD_NOT_COMPLETED,
                    decision_row=gate.decision,
                    detail=f"scope {scope_ref} evaluates {gate.code}, not CP_OK; no delivery record written",
                )
            if gate.decision is None:
                # The gate evaluated CP_OK but its C5 decision row could not
                # be written (complete-or-absent, the lane's payload-overrun
                # path): the provenance chain the record must cite is broken
                # -> fail closed, no record.
                return DeliveryResult(
                    CD_EVAL_ERROR,
                    decision_row=None,
                    detail=f"the citing C5 decision row was not written for {scope_ref}; no delivery record written",
                )

            # --- Cited-set re-check against the CURRENT-VALID ledger state
            # (C-D1 / C-D3: each id SEALED + covered by the current-valid
            # approving review row).  Bounded reads, tenant-scoped.
            bundles, load_error = await self._scope_task_bundles(
                db, kind, scope_id, builder_agent_ids=builder_agent_ids
            )
            if load_error is not None:
                code, detail = load_error
                return DeliveryResult(
                    code,
                    decision_row=gate.decision,
                    cited_ids=None,
                    detail=f"read of the cited set failed: {detail}",
                )
            approved = approved_artifact_ids_for_bundles(bundles, tenant_id=tenant_id)
            gate_code, cited = delivery_gate(
                scope_code=CP_OK,
                destination_kind=destination_kind,
                requested_artifact_ids=artifact_ids,
                approved_artifact_ids=approved,
            )
            if gate_code != CD_OK or cited is None:
                # CD_OK never carries None (the gate resolves the cited set on
                # the OK path); the None check is the type-narrowing guard.
                return DeliveryResult(
                    gate_code,
                    decision_row=gate.decision,
                    detail=f"cited-set / destination gate failed: {gate_code}; no delivery record written",
                )

            # --- Idempotent re-delivery (C-D3, design §8 recall deferral):
            # a terminal record of the same scope + same cited set is the
            # existing delivery — return it, append nothing.
            existing = await delivery_record_dao.list_by_scope(scope_ref, db=db)
            cited_set = set(cited)
            for record in existing:
                if record.state in ("DELIVERED", "FAILED") and set(record.artifact_ids or []) == cited_set:
                    return DeliveryResult(
                        CD_OK,
                        record=record,
                        decision_row=gate.decision,
                        cited_ids=cited,
                        already_delivered=True,
                        detail=f"re-delivery of {scope_ref} returned the existing terminal record {record.id}",
                    )

            cited_review_row_ids = sorted(self._citing_review_ids(bundles, cited, approved))
            # --- The ONE delivery record write (CD_OK only).
            terminal_at_decision = destination_kind == "project_record"
            owner_project_id = await self._owner_project_id(db, kind, scope_id)
            record = DeliveryRecord(
                tenant_id=tenant_id,
                project_id=owner_project_id,
                scope_ref=scope_ref,
                artifact_ids=cited,
                cited_review_row_ids=cited_review_row_ids,
                cp_decision_row_id=gate.decision.id if gate.decision is not None else None,
                destination_kind=destination_kind,
                destination_ref=destination_ref,
                state=_PROJECT_RECORD_TERMINAL_STATE if terminal_at_decision else "PENDING",
                executed_at=datetime.now(UTC) if terminal_at_decision else None,
                decided_by_agent=actor.agent_id,
                decided_by_user=actor.user_id,
            )
            written = await delivery_record_dao.add_record(record, tenant_id=tenant_id, db=db)
            return DeliveryResult(
                CD_OK,
                record=written,
                decision_row=gate.decision,
                cited_ids=cited,
                already_delivered=False,
                detail=(
                    f"delivery record {written.id} {'DELIVERED at decision' if terminal_at_decision else 'PENDING for the owning transport'} for {scope_ref}"
                ),
            )

    # --- bounded scope reads (the cited-set input, tenant re-asserted) ------
    async def _scope_task_bundles(
        self,
        db,
        kind: str,
        scope_id: uuid.UUID,
        *,
        builder_agent_ids: frozenset[uuid.UUID] | None,
    ) -> tuple[dict[uuid.UUID, TaskEvalInput], tuple[str, str] | None]:
        """The in-scope tasks' current-artifact + review bundles for the cited-
        set check (bounded, tenant-scoped; an over-cap scope is the fail-
        closed catch-all — complete-or-absent, never a silent partial)."""
        if kind == "wp":
            slots = await work_package_task_dao.list_slots_for_package(scope_id, db=db, limit=200)
            task_ids = sorted({s.task_id for s in slots if s.task_id is not None}, key=str)
        else:
            tasks = await task_provenance_dao.list_scoped(
                extra_filters=[Task.project_id == scope_id, Task.type == "todo"],
                db=db,
                limit=_MAX_DELIVERY_SCOPE_TASKS,
            )
            if len(tasks) >= _MAX_DELIVERY_SCOPE_TASKS:
                return {}, (CD_EVAL_ERROR, "in-scope task set exceeds the bounded cap")
            task_ids = sorted(t.id for t in tasks)

        bundles: dict[uuid.UUID, TaskEvalInput] = {}
        for tid in task_ids:
            artifacts = await artifact_record_dao.list_by_task(tid, db=db, current_only=False, limit=_MAX_DELIVERY_ARTIFACTS_PER_TASK)
            if len(artifacts) >= _MAX_DELIVERY_ARTIFACTS_PER_TASK:
                return {}, (CD_EVAL_ERROR, f"task {tid} artifact set exceeds the bounded cap")
            reviews = await evidence_record_dao.list_reviews_for_task(tid, db=db, limit=_MAX_DELIVERY_REVIEWS_PER_TASK)
            task = await task_provenance_dao.get_scoped(tid, db=db)
            if task is None:
                # A reachable task row left the tenant (or was never in it):
                # the cross-tenant re-assertion (I-7 shape) — fail closed, not
                # a hole (the completion lane's CP_TENANT_MISMATCH read-side).
                return {}, (CD_EVAL_ERROR, f"task {tid} is unreadable inside the caller's tenant")
            bundles[tid] = TaskEvalInput(
                task=task,
                artifacts=artifacts,
                reviews=reviews,
                builder_agent_ids=builder_agent_ids,
            )
        return bundles, None

    async def _owner_project_id(
        self, db, kind: str, scope_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The owning project of the delivery scope (matrix P1: a convenience
        link, never a second authority — for ``wp://`` the link resolves
        through the frozen planning run, for ``project://`` it is the scope
        id itself)."""
        if kind == "project":
            return scope_id
        wp = await work_package_dao.get_scoped(scope_id, db=db)
        if wp is None:
            return None
        run = await planning_run_dao.get_scoped(wp.planning_run_id, db=db)
        return run.project_id if run is not None else None

    @staticmethod
    def _citing_review_ids(
        bundles: Mapping[uuid.UUID, TaskEvalInput],
        cited: Sequence[str],
        approved: Mapping[str, uuid.UUID],
    ) -> set[str]:
        """The citing current-valid-approving review row ids (C-D3
        provenance: the delivery -> CP evaluation -> approving review row ->
        sealed artifact set chain, ledger-queryable).  Only the pass rows
        that COVER a cited artifact's task contribute (a review of a task
        whose artifacts are not in the cited set is not part of this
        delivery's provenance); bounded by the cited task set."""
        owner_tasks = {approved[art_id] for art_id in cited if art_id in approved}
        rows: set[str] = set()
        for task_id in owner_tasks:
            bundle = bundles.get(task_id)
            if bundle is None:
                continue
            review = current_valid_review(bundle.artifacts, bundle.reviews)
            if review is None or getattr(review, "outcome", None) != "pass":
                continue
            builders = bundle.builder_agent_ids
            reviewer = getattr(review, "created_by_agent", None)
            if reviewer is not None and builders is not None and reviewer in builders:
                continue  # C-D4 / invariant 13 read-side mirror
            rid = getattr(review, "id", None)
            if rid is not None:
                rows.add(str(rid))
        return rows


delivery_service = DeliveryService()


__all__ = [
    "DELIVERY_SCOPES",
    "DELIVERY_STATES",
    "DeliveryResult",
    "DeliveryService",
    "approved_artifact_ids_for_bundles",
    "delivery_gate",
    "delivery_service",
    "resolve_cited_artifact_ids",
    "sealed_and_approved_check",
]

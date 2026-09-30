"""Independent Review & Rework service — the Phase 4 guard layer.

Implements the V1 design of card ``t_dbb0c0dd``
(docs/architecture/PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md) on top of the
Artifact / Evidence storage owned by card ``t_f19aae89``
(docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md) and persisted by
``app/dao/artifact_evidence_dao.py``.

This module is **service-side only** (design t_dbb0c0dd §5 / R0): it adds the
``RV_*`` result-code set, the guarded transitions, and the "current valid
review" / "rework provenance" reads over the two *parent* ledger tables.  It
adds **no** review/rework table or column (decision R0/D4), and it does **not**
re-implement the assignment-time independence (the frozen
``PL_REVIEWER_NOT_INDEPENDENT`` / ``ReviewBinding`` in
``assignment_service.py``) or fold in the out-of-scope
``TaskCompletionGate._fail_open`` fix.  Sealing, independence, and
rework-linking all ride on ``artifact_records`` / ``evidence_records`` + the
existing REV-1/2 fact + the verdict-time ``EV_REVIEW_NOT_INDEPENDENT`` guard.

Structure (Phase 3 discipline: pure core + bounded transactional writes):

- The **pure derivation core** (module-level functions, DB-free, unit-testable)
  implements the four guarantees directly:
  - G1 no self-review   — :func:`plan_review` rejects a reviewer in the builder set
  - G2 non-destructive  — :func:`plan_rework` links new rows via ``superseded_by``
                           and never touches the old rows
  - G3 rework provenance — :func:`rework_provenance` walks the stored chain
  - current valid review — :func:`current_valid_review` over the non-superseded set
- The **DB-bound service** (:class:`ReviewReworkService`) runs a pure plan, then
  writes through the append-only DAOs in one transaction, mapping failures to
  the closed ``RV_*`` codes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.dao.artifact_evidence_dao import (
    ArtifactEvidenceClosedError,
    artifact_record_dao,
    evidence_record_dao,
)
from app.models.artifact_evidence import (
    EVIDENCE_OUTCOMES,
    MAX_EVIDENCE_PAYLOAD_BYTES,
    ArtifactRecord,
    EvidenceRecord,
)

# ---------------------------------------------------------------------------
# Closed result-code set for the review/rework lane (design t_dbb0c0dd §5).
# Service-side only; unknown values fail closed (the PLANNING_RUN_STATUSES
# re-validation pattern).  Independence reuses the two EXISTING codes rather
# than a new mechanism: PL_REVIEWER_NOT_INDEPENDENT (assignment-time, frozen)
# or EV_REVIEW_NOT_INDEPENDENT (verdict-time, the DAO guard this lane reuses).
# ---------------------------------------------------------------------------

RV_OK = "RV_OK"
#: EXECUTING->IN_REVIEW with an empty current artifact set (design §5).
RV_NO_CURRENT_ARTIFACTS = "RV_NO_CURRENT_ARTIFACTS"
#: no assigned reviewer row / actor to act (design §5).
RV_NO_REVIEWER = "RV_NO_REVIEWER"
#: lane-side mirror of EV_REVIEW_NOT_INDEPENDENT (a reviewer is a builder).
RV_REVIEW_NOT_INDEPENDENT = "RV_REVIEW_NOT_INDEPENDENT"
#: attempting a 2nd seal on an already-SEALED set (parent AE_ALREADY_SEALED).
RV_ALREADY_SEALED = "RV_ALREADY_SEALED"
#: verdict / re-review cited a superseded (historical) artifact set.
RV_SUPERSEDED_SET = "RV_SUPERSEDED_SET"
#: review parked in BLOCKED (inconclusive); must re-verify, never completes.
RV_INCONCLUSIVE = "RV_INCONCLUSIVE"
#: critiques / required_changes exceed the 32 KiB bound.
RV_PAYLOAD_OVERRUN = "RV_PAYLOAD_OVERRUN"
#: a generic fail-closed catch-all (unknown closed value, unexpected input).
RV_INVALID_INPUT = "RV_INVALID_INPUT"

REVIEW_RESULT_CODES = frozenset(
    {
        RV_OK,
        RV_NO_CURRENT_ARTIFACTS,
        RV_NO_REVIEWER,
        RV_REVIEW_NOT_INDEPENDENT,
        RV_ALREADY_SEALED,
        RV_SUPERSEDED_SET,
        RV_INCONCLUSIVE,
        RV_PAYLOAD_OVERRUN,
        RV_INVALID_INPUT,
    }
)

#: The three REVIEW outcomes on the evidence row (a CLOSED set, EVIDENCE_OUTCOMES):
#: pass -> APPROVE (+ seal), fail -> REQUEST_CHANGES, inconclusive -> BLOCKED.
#: Unknown values fail closed at the lane.
REVIEW_DECISIONS = frozenset(EVIDENCE_OUTCOMES)


class ReviewReworkError(Exception):
    """A fail-closed gate of the review/rework lane.

    Carries a ``code`` (a member of :data:`REVIEW_RESULT_CODES`) so the owning
    transport maps it without re-parsing the message (mirrors
    ``PlanningError`` / ``ClosedCodeError``).
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}" + (f": {message}" if message else ""))


# ---------------------------------------------------------------------------
# Pure, DB-free derivation core — the four guarantees as plain-data functions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewPlan:
    """The guarded plan for one review transition (design §3.3 row 2/3/4).

    ``evidence`` is the single ``kind='review'`` row to write; ``seal_ids``
    are the current artifact rows to seal (APPROVE only, R3); ``code`` is the
    closed result — :data:`RV_OK` when the transition may apply, a
    fail-closed :data:`RV_*` otherwise.
    """

    code: str
    outcome: str  # pass / fail / inconclusive (EVIDENCE_OUTCOMES)
    evidence: EvidenceRecord | None = None
    seal_ids: tuple[uuid.UUID, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReworkPlan:
    """The guarded plan for a rework (REQUEST_CHANGES -> REWORKING, R5/R6/R7).

    ``new_artifacts`` are the NEW current rows (each ``superseded_by``-free);
    ``supersede_links`` pairs (old_artifact_id, new_artifact_id) the builder
    must record so the old rows become historical.  The rework step ends HERE
    (design §3.3 REWORKING->RE_REVIEW: the builder writes "nothing new" — the
    new set is current); it produces **no** ``kind='review'`` verdict row.
    Old rows are NEVER mutated here — only *linked* (G2).
    """

    code: str
    new_artifacts: tuple[ArtifactRecord, ...] = ()
    supersede_links: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    new_evidence: tuple[EvidenceRecord, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReworkProvenance:
    """The stored, ordered Rework -> REQUEST_CHANGES chain (design §3.5, G3).

    Every link is a stored edge: ``REVIEW-2.rework_of -> REVIEW-1``,
    ``ART-2 -> (supersedes) ART-1``, ``EV-TEST-2.execution -> rework exec``.
    A reviewer or auditor can walk the whole chain from the two ledgers alone.
    """

    fail_review: EvidenceRecord  # the REQUEST_CHANGES row (outcome='fail')
    required_changes: list[Any]
    superseded_pairs: list[tuple[uuid.UUID, uuid.UUID]]  # (old, new)
    current_artifact_ids: tuple[uuid.UUID, ...]
    re_review: EvidenceRecord | None = None  # the outcome-? row with rework_of
    new_evidence: tuple[EvidenceRecord, ...] = ()


def _payload_size(payload: dict | None) -> int:
    if payload is None:
        return 0
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def current_valid_review(
    artifacts: Sequence[ArtifactRecord],
    reviews: Sequence[EvidenceRecord],
) -> EvidenceRecord | None:
    """The "current valid review" for one Task's artifact set (design §3.4).

    Pure function of the ledgers: the current (non-superseded) artifact set
    ``A_c(T)`` is recomputed from ``superseded_by IS NULL``; the current valid
    review is the latest (by ``created_at`` then ``id``) ``kind='review'`` row
    whose cited ``artifact_id`` is in that set.  An old APPROVE on a
    superseded set drops out automatically — it stays a historical row, never
    clobbering later changes (guarantee G2).  ``None`` when no review has
    been written over the current set yet.
    """
    current_ids = {a.id for a in artifacts if a.superseded_by is None}
    if not current_ids:
        return None
    candidates = [
        r
        for r in reviews
        if r.kind == "review" and r.artifact_id is not None and r.artifact_id in current_ids
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.created_at, r.id))


def plan_review(
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    outcome: str,
    reviewer_agent_id: uuid.UUID | None,
    reviewer_user_id: uuid.UUID | None,
    builder_agent_ids: frozenset[uuid.UUID],
    current_artifacts: Sequence[ArtifactRecord],
    cited_artifact_ids: Sequence[uuid.UUID],
    verdict: str = "",
    required_changes: Sequence[Any] | None = None,
    critiques: Sequence[Any] | None = None,
    rework_of: uuid.UUID | None = None,
) -> ReviewPlan:
    """Guard one review transition (design §3.3) — pure, DB-free.

    Returns a :class:`ReviewPlan` whose ``code`` is :data:`RV_OK` when the
    transition may apply, else a fail-closed :data:`RV_*`.  Enforces the lane's
    invariants on plain data:

    - I-2: a review cites >=1 current artifact (RV_NO_CURRENT_ARTIFACTS);
    - G1: the reviewer is disjoint from every builder (RV_REVIEW_NOT_INDEPENDENT);
    - I-8: critiques + required_changes are bounded (RV_PAYLOAD_OVERRUN);
    - R3: APPROVE (outcome='pass') seals the current set; a 2nd seal is a
      fail (RV_ALREADY_SEALED); REQUEST_CHANGES/BLOCKED leave it DRAFT.
    """
    if outcome not in REVIEW_DECISIONS:
        return ReviewPlan(RV_INVALID_INPUT, outcome, detail=f"outcome {outcome!r} outside {REVIEW_DECISIONS}")

    # G1 — no self-review (design R2, invariant 13): a reviewer in the builder
    # set is rejected.  When the reviewer is a user there is no builder
    # overlap to check (a human is never a builder agent).
    if reviewer_agent_id is not None and reviewer_agent_id in builder_agent_ids:
        return ReviewPlan(
            RV_REVIEW_NOT_INDEPENDENT,
            outcome,
            detail=f"reviewer {reviewer_agent_id} is a builder on the same work package",
        )

    current = [a for a in current_artifacts if a.superseded_by is None]
    # I-2 — a review cites >=1 current artifact (never the builder's
    # self-report; the row cites the ledgers, design §4 / G4).
    cited = set(cited_artifact_ids)
    if not (cited & {a.id for a in current}):
        return ReviewPlan(
            RV_NO_CURRENT_ARTIFACTS,
            outcome,
            detail="a review must cite at least one current (non-superseded) artifact",
        )

    # I-8 — bounded critiques / required_changes (complete-or-absent).
    payload: dict[str, Any] = {"verdict": verdict}
    if required_changes:
        payload["required_changes"] = list(required_changes)
    if critiques:
        payload["critiques"] = list(critiques)
    if rework_of is not None:
        payload["rework_of"] = str(rework_of)
    if cited:
        payload["cited_artifact_ids"] = [str(c) for c in cited]
    if _payload_size(payload) > MAX_EVIDENCE_PAYLOAD_BYTES:
        return ReviewPlan(
            RV_PAYLOAD_OVERRUN,
            outcome,
            detail=f"payload exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes",
        )

    primary = next(iter(sorted(cited, key=str)))
    evidence = EvidenceRecord(
        tenant_id=tenant_id,
        task_id=task_id,
        artifact_id=primary,
        kind="review",
        outcome=outcome,
        subject_ref=f"evidence://review/{task_id}",
        payload=payload,
        created_by_agent=reviewer_agent_id,
        created_by_user=reviewer_user_id,
    )

    # R3 — APPROVE is the disjoint reviewer's proof row + a one-way seal of the
    # current set.  A re-review (rework_of set) on an already-SEALED set is a
    # fail-closed double-seal; a fresh APPROVE seals.
    seal_ids = tuple(a.id for a in current) if outcome == "pass" else ()
    if outcome == "pass" and rework_of is None:
        # A fresh APPROVE seals any DRAFT current artifacts.
        already_sealed = [a.id for a in current if a.seal_status == "SEALED"]
        if already_sealed and all(a.seal_status == "SEALED" for a in current):
            # Everything already sealed with a fresh pass = a double-seal.
            return ReviewPlan(
                RV_ALREADY_SEALED,
                outcome,
                evidence=evidence,
                detail="the current artifact set is already SEALED (no re-review cycle)",
            )
    return ReviewPlan(RV_OK, outcome, evidence=evidence, seal_ids=seal_ids)


def plan_rework(
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    builder_agent_id: uuid.UUID,
    fail_review: EvidenceRecord,
    old_artifacts: Sequence[ArtifactRecord],
    new_artifacts: Sequence[ArtifactRecord],
    new_evidence: Sequence[EvidenceRecord],
) -> ReworkPlan:
    """Guard a rework (REQUEST_CHANGES -> REWORKING -> RE_REVIEW, R5/R6/R7).

    Pure, DB-free.  The builder's NEW rows supersede the OLD current set via
    ``superseded_by`` links (G2: old rows are never mutated, only linked).
    The rework step ends at "new artifacts + new evidence + superseded_by
    links" (design §3.3 REWORKING->RE_REVIEW: the builder writes *nothing
    new* at RE_REVIEW — the new set is simply current).  It produces **no**
    ``kind='review'`` verdict row: the re-review VERDICT is a separate
    disjoint-Reviewer act, recorded via :func:`plan_review` /
    :meth:`ReviewReworkService.record_review` with ``rework_of =
    fail_review.id`` (I-5, G3), which enforces invariant-13.  ``builder_agent_id``
    records *who ran the rework* (the builder, §3.3 REQUEST_CHANGES->
    REWORKING Actor=Builder) — it is provenance, never a verdict author.

    A rework MUST produce new evidence (I-4): at least one new
    ``test_result`` / ``file_revision`` evidence row bound to the rework
    execution.

    Fail-closed codes: RV_NO_CURRENT_ARTIFACTS (nothing to supersede / no new
    set), RV_NO_SOURCE-style EV codes are raised later by the DAO; here the
    lane reports RV_INVALID_INPUT when the new set re-cites a stale artifact
    with no new proof.
    """
    old_current = [a for a in old_artifacts if a.superseded_by is None]
    new_rows = list(new_artifacts)
    new_evidence_rows = list(new_evidence)
    if not new_rows:
        return ReworkPlan(
            RV_NO_CURRENT_ARTIFACTS,
            detail="a rework must produce at least one NEW artifact row",
        )
    # I-4 — a rework must add genuinely new verification evidence (Root §4).
    new_proof = [e for e in new_evidence_rows if e.kind in ("test_result", "file_revision")]
    if not new_proof:
        return ReworkPlan(
            RV_INVALID_INPUT,
            new_artifacts=tuple(new_rows),
            detail="a rework must produce new test_result / file_revision evidence (I-4)",
        )
    # Link each NEW row to the old row it replaces.  When the counts align one
    # for one (a superseding rework), pair them by index; otherwise the new
    # rows simply form the new current set and the old set is superseded as a
    # whole (a new row per old row is the design's minimal case).
    supersede_links: list[tuple[uuid.UUID, uuid.UUID]] = []
    for new_row in new_rows:
        # The old row a new row replaces: the matching old artifact (same
        # storage locator) if present, else the first old row that has not yet
        # been linked.  This is the in-storage provenance link (design R5).
        old_for_new = next(
            (o for o in old_current if o.superseded_by is None and o.storage_ref == new_row.storage_ref),
            None,
        )
        if old_for_new is not None:
            supersede_links.append((old_for_new.id, new_row.id))
    # The rework ends here — new artifacts + new evidence + superseded_by
    # links.  It writes NO ``kind='review'`` verdict row (the disjoint
    # reviewer's later record_review(rework_of=fail_review.id) is the RE_REVIEW
    # verdict, carrying payload.rework_of for G3 provenance).
    return ReworkPlan(
        RV_OK,
        new_artifacts=tuple(new_rows),
        supersede_links=tuple(supersede_links),
        new_evidence=tuple(new_evidence_rows),
    )


def rework_provenance(
    *,
    fail_review: EvidenceRecord,
    artifacts: Sequence[ArtifactRecord],
    reviews: Sequence[EvidenceRecord],
    evidence: Sequence[EvidenceRecord],
) -> ReworkProvenance:
    """Walk the stored Rework -> REQUEST_CHANGES chain (design §3.5, G3).

    Pure read over the two ledgers: returns the fail row's required_changes,
    the (old, new) supersession pairs it triggered, the NEW current artifact
    set, the re-review row (``payload.rework_of == fail_review.id``), and the
    new proof evidence.  Every link is a stored edge — no new column, no
    second authority.
    """
    payload = fail_review.payload or {}
    required_changes = payload.get("required_changes", [])
    by_old: dict[uuid.UUID, uuid.UUID] = {}
    for a in artifacts:
        if a.superseded_by is not None:
            by_old[a.id] = a.superseded_by
    superseded_pairs = [(old, new) for old, new in by_old.items()]
    current = tuple(a.id for a in artifacts if a.superseded_by is None)
    re_review = next(
        (
            r
            for r in reviews
            if r.kind == "review" and (r.payload or {}).get("rework_of") == str(fail_review.id)
        ),
        None,
    )
    new_evidence = tuple(
        e
        for e in evidence
        if e.kind in ("test_result", "file_revision") and e.artifact_id in current
    )
    return ReworkProvenance(
        fail_review=fail_review,
        required_changes=list(required_changes),
        superseded_pairs=superseded_pairs,
        current_artifact_ids=current,
        re_review=re_review,
        new_evidence=new_evidence,
    )


# ---------------------------------------------------------------------------
# Evidence generation from execution results (the task's guarantee #1).
# ---------------------------------------------------------------------------


def build_execution_evidence(
    execution: Any,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    kind: str = "tool_result",
    outcome: str | None = None,
    subject_ref: str | None = None,
    payload: dict | None = None,
) -> list[EvidenceRecord]:
    """Mint durable evidence rows FROM an execution result (guarantee #1).

    ``execution`` is a frozen ``AgentToolExecution`` (or any object exposing
    ``id`` / ``status`` / ``result_ref`` / ``result_metadata``).  Evidence is a
    *durable authority* — not the transient ``result_metadata`` carrier —
    bound to the source execution (``execution_id``, the D5 provenance edge):

    - ``outcome`` defaults to the execution's terminal fact
      (``succeeded`` -> pass, ``failed`` -> fail; an unsettled ``started`` /
      ``unknown`` yields no evidence — a half-proof is worse than none).
    - ``kind='tool_result'`` cites ``execution.result_ref`` (the
      ``tool-result://`` ref the deterministic verifier already re-checks).
    - ``kind='test_result'`` REQUIRES an execution (invariant 12) and carries
      bounded test counts in ``payload``.

    A row with no source execution AND no human/agent actor is refused
    (fail-closed, D5) — matching the design's "no Evidence without
    provenance" hard boundary.
    """
    if not getattr(execution, "id", None):
        return []
    execution_id = execution.id
    status = getattr(execution, "status", None)
    if status in (None, "started", "unknown"):
        # Unsettled execution: no durable verdict yet (fail-closed).
        return []
    if outcome is None:
        outcome = "pass" if status == "succeeded" else "fail"
    if outcome not in EVIDENCE_OUTCOMES:
        raise ReviewReworkError(RV_INVALID_INPUT, f"outcome {outcome!r} not in {EVIDENCE_OUTCOMES}")

    result_ref = getattr(execution, "result_ref", None)
    if subject_ref is None:
        if kind == "test_result":
            subject_ref = f"test://{task_id or execution_id}"
        else:
            subject_ref = result_ref or f"tool-result://{execution_id}"

    payload = dict(payload or {})
    result_metadata = getattr(execution, "result_metadata", None)
    if kind == "test_result" and isinstance(result_metadata, dict):
        # Bounded structured facts (counts / exit status) — never a full XML
        # (that is an artifact, design §10).
        for key in ("tests_total", "tests_passed", "tests_failed", "exit_code"):
            if key in result_metadata:
                payload[key] = result_metadata[key]

    row = EvidenceRecord(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        execution_id=execution_id,
        kind=kind,
        outcome=outcome,
        subject_ref=subject_ref,
        payload=payload or None,
    )
    return [row]


# ---------------------------------------------------------------------------
# DB-bound service — runs a pure plan, then writes through the append-only DAOs.
# ---------------------------------------------------------------------------


@dataclass
class ReviewOutcome:
    """The result of a :meth:`ReviewReworkService.record_review` attempt."""

    code: str
    evidence: EvidenceRecord | None = None
    sealed_artifact_ids: tuple[uuid.UUID, ...] = ()
    detail: str = ""


@dataclass
class ReworkOutcome:
    """The result of a :meth:`ReviewReworkService.record_rework` attempt.

    The rework ends at "new artifacts + new evidence + superseded_by links".
    It writes **no** ``kind='review'`` verdict row: the re-review VERDICT is a
    separate disjoint-Reviewer act (``record_review(rework_of=fail.id)``,
    invariant-13), so there is no reviewer-row field here.
    """

    code: str
    new_artifact_ids: tuple[uuid.UUID, ...] = ()
    superseded_links: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    new_evidence_ids: tuple[uuid.UUID, ...] = ()
    detail: str = ""


class ReviewReworkService:
    """The service-side guard layer over the two parent ledger tables.

    The DB-bound methods run a pure plan, then write through the append-only
    DAOs in the caller's transaction (``db``), mapping the DAO's closed codes
    to the lane's ``RV_*`` vocabulary.  No review/rework table or column is
    introduced (R0); no assignment-time independence is re-implemented (R2).
    """

    async def record_review(
        self,
        db,
        *,
        task_id: uuid.UUID,
        tenant_id: uuid.UUID,
        outcome: str,
        reviewer_agent_id: uuid.UUID | None,
        reviewer_user_id: uuid.UUID | None,
        builder_agent_ids: frozenset[uuid.UUID],
        current_artifacts: Sequence[ArtifactRecord],
        cited_artifact_ids: Sequence[uuid.UUID],
        verdict: str = "",
        required_changes: Sequence[Any] | None = None,
        critiques: Sequence[Any] | None = None,
        rework_of: uuid.UUID | None = None,
    ) -> ReviewOutcome:
        """Apply one guarded review transition (design §3.3).

        Writes the single ``kind='review'`` verdict row (the review lane's only
        durable record, R1) and, on APPROVE, seals the current artifact set
        DRAFT->SEALED one-way (R3).  A 2nd seal on an already-SEALED set is
        rejected (RV_ALREADY_SEALED).  All writes ride the caller's transaction.
        """
        plan = plan_review(
            task_id=task_id,
            tenant_id=tenant_id,
            outcome=outcome,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_user_id=reviewer_user_id,
            builder_agent_ids=builder_agent_ids,
            current_artifacts=current_artifacts,
            cited_artifact_ids=cited_artifact_ids,
            verdict=verdict,
            required_changes=required_changes,
            critiques=critiques,
            rework_of=rework_of,
        )
        if plan.code != RV_OK:
            return ReviewOutcome(plan.code, evidence=plan.evidence, detail=plan.detail)
        assert plan.evidence is not None
        try:
            evidence = await evidence_record_dao.add_evidence(
                plan.evidence,
                tenant_id=tenant_id,
                db=db,
                reviewer_builder_agents=set(builder_agent_ids),
            )
        except ArtifactEvidenceClosedError as exc:
            # The DAO backstop re-raises the verdict-time independence guard
            # (EV_REVIEW_NOT_INDEPENDENT) and the payload overrun — surface them
            # under the lane's closed vocabulary.
            code = exc.code if exc.code in (RV_REVIEW_NOT_INDEPENDENT, RV_PAYLOAD_OVERRUN, "EV_REVIEW_NOT_INDEPENDENT") else RV_INVALID_INPUT
            if exc.code == "EV_REVIEW_NOT_INDEPENDENT":
                code = RV_REVIEW_NOT_INDEPENDENT
            return ReviewOutcome(code, evidence=plan.evidence, detail=exc.detail)

        sealed: list[uuid.UUID] = []
        if plan.seal_ids:
            for artifact_id in plan.seal_ids:
                row = next((a for a in current_artifacts if a.id == artifact_id), None)
                if row is None or row.seal_status == "SEALED":
                    continue
                try:
                    await artifact_record_dao.seal(row, db=db)
                    sealed.append(row.id)
                except ArtifactEvidenceClosedError as exc:
                    # A double-seal on the same row -> the lane's closed code.
                    return ReviewOutcome(RV_ALREADY_SEALED, evidence=evidence, sealed_artifact_ids=tuple(sealed), detail=exc.detail)
        return ReviewOutcome(
            RV_OK, evidence=evidence, sealed_artifact_ids=tuple(sealed),
        )

    async def record_rework(
        self,
        db,
        *,
        task_id: uuid.UUID,
        tenant_id: uuid.UUID,
        builder_agent_id: uuid.UUID,
        fail_review: EvidenceRecord,
        old_artifacts: Sequence[ArtifactRecord],
        new_artifacts: Sequence[ArtifactRecord],
        new_evidence: Sequence[EvidenceRecord],
    ) -> ReworkOutcome:
        """Record a rework: NEW rows supersede the OLD set (G2, R5).

        Inserts the builder's NEW artifact rows + NEW proof evidence, then
        records the ``superseded_by`` links (old rows become historical, never
        mutated).  The rework ends at this handoff (design §3.3 REWORKING ->
        RE_REVIEW: the builder writes "nothing new" at RE_REVIEW).  It writes
        **no** ``kind='review'`` verdict row — the re-review VERDICT is a
        separate disjoint-Reviewer act via :meth:`record_review`
        (``rework_of = fail_review.id``, invariant-13 enforced there, carrying
        ``payload.rework_of`` for G3 provenance).  The rework MUST add new
        ``test_result`` / ``file_revision`` evidence (I-4) or the plan fails
        closed.
        """
        plan = plan_rework(
            task_id=task_id,
            tenant_id=tenant_id,
            builder_agent_id=builder_agent_id,
            fail_review=fail_review,
            old_artifacts=old_artifacts,
            new_artifacts=new_artifacts,
            new_evidence=new_evidence,
        )
        if plan.code != RV_OK:
            return ReworkOutcome(plan.code, detail=plan.detail)

        new_artifact_ids: list[uuid.UUID] = []
        for row in plan.new_artifacts:
            try:
                written = await artifact_record_dao.add_artifact(row, tenant_id=tenant_id, db=db)
                new_artifact_ids.append(written.id)
            except ArtifactEvidenceClosedError as exc:
                return ReworkOutcome(RV_INVALID_INPUT, detail=f"{exc.code}: {exc.detail}")
        new_evidence_ids: list[uuid.UUID] = []
        for row in plan.new_evidence:
            try:
                # The rework's new proof evidence (test_result / file_revision,
                # kind != 'review') — no verdict row is written here, so the
                # invariant-13 reviewer_builder_agents guard does not apply.
                written = await evidence_record_dao.add_evidence(row, tenant_id=tenant_id, db=db)
                new_evidence_ids.append(written.id)
            except ArtifactEvidenceClosedError as exc:
                return ReworkOutcome(RV_INVALID_INPUT, detail=f"{exc.code}: {exc.detail}")
        # Record the supersession links (old row -> new row).  The NEW row is
        # current; the OLD row becomes historical via superseded_by (D2/G2).
        superseded: list[tuple[uuid.UUID, uuid.UUID]] = []
        for old_id, new_id in plan.supersede_links:
            old_row = next((a for a in old_artifacts if a.id == old_id), None)
            if old_row is None:
                continue
            try:
                await artifact_record_dao.supersede(old_row, new_id=new_id, db=db)
                superseded.append((old_id, new_id))
            except ArtifactEvidenceClosedError:
                # Already superseded: the link is idempotent, keep going.
                superseded.append((old_id, new_id))
        return ReworkOutcome(
            RV_OK,
            new_artifact_ids=tuple(new_artifact_ids),
            superseded_links=tuple(superseded),
            new_evidence_ids=tuple(new_evidence_ids),
        )

    async def record_execution_evidence(
        self,
        db,
        *,
        tenant_id: uuid.UUID,
        execution: Any,
        task_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        kind: str = "tool_result",
        outcome: str | None = None,
        subject_ref: str | None = None,
        payload: dict | None = None,
    ) -> list[EvidenceRecord]:
        """Generate durable evidence from an execution result (guarantee #1).

        Mints the evidence rows via :func:`build_execution_evidence` and writes
        them through the append-only DAO in the caller's transaction.  An
        unsettled execution yields an empty list (fail-closed, no half-proof).
        """
        rows = build_execution_evidence(
            execution,
            tenant_id=tenant_id,
            task_id=task_id,
            project_id=project_id,
            kind=kind,
            outcome=outcome,
            subject_ref=subject_ref,
            payload=payload,
        )
        written: list[EvidenceRecord] = []
        for row in rows:
            try:
                written.append(
                    await evidence_record_dao.add_evidence(row, tenant_id=tenant_id, db=db)
                )
            except ArtifactEvidenceClosedError as exc:
                raise ReviewReworkError(RV_INVALID_INPUT, f"{exc.code}: {exc.detail}") from exc
        return written

    # --- reads (design §3.4 / §3.5) over the two parent ledgers ---
    async def current_valid_review(
        self,
        db,
        *,
        task_id: uuid.UUID,
    ) -> EvidenceRecord | None:
        """The "current valid review" for one Task (design §3.4, guarantee
        "能识别当前有效 Review").  Reads only — the ledgers are the sole
        authority; an old APPROVE on a superseded set drops out automatically."""
        artifacts = await artifact_record_dao.list_by_task(task_id, db=db, current_only=False)
        reviews = await evidence_record_dao.list_reviews_for_task(task_id, db=db)
        return current_valid_review(artifacts, reviews)

    async def rework_provenance_for_task(
        self,
        db,
        *,
        task_id: uuid.UUID,
        fail_review_id: uuid.UUID,
    ) -> ReworkProvenance | None:
        """Walk one stored Rework -> REQUEST_CHANGES chain (design §3.5, G3).

        Returns :data:`None` when no fail row of that id exists for the Task.
        """
        fail = await evidence_record_dao.get(fail_review_id, db=db)
        if fail is None or fail.kind != "review" or fail.outcome != "fail":
            return None
        artifacts = await artifact_record_dao.list_by_task(task_id, db=db, current_only=False)
        reviews = await evidence_record_dao.list_reviews_for_task(task_id, db=db)
        evidence = [r for r in reviews]
        return rework_provenance(
            fail_review=fail,
            artifacts=artifacts,
            reviews=reviews,
            evidence=evidence,
        )


__all__ = [
    "REVIEW_DECISIONS",
    "REVIEW_RESULT_CODES",
    "RV_ALREADY_SEALED",
    "RV_INCONCLUSIVE",
    "RV_INVALID_INPUT",
    "RV_NO_CURRENT_ARTIFACTS",
    "RV_NO_REVIEWER",
    "RV_OK",
    "RV_PAYLOAD_OVERRUN",
    "RV_REVIEW_NOT_INDEPENDENT",
    "RV_SUPERSEDED_SET",
    "ReviewOutcome",
    "ReviewPlan",
    "ReviewReworkError",
    "ReviewReworkService",
    "ReworkOutcome",
    "ReworkPlan",
    "ReworkProvenance",
    "build_execution_evidence",
    "current_valid_review",
    "plan_review",
    "plan_rework",
    "rework_provenance",
]

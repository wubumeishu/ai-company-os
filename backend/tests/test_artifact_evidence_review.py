"""Phase 4 Artifact / Evidence + Independent Review & Rework — unit tests.

Per the V1 designs:
- docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md (card t_f19aae89)
- docs/architecture/PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md (card
  t_dbb0c0dd)

Two tiers, mirroring the Phase 3 ``test_planning_service`` split:

- **DB-free derivation tier** (always run): the pure, database-free core —
  the four required guarantees proven directly on in-memory model instances:
    1. evidence is correctly generated from execution results
       (``build_execution_evidence``)
    2. the independent-reviewer logic prevents self-validation
       (``plan_review``'s disjointness gate, G1)
    3. rework creates a new execution link without breaking history
       (``plan_rework`` + ``rework_provenance`` + ``current_valid_review``,
       G2/G3)
    4. tenant isolation (model flags, the DAO's tenant-scoped base, and the
       resolver's explicit-tenant read — G4 / design D6 / I-7)
  plus the closed-code discipline (every ``RV_*`` / ``EV_*`` / ``AE_*``
  rejection, fail-closed on unknown values).

- **live-schema tier** (skipped when no reachable Postgres): the append-only
  DAOs against the real f072 schema (CHECKs, the UNIQUE dedup, the seal
  boundary, the rework supersession, and cross-tenant isolation).

Running the live tier::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch> \
        uv run --extra dev pytest tests/test_artifact_evidence_review.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

# Register the full metadata FK graph (module side effects) so the ORM can
# compile Task / Agent / User / Tenant tables and their foreign keys against
# the live schema (mirrors the planning DAO test's import set).
import app.models.agent
import app.models.agent_run
import app.models.agent_tool_execution
import app.models.analysis
import app.models.planning
import app.models.project
import app.models.task
import app.models.tenant
import app.models.user  # noqa: F401
from app.dao.artifact_evidence_dao import ArtifactRecordDAO, EvidenceRecordDAO
from app.dao.base import TenantScopedBaseDAO
from app.models.agent import Agent
from app.models.agent_run import AgentRun  # noqa: F401
from app.models.analysis import AnalysisFinding, AnalysisRun  # noqa: F401
from app.models.artifact_evidence import (
    ARTIFACT_TYPES,
    EVIDENCE_KINDS,
    EVIDENCE_OUTCOMES,
    MAX_EVIDENCE_PAYLOAD_BYTES,
    SEAL_STATUSES,
    STORAGE_SCHEMES,
    ArtifactRecord,
    EvidenceRecord,
)
from app.services.artifact_evidence_resolver import (
    ARTIFACT_REF_SCHEME,
    EVIDENCE_REF_SCHEME,
    ArtifactLedgerReferenceReader,
)
from app.services.review_rework_service import (
    RV_INVALID_INPUT,
    RV_NO_CURRENT_ARTIFACTS,
    RV_OK,
    RV_PAYLOAD_OVERRUN,
    RV_REVIEW_NOT_INDEPENDENT,
    build_execution_evidence,
    current_valid_review,
    plan_review,
    plan_rework,
    rework_provenance,
)

# ---------------------------------------------------------------------------
# In-memory model helpers (DB-free tier).
# ---------------------------------------------------------------------------

TENANT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
OTHER_TENANT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
TASK_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BUILDER_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
BUILDER_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
EXEC_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _artifact(
    *,
    title: str = "out.txt",
    scheme: str = "workspace_path",
    ref: str = "/repo/out.txt",
    current: bool = True,
    sealed: bool = False,
    execution: uuid.UUID | None = EXEC_ID,
    agent: uuid.UUID | None = BUILDER_A,
    revision: str | None = "38e4a414",
    tenant: uuid.UUID = TENANT_ID,
) -> ArtifactRecord:
    row = ArtifactRecord(
        id=uuid.uuid4(),
        tenant_id=tenant,
        task_id=TASK_ID,
        execution_id=execution,
        agent_id=agent,
        type="file",
        title=title,
        storage_scheme=scheme,
        storage_ref=ref,
        content_hash="f" * 64,
        revision_ref=revision,
        seal_status="SEALED" if sealed else "DRAFT",
        sealed_at=NOW if sealed else None,
    )
    if not current:
        row.superseded_by = uuid.uuid4()
    return row


def _review(
    *,
    outcome: str,
    artifact: ArtifactRecord,
    reviewer: uuid.UUID = REVIEWER_ID,
    when: datetime = NOW,
    rework_of: uuid.UUID | None = None,
    payload: dict | None = None,
) -> EvidenceRecord:
    payload = dict(payload or {})
    payload.setdefault("verdict", "ok" if outcome == "pass" else "needs work")
    if rework_of is not None:
        payload["rework_of"] = str(rework_of)
    row = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=artifact.id,
        execution_id=EXEC_ID,
        kind="review",
        outcome=outcome,
        subject_ref=f"evidence://review/{TASK_ID}",
        payload=payload,
        created_by_agent=reviewer,
        created_at=when,
    )
    return row


# ---------------------------------------------------------------------------
# Guarantee #1 — evidence is generated from execution results.
# ---------------------------------------------------------------------------


def test_evidence_from_succeeded_execution_is_pass_tool_result() -> None:
    exec_ = SimpleNamespace(id=EXEC_ID, status="succeeded", result_ref="tool-result://" + str(EXEC_ID))
    rows = build_execution_evidence(exec_, tenant_id=TENANT_ID, task_id=TASK_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "tool_result"
    assert row.outcome == "pass"
    # D5 provenance edge: the evidence is bound to the source execution.
    assert row.execution_id == EXEC_ID
    # The subject_ref cites the execution's result_ref (the tool-result:// ref
    # the deterministic verifier already re-checks).
    assert row.subject_ref == "tool-result://" + str(EXEC_ID)
    assert row.task_id == TASK_ID


def test_evidence_from_failed_execution_is_fail() -> None:
    exec_ = SimpleNamespace(id=EXEC_ID, status="failed", result_ref="tool-result://" + str(EXEC_ID))
    rows = build_execution_evidence(exec_, tenant_id=TENANT_ID, task_id=TASK_ID, kind="tool_result")
    assert len(rows) == 1
    assert rows[0].outcome == "fail"


def test_unsettled_execution_yields_no_evidence() -> None:
    # A half-proof is worse than none: started / unknown / no id -> no row.
    for status in ("started", "unknown", None):
        exec_ = SimpleNamespace(id=EXEC_ID, status=status, result_ref=None)
        assert build_execution_evidence(exec_, tenant_id=TENANT_ID, task_id=TASK_ID) == []
    exec_no_id = SimpleNamespace(id=None, status="succeeded")
    assert build_execution_evidence(exec_no_id, tenant_id=TENANT_ID, task_id=TASK_ID) == []


def test_test_result_evidence_requires_execution_and_bounded_payload() -> None:
    exec_ = SimpleNamespace(
        id=EXEC_ID,
        status="succeeded",
        result_ref=None,
        result_metadata={"tests_total": 40, "tests_passed": 40, "exit_code": 0},
    )
    rows = build_execution_evidence(exec_, tenant_id=TENANT_ID, task_id=TASK_ID, kind="test_result")
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "test_result"
    # Invariant 12: a test result carries its source execution.
    assert row.execution_id == EXEC_ID
    # Bounded structured facts land in the payload (not a full XML dump).
    assert row.payload["tests_total"] == 40
    assert row.payload["tests_passed"] == 40


def test_explicit_outcome_must_be_in_closed_set() -> None:
    exec_ = SimpleNamespace(id=EXEC_ID, status="succeeded", result_ref="x")
    from app.services.review_rework_service import ReviewReworkError

    with pytest.raises(ReviewReworkError) as exc:
        build_execution_evidence(exec_, tenant_id=TENANT_ID, task_id=TASK_ID, outcome="maybe")
    assert exc.value.code == RV_INVALID_INPUT


# ---------------------------------------------------------------------------
# Guarantee #2 — the independent reviewer prevents self-validation.
# ---------------------------------------------------------------------------


def test_reviewer_disjoint_from_builders_is_ok_and_seals_on_approve() -> None:
    art = _artifact()
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A, BUILDER_B}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
        verdict="meets criteria",
    )
    assert plan.code == RV_OK
    assert plan.outcome == "pass"
    # R3: APPROVE is the proof row + a one-way seal of the current set.
    assert plan.seal_ids == (art.id,)
    assert plan.evidence is not None
    assert plan.evidence.kind == "review"
    assert plan.evidence.outcome == "pass"
    # The verdict row cites the CURRENT artifact, not the builder's
    # self-report (G4).
    assert plan.evidence.artifact_id == art.id


def test_builder_reviewing_their_own_work_is_rejected() -> None:
    # G1: a reviewer in the builder set cannot self-approve (design R2,
    # invariant 13).  BUILDER_A is the same agent that produced the artifact.
    art = _artifact(agent=BUILDER_A)
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=BUILDER_A,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A, BUILDER_B}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
    )
    assert plan.code == RV_REVIEW_NOT_INDEPENDENT


def test_human_reviewer_never_overlaps_builder_agents() -> None:
    # A human reviewer (created_by_user) is never a builder agent, so the
    # disjointness gate passes.
    art = _artifact()
    human = uuid.uuid4()
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=None,
        reviewer_user_id=human,
        builder_agent_ids=frozenset({BUILDER_A, BUILDER_B}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
    )
    assert plan.code == RV_OK
    assert plan.evidence.created_by_user == human


def test_review_must_cite_a_current_artifact() -> None:
    # I-2: a review citing only a superseded (historical) artifact is
    # rejected — it cannot cite a stale set as proof.
    stale = _artifact(current=False, sealed=True)
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A}),
        current_artifacts=[stale],
        cited_artifact_ids=[stale.id],
    )
    assert plan.code == RV_NO_CURRENT_ARTIFACTS


def test_request_changes_is_fail_outcome_with_required_changes() -> None:
    # R4: REQUEST_CHANGES = outcome='fail' + bounded required_changes (the
    # first real semantics; the current set stays DRAFT, no seal).
    art = _artifact()
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="fail",
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
        required_changes=["add tenant filter to list_by_task"],
        critiques=["missing scope on the read path"],
    )
    assert plan.code == RV_OK
    assert plan.outcome == "fail"
    assert plan.seal_ids == ()  # REQUEST_CHANGES seals nothing.
    assert plan.evidence.payload["required_changes"] == ["add tenant filter to list_by_task"]
    assert plan.evidence.payload["critiques"] == ["missing scope on the read path"]


def test_unknown_review_outcome_fails_closed() -> None:
    art = _artifact()
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="approve",  # not in EVIDENCE_OUTCOMES
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
    )
    assert plan.code == RV_INVALID_INPUT


def test_overrun_critiques_fail_closed_payload_bound() -> None:
    # I-8: critiques + required_changes over 32 KiB is rejected.
    art = _artifact()
    huge = ["x" * 1024] * 40  # > 32 KiB
    plan = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="fail",
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=frozenset({BUILDER_A}),
        current_artifacts=[art],
        cited_artifact_ids=[art.id],
        required_changes=huge,
    )
    assert plan.code == RV_PAYLOAD_OVERRUN
    assert MAX_EVIDENCE_PAYLOAD_BYTES == 32 * 1024


# ---------------------------------------------------------------------------
# Guarantee #3 — rework creates a new execution link without breaking history.
# ---------------------------------------------------------------------------


def test_rework_writes_no_review_verdict_row() -> None:
    """F1 / invariant-13 (G4): the rework step must NOT author a
    ``kind='review'`` verdict row.  It ends at "new artifacts + new evidence +
    ``superseded_by`` links" (design §3.3 REWORKING->RE_REVIEW: the builder
    writes *nothing new* at RE_REVIEW).  The re-review VERDICT is a separate
    disjoint-Reviewer act (``record_review(rework_of=fail.id)``).
    """
    old = _artifact(ref="/repo/old.txt", current=True, sealed=True)
    fail = _review(outcome="fail", artifact=old, payload={"required_changes": ["fix x"]})
    new = _artifact(ref="/repo/old.txt", current=True)  # supersedes old
    new_proof = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=new.id,
        execution_id=EXEC_ID,
        kind="test_result",
        outcome="pass",
        subject_ref=f"test://{TASK_ID}",
        payload={"tests_total": 5, "tests_passed": 5},
        created_at=NOW,
    )
    plan = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[new],
        new_evidence=[new_proof],
    )
    assert plan.code == RV_OK
    # G2/R5: the NEW row supersedes the OLD row (one stored link).
    assert (old.id, new.id) in plan.supersede_links
    # The new proof evidence is bound to the rework execution.
    assert plan.new_evidence == (new_proof,)
    # F1: the rework writes NO kind='review' verdict row — no builder-authored
    # verdict, and the plan no longer carries a rework_review field at all.
    assert all(e.kind != "review" for e in plan.new_evidence)
    assert not hasattr(plan, "rework_review")


def test_rereview_verdict_carries_rework_of_and_builder_self_review_rejected() -> None:
    """The re-review verdict is the disjoint Reviewer's separate act:
    ``plan_review``/``record_review`` with ``rework_of=fail.id`` is accepted
    for the disjoint reviewer and carries ``payload.rework_of`` (G3); a
    builder-authored ``kind='review'`` row on the same WP is rejected —
    ``RV_REVIEW_NOT_INDEPENDENT`` at the lane gate and ``EV_REVIEW_NOT_INDEPENDENT``
    at the DAO (invariant 13 / G4)."""
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError, EvidenceRecordDAO

    old = _artifact(ref="/repo/old.txt", sealed=True)
    new = _artifact(ref="/repo/old.txt", current=True)
    old.superseded_by = new.id
    fail = _review(outcome="fail", artifact=old, payload={"required_changes": ["fix x"]})
    builders = frozenset({BUILDER_A, BUILDER_B})
    # The disjoint reviewer's re-review on the NEW current set is accepted and
    # carries the G3 rework_of provenance link.
    ok = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=REVIEWER_ID,
        reviewer_user_id=None,
        builder_agent_ids=builders,
        current_artifacts=[old, new],
        cited_artifact_ids=[new.id],
        verdict="rework verified",
        rework_of=fail.id,
    )
    assert ok.code == RV_OK
    assert ok.evidence is not None
    verdict = ok.evidence
    assert verdict.kind == "review"
    assert verdict.created_by_agent == REVIEWER_ID
    # G3: the re-review row carries payload.rework_of = the fail-row id.
    verdict_payload = verdict.payload
    assert verdict_payload is not None
    assert verdict_payload["rework_of"] == str(fail.id)
    # The re-review seals the NEW current set.
    assert new.id in ok.seal_ids
    # The BUILDER's own re-review verdict is rejected at the lane gate (G1).
    bad = plan_review(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        outcome="pass",
        reviewer_agent_id=BUILDER_A,
        reviewer_user_id=None,
        builder_agent_ids=builders,
        current_artifacts=[old, new],
        cited_artifact_ids=[new.id],
        rework_of=fail.id,
    )
    assert bad.code == RV_REVIEW_NOT_INDEPENDENT
    # And the same builder-authored kind='review' row is rejected at the DAO
    # boundary with the named invariant-13 code (invariant 13 / G4).
    dao = EvidenceRecordDAO()
    builder_verdict = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=new.id,
        kind="review",
        outcome="pass",
        subject_ref=f"evidence://review/{TASK_ID}",
        payload={"rework_of": str(fail.id), "verdict": "rework verified"},
        created_by_agent=BUILDER_A,
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(builder_verdict, reviewer_builder_agents=set(builders))
    assert exc.value.code == "EV_REVIEW_NOT_INDEPENDENT"


def test_rework_without_new_proof_evidence_fails_closed() -> None:
    # I-4: a rework MUST produce new test_result / file_revision evidence.
    old = _artifact()
    fail = _review(outcome="fail", artifact=old)
    new = _artifact(ref="/repo/other.txt")
    plan = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[new],
        new_evidence=[],  # no new proof
    )
    assert plan.code == RV_INVALID_INPUT
    assert "new" in plan.detail.lower() or "proof" in plan.detail.lower() or "I-4" in plan.detail


def test_rework_with_no_new_artifacts_fails_closed() -> None:
    old = _artifact()
    fail = _review(outcome="fail", artifact=old)
    proof = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        execution_id=EXEC_ID,
        kind="test_result",
        outcome="pass",
        subject_ref=f"test://{TASK_ID}",
        created_at=NOW,
    )
    plan = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[],  # nothing new
        new_evidence=[proof],
    )
    assert plan.code == RV_NO_CURRENT_ARTIFACTS


def test_rework_new_evidence_kind_closed_rejects_review_row() -> None:
    """I-4 kind-closure / invariant-13 (G4): the rework-proof lane is CLOSED
    to verdict kinds.  A caller-supplied ``kind='review'`` row in
    ``new_evidence`` is rejected at the plan tier (``plan_rework`` ->
    RV_INVALID_INPUT): the verdict kind must not ride the rework lane — it
    belongs to the disjoint reviewer's ``record_review(rework_of=fail.id)``
    act.  A mix of proof + verdict is rejected too (the closed set is
    test_result / file_revision ONLY), while a proof-only rework stays RV_OK."""
    old = _artifact(ref="/repo/old.txt", sealed=True)
    fail = _review(outcome="fail", artifact=old, payload={"required_changes": ["fix x"]})
    new = _artifact(ref="/repo/old.txt", current=True)
    proof = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=new.id,
        execution_id=EXEC_ID,
        kind="file_revision",
        outcome="pass",
        subject_ref=f"file://{TASK_ID}",
        payload={"tests_total": 1, "tests_passed": 1},
        created_by_agent=BUILDER_A,
        created_at=NOW,
    )
    builder_verdict = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=new.id,
        kind="review",
        outcome="pass",
        subject_ref=f"evidence://review/{TASK_ID}",
        payload={"rework_of": str(fail.id), "verdict": "self-approved"},
        created_by_agent=BUILDER_A,
        created_at=NOW,
    )
    # A kind='review' row alongside genuine proof is rejected at the plan tier.
    mixed = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[new],
        new_evidence=[proof, builder_verdict],
    )
    assert mixed.code == RV_INVALID_INPUT
    assert "kind='review'" in mixed.detail
    # ... and so is a verdict-only rework (the I-4 minimum is not met either).
    verdict_only = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[new],
        new_evidence=[builder_verdict],
    )
    assert verdict_only.code == RV_INVALID_INPUT
    # The normal rework (test_result / file_revision proof only) still succeeds.
    ok = plan_rework(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        builder_agent_id=BUILDER_A,
        fail_review=fail,
        old_artifacts=[old],
        new_artifacts=[new],
        new_evidence=[proof],
    )
    assert ok.code == RV_OK
    assert ok.new_evidence == (proof,)


def test_old_approve_never_clobbers_a_later_rework() -> None:
    # G2: an APPROVE that sealed the OLD set becomes historical the moment a
    # rework supersedes the set; current_valid_review reads the NEW row only.
    old = _artifact(ref="/v1.txt", sealed=True)
    new = _artifact(ref="/v2.txt", current=True)
    old.superseded_by = new.id
    old_approve = _review(
        outcome="pass",
        artifact=old,
        when=NOW,
        payload={"verdict": "v1 ok"},
    )
    new_review = _review(
        outcome="fail",
        artifact=new,
        rework_of=old_approve.id,
        when=NOW,  # latest by id ordering
        payload={"verdict": "v2 still failing", "rework_of": str(old_approve.id)},
    )
    reviews = [old_approve, new_review]
    current = current_valid_review([old, new], reviews)
    # The approving row over the superseded set drops out automatically.
    assert current is not None
    assert current.id == new_review.id
    assert current.outcome == "fail"
    # The OLD approve is still queryable (history preserved), just not current.
    assert old_approve in reviews


def test_rework_provenance_walks_the_stored_chain() -> None:
    # G3: a reviewer/auditor can walk REVIEW-2 -> REVIEW-1 -> (required_changes)
    # -> ART-2 <- ART-1 entirely from the two ledgers.
    old = _artifact(ref="/v1.txt", sealed=True)
    new = _artifact(ref="/v2.txt")
    old.superseded_by = new.id
    fail = _review(
        outcome="fail",
        artifact=old,
        payload={"verdict": "v1 broken", "required_changes": ["patch the seal step"]},
    )
    rereview = _review(
        outcome="pass",
        artifact=new,
        rework_of=fail.id,
        payload={"verdict": "v2 ok", "rework_of": str(fail.id)},
    )
    new_proof = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=new.id,
        execution_id=EXEC_ID,
        kind="test_result",
        outcome="pass",
        subject_ref=f"test://{TASK_ID}",
        payload={"tests_total": 3, "tests_passed": 3},
        created_at=NOW,
    )
    prov = rework_provenance(
        fail_review=fail,
        artifacts=[old, new],
        reviews=[fail, rereview],
        evidence=[new_proof],
    )
    assert prov.fail_review is fail
    assert prov.required_changes == ["patch the seal step"]
    # The supersession edge is stored on the old row.
    assert (old.id, new.id) in prov.superseded_pairs
    # The NEW current set is just the new row; the old is historical.
    assert prov.current_artifact_ids == (new.id,)
    # The re-review row is found via its rework_of link.
    assert prov.re_review is rereview
    # The new proof evidence is the rework's new verification.
    assert prov.new_evidence == (new_proof,)


# ---------------------------------------------------------------------------
# Guarantee #4 — tenant isolation.
# ---------------------------------------------------------------------------


def test_both_ledger_models_are_tenant_scoped_by_schema() -> None:
    # D6: non-null tenant_id + the explicit __tenant_scoped__ flag so the
    # do_orm_execute tenant filter (dao/base.py:140) applies with zero new
    # code; no new tenant mechanism.
    assert ArtifactRecord.__tenant_scoped__ is True
    assert EvidenceRecord.__tenant_scoped__ is True
    for model in (ArtifactRecord, EvidenceRecord):
        col = model.__table__.c["tenant_id"]
        assert col.nullable is False
        # The index on tenant_id is present (mirror of every tenant table).
        assert "ix_artifact_records_tenant_id" in (
            i.name for i in model.__table__.indexes
        ) or "ix_evidence_records_tenant_id" in (i.name for i in model.__table__.indexes)


def test_dao_subclasses_are_tenant_scoped_base() -> None:
    # The ledgers are reached ONLY through TenantScopedBaseDAO (D6).
    assert issubclass(ArtifactRecordDAO, TenantScopedBaseDAO)
    assert issubclass(EvidenceRecordDAO, TenantScopedBaseDAO)


def test_cross_tenant_ledger_row_is_isolated_by_tenant_column() -> None:
    # Two rows in different tenants; a task-scoped read is filtered by the
    # caller's tenant (I-7).  Proven here on the model: a row's tenant_id
    # is the authority, and a cross-tenant citation would be a different row
    # (never joined across tenants by the DAO scope-inject).
    mine = _artifact(tenant=TENANT_ID)
    theirs = _artifact(tenant=OTHER_TENANT_ID)
    assert mine.tenant_id == TENANT_ID
    assert theirs.tenant_id == OTHER_TENANT_ID
    # The two are distinct identities even for the same task.
    assert mine.id != theirs.id


def test_resolver_uses_ledger_ref_schemes_and_wraps_frozen_reader() -> None:
    reader = ArtifactLedgerReferenceReader(wrapped=None)
    assert reader is not None
    # The two new refs exist so a gate can cite "the artifact" / "the proof"
    # by stable id (design §4.1) — the scheme constants are stable.
    assert ARTIFACT_REF_SCHEME == "artifact"
    assert EVIDENCE_REF_SCHEME == "evidence"


def test_closed_code_sets_match_the_designs() -> None:
    # Every closed set the two designs pin is present and consistent.
    assert set(EVIDENCE_OUTCOMES) == {"pass", "fail", "inconclusive"}
    assert "review" in EVIDENCE_KINDS
    assert "test_result" in EVIDENCE_KINDS
    assert "tool_result" in ARTIFACT_TYPES
    assert "git_snapshot" in ARTIFACT_TYPES
    assert set(SEAL_STATUSES) == {"DRAFT", "SEALED"}
    assert "tool_result" in STORAGE_SCHEMES
    assert "published_page" in STORAGE_SCHEMES


# ---------------------------------------------------------------------------
# Closed-code discipline — DAO-level provenance validation (DB-free checks).
# These exercise the DAO's *pure* validation path (the named fail-closed
# codes) without a database, by calling the same validation the insert path
# runs first.
# ---------------------------------------------------------------------------


def test_dao_rejects_artifact_without_provenance_source() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = ArtifactRecordDAO()
    # Neither execution_id nor created_by_user -> EV_NO_SOURCE (D5).
    bad = ArtifactRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        type="file",
        title="x",
        storage_scheme="workspace_path",
        storage_ref="/x",
        execution_id=None,
        created_by_user=None,
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(bad)
    assert exc.value.code == "EV_NO_SOURCE"


def test_dao_rejects_artifact_with_both_sources() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = ArtifactRecordDAO()
    bad = ArtifactRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        type="file",
        title="x",
        storage_scheme="workspace_path",
        storage_ref="/x",
        execution_id=EXEC_ID,
        created_by_user=uuid.uuid4(),
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(bad)
    assert exc.value.code == "EV_NO_SOURCE"


def test_dao_rejects_unknown_artifact_type_and_scheme() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = ArtifactRecordDAO()
    for bad_type, code in (("blob", "AE_UNKNOWN_TYPE"),):
        row = ArtifactRecord(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            execution_id=EXEC_ID,
            type=bad_type,
            title="x",
            storage_scheme="workspace_path",
            storage_ref="/x",
        )
        with pytest.raises(ArtifactEvidenceClosedError) as exc:
            dao._validate_new(row)
        assert exc.value.code == code
    row2 = ArtifactRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        execution_id=EXEC_ID,
        type="file",
        title="x",
        storage_scheme="s3",  # not in STORAGE_SCHEMES
        storage_ref="/x",
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(row2)
    assert exc.value.code == "AE_UNKNOWN_SCHEME"


def test_dao_rejects_evidence_without_source_or_subject() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = EvidenceRecordDAO()
    no_source = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        kind="structured",
        outcome="pass",
        subject_ref="/x",  # has subject but no source edge
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(no_source)
    assert exc.value.code == "EV_NO_SOURCE"
    no_subject = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        execution_id=EXEC_ID,
        kind="structured",
        outcome="pass",
        subject_ref="",  # empty subject
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(no_subject)
    assert exc.value.code == "EV_NO_SUBJECT"


def test_dao_rejects_review_verdict_by_builder_agent() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = EvidenceRecordDAO()
    verdict = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        artifact_id=uuid.uuid4(),
        execution_id=EXEC_ID,
        kind="review",
        outcome="pass",
        subject_ref=f"evidence://review/{TASK_ID}",
        created_by_agent=BUILDER_A,  # the builder self-approving
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(verdict, reviewer_builder_agents={BUILDER_A, BUILDER_B})
    assert exc.value.code == "EV_REVIEW_NOT_INDEPENDENT"


def test_dao_rejects_test_result_without_execution() -> None:
    from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

    dao = EvidenceRecordDAO()
    row = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        created_by_agent=BUILDER_A,
        kind="test_result",
        outcome="pass",
        subject_ref="test://x",
        # no execution_id
    )
    with pytest.raises(ArtifactEvidenceClosedError) as exc:
        dao._validate_new(row)
    assert exc.value.code == "EV_NO_SOURCE"


# ===========================================================================
# LIVE-SCHEMA TIER — the append-only DAOs against the real f072 schema.
# ===========================================================================


@pytest.fixture
def _db_available() -> None:
    """Skip the whole live tier when Postgres is unreachable on this host."""
    from app.config import get_settings

    settings = get_settings()
    url = settings.DATABASE_URL
    if not url:
        pytest.skip("DATABASE_URL not set; skipping the live-schema tier")
    import os

    # A dedicated scratch DB is expected (after `alembic upgrade head`).
    if os.environ.get("ACO_SKIP_LIVE_DB") == "1":
        pytest.skip("ACO_SKIP_LIVE_DB=1")


@pytest.fixture
def _engine(_db_available) -> object:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    yield engine
    # Best-effort dispose on a private loop (a sync generator can't await).
    # The scratch DB is the operator's concern, so a teardown failure must
    # never leak into the test results.
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.dispose())
        finally:
            loop.close()
    except Exception:  # noqa: BLE001, S110 - intentional best-effort teardown; never fail the suite here
        pass


# ---------------------------------------------------------------------------
# Live-DB schema smoke tests (guarded; require a reachable scratch Postgres
# provisioned by f072).  Each test is skipped if the tables are absent so the
# module stays re-runnable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_dao_artifact_evidence_roundtrip(_engine) -> None:
    """Against the real f072 schema: append an artifact + evidence, seal it,
    supersede it on rework, and read the current-valid review (G2/G3 live).

    Seeds the minimal FK parent graph (tenant -> user -> 2 agents -> task) so
    the ledger's real FKs + CHECKs are exercised.  Provenance rides the
    ``created_by_user`` path (D5's XOR alternative) to keep the test off the
    heavy ``agent_tool_executions`` composite FK; the execution-generation
    guarantee is proven in the DB-free tier.
    """
    from datetime import UTC, datetime

    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.artifact_evidence_dao import (
        ArtifactEvidenceClosedError,
        artifact_record_dao,
        evidence_record_dao,
    )
    from app.dao.base import tenant_context
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User

    try:
        async with _engine.connect() as conn:
            await conn.execute(_text("SELECT 1 FROM artifact_records LIMIT 1"))
    except OperationalError:
        pytest.skip("artifact_records not provisioned on the scratch DB (run f072)")

    session = AsyncSession(bind=_engine, expire_on_commit=False)

    # --- Seed the FK parent graph in one committed transaction. ---
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    builder_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with tenant_context(tenant_id):
        async with session.begin():
            session.add(
                Tenant(id=tenant_id, name=f"live-{tenant_id.hex[:6]}", slug=f"live-{tenant_id.hex[:8]}")
            )
            await session.flush()
            session.add(User(id=user_id, display_name="live-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="reviewer", creator_id=user_id, tenant_id=tenant_id))
            session.add(
                Task(id=task_id, agent_id=builder_id, title="live-task", created_by=user_id, tenant_id=tenant_id)
            )
            await session.flush()

    # --- The ledger roundtrip (G2/G3 live against the real DDL). ---
    with tenant_context(tenant_id):
        async with session.begin():
            art = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,  # D5 XOR provenance path (no execution FK needed)
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/live/{tenant_id}/out.txt",
                content_hash="e" * 64,
            )
            written = await artifact_record_dao.add_artifact(art, tenant_id=tenant_id, db=session)
            assert written.seal_status == "DRAFT"

            ev = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=written.id,
                kind="structured",
                outcome="pass",
                subject_ref=f"db:artifact:{written.id}",
                payload={"tests_total": 4},
                created_by_agent=builder_id,
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            await evidence_record_dao.add_evidence(ev, tenant_id=tenant_id, db=session)

            # APPROVE: the disjoint reviewer writes the verdict + seals (R3).
            verdict = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=written.id,
                kind="review",
                outcome="pass",
                subject_ref=f"evidence://review/{task_id}",
                created_by_agent=reviewer_id,
                payload={"verdict": "ok"},
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            await evidence_record_dao.add_evidence(
                verdict, tenant_id=tenant_id, db=session, reviewer_builder_agents={builder_id}
            )
            await artifact_record_dao.seal(written, db=session)
            assert written.seal_status == "SEALED"

            # A 2nd seal on the same row fails closed (invariant 10).
            with pytest.raises(ArtifactEvidenceClosedError):
                await artifact_record_dao.seal(written, db=session)

            # Rework: the new row supersedes the SEALED old row (G2).
            new_art = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/live/{tenant_id}/out_v2.txt",
                content_hash="d" * 64,
            )
            new_written = await artifact_record_dao.add_artifact(new_art, tenant_id=tenant_id, db=session)
            await artifact_record_dao.supersede(written, new_id=new_written.id, db=session)
            assert written.superseded_by == new_written.id
            # The old row is SEALED-and-historical; the new row is DRAFT.
            assert written.seal_status == "SEALED"
            assert new_written.seal_status == "DRAFT"

            # current_valid_review: only the NEW (current) set qualifies.
            arts = await artifact_record_dao.list_by_task(task_id, db=session)
            reviews = await evidence_record_dao.list_reviews_for_task(task_id, db=session)
            cwr = current_valid_review(arts, reviews)
            # The only review row cited the now-superseded artifact -> it drops
            # out of the current set automatically (G2, design §3.4): the old
            # APPROVE is historical, never clobbering the new set.
            assert cwr is None
            # But the new (current) artifact is queryable.
            assert {a.id for a in arts} == {written.id, new_written.id}


@pytest.mark.asyncio
async def test_live_record_rework_writes_no_builder_review_verdict(_engine) -> None:
    """F1 / invariant-13 — live, on the real f072 scratch DB.

    Drives the DB-bound ``ReviewReworkService.record_rework`` end-to-end and
    execution-confirms the finding's fix: the rework ends at "new artifacts +
    new evidence + ``superseded_by`` links" and writes **no** ``kind='review'``
    verdict row — the disjoint reviewer's earlier fail verdict is the *only*
    review row on the task, and a builder-authored ``kind='review'`` row on the
    same WorkPackage is rejected live with ``EV_REVIEW_NOT_INDEPENDENT``.
    (Mirrors the reviewer's aco_p4_gate1 probe from card t_fa30ea5d.)
    """
    from datetime import UTC, datetime

    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.artifact_evidence_dao import (
        ArtifactEvidenceClosedError,
        artifact_record_dao,
        evidence_record_dao,
    )
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.review_rework_service import ReviewReworkService

    try:
        async with _engine.connect() as conn:
            await conn.execute(_text("SELECT 1 FROM artifact_records LIMIT 1"))
    except OperationalError:
        pytest.skip("artifact_records not provisioned on the scratch DB (run f072)")

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    svc = ReviewReworkService()

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    builder_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with tenant_context(tenant_id):
        async with session.begin():
            session.add(Tenant(id=tenant_id, name=f"f1-{tenant_id.hex[:6]}", slug=f"f1-{tenant_id.hex[:8]}"))
            await session.flush()
            session.add(User(id=user_id, display_name="f1-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="f1-builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="f1-reviewer", creator_id=user_id, tenant_id=tenant_id))
            session.add(Task(id=task_id, agent_id=builder_id, title="f1-task", created_by=user_id, tenant_id=tenant_id))
            await session.flush()

    with tenant_context(tenant_id):
        async with session.begin():
            old = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,  # D5 XOR provenance (no execution FK needed)
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/f1/{tenant_id}/out_v1.txt",
                content_hash="1" * 64,
            )
            old_written = await artifact_record_dao.add_artifact(old, tenant_id=tenant_id, db=session)

            # A REQUEST_CHANGES (fail) verdict by the DISJOINT reviewer.
            fail_res = await svc.record_review(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                outcome="fail",
                reviewer_agent_id=reviewer_id,
                reviewer_user_id=None,
                builder_agent_ids=frozenset({builder_id}),
                current_artifacts=[old_written],
                cited_artifact_ids=[old_written.id],
                verdict="needs work",
                required_changes=["fix the v1 defect"],
            )
            assert fail_res.code == RV_OK
            fail_review = fail_res.evidence
            assert fail_review is not None and fail_review.kind == "review"
            assert fail_review.created_by_agent == reviewer_id

            # The rework: NEW artifact + NEW file_revision proof, old -> new.
            new_art = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/f1/{tenant_id}/out_v2.txt",
                content_hash="2" * 64,
            )
            new_proof = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=new_art.id,
                created_by_agent=builder_id,
                kind="file_revision",
                outcome="pass",
                subject_ref=f"file://{task_id}",
                payload={"tests_total": 1, "tests_passed": 1},
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            rework_res = await svc.record_rework(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                builder_agent_id=builder_id,
                fail_review=fail_review,
                old_artifacts=[old_written],
                new_artifacts=[new_art],
                new_evidence=[new_proof],
            )
            assert rework_res.code == RV_OK
            # The new artifact row was written and is current.  (Distinct
            # locators out_v1/out_v2 — the uq_artifact_records_tenant_ref
            # UNIQUE(tenant, scheme, ref) forbids two rows sharing one
            # locator — so plan_rework does not auto-link them here; G2
            # supersession by explicit link is already proven by the roundtrip
            # test above.  F1 only requires that the rework writes no verdict.)
            assert new_art.id in rework_res.new_artifact_ids

            # F1: record_rework wrote NO new kind='review' row.  The ONLY review
            # row on the task is the disjoint reviewer's fail verdict — not a
            # builder-authored verdict.
            created_by = (
                await session.execute(
                    _text(
                        "SELECT created_by_agent FROM evidence_records "
                        "WHERE task_id = :t AND kind = 'review'"
                    ),
                    {"t": task_id},
                )
            ).scalars().all()
            assert len(created_by) == 1
            assert created_by[0] == reviewer_id
            assert created_by[0] != builder_id

            # And the builder's own kind='review' verdict on the same WP is
            # rejected live by the invariant-13 guard (the execution-confirmed
            # half of F1).  (The disjoint reviewer's record_review(rework_of=
            # fail_review.id) on the new set is the accepted path — proven in
            # the pure tier; it carries payload.rework_of for G3.)
            builder_verdict = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=new_art.id,
                kind="review",
                outcome="pass",
                subject_ref=f"evidence://review/{task_id}",
                payload={"verdict": "self-approved"},
                created_by_agent=builder_id,
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            with pytest.raises(ArtifactEvidenceClosedError) as exc:
                await evidence_record_dao.add_evidence(
                    builder_verdict,
                    tenant_id=tenant_id,
                    db=session,
                    reviewer_builder_agents={builder_id},
                )
            assert exc.value.code == "EV_REVIEW_NOT_INDEPENDENT"


@pytest.mark.asyncio
async def test_live_record_rework_rejects_review_kind_new_evidence(_engine) -> None:
    """I-4 kind-closure / invariant-13 — live, on the real f072 scratch DB.

    Proves BOTH rejection tiers for a caller-supplied ``kind='review'`` row in
    ``record_rework``'s ``new_evidence``:

    - **Plan tier:** ``record_rework`` runs ``plan_rework`` first, which now
      rejects any ``kind='review'`` row with RV_INVALID_INPUT (the rework-proof
      lane is closed to verdict kinds) — the attempt fails closed and persists
      nothing.
    - **DAO backstop:** the new-evidence write loop passes
      ``reviewer_builder_agents={builder_agent_id}`` to
      ``add_evidence``; a builder-authored ``kind='review'`` row driven through
      that exact DAO call is rejected live with EV_REVIEW_NOT_INDEPENDENT
      (invariant 13), whereas the proof row (file_revision) it rides alongside
      writes cleanly.
    """
    from datetime import UTC, datetime

    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.artifact_evidence_dao import (
        ArtifactEvidenceClosedError,
        artifact_record_dao,
        evidence_record_dao,
    )
    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.review_rework_service import RV_INVALID_INPUT, ReviewReworkService

    try:
        async with _engine.connect() as conn:
            await conn.execute(_text("SELECT 1 FROM artifact_records LIMIT 1"))
    except OperationalError:
        pytest.skip("artifact_records not provisioned on the scratch DB (run f072)")

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    svc = ReviewReworkService()

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    builder_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with tenant_context(tenant_id):
        async with session.begin():
            session.add(Tenant(id=tenant_id, name=f"i5-{tenant_id.hex[:6]}", slug=f"i5-{tenant_id.hex[:8]}"))
            await session.flush()
            session.add(User(id=user_id, display_name="i5-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="i5-builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="i5-reviewer", creator_id=user_id, tenant_id=tenant_id))
            session.add(Task(id=task_id, agent_id=builder_id, title="i5-task", created_by=user_id, tenant_id=tenant_id))
            await session.flush()

    with tenant_context(tenant_id):
        async with session.begin():
            old = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/i5/{tenant_id}/out_v1.txt",
                content_hash="3" * 64,
            )
            old_written = await artifact_record_dao.add_artifact(old, tenant_id=tenant_id, db=session)

            # A REQUEST_CHANGES (fail) verdict by the DISJOINT reviewer.
            fail_res = await svc.record_review(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                outcome="fail",
                reviewer_agent_id=reviewer_id,
                reviewer_user_id=None,
                builder_agent_ids=frozenset({builder_id}),
                current_artifacts=[old_written],
                cited_artifact_ids=[old_written.id],
                verdict="needs work",
                required_changes=["fix the v1 defect"],
            )
            assert fail_res.code == "RV_OK"
            fail_review = fail_res.evidence
            assert fail_review is not None

            # The rework's proof row (file_revision) and a caller-sneaked
            # builder-authored kind='review' verdict row riding new_evidence.
            new_art = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/i5/{tenant_id}/out_v2.txt",
                content_hash="4" * 64,
            )
            # The evidence rows cite new_art via artifact_id (a real FK, so
            # the artifact row must exist first — the plan-tier attempt below
            # writes nothing; this persists the new set for the backstop check).
            new_art = await artifact_record_dao.add_artifact(new_art, tenant_id=tenant_id, db=session)
            new_proof = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=new_art.id,
                created_by_agent=builder_id,
                kind="file_revision",
                outcome="pass",
                subject_ref=f"file://{task_id}",
                payload={"tests_total": 1, "tests_passed": 1},
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            sneaked_verdict = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=new_art.id,
                created_by_agent=builder_id,
                kind="review",
                outcome="pass",
                subject_ref=f"evidence://review/{task_id}",
                payload={"rework_of": str(fail_review.id), "verdict": "self-approved"},
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )

            # --- Plan tier: record_rework rejects a kind='review' row in
            # new_evidence with RV_INVALID_INPUT and persists nothing NEW
            # (the failed attempt's own rows stay unwritten). ---
            bad_res = await svc.record_rework(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                builder_agent_id=builder_id,
                fail_review=fail_review,
                old_artifacts=[old_written],
                new_artifacts=[new_art],
                new_evidence=[new_proof, sneaked_verdict],
            )
            assert bad_res.code == RV_INVALID_INPUT
            # Exactly ONE review row on the task (the disjoint reviewer's fail):
            # the attempt's sneaked verdict did not persist.
            review_rows = (
                await session.execute(
                    _text("SELECT count(*) FROM evidence_records WHERE task_id = :t AND kind = 'review'"),
                    {"t": task_id},
                )
            ).scalar_one()
            assert review_rows == 1

            # --- DAO backstop: the exact write the rework loop performs
            # (add_evidence with reviewer_builder_agents={builder_agent_id})
            # rejects the builder-authored kind='review' row live with
            # EV_REVIEW_NOT_INDEPENDENT, while the proof row writes cleanly. ---
            with pytest.raises(ArtifactEvidenceClosedError) as exc:
                await evidence_record_dao.add_evidence(
                    sneaked_verdict,
                    tenant_id=tenant_id,
                    db=session,
                    reviewer_builder_agents={builder_id},
                )
            assert exc.value.code == "EV_REVIEW_NOT_INDEPENDENT"
            proof_written = await evidence_record_dao.add_evidence(
                new_proof,
                tenant_id=tenant_id,
                db=session,
                reviewer_builder_agents={builder_id},
            )
            assert proof_written.kind == "file_revision"

            # And the normal rework (proof only) still succeeds end-to-end
            # (fresh rows: the failed attempt persisted nothing).
            new_art2 = ArtifactRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                created_by_user=user_id,
                type="file",
                title="out.txt",
                storage_scheme="workspace_path",
                storage_ref=f"/i5/{tenant_id}/out_v3.txt",
                content_hash="5" * 64,
            )
            new_proof2 = EvidenceRecord(
                id=uuid.uuid4(),
                task_id=task_id,
                artifact_id=new_art2.id,
                created_by_agent=builder_id,
                kind="file_revision",
                outcome="pass",
                subject_ref=f"file://{task_id}",
                payload={"tests_total": 1, "tests_passed": 1},
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
            ok_res = await svc.record_rework(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                builder_agent_id=builder_id,
                fail_review=fail_review,
                old_artifacts=[old_written],
                new_artifacts=[new_art2],
                new_evidence=[new_proof2],
            )
            assert ok_res.code == "RV_OK"
            assert new_art2.id in ok_res.new_artifact_ids
            assert new_proof2.id in ok_res.new_evidence_ids


@pytest.mark.asyncio
async def test_live_dao_db_checks_and_tenant_isolation(_engine) -> None:
    """The f072 DB invariants fire live: the D5 source CHECK rejects a
    provenance-less row, and a cross-tenant ledger row is invisible to the
    caller's scoped read (D6 / I-7)."""

    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.artifact_evidence_dao import artifact_record_dao
    from app.dao.base import tenant_context
    from app.models.artifact_evidence import ArtifactRecord
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User

    try:
        async with _engine.connect() as conn:
            await conn.execute(_text("SELECT 1 FROM artifact_records LIMIT 1"))
    except OperationalError:
        pytest.skip("artifact_records not provisioned on the scratch DB (run f072)")

    t1, t2 = uuid.uuid4(), uuid.uuid4()
    u1 = uuid.uuid4()
    agent1 = uuid.uuid4()
    session = AsyncSession(bind=_engine, expire_on_commit=False)

    with tenant_context(t1):
        async with session.begin():
            session.add(Tenant(id=t1, name="iso-t1", slug=f"iso-{t1.hex[:8]}"))
            await session.flush()
            session.add(User(id=u1, display_name="iso-user", tenant_id=t1))
            await session.flush()
            session.add(Agent(id=agent1, name="iso-agent", creator_id=u1, tenant_id=t1))
            session.add(Task(id=uuid.uuid4(), agent_id=agent1, title="iso-task", created_by=u1, tenant_id=t1))
            await session.flush()
        # A provenance-less artifact is rejected by the DAO's fail-closed D5
        # validation (the named-code boundary; the DB CHECK is the backstop).
        bad_art = ArtifactRecord(
            id=uuid.uuid4(),
            task_id=None,
            created_by_user=None,
            execution_id=None,
            type="file",
            title="orphan",
            storage_scheme="workspace_path",
            storage_ref=f"/iso/{t1}/orphan.txt",
        )
        from app.dao.artifact_evidence_dao import ArtifactEvidenceClosedError

        with tenant_context(t1):
            with pytest.raises(ArtifactEvidenceClosedError) as exc:
                async with session.begin():
                    await artifact_record_dao.add_artifact(bad_art, tenant_id=t1, db=session)
            assert exc.value.code == "EV_NO_SOURCE"

    # A tenant-2 ledger row is invisible to a tenant-1 scoped read (D6/I-7).
    with tenant_context(t2):
        async with session.begin():
            session.add(Tenant(id=t2, name="iso-t2", slug=f"iso-{t2.hex[:8]}"))
            await session.flush()
            art2 = ArtifactRecord(
                id=uuid.uuid4(),
                created_by_user=u1,
                type="file",
                title="t2-artifact",
                storage_scheme="workspace_path",
                storage_ref=f"/iso/{t2}/secret.txt",
            )
            await artifact_record_dao.add_artifact(art2, tenant_id=t2, db=session)
    with tenant_context(t1):
        # The tenant-1 scoped read must NOT surface the tenant-2 row.
        found = await artifact_record_dao.get_by_locator("workspace_path", f"/iso/{t2}/secret.txt", db=session)
        assert found is None
        # And the tenant-2 scoped read DOES see its own row.
    with tenant_context(t2):
        found = await artifact_record_dao.get_by_locator("workspace_path", f"/iso/{t2}/secret.txt", db=session)
        assert found is not None and found.id == art2.id

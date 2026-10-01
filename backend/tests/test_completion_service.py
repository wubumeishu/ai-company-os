"""The Phase 4 Completion lane tests — two tiers (card t_e399386f).

- **DB-free tier**: the pure CT / CW / CP predicates over in-memory rows —
  every ``CP_*`` code path (cycle → ``CP_EVAL_ERROR``, open slot, tenant
  mismatch, unexpected outcome, ...), the C5 chain payload shape, and the
  ``CD_*`` delivery-contract closed set.  No Postgres.
- **live f072 tier** (skipped when no reachable scratch Postgres): the C5
  chain-append proof against the real f072 DDL (two evaluations of the same
  task both persist; the 2nd row's ``payload.reverify_of`` cites the 1st —
  the live-DB adjudication of the invariant-5 partial-unique index),
  ``COMPLETED`` published exactly once on a ``CP_OK`` project + idempotent
  re-call, the ``CP_EVAL_ERROR`` path (cycle → no write), and the
  cross-tenant call failing ``CP_TENANT_MISMATCH`` before any read/write.

Provision the scratch DB once (the D3 lesson: the real alembic chain,
main-tree venv, PYTHONPATH = worktree backend — never os.getcwd()-based
discovery)::

    "I:/project/AI Company OS/backend/.venv/Scripts/python.exe" _provision_f072.py
    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t_e399386f_f072 \
        pytest tests/test_completion_service.py
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

import app.models.agent
import app.models.agent_run
import app.models.agent_tool_execution
import app.models.analysis
import app.models.planning
import app.models.project
import app.models.task
import app.models.tenant
import app.models.user  # noqa: F401
from app.services.completion_service import (
    CD_DESTINATION_INVALID,
    CD_EVAL_ERROR,
    CD_NO_SEALED_APPROVED,
    CD_NOT_COMPLETED,
    CD_OK,
    CD_RESULT_CODES,
    CP_DEPS_NOT_DONE,
    CP_EVAL_ERROR,
    CP_INCONCLUSIVE_REVIEW,
    CP_NO_APPROVING_REVIEW,
    CP_NO_WORK,
    CP_NOT_SEALED,
    CP_OK,
    CP_OPEN_REQUEST_CHANGES,
    CP_OPEN_SLOT,
    CP_RESULT_CODES,
    CP_REVIEW_NOT_INDEPENDENT,
    CP_TENANT_MISMATCH,
    DELIVERY_DESTINATION_KINDS,
    CompletionEvaluationError,
    EvaluationActor,
    EvaluationResult,
    ProjectEvalInput,
    TaskEvalContext,
    TaskEvalInput,
    WorkPackageEvalInput,
    completion_service,
    delivery_decision_code,
    delivery_destination_code,
    evaluate_cp,
    evaluate_ct,
    evaluate_cw,
    project_decision,
    task_decision,
    wp_decision,
)

# ---------------------------------------------------------------------------
# In-memory stand-in rows (DB-free tier): they duck-type the ledger / task
# models the pure core reads via ``getattr`` — no isinstance checks.
# ---------------------------------------------------------------------------
TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
BUILDER = uuid.uuid4()
REVIEWER = uuid.uuid4()
BUILDERS = frozenset({BUILDER})


def _task(tid: uuid.UUID, tenant: Any = None, ttype: str = "todo", status: str = "done") -> TaskEvalInput:
    return TaskEvalInput(task=cast(Any, SimpleNamespace(id=tid, type=ttype, tenant_id=tenant, status=status)))


def _artifact(aid: uuid.UUID, seal: str = "SEALED", superseded_by: Any = None, tenant: Any = None) -> Any:
    return SimpleNamespace(id=aid, seal_status=seal, superseded_by=superseded_by, tenant_id=tenant)


def _review(rid: uuid.UUID, outcome: str, art_id: Any, agent: Any = REVIEWER, tenant: Any = None, when: int = 1) -> Any:
    return SimpleNamespace(
        id=rid,
        kind="review",
        outcome=outcome,
        artifact_id=art_id,
        created_by_agent=agent,
        tenant_id=tenant,
        created_at=when,
    )


def _ok_bundle(tid: uuid.UUID, builders: Any = BUILDERS) -> TaskEvalInput:
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id)
    return TaskEvalInput(
        task=_task(tid).task,
        artifacts=[art],
        reviews=[rev],
        builder_agent_ids=builders,
    )


def _ctx(tasks: dict, deps: dict | None = None) -> TaskEvalContext:
    return TaskEvalContext(tasks=tasks, direct_deps=deps or {})


def _wp(wp_id: uuid.UUID, slots: list, wp_task_ids: list, bundles: dict, deps: dict | None = None, tenant: Any = None) -> WorkPackageEvalInput:
    return WorkPackageEvalInput(
        work_package=cast(Any, SimpleNamespace(id=wp_id, tenant_id=tenant)),
        slots=slots,
        wp_task_ids=wp_task_ids,
        task_bundles=bundles,
        direct_deps=deps or {},
    )


def _slot(task_id: Any) -> Any:
    return SimpleNamespace(task_id=task_id, work_package_id=uuid.uuid4())


def _proj(pid: uuid.UUID, tenant: Any = None, status: str = "EXECUTING") -> Any:
    return SimpleNamespace(id=pid, tenant_id=tenant, status=status)


# ---------------------------------------------------------------------------
# Closed code sets (C6, design §3.5 / §6.2).
# ---------------------------------------------------------------------------
def test_cp_code_set_is_exactly_closed() -> None:
    assert CP_RESULT_CODES == frozenset(
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


def test_cd_code_set_is_exactly_closed() -> None:
    assert CD_RESULT_CODES == frozenset({CD_OK, CD_NOT_COMPLETED, CD_NO_SEALED_APPROVED, CD_DESTINATION_INVALID, CD_EVAL_ERROR})
    assert set(DELIVERY_DESTINATION_KINDS) == {"channel", "published_page", "project_record"}


# ---------------------------------------------------------------------------
# CT — every CP_* path.
# ---------------------------------------------------------------------------
def test_ct_ok_with_healthy_transitive_dep() -> None:
    tid, dep = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx({tid: _ok_bundle(tid), dep: _ok_bundle(dep)}, {tid: [dep]})
    result = evaluate_ct(ctx, tid)
    assert result.code == CP_OK
    assert result.cited["review_id"] is not None
    assert result.cited["dep_task_ids"] == [str(dep)]


def test_ct_no_work_when_no_current_artifact() -> None:
    tid = uuid.uuid4()
    assert evaluate_ct(_ctx({tid: _task(tid)}), tid).code == CP_NO_WORK


def test_ct_superseded_only_artifacts_are_no_work() -> None:
    tid = uuid.uuid4()
    stale = _artifact(uuid.uuid4(), superseded_by=uuid.uuid4())
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[stale])
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_NO_WORK


def test_ct_not_sealed_when_current_artifact_is_draft() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4(), seal="DRAFT")
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art])
    result = evaluate_ct(_ctx({tid: inp}), tid)
    assert result.code == CP_NOT_SEALED
    assert result.cited["artifact_ids"] == [str(art.id)]


def test_ct_no_approving_review_when_sealed_but_unreviewed() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[])
    result = evaluate_ct(_ctx({tid: inp}), tid)
    assert result.code == CP_NO_APPROVING_REVIEW
    assert result.cited["review_id"] is None


def test_ct_stale_review_over_superseded_set_is_not_the_current_valid_one() -> None:
    tid = uuid.uuid4()
    old = _artifact(uuid.uuid4(), superseded_by=uuid.uuid4())
    new = _artifact(uuid.uuid4())
    old_review = _review(uuid.uuid4(), "pass", old.id, when=1)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[old, new], reviews=[old_review])
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_NO_APPROVING_REVIEW


def test_ct_open_request_changes() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "fail", art.id)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev])
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_OPEN_REQUEST_CHANGES


def test_ct_inconclusive_review_never_completes() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "inconclusive", art.id)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev])
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_INCONCLUSIVE_REVIEW


def test_ct_review_not_independent_when_reviewer_is_a_builder() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id, agent=BUILDER)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev], builder_agent_ids=BUILDERS)
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_REVIEW_NOT_INDEPENDENT


def test_ct_user_reviewer_row_is_vacuously_independent() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id, agent=None)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev], builder_agent_ids=BUILDERS)
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_OK


def test_ct_unknown_outcome_fails_closed() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "weird", art.id)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev])
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_EVAL_ERROR


def test_ct_builder_set_unknown_with_agent_reviewer_fails_closed() -> None:
    tid = uuid.uuid4()
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id)
    inp = TaskEvalInput(task=_task(tid).task, artifacts=[art], reviews=[rev], builder_agent_ids=None)
    assert evaluate_ct(_ctx({tid: inp}), tid).code == CP_EVAL_ERROR


def test_ct_cycle_is_eval_error_named_at_the_dep() -> None:
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx({c1: _ok_bundle(c1), c2: _ok_bundle(c2)}, {c1: [c2], c2: [c1]})
    result = evaluate_ct(ctx, c1)
    assert result.code == CP_EVAL_ERROR
    assert "failing_dep" in result.cited or "cycle_at" in result.cited


def test_ct_missing_dep_bundle_fails_closed() -> None:
    tid, ghost = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx({tid: _ok_bundle(tid)}, {tid: [ghost]})
    assert evaluate_ct(ctx, tid).code == CP_EVAL_ERROR


def test_ct_deps_not_done_when_a_dep_lacks_artifacts() -> None:
    tid, dep = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx({tid: _ok_bundle(tid), dep: _task(dep)}, {tid: [dep]})
    result = evaluate_ct(ctx, tid)
    assert result.code == CP_DEPS_NOT_DONE
    assert result.cited["failing_dep"] == str(dep)


def test_ct_out_of_scope_supervision_task_fails_closed() -> None:
    tid = uuid.uuid4()
    inp = _task(tid, ttype="supervision")
    assert evaluate_ct(_ctx({tid: TaskEvalInput(task=inp.task)}), tid).code == CP_EVAL_ERROR


def test_ct_tenant_mismatch_on_the_task_row() -> None:
    tid = uuid.uuid4()
    inp = _task(tid, tenant=OTHER_TENANT)
    result = evaluate_ct(_ctx({tid: TaskEvalInput(task=inp.task)}), tid, tenant_id=TENANT)
    assert result.code == CP_TENANT_MISMATCH


def test_ct_never_reads_task_status_ra() -> None:
    tid, ok_tid = uuid.uuid4(), uuid.uuid4()
    # A Run fact of 'failed' with a perfect ledger still completes.
    task_failed = _task(ok_tid, status="failed")
    art = _artifact(uuid.uuid4())
    good = TaskEvalInput(
        task=task_failed.task,
        artifacts=[art],
        reviews=[_review(uuid.uuid4(), "pass", art.id)],
        builder_agent_ids=BUILDERS,
    )
    assert evaluate_ct(_ctx({ok_tid: good}), ok_tid).code == CP_OK
    # A Run fact of 'done' with an empty ledger still fails CP_NO_WORK.
    task_done = _task(tid, status="done")
    assert evaluate_ct(_ctx({tid: TaskEvalInput(task=task_done.task)}), tid).code == CP_NO_WORK


# ---------------------------------------------------------------------------
# CW — the §4 code stack.
# ---------------------------------------------------------------------------
def test_cw_no_work_when_no_slot_materialized() -> None:
    wp = uuid.uuid4()
    inp = _wp(wp, [_slot(None)], [], {})
    assert evaluate_cw(inp).code == CP_NO_WORK


def test_cw_propagates_the_failing_task_code_named() -> None:
    tid, wp = uuid.uuid4(), uuid.uuid4()
    art = _artifact(uuid.uuid4(), seal="DRAFT")
    bundles = {tid: TaskEvalInput(task=_task(tid).task, artifacts=[art])}
    inp = _wp(wp, [_slot(tid)], [tid], bundles)
    result = evaluate_cw(inp)
    assert result.code == CP_NOT_SEALED
    assert result.cited["failing_task_id"] == str(tid)


def test_cw_open_slot_fails_closed() -> None:
    tid, wp = uuid.uuid4(), uuid.uuid4()
    bundles = {tid: _ok_bundle(tid)}
    inp = _wp(wp, [_slot(tid), _slot(None)], [tid], bundles)
    result = evaluate_cw(inp)
    assert result.code == CP_OPEN_SLOT
    assert result.cited["wp_id"] == str(wp)


def test_cw_tenant_mismatch_on_the_wp_row() -> None:
    wp = uuid.uuid4()
    tid = uuid.uuid4()
    inp = _wp(wp, [_slot(tid)], [tid], {tid: _ok_bundle(tid)}, tenant=OTHER_TENANT)
    assert evaluate_cw(inp, tenant_id=TENANT).code == CP_TENANT_MISMATCH


def test_cw_ok_all_materialized_and_ct_holds() -> None:
    t1, t2, wp = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bundles = {t1: _ok_bundle(t1), t2: _ok_bundle(t2)}
    inp = _wp(wp, [_slot(t1), _slot(t2)], [t1, t2], bundles, {t2: [t1]})
    result = evaluate_cw(inp)
    assert result.code == CP_OK
    assert result.cited["wp_id"] == str(wp)


# ---------------------------------------------------------------------------
# CP — the §5 term stack.
# ---------------------------------------------------------------------------
def test_cp_no_work_when_no_in_scope_tasks() -> None:
    assert evaluate_cp(ProjectEvalInput(project=_proj(uuid.uuid4()))).code == CP_NO_WORK


def test_cp_propagates_the_failing_in_scope_task_named() -> None:
    pid, tid = uuid.uuid4(), uuid.uuid4()
    art = _artifact(uuid.uuid4(), seal="DRAFT")
    bundles = {tid: TaskEvalInput(task=_task(tid).task, artifacts=[art])}
    inp = ProjectEvalInput(project=_proj(pid), in_scope_task_ids=[tid], task_bundles=bundles)
    result = evaluate_cp(inp)
    assert result.code == CP_NOT_SEALED
    assert result.cited["failing_task_id"] == str(tid)
    assert result.cited["project_id"] == str(pid)


def test_cp_delivery_wp_open_slot_fails_closed() -> None:
    pid, wp, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_bundles = {tid: _ok_bundle(tid)}
    good_task = uuid.uuid4()
    in_scope_bundles = dict(task_bundles)
    in_scope_bundles[good_task] = _ok_bundle(good_task)
    bad_wp = _wp(wp, [_slot(good_task), _slot(None)], [good_task], in_scope_bundles)
    inp = ProjectEvalInput(
        project=_proj(pid),
        in_scope_task_ids=[good_task],
        task_bundles=in_scope_bundles,
        delivery_packages=[bad_wp],
    )
    assert evaluate_cp(inp).code == CP_OPEN_SLOT


def test_cp_empty_delivery_set_satisfies_the_term() -> None:
    pid, tid = uuid.uuid4(), uuid.uuid4()
    bundles = {tid: _ok_bundle(tid)}
    inp = ProjectEvalInput(project=_proj(pid), in_scope_task_ids=[tid], task_bundles=bundles)
    assert evaluate_cp(inp).code == CP_OK


def test_cp_delivery_wps_all_ok() -> None:
    pid, wp, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bundles = {tid: _ok_bundle(tid)}
    good_wp = _wp(wp, [_slot(tid)], [tid], bundles)
    inp = ProjectEvalInput(
        project=_proj(pid), in_scope_task_ids=[tid], task_bundles=bundles, delivery_packages=[good_wp]
    )
    assert evaluate_cp(inp).code == CP_OK


def test_cp_tenant_mismatch_on_the_project_row() -> None:
    pid, tid = uuid.uuid4(), uuid.uuid4()
    bundles = {tid: _ok_bundle(tid)}
    inp = ProjectEvalInput(
        project=_proj(pid, tenant=OTHER_TENANT), in_scope_task_ids=[tid], task_bundles=bundles
    )
    assert evaluate_cp(inp, tenant_id=TENANT).code == CP_TENANT_MISMATCH


# ---------------------------------------------------------------------------
# C5 chain payload shape (the design-conflict adjudication, card t_e399386f).
# ---------------------------------------------------------------------------
def test_c5_first_decision_payload_has_no_reverify_of() -> None:
    tid = uuid.uuid4()
    decision = task_decision(tid, EvaluationResult(CP_OK, {"review_id": "x"}))
    payload = decision.payload()
    assert payload["decision"] == "completion_evaluated"
    assert payload["outcome"] == CP_OK
    assert payload["scope"] == "task"
    assert payload["subject_ref"] == f"task://{tid}"
    assert "reverify_of" not in payload
    assert decision.reverify_of is None


def test_c5_chained_decision_payload_cites_the_previous_row() -> None:
    tid, prev = uuid.uuid4(), uuid.uuid4()
    decision = task_decision(tid, EvaluationResult(CP_OK, {}), reverify_of=prev)
    payload = decision.payload()
    assert payload["reverify_of"] == str(prev)
    # The WP / project shapes mirror the task one.
    wp_prev = wp_decision(tid, EvaluationResult(CP_OPEN_SLOT, {}), reverify_of=prev).payload()
    assert wp_prev["reverify_of"] == str(prev)
    assert wp_prev["scope"] == "wp"
    proj_prev = project_decision(tid, EvaluationResult(CP_OK, {}), reverify_of=prev).payload()
    assert proj_prev["reverify_of"] == str(prev)
    assert proj_prev["scope"] == "project"


def test_c5_subject_namespaces() -> None:
    tid, wpid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert task_decision(tid, EvaluationResult(CP_OK, {})).subject_ref == f"task://{tid}"
    assert wp_decision(wpid, EvaluationResult(CP_OK, {})).subject_ref == f"wp://{wpid}"
    assert project_decision(pid, EvaluationResult(CP_OK, {})).subject_ref == f"project://{pid}"


# ---------------------------------------------------------------------------
# The CD_* delivery contract (design §6.2 / C-D1..C-D3).
# ---------------------------------------------------------------------------
def test_delivery_destination_closed_set() -> None:
    for kind in DELIVERY_DESTINATION_KINDS:
        assert delivery_destination_code(kind) == CD_OK
    assert delivery_destination_code("external_platform") == CD_DESTINATION_INVALID
    assert delivery_destination_code(None) == CD_DESTINATION_INVALID


def test_delivery_decision_stack() -> None:
    # Gate: the scope must be CP_OK.
    assert delivery_decision_code(scope_code=CP_NOT_SEALED, destination_kind="channel", sealed_and_approved=True) == CD_NOT_COMPLETED
    assert delivery_decision_code(scope_code=CP_OK, destination_kind="channel", sealed_and_approved=True) == CD_OK
    # Destination: closed V1 vocabulary.
    assert delivery_decision_code(scope_code=CP_OK, destination_kind="external", sealed_and_approved=True) == CD_DESTINATION_INVALID
    # Cited set: SEALED + current-valid approving.
    assert delivery_decision_code(scope_code=CP_OK, destination_kind="channel", sealed_and_approved=False) == CD_NO_SEALED_APPROVED
    # Unknown scope code is the catch-all, never CD_OK.
    assert delivery_decision_code(scope_code="bogus", destination_kind="channel", sealed_and_approved=True) == CD_EVAL_ERROR


def test_evaluation_actor_is_exactly_one_source() -> None:
    EvaluationActor(agent_id=BUILDER)
    EvaluationActor(user_id=TENANT)
    with pytest.raises(CompletionEvaluationError):
        EvaluationActor()
    with pytest.raises(CompletionEvaluationError):
        EvaluationActor(agent_id=BUILDER, user_id=TENANT)


# ---------------------------------------------------------------------------
# Live f072 tier (skipped when no reachable scratch Postgres; each test also
# skips when the f072 tables are absent so the module stays re-runnable).
# ---------------------------------------------------------------------------
@pytest.fixture
def _db_available() -> None:
    """Skip the whole live tier when Postgres is unreachable on this host."""
    from app.config import get_settings

    settings = get_settings()
    url = settings.DATABASE_URL
    if not url:
        pytest.skip("DATABASE_URL not set; skipping the live tier")
    import os

    if os.environ.get("ACO_SKIP_LIVE_DB") == "1":
        pytest.skip("ACO_SKIP_LIVE_DB=1")


@pytest.fixture
def _engine(_db_available) -> object:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    yield engine
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine.dispose())
        finally:
            loop.close()
    except Exception:  # noqa: BLE001, S110 - best-effort teardown; never fail the suite here
        pass


async def _require_f072_tables(conn) -> None:
    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError

    try:
        await conn.execute(_text("SELECT 1 FROM artifact_records LIMIT 1"))
        await conn.execute(_text("SELECT 1 FROM evidence_records LIMIT 1"))
    except OperationalError:
        pytest.skip("f072 ledger tables not provisioned on the scratch DB (run _provision_f072.py)")


async def _seed_completed_task(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    builder_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One task with a SEALED artifact + a disjoint-agent 'pass' review — the
    full CP_OK ledger proof.  Returns the task id."""
    from app.dao.artifact_evidence_dao import artifact_record_dao, evidence_record_dao
    from app.models.artifact_evidence import ArtifactRecord, EvidenceRecord
    from app.models.task import Task

    task_id = task_id or uuid.uuid4()
    task = Task(
        id=task_id,
        agent_id=builder_id,
        title="live-completion-task",
        created_by=user_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    session.add(task)
    await session.flush()
    art = ArtifactRecord(
        id=uuid.uuid4(),
        task_id=task_id,
        created_by_user=user_id,
        type="file",
        title="out.txt",
        storage_scheme="workspace_path",
        # storage_ref is unique per task: the ledger's
        # uq_artifact_records_tenant_ref dedups (tenant, scheme, ref), so two
        # tasks in one tenant must not share a locator.
        storage_ref=f"/live/{tenant_id.hex[:8]}/{task_id}/out.txt",
        content_hash="e" * 64,
    )
    written = await artifact_record_dao.add_artifact(art, tenant_id=tenant_id, db=session)
    await artifact_record_dao.seal(written, db=session)
    rev = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        artifact_id=written.id,
        kind="review",
        outcome="pass",
        subject_ref=f"evidence://review/{task_id}",
        payload={"verdict": "ok"},
        created_by_agent=reviewer_id,
    )
    await evidence_record_dao.add_evidence(rev, tenant_id=tenant_id, db=session, reviewer_builder_agents={builder_id})
    return task_id


async def _structured_decision_rows(session, tenant_id: uuid.UUID, subject_ref: str) -> list:
    from app.models.artifact_evidence import EvidenceRecord

    stmt = select(EvidenceRecord).where(
        EvidenceRecord.kind == "structured",
        EvidenceRecord.subject_ref == subject_ref,
        EvidenceRecord.tenant_id == tenant_id,
    )
    return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_live_c5_chain_appends_twice_and_chains(_engine) -> None:
    """Two evaluations of the same task both persist against the real f072
    DDL; the 2nd row's ``payload.reverify_of`` cites the 1st — the live-DB
    proof that the C5 chain is excluded from the invariant-5 partial-unique
    index ``uq_evidence_records_reverify``."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    async with _engine.connect() as conn:
        await _require_f072_tables(conn)

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            session.add(Tenant(id=tenant_id, name=f"live-{tenant_id.hex[:6]}", slug=f"live-{tenant_id.hex[:8]}"))
            await session.flush()
            session.add(User(id=user_id, display_name="live-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="reviewer", creator_id=user_id, tenant_id=tenant_id))
            task_id = await _seed_completed_task(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    with tenant_context(tenant_id):
        async with session.begin():
            r1 = await completion_service.evaluate_task(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                actor=actor,
                builder_agent_ids=builders,
            )
        async with session.begin():
            r2 = await completion_service.evaluate_task(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                actor=actor,
                builder_agent_ids=builders,
            )

    assert r1.code == CP_OK, r1
    assert r2.code == CP_OK, r2
    assert r1.decision is not None and r2.decision is not None
    rows = await _structured_decision_rows(session, tenant_id, f"task://{task_id}")
    assert len(rows) == 2, f"the C5 chain-append must persist BOTH rows: {len(rows)}"
    first = next(r for r in rows if "reverify_of" not in (r.payload or {}))
    second = next(r for r in rows if "reverify_of" in (r.payload or {}))
    assert second.payload["reverify_of"] == str(first.id)
    assert first.payload["outcome"] == CP_OK
    assert second.payload["outcome"] == CP_OK
    # The re-call's row cites the first — the LATEST-row read is the chain
    # tail (design §9 staleness note).
    latest = await completion_service._latest_decision(session, f"task://{task_id}")
    assert latest is not None and latest.id == second.id
    await session.close()


@pytest.mark.asyncio
async def test_live_project_completed_exactly_once_then_idempotent(_engine) -> None:
    """A fresh CP_OK project in an executable status publishes COMPLETED
    exactly once; the re-call recomputes CP_OK but does NOT rewrite (the
    terminal status is outside PROJECT_EXECUTABLE_STATUSES)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.dao.project_intake_dao import project_dao
    from app.models.agent import Agent
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.models.user import User

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            session.add(Tenant(id=tenant_id, name=f"live-{tenant_id.hex[:6]}", slug=f"live-{tenant_id.hex[:8]}"))
            await session.flush()
            session.add(User(id=user_id, display_name="live-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="reviewer", creator_id=user_id, tenant_id=tenant_id))
            session.add(Project(id=project_id, name="live-project", created_by=user_id, tenant_id=tenant_id, status="EXECUTING"))
            await session.flush()
            await _seed_completed_task(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    with tenant_context(tenant_id):
        async with session.begin():
            r1 = await completion_service.evaluate_project(
                session,
                project_id=project_id,
                tenant_id=tenant_id,
                actor=actor,
                builder_agent_ids=builders,
            )
            fresh1 = await project_dao.get_scoped(project_id, db=session)
            assert fresh1 is not None
            first_stamp = fresh1.status_changed_at
        assert r1.code == CP_OK, r1
        assert r1.project_completed is True
        assert r1.project_status == "COMPLETED"
        assert r1.decision is not None
        assert r1.decision.payload is not None
        assert r1.decision.payload["outcome"] == CP_OK
        first_row_id = r1.decision.id

        async with session.begin():
            r2 = await completion_service.evaluate_project(
                session,
                project_id=project_id,
                tenant_id=tenant_id,
                actor=actor,
                builder_agent_ids=builders,
            )
            fresh = await project_dao.get_scoped(project_id, db=session)
            assert fresh is not None
    assert r2.code == CP_OK, r2
    # Idempotent by construction (design §9): no 2nd COMPLETED write.
    assert r2.project_completed is False
    assert r2.project_status == "COMPLETED"
    assert fresh.status == "COMPLETED"
    assert fresh.status_changed_at == first_stamp, "the re-call must not rewrite the project row"
    # The C5 chain still appends (the decision row is the audit trace).
    assert r2.decision is not None
    assert r2.decision.payload is not None
    assert r2.decision.payload["reverify_of"] == str(first_row_id)
    await session.close()


@pytest.mark.asyncio
async def test_live_cp_eval_error_cycle_publishes_nothing(_engine) -> None:
    """A cyclic in-scope dependency graph fails CP_EVAL_ERROR (the pure core
    detects the cycle) and publishes NO COMPLETED write — the project stays
    in its executable status."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.dao.project_intake_dao import project_dao
    from app.models.agent import Agent
    from app.models.project import Project
    from app.models.task import TaskDependency
    from app.models.tenant import Tenant
    from app.models.user import User

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id, t1, t2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            session.add(Tenant(id=tenant_id, name=f"live-{tenant_id.hex[:6]}", slug=f"live-{tenant_id.hex[:8]}"))
            await session.flush()
            session.add(User(id=user_id, display_name="live-user", tenant_id=tenant_id))
            await session.flush()
            session.add(Agent(id=builder_id, name="builder", creator_id=user_id, tenant_id=tenant_id))
            session.add(Agent(id=reviewer_id, name="reviewer", creator_id=user_id, tenant_id=tenant_id))
            session.add(Project(id=project_id, name="live-project", created_by=user_id, tenant_id=tenant_id, status="EXECUTING"))
            await session.flush()
            await _seed_completed_task(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
                task_id=t1,
            )
            await _seed_completed_task(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
                task_id=t2,
            )
            # The cycle: t1 depends on t2 AND t2 depends on t1.
            session.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant_id, task_id=t1, depends_on_task_id=t2))
            session.add(TaskDependency(id=uuid.uuid4(), tenant_id=tenant_id, task_id=t2, depends_on_task_id=t1))

    actor = EvaluationActor(user_id=user_id)
    with tenant_context(tenant_id):
        async with session.begin():
            r = await completion_service.evaluate_project(
                session, project_id=project_id, tenant_id=tenant_id, actor=actor
            )
            fresh = await project_dao.get_scoped(project_id, db=session)
            assert fresh is not None
            fresh_status = fresh.status
    assert r.code == CP_EVAL_ERROR, r
    assert r.project_completed is False
    assert r.project_status == "EXECUTING"
    assert fresh_status == "EXECUTING", "CP_EVAL_ERROR must never publish COMPLETED"
    assert r.decision is not None
    assert r.decision.payload is not None
    assert r.decision.payload["outcome"] == CP_EVAL_ERROR
    await session.close()


@pytest.mark.asyncio
async def test_live_cross_tenant_fails_before_any_read_or_write(_engine) -> None:
    """A cross-tenant evaluation of a foreign task fails CP_TENANT_MISMATCH
    before any dependent read and writes NO C5 row in the caller's tenant
    (a row for an unseen subject would be meaningless)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    owner_tenant, other_tenant = uuid.uuid4(), uuid.uuid4()
    owner_user = uuid.uuid4()
    builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4()
    other_user = uuid.uuid4()

    with tenant_context(owner_tenant):
        async with session.begin():
            session.add(Tenant(id=owner_tenant, name=f"live-{owner_tenant.hex[:6]}", slug=f"live-{owner_tenant.hex[:8]}"))
            await session.flush()
            session.add(User(id=owner_user, display_name="owner-user", tenant_id=owner_tenant))
            await session.flush()
            session.add(Agent(id=builder_id, name="builder", creator_id=owner_user, tenant_id=owner_tenant))
            session.add(Agent(id=reviewer_id, name="reviewer", creator_id=owner_user, tenant_id=owner_tenant))
            task_id = await _seed_completed_task(
                session,
                tenant_id=owner_tenant,
                user_id=owner_user,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
            )

    with tenant_context(other_tenant):
        async with session.begin():
            session.add(Tenant(id=other_tenant, name=f"live-{other_tenant.hex[:6]}", slug=f"live-{other_tenant.hex[:8]}"))
            await session.flush()
            session.add(User(id=other_user, display_name="other-user", tenant_id=other_tenant))
            await session.flush()

    actor = EvaluationActor(user_id=other_user)
    with tenant_context(other_tenant):
        async with session.begin():
            r = await completion_service.evaluate_task(
                session, task_id=task_id, tenant_id=other_tenant, actor=actor
            )
    assert r.code == CP_TENANT_MISMATCH, r
    assert r.decision is None, "no C5 row for a subject the tenant cannot see"
    rows = await _structured_decision_rows(session, other_tenant, f"task://{task_id}")
    assert rows == [], "the cross-tenant call must write nothing in the caller's tenant"
    await session.close()

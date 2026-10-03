"""The Phase 4 Delivery record lane tests — two tiers (card t_af586c02).

- **DB-free tier**: the pure C-D1..C-D3 gate stack (every ``CD_*`` code
  path: non-CP_OK scope -> ``CD_NOT_COMPLETED`` (never a record written),
  unknown destination -> ``CD_DESTINATION_INVALID``, a cited id that is not
  SEALED + current-valid-approved -> ``CD_NO_SEALED_APPROVED``, unknown /
  unreadable inputs -> the ``CD_EVAL_ERROR`` catch-all, fail-closed), the
  closed code / destination / state sets, the cited-set resolver, the
  current-valid SEALED+approved set read, and the record provenance / payload
  shape.  No Postgres.
- **live f073 tier** (skipped when no reachable scratch Postgres): the
  delivery record persists + audit read-back (the C5 decision-row
  provenance link), a non-CP_OK project -> ``CD_NOT_COMPLETED`` with NO
  record written, cross-tenant delivery failing before any write, the
  idempotent re-delivery, and the PENDING -> terminal transport transition
  (the DAO's only post-append write path, one-way + fail-closed).

Provision the scratch DB once (the D3 lesson: the real alembic chain,
main-tree venv, PYTHONPATH = worktree backend — never os.getcwd()-based
discovery)::

    "I:/project/AI Company OS/backend/.venv/Scripts/python.exe" _provision_f073.py
    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t_af586c02_f073 \
        pytest tests/test_delivery_service.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

import app.models.agent
import app.models.agent_run
import app.models.agent_tool_execution
import app.models.analysis
import app.models.artifact_evidence
import app.models.delivery_record
import app.models.planning
import app.models.project
import app.models.task
import app.models.tenant
import app.models.user  # noqa: F401
from app.dao.delivery_record_dao import DeliveryRecordClosedError, delivery_record_dao
from app.models.artifact_evidence import EvidenceRecord
from app.models.delivery_record import DELIVERY_STATES, MAX_DELIVERY_CITED_IDS, DeliveryRecord
from app.services.completion_service import (
    CD_DESTINATION_INVALID,
    CD_EVAL_ERROR,
    CD_NO_SEALED_APPROVED,
    CD_NOT_COMPLETED,
    CD_OK,
    CD_RESULT_CODES,
    CP_DEPS_NOT_DONE,
    CP_NO_APPROVING_REVIEW,
    CP_NO_WORK,
    CP_NOT_SEALED,
    CP_OPEN_SLOT,
    CP_RESULT_CODES,
    CP_TENANT_MISMATCH,
    DELIVERY_DESTINATION_KINDS,
    EvaluationActor,
    TaskEvalInput,
)
from app.services.completion_service import (
    CP_EVAL_ERROR as CP_EVAL_ERROR_CODE,
)
from app.services.delivery_service import (
    DELIVERY_SCOPES,
    DeliveryResult,
    approved_artifact_ids_for_bundles,
    delivery_gate,
    delivery_service,
    resolve_cited_artifact_ids,
    sealed_and_approved_check,
)

# ---------------------------------------------------------------------------
# In-memory stand-in rows (DB-free tier): they duck-type the ledger / task
# models the pure core reads via ``getattr`` — no isinstance checks (same
# shape as the completion-lane tests).
# ---------------------------------------------------------------------------
TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
BUILDER = uuid.uuid4()
REVIEWER = uuid.uuid4()
BUILDERS = frozenset({BUILDER})


def _task(tid: uuid.UUID, tenant: Any = TENANT, ttype: str = "todo") -> Any:
    return SimpleNamespace(id=tid, type=ttype, tenant_id=tenant)


def _artifact(aid: uuid.UUID, seal: str = "SEALED", superseded_by: Any = None, tenant: Any = TENANT) -> Any:
    return SimpleNamespace(id=aid, seal_status=seal, superseded_by=superseded_by, tenant_id=tenant)


def _review(
    rid: uuid.UUID,
    outcome: str,
    art_id: Any,
    agent: Any = REVIEWER,
    tenant: Any = TENANT,
    when: int = 1,
) -> Any:
    return SimpleNamespace(
        id=rid,
        kind="review",
        outcome=outcome,
        artifact_id=art_id,
        created_by_agent=agent,
        tenant_id=tenant,
        created_at=when,
    )


def _bundle(tid: uuid.UUID, artifacts: list, reviews: list, builders: Any = BUILDERS) -> TaskEvalInput:
    return TaskEvalInput(task=cast(Any, _task(tid)), artifacts=artifacts, reviews=reviews, builder_agent_ids=builders)


def _approved() -> dict[str, object]:
    """One healthy task: SEALED artifact + a disjoint 'pass' review."""
    tid, art, rev = uuid.uuid4(), _artifact(uuid.uuid4()), _review(uuid.uuid4(), "pass", None)
    rev.artifact_id = art.id
    return approved_artifact_ids_for_bundles({tid: _bundle(tid, [art], [rev])}, tenant_id=TENANT)


APPROVED_IDS = {str(uuid.uuid4()): "t1"}


# ---------------------------------------------------------------------------
# Closed code sets (C6 / C-D2 / C-D3, consumed from the lane + the model).
# ---------------------------------------------------------------------------
def test_cd_code_set_is_exactly_closed() -> None:
    assert CD_RESULT_CODES == frozenset(
        {CD_OK, CD_NOT_COMPLETED, CD_NO_SEALED_APPROVED, CD_EVAL_ERROR, CD_DESTINATION_INVALID}
    )
    assert CP_RESULT_CODES, "the completion code set is consumed, not redefined"


def test_destination_and_state_vocabulary() -> None:
    assert tuple(DELIVERY_DESTINATION_KINDS) == ("channel", "published_page", "project_record")
    assert tuple(DELIVERY_STATES) == ("PENDING", "DELIVERED", "FAILED")
    assert tuple(DELIVERY_SCOPES) == ("wp", "project")


# ---------------------------------------------------------------------------
# The C-D1..C-D3 gate stack (DB-free, first failing term wins, fail-closed).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "scope_code",
    [
        CP_NO_WORK,
        CP_NOT_SEALED,
        CP_NO_APPROVING_REVIEW,
        CP_DEPS_NOT_DONE,
        CP_OPEN_SLOT,
        CP_TENANT_MISMATCH,
        CP_EVAL_ERROR_CODE,
    ],
)
def test_gate_non_cp_ok_scope_is_cd_not_completed(scope_code: str) -> None:
    """A non-CP_OK scope has NO input to build a delivery from — the
    structural root of 'Agent says done -> Delivery impossible'."""
    code, cited = delivery_gate(
        scope_code=scope_code,
        destination_kind="project_record",
        requested_artifact_ids=None,
        approved_artifact_ids=APPROVED_IDS,
    )
    assert code == CD_NOT_COMPLETED
    assert cited is None, "no record input on a failed gate"


def test_gate_unknown_scope_code_is_the_catch_all() -> None:
    code, cited = delivery_gate(
        scope_code="CP_BOGUS",
        destination_kind="channel",
        requested_artifact_ids=None,
        approved_artifact_ids=APPROVED_IDS,
    )
    assert code == CD_EVAL_ERROR, "unknown scope code fails closed, never CD_OK"
    assert cited is None


def test_gate_unknown_destination_is_invalid() -> None:
    for bad in ("external_platform", "email", "", None):
        code, cited = delivery_gate(
            scope_code="CP_OK", destination_kind=bad, requested_artifact_ids=None, approved_artifact_ids=APPROVED_IDS
        )
        assert code == CD_DESTINATION_INVALID, bad
        assert cited is None


def test_gate_every_closed_destination_kind_passes_the_destination_term() -> None:
    for kind in DELIVERY_DESTINATION_KINDS:
        code, cited = delivery_gate(
            scope_code="CP_OK", destination_kind=kind, requested_artifact_ids=None, approved_artifact_ids=APPROVED_IDS
        )
        assert code == CD_OK
        assert cited is not None and set(cited) == set(APPROVED_IDS)


def test_gate_cited_id_outside_the_sealed_approved_set() -> None:
    art_id = str(uuid.uuid4())
    code, cited = delivery_gate(
        scope_code="CP_OK",
        destination_kind="channel",
        requested_artifact_ids=[art_id],
        approved_artifact_ids=APPROVED_IDS,
    )
    assert code == CD_NO_SEALED_APPROVED
    assert cited is None, "a cited DRAFT / superseded / unapproved id writes nothing"


def test_gate_empty_resolved_cited_set_fails_closed() -> None:
    # CP_OK scope, closed destination, but ZERO approved artifacts (an
    # empty approved map): there is nothing SEALED+approved to hand over.
    code, cited = delivery_gate(
        scope_code="CP_OK", destination_kind="project_record", requested_artifact_ids=None, approved_artifact_ids={}
    )
    assert code == CD_NO_SEALED_APPROVED
    assert cited is None


def test_explicit_empty_cited_list_is_the_catch_all() -> None:
    # A delivery must name what it delivers: an explicit empty list is a
    # malformed input -> the catch-all, never a silent empty delivery.
    code, cited = delivery_gate(
        scope_code="CP_OK", destination_kind="channel", requested_artifact_ids=[], approved_artifact_ids=APPROVED_IDS
    )
    assert code == CD_EVAL_ERROR
    assert cited is None


def test_explicit_duplicate_cited_ids_fail_closed() -> None:
    first = next(iter(APPROVED_IDS))
    code, cited = delivery_gate(
        scope_code="CP_OK",
        destination_kind="channel",
        requested_artifact_ids=[first, first],
        approved_artifact_ids=APPROVED_IDS,
    )
    assert code == CD_EVAL_ERROR
    assert cited is None


def test_resolved_cited_ids_default_to_the_full_approved_set() -> None:
    cited, failure = resolve_cited_artifact_ids(None, APPROVED_IDS)
    assert failure is None
    assert cited == sorted(APPROVED_IDS, key=str)
    # An explicit subset resolves as-is.
    one = next(iter(APPROVED_IDS))
    cited, failure = resolve_cited_artifact_ids([one], APPROVED_IDS)
    assert failure is None
    assert cited == [one]
    # The bool helper mirrors it.
    assert sealed_and_approved_check(None, APPROVED_IDS) is True
    assert sealed_and_approved_check([one], APPROVED_IDS) is True
    assert sealed_and_approved_check([str(uuid.uuid4())], APPROVED_IDS) is False


# ---------------------------------------------------------------------------
# The current-valid SEALED+approved set read (the C-D1 cited-set input).
# ---------------------------------------------------------------------------
def test_approved_set_includes_sealed_artifact_with_pass_review() -> None:
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id)
    result = approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [rev])}, tenant_id=TENANT)
    assert set(result) == {str(art.id)}


def test_approved_set_excludes_draft_artifacts() -> None:
    art = _artifact(uuid.uuid4(), seal="DRAFT")
    rev = _review(uuid.uuid4(), "pass", art.id)
    assert approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [rev])}) == {}


def test_approved_set_excludes_superseded_artifacts() -> None:
    stale = _artifact(uuid.uuid4(), superseded_by=uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", stale.id)
    assert approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [stale], [rev])}) == {}


def test_approved_set_drops_stale_review_over_superseded_set() -> None:
    old = _artifact(uuid.uuid4(), superseded_by=uuid.uuid4())
    new = _artifact(uuid.uuid4())
    old_review = _review(uuid.uuid4(), "pass", old.id, when=1)
    bundles = {uuid.uuid4(): _bundle(uuid.uuid4(), [old, new], [old_review])}
    # The only pass row cites the superseded artifact: not current-valid over
    # the current set -> no approving coverage (the review card §3.4 shape).
    assert approved_artifact_ids_for_bundles(bundles, tenant_id=TENANT) == {}


def test_approved_set_excludes_fail_and_inconclusive_reviews() -> None:
    for outcome in ("fail", "inconclusive"):
        art = _artifact(uuid.uuid4())
        rev = _review(uuid.uuid4(), outcome, art.id)
        assert (
            approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [rev])}, tenant_id=TENANT)
            == {}
        ), outcome


def test_approved_set_excludes_builder_reviewer() -> None:
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id, agent=BUILDER)
    # G1 / invariant 13 read-side mirror: a builder's pass row is not
    # approving coverage.
    assert (
        approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [rev])}, tenant_id=TENANT)
        == {}
    )
    # ... but a user-captured review has no agent reviewer: vacuously held.
    user_review = _review(uuid.uuid4(), "pass", art.id, agent=None)
    result = approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [user_review])})
    assert set(result) == {str(art.id)}


def test_approved_set_fails_closed_on_unknown_builder_set() -> None:
    art = _artifact(uuid.uuid4())
    rev = _review(uuid.uuid4(), "pass", art.id, agent=REVIEWER)
    bundle = _bundle(uuid.uuid4(), [art], [rev], builders=None)
    assert approved_artifact_ids_for_bundles({uuid.uuid4(): bundle}, tenant_id=TENANT) == {}


def test_approved_set_reasserts_the_tenant_boundary() -> None:
    art = _artifact(uuid.uuid4(), tenant=OTHER_TENANT)
    rev = _review(uuid.uuid4(), "pass", art.id, tenant=OTHER_TENANT)
    assert approved_artifact_ids_for_bundles({uuid.uuid4(): _bundle(uuid.uuid4(), [art], [rev])}, tenant_id=TENANT) == {}


# ---------------------------------------------------------------------------
# The record provenance / payload shape (C-D3, DB-free row construction).
# ---------------------------------------------------------------------------
def _record(
    *,
    state: str = "PENDING",
    executed_at: datetime | None = None,
    destination_kind: str = "project_record",
    agent: Any = None,
    user: Any = None,
    artifact_ids: list[str] | None = None,
) -> DeliveryRecord:
    if artifact_ids is None:
        artifact_ids = ["a" * 32]
    return DeliveryRecord(
        tenant_id=TENANT,
        scope_ref=f"project://{uuid.uuid4()}",
        artifact_ids=artifact_ids,
        cited_review_row_ids=["b" * 32],
        cp_decision_row_id=uuid.uuid4(),
        destination_kind=destination_kind,
        destination_ref=None,
        state=state,
        executed_at=executed_at,
        decided_at=datetime.now(UTC),
        decided_by_agent=agent,
        decided_by_user=user,
    )


def test_pending_record_carries_the_full_provenance_shape() -> None:
    rec = _record(state="PENDING", user=TENANT)
    assert rec.state == "PENDING"
    assert rec.executed_at is None
    assert rec.artifact_ids == ["a" * 32]
    assert rec.cited_review_row_ids == ["b" * 32]
    assert rec.cp_decision_row_id is not None
    assert rec.decided_by_agent is None and rec.decided_by_user == TENANT
    # The DAO boundary accepts the well-formed PENDING + D5-XOR row.
    delivery_record_dao._validate_new(rec)


def test_terminal_record_must_carry_executed_at() -> None:
    for state in ("DELIVERED", "FAILED"):
        stamped = _record(state=state, executed_at=datetime.now(UTC), agent=BUILDER)
        delivery_record_dao._validate_new(stamped)
        # A terminal state without the stamp is rejected at the boundary;
        # the DAO stamps it (the one-way transition window).
        unstamped = _record(state=state, executed_at=None, agent=BUILDER)
        delivery_record_dao._validate_new(unstamped)
        assert unstamped.executed_at is not None, f"{state} is stamped exactly once at the boundary"
        # PENDING with a stamp is malformed: rejected, never silently fixed.
        with pytest.raises(DeliveryRecordClosedError):
            delivery_record_dao._validate_new(_record(state="PENDING", executed_at=datetime.now(UTC), user=TENANT))


def test_dao_rejects_unknown_state_fail_closed() -> None:
    rec = _record(state="RECALLED", user=TENANT)  # outside the closed V1 set (deferred, design §8)
    with pytest.raises(DeliveryRecordClosedError) as exc:
        delivery_record_dao._validate_new(rec)
    assert exc.value.code == CD_EVAL_ERROR, "an unknown state is the catch-all, never accepted"


def test_dao_rejects_destination_outside_the_closed_vocabulary() -> None:
    rec = _record(destination_kind="external_platform", user=TENANT)
    with pytest.raises(DeliveryRecordClosedError) as exc:
        delivery_record_dao._validate_new(rec)
    assert exc.value.code == CD_DESTINATION_INVALID


def test_dao_enforces_the_d5_xor_source() -> None:
    with pytest.raises(DeliveryRecordClosedError):
        delivery_record_dao._validate_new(_record(agent=BUILDER, user=TENANT))  # both
    with pytest.raises(DeliveryRecordClosedError):
        delivery_record_dao._validate_new(_record(agent=None, user=None))  # neither


def test_dao_bounds_the_cited_lists() -> None:
    rec = _record(artifact_ids=["x"] * (MAX_DELIVERY_CITED_IDS + 1), user=TENANT)
    with pytest.raises(DeliveryRecordClosedError) as exc:
        delivery_record_dao._validate_new(rec)
    assert exc.value.code == CD_EVAL_ERROR
    # A malformed (non-list-of-str) cited list is the catch-all too.
    rec2 = _record(artifact_ids=None, user=TENANT)
    rec2.artifact_ids = ["ok"]
    delivery_record_dao._validate_new(rec2)
    rec2.artifact_ids = cast(Any, "not-a-list")
    with pytest.raises(DeliveryRecordClosedError):
        delivery_record_dao._validate_new(rec2)


# ---------------------------------------------------------------------------
# Live f073 tier (skipped when no reachable scratch Postgres; each test also
# skips when the f073 table is absent so the module stays re-runnable).
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


async def _require_f073_tables(conn) -> None:
    from sqlalchemy import text as _text
    from sqlalchemy.exc import OperationalError

    try:
        await conn.execute(_text("SELECT 1 FROM delivery_records LIMIT 1"))
    except OperationalError:
        pytest.skip("f073 delivery_records table not provisioned on the scratch DB (run _provision_f073.py)")


async def _seed_cp_ok_project(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    builder_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    project_id: uuid.UUID,
    with_ledger: bool = True,
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
    """A CP_OK project ledger proof (the completion-lane seed shape): the
    Tenant/User/Agents/Project rows, one in-scope todo Task, and — when
    ``with_ledger`` — its SEALED artifact + disjoint-agent 'pass' review.
    Returns ``(task_id, artifact_id, review_id)``."""
    from app.dao.artifact_evidence_dao import artifact_record_dao, evidence_record_dao
    from app.models.agent import Agent
    from app.models.artifact_evidence import ArtifactRecord
    from app.models.project import Project
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.models.user import User

    session.add(Tenant(id=tenant_id, name=f"live-{tenant_id.hex[:6]}", slug=f"live-{tenant_id.hex[:8]}"))
    await session.flush()
    session.add(User(id=user_id, display_name="live-user", tenant_id=tenant_id))
    await session.flush()
    session.add(Agent(id=builder_id, name="builder", creator_id=user_id, tenant_id=tenant_id))
    session.add(Agent(id=reviewer_id, name="reviewer", creator_id=user_id, tenant_id=tenant_id))
    session.add(Project(id=project_id, name="live-project", created_by=user_id, tenant_id=tenant_id, status="EXECUTING"))
    await session.flush()
    task_id = uuid.uuid4()
    session.add(
        Task(
            id=task_id,
            agent_id=builder_id,
            title="live-delivery-task",
            created_by=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
    )
    await session.flush()  # the Task row must be flushed before the ledger rows reference it (FK)
    artifact_id, review_id = None, None
    if with_ledger:
        art = ArtifactRecord(
            id=uuid.uuid4(),
            task_id=task_id,
            created_by_user=user_id,
            type="file",
            title="out.txt",
            storage_scheme="workspace_path",
            storage_ref=f"/live/{tenant_id.hex[:8]}/{task_id}/out.txt",
            content_hash="e" * 64,
        )
        written = await artifact_record_dao.add_artifact(art, tenant_id=tenant_id, db=session)
        await artifact_record_dao.seal(written, db=session)
        artifact_id = written.id
        rev = EvidenceRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            artifact_id=artifact_id,
            kind="review",
            outcome="pass",
            subject_ref=f"evidence://review/{task_id}",
            payload={"verdict": "ok"},
            created_by_agent=reviewer_id,
        )
        added = await evidence_record_dao.add_evidence(
            rev, tenant_id=tenant_id, db=session, reviewer_builder_agents={builder_id}
        )
        review_id = added.id
    await session.flush()
    return task_id, artifact_id, review_id


async def _delivery_rows(session, tenant_id: uuid.UUID, scope_ref: str) -> list:
    from app.models.delivery_record import DeliveryRecord

    stmt = (
        select(DeliveryRecord)
        .where(DeliveryRecord.scope_ref == scope_ref, DeliveryRecord.tenant_id == tenant_id)
        .order_by(DeliveryRecord.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_live_delivery_record_persists_with_audit_read_back(_engine) -> None:
    """A CP_OK project delivery (``project_record`` destination) persists ONE
    terminal record; the audit read-back verifies the full provenance chain:
    the record -> its citing C5 decision row (outcome CP_OK) -> the citing
    review row -> the cited SEALED artifact."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.artifact_evidence_dao import evidence_record_dao
    from app.dao.base import tenant_context

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            _, artifact_id, review_id = await _seed_cp_ok_project(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    scope_ref = f"project://{project_id}"
    with tenant_context(tenant_id):
        async with session.begin():
            result = await delivery_service.deliver(
                session,
                scope_ref=scope_ref,
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=None,
                builder_agent_ids=builders,
            )
            # The audit read-back: the C5 decision row the record cites.
            decision_row = await evidence_record_dao.latest_decision_row_for_subject(scope_ref, db=session)

    assert isinstance(result, DeliveryResult)
    assert result.code == CD_OK, result
    assert result.record is not None
    rec = result.record
    # C-D3: project_record is terminal AT the decision (the record IS the
    # destination) — executed_at stamped one-way.
    assert rec.state == "DELIVERED"
    assert rec.executed_at is not None
    assert rec.decided_at is not None
    assert rec.decided_by_user == user_id and rec.decided_by_agent is None
    assert rec.tenant_id == tenant_id
    assert rec.project_id == project_id
    assert rec.scope_ref == scope_ref
    assert rec.destination_kind == "project_record"
    # Provenance chain: the record cites the C5 row + the approving review
    # row + the SEALED artifact set (all ledger-queryable, G3 shape).
    assert rec.artifact_ids == [str(artifact_id)], "the full current-valid approved set is delivered"
    assert rec.cited_review_row_ids == [str(review_id)]
    assert decision_row is not None and decision_row.payload is not None
    assert decision_row.payload["outcome"] == "CP_OK"
    assert rec.cp_decision_row_id == decision_row.id
    # The audit read-back through the tenant-scoped DAO.
    with tenant_context(tenant_id):
        rows = await delivery_record_dao.list_by_scope(scope_ref, db=session)
    assert len(rows) == 1
    assert rows[0].id == rec.id
    await session.close()


@pytest.mark.asyncio
async def test_live_non_cp_ok_project_writes_no_record(_engine) -> None:
    """A project with an in-scope task but NO ledger proof evaluates
    CP_NO_WORK (not CP_OK) -> CD_NOT_COMPLETED: the gate has no input, and
    NO delivery record is written (fail-closed at every hop)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            await _seed_cp_ok_project(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
                with_ledger=False,
            )

    actor = EvaluationActor(user_id=user_id)
    with tenant_context(tenant_id):
        async with session.begin():
            result = await delivery_service.deliver(
                session,
                scope_ref=f"project://{project_id}",
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=[str(uuid.uuid4())],
                builder_agent_ids=frozenset({builder_id}),
            )
    assert result.code == CD_NOT_COMPLETED, result
    assert result.record is None, "no record on a non-CP_OK scope"
    assert result.cited_ids is None
    with tenant_context(tenant_id):
        rows = await _delivery_rows(session, tenant_id, f"project://{project_id}")
    assert rows == [], "the gate rejection wrote nothing to delivery_records"
    await session.close()


@pytest.mark.asyncio
async def test_live_cp_ok_scope_citing_unapproved_id_writes_no_record(_engine) -> None:
    """A CP_OK project whose cited id is outside the current-valid SEALED+
    approved set -> CD_NO_SEALED_APPROVED: the cited-set term of C-D1 fails
    at write time and NOTHING is delivered (fail-closed)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            await _seed_cp_ok_project(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    scope_ref = f"project://{project_id}"
    foreign_id = str(uuid.uuid4())
    with tenant_context(tenant_id):
        async with session.begin():
            result = await delivery_service.deliver(
                session,
                scope_ref=scope_ref,
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=[foreign_id],
                builder_agent_ids=builders,
            )
            rows = await delivery_record_dao.list_by_scope(scope_ref, db=session)
    assert result.code == CD_NO_SEALED_APPROVED, result
    assert result.record is None
    assert rows == [], "a cited id outside the SEALED+approved set writes nothing"
    await session.close()


@pytest.mark.asyncio
async def test_live_cross_tenant_delivery_fails_before_any_write(_engine) -> None:
    """A cross-tenant delivery of a foreign project fails the gate read
    (CP_TENANT_MISMATCH) BEFORE any dependent read and writes NOTHING in
    either tenant: no delivery record, no C5 decision row for an unseen
    subject."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    owner_tenant, other_tenant = uuid.uuid4(), uuid.uuid4()
    owner_user, other_user = uuid.uuid4(), uuid.uuid4()
    builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(owner_tenant):
        async with session.begin():
            await _seed_cp_ok_project(
                session,
                tenant_id=owner_tenant,
                user_id=owner_user,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    with tenant_context(other_tenant):
        async with session.begin():
            session.add(Tenant(id=other_tenant, name=f"live-{other_tenant.hex[:6]}", slug=f"live-{other_tenant.hex[:8]}"))
            await session.flush()
            session.add(User(id=other_user, display_name="other-user", tenant_id=other_tenant))
            session.add(Agent(id=uuid.uuid4(), name="x", creator_id=other_user, tenant_id=other_tenant))
            await session.flush()

    actor = EvaluationActor(user_id=other_user)
    with tenant_context(other_tenant):
        async with session.begin():
            result = await delivery_service.deliver(
                session,
                scope_ref=f"project://{project_id}",
                tenant_id=other_tenant,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=None,
            )
    assert result.code == CD_NOT_COMPLETED, "the foreign scope is not CP_OK in the caller's tenant"
    assert result.record is None
    # No write in EITHER tenant: the caller's tenant has no delivery row, and
    # the owner's ledger is untouched by the cross-tenant call.
    with tenant_context(other_tenant):
        caller_rows = await _delivery_rows(session, other_tenant, f"project://{project_id}")
    assert caller_rows == [], "the cross-tenant call must write nothing in the caller's tenant"
    with tenant_context(owner_tenant):
        owner_rows = await _delivery_rows(session, owner_tenant, f"project://{project_id}")
    assert owner_rows == [], "the cross-tenant call must not write into the owner's tenant"
    await session.close()


@pytest.mark.asyncio
async def test_live_re_delivery_is_idempotent(_engine) -> None:
    """A re-delivery of the same scope + same cited set against an existing
    terminal record returns that record (``already_delivered``) and appends
    NOTHING — V1 has no RECALLED, a rework supersedes instead (design §8)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            await _seed_cp_ok_project(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    scope_ref = f"project://{project_id}"
    with tenant_context(tenant_id):
        async with session.begin():
            r1 = await delivery_service.deliver(
                session,
                scope_ref=scope_ref,
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=None,
                builder_agent_ids=builders,
            )
        assert r1.code == CD_OK and r1.record is not None
        first_id = r1.record.id
        async with session.begin():
            r2 = await delivery_service.deliver(
                session,
                scope_ref=scope_ref,
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="project_record",
                artifact_ids=None,
                builder_agent_ids=builders,
            )
        rows = await delivery_record_dao.list_by_scope(scope_ref, db=session)
    assert r2.code == CD_OK
    assert r2.already_delivered is True
    assert r2.record is not None and r2.record.id == first_id
    assert len(rows) == 1, "the re-delivery appended nothing"
    assert r2.record.state == "DELIVERED"
    await session.close()


@pytest.mark.asyncio
async def test_live_channel_delivery_pending_then_transport_transition(_engine) -> None:
    """A ``channel`` destination stays PENDING at the decision (the owning
    transport owns the terminal state — C-D3); ``transition_state`` is the
    ONE post-append write path: one-way PENDING -> DELIVERED + executed_at
    stamped, and a second transition is rejected (fail-closed, no RECALLED
    in V1)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.dao.base import tenant_context
    from app.dao.delivery_record_dao import delivery_record_dao

    session = AsyncSession(bind=_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id, builder_id, reviewer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_id = uuid.uuid4()

    with tenant_context(tenant_id):
        async with session.begin():
            await _seed_cp_ok_project(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                builder_id=builder_id,
                reviewer_id=reviewer_id,
                project_id=project_id,
            )

    actor = EvaluationActor(user_id=user_id)
    builders = frozenset({builder_id})
    scope_ref = f"project://{project_id}"
    with tenant_context(tenant_id):
        async with session.begin():
            r1 = await delivery_service.deliver(
                session,
                scope_ref=scope_ref,
                tenant_id=tenant_id,
                actor=actor,
                destination_kind="channel",
                destination_ref="tool-result://run-1/answer",
                artifact_ids=None,
                builder_agent_ids=builders,
            )
            assert r1.code == CD_OK and r1.record is not None
            assert r1.record.state == "PENDING", "channel delivery is not terminal at the decision"
            assert r1.record.executed_at is None
            record = r1.record
            # The owning-transport terminal write (the only post-append path).
            transitioned = await delivery_record_dao.transition_state(record, new_state="DELIVERED", db=session)
            assert transitioned.state == "DELIVERED"
            assert transitioned.executed_at is not None
            with pytest.raises(DeliveryRecordClosedError):
                await delivery_record_dao.transition_state(record, new_state="DELIVERED", db=session)
            with pytest.raises(DeliveryRecordClosedError):
                await delivery_record_dao.transition_state(record, new_state="RECALLED", db=session)
    await session.close()

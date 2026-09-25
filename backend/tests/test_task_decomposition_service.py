"""Phase 2D Task Decomposition service + graph validation — unit tests.

Covers the builder-lane contract (card t_b8545ece) per the two design docs:
- docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §5/§6 (graph validation,
  blocked/ready, the execution gate) and
- docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md §3/§4/§5/§7 (classification, field
  mapping, dedup, no-enqueue, provenance auto-fill).

Two tiers (mirroring the t_650ddd87 test split):

- **DB-free core** (always run): the pure functions ``classify`` /
  ``upstream_reachable`` / ``build_task_fields`` and the ``f070`` dedup
  migration contract.  These are deterministic and need no database — the
  80-combo classification grid, the 2-cycle / 3-cycle / diamond / broken-chain
  (断链) reachability proofs, and the 500-char truncation boundary.

- **live-schema tier** (skipped when no reachable Postgres): the
  ``TaskGraphService`` edge writes + ready/blocked against a real f069/f070
  schema, the ``TaskDecompositionService.convert`` boundary (pending-not-
  enqueued G4, dedup §4, provenance auto-fill), and the f070 UNIQUE dedup
  last-resort guard.

Running the live tier::

    DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/<scratch> \
        uv run --extra dev pytest tests/test_task_decomposition_service.py
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.analysis import AnalysisFinding, AnalysisRun
from app.models.project import Project
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.services.task_decomposition_service import (
    build_task_fields,
    classify,
    task_decomposition_service,
)
from app.services.task_graph_service import (
    task_graph_service,
    upstream_reachable,
)

# ---------------------------------------------------------------------------
# DB-FREE CORE
# ---------------------------------------------------------------------------

CATEGORY_VALUES = ("SECURITY", "RISK", "TECH_DEBT", "OPEN_QUESTION", "FACT")
SEVERITY_VALUES = ("INFO", "WARN", "HIGH", "CRITICAL")
TAG_VALUES = ("FACT", "OBSERVATION", "INFERENCE", "UNKNOWN")

# The executable class is EXACTLY the E1/E2 rows (spec §3.2).  The spec §7
# "200-combo truth table" is a doc typo: the closed grid is 5x4x4 = 80.
_EXPECTED_EXECUTABLE = {
    ("TECH_DEBT", "WARN", "FACT"),
    ("TECH_DEBT", "HIGH", "FACT"),
    ("TECH_DEBT", "CRITICAL", "FACT"),
    ("SECURITY", "HIGH", "FACT"),
    ("SECURITY", "CRITICAL", "FACT"),
}


def test_classify_full_closed_grid_exactly_e1_e2() -> None:
    executable = set()
    combos = 0
    for cat in CATEGORY_VALUES:
        for sev in SEVERITY_VALUES:
            for tag in TAG_VALUES:
                combos += 1
                result = classify(cat, sev, tag)
                assert result in ("executable", "planning")
                if result == "executable":
                    executable.add((cat, sev, tag))
    assert combos == 80, f"closed grid must be 5x4x4=80, saw {combos}"
    assert executable == _EXPECTED_EXECUTABLE, f"executable set drifted: {sorted(executable)}"


def test_classify_planning_exclusions_p1_p4() -> None:
    # P1 OPEN_QUESTION, P2 RISK (any sev/tag) -> planning.
    for cat in ("OPEN_QUESTION", "RISK"):
        for sev in SEVERITY_VALUES:
            for tag in TAG_VALUES:
                assert classify(cat, sev, tag) == "planning"
    # P3 low-confidence tag (INFERENCE / UNKNOWN) -> planning.
    for cat in CATEGORY_VALUES:
        for sev in SEVERITY_VALUES:
            for tag in ("INFERENCE", "UNKNOWN"):
                assert classify(cat, sev, tag) == "planning"
    # P4 severity INFO -> planning even for an executable category+tag.
    assert classify("TECH_DEBT", "INFO", "FACT") == "planning"
    assert classify("SECURITY", "INFO", "FACT") == "planning"


def test_classify_default_fail_closed_is_planning() -> None:
    # A concrete + confident shape NOT on the E1/E2 allow-list stays planning.
    assert classify("FACT", "CRITICAL", "FACT") == "planning"  # category FACT, not E1/E2
    assert classify("SECURITY", "WARN", "FACT") == "planning"  # SECURITY needs HIGH/CRITICAL


def _pairs(*edges: tuple[str, str]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Build the reachability input from a set of named ``A->B`` edges."""
    names = {n for e in edges for n in e}
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, name) for name in names}
    return [(ids[a], ids[b]) for a, b in edges]


def test_reachable_2_cycle_detected() -> None:
    # Existing edge A->B (A depends on B). Candidate B->A (B depends on A)
    # closes the loop. The service asks: does A already transitively depend on
    # B? -> follow depends-on from A (start=a); reaching B (target=b) = cycle.
    pairs = _pairs(("A", "B"))
    a, b = (uuid.uuid5(uuid.NAMESPACE_URL, "A"), uuid.uuid5(uuid.NAMESPACE_URL, "B"))
    assert upstream_reachable(pairs, start=a, target=b) is True


def test_reachable_3_cycle_detected() -> None:
    # A->B->C (A depends on B, B depends on C). Candidate C->A (C depends on
    # A) closes a 3-cycle: does A transitively depend on C? Follow depends-on
    # from A (start=a) to C (target=c).
    pairs = _pairs(("A", "B"), ("B", "C"))
    a = uuid.uuid5(uuid.NAMESPACE_URL, "A")
    c = uuid.uuid5(uuid.NAMESPACE_URL, "C")
    assert upstream_reachable(pairs, start=a, target=c) is True


def test_reachable_diamond_no_false_positive() -> None:
    # A<-B, A<-C (both B and C depend on A); no path from B to C or C to B.
    pairs = _pairs(("B", "A"), ("C", "A"))
    b = uuid.uuid5(uuid.NAMESPACE_URL, "B")
    c = uuid.uuid5(uuid.NAMESPACE_URL, "C")
    assert upstream_reachable(pairs, start=b, target=c) is False
    assert upstream_reachable(pairs, start=c, target=b) is False


def test_reachable_broken_chain_is_disconnected() -> None:
    # 断链: A->B->C exists; D is a disconnected component.  C cannot reach D.
    pairs = _pairs(("A", "B"), ("B", "C"))
    c = uuid.uuid5(uuid.NAMESPACE_URL, "C")
    d = uuid.uuid5(uuid.NAMESPACE_URL, "D")
    assert upstream_reachable(pairs, start=c, target=d) is False


def test_reachable_oversized_set_fails_closed() -> None:
    # A 500-node chain exceeds the default 1000-node bound only when traversed
    # past it — a chain of 2000 nodes, seeded so the target sits at the far end,
    # must be reported reachable (fail closed) rather than admitted.
    n = 2000
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, str(i)) for i in range(n)]
    pairs = [(ids[i], ids[i + 1]) for i in range(n - 1)]  # 0 -> 1 -> ... -> n-1
    assert upstream_reachable(pairs, start=ids[0], target=ids[n - 1], limit=1000) is True


def test_reachable_within_bound_is_accurate() -> None:
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, str(i)) for i in range(50)]
    pairs = [(ids[i], ids[i + 1]) for i in range(49)]
    assert upstream_reachable(pairs, start=ids[0], target=ids[49], limit=1000) is True
    # a node NOT on the path is not reachable.
    off = uuid.uuid5(uuid.NAMESPACE_URL, "off")
    assert upstream_reachable(pairs, start=ids[0], target=off, limit=1000) is False


def test_build_task_fields_truncation_boundary() -> None:
    run = AnalysisRun(
        id=uuid.uuid4(), project_id=uuid.uuid4(), revision_sha="a" * 64,
        status="AN_COMPLETED", tenant_id=uuid.uuid4(),
    )
    prefix = 11  # len("[TECH_DEBT] ")
    for length in (499, 500, 501):
        summary = "x" * length
        finding = AnalysisFinding(
            id=uuid.uuid4(), analysis_run_id=run.id, severity="HIGH",
            category="TECH_DEBT", tag="FACT", summary=summary,
            evidence={"anchors": ["a.py:1"]}, tenant_id=run.tenant_id,
        )
        title = build_task_fields(finding, run, agent_id=uuid.uuid4(), created_by=uuid.uuid4())["title"]
        assert len(title) <= 500, (length, len(title))
        if prefix + length <= 500:
            assert title == f"[TECH_DEBT] {summary}"
        else:
            assert title == f"[TECH_DEBT] {summary}"[:500]


def test_build_task_fields_provenance_autofill() -> None:
    pid, rid, fid, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run = AnalysisRun(id=rid, project_id=pid, revision_sha="b" * 64, status="AN_COMPLETED", tenant_id=tid)
    finding = AnalysisFinding(
        id=fid, analysis_run_id=rid, severity="HIGH", category="SECURITY",
        tag="FACT", summary="leak", evidence={"anchors": ["s.py:9"]}, tenant_id=tid,
    )
    fields = build_task_fields(finding, run, agent_id=uuid.uuid4(), created_by=uuid.uuid4())
    assert fields["created_reason"] == "ANALYSIS_FINDING"
    assert fields["status"] == "pending" and fields["type"] == "todo"  # G4
    assert fields["project_id"] == pid and fields["analysis_run_id"] == rid
    assert fields["finding_id"] == fid and fields["revision_sha"] == "b" * 64
    assert fields["tenant_id"] == tid
    assert fields["priority"] == "high"  # HIGH -> high


def test_severity_priority_closed_map() -> None:
    from app.services.task_decomposition_service import SEVERITY_TO_PRIORITY

    assert SEVERITY_TO_PRIORITY == {"CRITICAL": "urgent", "HIGH": "high", "WARN": "medium", "INFO": "low"}


# ---------------------------------------------------------------------------
# f070 migration contract (DB-free, monkeypatched-op convention).
# ---------------------------------------------------------------------------

_F070_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "v1_11_5_f070_analysis_task_dedup.py"


def _load_f070():
    spec = importlib.util.spec_from_file_location("f070_analysis_task_dedup", _F070_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_f070_mounts_off_f069_single_head() -> None:
    m = _load_f070()
    assert m.revision == "f070_analysis_task_dedup"
    assert m.down_revision == "f069_task_graph_provenance"


def test_f070_upgrade_is_noop_when_constraint_present(monkeypatch) -> None:
    m = _load_f070()
    record: list = []
    monkeypatch.setattr(m.op, "create_unique_constraint", lambda *a, **k: record.append(a))
    monkeypatch.setattr(m.op, "get_bind", lambda: object())  # upgrade() calls op.get_bind()
    monkeypatch.setattr(m, "_existing_tables", lambda _b: {"tasks"})
    monkeypatch.setattr(m, "_existing_columns", lambda _b, _t: {"analysis_run_id", "finding_id"})
    monkeypatch.setattr(m, "_existing_indexes", lambda _b, _t: {"uq_tasks_analysis_finding"})
    m.upgrade()
    assert record == [], "constraint already present -> upgrade must be a no-op"


def test_f070_upgrade_creates_constraint_on_existing_db(monkeypatch) -> None:
    m = _load_f070()
    record: list = []
    monkeypatch.setattr(m.op, "create_unique_constraint", lambda *a, **k: record.append((a[0], a[1], list(a[2]))))
    monkeypatch.setattr(m.op, "get_bind", lambda: object())
    monkeypatch.setattr(m, "_existing_tables", lambda _b: {"tasks"})
    monkeypatch.setattr(m, "_existing_columns", lambda _b, _t: {"analysis_run_id", "finding_id", "created_reason"})
    monkeypatch.setattr(m, "_existing_indexes", lambda _b, _t: set())  # absent -> must create
    m.upgrade()
    assert record == [("uq_tasks_analysis_finding", "tasks", ["analysis_run_id", "finding_id"])]


def test_f070_upgrade_skips_when_columns_missing(monkeypatch) -> None:
    m = _load_f070()
    record: list = []
    monkeypatch.setattr(m.op, "create_unique_constraint", lambda *a, **k: record.append(a))
    monkeypatch.setattr(m.op, "get_bind", lambda: object())
    monkeypatch.setattr(m, "_existing_tables", lambda _b: {"tasks"})
    monkeypatch.setattr(m, "_existing_columns", lambda _b, _t: {"analysis_run_id"})  # finding_id missing
    monkeypatch.setattr(m, "_existing_indexes", lambda _b, _t: set())
    m.upgrade()
    assert record == [], "a torn-down schema (missing finding_id) must not add the constraint"


def test_f070_downgrade_drops_only_when_present(monkeypatch) -> None:
    m = _load_f070()
    record: list = []
    monkeypatch.setattr(m.op, "drop_constraint", lambda *a, **k: record.append((a[0], a[1], k.get("type_"))))
    monkeypatch.setattr(m.op, "get_bind", lambda: object())
    monkeypatch.setattr(m, "_existing_tables", lambda _b: {"tasks"})
    monkeypatch.setattr(m, "_existing_indexes", lambda _b, _t: {"uq_tasks_analysis_finding"})
    m.downgrade()
    assert record == [("uq_tasks_analysis_finding", "tasks", "unique")]
    # absent -> no-op
    record.clear()
    monkeypatch.setattr(m, "_existing_indexes", lambda _b, _t: set())
    m.downgrade()
    assert record == []


# ---------------------------------------------------------------------------
# LIVE-SCHEMA TIER (Postgres-guarded; skipped when no DB is reachable).
# ---------------------------------------------------------------------------

LIVE = pytest.mark.usefixtures("_live_db")


class _Seed:
    """Committed tenant graph, created once per pytest process (idempotent)."""

    def __init__(self) -> None:
        self.tenant = None
        self.user = None
        self.agent = None
        self.project = None
        self.run = None
        self.findings: list[AnalysisFinding] = []
        self.seeded = False


_SEED = _Seed()


@pytest.fixture
def _live_db() -> None:
    """Skip the live tier when no reachable Postgres is configured."""
    import asyncio

    import asyncpg

    from app.config import get_settings

    if getattr(_live_db, "_result", None) is None:  # type: ignore[attr-defined]
        async def _probe() -> bool:
            dsn = get_settings().DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
            try:
                conn = await asyncpg.connect(dsn)
                await conn.execute("select 1")
                await conn.close()
                return True
            except Exception:  # noqa: BLE001 - no DB / creds / schema all mean "skip"
                return False

        loop = asyncio.new_event_loop()
        try:
            _live_db._result = loop.run_until_complete(_probe())  # type: ignore[attr-defined]
        finally:
            loop.close()
    if not _live_db._result:  # type: ignore[attr-defined]
        pytest.skip("no reachable Postgres for the live decomposition/graph tier; the DB-free tier carries the logic")


@pytest.fixture
async def db(_live_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import get_settings

    engine = create_async_engine(get_settings().DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(db_factory) -> None:
    """Seed one committed tenant graph (idempotent, once per pytest process).

    Creates a project + an AN_COMPLETED run + a spread of findings across the
    closed grid so ``convert()`` has real executable / planning material and
    the graph service has tenant-aligned todo tasks to hang edges on.
    """
    if _SEED.seeded:
        return

    async with db_factory() as sess, sess.begin():
        _SEED.tenant = Tenant(name="b8545ece", slug="b8545ece-" + uuid.uuid4().hex[:10])
        sess.add(_SEED.tenant)
        await sess.flush()
        _SEED.user = User(
            tenant_id=_SEED.tenant.id, display_name="b8545ece", role="member",
            is_active=True, email=f"b8545ece-{uuid.uuid4().hex}@example.test",
        )
        sess.add(_SEED.user)
        await sess.flush()
        _SEED.agent = Agent(
            name="b8545ece-agent", creator_id=_SEED.user.id, tenant_id=_SEED.tenant.id,
            access_mode="company", status="running",
        )
        sess.add(_SEED.agent)
        await sess.flush()
        _SEED.project = Project(
            name="b8545ece-proj", status="ANALYZING", created_by=_SEED.user.id,
            tenant_id=_SEED.tenant.id,
        )
        sess.add(_SEED.project)
        await sess.flush()
        _SEED.run = AnalysisRun(
            project_id=_SEED.project.id, revision_sha="f" * 64, status="AN_COMPLETED",
            tenant_id=_SEED.tenant.id,
        )
        sess.add(_SEED.run)
        await sess.flush()
        spec = [
            ("TECH_DEBT", "WARN", "FACT", "tech1"),        # E1 -> executable
            ("SECURITY", "CRITICAL", "FACT", "sec1"),      # E2 -> executable
            ("OPEN_QUESTION", "HIGH", "FACT", "open1"),    # P1 -> planning
            ("SECURITY", "INFO", "FACT", "sec-info"),     # P4 -> planning
        ]
        for i, (cat, sev, tag, label) in enumerate(spec):
            f = AnalysisFinding(
                analysis_run_id=_SEED.run.id, severity=sev, category=cat, tag=tag,
                summary=f"{label} summary", evidence={"anchors": [f"{label}.py:{i}"]},
                tenant_id=_SEED.tenant.id,
            )
            sess.add(f)
            _SEED.findings.append(f)
        await sess.flush()
    _SEED.seeded = True


@LIVE
async def test_graph_service_ready_blocked_and_cycle(db) -> None:
    await _seed(db)
    from app.dao.base import tenant_context

    tenant = _SEED.tenant.id
    async with db() as sess:
        async with sess.begin():
            with tenant_context(tenant):
                done_up = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                                project_id=_SEED.project.id, title="done-up", status="done")
                pending_up = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                                   project_id=_SEED.project.id, title="pending-up", status="pending")
                down = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                             project_id=_SEED.project.id, title="down", status="pending")
                solo = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                            project_id=_SEED.project.id, title="solo", status="pending")
                for t in (done_up, pending_up, down, solo):
                    sess.add(t)
                await sess.flush()
                await task_graph_service.add_edge(
                    sess, task_id=down.id, depends_on_task_id=done_up.id, tenant_id=tenant
                )
                # down depends on a DONE task -> ready.
                assert await task_graph_service.is_ready(sess, task_id=down.id, tenant_id=tenant) is True
                # solo has no edges -> trivially ready.
                assert await task_graph_service.is_ready(sess, task_id=solo.id, tenant_id=tenant) is True
            await sess.commit()

        # 断链 + cycle: add a second edge so down also depends on a pending
        # upstream -> blocked. (a fresh pending upstream; the first block's
        # pending_up was not committed, so create a new one here.)
        async with db() as sess:
            async with sess.begin():
                with tenant_context(tenant):
                    pending_up = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                                       project_id=_SEED.project.id, title="pending-up", status="pending")
                    sess.add(pending_up)
                    await sess.flush()
                    await task_graph_service.add_edge(
                        sess, task_id=down.id, depends_on_task_id=pending_up.id, tenant_id=tenant
                    )
                    states = await task_graph_service.ready_states(
                        sess, task_ids=[down.id, solo.id], tenant_id=tenant
                    )
                    assert states[down.id] == "blocked" and states[solo.id] == "ready", states
                    down_row = await sess.get(Task, down.id)
                    unmet = await task_graph_service.ensure_ready(sess, task=down_row, tenant_id=tenant)
                    assert pending_up.id in unmet
            await sess.commit()

        # Genuine 2-cycle: X depends on Y (allowed), then Y depends on X (CYCLE).
        async with db() as sess:
            async with sess.begin():
                with tenant_context(tenant):
                    x = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                             project_id=_SEED.project.id, title="cx", status="pending")
                    y = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                             project_id=_SEED.project.id, title="cy", status="pending")
                    sess.add_all([x, y])
                    await sess.flush()
                    ok = await task_graph_service.add_edge(
                        sess, task_id=x.id, depends_on_task_id=y.id, tenant_id=tenant
                    )
                    assert ok.state == "added", ok
                    cycle = await task_graph_service.add_edge(
                        sess, task_id=y.id, depends_on_task_id=x.id, tenant_id=tenant
                    )
                    assert cycle.state == "blocked" and cycle.code == "GRAPH_CYCLE", cycle
                    # self-dependency is refused with GRAPH_SELF.
                    selfdep = await task_graph_service.add_edge(
                        sess, task_id=x.id, depends_on_task_id=x.id, tenant_id=tenant
                    )
                    assert selfdep.code == "GRAPH_SELF", selfdep
            await sess.commit()


@LIVE
async def test_conversion_pending_not_enqueued_and_dedup(db) -> None:
    """G4 no-enqueue + §4 dedup: a conversion creates pending Task rows that are
    NEVER enqueued, and a second invocation skips already-converted findings."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.agent_run import AgentRun

    tenant = _SEED.tenant.id
    async with db() as sess:
        with tenant_context(tenant):
            outcome = await task_decomposition_service.convert(
                sess, project=_SEED.project, run=_SEED.run, agent=_SEED.agent, current_user=_SEED.user
            )
            assert outcome.state == "ok", outcome
            assert outcome.counts["converted"] == 2, outcome.counts
            assert outcome.counts["planning_only"] == 2, outcome.counts
            assert outcome.counts["skipped_duplicate"] == 0, outcome.counts

            created = [t.id for t in await _converted_tasks(sess, _SEED.run.id)]
            assert len(created) == 2
            for tid in created:
                trow = await sess.get(Task, tid)
                assert trow.status == "pending" and trow.created_reason == "ANALYSIS_FINDING"
                assert trow.finding_id is not None and trow.revision_sha == "f" * 64

            # G4 no-enqueue: NO AgentRun row references any converted task.
            run_rows = (
                await sess.execute(
                    select(AgentRun).where(
                        AgentRun.source_type == "task",
                        AgentRun.source_id.in_([str(t) for t in created]),
                    )
                )
            ).scalars().all()
            assert run_rows == [], "G4: a conversion must NEVER auto-enqueue a Run"
            await sess.commit()

            # Second invocation on the same run: both are skipped_duplicate, no new rows.
            outcome2 = await task_decomposition_service.convert(
                sess, project=_SEED.project, run=_SEED.run, agent=_SEED.agent, current_user=_SEED.user
            )
            assert outcome2.counts["converted"] == 0
            assert outcome2.counts["skipped_duplicate"] == 2, outcome2.counts
            still = [t.id for t in await _converted_tasks(sess, _SEED.run.id)]
            assert len(still) == 2, "dedup: a second invocation must not duplicate rows"


async def _converted_tasks(sess, run_id: uuid.UUID) -> list[Task]:
    rows = await sess.execute(
        select(Task).where(Task.analysis_run_id == run_id, Task.created_reason == "ANALYSIS_FINDING")
    )
    return list(rows.scalars().all())


@LIVE
async def test_f070_dedup_unique_is_last_resort_guard(db) -> None:
    """The f070 UNIQUE(analysis_run_id, finding_id) is the last-resort dedup guard
    on a racing write; NULL finding_id (manual tasks) never collides.

    Self-contained: this test creates its OWN run + finding (distinct from the
    shared seed the conversion test converts) so the first insert is guaranteed
    clean and only the second insert of the same pair hits the constraint.
    """
    await _seed(db)
    from sqlalchemy.exc import IntegrityError

    from app.dao.base import tenant_context
    from app.models.analysis import AnalysisFinding as AF
    from app.models.analysis import AnalysisRun as AR

    tenant = _SEED.tenant.id
    async with db() as sess:
        async with sess.begin():
            with tenant_context(tenant):
                local_run = AR(project_id=_SEED.project.id, revision_sha="c" * 64,
                               status="AN_COMPLETED", tenant_id=tenant)
                sess.add(local_run)
                await sess.flush()
                local_finding = AF(
                    analysis_run_id=local_run.id, severity="WARN", category="TECH_DEBT",
                    tag="FACT", summary="dedup subject", evidence={"anchors": ["d.py:1"]},
                    tenant_id=tenant,
                )
                sess.add(local_finding)
                await sess.flush()

                t1 = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                           project_id=_SEED.project.id, analysis_run_id=local_run.id,
                           finding_id=local_finding.id, revision_sha="c" * 64,
                           created_reason="ANALYSIS_FINDING", title="[TECH_DEBT] a")
                sess.add(t1)
                await sess.flush()  # first insert of the pair -> clean
                t2 = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                           project_id=_SEED.project.id, analysis_run_id=local_run.id,
                           finding_id=local_finding.id, revision_sha="c" * 64,
                           created_reason="ANALYSIS_FINDING", title="[TECH_DEBT] b")
                sess.add(t2)
                with pytest.raises(IntegrityError, match="uq_tasks_analysis_finding|unique"):
                    await sess.flush()  # second insert of the same pair -> rejected
        await sess.rollback()
        # Two NULL-finding (MANUAL) rows coexist freely (NULL finding_id is distinct).
        async with db() as sess:
            with tenant_context(tenant):
                m1 = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                          project_id=_SEED.project.id, title="manual-1")
                m2 = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                          project_id=_SEED.project.id, title="manual-2")
                sess.add_all([m1, m2])
                await sess.commit()


@LIVE
async def test_conversion_rejects_non_completed_run(db) -> None:
    """G1: conversion requires an AN_COMPLETED run; an open run is refused."""
    await _seed(db)
    from app.dao.base import tenant_context
    from app.models.analysis import AnalysisRun as AR

    tenant = _SEED.tenant.id
    async with db() as sess:
        with tenant_context(tenant):
            open_run = AR(project_id=_SEED.project.id, revision_sha="e" * 64, status="AN_OPEN", tenant_id=tenant)
            sess.add(open_run)
            await sess.flush()
            await sess.commit()
            outcome = await task_decomposition_service.convert(
                sess, project=_SEED.project, run=open_run, agent=_SEED.agent, current_user=_SEED.user
            )
            assert outcome.state == "failed" and outcome.code == "TD_RUN_NOT_COMPLETED", outcome


@LIVE
async def test_execution_gate_blocks_task_with_unmet_dependency(db) -> None:
    """§5.4 end-to-end gate through the real enqueue boundary:

    - a dependency-blocked todo task raises ``TaskBlockedError`` and NO Run is
      started (the task stays ``pending``);
    - a ready task (no unmet deps) passes the gate and enqueues.
    """
    await _seed(db)
    from unittest.mock import AsyncMock, patch

    from app.config import get_settings
    from app.dao.base import tenant_context
    from app.models.task import Task as T
    from app.services.agent_runtime.contracts import RunHandle
    from app.services.task_executor import TaskBlockedError, enqueue_task_runtime

    tenant = _SEED.tenant.id
    agent = _SEED.agent

    handle = RunHandle(
        tenant_id=agent.tenant_id, run_id=uuid.uuid4(), thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(), runtime_type="langgraph", created=True,
    )

    async with db() as sess, sess.begin():
        with tenant_context(tenant):
            from app.models.llm import LLMModel

            # A real model row: agents.primary_model_id is a hard FK, so the
            # agent needs a concrete model to clear the enqueue model-gate.
            model = LLMModel(
                tenant_id=tenant, provider="anthropic", model="claude-opus-4-6",
                api_key_encrypted="enc-test", label="gate-model",
            )
            sess.add(model)
            await sess.flush()
            agent.primary_model_id = model.id
            sess.add(agent)  # persist the primary_model_id for the model gate
            pending_dep = T(agent_id=agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                            project_id=_SEED.project.id, title="gate-dep", status="pending")
            blocked = T(agent_id=agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                        project_id=_SEED.project.id, title="gate-blocked", status="pending")
            ready = T(agent_id=agent.id, created_by=_SEED.user.id, tenant_id=tenant,
                      project_id=_SEED.project.id, title="gate-ready", status="pending")
            for t in (pending_dep, blocked, ready):
                sess.add(t)
            await sess.flush()
            # blocked depends on a pending task -> gate must refuse it.
            edge = await task_graph_service.add_edge(
                sess, task_id=blocked.id, depends_on_task_id=pending_dep.id, tenant_id=tenant
            )
            assert edge.state == "added", edge
            await sess.commit()

    settings = get_settings()
    v2 = type(settings)(_env_file=None, AGENT_RUNTIME_V2_ENABLED=True, AGENT_RUNTIME_V2_SOURCE_TYPES="task")

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        # Blocked task: the gate raises BEFORE any Run is started.
        async with db() as sess:
            with tenant_context(tenant):
                with pytest.raises(TaskBlockedError) as excinfo:
                    await enqueue_task_runtime(sess, task=blocked, agent=agent, settings_override=v2)
                assert pending_dep.id in excinfo.value.reason
                assert start_run.await_count == 0, "a blocked task must not start a Run"
                await sess.rollback()

        # Ready task (no unmet deps): the gate passes and enqueues.
        async with db() as sess:
            with tenant_context(tenant):
                result = await enqueue_task_runtime(sess, task=ready, agent=agent, settings_override=v2)
                assert result is handle
                assert start_run.await_count == 1, "a ready task must start exactly one Run"
                await sess.rollback()


@LIVE
async def test_r1_concurrent_inverse_edges_at_most_one_no_cycle(db) -> None:
    """Design §11 R1 / §8: two sessions concurrently insert inverse edges
    (``A->B`` and ``B->A``) into one project.  The §5.2 reachability check is a
    read-then-write over the project edge set; without a serialization point,
    under Postgres READ COMMITTED both writers read the pre-commit edge set,
    both pass the check, and both commit a 2-cycle that ``UNIQUE(self-pair)``
    + the self ``CHECK`` cannot catch.  The project-scoped advisory lock
    (``_lock_project_graph``) closes the window: the second writer blocks on
    the lock until the first commits, then re-reads the committed edge and is
    refused.  At most one edge lands and no cycle exists afterward."""
    import asyncio

    from sqlalchemy import select

    from app.dao import task_dependency_dao
    from app.dao.base import tenant_context

    await _seed(db)
    tenant = _SEED.tenant.id

    # Two fresh, project-scoped todo tasks (unique per run -> re-runnable).
    suffix = uuid.uuid4().hex[:8]
    a = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
             project_id=_SEED.project.id, title=f"r1-a-{suffix}", status="pending")
    b = Task(agent_id=_SEED.agent.id, created_by=_SEED.user.id, tenant_id=tenant,
             project_id=_SEED.project.id, title=f"r1-b-{suffix}", status="pending")
    async with db() as sess:
        async with sess.begin():
            with tenant_context(tenant):
                sess.add_all([a, b])
                await sess.flush()

    async def insert_edge(from_id: uuid.UUID, to_id: uuid.UUID):
        """``from_id depends on to_id`` in its OWN session + transaction."""
        async with db() as s:
            async with s.begin():
                with tenant_context(tenant):
                    return await task_graph_service.add_edge(
                        s, task_id=from_id, depends_on_task_id=to_id, tenant_id=tenant
                    )

    # Fire both inverse inserts concurrently.  The advisory lock serializes the
    # check+write window; the loser re-reads the committed edge and is refused.
    results = await asyncio.gather(
        insert_edge(a.id, b.id),  # a depends on b
        insert_edge(b.id, a.id),  # b depends on a
    )
    added = [r for r in results if r.state == "added"]
    blocked = [r for r in results if r.state == "blocked"]
    assert len(added) == 1 and len(blocked) == 1, [r.state for r in results]
    # The loser is refused specifically as a CYCLE (the lock made it see the
    # winner's committed edge), not some unrelated rejection.
    assert blocked[0].code == "GRAPH_CYCLE", blocked[0].code

    # No 2-cycle: the two endpoints are NOT mutually dependent.  Read the
    # committed edge set fresh in a new session.
    async with db() as s:
        with tenant_context(tenant):
            ab = await task_dependency_dao.list_dependencies(a.id, db=s)
            ba = await task_dependency_dao.list_dependencies(b.id, db=s)
            a_to_b = any(e.depends_on_task_id == b.id for e in ab)
            b_to_a = any(e.depends_on_task_id == a.id for e in ba)
            n_edges = (await s.execute(
                select(task_dependency_dao.model.id).where(
                    task_dependency_dao.model.task_id.in_([a.id, b.id]),
                    task_dependency_dao.model.depends_on_task_id.in_([a.id, b.id]),
                )
            )).all()
    assert not (a_to_b and b_to_a), "R1 violated: a 2-cycle was committed"
    assert len(n_edges) == 1, f"expected exactly one committed edge, got {len(n_edges)}"

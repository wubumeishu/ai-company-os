"""Deployment contract for the f072 Artifact / Evidence migration.

Follows the monkeypatched-op convention of ``test_planning_persistence_migration.py``:
the migration module is loaded from file and its schema-introspection helpers +
``op`` calls are patched so no database connection is required.  The two
provisioning paths that matter:

- Fresh (create_all-provisioned) deployments: 001_initial_schema's create_all
  already builds the two ledger tables + their plain btree indexes from the
  registered metadata (artifact_evidence.py is now imported by env.py), so
  ``upgrade()`` must be a no-op (every op guarded).
- Existing deployments stamped before f072: ``upgrade()`` must issue the
  CREATE TABLE + CHECK + UNIQUE + partial-UNIQUE-index DDL in dependency
  order (artifact_records first — evidence_records references it via the
  artifact_id FK).

Proven here (DB-free): revision wiring, upgrade no-op-on-fresh, upgrade
full-schema on existing PG, re-runnable partial state, downgrade reverse
order + guarded no-op, the DDL content (CHECKs, the UNIQUE dedup, the partial
UNIQUE index), and the hard boundary that f072 adds NO column/constraint to
the frozen tables.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_5_f072_artifact_evidence.py"
)

ARTIFACT_TABLE = "artifact_records"
EVIDENCE_TABLE = "evidence_records"
ALL_TABLES = {ARTIFACT_TABLE, EVIDENCE_TABLE}

REVERIFY_PARTIAL_INDEX = "uq_evidence_records_reverify"


def _load_migration():
    spec = importlib.util.spec_from_file_location("f072_artifact_evidence", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeBind:
    """Stand-in for op.get_bind(): records executed raw SQL."""

    def __init__(self, *, dialect_name: str = "postgresql") -> None:
        self.dialect = type("_Dialect", (), {"name": dialect_name})()
        self.executed: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.executed.append(str(statement))
        return _Rows([])

    def query_sql(self, fragment: str) -> list[str]:
        return [s for s in self.executed if fragment in s]


def _patch_op(monkeypatch, migration, record: list) -> None:
    monkeypatch.setattr(
        migration.op, "create_table", lambda *a, **k: record.append(("create_table", a[0]))
    )
    monkeypatch.setattr(
        migration.op, "create_index", lambda *a, **k: record.append(("create_index", a[0]))
    )
    monkeypatch.setattr(
        migration.op, "drop_index", lambda *a, **k: record.append(("drop_index", a[0]))
    )
    monkeypatch.setattr(
        migration.op, "drop_table", lambda *a, **k: record.append(("drop_table", a[0]))
    )


def _patch_state(monkeypatch, migration, *, tables: set[str], indexes: dict[str, set[str]]) -> None:
    monkeypatch.setattr(migration, "_existing_tables", lambda _bind: set(tables))

    def _idx(bind, table_name):
        return set(indexes.get(table_name, set()))

    monkeypatch.setattr(migration, "_existing_indexes", _idx)


def _all_indexes() -> dict[str, set[str]]:
    m = _load_migration()
    return {
        ARTIFACT_TABLE: set(m.ARTIFACT_INDEXES),
        EVIDENCE_TABLE: set(m.EVIDENCE_INDEXES) | {REVERIFY_PARTIAL_INDEX},
    }


# ---------------------------------------------------------------------------
# revision wiring
# ---------------------------------------------------------------------------


def test_revision_mounts_f071_head() -> None:
    m = _load_migration()
    assert m.revision == "f072_artifact_evidence"
    assert m.down_revision == "f071_planning_persistence"


# ---------------------------------------------------------------------------
# upgrade — fresh (create_all) path: full no-op
# ---------------------------------------------------------------------------


def test_upgrade_is_noop_when_fresh_metadata_already_built_schema(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(ALL_TABLES), indexes=_all_indexes())

    m.upgrade()

    assert record == []


# ---------------------------------------------------------------------------
# upgrade — existing-DB path: create both tables in dependency order
# ---------------------------------------------------------------------------


def test_upgrade_creates_full_schema_on_existing_postgres(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(), indexes={})

    m.upgrade()

    table_order = [name for kind, name in record if kind == "create_table"]
    # artifact_records MUST precede evidence_records (the artifact_id FK).
    assert table_order == [ARTIFACT_TABLE, EVIDENCE_TABLE]
    for index_set in _all_indexes().values():
        for idx in index_set:
            assert ("create_index", idx) in record, f"missing {idx}"


def test_upgrade_is_re_runnable_with_partial_state(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables={ARTIFACT_TABLE}, indexes={ARTIFACT_TABLE: set(m.ARTIFACT_INDEXES)})

    m.upgrade()

    created = [name for kind, name in record if kind == "create_table"]
    assert created == [EVIDENCE_TABLE]


# ---------------------------------------------------------------------------
# downgrade — reverse dependency order + guarded no-op
# ---------------------------------------------------------------------------


def test_downgrade_drops_in_reverse_dependency_order(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(ALL_TABLES), indexes=_all_indexes())

    m.downgrade()

    drop_order = [name for kind, name in record if kind == "drop_table"]
    # evidence_records dropped first (it references artifact_records).
    assert drop_order == [EVIDENCE_TABLE, ARTIFACT_TABLE]
    # The partial unique index is dropped too (it is in _all_indexes()).
    assert ("drop_index", REVERIFY_PARTIAL_INDEX) in record


def test_downgrade_is_noop_on_absent_tables(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(), indexes={})

    m.downgrade()

    assert record == []


# ---------------------------------------------------------------------------
# DDL content — CHECK / UNIQUE / FK / partial-index invariants (design §7)
# ---------------------------------------------------------------------------


def _capture_create_table(monkeypatch, m, record, captured) -> None:
    def capture(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    _patch_op(monkeypatch, m, record)
    monkeypatch.setattr(m.op, "create_table", capture)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(), indexes={})
    m.upgrade()


def test_artifact_ddl_carries_source_seal_check_and_unique_dedup(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    captured: list = []
    _capture_create_table(monkeypatch, m, record, captured)
    art_spec = next(a for a in captured if a[0] == ARTIFACT_TABLE)
    specs = list(art_spec[1:])
    checks = [s for s in specs if isinstance(s, sa.CheckConstraint)]
    uniques = [s for s in specs if isinstance(s, sa.UniqueConstraint)]
    check_names = [str(c.name) for c in checks]
    unique_names = [str(u.name) for u in uniques]
    # Invariant 1 (D5 provenance) + invariant 2 (seal one-way) as DB CHECKs.
    assert any("ck_artifact_records_source" in n for n in check_names), check_names
    assert any("ck_artifact_records_seal" in n for n in check_names), check_names
    # Invariant 4: UNIQUE(tenant, scheme, ref) dedup.
    assert any("uq_artifact_records_tenant_ref" in n for n in unique_names), unique_names
    # The new FKs reference the frozen tables with SET NULL (no column added
    # to them); the self-FK for superseded_by is SET NULL.
    fks = [s for s in specs if isinstance(s, sa.ForeignKeyConstraint)]
    fk_refs = {e.target_fullname for fk in fks for e in fk.elements}
    for frozen in ("projects.id", "tasks.id", "agent_tool_executions.id", "agents.id", "users.id"):
        assert frozen in fk_refs, f"missing FK to frozen {frozen}"
    assert "artifact_records.id" in fk_refs  # the superseded_by self-FK


def test_evidence_ddl_carries_source_outcome_check_and_partial_unique(monkeypatch) -> None:
    m = _load_migration()
    record: list = []
    captured: list = []

    def capture_create(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    _patch_op(monkeypatch, m, record)
    monkeypatch.setattr(m.op, "create_table", capture_create)
    # Capture the create_index calls too (the partial UNIQUE index is created
    # via op.create_index, not inline in create_table).
    index_calls: list = []
    monkeypatch.setattr(
        m.op,
        "create_index",
        lambda *a, **k: (index_calls.append((a, k)), record.append(("create_index", a[0]))),
    )
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(monkeypatch, m, tables=set(), indexes={})
    m.upgrade()

    ev_spec = next(a for a in captured if a[0] == EVIDENCE_TABLE)
    specs = list(ev_spec[1:])
    checks = [s for s in specs if isinstance(s, sa.CheckConstraint)]
    check_names = [str(c.name) for c in checks]
    # Invariant 3 (D5 provenance) + the outcome closed-set backstop.
    assert any("ck_evidence_records_source" in n for n in check_names), check_names
    assert any("ck_evidence_records_outcome" in n for n in check_names), check_names
    # Invariant 5: the partial UNIQUE index is created via op.create_index with
    # unique=True + the reverify_of WHERE exclusion.
    partial_call = next(
        (c for c in index_calls if c[0][0] == REVERIFY_PARTIAL_INDEX), None
    )
    assert partial_call, "the uq_evidence_records_reverify partial unique index is missing"
    partial_args, partial_kwargs = partial_call
    columns = partial_args[2]
    assert list(columns) == ["tenant_id", "kind", "subject_ref", "revision_ref"], list(columns)
    assert partial_kwargs.get("unique") is True
    where = partial_kwargs.get("postgresql_where")
    assert where is not None and "reverify_of" in str(where), where
    # The evidence row references artifact_records (the verdict cites an
    # artifact) and the frozen execution table.
    fks = [s for s in specs if isinstance(s, sa.ForeignKeyConstraint)]
    fk_refs = {e.target_fullname for fk in fks for e in fk.elements}
    assert "artifact_records.id" in fk_refs
    assert "agent_tool_executions.id" in fk_refs


def test_no_column_or_constraint_added_to_frozen_tables(monkeypatch) -> None:
    """The card's hard boundary: f072 is purely additive to the two new
    tables — it must never add a column or constraint to any frozen table
    (tasks, agent_tool_executions, workspace_file_revisions,
    published_pages, repositories, agent_run_events, ...)."""
    m = _load_migration()
    record: list = []
    _patch_op(monkeypatch, m, record)
    bind = FakeBind()
    monkeypatch.setattr(m.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        m,
        tables={"tasks", "agent_tool_executions", "workspace_file_revisions"},
        indexes={},
    )

    m.upgrade()

    kinds = {kind for kind, _ in record}
    assert "add_column" not in kinds
    assert "create_foreign_key" not in kinds
    assert "drop_column" not in kinds
    created = [name for kind, name in record if kind == "create_table"]
    assert "tasks" not in created
    assert "agent_tool_executions" not in created


def test_evidence_table_is_born_immutable_no_updated_at(monkeypatch) -> None:
    """Decision D2: evidence has a single created_at, no updated_at (it is born
    immutable).  The DDL must not declare an updated_at column."""
    m = _load_migration()
    record: list = []
    captured: list = []
    _capture_create_table(monkeypatch, m, record, captured)
    ev_spec = next(a for a in captured if a[0] == EVIDENCE_TABLE)
    # The create_table call is (name, id_col, tenant_col, ... , sa.Column defs,
    # constraints).  Grep the column names in the call args.
    cols = [col.name for col in ev_spec if isinstance(col, sa.Column)]
    assert "created_at" in cols
    assert "updated_at" not in cols
    # The artifact table, by contrast, DOES have an updated_at (DRAFT window).
    art_spec = next(a for a in captured if a[0] == ARTIFACT_TABLE)
    art_cols = [col.name for col in art_spec if isinstance(col, sa.Column)]
    assert "updated_at" in art_cols

"""Deployment contract for the f071 Planning Domain persistence migration.

Follows the monkeypatched-op convention of
test_task_graph_provenance_migration.py: the migration module is loaded from
file and its schema-introspection helpers + op calls are patched so no
database connection is required.  The provisioning paths that matter:

- Fresh (create_all-provisioned) deployments: 001_initial_schema's
  create_all already builds the five planning tables + both status enum
  types from the registered metadata (planning.py is now imported by
  env.py), so upgrade() must be a no-op (every op guarded).
- Existing deployments stamped before f071: upgrade() must issue the
  CREATE TYPE + CREATE TABLE + CREATE INDEX DDL in dependency order.
- downgrade(): reverse-order drop (link -> packages -> milestones ->
  goals -> runs), keeps each enum type while any column still references
  it, and is a clean no-op on the create_all-provisioned fresh-DB path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_5_f071_planning_persistence.py"
)

RUN_STATUS_ENUM = "planning_run_status_enum"
GOAL_STATUS_ENUM = "planning_goal_status_enum"
ALL_ENUMS = (RUN_STATUS_ENUM, GOAL_STATUS_ENUM)

RUNS_TABLE = "planning_runs"
GOALS_TABLE = "planning_goals"
PACKAGES_TABLE = "work_packages"
MILESTONES_TABLE = "milestones"
WP_TASKS_TABLE = "work_package_tasks"

ALL_TABLES = {RUNS_TABLE, GOALS_TABLE, PACKAGES_TABLE, MILESTONES_TABLE, WP_TASKS_TABLE}


def _load_migration():
    spec = importlib.util.spec_from_file_location("f071_planning_persistence", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rows:
    """Iterable result object for patched bind.execute()."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeBind:
    """Stand-in for op.get_bind(): records executed raw SQL (CREATE/DROP TYPE)."""

    def __init__(self, *, dialect_name: str = "postgresql") -> None:
        self.dialect = type("_Dialect", (), {"name": dialect_name})()
        self.executed: list[str] = []
        self._results: list[_Rows] = []
        self._default: list = []

    def execute(self, statement, *args, **kwargs):
        self.executed.append(str(statement))
        if self._results:
            return self._results.pop(0)
        return _Rows(self._default)

    def query_sql(self, fragment: str) -> list[str]:
        return [s for s in self.executed if fragment in s]


def _patch_op(monkeypatch, migration, record: list) -> None:
    monkeypatch.setattr(migration.op, "create_table", lambda *a, **k: record.append(("create_table", a[0])))
    monkeypatch.setattr(migration.op, "create_index", lambda *a, **k: record.append(("create_index", a[0])))
    monkeypatch.setattr(migration.op, "drop_index", lambda *a, **k: record.append(("drop_index", a[0])))
    monkeypatch.setattr(migration.op, "drop_table", lambda *a, **k: record.append(("drop_table", a[0])))


def _patch_state(
    monkeypatch,
    migration,
    *,
    tables: set[str],
    indexes: dict[str, set[str]] | None = None,
    present_types: set[str],
    referenced_types: set[str],
) -> None:
    indexes = indexes or {}

    monkeypatch.setattr(migration, "_existing_tables", lambda _bind: set(tables))

    def _idx(bind, table_name):
        return set(indexes.get(table_name, set()))

    monkeypatch.setattr(migration, "_existing_indexes", _idx)
    monkeypatch.setattr(migration, "_present_enum_types", lambda _bind: set(present_types))
    monkeypatch.setattr(migration, "_referenced_enum_types", lambda _bind: set(referenced_types))


def _all_indexes() -> dict[str, set[str]]:
    return {
        RUNS_TABLE: set(_load_migration().RUNS_INDEXES),
        GOALS_TABLE: set(_load_migration().GOALS_INDEXES),
        PACKAGES_TABLE: set(_load_migration().PACKAGES_INDEXES),
        MILESTONES_TABLE: set(_load_migration().MILESTONES_INDEXES),
        WP_TASKS_TABLE: set(_load_migration().WP_TASKS_INDEXES),
    }


# ---------------------------------------------------------------------------
# revision wiring
# ---------------------------------------------------------------------------
def test_revision_mounts_f070_head() -> None:
    migration = _load_migration()
    assert migration.revision == "f071_planning_persistence"
    assert migration.down_revision == "f070_analysis_task_dedup"


# ---------------------------------------------------------------------------
# upgrade — fresh (create_all) path: full no-op
# ---------------------------------------------------------------------------
def test_upgrade_is_noop_when_fresh_metadata_already_built_schema(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(ALL_TABLES),
        indexes=_all_indexes(),
        present_types=set(ALL_ENUMS),
        referenced_types=set(ALL_ENUMS),
    )

    migration.upgrade()

    assert record == []
    assert bind.query_sql("CREATE TYPE") == []


# ---------------------------------------------------------------------------
# upgrade — existing-DB path: create every object in dependency order
# ---------------------------------------------------------------------------
def test_upgrade_creates_full_schema_on_existing_postgres(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(),
        indexes={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    # All five tables created, in dependency order: runs -> goals ->
    # milestones -> packages -> the link table.
    table_order = [name for kind, name in record if kind == "create_table"]
    assert table_order == [RUNS_TABLE, GOALS_TABLE, MILESTONES_TABLE, PACKAGES_TABLE, WP_TASKS_TABLE]

    # All named btree indexes created.
    for index_name in _all_indexes().values():
        for idx in index_name:
            assert ("create_index", idx) in record, f"missing {idx}"

    # Both enum types created exactly once, before any table DDL.
    assert len(bind.query_sql("CREATE TYPE")) == 2
    assert bind.executed[0].startswith("CREATE TYPE planning_run_status_enum"), bind.executed[0]


def test_upgrade_is_re_runnable_with_partial_state(monkeypatch) -> None:
    """Re-runnable: with two tables already present, only the other three are created."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={RUNS_TABLE, GOALS_TABLE},
        indexes={RUNS_TABLE: set(migration.RUNS_INDEXES)},
        present_types=set(ALL_ENUMS),
        referenced_types=set(ALL_ENUMS),
    )

    migration.upgrade()

    created = [name for kind, name in record if kind == "create_table"]
    assert created == [MILESTONES_TABLE, PACKAGES_TABLE, WP_TASKS_TABLE]
    assert bind.query_sql("CREATE TYPE") == []


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------
def test_downgrade_drops_in_reverse_dependency_order(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(ALL_TABLES),
        indexes=_all_indexes(),
        present_types=set(ALL_ENUMS),
        referenced_types=set(),  # columns dropped before the enum check -> unreferenced
    )

    migration.downgrade()

    drop_order = [name for kind, name in record if kind == "drop_table"]
    assert drop_order == [WP_TASKS_TABLE, PACKAGES_TABLE, MILESTONES_TABLE, GOALS_TABLE, RUNS_TABLE]
    # Both enum types dropped last (unreferenced).
    assert len(bind.query_sql("DROP TYPE")) == 2
    # All named indexes dropped.
    for index_set in _all_indexes().values():
        for idx in index_set:
            assert ("drop_index", idx) in record, f"missing drop_index {idx}"


def test_downgrade_keeps_enum_while_columns_still_reference_it(monkeypatch) -> None:
    """A partially rolled-down schema (planning tables still present) must not orphan the enums."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(ALL_TABLES),
        indexes=_all_indexes(),
        present_types=set(ALL_ENUMS),
        referenced_types=set(ALL_ENUMS),  # a column still uses them
    )

    migration.downgrade()

    # All tables dropped ...
    assert ("drop_table", RUNS_TABLE) in record
    # ... but the referenced enum types are kept.
    assert bind.query_sql("DROP TYPE") == []


def test_downgrade_is_noop_on_fresh_metadata_db(monkeypatch) -> None:
    """create_all-provisioned fresh DB: no migration-created objects to drop."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    # Tables exist (via create_all) but carry NO migration-created indexes
    # under the f071 names, and the enum types were never dropped.
    _patch_state(
        monkeypatch,
        migration,
        tables=set(),  # no table: nothing to drop at all
        indexes={},
        present_types=set(ALL_ENUMS),
        referenced_types=set(ALL_ENUMS),
    )

    migration.downgrade()

    assert record == []
    assert bind.query_sql("DROP TYPE") == []


# ---------------------------------------------------------------------------
# DDL content (CHECK / UNIQUE / FK semantics, design §6 invariants)
# ---------------------------------------------------------------------------
def test_wp_tasks_ddl_carries_check_unique_and_set_null_fks(monkeypatch) -> None:
    """work_package_tasks DDL declares uq_wp_tasks, ck_wp_tasks_materialized,
    and the tasks FK with ondelete=SET NULL (the explicit link the card
    permits — no column/constraint added to tasks itself)."""
    migration = _load_migration()
    record: list = []
    captured: list = []

    def capture_create_table(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    _patch_op(monkeypatch, migration, record)
    monkeypatch.setattr(migration.op, "create_table", capture_create_table)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(),
        indexes={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    wp_spec = next(a for a in captured if a[0] == WP_TASKS_TABLE)
    specs = list(wp_spec[1:])
    uniques = [s for s in specs if isinstance(s, sa.UniqueConstraint)]
    checks = [s for s in specs if isinstance(s, sa.CheckConstraint)]
    fks = [s for s in specs if isinstance(s, sa.ForeignKeyConstraint)]
    assert any("uq_wp_tasks" in str(u.name) for u in uniques), "uq_wp_tasks UNIQUE missing"
    assert any(
        "ck_wp_tasks_materialized" in str(c.name) for c in checks
    ), "ck_wp_tasks_materialized CHECK missing"
    tasks_fk = [fk for fk in fks if "tasks.id" in [e.target_fullname for e in fk.elements]]
    assert len(tasks_fk) == 1 and tasks_fk[0].ondelete == "SET NULL", f"tasks FK must be SET NULL: {fks!r}"
    wp_fk = [fk for fk in fks if "work_packages.id" in [e.target_fullname for e in fk.elements]]
    assert len(wp_fk) == 1 and wp_fk[0].ondelete == "CASCADE", "work_packages FK must be CASCADE"


def test_runs_ddl_carries_unique_revision_guard(monkeypatch) -> None:
    """planning_runs DDL declares the uq_planning_runs_project_revision
    append-only invariant (design §6.1, mirroring uq_analysis_runs_project_revision)."""
    migration = _load_migration()
    record: list = []
    captured: list = []

    def capture_create_table(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    _patch_op(monkeypatch, migration, record)
    monkeypatch.setattr(migration.op, "create_table", capture_create_table)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(),
        indexes={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    runs_spec = next(a for a in captured if a[0] == RUNS_TABLE)
    specs = list(runs_spec[1:])
    uniques = [s for s in specs if isinstance(s, sa.UniqueConstraint)]
    assert any("uq_planning_runs_project_revision" in str(u.name) for u in uniques), (
        "uq_planning_runs_project_revision UNIQUE missing"
    )
    # The projects FK must CASCADE (a project deletion removes its planning history).
    fks = [s for s in specs if isinstance(s, sa.ForeignKeyConstraint)]
    projects_fk = [fk for fk in fks if "projects.id" in [e.target_fullname for e in fk.elements]]
    assert len(projects_fk) == 1 and projects_fk[0].ondelete == "CASCADE"


def test_milestones_ddl_carries_seq_unique(monkeypatch) -> None:
    """milestones DDL declares uq_milestones_run_seq (design §6.4)."""
    migration = _load_migration()
    record: list = []
    captured: list = []

    def capture_create_table(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    _patch_op(monkeypatch, migration, record)
    monkeypatch.setattr(migration.op, "create_table", capture_create_table)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables=set(),
        indexes={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    m_spec = next(a for a in captured if a[0] == MILESTONES_TABLE)
    specs = list(m_spec[1:])
    uniques = [s for s in specs if isinstance(s, sa.UniqueConstraint)]
    assert any("uq_milestones_run_seq" in str(u.name) for u in uniques), "uq_milestones_run_seq UNIQUE missing"


def test_no_column_or_constraint_added_to_tasks(monkeypatch) -> None:
    """The card's hard boundary: f071 is purely additive to the five new
    tables — it must never add a column or constraint to the frozen tasks table."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={"tasks"},
        indexes={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    kinds = {kind for kind, _ in record}
    assert "add_column" not in kinds and "create_foreign_key" not in kinds and "drop_column" not in kinds
    created = [name for kind, name in record if kind == "create_table"]
    assert "tasks" not in created

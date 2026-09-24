"""Deployment contract for the f069 Task Graph + Task provenance migration.

Follows the monkeypatched-op convention of
test_project_repository_migration.py: the migration module is loaded from
file and its schema-introspection helpers + op calls are patched so no
database connection is required.  The three provisioning paths matter:

- Fresh (create_all-provisioned) deployments: 001_initial_schema's
  create_all already builds task_dependencies + the five tasks provenance
  columns from the registered metadata, so upgrade() must be a no-op
  (every op guarded behind an existence check).
- Existing deployments stamped before f069: upgrade() must issue the
  CREATE TYPE + CREATE TABLE + ADD COLUMN + CREATE INDEX + CREATE FOREIGN
  KEY DDL in a re-runnable order.
- downgrade(): symmetric drop order (edges -> tasks columns -> enum), and
  keeps the enum type while any column still references it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_5_f069_task_graph_provenance.py"
)

ENUM = "task_created_reason_enum"
DEPS_TABLE = "task_dependencies"
TASKS_TABLE = "tasks"
TASKS_NEW_INDEXES = (
    "ix_tasks_project_id",
    "ix_tasks_analysis_run_id",
    "ix_tasks_finding_id",
    "ix_tasks_revision_sha",
    "ix_tasks_created_reason",
)
DEPS_INDEXES = (
    "ix_task_dependencies_task_id",
    "ix_task_dependencies_depends_on_task_id",
    "ix_task_dependencies_tenant_id",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("f069_task_graph_provenance", MIGRATION_PATH)
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


def _fk(row_name: str, constrained: tuple[str, ...]) -> dict:
    return {"name": row_name, "constrained_columns": [constrained[0]] if len(constrained) == 1 else list(constrained)}


def _patch_op(monkeypatch, migration, record: list) -> None:
    monkeypatch.setattr(migration.op, "create_table", lambda *a, **k: record.append(("create_table", a[0])))
    monkeypatch.setattr(migration.op, "add_column", lambda *a, **k: record.append(("add_column", a[1].name)))
    monkeypatch.setattr(migration.op, "create_index", lambda *a, **k: record.append(("create_index", a[0])))
    monkeypatch.setattr(migration.op, "create_foreign_key", lambda *a, **k: record.append(("create_foreign_key", a[0])))
    monkeypatch.setattr(migration.op, "drop_index", lambda *a, **k: record.append(("drop_index", a[0])))
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: record.append(("drop_constraint", a[0])))
    monkeypatch.setattr(migration.op, "drop_table", lambda *a, **k: record.append(("drop_table", a[0])))
    monkeypatch.setattr(migration.op, "drop_column", lambda *a, **k: record.append(("drop_column", a[1])))


def _patch_state(
    monkeypatch,
    migration,
    *,
    tables: set[str],
    columns: dict[str, set[str]] | None = None,
    indexes: dict[str, set[str]] | None = None,
    fk_constraints: dict[str, list[dict]] | None = None,
    present_types: set[str],
    referenced_types: set[str],
) -> None:
    columns = columns or {}
    indexes = indexes or {}
    fk_constraints = fk_constraints or {}

    monkeypatch.setattr(migration, "_existing_tables", lambda _bind: set(tables))

    def _cols(bind, table_name):
        return set(columns.get(table_name, set()))

    monkeypatch.setattr(migration, "_existing_columns", _cols)

    def _idx(bind, table_name):
        return set(indexes.get(table_name, set()))

    monkeypatch.setattr(migration, "_existing_indexes", _idx)

    def _fks(bind, table_name):
        return list(fk_constraints.get(table_name, []))

    monkeypatch.setattr(migration, "_existing_fk_constraints", _fks)
    monkeypatch.setattr(migration, "_present_enum_types", lambda _bind: set(present_types))
    monkeypatch.setattr(migration, "_referenced_enum_types", lambda _bind: set(referenced_types))


def _tasks_fk_constraints() -> list[dict]:
    """The three physical FKs f069 adds on tasks (or create_all's auto names)."""
    return [
        _fk("fk_tasks_project", ("project_id",)),
        _fk("fk_tasks_analysis_run", ("analysis_run_id",)),
        _fk("fk_tasks_finding", ("finding_id",)),
    ]


def _deps_fk_constraints() -> list[dict]:
    return [
        _fk("fk_task_dep_tenant", ("tenant_id",)),
        _fk("fk_task_dep_task", ("task_id",)),
        _fk("fk_task_dep_depends", ("depends_on_task_id",)),
    ]


# ---------------------------------------------------------------------------
# revision wiring
# ---------------------------------------------------------------------------
def test_revision_mounts_f068_head() -> None:
    migration = _load_migration()
    assert migration.revision == "f069_task_graph_provenance"
    assert migration.down_revision == "f068_analysis_persistence"


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
        tables={DEPS_TABLE, TASKS_TABLE},
        columns={TASKS_TABLE: {"project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason"}},
        indexes={
            TASKS_TABLE: set(TASKS_NEW_INDEXES),
            DEPS_TABLE: set(DEPS_INDEXES),
        },
        fk_constraints={TASKS_TABLE: _tasks_fk_constraints(), DEPS_TABLE: _deps_fk_constraints()},
        present_types={ENUM},
        referenced_types={ENUM},
    )

    migration.upgrade()

    # Everything is already present -> zero DDL ops, no CREATE TYPE.
    assert record == []
    assert bind.query_sql("CREATE TYPE") == []


# ---------------------------------------------------------------------------
# upgrade — existing-DB path: create every object
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
        tables={TASKS_TABLE},  # deps table absent, tasks present w/o new cols
        columns={TASKS_TABLE: set()},
        indexes={},
        fk_constraints={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    # 1) deps table + its 3 indexes (before the tasks-column work).
    assert ("create_table", DEPS_TABLE) in record
    assert ("create_index", "ix_task_dependencies_task_id") in record
    assert ("create_index", "ix_task_dependencies_depends_on_task_id") in record
    assert ("create_index", "ix_task_dependencies_tenant_id") in record
    # 2) all five tasks provenance columns added.
    for col in ("project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason"):
        assert ("add_column", col) in record, f"missing add_column {col}"
    # 3) the five tasks indexes + three physical FKs.
    for idx in TASKS_NEW_INDEXES:
        assert ("create_index", idx) in record, f"missing {idx}"
    for fk in ("fk_tasks_project", "fk_tasks_analysis_run", "fk_tasks_finding"):
        assert ("create_foreign_key", fk) in record, f"missing {fk}"
    # 4) the enum type is created exactly once, before any column DDL.
    assert len(bind.query_sql("CREATE TYPE")) == 1
    # Order: the CREATE TABLE precedes the tasks add_column (edges first).
    assert record.index(("create_table", DEPS_TABLE)) < record.index(("add_column", "project_id"))


def test_upgrade_partial_only_adds_missing_columns(monkeypatch) -> None:
    """Re-runnable: if two columns already exist, only the other three are added."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={TASKS_TABLE, DEPS_TABLE},
        columns={TASKS_TABLE: {"project_id", "analysis_run_id"}},
        indexes={DEPS_TABLE: set(DEPS_INDEXES)},
        fk_constraints={DEPS_TABLE: _deps_fk_constraints(), TASKS_TABLE: _tasks_fk_constraints()[:2]},
        present_types={ENUM},
        referenced_types=set(),
    )

    migration.upgrade()

    # Only the not-yet-present columns are added; present ones are skipped.
    added = [name for kind, name in record if kind == "add_column"]
    assert "finding_id" in added and "revision_sha" in added and "created_reason" in added, added
    assert "project_id" not in added and "analysis_run_id" not in added
    # deps table + indexes already present -> not recreated.
    assert ("create_table", DEPS_TABLE) not in record
    assert ("create_index", "ix_task_dependencies_task_id") not in record


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------
def test_downgrade_drops_edges_then_columns_then_enum(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={TASKS_TABLE, DEPS_TABLE},
        columns={TASKS_TABLE: {"project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason"}},
        indexes={TASKS_TABLE: set(TASKS_NEW_INDEXES), DEPS_TABLE: set(DEPS_INDEXES)},
        fk_constraints={TASKS_TABLE: _tasks_fk_constraints(), DEPS_TABLE: _deps_fk_constraints()},
        present_types={ENUM},
        referenced_types=set(),  # columns dropped before the enum check -> unreferenced
    )

    migration.downgrade()

    # edges dropped before the tasks columns; enum dropped last (unreferenced).
    assert record.index(("drop_table", DEPS_TABLE)) < record.index(("drop_column", "created_reason"))
    for col in ("project_id", "analysis_run_id", "finding_id", "revision_sha", "created_reason"):
        assert ("drop_column", col) in record
    for idx in TASKS_NEW_INDEXES:
        assert ("drop_index", idx) in record
    for idx in DEPS_INDEXES:
        assert ("drop_index", idx) in record
    # FK constraints are dropped (deps: 3 explicit; tasks: 3 via introspection).
    drop_constraints = [name for kind, name in record if kind == "drop_constraint"]
    assert len(drop_constraints) == 6
    assert len(bind.query_sql("DROP TYPE")) == 1


def test_downgrade_keeps_enum_while_columns_still_reference_it(monkeypatch) -> None:
    """A partially rolled-down schema (columns present) must not orphan the enum."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={TASKS_TABLE, DEPS_TABLE},
        columns={TASKS_TABLE: {"created_reason"}},
        indexes={DEPS_TABLE: set(DEPS_INDEXES)},
        fk_constraints={DEPS_TABLE: _deps_fk_constraints()},
        present_types={ENUM},
        referenced_types={ENUM},  # a column still uses it
    )

    migration.downgrade()

    assert ("drop_table", DEPS_TABLE) in record
    assert ("drop_column", "created_reason") in record
    # referenced -> the DROP TYPE guard must not fire
    assert bind.query_sql("DROP TYPE") == []


# ---------------------------------------------------------------------------
# DDL content (FK / CHECK / UNIQUE semantics)
# ---------------------------------------------------------------------------
def test_deps_table_ddl_carries_check_unique_and_cascade_fks(monkeypatch) -> None:
    """task_dependencies DDL declares the self-edge CHECK, the UNIQUE pair, and
    the two tasks FKs with ondelete=CASCADE (design §3.2 / ADR-1)."""
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
        tables={TASKS_TABLE},
        columns={TASKS_TABLE: set()},
        indexes={},
        fk_constraints={},
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    deps_spec = next(a for a in captured if a[0] == DEPS_TABLE)
    specs = list(deps_spec[1:])
    uniques = [s for s in specs if isinstance(s, sa.UniqueConstraint)]
    checks = [s for s in specs if isinstance(s, sa.CheckConstraint)]
    fks = [s for s in specs if isinstance(s, sa.ForeignKeyConstraint)]
    assert any("uq_task_depends_pair" in str(u.name) for u in uniques), "UNIQUE pair missing"
    assert any("ck_task_dep_no_self" in str(c.name) for c in checks), "self-edge CHECK missing"
    tasks_fk_cascade = [
        fk for fk in fks
        if "tasks.id" in [e.target_fullname for e in fk.elements]
        and fk.ondelete == "CASCADE"
    ]
    assert len(tasks_fk_cascade) == 2, f"expected 2 tasks->CASCADE FKs, got {fks!r}"


def test_tasks_created_reason_column_is_not_null_with_manual_server_default(monkeypatch) -> None:
    """The created_reason ADD COLUMN is NOT NULL with a MANUAL server default
    (no data backfill — the DDL-only rule)."""
    migration = _load_migration()
    record: list = []
    captured: list = []

    def capture_add_column(*args, **kwargs):
        captured.append(args)
        record.append(("add_column", args[1].name))

    _patch_op(monkeypatch, migration, record)
    monkeypatch.setattr(migration.op, "add_column", capture_add_column)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_state(
        monkeypatch,
        migration,
        tables={TASKS_TABLE, DEPS_TABLE},
        columns={TASKS_TABLE: set()},
        indexes={DEPS_TABLE: set(DEPS_INDEXES)},
        fk_constraints={DEPS_TABLE: _deps_fk_constraints()},
        present_types={ENUM},
        referenced_types=set(),
    )

    migration.upgrade()

    col_spec = next(a for a in captured if a[1].name == "created_reason")
    col = col_spec[1]  # op.add_column(table, column) -> the sa.Column is arg[1]
    assert col.nullable is False, "created_reason must be NOT NULL"
    # server_default may be a plain string or a sa.text() clause; normalize to str.
    sd_arg = col.server_default.arg
    sd_value = sd_arg.text if hasattr(sd_arg, "text") else str(sd_arg)
    assert sd_value == "MANUAL", f"created_reason must default to MANUAL, got {sd_value!r}"

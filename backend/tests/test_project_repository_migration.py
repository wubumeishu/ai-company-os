"""Deployment contract for the f066 Project/Repository tables migration.

Follows the monkeypatched-op convention of test_agent_model_deleted_at_migration.py:
the migration module is loaded from file and its schema-introspection helpers
are patched so no database connection is required. The two paths matter:

- Fresh deployments: 001_initial_schema's create_all already builds
  projects/repositories from the registered metadata, so upgrade() must be a
  no-op (guarded).
- Existing deployments stamped before f066: upgrade() must issue the
  CREATE TYPE + CREATE TABLE + CREATE INDEX DDL in a re-runnable order.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_4_f066_add_project_repo_tables.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "project_repo_tables_migration",
        MIGRATION_PATH,
    )
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
    """Stand-in for op.get_bind(): records executed SQL, returns canned rows."""

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


def _patch_schema_state(
    monkeypatch,
    migration,
    *,
    tables: set[str],
    present_types: set[str],
    referenced_types: set[str],
) -> None:
    monkeypatch.setattr(migration, "_existing_tables", lambda _bind: set(tables))
    monkeypatch.setattr(migration, "_present_enum_types", lambda _bind: set(present_types))
    monkeypatch.setattr(migration, "_referenced_enum_types", lambda _bind: set(referenced_types))


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


PG_TYPES = {"project_status_enum", "repository_source_type_enum"}
PG_TABLES = {"projects", "repositories"}


def test_revision_mounts_f065_head() -> None:
    migration = _load_migration()
    assert migration.revision == "f066_add_project_repo_tables"
    assert migration.down_revision == "f065_feishu_group_target"


def test_upgrade_is_noop_when_fresh_metadata_already_created_schema(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables=PG_TABLES,
        present_types=PG_TYPES,
        referenced_types=PG_TYPES,
    )

    migration.upgrade()

    assert record == []
    # Enum types already exist: no CREATE TYPE; tables exist: no CREATE TABLE.
    assert bind.query_sql("CREATE TYPE") == []


def test_upgrade_creates_both_tables_with_enum_types_on_existing_postgres(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables=set(),
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    assert record == [
        ("create_table", "projects"),
        ("create_index", "ix_projects_created_by"),
        ("create_index", "ix_projects_tenant_id"),
        ("create_table", "repositories"),
        ("create_index", "ix_repositories_project_id"),
        ("create_index", "ix_repositories_tenant_id"),
    ]
    create_types = bind.query_sql("CREATE TYPE")
    assert len(create_types) == 2
    assert "project_status_enum" in create_types[0]
    assert "repository_source_type_enum" in create_types[1]
    # Types are created (recorded SQL, executed before any op call) before
    # any table DDL op call: projects is record[0], repositories is record[3].
    type_sql_positions = [i for i, s in enumerate(bind.executed) if "CREATE TYPE" in s]
    table_call_order = [i for i, (kind, name) in enumerate(record) if kind == "create_table"]
    assert type_sql_positions == [0, 1] and table_call_order == [0, 3]


def test_upgrade_creates_tables_on_non_postgres_without_create_type(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind(dialect_name="sqlite")
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables=set(),
        present_types=PG_TYPES,  # non-PG dialect reports "present" (VARCHAR path)
        referenced_types=set(),
    )

    migration.upgrade()

    assert ("create_table", "projects") in record
    assert ("create_table", "repositories") in record
    assert bind.query_sql("CREATE TYPE") == []


def test_downgrade_drops_in_reverse_order_on_postgres(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    bind._default = [("project_status_enum",), ("repository_source_type_enum",)]
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables=PG_TABLES,
        present_types=PG_TYPES,
        referenced_types=set(),  # tables dropped first -> no column references left
    )

    migration.downgrade()

    assert record == [
        ("drop_index", "ix_repositories_project_id"),
        ("drop_index", "ix_repositories_tenant_id"),
        ("drop_table", "repositories"),
        ("drop_index", "ix_projects_created_by"),
        ("drop_index", "ix_projects_tenant_id"),
        ("drop_table", "projects"),
    ]
    assert len(bind.query_sql("DROP TYPE")) == 2


def test_downgrade_keeps_enum_types_while_columns_still_reference_them(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)
    bind = FakeBind()
    bind._default = [("project_status_enum",), ("repository_source_type_enum",)]
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables={"projects"},  # repositories already gone, projects still present
        present_types=PG_TYPES,
        referenced_types=PG_TYPES,  # projects.status/source_type columns still use them
    )

    migration.downgrade()

    assert ("drop_table", "projects") in record
    assert ("drop_table", "repositories") not in record
    assert bind.query_sql("DROP TYPE") == []


def test_create_table_statements_carry_fk_and_cascade(monkeypatch) -> None:
    """The repositories DDL declares the project FK with ondelete=CASCADE."""
    migration = _load_migration()
    record: list = []
    _patch_op(monkeypatch, migration, record)

    captured: list = []

    def capture_create_table(*args, **kwargs):
        captured.append(args)
        record.append(("create_table", args[0]))

    monkeypatch.setattr(migration.op, "create_table", capture_create_table)
    bind = FakeBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    _patch_schema_state(
        monkeypatch,
        migration,
        tables=set(),
        present_types=set(),
        referenced_types=set(),
    )

    migration.upgrade()

    repos_spec = next(a for a in captured if a[0] == "repositories")
    fk_args = [arg for arg in repos_spec[1:] if isinstance(arg, sa.ForeignKeyConstraint)]
    cascade_fk = [
        arg
        for arg in fk_args
        if "projects.id" in [e.target_fullname for e in arg.elements] and arg.ondelete == "CASCADE"
    ]
    assert cascade_fk, f"expected repositories -> projects.id FK with ondelete=CASCADE, got {fk_args!r}"

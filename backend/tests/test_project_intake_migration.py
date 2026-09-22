"""Deployment contract for the f067 Intake rejection/pending-verifier migration.

Follows the monkeypatched-op convention of test_project_repository_migration.py:
the migration module is loaded from file and its schema-introspection
helpers are patched so no database connection is required. The paths that
matter:

- Existing deployments stamped at f066: upgrade() must issue the four
  ADD COLUMN DDLs (projects.rejection_reason, projects.rejection_detail,
  repositories.pending_verifier, repositories.retry_count).
- Idempotent re-run: when the columns already exist, upgrade() must be a
  no-op.
- downgrade() must drop the four columns and be a no-op when they are
  already gone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_5_f067_intake_rejection_fields.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("intake_rejection_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_existing_columns(
    monkeypatch, migration, *, projects_columns: set[str], repos_columns: set[str]
) -> None:
    def fake_columns(_bind, table_name):
        if table_name == "projects":
            return set(projects_columns)
        if table_name == "repositories":
            return set(repos_columns)
        raise AssertionError(f"unexpected table {table_name}")

    monkeypatch.setattr(migration, "_existing_columns", fake_columns)


def _patch_op_record(monkeypatch, migration, record: list) -> None:
    # add_column receives (table, sa.Column); drop_column receives (table, name).
    # Normalize every entry to (verb, table, column_name) for clean asserts.
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, col: record.append(("add_column", table, col.name)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, name: record.append(("drop_column", table, name)),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())


def _record_columns(migration, monkeypatch, record: list) -> dict:
    """Capture the raw sa.Column DDL specs keyed by (table, column)."""
    specs: dict[tuple[str, str], object] = {}

    def capture_add(table, col):
        specs[(table, col.name)] = col
        record.append(("add_column", table, col.name))

    monkeypatch.setattr(migration.op, "add_column", capture_add)
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, name: record.append(("drop_column", table, name)),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    return specs


def test_revision_mounts_f066_head() -> None:
    migration = _load_migration()
    assert migration.revision == "f067_intake_rejection_fields"
    assert migration.down_revision == "f066_add_project_repo_tables"


def test_upgrade_adds_four_columns_when_absent(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op_record(monkeypatch, migration, record)
    _patch_existing_columns(
        monkeypatch,
        migration,
        projects_columns={"id", "name"},
        repos_columns={"id", "project_id"},
    )

    migration.upgrade()

    assert record == [
        ("add_column", "projects", "rejection_reason"),
        ("add_column", "projects", "rejection_detail"),
        ("add_column", "repositories", "pending_verifier"),
        ("add_column", "repositories", "retry_count"),
    ]


def test_upgrade_is_noop_when_columns_already_present(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op_record(monkeypatch, migration, record)
    _patch_existing_columns(
        monkeypatch,
        migration,
        projects_columns={"rejection_reason", "rejection_detail"},
        repos_columns={"pending_verifier", "retry_count"},
    )

    migration.upgrade()

    assert record == []


def test_upgrade_adds_only_missing_columns_on_partial_state(monkeypatch) -> None:
    """Partial column presence adds just the missing columns (idempotent)."""
    migration = _load_migration()
    record: list = []
    _patch_op_record(monkeypatch, migration, record)
    _patch_existing_columns(
        monkeypatch,
        migration,
        projects_columns={"rejection_reason"},
        repos_columns=set(),
    )

    migration.upgrade()

    assert record == [
        ("add_column", "projects", "rejection_detail"),
        ("add_column", "repositories", "pending_verifier"),
        ("add_column", "repositories", "retry_count"),
    ]


def test_downgrade_drops_four_columns_when_present(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op_record(monkeypatch, migration, record)
    _patch_existing_columns(
        monkeypatch,
        migration,
        projects_columns={"rejection_reason", "rejection_detail"},
        repos_columns={"pending_verifier", "retry_count"},
    )

    migration.downgrade()

    assert record == [
        ("drop_column", "projects", "rejection_reason"),
        ("drop_column", "projects", "rejection_detail"),
        ("drop_column", "repositories", "pending_verifier"),
        ("drop_column", "repositories", "retry_count"),
    ]


def test_downgrade_is_noop_when_columns_absent(monkeypatch) -> None:
    migration = _load_migration()
    record: list = []
    _patch_op_record(monkeypatch, migration, record)
    _patch_existing_columns(monkeypatch, migration, projects_columns=set(), repos_columns=set())

    migration.downgrade()

    assert record == []


def test_column_specs_are_ddl_only_and_nullability_correct(monkeypatch) -> None:
    """The four column DDL specs carry the documented nullability/defaults."""
    migration = _load_migration()
    record: list = []
    specs = _record_columns(migration, monkeypatch, record)
    _patch_existing_columns(monkeypatch, migration, projects_columns=set(), repos_columns=set())

    migration.upgrade()

    rr = specs[("projects", "rejection_reason")]
    rd = specs[("projects", "rejection_detail")]
    pv = specs[("repositories", "pending_verifier")]
    rc = specs[("repositories", "retry_count")]

    # Nullability: rejection fields nullable; pending/retry NOT NULL.
    assert rr.nullable is True
    assert rd.nullable is True
    assert pv.nullable is False
    assert rc.nullable is False

    # Declarative server defaults: pending_verifier = FALSE, retry_count = 0.
    assert str(pv.server_default.arg).lower() in ("false", "0", "f")
    assert str(rc.server_default.arg) == "0"

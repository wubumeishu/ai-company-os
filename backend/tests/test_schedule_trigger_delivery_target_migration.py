"""Deployment contract for the f065 Feishu group delivery target migration.

Root-cause guard for t_18a69c3b: 001_initial_schema's create_all builds
agent_schedules / agent_triggers from the current SQLAlchemy metadata, and the
registered models already carry delivery_target_id. The original unguarded
op.add_column therefore raised DuplicateColumnError on a truly fresh
database when the full chain 001 -> head ran; upgrade()/downgrade() are now
existence-guarded and no-ops in the opposite state.

Follows the monkeypatched-inspector convention of
test_agent_model_deleted_at_migration.py: the migration module is loaded from
file and its schema-introspection helper is patched, so no live database is
required. The two paths matter:

- Fresh deployments: 001 create_all already built the column -> upgrade()
  is a no-op and downgrade() still drops it.
- Existing deployments stamped before the models gained the column:
  upgrade() must add it, and a partial state adds only the missing column.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608181600_schedule_trigger_feishu_group_target.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "schedule_trigger_feishu_group_target_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeInspector:
    def __init__(self, *, columns: dict[str, set[str]]):
        self.columns = columns

    def get_table_names(self):
        return list(self.columns)

    def get_columns(self, table_name):
        return [{"name": name} for name in self.columns.get(table_name, set())]


def _install_inspector(monkeypatch, migration, *, columns: dict[str, set[str]]) -> None:
    inspector = FakeInspector(columns=columns)
    monkeypatch.setattr(migration, "_existing_columns", lambda _bind: {
        table: set(names) for table, names in columns.items()
    })
    monkeypatch.setattr(migration.op, "get_bind", lambda: inspector)


def _add_column_calls(calls: list) -> list:
    return [(kind, args[0]) for kind, args, _ in calls if kind == "add_column"]


def test_revision_mounts_f064_tool_call_tenants() -> None:
    migration = _load_migration()
    assert migration.revision == "f065_feishu_group_target"
    assert migration.down_revision == "f064_tool_call_tenants"
    # The f066 contract (test_project_repository_migration.py) mounts on this
    # exact revision id; a change would fork the graph or break that chain.
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_registered_models_already_carry_the_column() -> None:
    """If 001's create_all parity holds, guarded f065 must stay a no-op.

    The DuplicateColumnError defect exists only because these two models
    register delivery_target_id in Base.metadata: any removal here would
    silently make the upgrade() guard re-add a column 001 already owns.
    """
    for table in (AgentSchedule.__table__, AgentTrigger.__table__):
        column = table.c["delivery_target_id"]
        assert column is not None
        assert column.nullable
        assert isinstance(column.type, sa.UUID)


def test_upgrade_is_noop_when_fresh_metadata_already_created_column(monkeypatch) -> None:
    migration = _load_migration()
    _install_inspector(
        monkeypatch,
        migration,
        columns={
            "agent_schedules": {"id", "delivery_target_id"},
            "agent_triggers": {"id", "delivery_target_id"},
        },
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected add_column on fresh schema")
        ),
    )

    migration.upgrade()


def test_upgrade_adds_only_missing_columns(monkeypatch) -> None:
    migration = _load_migration()
    calls: list = []
    _install_inspector(
        monkeypatch,
        migration,
        columns={
            "agent_schedules": {"id", "delivery_target_id"},
            "agent_triggers": {"id"},
        },
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args, **kwargs: calls.append(("add_column", args, kwargs)),
    )

    migration.upgrade()

    assert _add_column_calls(calls) == [("add_column", "agent_triggers")]
    table, column = calls[0][1]
    assert table == "agent_triggers"
    assert column.name == "delivery_target_id"
    assert column.nullable


def test_upgrade_adds_both_columns_when_schema_lacks_them(monkeypatch) -> None:
    migration = _load_migration()
    calls: list = []
    _install_inspector(
        monkeypatch,
        migration,
        columns={"agent_schedules": {"id"}, "agent_triggers": {"id"}},
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args, **kwargs: calls.append(("add_column", args, kwargs)),
    )

    migration.upgrade()

    assert _add_column_calls(calls) == [
        ("add_column", "agent_schedules"),
        ("add_column", "agent_triggers"),
    ]


def test_downgrade_drops_only_present_columns(monkeypatch) -> None:
    migration = _load_migration()
    calls: list = []
    _install_inspector(
        monkeypatch,
        migration,
        columns={"agent_schedules": {"id"}, "agent_triggers": {"id", "delivery_target_id"}},
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda *args, **kwargs: calls.append(("drop_column", args, kwargs)),
    )

    migration.downgrade()

    assert [(kind, args[0], args[1]) for kind, args, _ in calls] == [
        ("drop_column", "agent_triggers", "delivery_target_id"),
    ]


def test_downgrade_is_noop_when_nothing_to_drop(monkeypatch) -> None:
    migration = _load_migration()
    _install_inspector(
        monkeypatch,
        migration,
        columns={"agent_schedules": {"id"}, "agent_triggers": {"id"}},
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected drop_column")
        ),
    )

    migration.downgrade()

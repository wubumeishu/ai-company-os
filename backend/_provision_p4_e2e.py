"""Provision the t_ab1fb86a scratch Postgres + run the real alembic chain.

(Untracked scratch tool, same convention as the Phase 4 parent cards.)
Creates ``clawith_t_ab1fb86a_f073`` owned by ``clawith`` (CREATEDB role on
the native Windows PG16 maintenance DB), then runs ``alembic upgrade head``
on it with the main-tree venv so the live E2E tier runs against the REAL
f072/f073 DDL (the D3 lesson: the real alembic chain, never create_all),
and finally prints ``alembic heads`` to confirm the single-head invariant.

Usage (from anywhere; paths are native Windows):

    "I:/project/AI Company OS/backend/.venv/Scripts/python.exe" _provision_p4_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

VENV_PY = "I:/project/AI Company OS/backend/.venv/Scripts/python.exe"
WORKTREE_BACKEND = "I:/project/AI Company OS/.worktrees/t_ab1fb86a/backend"
MAINT_DSN = "postgresql://clawith:clawith@127.0.0.1:5432/postgres"
SCRATCH_DB = "clawith_t_ab1fb86a_f073"
SCRATCH_URL = f"postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/{SCRATCH_DB}"


def _checkpoint_dsn() -> str:
    """The scratch DB under the app's dedicated checkpoint search_path.

    Mirrors ``app.services.agent_runtime.checkpointer.checkpoint_database_url``
    (``-c search_path=langgraph_checkpoint,public``) so the CCI migrations
    land in the schema the runtime checkpointer actually uses.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(f"postgresql://clawith:clawith@127.0.0.1:5432/{SCRATCH_DB}")
    options = quote("-c search_path=langgraph_checkpoint,public", safe="")
    return urlunsplit(parts._replace(query=f"options={options}"))


def _setup_checkpoint_schema() -> None:
    """Apply the langgraph checkpoint CCI migrations at PROVISION time.

    The f072/f073 DDL leaves the checkpoint schema at migration v5: the
    v6-v8 ``CREATE INDEX CONCURRENTLY`` statements cannot run inside the
    alembic transaction and are deferred to first use.  A CCI commits only
    after every open snapshot ends, so running it during a test chain —
    where an earlier test's session can sit idle-in-transaction — blocks
    forever (this was the t_ab1fb86a full-chain hang: ``saver.setup()`` in
    test 4 dead-waiting on a CCI behind a leaked snapshot).  Running it
    here, against a freshly provisioned DB with no competing sessions,
    completes the CCIs once; every test-time ``saver.setup()`` is then a
    no-op (the migrations table is already at latest).
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    if sys.platform == "win32":
        # psycopg's socket poller needs a selector loop on Windows.  The
        # policy must be set BEFORE asyncio.run builds the loop (setting it
        # inside the coroutine is too late — psycopg's connect() rejects a
        # ProactorEventLoop at connect time).
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _setup() -> None:
        manager = AsyncPostgresSaver.from_conn_string(_checkpoint_dsn())
        async with manager as saver:
            await saver.setup()

    asyncio.run(_setup())
    print("checkpointer schema: all CCI migrations applied at provision time")


async def _create_scratch_db() -> None:
    import asyncpg

    conn = await asyncpg.connect(MAINT_DSN)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        await conn.execute(f"CREATE DATABASE {SCRATCH_DB}")
    finally:
        await conn.close()
    print(f"created scratch DB {SCRATCH_DB}")


def main() -> int:
    asyncio.run(_create_scratch_db())

    env = {**os.environ, "DATABASE_URL": SCRATCH_URL}
    subprocess.run(
        [VENV_PY, "-m", "alembic", "upgrade", "head"],
        cwd=WORKTREE_BACKEND,
        env=env,
        check=True,
    )
    print("alembic upgrade head: clean")
    _setup_checkpoint_schema()
    heads = subprocess.run(
        [VENV_PY, "-m", "alembic", "heads"],
        cwd=WORKTREE_BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    print("alembic heads:\n" + heads.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

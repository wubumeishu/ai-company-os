"""Provision the t_af586c02 scratch Postgres + run the real alembic chain.

(Untracked scratch tool, same convention as the parent completion-lane card.)
Creates ``clawith_t_af586c02_f073`` owned by ``clawith`` (CREATEDB role on the
native Windows PG16 maintenance DB), then runs ``alembic upgrade head`` on it
with the main-tree venv so the live test tier runs against the REAL f072/f073
DDL (the D3 lesson: the real alembic chain, never create_all from
os.getcwd()), and finally prints ``alembic heads`` to confirm the single
head invariant.

Usage (from anywhere; paths are native Windows):

    "I:/project/AI Company OS/backend/.venv/Scripts/python.exe" _provision_f073.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

VENV_PY = "I:/project/AI Company OS/backend/.venv/Scripts/python.exe"
WORKTREE_BACKEND = "I:/project/AI Company OS/.worktrees/t_af586c02/backend"
MAINT_DSN = "postgresql://clawith:***@127.0.0.1:5432/postgres"
SCRATCH_DB = "clawith_t_af586c02_f073"
SCRATCH_URL = f"postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/{SCRATCH_DB}"


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

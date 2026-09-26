"""Cheap preflight for the concurrency ladder: PG admin reachable, key present."""
import asyncio
import os
import sys

ADMIN = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")


async def main() -> int:
    import asyncpg

    # asyncpg url form
    url = ADMIN.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncio.wait_for(asyncpg.connect(url), timeout=10)
    except Exception as exc:
        print(f"PG_CONNECT_FAIL: {exc}")
        return 1
    rows = await conn.fetch("SELECT current_setting('server_version') AS v, 1 AS ok")
    print(f"PG_OK version={rows[0]['v']}")
    dbs = await conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE 'clawith_2f_%' ORDER BY datname")
    print(f"existing scratch dbs: {len(dbs)}")
    for r in dbs[:15]:
        print(f"  - {r['datname']}")
    await conn.close()
    key = os.environ.get("AGNES_API_KEY", "").strip()
    print(f"AGNES_API_KEY set: {bool(key)} (len={len(key)})")
    print(f"AGNES_BASE_URL: {os.environ.get('AGNES_BASE_URL')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

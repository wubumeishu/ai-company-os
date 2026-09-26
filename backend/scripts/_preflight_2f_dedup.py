"""Phase 2F t_f28b2fa3 preflight — check Postgres admin + LLM endpoint reachability.

Read-only, no scratch DB created, no product import. Run:
    cd backend && uv run --no-sync python scripts/_preflight_2f_dedup.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
ADMIN_BASE = os.environ.get("CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432")


async def _pg() -> None:
    import asyncpg

    dsn = ADMIN_BASE.replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn, timeout=8)
    try:
        dbs = await conn.fetch(
            "select datname from pg_database where datname like 'clawith_2f%' order by datname desc limit 10"
        )
        print(f"PG REACHABLE ({dsn.rsplit('/', 1)[0]})")
        for r in dbs:
            print("   scratch db present:", r["datname"])
        if not dbs:
            print("   (no prior 2f scratch DBs — fine, we create a fresh one)")
    finally:
        await conn.close()


def _llm() -> None:
    body = json.dumps(
        {"model": AGNES_MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "reply with the single word: pong"}]}
    ).encode()
    req = urllib.request.Request(
        AGNES_BASE_URL + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
            print(f"LLM OK: {d['choices'][0]['message']['content'][:60]!r} model={d.get('model')}")
    except Exception as e:
        print(f"LLM FAIL: {e!r}")


async def main() -> int:
    await _pg()
    _llm()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

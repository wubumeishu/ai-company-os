import asyncio, sys, json
async def main():
    import asyncpg
    db = sys.argv[1] if len(sys.argv)>1 else "clawith_2f_perf_s1_5"
    conn = await asyncio.wait_for(asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{db}"), 15)
    rows = await conn.fetch("SELECT run_id, event_type, payload, summary, created_at FROM agent_run_events WHERE event_type IN ('waiting_started','run_failed','status_changed') ORDER BY created_at")
    for r in rows[:20]:
        p = r['payload']; p = json.dumps(p, ensure_ascii=False)[:500] if p else "(none)"
        print(f"run={str(r['run_id'])[:8]} {r['event_type']} ts={r['created_at'].time()}")
        print(f"   summary: {str(r['summary'])[:200]}")
        print(f"   payload: {p}")
        print()
    await conn.close()
asyncio.run(main())

import asyncio, sys, json
async def main():
    import asyncpg
    db = sys.argv[1] if len(sys.argv)>1 else "clawith_2f_perf_s1_5"
    conn = await asyncio.wait_for(asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{db}"), 15)
    # waiting_request lives in the checkpoint's channel_values / values JSON
    rows = await conn.fetch("SELECT thread_id, checkpoint_id, channel_values, metadata FROM langgraph_checkpoint.checkpoints ORDER BY checkpoint_id LIMIT 60")
    print(f"checkpoints: {len(rows)}")
    seen = set()
    for r in rows:
        cv = r['channel_values']
        s = cv if isinstance(cv, str) else json.dumps(cv, ensure_ascii=False) if cv else ""
        # look for error / waiting markers
        for kw in ("429","rate","limit","500","502","503","504","timeout","Timeout","error","LLM","waiting","retry"):
            pass
        import re
        m = re.search(r'".*(?:429|rate_limit|rate limit|Too Many|503|502|500|ReadTimeout|timeout|timeouted|error).{0,200}', s)
        if m and r['checkpoint_id'] not in seen:
            seen.add(r['checkpoint_id'])
            print(f"\nthread={r['thread_id'][:8]} cp={r['checkpoint_id'][:8]}")
            print("  HIT:", m.group(0)[:400])
    await conn.close()
asyncio.run(main())

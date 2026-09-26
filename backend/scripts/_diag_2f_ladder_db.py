"""Diagnostic: inspect a ladder stage scratch DB (read-only, 0 LLM calls)."""
import asyncio
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "clawith_2f_perf_s1_5"


async def main() -> int:

    import asyncpg

    conn = await asyncio.wait_for(
        asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{DB}"), timeout=15
    )
    rows = await conn.fetch("""
        SELECT c.run_id, c.status AS cmd_status, c.attempt_count, c.error_code,
               c.created_at, c.applied_at, c.claimed_by
        FROM agent_run_commands c WHERE c.command_type='start'
        ORDER BY c.created_at
    """)
    print(f"=== commands in {DB} ===")
    for r in rows:
        print(f"  run={str(r['run_id'])[:8]} status={r['cmd_status']} attempt={r['attempt_count']} "
              f"err={r['error_code']} created={r['created_at']} applied={r['applied_at']} by={str(r['claimed_by'])[:16]}")

    evs = await conn.fetch("""
        SELECT run_id, event_type, created_at FROM agent_run_events
        ORDER BY run_id, created_at
    """)
    print("=== run events (per run) ===")
    by_run = {}
    for e in evs:
        by_run.setdefault(str(e['run_id'])[:8], []).append(f"{e['event_type']}@{e['created_at'].time().isoformat()}")
    for k in sorted(by_run):
        print(f"  {k}: {by_run[k]}")

    tools = await conn.fetch("""
        SELECT run_id, tool_name, status, started_at, completed_at, result_summary
        FROM agent_tool_executions ORDER BY run_id, started_at
    """)
    print(f"=== tool executions ({len(tools)}) ===")
    for t in tools:
        dur = f"{(t['completed_at']-t['started_at']).total_seconds()}s" if t['started_at'] and t['completed_at'] else "?"
        rs = (t['result_summary'] or "")[:60]
        print(f"  run={str(t['run_id'])[:8]} {t['tool_name']} -> {t['status']} ({dur}) result-metadata-check rs={rs!r}")

    # the waiting_started payload (decisive: what kind of WAIT?)
    waits = await conn.fetch("""
        SELECT run_id, event_type, payload, created_at FROM agent_run_events
        WHERE event_type='waiting_started' ORDER BY run_id
    """)
    print(f"=== waiting_started payloads ({len(waits)}) ===")
    for w in waits:
        p = w['payload'] or {}
        wt = p.get('waiting_type') or p.get('type')
        print(f"  run={str(w['run_id'])[:8]} waiting_type={wt} keys={list(p.keys())}")
        print(f"     payload={str(p)[:300]}")

    runs = await conn.fetch("SELECT id, status, created_at FROM agent_runs ORDER BY created_at")
    print(f"=== agent_runs ({len(runs)}) ===")
    for r in runs:
        print(f"  {str(r['id'])[:8]} status={r['status']} created={r['created_at']}")

    # checkpoint state (LangGraph) — is the graph still in flight?
    try:
        chk = await conn.fetch(
            "SELECT count(*) AS n FROM langgraph_checkpoint.checkpoints")
        print(f"langgraph checkpoints: {chk[0]['n']}")
    except Exception as exc:  # noqa: BLE001 - diagnostic helper: a missing/renamed
        # checkpoint table should report, not crash the read-only inspection.
        print(f"checkpoint table read failed: {exc}")

    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

# Phase 2E Execution-Chain Verification Report (t_84836b4b)

- Date: 2026-09-26 (UTC+09:00)
- Reviewer worktree: `wt/t_84836b4b-e3262d92` @ `I:/project/AI Company OS/.worktrees/t_84836b4b-e3262d92`
- Under test: commit `e3262d92` on `wt/t_c2ed29df` (Phase 2E V1 TaskExecutionService bridge, pushed to origin)
- Spec source of truth: `docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md` (t_180370c7 @ 33b4d78c)

---

## 0. Scope

Independently verify the four execution-chain behaviors the Kanban card requires,
against the real code, WITHOUT modifying core Agent-Runtime components:

1. Tasks with unmet dependencies are rejected (P4 gate, fail-closed, no Run).
2. Duplicate execution attempts are handled (reuse, not a second Run; explicit
   retry after a terminal Run under a new attempt key).
3. Independent Tasks on distinct Agents run concurrently (physically isolated,
   no lane block).
4. Workspace conflicts are handled by the EXISTING lock/queue rules (not
   re-invented by this diff).

All conclusions are backed by MY OWN execution (new test suite re-run, task-lane
regression re-run, and a genuine real-Postgres harness), not by the builder's
narrative.

---

## 1. Evidence — commands + results (all run by the reviewer)

Run from the reviewer worktree's `backend/` via the worktree-local venv
(`uv run --no-sync`):

| # | Command | Result |
|---|---------|--------|
| 1 | `uv run --no-sync pytest tests/test_task_execution_service.py -q` | **37 passed** (spec §12 matrix, re-run by reviewer) |
| 2 | `uv run --no-sync pytest tests/test_task_runtime_intake.py tests/test_task_api_runtime_intake.py tests/test_task_graph_api.py tests/test_task_decomposition_service.py tests/test_task_graph_provenance.py tests/test_agent_runtime_task_completion.py -q` | **44 passed, 17 skipped** (task-lane regression) |
| 3 | combined `… + tests/test_task_execution_service.py` | **81 passed, 17 skipped** (reconciles the build's "81 passed/17 skipped") |
| 4 | `uv run --no-sync pyright app/services/task_execution_service.py app/dao/audit_dao.py` | **0 errors** |
| 5 | `uv run --no-sync ruff check <new files>` (service, audit_dao, agent_run_dao, task_dao, new test) | **0 errors** |
| 6 | `python verify_2e_execution_chain.py` (real Postgres scratch DB) | **VERDICT: PASS** — see §2 |

Ruff on the two baseline files (`app/api/tasks.py`, `app/services/task_executor.py`)
reports 29 items, all of which are the repo-wide pre-existing style baseline
(22× B008 universal `Depends(get_current_user)`, I001, RUF059, one pre-existing
BLE001 broad-catch in legacy `execute_task`, SIM117). The diff only ADDED +2 B008
matching the two new handlers; no NEW error class was introduced. One
pre-existing pyright `reportReturnType` in `schemas.py` sits in the
`_redact_channel_secrets` channel function (parent commit already carried it;
NOT in this diff's +34-line schema region).

---

## 2. Real-Postgres execution-chain evidence (`verify_2e_execution_chain.py`)

The harness drives the ACTUAL pre-existing intake boundary
(`register_run_with_start` — the step `enqueue_task_runtime` feeds the Phase-2E
source keys into) and the REAL executor (`enqueue_task_runtime` + its P4
dependency gate) against a scratch PostgreSQL DB. No LangGraph/worker
internals touched.

```
S1_blocked_raised: True          # real executor P4 gate: B (dep on A) rejected
S1_unmet_is_taskA: True          # unmet list = [task A]
S1_taskB_still_pending: True     # task stays pending on rejection
S1_no_run_after_blocked: 0       # NO Run row created (fail-closed, nothing enqueued)

S2_first_created: True           # first Execute registers the Run
S2_second_created: False         # duplicate under same stable key is REUSED
S2_same_run_id: True             # … the SAME Run row, not a second one
S2_run_rows_for_key: 1           # physically one row (uq_agent_runs_source_execution)

S3_taskY_runs: 1                 # independent task on a distinct agent
S3_taskY_lane_key_null: True     # scheduling_lane_key NULL -> no lane block
S3_stress_run_rows: 1            # two CONCURRENT sessions, same key -> 1 row
S3_stress_created_flags: [True, False]  # winner created, loser reused (SAVEPOINT recovery)
S3_stress_same_run: True
```

---

## 3. Scenario-by-scenario verdict

### S1 — Dependency rejection (P4 gate)
- Mechanism: `task_graph_service.ensure_ready` returns the unmet direct-dep ids;
  the P4 gate in `TaskExecutionService._gate` and the real
  `enqueue_task_runtime` both refuse to register a Run when it is non-empty.
- Evidence: real-DB — a `Task B → Task A (not done)` graph makes the real
  executor raise `TaskBlockedError` with `reason=[A]`, the Task stays `pending`,
  and 0 Run rows exist.
- Conclusion: **PASS** — unmet dependencies are rejected fail-closed; nothing
  is enqueued.

### S2 — Idempotency / duplicate handling
- Mechanism: stable `source_execution_id = task:{id}` (first attempt only) +
  DB-unique `uq_agent_runs_source_execution`; the intake's exact-input
  re-resolution (`_find_start_retry` / `_resolve_source_retry`) returns the
  existing Run with `created=False` on a duplicate; a terminal failed/cancelled
  Run gets an explicit new attempt key `task:{id}:retry:{attempt_id}` (R3/R5,
  never auto — R4).
- Evidence: real-DB — same key registers ONE row; the second identical call
  reuses it (`created=False`, same `run_id`). A genuine two-session CONCURRENT
  same-key registration also yields exactly one row (winner `created=True`,
  loser `created=False` via the SAVEPOINT/IntegrityError recovery at
  `persistence.py:382-400`).
- Conclusion: **PASS** — duplicates are reused / a second Run is physically
  impossible; post-failure retry is an explicit new attempt, never automatic.

### S3 — Concurrent independent execution
- Mechanism: assignment = `Task.agent_id`; two Tasks on two distinct Agents use
  distinct source keys and physically isolated storage namespaces; V1 leaves
  `scheduling_lane_key = NULL` on task Runs, so the lane-serialization unique
  index (`uq_agent_runs_active_lane`, where `scheduling_lane_key IS NOT NULL
  AND lane_held`) does NOT fire — no invented queue, no single-writer.
- Evidence: real-DB — two independent tasks (distinct agents) coexist as two
  coexisting Runs with `scheduling_lane_key = NULL`.
- Conclusion: **PASS** — independent tasks register as parallel Runs; the
  dependency gate is the only load-bearing edge.

### S4 — Workspace conflicts via existing lock/queue rules
- Evidence: the diff footprint is 10 files; **0 files under
  `app/services/agent_runtime/` and 0 alembic files** were touched. The
  same-agent conflict surface (`command_worker.py` `thread_lock_busy` fast-fail
  + workspace-lock layers) is pre-existing and unmodified. The P6 ruling (no
  enqueue-time workspace check; a busy lock surfaces as the transient
  `WORKSPACE_CONFLICT` at Run time) is honored — the service adds no gate and
  no queueing facility.
- Conclusion: **PASS** — workspace-conflict handling stays owned by the
  existing worker/lock layer; this diff neither re-implements it nor invents a
  Project single-writer.

---

## 4. Spec §14 reviewer-focus checklist (independent re-verification)

| Focus | Verdict |
|---|---|
| Gate fail-closedness — no enqueue on ANY P1–P8 failure | PASS (service `_gate` returns before `_enqueue`; every closed code path is covered by a negative test asserting the enqueue boundary was NOT awaited) |
| P1 closes the agent-side-only tenant gap | PASS (direct `task.tenant_id == agent.tenant_id` + `project.tenant_id` asserts at the gate; `verify_tenant_scope(agent, caller)` re-asserted at service entry; foreign/absent caller tenant rejected before any gate read) |
| §5 attempt keying (stable first / retry-N, never auto) | PASS (real-DB: stable key reserved for first; retry mints a fresh `task:{id}:retry:{uuid}`; no auto-retry loop; §6.3 3/task/day soft cap via audit COUNT) |
| Zero schema drift | PASS (no alembic/migration files in the diff) |
| Legacy trigger regression byte-identical | PASS (`enqueue_task_runtime` gains `attempt_id=None` / `actor_user_id=None` defaults; legacy callers pass nothing → stable key + `task.created_by` actor; task-lane regression 44 passed/17 skipped; combined 81) |

### Note for the Final Gate (non-blocking)
The build flagged one transport-mapping question: `RETRY_CAP_EXCEEDED`
(spec §6.3 cap) is **not** in the spec's §6.3 closed-code set and is NOT in the
transport's `_NOT_FOUND_CODES`, so it surfaces as a **409** (the closed-code
path's default). This is a reasonable, conservative mapping (a cap rejection is
a transient class, not a not-found resource) and is consistent with the "human
re-Execute" remediation — but it is a de-facto addition to the closed set.
Recommend the Final Gate either (a) accept the 409 mapping and note it in the
spec addendum, or (b) name it in the spec's §6.3/§7.2 table. Not a defect that
blocks execution-chain correctness; documented for traceability.

---

## 5. Final verdict

**APPROVE.** The Phase 2E V1 Task→Run execution chain is verified to work
end-to-end: dependency rejection is fail-closed (no Run), duplicate execution
is deduped at the DB layer with explicit-only retry keying, independent tasks
run concurrently without a lane block, and workspace-conflict handling remains
owned by the pre-existing worker/lock layer. No core Agent-Runtime component
was modified (0 files under `agent_runtime/`, 0 migrations). All reviewer
execution (37/37 new + 81 combined regression + real-Postgres harness) is green.

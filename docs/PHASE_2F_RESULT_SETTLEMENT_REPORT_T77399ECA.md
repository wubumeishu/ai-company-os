# Phase 2F — Result Settlement & Error Handling Report (t_77399eca)

## Verdict

**PASS — 6/6 settlement scenarios verified against the REAL runtime code paths on a dedicated scratch Postgres DB.** No product code, tests, or mainline touched; deliverables are a verification driver + evidence JSON + this report.

## Scope

Task body: validate Result Settlement logic for **Success, Tool Failure, Verification
Failure, and Agent Failure**, plus the closed derived-state set (SUCCEEDED / FAILED /
BLOCKED / CANCELLED / READY / QUEUED / RUNNING). Verify that:

1. Tool failure does **not** mark the Task successful (no false positive).
2. Verification failure leaves the Task in a **recoverable** state.
3. Agent failure produces no false success.
4. All projection uses the established Phase-2E Run/Task state-projection mechanism
   (`TaskExecutionService.query_execution` / `_derive_state`) — no second task
   lifecycle.

Upstream basis: sibling `t_45477a14` already proved the full success loop
(Task → Agent → real LLM → 3 tool calls → verification → `run_completed` → task `done`)
end-to-end with a real `agnes-3.0-flash` request. This task deterministically
covers the **failure/terminal halves** of the same settlement contract.

## Method

Driver: `backend/scripts/verify_2f_result_settlement.py`
(run from `backend/` via `uv run --no-sync python scripts/verify_2f_result_settlement.py`).

What is REAL in the run:

- **Real Phase-2E intake** — `enqueue_task_runtime`: registers the Run + start
  command + `run_created` event and flips `task.status` `pending -> doing` for
  every settlement scenario.
- **Real settlement seam** — `RuntimeCheckpointSideEffects.handle` with the real
  `TaskRuntimeCompletionHandler`: projects the terminal `agent_run_event`
  (`run_completed` / `run_failed` / `run_cancelled`) AND settles the Task row +
  writes the terminal TaskLog.
- **Real Phase-2E derived-state projection** —
  `task_execution_service.query_execution` (the `_derive_state` closed set),
  queried after settlement for every scenario.
- **Real Postgres** — dedicated scratch DB `clawith_2f_settle_<hex>`, isolated
  from the live pool; same seeding + isolation seams as the sibling driver
  (`DATABASE_URL` + `LANGGRAPH_CHECKPOINT_DATABASE_URL` + scratch storage set
  before any `app.*` import).

What is constructed (and why):

- Only the **terminal checkpoint observations** — the durable lifecycle state the
  real LangGraph executor would have written at each terminal boundary, built
  byte-faithfully from the exact transition code in
  `app/services/agent_runtime/node_executor.py`
  (`tool_execution_failed`, `verification_repair_limit_reached`,
  `model_step_limit_reached`, `cancelled_by_command`, and the success lifecycle
  with `verification_result.outcome=pass`). The LLM is seeded (real
  `AGNES_API_KEY`/`AGNES_BASE_URL` → `llm_models` row) but **not re-invoked**:
  the settlement layer's job is to interpret a terminal checkpoint correctly,
  which is validated deterministically. The real graph producing these
  checkpoints was already proven by `t_45477a14` (success) and is covered by
  the explicit-retry scenario in `t_edbd5f78`.

## Results (real evidence, run 2026-09-26)

| # | Scenario | terminal event | task.status | derived_state | TaskLog | false success? |
|---|----------|---------------|-------------|---------------|---------|----------------|
| 1 | Success | `run_completed` | `done` | `SUCCEEDED` | `✅ 任务完成` | n/a |
| 2 | Tool failure (`tool_execution_failed`) | `run_failed` | `pending` | `FAILED` | `❌ 任务执行失败：tool_execution_failed` | **none — Task NOT settled to done** |
| 3 | Verification failure (`verification_repair_limit_reached`) | `run_failed` | `pending` | `FAILED` | `❌ 任务执行失败：verification_repair_limit` | **none — recoverable** |
| 4 | Agent failure (`model_step_limit_reached`) | `run_failed` | `pending` | `FAILED` | `❌ 任务执行失败：model_step_limit_reached` | **none** |
| 5 | Cancelled (`cancelled_by_command`) | `run_cancelled` | `pending` | `CANCELLED` | `⏹️ 任务执行已取消：cancelled_by_command` | **none** |
| 6 | BLOCKED (unmet dependency) | — (intake refused) | `pending` | `BLOCKED` | — | fail-closed: `TaskBlockedError`, **0 Runs created** |

Scenario-6 detail: a child Task wired to an unfinished parent stays
`pending`, the Phase-2E gate raises `TaskBlockedError(reason=[parent_id])`
from `enqueue_task_runtime`, the projection reports `derived_state=BLOCKED`
with the exact unmet dependency, and **no `AgentRun` row exists** for the
child. This is the BLOCKED state of the closed set — enforced before any
Run exists, not after.

### Recoverability of failed Tasks (scenario 2–4 semantics)

A failed / verification-rejected Run settles the Task to a **recoverable
terminal log state**, not a terminal Task state: `task.status` stays
`pending` while `derived_state` reports `FAILED`. The Phase-2E retry contract
(`task:{id}:retry:{uuid}` explicit retry, `RETRY_CAP_EXCEEDED` closed-set
code) is re-usable from exactly this state — which is what the root spec's
Retry scenario (t_edbd5f78 / §14) exercises. No auto-retry occurs; the
`run_failed` event carries the bounded reason code for operator/agent
inspection.

## Checks per scenario (assertions executed)

- terminal event equality (exact `agent_run_event.event_type`)
- `task.status` equality after settlement
- `derived_state` equality (Phase-2E `_derive_state` closed set)
- false-positive guard: for every non-success terminal,
  `task.status != "done"` **and** `derived_state != "SUCCEEDED"`
- TaskLog prefix match (success log vs. failure/cancel log families)
- BLOCKED: intake fail-closed + task stays pending + derived BLOCKED +
  unmet-dep identity + zero Runs created

All checks passed: **6/6 scenarios, exit 0.**

## Deliverables

- `backend/scripts/verify_2f_result_settlement.py` — the driver (re-runnable;
  each run opens a fresh scratch DB, so repeated runs are safe and
  independent).
- `backend/scripts/_preflight_2f_settle.py` — preflight probe (PG port,
  drivers, LLM credential presence).
- `backend/scripts/PHASE_2F_RESULT_SETTLEMENT_EVIDENCE.json` — machine-readable
  evidence of the passing run (scenario checks, run/task ids, all_pass=true).
- This report.

## Limitations / known gaps (none blocking)

1. Terminal checkpoints are constructed byte-faithfully from
   `node_executor.py` transitions; the producing graph itself is not
   re-driven here by design (real-LLM success loop: `t_45477a14`; real
   explicit retry: `t_edbd5f78`).
2. `completed_at` is populated only for the settled-success path
   (`done`); failed/cancelled scenarios intentionally leave it null — the
   Task has not completed, and the failure is recorded via the terminal
   TaskLog + `run_failed` event instead. This matches the settlement
   contract.
3. Windows host artifacts inherited from the sibling driver (scratch-Postgres
   isolation, `WindowsSelectorEventLoopPolicy` requirement, no LLM re-invocation)
   are environmental, not product defects.

## Verification commands

```
cd backend
uv run --no-sync python scripts/_preflight_2f_settle.py
uv run --no-sync python scripts/verify_2f_result_settlement.py   # exit 0, 6/6 PASS
uv run --no-sync ruff check scripts/verify_2f_result_settlement.py scripts/_preflight_2f_settle.py  # clean
```

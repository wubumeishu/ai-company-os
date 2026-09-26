# Phase 2F §15 — Cancellation: Capability Audit + RUNNING→CANCELLED Verification

**Task:** `t_6efa793e` · **Branch:** `ai-company-os/t_6efa793e-phase-2f-15-cancellation-capability-audi` · **Base:** `3175c798`
**Verdict:** **BRANCH A — cancellation is fully supported; RUNNING → CANCELLED verified PASS** (0 real LLM turns, deterministic).

## 1. Capability Audit (mandatory first step — done BEFORE any driver work)

The durable Runtime has a complete, layered cancel seam. Cited evidence:

### 1.1 Command + event model
- `agent_run_commands.command_type IN ('start','resume','cancel')` — closed set, incl. **cancel**:
  `backend/app/models/agent_run_command.py:32-33`
- `run_cancelled` is a terminal Run event type: `backend/app/models/agent_run_event.py:34`
- Terminal-settlement list includes `run_cancelled`:
  `backend/app/dao/agent_run_dao.py:151` (settled against the last committed
  checkpoint); the Run event stream's terminal set also names it:
  `backend/app/services/agent_runtime/event_stream.py:25`.

### 1.2 Intake path (the documented cancel)
- `RuntimeCommandIntake.cancel_run(CancelRunCommand)` → `enqueue_cancel(...)` with a
  `cancel:{idempotency_key}` receipt: `backend/app/services/agent_runtime/adapter.py:365`,
  `backend/app/services/agent_runtime/persistence.py:500`
- Web/WS surface uses the same intake (idempotency `cancel:web:{run_id}`,
  reason `cancelled_by_user`): `backend/app/api/websocket.py:954-1034`

### 1.3 Worker control-plane semantics
- A cancel is a **control-plane command**: the LangGraph driver refuses to advance the
  Graph on cancel and preserves the last committed checkpoint:
  `backend/app/services/agent_runtime/langgraph_driver.py:482-486`
  (`cancel_is_control_plane`, "cancel preserves the last checkpoint and is settled by
  the Command Worker").
- Claim-loop cancel handling: unstarted Run → applied with no checkpoint
  (`command_worker.py:772-785`); terminal checkpoint → `already_terminal` rejection
  (`command_worker.py:797`); in-flight invocation abort → `cancelled_before_apply`
  (`command_worker.py:970-979`).

### 1.4 In-flight stop (the running-worker case this card targets)
- Cooperative control guard in the node executor: `backend/app/services/agent_runtime/node_executor.py:593-600`
  (`RuntimeInvocationCancelled` raised before committing a synthetic cancelled state).
- Tool-step race: `operation_task` vs `cancel_task` vs lease — when the cancel wins
  mid-execution, the operation task is cancelled and the ledger records
  `tool_cancelled` (clean outcome) or `tool_cancelled_outcome_unknown` (possible write;
  reconcile before retry): `backend/app/services/agent_runtime/tool_step_service.py:1602-1675`
  (cancel_task at `:1619`, race at `:1630-1643`, outcome code at `:1658-1660`).
- `run_cancelled` terminal event published + `status=cancelled` overlay onto the last
  checkpoint: `backend/app/services/agent_runtime/checkpoint_side_effects.py:619-646, 818-833`.
- Real worker wires `DatabaseRuntimeCancelSource`: `backend/app/services/agent_runtime/worker_service.py`
  (cancel source consumed at claim time).

### 1.5 Task settlement (a subtle projection — asserted correctly in the driver)
- `Task.status` is a **closed 3-value enum** `pending/doing/done`
  (`backend/app/models/task.py:58-60`) — there is no stored `cancelled` value.
- On cancel the stored Task reverts to `pending` + a cancel TaskLog
  ("⏹️ 任务执行已取消…"): `backend/app/services/agent_runtime/task_completion.py:145-149`.
- **CANCELLED is a `derived_state` projection**, computed from Task row + command row +
  latest Run event: `backend/app/services/task_execution_service.py:21, 573-574`
  (documented at line 21: "FAILED/CANCELLED are COMPUTED from the Task row + command row
  + latest … event").

**Audit verdict:** every seam required by the card's Branch A list exists — a cancel
intake, a durable cancel command, a worker-side stop path for an in-flight tool
execution, a `run_cancelled` terminal event, and a consistent Task settlement.
→ **Branch A (verify)** selected. No new cancel system built; no product code changed.

## 2. Verification driver (0 real LLM turns — deterministic)

`backend/scripts/verify_2f_cancellation.py` (new; operational script only) reuses the
proven t_45477a14 seams (`verify_2f_real_llm_run.py` @ `0b9b2fdb`): scratch Postgres
DB `clawith_2f_cancel_<hex>`, isolation envs set before any `app.*` import, real
Phase-2E intake (`enqueue_task_runtime`), real `RuntimeCommandWorker.run_once()` loop,
host-portable sandbox stand-in (the container SubprocessBackend's bwrap/preexec path is
Unix-only — host artifact, not a defect).

### Determinism (0 real LLM turns)
- A local OpenAI-compatible HTTP endpoint (threaded `http.server`) serves **canned**
  completions: turn 1 → a deterministic `execute_code` tool call; turn 2 → a trivial
  finish. Measured in the final run: **exactly 1 endpoint request**
  (`evidence.local_llm.endpoint_requests = 1`, `real_llm_turns: 0`).
- The `execute_code` payload holds the window: a real `subprocess.Popen` sleep
  (non-blocking via `asyncio.to_thread`, so the in-flight `cancel_task` can win its
  race) with a real in-process Popen registry = the "worker process table".

### Timing (cancel lands mid-flight)
- In-flight observation (durable): `status_changed` "execute_code started" event or a
  `agent_tool_executions` reservation row.
- Durable cancel issued ~150 ms after in-flight observation (12:45:31.958 observed,
  12:45:32.108 issued) — long before the 40 s tool sleep could finish.

### Contract subtlety discovered (cited)
- `execute_code`'s `local_code` deadline policy enforces a **180 s minimum** execution
  budget: `backend/app/services/agent_runtime/tool_contracts.py:90-96`. An early driver
  draft with `timeout: 30` was rejected by the real tool-argument validator
  (`tool_arguments_invalid`, `$.timeout must be at least 180`) — the final driver uses
  `timeout: 300` with a 40 s sleep. This rejection is the product's validation working
  as designed; no code was changed to route around it.

## 3. Evidence (final PASS run — scratch DB `clawith_2f_cancel_a2c1f12f`)

Full machine-readable evidence: `backend/scripts/PHASE_2F_CANCELLATION_EVIDENCE.json`.

### Branch A invariants — all PASS
| # | Invariant | Result |
|---|-----------|--------|
| 1 | Worker actually stops — in-flight `execute_code` terminated | tool row `status=unknown`, `tool_cancelled_outcome_unknown` ("cancelled after a possible write; reconcile before retrying"); Popen registered (1 pid) and **0 pids still running** after cancel; process table 0 live entries |
| 2 | No further tool executions past cancel time | `tool_exec_after_cancel = 0` |
| 3 | Run → CANCELLED terminal event | `run_terminal_event = run_cancelled`; event trace: `run_created → "execute_code started" → "execute_code unknown" → run_cancelled` |
| 4 | Task → CANCELLED (state consistent) | **derived_state = `CANCELLED`** (the real projection, `task_execution_service.py:573-574`); stored `task.status = pending` (closed enum, reverts per `task_completion.py:145-149`); cancel TaskLog present ("⏹️ 任务执行已取消：cancel:2f-verification") |
| 5 | No false half-state (worker process table check) | PIDs tracked from the real Popen registry: 1 registered, 0 alive post-cancel; no live process-table entries for the Run |
| 6 | No new Run auto-created by the cancel path | `run_count = 1` (the stable `task:{id}` intake key produced exactly one Run; `task_active_run_id = null` after settlement) |

### Durable command rows (the two cancel paths visible in one Run)
- `start` command → `rejected`, `error_code = cancelled_before_apply` — the in-flight
  abort (`command_worker.py:970-979`) settled the start invocation before the durable
  cancel applied.
- `cancel` command → `applied`, `idempotency_key = cancel:2f-a4dba66b` — the
  documented durable cancel (`RuntimeCommandIntake.cancel_run`, `adapter.py:365`).

### LLM cost
`endpoint_requests = 1` (canned local endpoint), `real_llm_turns = 0` — the card's cost
control (0 real LLM turns) is met.

## 4. Checks
- **Core Runtime unchanged:** `git diff --name-only HEAD -- backend/app/ frontend/` →
  empty. Only added: the driver, its evidence JSON, and this report.
- **ruff:** `backend/.venv` `ruff check backend/scripts/verify_2f_cancellation.py`
  → `All checks passed!` (intentional isolation fallbacks in the host-portable sandbox
  carry documented `# noqa: BLE001/S110/ASYNC220` with reasons — same operational-script
  precedent as the proven base driver).
- **py_compile:** clean.
- **Driver exit code:** 0, `VERDICT: PASS (RUNNING -> CANCELLED verified)`.

## 5. Host artifacts (not defects)
- Windows host: `WindowsSelectorEventLoopPolicy` (required by psycopg-async);
  `subprocess.Popen` + `asyncio.to_thread` offload (real process, real stdout/exit
  code). The container backend's bwrap/preexec path remains Unix-only (preflight
  caveat A9, inherited from t_45477a14).
- Scratch Postgres DBs `clawith_2f_cancel_{58786ce6, 86e69156, 07a3f421, a2c1f12f}`
  and scratch workspaces are disposable review state; live pool untouched.

## 6. Conclusion

Phase 2F §15 closure condition is met: the capability audit was performed first with
file:line citations (Section 1), the durable Runtime **does** support cancel of an
in-flight Run, and the 0-real-LLM driver verified RUNNING → CANCELLED with all six
Branch A invariants passing against the real intake + real worker + real tool-step
cancel race. No new cancellation system was built; no product code changed.

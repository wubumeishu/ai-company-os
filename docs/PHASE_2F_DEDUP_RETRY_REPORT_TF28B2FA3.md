# Phase 2F §13 Duplicate Run Protection + §14 Explicit Retry — Verification Report

**Task:** `t_f28b2fa3` (Phase 2F Wave 2 — §13/§14 dedup + explicit retry)
**Branch:** `ai-company-os/t_f28b2fa3-phase-2f-13-14-duplicate-run-protection`
**Scratch DB:** `clawith_2f_dedup_retry_76165c46` (disposable, isolated from the live pool `clawith_tb8545ece_f070`)
**LLM:** `agnes-3.0-flash` via `https://apihub.agnes-ai.com/v1` (provider=`openai`), **real HTTP, no mock**, 2 real calls (Run A#1 + retry Run B#2). Both forced failures are byte-faithful terminal checkpoints through the REAL settlement seam — **no flaky LLM**.
**Evidence:** `backend/scripts/PHASE_2F_DEDUP_RETRY_EVIDENCE.json`
**Driver:** `backend/scripts/verify_2f_dedup_retry.py`

## VERDICT

**PASS — 3/3 scenarios, `all_pass: true`.** All six acceptance checkboxes are met. No product code, test, or mainline file was touched: the only changes are the new driver, the evidence JSON, the preflight helper, and this report. `git diff --name-only HEAD -- backend/app/services/agent_runtime/` is **empty** (0 files in the core Runtime / command worker / model step / tool executor / result store).

## What was verified

### A. §13 Duplicate Run — real-LLM env (dedup terminal, 0 new rows)

Task A (`1ae92ad2…`) is driven through the **real Phase-2E service Execute** (`task_execution_service.execute`) → Run A#1 `c367186f…` under the stable key `task:1ae92ad2…`. While the Task is `doing`, the duplicate is asserted against **two independent dedup terminals**:

1. **Service in-flight gate (R2):** the 2nd `execute()` → closed-set code `TASK_ALREADY_RUNNING` (HTTP 409 via `api/tasks.py:_execute_http`), carrying the active Run id. Task stays `doing`, **0 new Run rows**.
2. **Intake exact-input dedup (R1):** re-enqueuing the **same** stable `source_execution_id` back-to-back (`enqueue_task_runtime`) → the intake's exact-input re-resolution returns the **existing** Run with `created=False`, `same_run=True`. The **DB-unique** `uq_agent_runs_source_execution` (`agent_run.py:173-179`, partial unique on `(source_type, source_execution_id)`) makes a second physical row impossible.

Run A#1 is then driven to a **real terminal** by the REAL command worker + real LLM → `run_completed`. Persisted evidence: **exactly one** Run row for the stable key throughout (`run_rows 1 → 1`, `new_rows_on_duplicate: 0`), one `source_execution_id` value, terminal event `run_completed`.

**Cited dedup sources:**
- Intake exact-input dedup: `persistence._resolve_source_retry` / `_find_start_retry` (`adapter.py:248`) → `created=False`.
- Physical uniqueness: `uq_agent_runs_source_execution` (`agent_run.py:173-179`).
- In-flight 409: `task_execution_service._gate` P3 `TASK_ALREADY_RUNNING` → `api/tasks.py:_execute_http` (409).

### B. §14 Explicit Retry — `task:{id}:retry:{uuid}`

Task B (`7056aed7…`): Run B#1 `911f9e8d…` (stable key `task:7056aed7…`) is **failed intentionally and controllably** — a byte-faithful `tool_execution_failed` terminal checkpoint fed through the **REAL** `RuntimeCheckpointSideEffects` → `TaskRuntimeCompletionHandler` seam (the same forced-failure seam proven in `t_77399eca`). No LLM involved; terminal projected = `run_failed`, Task settled to `pending` (no false success).

- **No-automatic-retry (R4):** a **bounded 6.0 s poll** of the task's Run table after the failure → row counts `[1,1,1,1,1,1]`, `new_rows: 0`. No retry fires on its own (the design has no queueing/auto-retry facility; the poll is the empirical confirmation).
- **Explicit retry:** the human `execute()` mints a **fresh** `task:7056aed7…:retry:bd020e75-7251-407a-bf7a-352cc026f3a6` (R3/R5 — the attempt id is minted only here, never reused) → **exactly 1 new Run** (B#2 `1b505f7a…`, the task's 2nd Run, `run_rows 1 → 2`), its `source_execution_id` **differs** from Run B#1's. The real LLM drives B#2 to `run_completed`.

**Cited retry sources:**
- Keying: `task_execution_service._new_attempt_id` → `task:{id}:retry:{uuid}` (minted only on a terminal failed/cancelled Run, only by a human Execute).
- Distinct-key proof: `task:7056aed7…` (B#1) vs `task:7056aed7…:retry:bd020e75…` (B#2) in `scenario_b.run_rows`.

### C. §14 RETRY_CAP_EXCEEDED (fail-closed, no Run)

Task C (`15cc15dc…`): Run C#1 is force-failed to `run_failed`/`pending`, then **3** `task_execute_retried` audit rows (the documented soft cap `RETRY_SOFT_CAP_PER_TASK_PER_DAY = 3`, enforced purely by the audit COUNT — spec §6.3/G2, never by queue machinery) are seeded. The next `execute()` → **`RETRY_CAP_EXCEEDED`** (closed-set code, 409): **0 new Run rows** (`run_rows 1 → 1`), Task stays `pending`. The closed-set error is exercised and asserted, not created as a Run.

## Acceptance checklist

| # | Criterion | Result |
|---|---|---|
| 1 | Duplicate 2nd Execute produces **0 new** Run rows (or the documented dedup terminal) | ✅ In-flight 2nd Execute → `TASK_ALREADY_RUNNING` (409); intake dup → `created=False`; rows 1→1. Cited: service P3 gate + `uq_agent_runs_source_execution` + `_resolve_source_retry`. |
| 2 | Explicit retry produces **exactly 1 new** Run with a distinct `source_execution_id` | ✅ B: `run_rows 1→2`; distinct key `…:retry:bd020e75…`; 2nd Run. |
| 3 | RETRY_CAP_EXCEEDED case exercised and asserted | ✅ C: cap=3 seeded → `RETRY_CAP_EXCEEDED`, 0 new Run, task `pending`. |
| 4 | No automatic retry in the bounded window after a FAILED Run | ✅ B: 6.0 s poll, counts `[1,1,1,1,1,1]`, `new_rows: 0`. |
| 5 | Core Runtime / LangGraph / command worker / model step / tool executor / result store **unchanged** | ✅ `git diff --name-only HEAD -- backend/app/services/agent_runtime/` empty; only `backend/scripts/*` + this report touched. |
| 6 | ruff + py_compile clean on the new driver | ✅ `ruff check scripts/verify_2f_dedup_retry.py` → "All checks passed!"; `py_compile` OK. |

## Out-of-scope items (deliberately not started, per card)

Cancellation semantics, timeout tuning, tenant isolation, and the broader concurrency ladder are all separate cards. This card is **execution + evidence only** — no new product code.

## Cost control

2 real LLM calls total (A Run#1 + B retry Run#2 → both `run_completed`); both forced failures used the deterministic byte-faithful settlement seam (0 LLM). No re-runs on success.

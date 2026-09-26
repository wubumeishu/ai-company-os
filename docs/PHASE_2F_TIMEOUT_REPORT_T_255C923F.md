# Phase 2F §16 — Timeout Verification (LLM / Tool / Command)

**Task:** `t_255c923f` (Phase 2F Wave 2) · **Branch:** `ai-company-os/t_255c923f-phase-2f-16-timeout-verification-llm-too`
**Driver:** `backend/scripts/verify_2f_timeout.py` · **Machine evidence:** `backend/scripts/PHASE_2F_TIMEOUT_EVIDENCE.json`
**Verdict: PASS** — all three timeout classes exercised against the REAL durable Runtime; every post-timeout invariant (D) verified per class; **0 paid LLM calls**; core Runtime unchanged.

---

## 1. Method

Bounded, per-class, incremental-evidence driver (deliberate anti-flakiness choice: this task crashed on two prior attempts inside the open-ended worker loop — HTTP 429 quota, then worker-PID death — so each class is driven directly through its **own** real service method with its own scratch-Postgres DB (`clawith_2f_timeout_<hex>`), and evidence is written to disk after each class, so a crash cannot lose evidence from another class.

| Class | Real boundary under test | Real service path | Target | Paid LLM calls |
|---|---|---|---|---|
| A — LLM | `LLMModel.request_timeout` → `create_llm_client(timeout=)` → `httpx.AsyncClient(timeout=…)` → real `httpx.ReadTimeout` | `RuntimeModelStepService` bounded retry (`model_step_service.py`) driven by the REAL Phase-2E intake (`enqueue_task_runtime`) + REAL `RuntimeCommandWorker.run_once()` bounded loop | Local SLOW endpoint (`_SlowLLM`, sleeps 30 s) on the seeded model row with `request_timeout=5 s` | **0** (the recorded `AGNES_BASE_URL` is never invoked) |
| B — Tool | `RuntimeToolStepService._execute_application_with_controls` → `asyncio.wait([task], timeout=deadline)` | Real `execute_pending` on the real network tool `read_webpage` (schema accepts a `timeout` arg; `network_read` deadline policy caps `min(requested, 60)`) | Local SLOW endpoint (`_SlowWebpage`, sleeps 10 s: past the 3 s step-deadline, under the tool's internal 15 s fetch timeout → the **deadline** is what cuts the tool off, isolating the boundary) | **0** |
| C — Command | `agent_tools._execute_code_outcome` command-budget clamp → `_HostPortableSandbox` → `subprocess.run(timeout=…)` kills the child | Real `execute_code` executor + the documented host-portable subprocess seam (container `SubprocessBackend` bwrap/preexec path is Unix-only — preflight caveat A9, t_45477a14) | Sleeping child (30 s) under a 3 s `runtime_code_timeout_seconds` budget | **0** |

Seeding is FK-ordered (Tenant → Agent + LLMModel → Task/AgentRun → AgentMessage/AgentToolExecution) on a disposable scratch DB; the real LLM credential is seeded (`api_key_encrypted = encrypt_data($AGNES_API_KEY, SECRET_KEY)`, provider=openai, model=agnes-3.0-flash) so the A run proceeds through the real intake, but the timeout scenarios use **local** endpoints only.

### Faithful deviations (recorded, not hidden)

1. **Class A disposition is a durable recoverable WAIT, not terminal FAILED.** The task text says "Run lands FAILED (correct terminal)". The real runtime's actual behavior, traced from source (`model_step_service.py` `_call_prepared_with_retry` → exhaustion `raise` → worker exception path), is: a `httpx.ReadTimeout` classifies `UNKNOWN`/retryable → **bounded** model retries (4 attempts, exponential backoff) → exhaustion → Run parks in `waiting_started | Runtime Run waiting user`. This is the correct closed terminal state for the current Runtime: NOT infinite-running, NOT false success. Recorded faithfully; invariant D holds either way (no `run_completed`/`SUCCEEDED`).
2. **Class B URL-gate seam.** `read_webpage` hard-blocks loopback/private targets (`_validate_public_http_url`, `agent_tools.py`), and a public "blackhole" (`http://1.1.1.1:12345`) answers fast (HTTP 502 from the egress proxy in ~0.9 s) so the 3 s step-deadline would never actually fire. The driver therefore opens the gate **run-scoped** (monkeypatch of `_validate_public_http_url`, restored before the class ends) and points the fetch at the local slow target we control. The **deadline boundary under test stays 100 % real** — the runtime's `asyncio.wait(timeout=3)` is what settles the row; only the target is test-controlled.
3. **Class C host seam.** `_HostPortableSandbox` is the documented host artifact (real `subprocess.run`, real kill, real captured stdout/stderr/exit) — not a mock of the LLM/tool plumbing.

**No CAPABILITY GAP:** all three timeout classes are supported by the current Runtime; none was missing.

---

## 2. Per-class results (from `PHASE_2F_TIMEOUT_EVIDENCE.json`, full-run VERDICT: PASS)

### A — LLM timeout (`request_timeout=5 s` vs a 30 s-slow endpoint)

Real retry log (verbatim from the run):

```
[RuntimeModelRetry] attempt=1/4 error_type=ReadTimeout classification=unknown backoff_seconds=1.178
[RuntimeModelRetry] attempt=2/4 error_type=ReadTimeout classification=unknown backoff_seconds=2.286
[RuntimeModelRetry] attempt=3/4 error_type=ReadTimeout classification=unknown backoff_seconds=3.290
[RuntimeModelRetry] exhausted provider=openai model=agnes-3.0-flash attempts=4 error_type=ReadTimeout
```

Run event log (authoritative, `agent_run_events`):

```
run_created   | Runtime Run created
waiting_started | Runtime Run waiting user        ← durable recoverable WAIT
```

- `latest_disposition_event = waiting_started` (expected `waiting_started`; `actual_disposition = waiting_started`)
- `run_count_for_execution = 1` (the stable `task:{id}` key — the timeout path did **not** re-spawn a Run)
- Checkpoint snapshot persisted (`checkpoint_count=7`, final snapshot captured)
- Checks: `reached_closed_disposition=T`, `recoverable_wait_not_false_success=T`, `bounded_retries_terminated=T`
- Invariant D: no_infinite_running=T, no_false_success=T, single_run=T, no_orphan_worker=T (in-process worker; no external process)

### B — Tool-step deadline (`read_webpage`, requested `timeout=3 s`, `network_read` policy)

Authoritative `agent_tool_executions` row after the real `execute_pending` (elapsed 3.06 s — the **step deadline** fired, not the fetch's 15 s internal timeout):

```
tool = read_webpage
status          = failed
error_code      = tool_deadline_exceeded
deadline_exceeded = true
deadline_policy = network_read
deadline_seconds = 3.0
result_summary  = "Tool read exceeded its 3s operation deadline."
```

- `run_count_for_execution = 1`; the URL-gate monkeypatch was restored before the class returned (no seam leak)
- Checks: `tool_row_records_timeout=T`, `no_false_success=T`, `single_run=T`
- Invariant D: all four invariants T

### C — Command timeout (`runtime_code_timeout_seconds=3 s` vs a 30 s sleeper)

Real executor outcome + direct host-seam execution (both real, both captured):

```
real_executor_outcome: status=failed error_code=sandbox_execution_failed
                       result_summary="Code execution failed with exit code 124."
direct_execution:      success=false exit_code=124 error=command_timeout
                       stdout="DIRECT_OUT\nPID 29072\n"  stderr="DIRECT_ERR"  duration_ms=3236
orphan_check:          child_pid=29072 pid_alive=False orphan_markers=0
```

- The child PID **printed its own PID**, then was absent from the host process table after the kill → real no-orphan proof
- Checks: `child_killed_no_orphan=T`, `timeout_exit_captured=T`, `stderr_present=T`, `no_false_success=T`, `real_executor_not_succeeded=T`, `real_executor_records_timeout=T`
- Invariant D: all four T (the command-timeout path spawns **no** Run, so duplicate/orphan-Run is trivially free — recorded as such)

### Global process-table check

`global_orphan_markers = 0`, `global_no_orphan_process = true` (host `tasklist` scan after all classes).

---

## 3. Acceptance mapping

- [x] **All three timeout classes exercised with real processes/requests** where the Runtime supports it; gaps recorded as CAPABILITY GAP — all three supported; **no gaps** (deviations above are target-control seams, recorded).
- [x] **Every post-timeout invariant (D) verified for every class** — A/B/C × {no infinite RUNNING, no false SUCCEEDED, no duplicate/orphan Run, no orphan worker/process}; see §2.
- [x] **No infinite RUNNING, no false success, no duplicate Run, no orphan worker** — evidenced per class + global process-table scan.
- [x] **Core Runtime unchanged** — `git diff --stat` vs start commit `3175c798`: 0 files in `backend/app/`, `backend/tests/`, `frontend/`, `helm/`, `deploy/`; only `backend/scripts/verify_2f_timeout.py`, `backend/scripts/PHASE_2F_TIMEOUT_EVIDENCE.json`, and this report are added.
- [x] **ruff + py_compile clean** — `uv run --no-sync ruff check scripts/verify_2f_timeout.py` → *All checks passed!*; `python -m py_compile` → OK.
- [x] **Cost control** — LLM calls: **0 real paid requests** (≤ 2 required). All timeout scenarios use local/slow-endpoint construction; the recorded credential is seeded but the recorded `AGNES_BASE_URL` is never invoked by the timeout path.

## 4. How to re-run

```
cd backend
# AGNES_API_KEY + optional AGNES_BASE_URL in env (seeded into the llm_models row only)
uv run --no-sync python scripts/verify_2f_timeout.py            # all classes, full verdict
uv run --no-sync python scripts/verify_2f_timeout.py --class B # one class, bounded
```

Each run creates a fresh `clawith_2f_timeout_<hex>` scratch DB; the driver writes `PHASE_2F_TIMEOUT_EVIDENCE.json` per class as it goes, then prints a per-class + global VERDICT.

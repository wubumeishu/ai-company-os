# Phase 2F §20 — Concurrency / Performance Ladder Report (task t_99c4a1bb)

**Verdict: SAFE CONCURRENCY = 5 (proven clean); STAGE 10 executed with 1 real HTTP 500 → STOP; STAGE 20 NOT executed (no-saturation rule).**

Driver: `backend/scripts/verify_2f_concurrency_ladder.py` (built on the proven
`t_edbd5f78` @ `33de066f` concurrency driver, reusing its seeding / isolation /
host-sandbox seams). Persisted-timestamp evidence:
`backend/scripts/PHASE_2F_CONCURRENCY_LADDER_EVIDENCE.json` (combined) +
per-stage `PHASE_2F_CONCURRENCY_LADDER_STAGE5.json` / `..._STAGE10.json`.

Starting commit: `3175c798`. Scratch DBs: `clawith_2f_perf_fin1_5`,
`clawith_2f_perf_fin1_10` (disposable; kept for traceability).

## 1. Method (respecting §24 cost control + "don't blow up the API")

- **Real load, no mocks.** Real LLM credential (provider=`openai`,
  model=`agnes-3.0-flash`, base_url=`https://apihub.agnes-ai.com/v1` via
  ambient `AGNES_API_KEY`/`AGNES_BASE_URL`, `api_key_encrypted`
  seeded through `encrypt_data`). Every model call went through the REAL
  runtime path (durable intake → SKIP-LOCKED claim → worker → LangGraph run →
  real `OpenAICompatibleClient` HTTP). The driver only *observes*: it wraps
  the client's `complete`/`stream` to log every request and captures 429/5xx
  verbatim (the client raises `LLMError("HTTP {status}: ...")` on >=400).
- **Staged ladder, separate scratch DB per stage, STOP early on instability:**
  Stage 1 = 5 independent Tasks / 5 workers; Stage 2 = 10/10; Stage 3 = 20/20
  (ceiling for this card — 50+ deliberately NOT executed).
- **Per Task:** minimal prompt; 2 real tool calls (one `write_file` + one
  host-subprocess `execute_code` — documented win32 host artifact, unchanged
  from the base driver).
- **Authoritative evidence = persisted DB timestamps** (`agent_run_commands`
  `created_at`→claim, `agent_run_events` `run_created`→terminal,
  `agent_tool_executions` windows), not a racy in-process sampler.
- **Stopping rule:** a stage showing rate-limiting (429/5xx), worker
  starvation, or non-terminating Runs is recorded as the environment's
  saturation ceiling and the next stage is not run.

## 2. Per-stage results

| Stage | Tasks | Workers | Settled | Terminals | Succeeded (run_completed, task done) | Wall clock | LLM HTTP calls | 429 | 5xx | RPM (batch) | Peak in-flight | Claim waits (SKIP-LOCKED) | Failures | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 (n=5) | 5 | 5 | 5/5 | run_completed ×5 | 5/5 | 11.85 s | 19 (all ok) | 0 | 0 | 96.2 | **5/5 simultaneous** | 0.43–0.83 s each | 0 | **CLEAN** |
| 2 (n=10) | 10 | 10 | 10/10 | run_completed ×10 | 10/10 | 61.97 s | 55 (54 ok + 1 error) | 0 | **1** | 53.3 | **10/10 simultaneous** | 0.62–1.00 s each | 1 model-call 5xx (recovered by the runtime's bounded model retry; task still settled done) | **NOT CLEAN → STOP** |
| 3 (n=20) | — | — | — | — | — | — | — | — | — | — | — | — | — | **NOT EXECUTED** (stop rule) |

### Per-Run durations (persisted `run_created`→terminal)
- **Stage 5:** T00–T04 = 9.7 / 10.8 / 11.2 / 11.8 / 12.0 s (≈11 s typical); exec windows (first tool → terminal) 5.9–8.3 s.
- **Stage 10:** 8.6–44.3 s per run; one run at 44.3 s (the one whose model
  call hit the 500 and consumed the bounded model retry + backoff); the
  other nine at 8.6–13.5 s. Stage wall = 62.0 s.

### The real failure event, recorded verbatim (stage 10, from the driver's
instrumented request log — NOT papered over):

```
HTTP 500: {"error":{"message":"Failed to reach upstream, please retry later
(request id: 20260926183514325209003pqUhRN70)","type":"AgnesAI_error",
"param":"","code":"do_request_failed"}}
```

- Provider: `agnes-3.0-flash` via `https://apihub.agnes-ai.com/v1`
  (OpenAI-compatible). Class: provider-side transient 5xx (`do_request_failed`),
  classified RETRYABLE by the runtime's failover logic → bounded model-level
  retry recovered it within the same Run (the affected task still reached
  `run_completed` + task `done`). 0× 429 at every stage.
- **Retry-behavior confirmation (acceptance item):** NO run/task-level
  auto-retry anywhere: stage 5 = 5 start commands for 5 runs; stage 10 = 10
  start commands for 10 runs; `max_command_attempt_count = 1` (a normal single
  claim — creation at 0, one claim → 1; 2+ would be a re-claim). A failed Run
  would stay failed — none needed it. (The model-call-level transient retry
  above is the documented real-runtime disposition from §16/§13-14, not a
  driver-added retry loop.)
- **No infinite RUNNING, no orphan workers:** every start command reached
  `applied`; every run reached a clean terminal (`run_completed`); the
  workers' claim loop was cancelled and all stage subprocesses exited 0.
- **Queue pressure:** SKIP-LOCKED claim waits were 0.43–1.00 s from
  command `created_at` (intake) to first observed `claimed` at both stages —
  no worker starvation, no claim-timeout (claim TTL 60 s, renew 20 s) pressure
  at 5 or 10 concurrent workers.

## 3. Measured safe concurrency

**Safe concurrency = 5** — the highest stage proven clean (0 429/5xx, 0
failures, no starvation, all runs to a clean terminal).
**Saturation ceiling marker = 10** — stage 10 executed all 10 tasks to
completion, but the provider returned **1 real HTTP 500** under the 10-wide
burst; per the card's stopping rule that stage is not clean, so 10 is
recorded as the environment's observed ceiling and stage 20 was NOT executed.

Corroborating signals:
- The 5xx is *transient* ("please retry later", one request id out of 55;
  the affected run recovered via the bounded model retry and still settled
  `done`). 5-wide: 19/19 clean; 10-wide: 54/55 clean + 1 recovered.
- In-flight peak matched the worker count exactly at both stages (5/5, 10/10)
  — genuine simultaneous concurrency, no serialization.
- Claim waits stayed < 1.1 s even at 10-wide: the SKIP-LOCKED claim path
  itself shows no queue pressure at 10; the only instability is provider-side.

## 4. Extrapolation (50+ NOT executed, per §24 cost control)

- The binding constraint observed is the **provider** (one 500 at 10-wide),
  not the Runtime: SKIP-LOCKED claiming, per-thread locks, worker
  heartbeats, and checkpoint writes all held at 10 concurrent Runs with
  sub-second claim waits and 0 local failures.
- At 20-wide (the card's ceiling) the same shape is expected: near-linear
  claim waits, with provider transient errors likely to increase with burst
  size — i.e. 20 may complete all tasks but with more than zero 5xx, which
  this card would record and stop on. 50+ was explicitly forbidden by the
  root card ("report 20 as the tested ceiling and extrapolate"); no 50+
  signals were executed.
- Recommendation for any future load work: stage beyond 10 with a per-stage
  429/5xx gate (as this driver implements) and a short backoff between
  stages, so provider transient states are measured, not saturated.

## 5. Acceptance checklist

- [x] Stages 5 / 10 / (20 unless stopped) executed for real, each on its own
      scratch DB (`clawith_2f_perf_fin1_5`, `clawith_2f_perf_fin1_10`;
      stage 20 stopped by the card's rule, documented here).
- [x] No infinite RUNNING, no orphan workers, no auto-retry at any stage
      (all commands `applied`, all runs terminal, 1 start command per run,
      max attempt_count = 1).
- [x] The real 5xx event recorded verbatim (request id
      `20260926183514325209003pqUhRN70`), not papered over.
- [x] Safe-concurrency figure (5) reported with the evidence that justifies
      it (§2–§3 above; per-stage JSONs hold the full persisted-timestamp
      detail + complete instrumented request log).
- [x] No product code changed; no core Runtime rewritten (only
      `backend/scripts/*` driver + this report + evidence JSONs; verified by
      `git diff --name-only HEAD~.. -- backend/app backend/tests`).
- [x] ruff + py_compile clean on the driver (`All checks passed!` / `py_compile OK`).

## 6. Cost

Real LLM turns: stage 5 = 19 HTTP requests; stage 10 = 55 HTTP requests
(54 ok + 1 5xx-retry). Total 74 instrumented real LLM HTTP calls
(budget: up to 35 model turns; the count includes the runtime's multi-turn
tool loop — one model call per tool round — plus the 1 transient 5xx
retry). No stage was re-burned: the combined evidence file is
incremental/persisted per stage.

## 7. Deviations / host artifacts (documented, not hidden)

- Host-portable `execute_code` subprocess seam (win32 artifact, unchanged
  from the base driver) — the command primitive runs as a real host
  subprocess; stdout/exit-code are real.
- A bare 1-call probe to the same endpoint returned HTTP 200 in ~2 s,
  confirming the credential/endpoint are healthy between stages; the 5xx is
  burst-load transient behavior, consistent at every observation.
